#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FPK更新 —— 自建应用的更新台（后端服务）

设计约束（改前必读）
--------------------
1. **只用 Python 标准库**。fnOS 上没有可用的 pip 环境，任何第三方依赖都会让
   装机变脆。本文件不 import 任何非标准库模块。
2. **本服务以 root 运行**（config/privilege: run-as=root）。因为飞牛应用中心的
   升级通道（unix socket /var/run/com.trim.app.center.sock）只对 OfficialAppUsers
   组开放，第三方应用用户拿不到权限。因此这里的能力被硬限死在：
     · 只读 /var/apps 下白名单应用的版本与图标
     · 只向 GitHub 上白名单仓库的 Release 取包
     · 只调用应用中心的「暂存 / 升级」RPC
   没有「安装任意 fpk」「卸载」「上传文件」这类入口，白名单来自数据目录的
   apps.json（包内默认 3 个自建应用），网页端无法增删。
3. **升级走的是应用中心官方的就地升级通道**（与用户在应用中心点「更新」完全
   同一条路，日志 class=upgrade）：保数据、不重装。实测见项目 README。
   ⚠️ 绝不使用 `appcenter-cli install-local` —— 它在 fnOS 上是「卸载再安装」，
   重装步骤失败会把应用连同数据一起抹掉。
4. **升级前必须校验 sha256**（取自 GitHub Release 的 asset digest），校验不过
   一律不升级。
5. 同一时刻只允许一个升级任务（全局锁），失败不自动重试。
"""

import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# ───────────────────────────── 运行环境 ─────────────────────────────

APP_NAME = "fpk-update"
# 本应用自身的更新来源（**不在**「被管理的应用」列表里，单独一条链路）。
# 为什么必须单独：它有三个别处没有的特殊性 ——
#   ① 更新它 = 应用中心会重启本进程、页面上的 js/css 立刻过期（得自动刷新界面）；
#   ② 「全部更新」绝不能带上它，否则队列里排在后面的应用会被连累着中断；
#   ③ 它没有「本机已安装」这道门（自己当然装了），而列表里每条都要过这道门。
# 仓库可在「设置 → 应用」里改，默认就是打包本应用的那个仓库。
SELF_DEFAULT_REPO = os.environ.get("FPKU_SELF_REPO", "") or "mickeyttsc/fpk-update"

HOST = os.environ.get("FPKU_HOST", "0.0.0.0")
PORT = int(os.environ.get("FPKU_PORT", "18600"))
STATIC_DIR = os.environ.get("FPKU_STATIC", "/dev/null")
APPS_DEFAULT = os.environ.get("FPKU_APPS_DEFAULT", "")
APPS_USER = os.environ.get("FPKU_APPS_USER", "")
VAR_DIR = os.environ.get("FPKU_VAR", "/tmp")
HOME_DIR = os.environ.get("FPKU_HOME", "/tmp")
DEFAULT_DOWNLOAD_DIR = os.environ.get("FPKU_DOWNLOAD_DIR", os.path.join(VAR_DIR, "downloads"))
APPS_ROOT = os.environ.get("FPKU_APPS_ROOT", "/var/apps")

DAEMON_SOCKET = "/var/run/com.trim.app.center.sock"
GH_TOKEN_FILE = os.path.join(VAR_DIR, "github-token.txt")
LAST_CHECK_FILE = os.path.join(VAR_DIR, "last-check.json")
JOB_FILE = os.path.join(VAR_DIR, "job.json")
SETTINGS_FILE = os.path.join(VAR_DIR, "settings.json")
PKG_REGISTRY_FILE = os.path.join(VAR_DIR, "downloads.json")

MAX_BODY_BYTES = 1 << 20       # 请求体上限（本应用的请求都是几十字节的 JSON）
CHECK_INTERVAL_MIN = 60          # 后台自动「检测」周期（只查版本，绝不自动升级）
HTTP_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 900

CST = timezone(timedelta(hours=8))

# 白名单 / 设置 / 安装包台账 的读改写共用这一把锁。用 RLock 而非 Lock：
# 这些操作彼此会调用（例如清点台账时读白名单），可重入才不会自己把自己锁死。
_lock = threading.RLock()
# 检测的并发闸门（同一时刻只允许一轮检测在跑）
_check_lock = threading.Lock()
# 检测进度：让界面在检查期间显示「正在检查 x/y」并保持快轮询。
# 为什么需要：串行检查 6 个应用实测要 6.1 秒，而原来是「前端 await 到底、期间零反馈」——
# 用户感受到的就是「点一下卡六七秒」。现在改成后台跑 + 实时进度。
_check_state = {"running": False, "done": 0, "total": 0, "current": "",
                "trigger": "", "started_at": None, "finished_at": None}


def log(msg):
    """写运行日志。

    ⚠️ 只输出到 stdout，**不要**再自己往文件里追加 —— cmd/main 启动服务时已经把
    stdout/stderr 重定向到同一个 $TRIM_PKGVAR/fpk-update.log，两边都写会让每行
    出现两次（实测踩过）。/api/log 读的就是那个文件。
    """
    ts = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 临时名必须**每次调用都唯一**：原来只用 pid，同一进程里两个线程同时写同一个
        # 文件时会撞在一起（一个把另一个的临时文件改名走了）→ 内容交错或整条更新丢失。
        # 加线程号 + 单调时钟纳秒：同一线程两次调用不可能落在同一个纳秒上。
        tmp = "%s.tmp.%d.%d.%d" % (path, os.getpid(), threading.get_ident(),
                                   time.monotonic_ns())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        log("保存 %s 失败: %s" % (path, exc))


# ───────────────────────────── 版本比较 ─────────────────────────────

_VER_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:-(\d+))?$")


def norm_version(v):
    v = (v or "").strip()
    if v.startswith(("v", "V")):
        v = v[1:]
    return v


def split_version(v):
    """'8.0.31-1' -> ((8,0,31), 1)。不符合规范时返回 None。"""
    m = _VER_RE.match((v or "").strip())
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    it = int(m.group(2)) if m.group(2) else 0
    return nums, it


def cmp_version(a, b):
    """a>b -> 1；a<b -> -1；相等 -> 0。无法比较时返回 None。"""
    sa, sb = split_version(a), split_version(b)
    if sa is None or sb is None:
        return None
    na, ia = sa
    nb, ib = sb
    n = max(len(na), len(nb))
    na = na + (0,) * (n - len(na))
    nb = nb + (0,) * (n - len(nb))
    if na != nb:
        return 1 if na > nb else -1
    if ia != ib:
        return 1 if ia > ib else -1
    return 0


def is_newer(candidate, current):
    c = cmp_version(candidate, current)
    return c == 1 if c is not None else False


# ───────────────────────────── 白名单 / 应用 ─────────────────────────────

APPNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def load_whitelist():
    """读被管理的应用列表。

    每条只需给出「来源」即可：
      {"repo": "owner/name"}                      ← 最简，appname/显示名/版本都自动识别
      {"repo": "owner/name", "appname": "xxx"}     ← 想固定 appname 时写
      {"repo": "owner/name", "tag": "v1.2.3"}      ← 钉住某个 Release
      {"repo": "owner/name", "asset": "*x86*.fpk"} ← 多个附件时指定挑哪个
      {"repo": "owner/name", "allow_prerelease": true}
      {"url": "https://.../x.fpk"}                 ← 直链（非 GitHub 也行）
    """
    data = load_json(APPS_USER)
    if not isinstance(data, dict):
        data = load_json(APPS_DEFAULT) or {}
    apps = data.get("apps")
    if not isinstance(apps, list):
        return []
    out = []
    for item in apps:
        if not isinstance(item, dict):
            continue
        src = normalize_source(item)
        name = src["appname"]
        # 自身更新不在被管理列表里（见 SELF_DEFAULT_REPO 的注释）。
        # 这里兜底过滤：即使有人手工把那条记录写回 apps.json，也不会混进列表、
        # 不会出现在「全部更新」里，更不会绕过下面的 /api/self 链路。
        if name == APP_NAME or (src["repo"] or "").split("/")[-1] == APP_NAME:
            continue
        if name and not APPNAME_RE.match(name):
            continue
        if not src["repo"] and not src["url"]:
            continue
        if src["repo"] and not REPO_RE.match(src["repo"]):
            continue
        out.append({
            "appname": name,
            "display": str(item.get("display") or ""),
            "repo": src["repo"],
            "url": src["url"],
            "tag": src["tag"],
            "asset": src["asset"],
            "allow_prerelease": src["allow_prerelease"],
            "note": str(item.get("note") or ""),
        })
    return out


def _whitelist_doc():
    """读回原始 apps.json（保留用户的其它字段与注释性键），供增删时写回。"""
    data = load_json(APPS_USER)
    if not isinstance(data, dict):
        data = load_json(APPS_DEFAULT)
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("apps"), list):
        data["apps"] = []
    return data


def save_whitelist(doc):
    save_json(APPS_USER, doc)


def remember_appname(repo_or_url, appname, display, version):
    """把识别出来的 appname/显示名写回 apps.json（只填空缺，不改用户写过的值）。

    为什么要写回：识别出来一次就记住，后续动作/图标/校验都用得到它，
    用户也能在文件里看到实际识别成了什么。
    """
    if not appname:
        return
    with _lock:
        doc = _whitelist_doc()
        changed = False
        for item in doc["apps"]:
            if not isinstance(item, dict):
                continue
            src = normalize_source(item)
            key = src["repo"] or src["url"]
            if key != repo_or_url:
                continue
            if not src["appname"]:
                item["appname"] = appname
                changed = True
            if not item.get("display") and display:
                item["display"] = display
                changed = True
        if changed:
            save_whitelist(doc)
            log("已识别并记下 appname=%s（来源 %s，版本 %s）" % (appname, repo_or_url, version))


def installed_version(appname):
    path = os.path.join(APPS_ROOT, appname, "manifest")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("version"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"')
    except OSError:
        return None
    return None


def installed_display(appname):
    path = os.path.join(APPS_ROOT, appname, "manifest")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("display_name"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"')
    except OSError:
        pass
    return None


def icon_path(appname):
    base = os.path.join(APPS_ROOT, appname)
    for name in ("ICON_256.PNG", "ICON.PNG", "ICON_256.png", "ICON.png"):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return None



# ───────────────────────────── GitHub Release ─────────────────────────────

def gh_token():
    try:
        with open(GH_TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def write_token(tok):
    """保存 GitHub 访问令牌（只读即可）。空字符串 = 删除，回退匿名访问。"""
    tok = (tok or "").strip()
    if not tok:
        try:
            os.unlink(GH_TOKEN_FILE)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(GH_TOKEN_FILE), exist_ok=True)
    tmp = GH_TOKEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, GH_TOKEN_FILE)


def token_hint():
    """只回显令牌尾部，便于用户确认填的是哪一个，又不泄露完整值。"""
    tok = gh_token()
    if not tok:
        return ""
    return "…%s（%d 位）" % (tok[-4:], len(tok))


GH_CACHE_TTL = 15               # Release 列表缓存秒数
_gh_cache = {}                  # repo -> (ts, data)

GH_RETRIES = 2                  # 瞬时故障重试次数
GH_RETRY_WAIT = 0.8             # 首次重试等待（之后线性增长）


def gh_get(url, retries=GH_RETRIES):
    """请求 GitHub API，瞬时故障自动重试。

    为什么必须有重试：实测遇到过一次「find_release 返回空 → 界面报『仓库没有可用的
    Release』」，而几秒后完全相同的调用一切正常（GitHub 侧瞬时抖动）。
    没有重试的话，这种抖动会变成一条**误导用户且会持续到下次检测**的失败状态
    （错误会被写进 last-check 缓存）。重试把这类抖动挡在用户看不见的地方。

    4xx（除 429）是确定性错误，重试没意义，原样抛给上层翻译成人话。
    """
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "fpk-update/1.0")
        tok = gh_token()
        if tok:
            req.add_header("Authorization", "token %s" % tok)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = exc
        if attempt < retries:
            log("GitHub 请求失败（第 %d 次）：%s —— %.1f 秒后重试"
                % (attempt + 1, last, GH_RETRY_WAIT * (attempt + 1)))
            time.sleep(GH_RETRY_WAIT * (attempt + 1))
    raise last



def gh_releases(repo, force=False):
    """取一个仓库的 Release 列表，带 15 秒缓存。

    为什么加缓存：界面上的「获取版本 → 预览 → 添加」是一串操作，每一步原来都重新打
    一次 GitHub（实测 0.7~1.4 秒/次，用户感受就是「点一下卡一秒」）。同一份列表在
    十几秒内复用，既快又不白烧额度。TTL 取得很短（15 秒）：刚发布的版本最多晚十几秒
    出现，与「检查更新」本身 10 秒的节流是同一个量级，不会让人以为「检测不出来」。
    """
    now = time.time()
    hit = _gh_cache.get(repo)
    # force：绕过缓存。为什么需要 —— 刚发布一个版本、立刻点「检查更新」时，
    # 15 秒内的旧列表会让界面坚持说「已是最新」，看起来像检测坏了。
    if hit and not force and now - hit[0] < GH_CACHE_TTL:
        return hit[1]
    data = gh_get("https://api.github.com/repos/%s/releases?per_page=50" % repo)
    if not isinstance(data, list):
        raise ValueError("GitHub 返回格式异常")
    with _lock:
        _gh_cache[repo] = (now, data)
        if len(_gh_cache) > 50:
            for k in list(_gh_cache)[:-50]:
                _gh_cache.pop(k, None)
    return data


def _gh_error_message(exc):
    """把 GitHub 的 HTTP 错误翻译成用户能照着做的提示。"""
    code = getattr(exc, "code", None)
    headers = getattr(exc, "headers", None)
    remaining = ""
    try:
        remaining = (headers.get("x-ratelimit-remaining") or "") if headers else ""
    except Exception:  # noqa: BLE001
        remaining = ""
    if code == 403 and remaining == "0":
        if gh_token():
            return ("GitHub 访问令牌的额度也用尽了（5000 次/小时）—— 稍后再试，"
                    "或换一个令牌")
        return ("GitHub 匿名额度已用尽（60 次/小时，按出口 IP 计算，同一个 IP 上"
                "别的程序也会消耗它）。请在「更多 → GitHub 访问令牌」里填入一个"
                "令牌（只读即可，5000 次/小时）后重试")
    if code == 403:
        return "GitHub 拒绝访问（403）—— 若是限流请稍后重试；也可能是令牌权限不足"
    if code == 404:
        return "仓库不存在或不可见（404）"
    if code == 401:
        return "GitHub 访问令牌无效或已过期（401）—— 请重新填写"
    return "GitHub 返回 HTTP %s" % code



# ───────────────────────────── 远端 fpk 的 manifest（通用性的关键） ─────────────────────────────
#
# 为什么必须读 fpk 自己的 manifest：
#   之前是「从 tag 猜版本、按文件名约定挑附件」，这就要求每个仓库都遵守我们的命名习惯，
#   换个仓库（tag 叫 release-2026.10、附件叫 xxx_linux_amd64.fpk）就认不出来。
#   而 fpk 内层的 manifest 才是 fnOS 安装时真正读的那份 —— 以它为准，tag 和文件名怎么
#   起都无所谓，任何仓库都能用。
#
# 怎么做到不必下载整个包：
#   fpk 是 gzip 包，而 fnOS 要求外层第一条必须是 manifest（fnpack 与本仓门禁都遵守）。
#   于是用 HTTP Range 只取开头几百 KB，做「部分解压」就能拿到 manifest。
#   实测 GitHub Release 直链支持 Range。

MANIFEST_CACHE_FILE = os.path.join(VAR_DIR, "manifest-cache.json")
_manifest_cache = None

# 常见「滚动 tag」——不是版本号，按约定跳过（除非用户显式钉 tag）
ROLLING_TAGS = {"latest", "nightly", "dev", "snapshot", "rolling", "edge", "canary", "test"}

ARCH_KEYS = {
    "x86": ["x86_64", "amd64", "x86", "x64", "win64"],
    "arm": ["aarch64", "arm64", "arm"],
}


def device_arch():
    """本机架构，用于在多个附件里挑对的那个。"""
    m = (os.uname().machine or "").lower() if hasattr(os, "uname") else ""
    if m in ("aarch64", "arm64", "armv8l", "armv7l"):
        return "arm"
    return "x86"


def _load_manifest_cache():
    global _manifest_cache
    if _manifest_cache is None:
        d = load_json(MANIFEST_CACHE_FILE)
        _manifest_cache = d if isinstance(d, dict) else {}
    return _manifest_cache


def _save_manifest_cache():
    # 读改写加锁：检测线程与网页请求可能同时触发读 manifest 并写这份缓存
    if _manifest_cache is not None:
        with _lock:
            save_json(MANIFEST_CACHE_FILE, _manifest_cache)


def _parse_manifest(text):
    """解析 manifest 的 key = value 行。"""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().lower()
        v = v.strip().strip('"')
        if k and k not in out:
            out[k] = v
    return out



def _tar_walk_for(raw, want=b"manifest"):
    """在（可能被截断的）tar 字节流里逐条走，找到 want 这条普通文件的内容。

    ⚠️ 不能假设 manifest 是第一条 —— 实测第三方打包（如 jia070310/lemon-muisc）
    把 app.tgz 放在最前、manifest 放在最后；只有 fnpack / 本仓的产物才是 manifest 在前。
    所以这里必须真的按 tar 结构逐条跳（用每条的 size 算下一条的偏移）。
    """
    off = 0
    n = len(raw)
    while off + 512 <= n:
        header = raw[off:off + 512]
        if header[:1] == b"\0":
            break  # 结束块
        name = header[:100].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
        try:
            size = int((header[124:136].split(b"\0", 1)[0].strip() or b"0"), 8)
        except ValueError:
            return None
        typeflag = header[156:157]
        body_off = off + 512
        if name == want.decode() and typeflag in (b"0", b"", b"\0"):
            body = raw[body_off:body_off + size]
            return body if len(body) == size else None
        off = body_off + ((size + 511) // 512) * 512
    return None


def _manifest_from_tail(raw):
    """在文件尾部窗口里找 manifest（应对「manifest 在最后」的打包方式）。

    只认「name 字段正好是 manifest 且后面全是 NUL」的 tar 头，避免误命中正文里的字符串。
    """
    idx = 0
    while True:
        i = raw.find(b"manifest", idx)
        if i < 0:
            return None
        hdr = raw[i:i + 512]
        if len(hdr) == 512 and hdr[9:100].strip(b"\0") == b"":
            try:
                size = int((hdr[124:136].split(b"\0", 1)[0].strip() or b"0"), 8)
            except ValueError:
                size = -1
            if 0 < size < 1 << 20:
                body = raw[i + 512:i + 512 + size]
                if len(body) == size:
                    return body
        idx = i + 1


def _fetch_range(url, spec, timeout=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "fpk-update/1.0")
    req.add_header("Range", spec)
    with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as r:
        return r.read()


def _fetch_head(url, nbytes):
    """取开头 nbytes。Range 不被支持时退回普通 GET（只读前 nbytes 就断开）。

    实测：部分 GitHub Release 资源的 CDN 对 Range 直接回 501 Unsupported client
    range（如 jia070310/lemon-muisc 的附件）。这种情况不能当成"读不到包"，
    退回普通 GET 仍能拿到开头（manifest 在前的包照样能解出来）。
    """
    try:
        return _fetch_range(url, "bytes=0-%d" % (nbytes - 1))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 416, 501, 505):
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "fpk-update/1.0")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return r.read(nbytes)
        raise


def _fetch_tail(url, nbytes):
    """取结尾 nbytes；不支持 Range 时返回 None（由调用方走整包兜底）。"""
    try:
        return _fetch_range(url, "bytes=-%d" % nbytes)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 416, 501, 505):
            return None
        raise


# 小于这个体积的包，读不出 manifest 时直接整包下载来读（省事且可靠）
FULL_FETCH_LIMIT = 64 * 1024 * 1024


def _decode_manifest(body):
    """manifest 正文 → dict（顺带做必填校验）。"""
    mf = _parse_manifest(body.decode("utf-8", "replace"))
    if not mf.get("appname") or not mf.get("version"):
        raise RuntimeError("安装包的 manifest 缺 appname/version")
    return {
        "appname": mf.get("appname", ""),
        "version": mf.get("version", ""),
        "display_name": mf.get("display_name", ""),
        "platform": mf.get("platform", ""),
        "changelog": mf.get("changelog", ""),
        "service_port": mf.get("service_port", ""),
    }


def read_fpk_manifest(url, size_hint=None, max_bytes=768 * 1024, use_cache=True):
    """读取远端 fpk 的 manifest（优先只读首尾，必要时整包下载）。返回 dict。

    manifest 的位置随打包方式不同，实测有两种：
      · fnpack / 本仓：manifest 在**最前** → 读开头即可；
      · 第三方打包（实测 lemon-music）：app.tgz 在前、manifest 在**最后** → 读结尾。
    再不行（Range 被拒、位置古怪）且包不大 → 整包下到临时文件用 tarfile 读。
    """
    import zlib

    key = "%s|%s" % (url, size_hint if size_hint is not None else "?")
    cache = _load_manifest_cache()
    if use_cache and key in cache:
        hit = cache[key]
        if isinstance(hit, dict) and hit.get("appname"):
            return hit

    body = None

    # ① 开头窗口：部分解压后按 tar 结构逐条走
    try:
        head = _fetch_head(url, max_bytes)
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = d.decompress(head)
        body = _tar_walk_for(raw)
    except zlib.error:
        body = None
    except (urllib.error.URLError, OSError):
        raise

    # ② 结尾窗口：应对「manifest 在最后」
    if not body:
        tail = _fetch_tail(url, 256 * 1024)
        if tail:
            body = _manifest_from_tail(tail)

    # ③ 整包兜底：包不大就直接下下来用 tarfile 读（最可靠）
    if not body and (size_hint is None or size_hint <= FULL_FETCH_LIMIT):
        tmp = None
        try:
            import tempfile
            fd, tmp = tempfile.mkstemp(prefix="fpku-probe-", suffix=".fpk", dir=VAR_DIR)
            os.close(fd)
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "fpk-update/1.0")
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            mf = read_local_fpk_manifest(tmp)
            log("整包下载后读到 manifest：%s %s（%s 字节）"
                % (mf["appname"], mf["version"], os.path.getsize(tmp)))
            cache[key] = mf
            _save_manifest_cache()
            return mf
        except Exception as exc:  # noqa: BLE001 - 兜底失败就交给调用方回退
            log("整包兜底读取 manifest 失败：%s" % exc)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    if not body:
        raise RuntimeError("没能在安装包里找到 manifest（首/尾/整包都试过了）")

    info = _decode_manifest(body)
    cache[key] = info
    if len(cache) > 200:
        for k in list(cache)[:100]:
            cache.pop(k, None)
    _save_manifest_cache()
    return info


def read_local_fpk_manifest(path, max_members=400):
    """读**本地** fpk 的 manifest（下载完之后用它做最终复核）。

    与远端读法不同：文件已在本地，直接用 tarfile 流式读，不猜位置。
    这是安装前的最后一道关口 —— 检测阶段可以用 tag 之类的启发式，
    但真正要装的那个包必须自己说明它是什么。
    """
    import tarfile
    try:
        with tarfile.open(path, "r:gz") as tf:
            for i, m in enumerate(tf):
                if i > max_members:
                    break
                if m.name == "manifest" and m.isfile():
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    return _decode_manifest(f.read())
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise RuntimeError("读取本地安装包失败：%s" % exc)
    raise RuntimeError("本地安装包里没有 manifest")


# ───────────────────────────── 源解析（仓库 / 直链） ─────────────────────────────

def _pick_asset(assets, want_arch=None, pattern=None, strict=False):
    """在多个 .fpk 附件里挑一个。

    优先级（都只是「同一架构内怎么选」，不改变架构判定）：
      1. 用户写的 asset 通配符命中者优先
      2. 匹配本机架构的优先，明确标了对方架构的排除
      3. 名字里带 iframe（fnOS 桌面小窗入口）的优先于带 url/browser 的
         —— 实测 guan-ry/FanControlServerApp 同一架构同时发 -iframe 与 -url 两个包，
         而本项目的既定形态是「桌面图标点开 = 小窗」。**不能靠"取最大文件"撞运气**：
         两个包体积只差 184 字节，哪天反过来就会挑错，用户点图标变成弹浏览器。
      4. 名字里带 untested 的排最后（作者自己标了没测过）
      5. 都不满足时取体积大的

    `strict=True`：用户明确指定了 asset（或通配符）但一个都没命中时**报错而不是
    悄悄回退成自动挑选**。回退很危险 —— 可能挑到另一个架构、或另一个入口形态的包，
    用户以为自己钉的是 A，实际装的是 B。真正要装之前必须把这个说清楚。
    """
    import fnmatch
    cands = list(assets)
    if pattern:
        pat = str(pattern).strip()
        if any(ch in pat for ch in "*?["):
            hit = [a for a in cands if fnmatch.fnmatch(str(a.get("name") or ""), pat)]
        else:
            # 不带通配符 = 精确指定某个附件（UI 里下拉选出来的就是这个）
            hit = [a for a in cands if str(a.get("name") or "") == pat]
        if hit:
            cands = hit
        elif strict:
            raise RuntimeError("这个 Release 里没有匹配「%s」的 .fpk 附件；可选：%s"
                               % (pat, "、".join(str(a.get("name")) for a in cands)))
    want_arch = want_arch or device_arch()
    other = "arm" if want_arch == "x86" else "x86"

    def token_re(word):
        return re.compile(r"(?:^|[-_.])%s(?:[-_.]|$)" % re.escape(word), re.I)

    re_iframe = token_re("iframe")
    re_url = token_re("url")
    re_browser = token_re("browser")
    re_untested = token_re("untested")

    def score(a):
        n = str(a.get("name") or "")
        s = 0
        if any(k in n.lower() for k in ARCH_KEYS.get(want_arch, [])):
            s -= 1000
        if any(k in n.lower() for k in ARCH_KEYS.get(other, [])):
            s += 1000
        if re_iframe.search(n):
            s -= 40
        if re_url.search(n) or re_browser.search(n):
            s += 40
        if re_untested.search(n):
            s += 200
        return s

    return sorted(cands, key=lambda a: (score(a), -int(a.get("size") or 0)))[0]


def normalize_source(entry):
    """把一条白名单配置归一化成 {'repo','url','tag','asset','allow_prerelease','appname'}。"""
    repo = str(entry.get("repo") or "").strip()
    url = str(entry.get("url") or "").strip()
    # 允许直接贴完整 GitHub 地址
    m = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?/?$", repo or url)
    if m:
        repo = "%s/%s" % (m.group(1), m.group(2))
        url = "" if url == repo or m.group(0) == url else url
    return {
        "repo": repo,
        "url": url,
        "tag": str(entry.get("tag") or "").strip(),
        "asset": str(entry.get("asset") or "").strip(),
        "allow_prerelease": bool(entry.get("allow_prerelease")),
        "appname": str(entry.get("appname") or "").strip(),
    }


def self_source():
    """本应用自身的更新来源（独立于被管理列表）。

    形状故意和被管理条目一致 —— 检测/下载/升级这条链路上的每个函数都直接吃它，
    所以「自身更新」复用的是同一套经过验证的代码，不是另写一份平行实现。
    """
    repo = str(load_settings().get("self_repo") or "").strip() or SELF_DEFAULT_REPO
    m = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?/?$", repo)
    if m:
        repo = "%s/%s" % (m.group(1), m.group(2))
    if not REPO_RE.match(repo):
        repo = SELF_DEFAULT_REPO
    return {"appname": APP_NAME, "display": "FPK更新", "repo": repo, "url": "",
            "tag": "", "asset": "", "allow_prerelease": False, "note": "",
            "self": True}


def migrate_self_entry():
    """一次性把「把自己当普通应用加进列表」的旧记录迁走（非破坏）。

    以前自身更新是实现成「把自己加进 apps.json 当普通应用」的，那是复用现成机制的
    取巧做法，副作用是它会出现在列表里、还会被「全部更新」带上。现在整条链路独立了，
    这条记录留着只会变成列表里一个多余且危险的条目 —— 所以启动时移出列表。
    用户当时填的仓库不会丢：它会变成 self_repo 设置，照样生效。
    """
    with _lock:
        doc = _whitelist_doc()
        keep, moved = [], []
        for item in (doc.get("apps") or []):
            if not isinstance(item, dict):
                continue
            src = normalize_source(item)
            if src["appname"] == APP_NAME or (src["repo"] or "").split("/")[-1] == APP_NAME:
                moved.append(src["repo"])
            else:
                keep.append(item)
        if not moved:
            return False
        doc["apps"] = keep
        save_whitelist(doc)
    repo = next((r for r in moved if r), "")
    if repo and not load_settings().get("self_repo"):
        save_settings({"self_repo": repo})
    log("自身更新已独立：从被管理列表移出 %d 条自己%s（以后不参与「全部更新」）"
        % (len(moved), ("，仓库沿用 %s" % repo) if repo else ""))
    return True


def find_release(repo, allow_prerelease=False, want_tag="", force=False):
    """找一个可用的 Release（不要求 tag 长得像版本号 —— 版本从 fpk 里读）。

    找不到时**要说清是哪一种「找不到」** —— 空列表、全是 draft/预发布、附件还没传完
    是完全不同的三件事，报成一句笼统的「没有可用的 Release」会让人以为是自己配错了。
    实测踩过：GitHub 瞬时返回空列表 → 界面报「仓库没有可用的 Release」，几秒后同样
    调用正常。所以这里对「列表为空」单独给一句带重试指引的话。
    """
    if want_tag:
        # 直接问「这个 tag 的 Release」，而不是在前 N 条列表里找。
        # 原因：列表是分页的（UI 的版本下拉能列出 50 个），用户完全可能选一个排在
        # 第 31 位之后的旧版本做回滚 —— 在 30 条的列表里找不到就会被误判成「不存在」。
        try:
            one = gh_get("https://api.github.com/repos/%s/releases/tags/%s"
                         % (repo, quote(str(want_tag))))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError("仓库 %s 里没有 tag 为 %s 的 Release" % (repo, want_tag))
            raise
        if not isinstance(one, dict):
            raise ValueError("GitHub 返回格式异常")
        data = [one]
    else:
        data = gh_releases(repo, force=force)
    if not data:
        raise RuntimeError(
            "仓库 %s 这次读不到任何 Release —— 多半是 GitHub 侧瞬时抖动。"
            "点一次「检查更新」重试即可。" % repo)
    skipped_draft = skipped_pre = 0
    for rel in data:
        if rel.get("draft"):
            skipped_draft += 1
            continue
        tag = str(rel.get("tag_name") or "")
        if want_tag:
            if tag != want_tag:
                continue
        else:
            if rel.get("prerelease") and not allow_prerelease:
                skipped_pre += 1
                continue
            if tag.strip().lower() in ROLLING_TAGS and not allow_prerelease:
                skipped_pre += 1
                continue
        assets = [a for a in (rel.get("assets") or [])
                  if str(a.get("name") or "").lower().endswith(".fpk")]
        if not assets:
            continue
        return {
            "tag": tag,
            "prerelease": bool(rel.get("prerelease")),
            "published_at": rel.get("published_at"),
            "notes": (rel.get("body") or "").strip()[:400],
            "assets": assets,
        }
    if want_tag:
        raise RuntimeError("仓库 %s 里没有 tag 为 %s 的 Release（或者它的附件还不叫 .fpk）"
                           % (repo, want_tag))
    raise RuntimeError(
        "仓库 %s 的 %d 个 Release 里没有可用的 .fpk 附件（draft %d 个、预发布/滚动 tag %d 个已跳过）。"
        "如果是刚发布的版本，等附件上传完再点一次「检查更新」。"
        % (repo, len(data), skipped_draft, skipped_pre))


def list_options(source, max_releases=20):
    """列出一个来源所有可选的「版本」与「安装包」，供用户手动挑。

    为什么要这个：同一架构常常有多个包（实测 guan-ry/FanControlServerApp 同时发
    `-iframe.fpk` 与 `-url.fpk`；kyeo-hub/CleanFnOS 发 `_x86` 与 `_arm`），
    而不同人的用法不同（小窗 vs 浏览器入口、x86 vs arm）。自动取舍只能给个**推荐**，
    最终该由用户定 —— 所以这里把全部选项摊开，并标出「推荐」是哪个。

    只发一次 GitHub 请求，不逐个读 manifest（读 manifest 放到 preview 那一步，
    只读用户真正选中的那个）。
    """
    src = normalize_source(source)
    out = []

    if not src["repo"]:
        if not src["url"]:
            raise RuntimeError("配置里既没有 repo 也没有 url")
        name = os.path.basename(urlparse(src["url"]).path) or "package.fpk"
        out.append({
            "tag": "",
            "published_at": None,
            "prerelease": False,
            "draft": False,
            "notes": "（直链来源：只有一个安装包，没有 sha256 摘要，出于安全不会下载/安装）",
            "assets": [{"name": name, "size": None, "sha256": "", "recommended": True}],
        })
        return {"source_kind": "url", "releases": out, "device_arch": device_arch()}

    data = gh_releases(src["repo"])
    if not isinstance(data, list):
        raise ValueError("GitHub 返回格式异常")
    for rel in data:
        if rel.get("draft"):
            continue
        assets = [a for a in (rel.get("assets") or [])
                  if str(a.get("name") or "").lower().endswith(".fpk")]
        if not assets:
            continue
        rec = _pick_asset(assets)
        rows = []
        for a in assets:
            digest = str(a.get("digest") or "")
            rows.append({
                "name": a.get("name"),
                "size": a.get("size"),
                "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
                "recommended": a is rec,
            })
        out.append({
            "tag": rel.get("tag_name") or "",
            "published_at": rel.get("published_at"),
            "prerelease": bool(rel.get("prerelease")),
            "draft": False,
            "rolling": (rel.get("tag_name") or "").strip().lower() in ROLLING_TAGS,
            "notes": (rel.get("body") or "").strip()[:200],
            "assets": rows,
        })
        if len(out) >= max_releases:
            break
    if not out:
        raise RuntimeError("仓库 %s 没有任何带 .fpk 附件的 Release" % src["repo"])
    return {"source_kind": "repo", "releases": out, "device_arch": device_arch()}


def _version_from_tag(tag):
    v = norm_version(tag)
    return v if split_version(v) is not None else ""


def resolve_source(entry, force=False):
    """解析一条配置 → 目标版本信息。

    版本优先取 **fpk 自身的 manifest**（权威，与 fnOS 安装时读的是同一份）；
    读不到时退回「用 tag 当版本」（大多数仓库 tag 就是版本号），并在结果里标明来源，
    页面会据此提示。真正安装前还会用本地包再复核一次（见 read_local_fpk_manifest）。

    返回 {'appname','version','version_source','display_name','platform','tag',
          'asset_name','asset_url','sha256','size','published_at','notes','source_kind'}
    """
    src = normalize_source(entry)
    if not src["repo"] and not src["url"]:
        raise RuntimeError("配置里既没有 repo 也没有 url")

    if src["url"] and not src["repo"]:
        asset_url = src["url"]
        asset_name = os.path.basename(urlparse(asset_url).path) or "package.fpk"
        rel = {"tag": "", "published_at": None,
               "notes": "（直链来源，没有 sha256 摘要，出于安全不会下载/安装）"}
        sha, size = "", None
    else:
        rel = find_release(src["repo"], src["allow_prerelease"], src["tag"], force=force)
        if not rel:
            raise RuntimeError("仓库 %s 没有可用的 Release（含 .fpk 附件）" % src["repo"])
        # strict：用户显式指定了 asset 而名字对不上时直接报错。
        # 静默回退成「自动挑」会很危险 —— 可能挑到另一个架构或另一个入口形态的包，
        # 用户以为钉的是 A、实际装的是 B。
        asset = _pick_asset(rel["assets"], pattern=src["asset"] or None, strict=True)
        asset_url = asset.get("browser_download_url")
        asset_name = asset.get("name")
        digest = str(asset.get("digest") or "")
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        size = asset.get("size")

    appname = src["appname"]
    version = ""
    display = ""
    platform = ""
    version_source = ""

    try:
        mf = read_fpk_manifest(asset_url, size)
        appname = appname or mf["appname"]
        version = mf["version"]
        display = mf.get("display_name", "")
        platform = mf.get("platform", "")
        version_source = "fpk"
    except Exception as exc:  # noqa: BLE001 - 读不到就退回 tag，但要如实标注
        log("读取 %s 的 manifest 失败（%s），回退用 tag 当版本" % (asset_name, exc))
        version = _version_from_tag(rel.get("tag") or "")
        version_source = "tag" if version else ""
        if not version:
            raise RuntimeError("无法确定版本：既读不到安装包里的 manifest，"
                               "tag（%s）也不是版本号" % (rel.get("tag") or "无"))
        if not appname:
            raise RuntimeError("无法确定应用名：安装包的 manifest 读不到，"
                               "请在配置里写明 appname")

    return {
        "appname": appname,
        "version": version,
        "version_source": version_source,
        "display_name": display,
        "platform": platform,
        "tag": rel.get("tag") or "",
        "asset_name": asset_name,
        "asset_url": asset_url,
        "sha256": sha,
        "size": size,
        "published_at": rel.get("published_at"),
        "notes": rel.get("notes") or "",
        "source_kind": "url" if (src["url"] and not src["repo"]) else "repo",
    }


# ───────────────────────────── 应用中心守护进程 RPC ─────────────────────────────

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout=HTTP_TIMEOUT):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.sock_path)
        self.sock = s


def daemon_call(route, payload, timeout=HTTP_TIMEOUT):
    body = json.dumps(payload).encode("utf-8")
    conn = UnixHTTPConnection(DAEMON_SOCKET, timeout=timeout)
    try:
        conn.request("POST", route, body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()
    try:
        env = json.loads(raw)
    except ValueError:
        raise RuntimeError("应用中心返回非 JSON：%s" % raw[:200])
    code = env.get("code", -1)
    if code != 0:
        raise RuntimeError("应用中心拒绝（code=%s）：%s" % (code, env.get("msg") or raw[:200]))
    return env.get("data") or {}


def stage_fpk(fpk_path, progress=None):
    """把本地 fpk 交给应用中心识别（只识别，不改任何东西）。"""
    if progress:
        progress("staging", "正在让应用中心识别安装包…")
    task = daemon_call("/rpc/v1/download/task",
                       {"packageSourceType": "file", "path": fpk_path})
    tid = task.get("downloadTaskId")
    if not tid:
        raise RuntimeError("应用中心未返回暂存任务 ID")
    deadline = time.time() + 180
    while time.time() < deadline:
        st = daemon_call("/rpc/v1/download/status", {"downloadTaskId": tid})
        status = st.get("status")
        if status == 2:
            if not st.get("appName") or not st.get("version"):
                raise RuntimeError("暂存完成但应用中心未能识别安装包内容")
            return st
        if status != 1:
            raise RuntimeError("暂存失败（status=%s）：%s" % (status, st.get("message")))
        time.sleep(1)
    raise RuntimeError("暂存超时")


def upgrade_via_daemon(fpk_path, appname, target_version, progress=None):
    """驱动应用中心的【就地升级】通道（与界面点「更新」同一条路）。

    实测要点（fnOS 1.2.0800）：
      · 必须先暂存，否则 update/info 返回 10100（包不存在）
      · update/info 里取 installedVolumeID —— 取不到就中止（别猜卷，会丢数据）
      · 任务状态：1=进行中 2=成功 5=守护进程已不再持有该任务（**不代表失败**，
        要用「最终版本」来判定）
    """
    def say(stage, msg):
        if progress:
            progress(stage, msg)

    staged = stage_fpk(fpk_path, progress)
    if staged.get("appName") != appname:
        raise RuntimeError("安装包应用名不符：包内为 %s，期望 %s"
                           % (staged.get("appName"), appname))
    pkg_type = staged.get("packageType") or "app"

    say("precheck", "正在读取当前安装位置…")
    info = daemon_call("/rpc/v1/update/info", {
        "appName": appname,
        "updateVersion": target_version,
        "packageType": pkg_type,
        "language": "zh-CN",
    })
    wizard = info.get("wizardInfo") or {}
    volume = wizard.get("installedVolumeID")
    if not isinstance(volume, int) or volume <= 0:
        raise RuntimeError("取不到当前安装卷（installedVolumeID=%r），已中止以保护数据" % volume)

    say("upgrading", "正在升级（%s → %s）…" % (wizard.get("version") or "?", target_version))
    task = daemon_call("/rpc/v1/update/task", {
        "appName": appname,
        "updateVersion": target_version,
        "packageType": pkg_type,
        "systemParameters": {
            "agreedToProtocol": True,
            "installVolumeID": volume,
            "dataVolumeId": volume,
            "immediateStart": False,
        },
        "customParameters": [],
        "language": "zh-CN",
    })
    task_id = task.get("taskId")
    if not task_id:
        raise RuntimeError("应用中心未返回升级任务 ID")

    deadline = time.time() + 15 * 60
    final = None
    while time.time() < deadline:
        st = daemon_call("/rpc/v1/common/status", {"taskId": task_id})
        status = st.get("status")
        pct = st.get("progress")
        if status == 2:
            final = "ok"
            break
        if status == 5:
            final = "unknown"
            break
        if status == 1:
            if progress and pct is not None:
                progress("upgrading", "正在升级… %s%%" % pct)
            time.sleep(2)
            continue
        detail = st.get("message") or st.get("outputText") or ""
        raise RuntimeError("升级任务失败（status=%s）：%s" % (status, detail))
    if final is None:
        raise RuntimeError("升级任务超时未完成（已等待 15 分钟）")

    say("verifying", "正在核验升级结果…")
    time.sleep(2)
    now_ver = installed_version(appname)
    if norm_version(now_ver or "") != norm_version(target_version):
        if final == "unknown":
            raise RuntimeError("应用中心已不再持有该任务，且当前版本仍为 %s（期望 %s）"
                               % (now_ver, target_version))
        raise RuntimeError("升级结束但版本未变化：当前 %s（期望 %s）" % (now_ver, target_version))

    # 升级流程通常会自动拉起应用；这里幂等地确认一次
    try:
        subprocess.run(["appcenter-cli", "start", appname],
                       capture_output=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        pass
    say("done", "升级完成：%s → %s" % (norm_version(target_version), norm_version(now_ver)))
    return True


# ───────────────────────────── 检测 ─────────────────────────────

def check_one(app, force=False):
    """查一个应用的最新版本（只读，不改任何东西）。

    force：绕过 Release 列表的 15 秒缓存（自己刚发一版、马上点检查的场合）。

    通用性要点：**目标版本来自 fpk 自身的 manifest**，不是从 tag 猜的。
    所以仓库的 tag 怎么命名、附件叫什么名字都无所谓；appname 也可以留空自动识别。
    """
    src_key = app["repo"] or app["url"]
    item = {
        "appname": app["appname"],
        "display": app["display"],
        "note": app["note"],
        "installed": installed_version(app["appname"]) if app["appname"] else None,
        "manifest_display": installed_display(app["appname"]) if app["appname"] else None,
        "repo": app["repo"],
        "url": app["url"],
        "tag": app["tag"],
        "source": src_key,
        "has_icon": bool(app["appname"]) and icon_path(app["appname"]) is not None,
        "latest": None,
        "update_available": False,
        "error": None,
        "published_at": None,
        "notes": "",
        "asset_name": None,
        "size": None,
        "downloaded": None,
        "local_unknown": None,
        "detected_appname": None,
        "sha256": "",
    }
    try:
        rel = resolve_source(app, force=force)
    except urllib.error.HTTPError as exc:
        item["error"] = _gh_error_message(exc)
        return item
    except urllib.error.URLError as exc:
        item["error"] = "连不上源：%s" % (exc.reason,)
        return item
    except Exception as exc:  # noqa: BLE001 - 网络/格式原因多样，统一呈现
        item["error"] = "检查失败：%s" % exc
        return item

    # 以 fpk 内层 manifest 为准（这是 fnOS 安装时真正读的那份）
    detected = rel["appname"]
    item["detected_appname"] = detected
    if app["appname"] and detected != app["appname"]:
        item["error"] = ("安装包内的应用名（%s）与配置的 appname（%s）不一致 —— "
                         "这个包不是给该应用用的" % (detected, app["appname"]))
        return item
    if not app["appname"]:
        # 没写 appname：用识别出来的那个，并写回配置记住
        item["appname"] = detected
        item["installed"] = installed_version(detected)
        item["manifest_display"] = installed_display(detected)
        item["has_icon"] = icon_path(detected) is not None
        remember_appname(src_key, detected, rel.get("display_name") or "", rel["version"])

    if not item["display"]:
        item["display"] = (rel.get("display_name") or item["manifest_display"]
                           or item["appname"])
    if not item["installed"]:
        item["error"] = "未安装（本机没有 %s）" % item["appname"]
        return item

    # 架构对不上就别动
    plat = (rel.get("platform") or "").strip().lower()
    if plat and plat not in ("all", device_arch()):
        item["error"] = ("安装包面向 %s，本机是 %s —— 已跳过" % (plat, device_arch()))
        return item

    item["latest"] = rel["version"]
    item["published_at"] = rel["published_at"]
    item["notes"] = rel["notes"]
    item["asset_name"] = rel["asset_name"]
    item["size"] = rel["size"]
    item["sha256"] = rel.get("sha256") or ""
    item["version_source"] = rel.get("version_source") or ""
    item["tag"] = rel.get("tag") or ""
    item["update_available"] = is_newer(rel["version"], item["installed"])
    # 关系：钉住的版本可能比已装的旧（用于回滚）—— 界面据此显示不同徽章
    _c = cmp_version(rel["version"], item["installed"])
    item["relation"] = {1: "newer", 0: "same", -1: "older"}.get(_c, "unknown")
    if not item["sha256"]:
        item["error"] = ("该来源没有 sha256 摘要，出于安全不会下载/安装"
                         "（可改用仓库来源，GitHub 的 Release 附件一般带 digest）")
        item["update_available"] = False
    elif item["version_source"] == "tag":
        item["note"] = ((item["note"] + " ").strip()
                        + "（版本取自 Release tag：该安装包读不到 manifest）").strip()

    # 本地是否已经下好这一版（决定「安装」按钮能不能点）
    # 本地是否已经下好这一版（决定「安装」按钮能不能点）。
    # 身份必须以**台账**为准（台账记着本应用下载的版本 + sha256 前 16 位）。
    # 原来只看「同名文件在不在」——对附件名不带版本号的仓库，上游一发新版就会把
    # 用户自己归档的同名旧包当成当前版本：界面要么误报「本地安装包不完整」，
    # 要么显示「已在本地：<新版本>」而点安装时报「sha256 与 Release 不一致」，
    # 把正常状态说成文件被篡改。
    try:
        d = download_dir()
        local = os.path.join(d, rel["asset_name"])
        if os.path.isfile(local):
            size = os.path.getsize(local)
            want_sha = (rel.get("sha256") or "").lower()
            # 快路：台账里有这一版、且 sha 前 16 位对得上 → 不必再算哈希
            led = _load_registry().get(os.path.realpath(local)) or {}
            by_ledger = (bool(want_sha)
                         and norm_version(led.get("version") or "") == norm_version(rel["version"])
                         and led.get("sha256") == want_sha[:16])
            # 慢路（但正确）：直接比对**内容**。谁下载的不重要，字节对不对才重要 ——
            # 归档目录里的包常常是别的脚本下的，只看「谁下的」会把它们全误判成外来，
            # 连带把「安装」也禁掉（实测用户就是这么被误伤的）。
            by_hash = False
            if not by_ledger and want_sha:
                by_hash = (_local_sha256_cached(local) == want_sha)
            if by_ledger or by_hash:
                item["downloaded"] = {
                    "version": rel["version"], "path": local, "size": size,
                    # 内容已核对一致 → 就是这一版（尺寸自然也吻合）
                    "complete": True,
                    "verified_by": "ledger" if by_ledger else "sha256",
                }
            else:
                item["local_unknown"] = {
                    "name": rel["asset_name"], "size": size,
                    "reason": ("内容与 Release 不一致（可能是旧版本或没下完）" if want_sha
                               else "这个来源没有 sha256 摘要，无法核对内容"),
                }
    except OSError:
        pass
    return item


AUTO_DL_STATE = os.path.join(VAR_DIR, "auto-download.json")
AUTO_DL_RETRY_SEC = 30 * 60      # 同一个应用两次自动下载至少隔这么久


def _auto_download(results):
    """【自动下载】把「有新版本但本地还没下」的排进下载队列。

    ⚠️ **只下载，绝不自动安装** —— 装不装、什么时候装仍然由用户点。
    这是刻意的边界：自动替换正在运行的应用（尤其承载会话的 Ekko Studio）风险太大。
    """
    tried = load_json(AUTO_DL_STATE)
    if not isinstance(tried, dict):
        tried = {}
    now = time.time()
    n = 0
    for r in results:
        if not r.get("update_available") or r.get("error"):
            continue
        dl = r.get("downloaded")
        if dl and dl.get("complete") and norm_version(dl.get("version") or "") == norm_version(r.get("latest") or ""):
            continue          # 这一版的包已经在本地了
        # 退避：同一个应用 30 分钟内只自动试一次。
        # 不加这道闸，一个下不动的包（网络/CDN 拒绝等）会每轮检测都重试一遍，没完没了。
        try:
            last = float(tried.get(r["appname"]) or 0)
        except (TypeError, ValueError):
            last = 0
        if now - last < AUTO_DL_RETRY_SEC:
            log("[auto] %s 上次自动下载在 %.0f 分钟前，本轮跳过（30 分钟内只试一次）"
                % (r["appname"], (now - last) / 60))
            continue
        try:
            tid = enqueue(r["appname"], "download")
            tried[r["appname"]] = now
            n += 1
            log("[auto] 有新版本，自动排入下载：%s %s → %s（任务 %s）"
                % (r["appname"], r.get("installed"), r.get("latest"), tid))
        except Exception as exc:  # noqa: BLE001
            log("[auto] 自动下载排队失败 %s：%s" % (r["appname"], exc))
    if n:
        save_json(AUTO_DL_STATE, tried)
        log("[auto] 本轮自动下载共排队 %d 个任务" % n)


def run_check(trigger="manual", force=False):
    """跑一轮检测。

    带**并发闸门**：原来只有「距上次完成不足 10 秒就跳过」的时间节流，跑在中途的检查
    完全不受限 —— 多标签页/桌面小窗+浏览器同开时两轮检测会并行，把按出口 IP 共享的
    GitHub 额度翻倍烧掉（匿名只有 60 次/小时），还会并发写同一份缓存与 last-check.json。
    """
    if not _check_lock.acquire(blocking=False):
        log("已有一轮检测在进行（%s），本次直接返回上次结果，不重复打 GitHub" % trigger)
        return cached_state().get("apps") or []
    try:
        return _run_check_locked(trigger, force=force)
    finally:
        _check_lock.release()


def check_state():
    with _lock:
        return dict(_check_state)


def _run_check_locked(trigger, force=False):
    managed = load_whitelist()
    # 自身也一起查（省一轮 GitHub 往返），但结果**单独存**在 state["self"]：
    # 它不属于「被管理的应用列表」，界面上下面的列表 + 上面单独一块。
    apps = managed + [self_source()]
    log("开始检测（%s）：%d 个被管理应用 + 本应用自身" % (trigger, len(managed)))
    log("开始检测（%s）：%d 个应用" % (trigger, len(apps)))
    with _lock:
        _check_state.update({"running": True, "done": 0, "total": len(apps),
                             "current": "", "trigger": trigger,
                             "started_at": datetime.now(CST).isoformat(timespec="seconds")})
    t0 = time.time()
    results = [None] * len(apps)
    cnt = {"done": 0}
    cnt_lock = threading.Lock()

    def one(i, app):
        """一个应用一个线程。

        为什么并行：每个应用都要打一次 GitHub API（拿 Release）+ 读一次包内 manifest
        （HTTP Range 好几个请求），串行 6 个应用实测 6.1 秒。并行之后 ≈ 最慢的那个。
        """
        try:
            r = check_one(app, force=force)
            if app.get("self"):
                r["self"] = True                      # 打标：结果要单独归到 state["self"]
            results[i] = r
        except Exception as exc:                      # noqa: BLE001
            log("[%s] 检测异常：%s" % (app.get("appname") or app.get("repo"), exc))
            results[i] = {"appname": app.get("appname") or "", "display": app.get("display") or "",
                          "repo": app.get("repo"), "url": app.get("url"),
                          "installed": installed_version(app.get("appname") or "") if app.get("appname") else None,
                          "latest": None, "update_available": False, "downloaded": None,
                          "local_unknown": None, "sha256": "", "self": bool(app.get("self")),
                          "error": "检查失败：%s" % exc}
        with cnt_lock:
            cnt["done"] += 1
            done_n = cnt["done"]
        with _lock:
            _check_state.update({"done": done_n, "current": app.get("appname") or ""})

    threads = [threading.Thread(target=one, args=(i, a), daemon=True)
               for i, a in enumerate(apps)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log("检测完成：%d 个应用（含本应用自身），用时 %.1f 秒" % (len(apps), time.time() - t0))
    for r in results:
        log("  %s: 已装=%s 最新=%s 可更新=%s%s"
            % (r["appname"], r["installed"], r["latest"],
               r["update_available"], (" 错误=" + r["error"]) if r["error"] else ""))
    # 状态先落盘、再把 running 复位 —— 顺序不能反。
    # 反了会留一个几毫秒的窗口：running 已经是 False（界面停止转圈、开始读新结果），
    # 而 last-check.json 还是上一轮的内容。实测这个窗口让「门禁自己」都 flaky 了
    # （14 次里红 2 次），真实场景里对应的是「检查完了但卡片还是旧的」闪一下。
    with _lock:
        save_json(LAST_CHECK_FILE, {
            "at": datetime.now(CST).isoformat(timespec="seconds"),
            "trigger": trigger,
            # 自身单独一格：列表接口只回 apps，自身走 /api/state 的 self 字段。
            # 「全部更新」按 apps 迭代，所以天然不会带上它。
            "apps": [r for r in results if not r.get("self")],
            "self": next((r for r in results if r.get("self")), None),
        })
    with _lock:
        _check_state.update({"running": False, "current": "",
                             "finished_at": datetime.now(CST).isoformat(timespec="seconds")})
    if load_settings().get("auto_download"):
        _auto_download(results)
    return results


def state_item(appname):
    """从最近一次检测结果里取某个应用的那条。"""
    for r in (cached_state().get("apps") or []):
        if r.get("appname") == appname:
            return r
    return None


def download_needed(item):
    """这个应用现在还需不需要「下载 / 下载并更新」？

    用户要求：**已经是最新的就不下载**。
    实测踩过：6 个应用全是最新，却因为归档目录刚被清空而全部重新下了一遍 —— 白下 400MB。
    逻辑上也不该下：包是用来升级的，已经是最新就没有升级可做。

    唯一例外：本地有同名包但**内容核对不过**（旧版本 / 没下完 / 被改坏）——
    那是「修包」而不是「更新」。不给这条出路的话，卡片上那条提示就没有出口。
    """
    if not item:
        return True                     # 没有检测结果：不拦，让后面的流程给出具体原因
    if item.get("error"):
        return True                     # 状态不明（限流/仓库问题）：不在这里拦
    latest = norm_version(item.get("latest") or "")
    if not latest:
        return True
    inst = norm_version(item.get("installed") or "")
    if inst and inst == latest and not item.get("update_available"):
        dl = item.get("downloaded") or {}
        # 例外：本地包核对不过、或已有包但不完整 → 允许重下（这是「修包」，不是「更新」）
        return bool(item.get("local_unknown")) or dl.get("complete") is False
    return True


def cached_state():
    data = load_json(LAST_CHECK_FILE)
    if not isinstance(data, dict):
        return {"at": None, "trigger": None, "apps": [], "self": None}
    data.setdefault("self", None)          # 老状态文件没有这一格 → 补上空值
    return data


# ───────────────────────────── 内建下载服务 ─────────────────────────────
#
# 三件事：
#   1) 任务队列 —— 下载/安装都在后台线程里跑，**页面关掉也继续**；重新打开能看到进度。
#      队列顺序执行（一次一个），避免几个大包互相抢带宽。
#   2) 真实进度 —— 已下字节 / 总量 / 百分比 / 速度 / 剩余时间，边下边报。
#   3) 下载加速 —— 分段并发（Range 分段多线程）+ 可配置镜像回退。
#
# 安全：无论直连还是镜像、单流还是分段，**最终字节必须通过 sha256 校验**，不过就删除。
# 所以用第三方加速镜像不降低安全性 —— 镜像只能影响速度，改不了内容（哈希对不上会被拒）。

TASKS_FILE = os.path.join(VAR_DIR, "tasks.json")
TASKS_KEEP = 60
_tasks_lock = threading.RLock()
_worker_wake = threading.Condition(_tasks_lock)
_worker_thread = None


def _worker_alive():
    """工作线程是否还活着（服务重启后残留的 running 状态要靠它判断）。"""
    return bool(_worker_thread and _worker_thread.is_alive())


def _last_task(tasks):
    """最近一条任务（用于界面显示"上次做了什么"）。"""
    return dict(tasks[-1]) if tasks else {}


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def _load_tasks():
    d = load_json(TASKS_FILE)
    if not isinstance(d, dict) or not isinstance(d.get("tasks"), list):
        d = {"seq": 0, "tasks": []}
    if not isinstance(d.get("seq"), int):
        d["seq"] = 0
    return d


def _save_tasks(d):
    tasks = [t for t in d.get("tasks", []) if isinstance(t, dict)]
    if len(tasks) > TASKS_KEEP:
        live = [t for t in tasks if t.get("status") in ("running", "queued")]
        done = [t for t in tasks if t.get("status") not in ("running", "queued")]
        keep = max(0, TASKS_KEEP - len(live))
        tasks = (done[-keep:] if keep else []) + live
    d["tasks"] = tasks
    save_json(TASKS_FILE, d)


def list_tasks():
    with _tasks_lock:
        return list(_load_tasks()["tasks"])


def current_task():
    with _tasks_lock:
        for t in _load_tasks()["tasks"]:
            if t.get("status") == "running":
                return dict(t)
    return None


def update_task(tid, **kw):
    if not tid:
        return
    with _tasks_lock:
        d = _load_tasks()
        for t in d["tasks"]:
            if t.get("id") == tid:
                t.update(kw)
                t["updated_at"] = now_iso()
                break
        _save_tasks(d)


def clear_finished_tasks():
    with _tasks_lock:
        d = _load_tasks()
        d["tasks"] = [t for t in d["tasks"] if t.get("status") in ("running", "queued")]
        _save_tasks(d)
    return list_tasks()


def enqueue(appname, action):
    """把一次动作放进队列（后台执行），返回任务 id。重复排队会被合并。"""
    if action not in ACTIONS:
        raise ValueError("未知动作：%s" % action)
    app = _resolve_app(appname)
    if not app:
        raise ValueError("该应用不在列表里")
    with _tasks_lock:
        d = _load_tasks()
        for t in d["tasks"]:
            if (t.get("appname") == appname and t.get("action") == action
                    and t.get("status") in ("queued", "running")):
                return t["id"]          # 同一个应用的同一动作已在排队/运行，不重复排
        d["seq"] = int(d.get("seq") or 0) + 1
        tid = "t%d" % d["seq"]
        # 目标版本在**入队时**就记：自更新时进程会在升级中途被重启，重启后靠它判断
        # 「到底装上了没」。写在这里比「下载完再写」更早，进程更早掉也不怕。
        _tgt = ""
        if action in ("install", "update"):
            _tgt = str((state_item(appname) or {}).get("latest") or "")
        d["tasks"].append({
            "id": tid, "appname": appname, "display": app["display"] or appname,
            "version": (_tgt or None),
            "action": action, "status": "queued", "stage": "queued",
            # percent 用 None（= 进度未知），界面据此显示「不确定态」进度条。
            # 写 0 的话界面会画一条 0% 的空条，看起来像卡住了（实测观感问题）。
            "message": "排队中…", "percent": None,
            "created_at": now_iso(), "updated_at": now_iso(),
        })
        _save_tasks(d)
        _worker_wake.notify_all()
    return tid


def worker_loop():
    """队列工作线程：顺序取任务执行。页面关掉/刷新都不影响它。"""
    log("下载服务：队列工作线程已启动")
    while True:
        tid = appname = action = None
        with _tasks_lock:
            d = _load_tasks()
            for t in d["tasks"]:
                if t.get("status") == "queued":
                    tid, appname, action = t["id"], t.get("appname"), t.get("action")
                    t["status"] = "running"
                    t["stage"] = "starting"
                    t["message"] = "正在启动…"
                    t["started_at"] = now_iso()
                    _save_tasks(d)
                    break
            if tid is None:
                _worker_wake.wait(timeout=30)
                continue
        try:
            ACTIONS[action](tid, appname)
        except Exception as exc:  # noqa: BLE001 - 单个任务失败不能拖垮队列
            log("[%s] 任务 %s 异常：%s" % (appname, action, exc))
            log(traceback.format_exc())
            _fail_task(tid, appname, exc)


# ── 下载引擎 ──

def _fmt_speed(bps):
    if not bps or bps <= 0:
        return ""
    return _human(bps) + "/s"


def _fmt_eta(sec):
    if sec is None:
        return ""
    sec = int(sec)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 小时 %d 分" % (sec // 3600, (sec % 3600) // 60)


def _dl_message(kw):
    """把进度字段拼成一句人话（卡片与任务面板都用它）。"""
    done, total = kw.get("downloaded"), kw.get("total")
    parts = []
    if total:
        parts.append("%s / %s" % (_human(done), _human(total)))
        if kw.get("percent") is not None:
            parts.append("%d%%" % kw["percent"])
    elif done:
        parts.append(_human(done))
    sp = _fmt_speed(kw.get("speed"))
    if sp:
        parts.append(sp)
    eta = _fmt_eta(kw.get("eta"))
    if eta:
        parts.append("剩 " + eta)
    if kw.get("segmented"):
        parts.append("分段加速")
    head = kw.get("message") or "正在下载…"
    return (head + " " + " · ".join(parts)).strip() if parts else head


class Downloader:
    """内建下载器：分段并发 + 断点续传 + 镜像回退 + 实时进度。

    流程：
      1. 逐个候选地址（按设置顺序）探测：发 `Range: bytes=0-0`，看是否 206 且能读到
         总长度 —— 决定「能不能分段」。（实测部分 CDN 对 Range 回 501，那就退回单流。）
      2. 能分段且文件够大 → 按段并发下到 `dest.partN`，**已下好的段直接复用**（断点续传）。
         不能分段 → 单流下到 `dest.part`（也支持 Range 续传）。
      3. 合并 → 校验 sha256 → 改名。校验不过一律删除，绝不留可疑文件。
    """

    SEG_MIN = 4 * 1024 * 1024      # 小于这个体积不分段（收益抵不过开销）
    PROBE_TIMEOUT = 25

    def __init__(self, urls, dest, sha256="", size=None, on_progress=None,
                 concurrency=4, timeout=DOWNLOAD_TIMEOUT):
        self.urls = [u for u in urls if u]
        if not self.urls:
            raise ValueError("没有可用的下载地址")
        self.dest = dest
        self.sha256 = (sha256 or "").lower()
        self.size = size if isinstance(size, int) and size > 0 else None
        self.on_progress = on_progress or (lambda **kw: None)
        self.concurrency = max(1, min(8, int(concurrency or 1)))
        self.timeout = timeout
        self.total = self.size
        self.done_bytes = 0
        self.url = self.urls[0]
        self.segmented = False
        self._lock = threading.Lock()
        self._samples = []

    def run(self):
        last_err = None
        for url in self.urls:
            self.url = url
            try:
                total, ranges = self._probe(url)
                if total:
                    self.total = total
                self._report("downloading", "正在下载…")
                if ranges and self.total and self.total >= self.SEG_MIN and self.concurrency > 1:
                    self.segmented = True
                    self._segmented(self.total)
                else:
                    self._single()
                got = _sha256_file(self.dest)
                if self.sha256 and got != self.sha256:
                    os.unlink(self.dest)
                    raise RuntimeError("sha256 校验失败（期望 %s…，实际 %s…）"
                                       % (self.sha256[:16], got[:16]))
                return {"path": self.dest, "size": os.path.getsize(self.dest),
                        "sha256": got, "url": url, "segmented": self.segmented}
            except Exception as exc:  # noqa: BLE001 - 换下一个候选地址重试
                last_err = exc
                log("下载失败（%s）：%s" % (url, exc))
                self._cleanup_parts()
                self.done_bytes = 0
                self.segmented = False
                continue
        raise RuntimeError("所有下载地址都失败了：%s" % last_err)

    # ── 内部 ──
    def _probe(self, url):
        """探测 (总长度或 None, 是否支持 Range)。"""
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "fpk-update/1.0")
        req.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(req, timeout=self.PROBE_TIMEOUT) as r:
                code = getattr(r, "status", None) or r.getcode()
                cr = r.headers.get("Content-Range") or ""
                if code == 206 and "/" in cr:
                    tail = cr.rsplit("/", 1)[1].strip()
                    if tail.isdigit():
                        return int(tail), True
                cl = r.headers.get("Content-Length")
                return (int(cl) if cl and cl.isdigit() else None), False
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 416, 501, 505):
                req2 = urllib.request.Request(url, method="HEAD")
                req2.add_header("User-Agent", "fpk-update/1.0")
                try:
                    with urllib.request.urlopen(req2, timeout=self.PROBE_TIMEOUT) as r2:
                        cl = r2.headers.get("Content-Length")
                        return (int(cl) if cl and cl.isdigit() else None), False
                except Exception:  # noqa: BLE001
                    return None, False
            raise

    def _report(self, stage, message):
        with self._lock:
            now = time.time()
            self._samples.append((now, self.done_bytes))
            self._samples = [(t, b) for (t, b) in self._samples if now - t <= 4]
            speed = 0.0
            if len(self._samples) >= 2:
                (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
                if t1 > t0:
                    speed = (b1 - b0) / (t1 - t0)
            done, total = self.done_bytes, self.total
        pct = int(done * 100 / total) if total else None
        eta = int(max(0, total - done) / speed) if (total and speed > 1024) else None
        self.on_progress(stage=stage, message=message, percent=pct, downloaded=done,
                         total=total, speed=speed, eta=eta, url=self.url,
                         segmented=self.segmented)

    def _cleanup_parts(self):
        d = os.path.dirname(self.dest) or "."
        base = os.path.basename(self.dest)
        try:
            for n in os.listdir(d):
                if n.startswith(base + ".part") or n == base + ".merge":
                    try:
                        os.unlink(os.path.join(d, n))
                    except OSError:
                        pass
        except OSError:
            pass

    def _single(self):
        part = self.dest + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if self.total and have >= self.total:
            os.replace(part, self.dest)
            self.done_bytes = have
            self._report("downloading", "已完成（复用上次下好的文件）")
            return
        headers = {"User-Agent": "fpk-update/1.0"}
        if have:
            headers["Range"] = "bytes=%d-" % have
        req = urllib.request.Request(self.url, headers=headers)
        self.done_bytes = have
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            # 续传前必须确认服务端**真的按 Range 回应**：有些服务器/镜像直接忽略 Range
            # 回 200 整包。那样以附加模式写下去，文件会变成「半截 + 整包」的拼接体，
            # 最后 sha256 失败 → 报成「包被篡改/损坏」（其实只是这个源不支持续传），
            # 而且配了多个候选地址时每个地址都白下一遍整包。
            if have and getattr(r, "status", 200) != 206:
                log("该地址不支持断点续传（回了 %s），已丢弃半截文件从头下：%s"
                    % (getattr(r, "status", "?"), self.url))
                have = 0
                self.done_bytes = 0
            mode = "ab" if have else "wb"
            with open(part, mode) as f:
                last = time.time()
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    self.done_bytes += len(chunk)
                    if time.time() - last >= 0.4:
                        last = time.time()
                        self._report("downloading", "正在下载…")
                f.flush()
                os.fsync(f.fileno())
        # 到这里才说明整包收全了；半截文件与目标文件都不会被当成成品
        os.replace(part, self.dest)

    def _segmented(self, total):
        n = max(1, min(self.concurrency, (total + self.SEG_MIN - 1) // self.SEG_MIN))
        step = (total + n - 1) // n
        segs = []
        for i in range(n):
            start = i * step
            end = min(total - 1, start + step - 1)
            if start > end:
                break
            segs.append((i, start, end))

        for (i, start, end) in segs:      # 复用已下好的段（断点续传）
            p = "%s.part%d" % (self.dest, i)
            want = end - start + 1
            if os.path.isfile(p) and os.path.getsize(p) == want:
                self.done_bytes += want
        self._report("downloading", "分段下载中（%d 段并发）…" % len(segs))

        errors = []
        lock = threading.Lock()

        def fetch(i, start, end):
            p = "%s.part%d" % (self.dest, i)
            want = end - start + 1
            if os.path.isfile(p) and os.path.getsize(p) == want:
                return
            req = urllib.request.Request(self.url)
            req.add_header("User-Agent", "fpk-update/1.0")
            req.add_header("Range", "bytes=%d-%d" % (start, end))
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    got = 0
                    last = time.time()
                    with open(p, "wb") as f:
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                            with lock:
                                self.done_bytes += len(chunk)
                            if time.time() - last >= 0.4:
                                last = time.time()
                                self._report("downloading", "分段下载中…")
                        f.flush()
                        os.fsync(f.fileno())
                if got != want:
                    raise RuntimeError("第 %d 段长度不符（期望 %d，得到 %d）" % (i, want, got))
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append("第 %d 段：%s" % (i, exc))

        threads = [threading.Thread(target=fetch, args=s, daemon=True) for s in segs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise RuntimeError("；".join(errors[:3]))

        self._report("verifying", "正在合并并校验…")
        tmp = self.dest + ".merge"
        with open(tmp, "wb") as out:
            for (i, _s, _e) in segs:
                with open("%s.part%d" % (self.dest, i), "rb") as f:
                    shutil.copyfileobj(f, out, 1 << 20)
        os.replace(tmp, self.dest)
        for (i, _s, _e) in segs:
            try:
                os.unlink("%s.part%d" % (self.dest, i))
            except OSError:
                pass


def download_urls(asset_url, st=None):
    """候选下载地址：配了加速镜像就先走镜像，直连兜底（镜像挂了自动回落）。

    注意：走镜像不降低安全性 —— 下完照样要过 sha256 校验，哈希对不上直接丢弃。
    """
    st = st or load_settings()
    out = []
    prefix = (st.get("mirror") or "").strip()
    if prefix and asset_url.startswith(("http://github.com/", "https://github.com/")):
        out.append(prefix.rstrip("/") + "/" + asset_url)
    out.append(asset_url)
    if prefix and out and out[0] == asset_url:
        out = [asset_url]     # 镜像不适用于这个地址，只走直连
    return out


# ── 动作实现（都在队列工作线程里跑） ──

def _resolve_app(appname):
    """appname → 配置条目（列表里的，或本应用自身）。"""
    apps = {a["appname"]: a for a in load_whitelist()}
    if appname in apps:
        return apps[appname]
    # 自身更新不在列表里，但下载/安装/升级这条链路必须认它 ——
    # 而且**跨版本过渡期**也得认：用户点更新那一刻，页面还是旧版的 JS，
    # 它发的就是 /api/update?appname=fpk-update。拒掉会让升级卡死在半路。
    if appname == APP_NAME:
        return self_source()
    return None


def _task_progress(tid):
    def progress(stage, message, **fields):
        kw = {"stage": stage}
        if fields:
            kw["message"] = _dl_message(dict(fields, message=message))
            for k in ("percent", "downloaded", "total", "speed", "eta"):
                if k in fields:
                    kw[k] = fields[k]
        else:
            kw["message"] = message
        update_task(tid, **kw)
        log("[task %s] %s: %s" % (tid, stage, kw.get("message")))
    return progress


def _download_package(app, rel, tid):
    """把 Release 附件下载到下载目录并校验 sha256。返回 (路径, 字节数, sha256, manifest)。"""
    if not rel.get("sha256"):
        raise RuntimeError("该来源没有 sha256 摘要，出于安全拒绝下载")
    st = load_settings()
    d = st["download_dir"]
    os.makedirs(d, exist_ok=True)
    fname = rel["asset_name"]
    fpk_path = os.path.join(d, fname)

    urls = download_urls(rel["asset_url"], st)
    dl = Downloader(urls, fpk_path, sha256=rel["sha256"], size=rel.get("size"),
                    on_progress=_task_progress(tid),
                    concurrency=st.get("concurrency") or 4)
    res = dl.run()
    log("[%s] 已下载 %s size=%s sha256=%s 来源=%s%s"
        % (app["appname"], fpk_path, res["size"], res["sha256"][:16], res["url"],
           "（分段）" if res.get("segmented") else ""))

    # 下载完立刻用本地包复核身份 —— 权威口径（与 fnOS 安装时读的是同一份 manifest）
    mf = read_local_fpk_manifest(fpk_path)
    if mf["appname"] != app["appname"]:
        raise RuntimeError("下载到的安装包属于应用「%s」，与目标「%s」不符 —— 已中止"
                           % (mf["appname"], app["appname"]))
    # 记进台账：清理功能只认这份台账，同目录里用户自己的备份文件不会被误删
    record_package(fpk_path, app["appname"], mf["version"], res["size"], res["sha256"])
    return fpk_path, res["size"], res["sha256"], mf


def do_download(tid, appname):
    """【只下载】：下到本地并校验 sha256，不改动任何应用。"""
    app = _resolve_app(appname)
    if not app:
        raise RuntimeError("该应用不在列表里")
    try:
        update_task(tid, stage="checking", message="正在确认最新版本…")
        rel = resolve_source(app)
        fpk_path, total, got, mf = _download_package(app, rel, tid)
        update_task(tid, status="ok", stage="downloaded", percent=100,
                    message="已下载 %s（%s，sha256 %s…）" % (rel["asset_name"], _human(total), got[:16]),
                    version=mf["version"], path=fpk_path, size=total, sha256=got,
                    finished_at=now_iso())
    finally:
        # 下完要立刻刷新检测结果 —— 否则卡片上的「已下载/本地包」会一直停在旧状态，
        # 直到下一次检测（可能是一小时后）才更新（原来只有 do_update 刷新，是个疏漏）。
        _refresh_after_action()


def do_install(tid, appname):
    """【只安装】：用下载目录里已有的 fpk 走应用中心就地升级，不联网下载。"""
    app = _resolve_app(appname)
    if not app:
        raise RuntimeError("该应用不在列表里")
    try:
        update_task(tid, stage="checking", message="正在核对本地安装包…")
        rel = resolve_source(app)
        # 没有摘要就**不许安装** —— 与「下载」保持一致。原来这里用
        # `if rel.get("sha256") and ...` 短路，等于「来源没有摘要时完全不校验」，
        # 而下载目录往往就是用户的归档目录、里面可能有同名文件 →
        # 会用来源不明的本地文件覆盖一个已安装的应用，且没有任何完整性证据。
        if not rel.get("sha256"):
            raise RuntimeError("这个来源没有 sha256 摘要，出于安全不会安装"
                               "（可改用仓库来源，GitHub 的 Release 附件一般带 digest）")
        d = download_dir()
        fpk_path = os.path.join(d, rel["asset_name"])
        if not os.path.isfile(fpk_path):
            raise RuntimeError("下载目录里没有 %s —— 请先点「下载」" % rel["asset_name"])
        got = _sha256_file(fpk_path)
        if got != rel["sha256"]:
            raise RuntimeError("本地安装包 sha256 与 Release 不一致（可能已损坏或被替换）："
                               "期望 %s…，实际 %s…" % (rel["sha256"][:16], got[:16]))
        mf = read_local_fpk_manifest(fpk_path)
        if mf["appname"] != appname:
            raise RuntimeError("本地安装包属于应用「%s」，与目标「%s」不符" % (mf["appname"], appname))
        target_ver = mf["version"]
        cur = installed_version(appname)
        if norm_version(cur or "") == norm_version(target_ver):
            # 已经是这一版：不装，但本地这个包就是多余的 —— 自动清理开着就一并清掉，
            # 否则「装完自动清理」在这条路径上会静默失约（界面通常不会走到这里，
            # 因为同版本时「安装」按钮是禁用的，但 API 可以）。
            note = clean_after_install(appname, fpk_path, target_ver)
            update_task(tid, status="ok", stage="done", percent=100,
                        message="已是该版本（%s），无需安装%s" % (cur, note), version=target_ver,
                        path=("" if note else fpk_path), finished_at=now_iso())
            return
        update_task(tid, version=target_ver, stage="installing",
                    message="正在安装（%s → %s）…" % (cur, target_ver))
        upgrade_via_daemon(fpk_path, appname, target_ver, progress=_task_progress(tid))
        note = clean_after_install(appname, fpk_path, target_ver)
        update_task(tid, status="ok", stage="done", percent=100,
                    message="安装完成：%s → %s%s" % (cur, target_ver, note), version=target_ver,
                    path=("" if note else fpk_path), finished_at=now_iso())
        log("[%s] 安装成功 %s -> %s" % (appname, cur, target_ver))
    finally:
        _refresh_after_action()


def _refresh_after_action():
    """动作结束后刷新一次检测结果（失败也不能影响任务本身的结论）。"""
    try:
        run_check(trigger="after-action")
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())


def local_package_for(appname, rel):
    """本地是否已有「就是这一版、且校验过」的安装包？返回 (路径, manifest) 或 (None, None)。

    为什么要它：卡片上只有一个「更新」按钮，**「有包就装、没包才下」必须由后端判断**，
    不能让用户去选。实测白下过 340MB；而带 sha256 的校验过的包照着用是安全的。

    两条快慢路径：台账里记的就是这一版（快），否则直接比对内容（慢但正确）。
    """
    if not rel.get("sha256"):
        return None, None
    cand = os.path.join(download_dir(), rel.get("asset_name") or "")
    if not os.path.isfile(cand):
        return None, None
    led = _load_registry().get(os.path.realpath(cand)) or {}
    known = (led.get("sha256") == rel["sha256"][:16]
             and norm_version(led.get("version") or "") == norm_version(rel["version"]))
    try:
        if not known and _local_sha256_cached(cand) != rel["sha256"]:
            return None, None
        mf = read_local_fpk_manifest(cand)
        if (mf.get("appname") == appname
                and norm_version(mf.get("version")) == norm_version(rel["version"])):
            log("[%s] 复用本地已校验的包，跳过下载：%s" % (appname, cand))
            return cand, mf
    except Exception as exc:               # noqa: BLE001 - 本地包读不了就退回下载
        log("[%s] 本地包不可用（%s），改为下载" % (appname, exc))
    return None, None


def do_update(tid, appname):
    """【下载并更新】：下载 → 校验 → 应用中心就地升级 → 核验。"""
    app = _resolve_app(appname)
    if not app:
        raise RuntimeError("该应用不在列表里")
    fpk_path = None
    try:
        update_task(tid, stage="checking", message="正在确认最新版本…")
        rel = resolve_source(app)
        cur = installed_version(appname)
        if not is_newer(rel["version"], cur):
            update_task(tid, status="ok", stage="done", percent=100,
                        message="已是最新版本（%s）" % cur, version=cur,
                        finished_at=now_iso())
            return
        # 本地已有「就是这一版、且校验过」的包 → 直接用它，不重复下载。
        mf = None
        fpk_path, mf = local_package_for(appname, rel)
        if fpk_path:
            update_task(tid, stage="downloaded", percent=100,
                        message="本地已有校验过的 %s，直接安装（不重复下载）" % rel["version"])
        else:
            fpk_path, _total, _got, mf = _download_package(app, rel, tid)
        # 先把目标版本写进任务里：**自更新**（升级 fpk-update 自己）会在升级过程中被应用中心
        # 重启，进程没了、后面的代码不会执行 —— 重启后靠这个字段判断「到底装上了没」。
        update_task(tid, version=mf["version"], stage="installing",
                    message="正在升级（%s → %s）…" % (cur, mf["version"]))
        upgrade_via_daemon(fpk_path, appname, mf["version"], progress=_task_progress(tid))
        note = clean_after_install(appname, fpk_path, mf["version"])
        if note:
            fpk_path = ""                 # 已清理，别再当回滚包写在任务里
        update_task(tid, status="ok", stage="done", percent=100,
                    message="更新完成：%s → %s%s" % (cur, mf["version"], note),
                    version=mf["version"], path=fpk_path, finished_at=now_iso())
        log("[%s] 更新成功 %s -> %s" % (appname, cur, mf["version"]))
    except Exception as exc:  # noqa: BLE001
        _fail_task(tid, appname, exc, keep_path=fpk_path)
        return
    finally:
        _refresh_after_action()


def _fail_task(tid, appname, exc, keep_path=None):
    detail = str(exc)
    log("[%s] 失败：%s" % (appname, detail))
    keep = ""
    if keep_path and os.path.isfile(keep_path):
        keep = "（已下载的安装包保留在 %s，可用于回滚）" % keep_path
    update_task(tid, status="failed", stage="error",
                message="%s %s" % (detail, keep), finished_at=now_iso())


_LOCAL_HASH_CACHE = {}          # realpath -> (size, mtime, sha256)


def _local_sha256_cached(path):
    """算本地文件的 sha256，按 (大小, mtime) 缓存。

    为什么要**按内容**认包、而不是看「谁下载的」：下载目录往往同时是用户的归档目录，
    里面的包很可能是别的脚本（定时检测之类）下进去的。只说「不是本应用下载的」对用户
    毫无用处 —— 有用的是「这份文件的字节是不是就是那个 Release 的字节」。
    实测就是这么误伤过：归档目录里 6 个包全被标成「外来」，连「安装」都被禁用。

    缓存键带 mtime：文件一被覆盖就重算。350MB 的包算一次约 1 秒，而检测是并行的、
    且只在 (大小, mtime) 变化后才会重算，代价可接受。
    """
    rp = os.path.realpath(path)
    try:
        st = os.stat(rp)
    except OSError:
        return ""
    key = (st.st_size, st.st_mtime_ns)
    with _lock:
        hit = _LOCAL_HASH_CACHE.get(rp)
        if hit and hit[0] == key[0] and hit[1] == key[1]:
            return hit[2]
    try:
        h = _sha256_file(rp)
    except OSError:
        return ""
    with _lock:
        _LOCAL_HASH_CACHE[rp] = (key[0], key[1], h)
        if len(_LOCAL_HASH_CACHE) > 100:
            for k in list(_LOCAL_HASH_CACHE)[:-100]:
                _LOCAL_HASH_CACHE.pop(k, None)
    return h


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _human(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "?"


ACTIONS = {
    "download": do_download,
    "install": do_install,
    "update": do_update,
}

# ───────────────────────────── 设置 ─────────────────────────────
#
# 本应用【没有登录/密码】—— 打开即用。为了安全，能力被硬限死（见文件头说明），
# 且若将来要挂到公网，请在反向代理层加认证（如 Lucky 的 Basic Auth），
# 不要把本服务直接暴露到公网。
#
# settings.json 里目前五项：
#   download_dir  —— 安装包下载目录（默认 $TRIM_PKGVAR/downloads）
#   concurrency   —— 分段并发数（1-8，默认 4；1 = 单流不分段）
#   mirror        —— 加速镜像前缀（"" = 直连；非空时先走镜像，直连兜底）
#   auto_download —— 检测到有新版本时自动下载（**只下载，绝不自动安装**）
#   auto_clean    —— 安装成功后自动清理刚装的那个包（默认开；装成功了这个包就没用了，
#                    而且它是刚下下来的、随时能从 Release 再取。失败时一律保留作回滚包）
#   theme         —— 界面主题：auto（跟随系统）/ light / dark
THEME_CHOICES = ("auto", "light", "dark")
MIRROR_PRESETS = [
    {"key": "", "label": "直连（默认）", "prefix": ""},
    {"key": "gh-proxy", "label": "gh-proxy.com", "prefix": "https://gh-proxy.com"},
    {"key": "ghfast", "label": "ghfast.top", "prefix": "https://ghfast.top"},
    {"key": "ghproxy-net", "label": "ghproxy.net", "prefix": "https://ghproxy.net"},
    {"key": "gh-ddlc", "label": "gh.ddlc.top", "prefix": "https://gh.ddlc.top"},
]


def load_settings():
    data = load_json(SETTINGS_FILE)
    if not isinstance(data, dict):
        data = {}
    dd = str(data.get("download_dir") or "").strip()
    if not dd or not os.path.isabs(dd):
        dd = DEFAULT_DOWNLOAD_DIR
    try:
        conc = int(data.get("concurrency") or 4)
    except (TypeError, ValueError):
        conc = 4
    conc = max(1, min(8, conc))
    mir = str(data.get("mirror") or "").strip()
    if mir and not re.match(r"^https?://[^\s]+$", mir):
        mir = ""
    th = str(data.get("theme") or "auto").strip().lower()
    if th not in THEME_CHOICES:
        th = "auto"
    # 装完自动清理安装包（默认开）：装成功了这个包就没用了，而它是刚下下来的、
    # 随时能从 Release 再取。失败/未核验通过时绝不删（保留作回滚包）。
    auto_clean = data.get("auto_clean", True)
    # 清理时是否连「当前版本」的安装包一起清（默认**保留**）。
    # 为什么默认保留：当前版本的包就是回滚/修复用的那一份，实测用户的归档目录被
    # 「一键清理」一次删掉 6 个包共 383MB（都是当前版本），要回滚就只能重新下载。
    # 旧版本 + 下载残留照清，不占用「清不掉」的借口。
    keep_current = data.get("clean_keep_current", True)
    # 本应用自身的更新仓库（界面上方「本应用」区块用哪条来源）。
    # 留空 = 用打包本应用的那个默认仓库（设置里能看到是哪个）。
    self_repo = str(data.get("self_repo") or "").strip()
    _m = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?/?$", self_repo)
    if _m:
        self_repo = "%s/%s" % (_m.group(1), _m.group(2))
    if self_repo and not REPO_RE.match(self_repo):
        self_repo = ""
    return {"download_dir": dd, "concurrency": conc, "mirror": mir,
            "auto_download": bool(data.get("auto_download")),
            "auto_clean": bool(auto_clean),
            "clean_keep_current": bool(keep_current),
            "self_repo": self_repo,
            "theme": th}


def download_dir():
    return load_settings()["download_dir"]


def validate_download_dir(path):
    """校验用户填的下载目录。返回 (ok, 归一化路径或错误信息)。

    拒绝的几类（都是会真出事的）：
      · 相对路径 —— 进程工作目录不确定，落点会漂
      · / 或 /vol* 这类过宽的根
      · /vol*/@appcenter/... —— 应用安装目录，升级时会被整体替换
      · /vol*/@appdata|@apphome|@appconf|@appmeta|@appshare|@apptemp/...
        —— fnOS 应用私有目录，写进去会被覆盖、也可能破坏别的应用
        （唯独本应用自己的 @appdata 是允许的）
      · /tmp、/var/tmp —— 系统重启就没了
    允许：本应用自己的数据目录、用户存储（/vol*/<uid>/...）、以及任意其它可写目录。
    """
    p = os.path.normpath((path or "").strip())
    if not p or not os.path.isabs(p):
        return False, "必须是绝对路径（例如 /vol2/backups/fpk）"
    if p in ("/",) or re.match(r"^/vol[0-9]+$", p):
        return False, "不能直接用存储根目录，请指定一个子目录"
    if re.match(r"^/vol[0-9]+/@appcenter(/|$)", p):
        return False, "不能放在应用安装目录（@appcenter）里 —— 升级时会被整体替换"
    # fnOS 的应用私有目录一律不许写：写进去会被应用/系统覆盖，也可能破坏别的应用。
    # 自己的 @appdata 例外（那是本应用自己的地盘）。
    m_priv = re.match(r"^/vol[0-9]+/@(appdata|apphome|appconf|appmeta|appshare|apptemp)(/|$)", p)
    if m_priv:
        kind = m_priv.group(1)
        if kind == "appdata":
            m_sub = re.match(r"^/vol[0-9]+/@appdata/([^/]+)(/|$)", p)
            if m_sub and m_sub.group(1) == APP_NAME:
                pass                     # 自己的数据目录，允许
            else:
                return False, "不能放在应用数据目录（@appdata/…）里 —— 那是应用自己的地盘"
        else:
            return False, ("不能放在 fnOS 应用私有目录（@%s）里 —— 那是应用自己的地盘，"
                           "写进去会被覆盖或破坏" % kind)
    if p in ("/tmp", "/var/tmp") or p.startswith("/tmp/") or p.startswith("/var/tmp/"):
        return False, "不能放在 /tmp 下 —— 系统重启会清空，下载好的安装包会丢"
    try:
        os.makedirs(p, exist_ok=True)
    except OSError as exc:
        return False, "无法创建目录：%s" % exc
    probe = os.path.join(p, ".fpk-update-write-test")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
    except OSError as exc:
        return False, "目录不可写：%s" % exc
    return True, p


def save_settings(new):
    # 读改写要加锁：两个并发的设置请求（界面同时改主题和自动清理）各自读到旧值再写回，
    # 后写的那次会覆盖前一次，用户以为改好了其实没生效。
    with _lock:
        cur = load_settings()
        cur.update(new)
        save_json(SETTINGS_FILE, cur)
    return cur


# ─────────────────── 安装包台账与清理（一键清理 / 装完自动清理） ───────────────────
#
# 为什么要有「台账」而不是「把下载目录里的 *.fpk 全删了」：
# 下载目录是用户自己选的，很可能就是他放安装包备份的目录 —— 里面除了本应用下的包，
# 还躺着他自己归档的旧版本、别处 manual 拷进来的包。一律全删就是事故。
# 所以只清理「本应用确实下载过的（台账）」，以及「与当前这一版附件同名的（本来就是
# 本应用要用的那个文件）」，其余文件一概不碰。


def _load_registry():
    data = load_json(PKG_REGISTRY_FILE)
    return data if isinstance(data, dict) else {}


def _save_registry(d):
    save_json(PKG_REGISTRY_FILE, d)


def record_package(path, appname="", version="", size=0, sha256=""):
    """记下本应用下载过的安装包。清理功能只认这份台账。"""
    p = os.path.realpath(path)
    # 读改写整体加锁：下载线程在写台账的同时，网页可能在清理（也在改这份台账），
    # 不加锁会丢条目 —— 后果是那个包从此「清理不掉」（不会误删，但会残留）。
    with _lock:
        d = _load_registry()
        d[p] = {"appname": appname, "version": version, "size": int(size or 0),
                "sha256": (sha256 or "")[:16], "at": now_iso()}
        if len(d) > 500:                  # 只留最近 500 条，避免台账无限膨胀
            for k in list(d)[:-500]:
                d.pop(k, None)
        _save_registry(d)
    return p


def forget_package(path):
    p = os.path.realpath(path)
    with _lock:
        d = _load_registry()
        if d.pop(p, None) is not None:
            _save_registry(d)


def _foreign_asset_paths():
    """本目录里「当前版本对应的那个附件」的路径 —— 这些**不是**本应用下载的。

    用途只有一个：让界面如实告诉你「这些我看见了，但不碰」。
    为什么不把它们列入可清理：下载目录往往就是用户归档安装包的目录，而他大概率
    从别处（启动脚本、手工拷贝）把包放进来的 —— 那是他的回滚包。实测中这里躺着
    一份 357MB 的 Ekko Studio 包，而它在 GitHub 上的附件被重传过，删掉就再也拿不回
    逐字节相同的一份。清理功能只该清「本应用自己下下来的」，不该替用户决定归档去留。
    """
    out = {}
    try:
        d = download_dir()
    except Exception:                     # noqa: BLE001
        return out
    for r in (cached_state().get("apps") or []):
        nm = r.get("asset_name")
        if nm:
            out[os.path.realpath(os.path.join(d, nm))] = {
                "appname": r.get("appname") or "", "version": r.get("latest") or ""}
    return out


def _package_in_use(path):
    """正在被进行中/排队中的任务使用的包不许删。"""
    p = os.path.realpath(path)
    for t in list_tasks():
        if t.get("status") in ("running", "queued") and t.get("path"):
            try:
                if os.path.realpath(t["path"]) == p:
                    return True
            except OSError:
                pass
    return False


def list_packages():
    """列出「可以清理的安装包」—— **只含本应用自己下载过的**（台账）。

    同目录里其它来源的包不进这个清单（见 _foreign_asset_paths）。
    """
    try:
        os.path.realpath(download_dir())
    except Exception:                     # noqa: BLE001
        return []
    with _lock:                           # 读改写整体在锁内（与下载线程的登记互斥）
        seen, reg, changed = {}, _load_registry(), False
        for p, meta in list(reg.items()):
            if not os.path.isfile(p):     # 已经不在了 → 顺手把台账清干净
                reg.pop(p, None)
                changed = True
                continue
            seen[p] = dict(meta)
        if changed:
            _save_registry(reg)
    items = []
    inst_cache = {}
    for p, meta in seen.items():
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        app = meta.get("appname") or ""
        if app not in inst_cache:
            inst_cache[app] = installed_version(app) or ""
        inst = inst_cache[app]
        # current=True：这份包就是「当前装着的那一版」——回滚/修复都用得上，默认不清
        is_current = bool(inst) and bool(meta.get("version")) \
            and norm_version(inst) == norm_version(meta.get("version"))
        items.append({"path": p, "name": os.path.basename(p),
                      "appname": app,
                      "version": meta.get("version") or "",
                      "size": sz, "at": meta.get("at") or "",
                      "current": is_current,
                      "in_download_dir": os.path.dirname(p) == os.path.realpath(download_dir()),
                      "locked": _package_in_use(p)})
    items.sort(key=lambda x: (x["appname"], x["name"]))
    return items


def foreign_packages():
    """下载目录里本应用**不会动**的安装包（直接扫目录，只用于如实告知用户）。

    为什么扫目录、而不是从检测结果的 asset_name 反推：某个应用一旦检测失败
    （限流 / tag 钉错 / 仓库改名 / 条目被移除），它的 asset_name 就成了 None，
    那份归档包会**从两个清单里一起消失** —— 用户看到的占用数字严重偏小。
    实测：目录里一份 357MB 的 Ekko 包就这样「隐身」过。
    """
    owned = {os.path.realpath(i["path"]) for i in list_packages()}
    out = []
    try:
        d = os.path.realpath(download_dir())
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for nm in names:
        if not nm.lower().endswith(".fpk"):
            continue
        p = os.path.join(d, nm)
        if os.path.islink(p) or not os.path.isfile(p) or os.path.realpath(p) in owned:
            continue
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        out.append({"name": nm, "size": sz, "appname": ""})
    return out


def package_summary():
    items = list_packages()
    keep = bool(load_settings().get("clean_keep_current", True))
    stale = [i for i in items if not (keep and i.get("current"))]
    kept = [i for i in items if keep and i.get("current")]
    fgn = foreign_packages()
    parts = [p for p in orphan_parts() if p["old_enough"]]
    return {"count": len(stale), "size": sum(i["size"] for i in stale),
            "keep_count": len(kept), "keep_size": sum(i["size"] for i in kept),
            "foreign_count": len(fgn), "foreign_size": sum(i["size"] for i in fgn),
            "parts_count": len(parts), "parts_size": sum(p["size"] for p in parts)}


PART_KEEP_SEC = 3600              # 分片文件闲置超过这么久才算「孤儿」

# 下载引擎下大包时会先写 `<包名>.part0/.part1/...`，合并时还会临时写 `.merge`。
# 正常结束会自己删掉；但下载失败、或「下到一半换了版本」（目标文件名变了）时，
# 这些中间文件就永远留在下载目录里 —— 而清理功能只认 .fpk 台账，一辈子清不掉。
# 判据用**闲置时间**而不是「有没有任务在跑」：进行中的下载在持续写自己的分片、
# mtime 必然很新，所以「闲置一小时以上」既能清掉孤儿，又绝不会碰到正在下的分包。


def orphan_parts(now=None):
    """下载目录里残留的中间文件（`*.fpk.partN` / `*.fpk.merge`），只列闲置够久的。"""
    try:
        d = os.path.realpath(download_dir())
    except Exception:                     # noqa: BLE001
        return []
    now = now if now is not None else time.time()
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for nm in names:
        if not re.match(r"^.+\.fpk\.(part[0-9]+|merge)$", nm):
            continue
        full = os.path.join(d, nm)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        out.append({"path": full, "name": nm, "size": st.st_size,
                    "idle": int(now - st.st_mtime),
                    "old_enough": (now - st.st_mtime) >= PART_KEEP_SEC})
    return out


def _safe_unlink_part(path):
    """删一个中间分片文件。护栏：必须在下载目录直属层级、名字形态对得上、闲置够久。"""
    p = os.path.realpath(str(path or ""))
    if os.path.islink(str(path or "")):
        return False, "这是符号链接，拒绝删除"
    if os.path.dirname(p) != os.path.realpath(download_dir()):
        return False, "不在当前的安装包下载目录里，拒绝删除"
    if not re.match(r"^.+\.fpk\.(part[0-9]+|merge)$", os.path.basename(p)):
        return False, "不是下载中间文件"
    if not os.path.isfile(p):
        return False, "文件不存在"
    try:
        idle = time.time() - os.stat(p).st_mtime
    except OSError:
        return False, "读不到文件状态"
    if idle < PART_KEEP_SEC:
        return False, "可能正在下载中（闲置不足 %d 秒），拒绝删除" % PART_KEEP_SEC
    os.unlink(p)
    return True, ""


def _safe_unlink(path):
    """删一个安装包。所有护栏都在这一个函数里，清理功能不可能越界。

    逐条拒绝：非法路径 / 符号链接 / 非 .fpk / 不在当前下载目录的直属层级 /
    文件不存在 / 正被进行中的任务占用。
    """
    if os.path.islink(str(path or "")):
        return False, "这是符号链接，拒绝删除"
    p = os.path.realpath(str(path or ""))
    if not p or not os.path.isabs(p):
        return False, "路径不合法"
    if not p.lower().endswith(".fpk"):
        return False, "只清理 .fpk 安装包"
    if os.path.dirname(p) != os.path.realpath(download_dir()):
        return False, "不在当前的安装包下载目录里，拒绝删除"
    if not os.path.isfile(p):
        return False, "文件不存在"
    if _package_in_use(p):
        return False, "正被进行中的任务使用，请稍后再试"
    if p not in _load_registry():
        # 兜底：不是本应用下载的包，一律不删。目录里可能躺着用户的归档/回滚包。
        return False, "不是本应用下载的安装包，拒绝删除"
    os.unlink(p)
    forget_package(p)
    return True, ""


def clean_packages(include_current=None):
    """一键清理：删掉「用不上的」安装包 + 残留的下载中间文件。

    `include_current` 缺省时读设置 `clean_keep_current`（默认 True）：
      保留当前版本的包（回滚/修复要用），只清旧版本 + 残留。
    """
    if include_current is None:
        include_current = not load_settings().get("clean_keep_current", True)
    deleted, kept, failed, freed = [], [], [], 0
    for it in list_packages():
        if it["locked"]:
            failed.append({"name": it["name"], "reason": "正被进行中的任务使用"})
            continue
        if it.get("current") and not include_current:
            kept.append(it["name"])
            continue
        ok, why = _safe_unlink(it["path"])
        if ok:
            deleted.append(it["name"])
            freed += it["size"]
        else:
            failed.append({"name": it["name"], "reason": why})
    parts, parts_freed = [], 0
    for it in orphan_parts():
        if not it["old_enough"]:
            continue
        ok, why = _safe_unlink_part(it["path"])
        if ok:
            parts.append(it["name"])
            parts_freed += it["size"]
        else:
            failed.append({"name": it["name"], "reason": why})
    return {"deleted": deleted, "kept": kept, "failed": failed, "freed": freed,
            "parts": parts, "parts_freed": parts_freed}


def clean_after_install(appname, fpk_path, target_version):
    """安装成功后清理「刚装的这个包」（可选，默认开）。

    只在两道核验都过了才删：**装完的版本确实等于目标版本**，且应用目录还在。
    任何一样不成立就保留安装包 —— 它此时是回滚包，删了就是自断后路。
    返回给任务消息用的后缀。
    """
    if not load_settings().get("auto_clean", True):
        return ""
    if not fpk_path or not os.path.isfile(fpk_path):
        return ""
    try:
        got = norm_version(installed_version(appname) or "")
        if got != norm_version(target_version):
            log("[%s] 升级后核验到的版本是 %s（目标 %s），保留安装包不清理"
                % (appname, got or "?", target_version))
            return ""
        if not os.path.isdir(os.path.join(APPS_ROOT, appname)):
            log("[%s] 应用目录不存在，保留安装包不清理" % appname)
            return ""
    except OSError:
        return ""
    ok, why = _safe_unlink(fpk_path)
    if ok:
        log("[%s] 安装成功，已清理安装包 %s" % (appname, fpk_path))
        return "，安装包已清理"
    log("[%s] 安装包未清理：%s" % (appname, why))
    return ""


# ───────────────────────────── HTTP ─────────────────────────────

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
}


def _entry_from_request(body):
    """从请求体里取出「来源 + 可选的版本/安装包指定」。"""
    raw = str(body.get("source") or "").strip()
    if not raw:
        raise ValueError("请填写仓库地址（owner/name 或完整链接），或 fpk 直链")
    entry = {"repo": raw} if ("/" in raw and not raw.startswith("http")) else {"url": raw}
    if raw.startswith("http") and "github.com" not in raw:
        entry = {"url": raw}
    tag = str(body.get("tag") or "").strip()
    asset = str(body.get("asset") or "").strip()
    if tag:
        entry["tag"] = tag
    if asset:
        entry["asset"] = asset
    src = normalize_source(entry)
    if not src["repo"] and not src["url"]:
        raise ValueError("看起来不是仓库地址，也不是 http(s) 直链")
    return raw, src, entry


class Handler(BaseHTTPRequestHandler):
    server_version = "FPKUpdate/1.0"
    protocol_version = "HTTP/1.1"

    # ── 小工具 ──
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # 本应用会被 fnOS 桌面小窗 iframe 嵌入，且可能经反代/自有域名访问，
        # 因此不做父页面白名单（与自建 fpk 的既定口径一致）。
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "frame-ancestors *")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), extra=extra)

    def _err(self, code, msg):
        self._json({"ok": False, "error": msg}, code=code)

    def _same_origin_ok(self):
        """POST 必须带自定义头。本应用没有登录，这道检查挡的是「别的网站
        让浏览器替我们发请求」—— 跨站请求带不上自定义头（且我们不发 CORS 头）。"""
        return self.headers.get("X-Requested-With") == "fpk-update"

    def _read_body(self):
        """读请求体。

        关键约束：**读不完就必须断开连接**。HTTP/1.1 是长连接，socket 里剩下的字节会被
        BaseHTTPRequestHandler 当成下一条请求行解析 —— 表现为「同一条连接上的后续请求
        莫名 400/垃圾响应」。本应用的请求体都是几十字节，所以超限直接拒并断开最干脆。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            # Content-Length 本身不合法：无法知道还有多少字节没读 → 断开
            self.close_connection = True
            return {}
        if n <= 0:
            return {}
        if n > MAX_BODY_BYTES:
            log("请求体过大（%d 字节 > %d），拒绝并断开连接：%s"
                % (n, MAX_BODY_BYTES, self.path))
            self.close_connection = True
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    # ── 路由 ──
    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        """预检请求。

        为什么必须有：BaseHTTPRequestHandler 对**未实现的方法**一律回
        `501 Unsupported method` + 一张 HTML 错误页。而前端拿到非 JSON 响应时只能报
        「请求失败（HTTP 501）」—— 用户看到的就是这条没头没尾的红字（实测反馈）。
        跨源/反向代理场景下浏览器会先发 OPTIONS 预检，本应用又是自定义头 + JSON 的 POST，
        所以这里必须显式应答，而不是让它掉进 501。
        """
        self.close_connection = True
        self._send(204, b"", ctype="text/plain; charset=utf-8", extra={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Requested-With",
            "Access-Control-Max-Age": "600",
        })

    def _method_not_allowed(self):
        """其它方法（PUT/DELETE/PATCH…）：明确回 405 JSON，不要回 501 HTML。"""
        self._json({"ok": False, "error": "不支持 %s 方法（本应用只有 GET / POST / OPTIONS）"
                                       % self.command}, code=405,
                   extra={"Allow": "GET, POST, HEAD, OPTIONS"})

    def do_PUT(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    def do_PATCH(self):
        self._method_not_allowed()

    def do_GET(self):
        try:
            self._route_get()
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            log("GET %s 出错: %s" % (self.path, exc))
            log(traceback.format_exc())
            try:
                self._err(500, "服务内部错误")
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self):
        try:
            self._route_post()
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            log("POST %s 出错: %s" % (self.path, exc))
            log(traceback.format_exc())
            try:
                self._err(500, "服务内部错误")
            except Exception:  # noqa: BLE001
                pass

    def _route_get(self):
        path = urlparse(self.path).path

        if path == "/api/health":
            return self._json({"ok": True, "time": datetime.now(CST).isoformat(timespec="seconds")})

        if path == "/api/icon/":
            return self._err(400, "缺少应用名")

        if path.startswith("/api/icon/"):
            appname = path[len("/api/icon/"):].strip("/")
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", appname):
                return self._err(400, "应用名非法")
            # 只允许白名单内的应用，避免变成任意文件读取
            if appname not in {a["appname"] for a in load_whitelist()}:
                return self._err(404, "不在白名单内")
            p = icon_path(appname)
            if not p:
                return self._err(404, "没有图标")
            # 图标只在应用升级时才变，而前端每次轮询都会重绘卡片 —— 必须让浏览器缓存。
            # 原来是 no-cache 且无 ETag：实测每轮轮询都会重拉 6 个图标（1 秒一次）。
            # 前端还会带上 ?v=<已装版本>，升级后 URL 变化 → 自动换新图，不会拿到旧图标。
            etag = None
            try:
                stt = os.stat(p)
                etag = '"%x-%x"' % (int(stt.st_mtime), stt.st_size)
            except OSError:
                pass
            extra = {"Cache-Control": "public, max-age=604800"}
            if etag:
                extra["ETag"] = etag
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    for k, v in extra.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            with open(p, "rb") as f:
                data = f.read()
            return self._send(200, data, ctype="image/png", extra=extra)

        if path == "/api/state":
            st = cached_state()
            tasks = list_tasks()
            # 服务重启后残留的 running 状态 → 归为未知，别让界面永远转圈
            for t in tasks:
                if t.get("status") == "running" and not _worker_alive():
                    # ⚠️ 变量名不要用 st —— 上面 st 是缓存状态，被这里覆盖后
                    # 后面的 st.get("at") 会抛异常、整个 /api/state 变 500（实测踩过）
                    _jst, _jstage, _jmsg, _jpct = judge_interrupted(t)
                    update_task(t["id"], status=_jst, stage=_jstage, message=_jmsg,
                                percent=(_jpct if _jpct is not None else t.get("percent")))
            tasks = list_tasks()
            return self._json({
                "ok": True,
                "last_check": st.get("at"),
                "last_trigger": st.get("trigger"),
                "apps": st.get("apps") or [],
                # 本应用自身：单独一格（界面上是列表上方独立的一块）
                "self": st.get("self"),
                "self_repo": self_source()["repo"],
                "self_default_repo": SELF_DEFAULT_REPO,
                "tasks": tasks,
                "job": current_task() or _last_task(tasks),
                "check_interval_min": CHECK_INTERVAL_MIN,
                "busy": bool(current_task()),
                "whitelist_file": APPS_USER,
                "has_token": bool(gh_token()),
                "token_hint": token_hint(),
                "token_file": GH_TOKEN_FILE,
                "settings": load_settings(),
                "default_download_dir": DEFAULT_DOWNLOAD_DIR,
                "mirror_presets": MIRROR_PRESETS,
                "whitelist": load_whitelist(),
                "device_arch": device_arch(),
                # 本应用自己的版本（读 /var/apps/fpk-update/manifest，即当前真正在跑的这份）。
                # 之前界面上一个版本号都没有 —— 用户想确认「我装的是哪一版」无处可看。
                "app_version": installed_version(APP_NAME) or "",
                "checking": check_state(),
                "packages": package_summary(),
            })

        if path == "/api/packages":
            items = list_packages()
            fgn = foreign_packages()
            return self._json({"ok": True, "dir": download_dir(), "items": items,
                               "count": len(items),
                               "size": sum(i["size"] for i in items),
                               "foreign": fgn,
                               "foreign_count": len(fgn),
                               "foreign_size": sum(i["size"] for i in fgn)})

        if path == "/api/tasks":
            return self._json({"ok": True, "tasks": list_tasks(),
                               "busy": bool(current_task())})

        if path == "/api/log":
            try:
                n = int(parse_qs(urlparse(self.path).query).get("lines", ["120"])[0])
            except ValueError:
                n = 120
            n = max(10, min(500, n))
            lp = os.path.join(VAR_DIR, "fpk-update.log")
            lines = []
            try:
                with open(lp, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-n:]
            except OSError:
                pass
            return self._json({"ok": True, "lines": [l.rstrip("\n") for l in lines]})

        # 静态资源
        if path == "/":
            path = "/index.html"
        rel = path.lstrip("/")
        if ".." in rel.split("/"):
            return self._err(400, "非法路径")
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            return self._err(404, "找不到资源")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        # 静态资源一律 no-cache：升级 fpk 后文件名不变（app.js/style.css），
        # 不加这个头，浏览器/桌面小窗可能长时间用旧界面（实测风险）。
        return self._send(200, data, ctype=MIME.get(ext, "application/octet-stream"),
                          extra={"Cache-Control": "no-cache"})

    def _route_post(self):
        path = urlparse(self.path).path
        if not self._same_origin_ok():
            return self._err(403, "缺少必要请求头")

        if path == "/api/check":
            body = self._read_body()
            # 节流：10 秒内重复点「检查更新」直接返回上次结果，不再打 GitHub。
            # 理由：本应用没有登录，多设备/误触/页面刷新都可能连点，而匿名额度只有
            # 60 次/小时且按出口 IP 共享 —— 白白烧掉额度会让检测真的失败。
            st0 = cached_state()
            if not body.get("force") and st0.get("at"):
                try:
                    last = datetime.fromisoformat(st0["at"])
                    if (datetime.now(CST) - last).total_seconds() < 10:
                        log("检查节流：10 秒内刚查过，直接返回上次结果")
                        return self._json({"ok": True, "started": False, "throttled": True,
                                           "apps": st0.get("apps") or [],
                                           "last_check": st0.get("at"),
                                           "checking": check_state()})
                except ValueError:
                    pass
            if check_state()["running"]:
                # 已经在查了：不要再排一轮。连点不该变成「排队打 GitHub」。
                return self._json({"ok": True, "started": False, "running": True,
                                   "checking": check_state()})
            # 立刻返回，检查在后台跑：实测串行 6 个应用要 6.1 秒，
            # 前端若是 await 到底，用户看到的就是「点一下卡六七秒、屏幕毫无变化」。
            # force：用户手动点的「检查更新」= 我要看到此刻的真相，
            # 所以连 Release 列表的 15 秒缓存也一起绕过（改完配置/刚发版就用得上）。
            threading.Thread(target=run_check, args=("manual",),
                             kwargs={"force": True}, daemon=True).start()
            return self._json({"ok": True, "started": True, "running": True,
                               "checking": check_state()})

        if path in ("/api/download", "/api/install", "/api/update"):
            body = self._read_body()
            appname = str(body.get("appname") or "")
            # 自身不在列表里，但这条链路必须放行它（见 _resolve_app 的说明）：
            # 用户点「更新」那一刻页面还是旧版 JS，发的就是这个接口。
            allowed = {a["appname"] for a in load_whitelist()} | {APP_NAME}
            if appname not in allowed:
                return self._err(400, "该应用不在列表里")
            action = path.rsplit("/", 1)[1]
            # 「已是最新就不下载」—— 界面会禁用按钮，但接口也必须拦：
            # 界面不是唯一调用方（脚本、误触、旧缓存页面都能发这个请求）。
            it = state_item(appname)
            if action in ("download", "update") and not download_needed(it):
                disp = (it or {}).get("display") or appname
                return self._err(409, "「%s」已经是最新（%s），无需%s"
                                      % (disp, (it or {}).get("installed") or "?",
                                         "下载" if action == "download" else "下载或更新"))
            try:
                tid = enqueue(appname, action)
            except ValueError as exc:
                return self._err(400, str(exc))
            return self._json({"ok": True, "task": tid, "message": "已加入队列"})

        if path == "/api/self/check":
            # 只重查自身：**绕缓存**（force）。为什么必须绕：用户刚给自己的应用发了一版，
            # 回来点「检查」，而 Release 列表有 15 秒缓存 —— 不绕的话界面会坚持说
            # 「已是最新」，看起来像坏了。
            self._read_body()
            if check_state()["running"]:
                return self._json({"ok": True, "started": False, "running": True,
                                   "checking": check_state()})
            # 立刻返回、后台跑（与「检查更新」同一个理由：不能让界面卡着等网络）
            threading.Thread(target=run_self_check, kwargs={"force": True}, daemon=True).start()
            return self._json({"ok": True, "started": True, "running": True,
                               "checking": check_state()})

        if path == "/api/self/update":
            # 自身更新的唯一入口。与列表里的应用走**同一套**队列/下载/校验/升级代码，
            # 区别只有三点：目标固定是自己、跑完页面要自动刷新、不进「全部更新」。
            self._read_body()
            it = cached_state().get("self") or {}
            if it.get("error"):
                return self._err(409, "自身状态未知（%s），请先点「检查」再更新" % it["error"])
            if not download_needed(it):
                return self._err(409, "本应用已经是最新（%s），无需更新"
                                      % (it.get("installed") or "?"))
            try:
                tid = enqueue(APP_NAME, "update")
            except ValueError as exc:
                return self._err(400, str(exc))
            return self._json({"ok": True, "task": tid, "message": "已加入队列"})

        if path == "/api/all":
            # 批量动作（对标手机应用商店的「全部更新」）。
            # 只对「确实该做」的应用排队：没更新可做的、有错的一律跳过并如实回报。
            body = self._read_body()
            action = str(body.get("action") or "update")
            if action not in ("download", "update"):
                return self._err(400, "批量只支持 download / update")
            results = cached_state().get("apps") or []
            if not results:
                return self._err(409, "还没有检测结果，请先点「检查更新」")
            queued, skipped = [], []
            for r in results:
                name = r.get("appname")
                if not name:
                    continue
                if action == "update":
                    ok = bool(r.get("update_available")) and not r.get("error")
                else:
                    # 同一判据：已是最新（且本地包没问题）就不排队，别白下几百 MB
                    ok = (bool(r.get("latest")) and not r.get("error")
                          and download_needed(r))
                if not ok:
                    skipped.append(name)
                    continue
                try:
                    enqueue(name, action)
                    queued.append(name)
                except Exception as exc:  # noqa: BLE001
                    skipped.append("%s（%s）" % (name, exc))
            log("[批量] %s：排队 %d 个（%s），跳过 %d 个"
                % (action, len(queued), ",".join(queued) or "-", len(skipped)))
            return self._json({"ok": True, "action": action,
                               "queued": queued, "skipped": skipped})

        if path == "/api/tasks/clear":
            return self._json({"ok": True, "tasks": clear_finished_tasks()})

        if path == "/api/clean":
            # 一键清理安装包 + 下载残留（中间分片）。**只清本应用自己下载过的**（台账）。
            # `dry` 只复盘不删（界面在确认前先取一次清单）。
            body = self._read_body()
            include_current = bool(body.get("include_current")) if "include_current" in body \
                else not load_settings().get("clean_keep_current", True)
            all_items = list_packages()
            items = [i for i in all_items if include_current or not i.get("current")]
            kept_items = [i for i in all_items if not include_current and i.get("current")]
            fgn = foreign_packages()
            parts = [p for p in orphan_parts() if p["old_enough"]]
            extra = {"keep_count": len(kept_items),
                     "keep_size": sum(i["size"] for i in kept_items),
                     "keep": [i["name"] for i in kept_items],
                     "foreign_count": len(fgn),
                     "foreign_size": sum(i["size"] for i in fgn),
                     "foreign": [i["name"] for i in fgn],
                     "parts": [p["name"] for p in parts],
                     "parts_count": len(parts),
                     "parts_size": sum(p["size"] for p in parts)}
            if not items and not parts:
                return self._json({"ok": True, "dry": bool(body.get("dry")),
                                   "items": [], "count": 0, "size": 0,
                                   "deleted": [], "failed": [], "freed": 0, **extra})
            if body.get("dry"):
                return self._json({"ok": True, "dry": True, "items": items,
                                   "count": len(items),
                                   "size": sum(i["size"] for i in items), **extra})
            res = clean_packages(include_current=include_current)
            log("[清理] 删除 %d 个安装包（%s）与 %d 个下载残留，释放 %s；保留 %d 个当前版本；未删 %d 个"
                % (len(res["deleted"]), ",".join(res["deleted"]) or "-",
                   len(res.get("parts") or []),
                   _human(res["freed"] + res.get("parts_freed", 0)),
                   len(res.get("kept") or []), len(res["failed"])))
            return self._json({"ok": True, "dry": False,
                               "count": len(res["deleted"]),
                               "size": sum(i["size"] for i in items),
                               "deleted": res["deleted"],
                               "kept": res.get("kept") or [],
                               "parts_deleted": res.get("parts") or [],
                               "parts_freed": res.get("parts_freed", 0),
                               "failed": res["failed"], "freed": res["freed"], **extra})

        if path == "/api/job/dismiss":
            return self._json({"ok": True, "tasks": clear_finished_tasks()})

        if path == "/api/settings":
            body = self._read_body()
            new = {}
            if "download_dir" in body:
                ok, res = validate_download_dir(str(body.get("download_dir") or ""))
                if not ok:
                    return self._err(400, res)
                new["download_dir"] = res
            if "concurrency" in body:
                try:
                    conc = int(body.get("concurrency"))
                except (TypeError, ValueError):
                    return self._err(400, "并发数必须是 1–8 的整数")
                if not 1 <= conc <= 8:
                    return self._err(400, "并发数必须在 1–8 之间（1 = 单流不分段）")
                new["concurrency"] = conc
            if "mirror" in body:
                mir = str(body.get("mirror") or "").strip()
                if mir and not re.match(r"^https?://[^\s]+$", mir):
                    return self._err(400, "加速镜像要填 http(s):// 开头的地址，留空 = 直连")
                new["mirror"] = mir
            if "auto_download" in body:
                new["auto_download"] = bool(body.get("auto_download"))
            if "auto_clean" in body:
                new["auto_clean"] = bool(body.get("auto_clean"))
            if "clean_keep_current" in body:
                new["clean_keep_current"] = bool(body.get("clean_keep_current"))
            if "theme" in body:
                th = str(body.get("theme") or "").strip().lower()
                if th not in THEME_CHOICES:
                    return self._err(400, "主题只能是 auto / light / dark")
                new["theme"] = th
            if "self_repo" in body:
                rp = str(body.get("self_repo") or "").strip()
                _m = re.match(r"^https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?/?$", rp)
                if _m:
                    rp = "%s/%s" % (_m.group(1), _m.group(2))
                if rp and not REPO_RE.match(rp):
                    return self._err(400, "自身更新仓库要填 owner/name（或完整 GitHub 链接）；"
                                          "留空 = 用默认的 %s" % SELF_DEFAULT_REPO)
                new["self_repo"] = rp
            if new:
                save_settings(new)
                log("设置已更新：%s" % new)
            return self._json({"ok": True, "settings": load_settings()})

        if path == "/api/apps/options":
            body = self._read_body()
            try:
                raw, src, entry = _entry_from_request(body)
                opts = list_options(entry)
            except ValueError as exc:
                return self._err(400, str(exc))
            except urllib.error.HTTPError as exc:
                return self._err(400, _gh_error_message(exc))
            except Exception as exc:  # noqa: BLE001
                return self._err(400, "无法列出可选版本：%s" % exc)
            opts["ok"] = True
            opts["source"] = src["repo"] or src["url"]

            # 顺手在后台把「推荐那个附件」的 manifest 预读一下。
            # 界面上的下一步就是「预览」，而预览最慢的一步就是读包内 manifest
            # （要向附件所在的 CDN 发几个 Range 请求，实测 1.4 秒）。这里先把它读进缓存，
            # 用户选完版本点「预览」时就直接命中，感觉是瞬间出来。
            def _prefetch(url, size):
                try:
                    read_fpk_manifest(url, size)
                except Exception:                     # noqa: BLE001 - 预取失败无所谓
                    pass

            _rels = opts.get("releases") or []
            if _rels:
                _rec = None
                for _a in (_rels[0].get("assets") or []):
                    if _a.get("recommended"):
                        _rec = _a
                        break
                if _rec is None and (_rels[0].get("assets") or []):
                    _rec = _rels[0]["assets"][0]
                if _rec and _rec.get("url"):
                    threading.Thread(target=_prefetch,
                                     args=(_rec["url"], _rec.get("size")),
                                     daemon=True).start()
            return self._json(opts)

        if path == "/api/apps/preview":
            body = self._read_body()
            try:
                raw, src, entry = _entry_from_request(body)
                rel = resolve_source(entry)
            except ValueError as exc:
                return self._err(400, str(exc))
            except urllib.error.HTTPError as exc:
                return self._err(400, _gh_error_message(exc))
            except Exception as exc:  # noqa: BLE001
                return self._err(400, "无法识别：%s" % exc)
            appname = rel["appname"]
            installed = installed_version(appname)
            return self._json({
                "ok": True,
                "appname": appname,
                "display_name": rel.get("display_name") or installed_display(appname) or appname,
                "version": rel["version"],
                "version_source": rel.get("version_source"),
                "platform": rel.get("platform"),
                "tag": rel.get("tag"),
                "asset_name": rel.get("asset_name"),
                "size": rel.get("size"),
                "has_sha256": bool(rel.get("sha256")),
                "installed": bool(installed),
                "installed_version": installed,
                "update_available": bool(installed) and is_newer(rel["version"], installed),
                "source": (rel.get("source_kind") == "repo" and src["repo"]) or src["url"],
            })

        if path == "/api/apps/add":
            body = self._read_body()
            try:
                raw, src, entry = _entry_from_request(body)
                # 自身更新不进列表（它有独立的 /api/self 链路 + 独立区块）。
                # 在这里就拦住，省掉一次没用的 GitHub 往返，也说清该去哪儿改它。
                if (src["repo"] or "").split("/")[-1] == APP_NAME:
                    return self._err(400, "这是本应用自己（%s）。自身更新在上方「本应用」区块里，"
                                          "不用加进列表；要换仓库请去 设置 → 应用。"
                                          % SELF_DEFAULT_REPO.split("/")[-1])
                # 先在本地查重（亚毫秒），再谈联网识别 ——
                # 重复添加原来会白跑一趟 GitHub（实测 914 毫秒）才告诉你「已经在列表里」。
                _key = src["repo"] or src["url"]
                _dup = False
                with _lock:
                    for _it in (_whitelist_doc().get("apps") or []):
                        if not isinstance(_it, dict):
                            continue
                        _s2 = normalize_source(_it)
                        if (_s2["repo"] or _s2["url"]) == _key:
                            _dup = True
                            break
                if _dup:
                    return self._err(409, "这个来源已经在列表里了（可用「换版本」改它的版本）")
                rel = resolve_source(entry)
            except ValueError as exc:
                return self._err(400, str(exc))
            except urllib.error.HTTPError as exc:
                return self._err(400, _gh_error_message(exc))
            except Exception as exc:  # noqa: BLE001
                return self._err(400, "无法识别：%s" % exc)
            appname = rel["appname"]
            # 包内应用名不合法就别写进配置：写进去会在读回时被静默丢弃（白名单校验更严），
            # 用户看到「已添加」但列表里什么都没有 —— 说不清到底发生了什么。
            if not APPNAME_RE.match(appname or ""):
                return self._err(400, "安装包内的应用名不合法（%r），拒绝添加" % appname)
            # 只能管理「本机已安装」的应用 —— 硬边界
            if not installed_version(appname):
                return self._err(400, "该安装包对应应用 %s，但本机没有安装它，无法管理"
                                      "（本工具只更新已安装的应用）" % appname)
            with _lock:
                doc = _whitelist_doc()
                for item in doc["apps"]:
                    if not isinstance(item, dict):
                        continue
                    s = normalize_source(item)
                    if (s["repo"] or s["url"]) == (src["repo"] or src["url"]):
                        return self._err(409, "这个来源已经在列表里了（可用「换版本」改它的版本）")
                new_item = {"appname": appname}
                if src["repo"]:
                    new_item["repo"] = src["repo"]
                else:
                    new_item["url"] = src["url"]
                new_item["display"] = rel.get("display_name") or appname
                if src["tag"]:
                    new_item["tag"] = src["tag"]
                if src["asset"]:
                    new_item["asset"] = src["asset"]
                doc["apps"].append(new_item)
                save_whitelist(doc)
            log("新增被管理应用：%s（来源 %s，版本 %s，包 %s）"
                % (appname, src["repo"] or src["url"], rel["version"], rel["asset_name"]))
            return self._json({"ok": True, "appname": appname, "apps": load_whitelist()})

        if path == "/api/apps/pin":
            # 给已有条目换版本 / 换安装包（不改来源）
            body = self._read_body()
            key = str(body.get("source") or "").strip()
            tag = str(body.get("tag") or "").strip()
            asset = str(body.get("asset") or "").strip()
            if not key:
                return self._err(400, "缺少 source")

            # 先在锁外做只读查找：把网络请求放到锁外，否则一次 GitHub 抖动会把
            # 所有白名单操作卡住（重试最多几十秒）。
            target_repo = None
            cur_tag = cur_asset = ""
            for item in (_whitelist_doc().get("apps") or []):
                if not isinstance(item, dict):
                    continue
                s = normalize_source(item)
                if (s["repo"] or s["url"]) == key or (s["appname"] and s["appname"] == key):
                    target_repo = s["repo"]
                    cur_tag = s.get("tag") or ""
                    cur_asset = s.get("asset") or ""
                    break
            if target_repo is None:
                return self._err(404, "列表里没有这个来源")

            # 校验用户钉的版本/附件**真实存在** —— 之前是不校验直接写，
            # 实测能把条目钉到一个根本不存在的 tag 上：界面显示「已改钉」，
            # 之后每次检测都失败，而错误信息看起来像是仓库的问题。
            # 只有「真的改了」才联网校验：内容没变时再打一次 GitHub 只是白等 0.7 秒
            if (tag or asset) and (tag != cur_tag or asset != cur_asset):
                try:
                    rel = find_release(target_repo, True, tag) if tag else \
                        find_release(target_repo, True, "")
                    if asset:
                        _pick_asset(rel["assets"], pattern=asset, strict=True)
                except ValueError as exc:
                    return self._err(400, "改钉失败：%s" % exc)
                except urllib.error.HTTPError as exc:
                    return self._err(400, "改钉失败：%s" % _gh_error_message(exc))
                except Exception as exc:  # noqa: BLE001
                    return self._err(400, "改钉失败：%s" % exc)

            with _lock:
                doc = _whitelist_doc()
                hit = None
                for item in doc["apps"]:
                    if not isinstance(item, dict):
                        continue
                    s = normalize_source(item)
                    if (s["repo"] or s["url"]) == key or (s["appname"] and s["appname"] == key):
                        hit = item
                        break
                if hit is None:
                    return self._err(404, "列表里没有这个来源")
                if tag:
                    hit["tag"] = tag
                else:
                    hit.pop("tag", None)
                if asset:
                    hit["asset"] = asset
                else:
                    hit.pop("asset", None)
                save_whitelist(doc)
            log("已改钉版本：%s -> tag=%s asset=%s" % (key, tag or "（自动最新）", asset or "（自动挑）"))
            return self._json({"ok": True, "apps": load_whitelist()})


        if path == "/api/apps/remove":
            body = self._read_body()
            key = str(body.get("source") or "").strip()
            appname = str(body.get("appname") or "").strip()
            with _lock:
                doc = _whitelist_doc()
                before = len(doc["apps"])
                kept = []
                removed = None
                for item in doc["apps"]:
                    if not isinstance(item, dict):
                        continue
                    s = normalize_source(item)
                    ident = s["repo"] or s["url"]
                    if (key and ident == key) or (appname and s["appname"] == appname):
                        removed = ident or s["appname"]
                        continue
                    kept.append(item)
                if removed is None:
                    return self._err(404, "列表里没有这一条")
                doc["apps"] = kept
                save_whitelist(doc)
            log("移除被管理应用：%s（原 %d 条 → %d 条）" % (removed, before, len(kept)))
            return self._json({"ok": True, "apps": load_whitelist()})

        if path == "/api/apps/reset":
            with _lock:
                save_whitelist({"apps": (load_json(APPS_DEFAULT) or {}).get("apps", [])})
            return self._json({"ok": True, "apps": load_whitelist()})

        if path == "/api/token":
            body = self._read_body()
            tok = str(body.get("token") or "")
            if tok and not re.match(r"^[A-Za-z0-9_\-]{20,255}$", tok):
                return self._err(400, "令牌格式看起来不对（应为 20–255 位字母数字，"
                                      "github_pat_… 或 ghp_… 开头）")
            write_token(tok)
            log("GitHub 访问令牌已%s" % ("更新" if tok else "清除"))
            return self._json({"ok": True, "has_token": bool(tok), "token_hint": token_hint()})

        return self._err(404, "未知接口")

    def log_message(self, fmt, *args):
        # 默认会把每条请求写进 stderr，太吵；只留我们自己的结构化日志
        pass


# ───────────────────────────── 后台检测线程 ─────────────────────────────

def checker_loop():
    time.sleep(8)  # 等服务先起来
    while True:
        try:
            run_check(trigger="auto")
        except Exception:  # noqa: BLE001
            log("自动检测出错：")
            log(traceback.format_exc())
        time.sleep(max(300, CHECK_INTERVAL_MIN * 60))


def judge_interrupted(t):
    """重启后遇到残留的「进行中」任务：判断它其实是不是已经完成了。

    返回 (status, stage, message, percent)。两条自愈路径（启动时、/api/state 兜底）共用，
    免得一条认成功、另一条把它改回失败 —— 那正是自更新时用户看到的矛盾现象。

    判据：任务记了目标版本（`version`），而该应用现在装的正好就是这个版本 → 其实成功了。
    ⚠️ 前提是**执行升级的那个进程**已经会记录目标版本；若执行者是更旧的版本（没这行代码），
    目标版本就是空的，只能按中断处理（实测第一次自更新就是这样）。
    """
    _app = str(t.get("appname") or "")
    _tgt = str(t.get("version") or "")
    _act = str(t.get("action") or "")
    done = (_act in ("install", "update") and bool(_app and _tgt)
            and norm_version(installed_version(_app) or "") == norm_version(_tgt))
    if done:
        _label = {"install": "安装", "update": "更新"}.get(_act, "任务")
        return ("ok", "done",
                "%s完成：%s（升级过程中服务重启过，已确认装好）" % (_label, _tgt), 100)
    return ("failed", "error", "上一次任务在服务重启时中断了，请重试", t.get("percent"))


def _recover_stale_tasks():
    """启动时收拾上一次进程留下的「进行中」任务。

    为什么必须有：tasks.json 是落盘的，服务重启（升自己、重启应用、断电）时可能停在
    status=running。而新起的 worker_loop **只认 queued**，永远不会再碰它 →
    那条任务就永久卡在「进行中」。后果是：
      · 入队去重会把它当成「同一动作已在跑」，新请求直接被吃掉、什么都不做；
      · 界面上该应用的按钮永久禁用（`busy = running`）；
      · 界面唯一的「清理已完成的任务」明确保留 running 任务，用户自救不了。
    实测确认过（注入一条 running 任务 → busy=True、再点下载返回的还是那条旧任务）。

    所以启动时把「running」一律判为中断：此刻所有 running 都必然是上一个进程留下的
    （本进程的 worker 还没开始跑）。
    """
    with _tasks_lock:
        d = _load_tasks()
        n = 0
        for t in d["tasks"]:
            if t.get("status") == "running":
                # 先分辨一种特别常见的情况：**升级自己**（或正在被升级的那个应用）时，
                # 应用中心会在升级中途把进程重启 —— 那条任务并不是失败。
                # 判据：任务记了目标版本，而该应用现在装的正好就是这个版本 → 其实成功了。
                # （只有 install/update 会改「已装版本」，download 不套用这条。）
                st, stage, msg, pct = judge_interrupted(t)
                t["status"] = st
                t["stage"] = stage
                t["message"] = msg
                if pct is not None:
                    t["percent"] = pct
                if st == "ok":
                    log("启动自愈：%s 的 %s 任务在重启前已完成（已装 %s）"
                        % (t.get("appname"), t.get("action"), t.get("version")))
                t["finished_at"] = now_iso()
                t["updated_at"] = now_iso()
                n += 1
        if n:
            _save_tasks(d)
    if n:
        log("启动自愈：把 %d 条残留的「进行中」任务标为中断（否则会永久卡住）" % n)
    return n


def check_self(force=True):
    """只重查本应用自身（结论写回 last-check.json 的 self 格，不动 apps）。"""
    with _lock:
        _check_state.update({"running": True, "done": 0, "total": 1, "current": APP_NAME,
                             "trigger": "self",
                             "started_at": datetime.now(CST).isoformat(timespec="seconds")})
    app = self_source()
    try:
        r = check_one(app, force=force)
        r["self"] = True
    except Exception as exc:                          # noqa: BLE001
        r = {"appname": APP_NAME, "display": "FPK更新", "note": "", "repo": app["repo"],
             "url": "", "tag": "", "source": app["repo"], "has_icon": False,
             "installed": installed_version(APP_NAME), "latest": None,
             "update_available": False, "published_at": None, "notes": "",
             "asset_name": None, "size": None, "downloaded": None,
             "local_unknown": None, "detected_appname": None, "sha256": "",
             "self": True, "error": "检查失败：%s" % exc}
    with _lock:
        st = cached_state()
        if not isinstance(st, dict) or "apps" not in st:
            st = {"at": now_iso(), "trigger": "self", "apps": []}
        st["self"] = r
        save_json(LAST_CHECK_FILE, st)
        _check_state.update({"running": False, "current": "",
                             "finished_at": datetime.now(CST).isoformat(timespec="seconds")})
    log("自身检测完成：已装=%s 最新=%s 可更新=%s%s"
        % (r["installed"], r["latest"], r["update_available"],
           (" 错误=" + r["error"]) if r["error"] else ""))
    return r


def run_self_check(force=True):
    """带闸门地跑一次自身检测（整轮检测正在跑时直接放弃：那一轮本来就会查自身）。"""
    if not _check_lock.acquire(blocking=False):
        log("已有一轮检测在进行，本次自身检测并入那一轮，不重复打 GitHub")
        return None
    try:
        return check_self(force=force)
    finally:
        _check_lock.release()


def main():
    global _worker_thread
    for d in (VAR_DIR, HOME_DIR, download_dir()):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            log("创建目录 %s 失败: %s" % (d, exc))
    log("=" * 60)
    log("FPK更新 启动：%s:%s  static=%s  var=%s" % (HOST, PORT, STATIC_DIR, VAR_DIR))
    if not os.path.exists(DAEMON_SOCKET):
        log("⚠️ 找不到应用中心 socket %s —— 检测可用，但升级会失败" % DAEMON_SOCKET)
    st = load_settings()
    log("下载目录=%s  分段并发=%d  加速镜像=%s"
        % (st["download_dir"], st["concurrency"], st["mirror"] or "直连"))
    # 自身更新的旧记录（曾经把自己当普通应用加进列表）→ 移出列表、独立成上方的区块
    try:
        migrate_self_entry()
    except Exception as exc:                          # noqa: BLE001
        log("自身条目迁移失败（不影响其它功能）：%s" % exc)
    # 先自愈残留任务，再起 worker —— 顺序不能反，否则新 worker 可能先跑到别的任务上
    _recover_stale_tasks()
    _worker_thread = threading.Thread(target=worker_loop, name="dl-worker", daemon=True)
    _worker_thread.start()
    threading.Thread(target=checker_loop, name="checker", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    log("已就绪，监听 %s:%s" % (HOST, PORT))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        log("已停止")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        log("致命错误：")
        log(traceback.format_exc())
        sys.exit(1)
