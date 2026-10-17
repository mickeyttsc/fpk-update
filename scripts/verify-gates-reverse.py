#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反向验证：逐条破坏修复点，确认门禁真的会报红（否则就是摆设）。

每条都：改 src 的副本 → 跑门禁 → 期望 exit != 0 → 恢复。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.chdir(ROOT)                        # 门禁要在仓库根下跑（构建与 CI 都是）
SRC = os.path.join(ROOT, "app", "bin", "server.py")
GATES = [
    (os.path.join(ROOT, "scripts", "verify-clean-safety.py"), "安装包清理安全"),
    (os.path.join(ROOT, "scripts", "verify-runtime-recovery.py"), "运行时健壮性"),
    (os.path.join(ROOT, "scripts", "verify-endpoint-latency.py"), "接口延迟/行为"),
]

BREAKS = [
    ("设置读改写去掉锁", '''    with _lock:
        cur = load_settings()
        cur.update(new)
        save_json(SETTINGS_FILE, cur)
    return cur''', '''    cur = load_settings()
    cur.update(new)
    save_json(SETTINGS_FILE, cur)
    return cur'''),
    ("台账读改写去掉锁", "    with _lock:\n        d = _load_registry()\n        d[p] = {",
     "    if True:\n        d = _load_registry()\n        d[p] = {"),
    ("main 里不调自愈", "    _recover_stale_tasks()\n    _worker_thread", "    _worker_thread"),
    ("GitHub 请求不重试", "GH_RETRIES = 2", "GH_RETRIES = 0"),
    ("指定的附件对不上时静默替换", "pattern=src[\"asset\"] or None, strict=True)",
     "pattern=src[\"asset\"] or None)"),
    ("清理不再处理下载残留", '''    parts, parts_freed = [], 0
    for it in orphan_parts():
        if not it["old_enough"]:
            continue
        ok, why = _safe_unlink_part(it["path"])
        if ok:
            parts.append(it["name"])
            parts_freed += it["size"]
        else:
            failed.append({"name": it["name"], "reason": why})''',
     '''    parts, parts_freed = [], 0'''),
    # ── 本应用自身：独立成一块（4 条）──
    ("列表不再过滤自身（自己混进被管理列表）",
     '        if name == APP_NAME or (src["repo"] or "").split("/")[-1] == APP_NAME:\n            continue\n',
     '        if False:\n            continue\n'),
    ("检测结果不再分格（自身混进 apps，会被「全部更新」带上）",
     '            "apps": [r for r in results if not r.get("self")],',
     '            "apps": results,'),
    ("动作链路不认自身",
     '    if appname == APP_NAME:\n        return self_source()',
     '    if False:\n        return self_source()'),
    ("自身来源不再可配（忽略 self_repo 设置）",
     '    repo = str(load_settings().get("self_repo") or "").strip() or SELF_DEFAULT_REPO',
     '    repo = SELF_DEFAULT_REPO'),
    ("save_json 临时名退回只用 pid", '''        tmp = "%s.tmp.%d.%d.%d" % (path, os.getpid(), threading.get_ident(),
                                   time.monotonic_ns())''',
     '''        tmp = path + ".tmp.%d" % os.getpid()'''),
    ("钉版本不再校验", "                    rel = find_release(target_repo, True, tag) if tag else \\\n                        find_release(target_repo, True, \"\")",
     "                    rel = find_release(target_repo, True, \"\")"),
    ("安装不要求摘要（无摘要来源跳过校验）", """        if not rel.get("sha256"):
            raise RuntimeError("这个来源没有 sha256 摘要，出于安全不会安装"
                               "（可改用仓库来源，GitHub 的 Release 附件一般带 digest）")
""" ,
     "        pass\n"),
    ("请求体超限不断开连接", "            self.close_connection = True\n            return {}\n        try:\n            return json.loads(self.rfile.read(n).decode",
     "            return {}\n        try:\n            return json.loads(self.rfile.read(n).decode"),
    ("按 tag 精确取改回列表查找", '            one = gh_get("https://api.github.com/repos/%s/releases/tags/%s"\n                         % (repo, quote(str(want_tag))))',
     '            one = gh_get("https://api.github.com/repos/%s/releases?per_page=30" % repo)\n            one = one[0] if one else None'),
    ("检查改回串行（去掉线程）", '    threads = [threading.Thread(target=one, args=(i, a), daemon=True)\n               for i, a in enumerate(apps)]\n    for t in threads:\n        t.start()',
     '    for _i, _a in enumerate(apps):        # 破坏：改回串行（不再并发）\n        one(_i, _a)'),
    ("Release 列表不缓存", "    if hit and not force and now - hit[0] < GH_CACHE_TTL:",
     "    if False:"),
    ("OPTIONS 预检不实现（退回 501）",
     "    def do_OPTIONS(self):", "    def do_OPTIONS_disabled(self):"),
    # ── 长连接请求体消费（2026-10-16 实测的 501 根因，3 条）──
    ("do_POST 不再中央消费请求体（残留字节污染下一请求）",
     "            self._read_body()\n            self._route_post()",
     "            self._route_post()"),
    ("请求体缓存不按请求重置（上一请求的 body 串给下一请求）",
     "        self._body_cache = None\n        super().handle_one_request()",
     "        if not hasattr(self, \"_body_cache\"):\n            self._body_cache = None\n        super().handle_one_request()"),
    ("非 dict 请求体不再归一（null 体让连接挂起）",
     "        self._body_cache = parsed if isinstance(parsed, dict) else {}",
     "        self._body_cache = parsed"),
    # ── Scheme B：未安装的应用（2 条）──
    ("未安装的仓库退回「只允许已安装」硬边界",
     '''            # 未安装的应用也允许加入（Scheme B）：列表先把它管起来 —— 可以先看版本、
            # 下载安装包；「更新/安装」链路后端有专门守卫（初次安装必须走应用中心向导，
            # 因为要选存储空间），不会误触。加进来后等用户在应用中心装好，
            # 检测会自然把它转成正常的「已装」状态。
            was_installed = bool(installed_version(appname))''',
     '''            if not installed_version(appname):
                return self._err(400, "破坏：退回只允许已安装应用")
            was_installed = True'''),
    ("未安装应用的 update 守卫被摘掉（update 混进安装链路）",
     '            if not installed_version(appname):\n                if action == "update":',
     '            if not installed_version(appname):\n                if False:'),
    ("首次安装放行非法存储空间编号",
     '                if not 1 <= volume <= 99:\n                    return self._err(400, "存储空间编号超出范围（1–99）")',
     '                if False:\n                    return self._err(400, "存储空间编号超出范围（1–99）")'),
    ("安装参数名不再校验（可注入任意环境变量）",
     '                    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", k):',
     "                    if False:"),
    ("安装参数值允许换行（env 文件可被注入）",
     '                    if len(v) > 4096 or "\\n" in v or "\\r" in v:',
     "                    if False:"),
    ("排序设置不再校验（任意字符串都能存）",
     "                if srt not in SORT_CHOICES:",
     "                if False:"),
    ("清理不再处理本应用自己的安装包",
     "    for it in self_owned_packages():",
     "    for it in []:"),
    ("wizard 的 defer 结果又被缓存（已下载仍卡提示下载）",
     '    spec["defer"] = True\n    return dict(spec)',
     '    _WIZ_CACHE[key] = spec\n    return dict(spec)'),
    ("有本地包也强制重新下载", "        if not known and _local_sha256_cached(cand) != rel[\"sha256\"]:",
     "        if True:"),
    ("共享判定失效（重启后不再认「已装好」）",
     '    done = (_act in ("install", "update") and bool(_app and _tgt)',
     '    done = False and (_act in ("install", "update") and bool(_app and _tgt)'),
    ("清理不再保留当前版本的包",
     "        if it.get(\"current\") and not include_current:", "        if False:"),
    ("「已是最新就不下载」判定失效",
     "and not download_needed(it):", "and False:"),
    ("本地包改回只看台账（不比对内容）",
     "            if not by_ledger and want_sha:", "            if False:"),
    ("空列表错误提示去掉重试指引", '''        raise RuntimeError(
            "仓库 %s 这次读不到任何 Release —— 多半是 GitHub 侧瞬时抖动。"
            "点一次「检查更新」重试即可。" % repo)''',
     '''        raise RuntimeError("仓库 %s 没有可用的 Release" % repo)'''),
]


# 这两条只有「字符串级发布守卫」能抓（函数级门禁覆盖不到），交给下面的 FILE_BREAKS
STRING_ONLY = {
    "安装不要求摘要（无摘要来源跳过校验）",
    "请求体超限不断开连接",
}

# 字符串级破坏用例：(标签, 相对路径, 原文, 改成)
FILE_BREAKS = [
    ("安装不要求摘要", "bin/server.py",
     '        if not rel.get("sha256"):\n            raise RuntimeError("这个来源没有 sha256 摘要，出于安全不会安装"\n                               "（可改用仓库来源，GitHub 的 Release 附件一般带 digest）")\n',
     '        pass\n'),
    ("请求体超限不断开连接", "bin/server.py",
     "self.close_connection = True", "pass   # 破坏：不再断开连接"),
    ("添加应用不校验 appname", "bin/server.py",
     '            if not APPNAME_RE.match(appname or ""):\n                return self._err(400, "安装包内的应用名不合法（%r），拒绝添加" % appname)\n',
     ""),
    ("续传不校验 Range", "bin/server.py",
     'getattr(r, "status", 200) != 206', "True"),
    ("弹窗改回浏览器原生 confirm", "www/app.js",
     "ok = await askDialog(CONFIRM[action](app));",
     "ok = confirm(CONFIRM[action](app));"),
    ("前端确认框去掉 downloaded 兜底", "www/app.js",
     "((a.downloaded && a.downloaded.version) || a.latest || '?')",
     "a.downloaded.version"),
    ("自身更新接口被摘掉", "bin/server.py",
     'if path == "/api/self/update":', 'if path == "/api/self/update-DISABLED":'),
    ("添加应用时不再拒绝自身", "bin/server.py",
     'if (src["repo"] or "").split("/")[-1] == APP_NAME:', 'if False:'),
    ("向导里塞回「密码」文案", "wizard/upgrade",
     '"helpText": "本应用没有密码，升级步骤也不需要你输入任何东西 —— 升级完直接打开就能用。"',
     '"helpText": "升级会保留你的访问密码，无需重新设置。"'),
]


# 前端破坏用例：(标签, 原文, 改成) —— 跑 render-selftest.js 应当报红
FE_BREAKS = [
    ("自更新后不再自动刷新界面", "  if (!selfUpdated && bootVersion", "  if (false)"),
    ("已是最新时按钮又写回「安装」",
     "btn(upToDate ? '已是最新' : actLabel", "btn(actLabel"),
    ("成功时又留结果条（卡片不回到普通形态）",
     "  if (ok) return null;", "  if (false) return null;"),
    ("卡片改回全量重建（不再复用节点）",
     "      if (n.__sig) (pool[n.__sig] = pool[n.__sig] || []).push(n);",
     "      if (false) (pool[n.__sig] = pool[n.__sig] || []).push(n);"),
    ("界面不再渲染自身那一块",
     "  renderSelf(s);", "  /* 破坏：不画自身区块 */"),
    ("列表清空后不清 DOM（留残影）",
     "    cards.textContent = '';",
     "    /* 破坏：不清空 */"),
]


def run_fe_breaks():
    """在 app/www/app.js 的副本上改一处，跑前端渲染自测，返回是否被拦住。"""
    import shutil
    node = shutil.which("node") or "/vol2/@appcenter/nodejs_v24/bin/node"
    if not os.path.isfile(node):
        return None
    results = []
    for label, a, b in FE_BREAKS:
        tmp = tempfile.mkdtemp(prefix="fpku-rev-fe-")
        try:
            shutil.copytree(os.path.join(ROOT, "app", "www"), os.path.join(tmp, "www"))
            target = os.path.join(tmp, "www", "app.js")
            text = io.open(target, encoding="utf-8").read()
            if a not in text:
                results.append((label, None))
                continue
            io.open(target, "w", encoding="utf-8").write(text.replace(a, b))
            r = subprocess.run([node, os.path.join(ROOT, "scripts", "render-selftest.js"),
                                target], capture_output=True, text=True, cwd=ROOT)
            results.append((label, r.returncode != 0))
        finally:
            import shutil as sh
            sh.rmtree(tmp, ignore_errors=True)
    return results


def run_guards_on_copy(rel, old_t, new_t):
    """在产物副本上改一处，跑发布守卫脚本，返回是否被拦住（None = 锚点失效）。"""
    import shutil
    tmp = tempfile.mkdtemp(prefix="fpku-rev-guard-")
    try:
        for sub in ("bin", "www"):
            src = os.path.join(ROOT, "app", sub)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(tmp, sub))
        wsrc = os.path.join(ROOT, "wizard")
        if os.path.isdir(wsrc):
            shutil.copytree(wsrc, os.path.join(tmp, "wizard"))
        target = os.path.join(tmp, rel)
        text = io.open(target, encoding="utf-8").read()
        if old_t not in text:
            return None
        # 全量替换：有的破坏（如「去掉断开连接」）需要把同名片段**每一处**都改掉，
        # 否则另一处还在、守卫仍通过 —— 那种绿是假的（实测踩过）
        io.open(target, "w", encoding="utf-8").write(text.replace(old_t, new_t))
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "verify-release-guards.py"), tmp],
            capture_output=True, text=True)
        return r.returncode != 0
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def run_gates(path):
    bad = []
    for gate, label in GATES:
        r = subprocess.run([sys.executable, gate, path], capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(label)
    return bad


def main():
    orig = io.open(SRC, encoding="utf-8").read()
    print("基线：门禁应当全绿")
    baseline = run_gates(SRC)
    if baseline:
        print("  ✘ 基线都不绿，先修好再来做反向验证：%s" % baseline)
        return 1
    print("  ✔ 基线全绿\n")

    failures = 0
    for label, old, new in BREAKS:
        if label in STRING_ONLY:
            continue                      # 见 FILE_BREAKS
        if orig.count(old) != 1:
            print("  ? 跳过「%s」：锚点命中 %d 次" % (label, orig.count(old)))
            failures += 1
            continue
        tmp = tempfile.mktemp(suffix=".py")
        io.open(tmp, "w", encoding="utf-8").write(orig.replace(old, new))
        try:
            caught = run_gates(tmp)
        finally:
            os.unlink(tmp)
        ok = bool(caught)
        print("  %s 破坏「%s」→ %s" % ("✔" if ok else "✘", label,
                                      ("被拦住（%s）" % "、".join(caught)) if ok else "**没被拦住**"))
        if not ok:
            failures += 1

    print()
    print("▸ 发布守卫（字符串级：改副本 → 跑守卫 → 必须报红）")
    for label, rel, a, b in FILE_BREAKS:
        got = run_guards_on_copy(rel, a, b)
        if got is None:
            print("  ? 跳过「%s」：锚点失效" % label)
            failures += 1
            continue
        print("  %s 破坏「%s」→ %s" % ("✔" if got else "✘", label,
                                      "被拦住" if got else "**没被拦住**"))
        if not got:
            failures += 1

    print()
    print("▸ 前端渲染（改副本 → 跑渲染自测 → 必须报红）")
    fe = run_fe_breaks()
    if fe is None:
        print("  ? 本机没有 node，跳过前端反向验证")
        failures += 1
    else:
        for label, got in fe:
            if got is None:
                print("  ? 跳过「%s」：锚点失效" % label)
                failures += 1
                continue
            print("  %s 破坏「%s」→ %s" % ("✔" if got else "✘", label,
                                          "被拦住" if got else "**没被拦住**"))
            if not got:
                failures += 1

    print()
    if failures:
        print("✘ 有 %d 条破坏没被门禁拦住 —— 门禁不完整" % failures)
        return 1
    total = len(BREAKS) - len(STRING_ONLY) + len(FILE_BREAKS) + len(FE_BREAKS)
    print("✅ 全部 %d 条破坏都被门禁拦住（函数级 %d + 守卫级 %d + 前端 %d，门禁不是空转）"
          % (total, len(BREAKS) - len(STRING_ONLY), len(FILE_BREAKS), len(FE_BREAKS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
