#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布守卫（字符串级）—— 检查「QA 修掉的每一处」都还在产物里。

为什么单独抽一个脚本：这些断言原来散在 verify-fpk-gates.sh 里当 grep，
**没法被反向验证**（要证明「某条断言真的会报红」，得改源码、重新打包、再跑门禁，
一次一分钟）。抽出来之后，反向验证脚本可以直接拿改动过的副本跑它，几毫秒一条。

用法：
    python3 scripts/verify-release-guards.py <解包后的产物根目录>
产物布局是 bin/ www/ wizard/；仓库布局是 app/bin … + wizard/，两种都认。
"""
import os
import sys

BAD = 0


def chk(cond, label, detail=""):
    global BAD
    if cond:
        print("  ✔ %s" % label)
    else:
        BAD += 1
        print("  ✘ %s%s" % (label, ("  -> " + str(detail)) if detail else ""))


def read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def loc(root):
    """产物布局 bin/ www/；仓库布局 app/bin app/www。返回 (载荷根, 外层根)。"""
    for cand in ("", "app"):
        p = os.path.join(root, cand)
        if os.path.isdir(os.path.join(p, "bin")):
            return p, root
    return root, root


# 「密码」类文案：只拦**肯定式**的。不能简单 grep「密码」——
# 「本应用没有密码」这句是正确文案，会被误判成违规（我自己踩过一次）。
FORBIDDEN_PHRASES = (
    "默认密码", "访问密码", "已设过密码", "密码文件", "输入密码",
    "access-password", "/api/login", "valid_session", "= password", "password)",
)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    app, outer = loc(root)
    srv = read(os.path.join(app, "bin", "server.py"))
    js = read(os.path.join(app, "www", "app.js"))
    chk(bool(srv), "读到了 bin/server.py", os.path.join(app, "bin", "server.py"))
    chk(bool(js), "读到了 www/app.js", os.path.join(app, "www", "app.js"))

    REQUIRED = [
        (srv, "_recover_stale_tasks()", "启动时自愈残留任务（否则重启后永久卡住）"),
        (srv, "_check_lock", "检测的并发闸门（多标签页不再并行打 GitHub）"),
        (srv, "GH_RETRIES", "GitHub 请求的瞬时故障重试"),
        (srv, "瞬时抖动", "读不到 Release 时给出可重试的说明"),
        (srv, "def orphan_parts", "下载残留清点"),
        (srv, "def _safe_unlink_part", "下载残留删除护栏"),
        (srv, "parts_count", "把下载残留报给界面"),
        # 判据必须是「修好后才会出现的那一处」，不能只认一个到处都出现的名字
        # （否则破坏掉其中一处、另一处还在 → 守卫照样通过，是假绿：实测踩过）
        (srv, 'item["local_unknown"]', "同名外来文件不认领（本地包身份以台账为准）"),
        (srv, 'getattr(r, "status", 200) != 206', "续传前确认服务端真的支持 Range"),
        (srv, "改钉失败", "钉版本前校验 tag / 附件真实存在"),
        (srv, "strict=True", "指定附件对不上时报错，而不是悄悄换包"),
        (srv, 'percent": None', "排队中用「进度未知」表达"),
        (srv, "出于安全不会安装", "「安装」强制要求 sha256 摘要"),
        (srv, "MAX_BODY_BYTES", "请求体上限常量"),
        (srv, "self.close_connection = True", "请求体读不完时断开连接"),
        (srv, "安装包内的应用名不合法", "添加应用时校验包内 appname"),
        (srv, "releases/tags/", "指定 tag 时按 tag 精确取（不受列表分页影响）"),
        (js, "parts_count", "界面呈现下载残留"),
        (js, "a.downloaded && a.downloaded.version", "确认框里 downloaded 有兜底"),
        (js, "localUnknownRow", "界面如实说明同名外来文件"),
        (srv, "_local_sha256_cached", "本地包按内容（sha256）认领，而不是只看谁下载的"),
        (srv, "def download_needed", "已是最新就不下载（后端判定）"),
        (srv, "def local_package_for", "有校验过的本地包就直接装（不重复下载）"),
        (js, "actLabel", "卡片只留一个动作按钮（更新 / 安装）"),
        (srv, "clean_keep_current", "清理默认保留当前版本的包（回滚/修复用）"),
        (srv, "def do_OPTIONS", "显式应答 OPTIONS 预检（不再回 501 HTML）"),
        (srv, "升级过程中服务重启过", "自更新/升级中途重启的任务记为完成（不是失败）"),
        (srv, "def _method_not_allowed", "其它方法回 405 JSON（不再回 501）"),
        (js, "function notifyFinishedTasks", "任务完成单独弹通知"),
        (srv, "def judge_interrupted", "两条自愈路径共用同一判定（不再误报中断失败）"),
        (js, "selfUpdated", "自己升级自己后自动刷新界面"),
        # 判据尽量不含标点：全角/半角括号最容易重打错（实测踩过两次）
        (js, "+ path +", "错误提示带上出错的接口路径（便于定位）"),
        (srv, "include_current", "清理接口支持显式「连当前版本一起清」"),
        (js, "keep-current-input", "设置里有「清理时保留当前版本」开关"),
        (js, "function askDialog", "应用内确认弹窗（替掉原生 confirm）"),
        (js, "justDone", "任务刚结束时保持快轮询（避免卡片与结果条自相矛盾）"),
        (open(os.path.join(app, "..", "www", "index.html")).read() if False else js, "cf-ok", "弹窗按钮接线"),
        (js, "lu.reason", "提示只在内容真的对不上时出现，并说清原因"),
        (srv, '"app_version"', "后端把本应用版本回给界面"),
        (js, "app-ver-chip", "顶栏常驻显示本应用版本"),
        (js, "app-ver-kv", "设置「运行信息」里也显示版本"),
        (srv, '"checking": check_state()', "后端把检查进度暴露给界面"),
        (srv, "threading.Thread(target=one", "各应用并行检查（不再是串行干等）"),
        (srv, "检查在后台跑", "检查不再阻塞请求（立刻返回）"),
        (srv, "def gh_releases", "Release 列表带短缓存（获取版本→预览→添加不再每步都打 GitHub）"),
        (srv, 'public, max-age=604800', "图标可缓存（不再每次重绘都重拉）"),
        (srv, "先在本地查重", "重复添加先本地查重（不再白跑一趟 GitHub）"),
        (js, "__sig", "卡片差分渲染（状态没变不重建，避免每次重建都重拉图标）"),
        (js, "function busyOn", "慢按钮有即时反馈（转圈 + 顶栏状态文字）"),
        (js, "正在读取仓库", "「获取版本」有明确状态文字"),
        (js, "ck.running", "前端按检查状态决定轮询快慢与按钮转圈"),
        (js, "'正在检查 '", "顶栏显示「正在检查 x/y」"),
        # ── 本应用自身：独立成一块（不进被管理列表、不进「全部更新」）──
        (srv, "SELF_DEFAULT_REPO", "自身更新有独立来源常量（默认=打包本应用的仓库）"),
        (srv, "def self_source", "自身来源单独解析（不来自被管理列表）"),
        (srv, "def migrate_self_entry", "旧的「把自己当普通应用」记录会被移出列表"),
        (srv, 'if name == APP_NAME or (src["repo"] or "").split("/")[-1] == APP_NAME:',
         "读列表时兜底过滤掉自身（手工写回也不会混进来）"),
        (srv, '"/api/self/update"', "自身更新走独立接口"),
        (srv, '"/api/self/check"', "自身检测走独立接口（绕过 15 秒缓存）"),
        (srv, '"apps": [r for r in results if not r.get("self")]',
         "检测结果分格：apps 里没有自身"),
        (srv, "if appname == APP_NAME:\n        return self_source()",
         "动作链路认自身（跨版本过渡期页面发的就是 /api/update）"),
        (srv, "def check_self", "只重查自身的实现"),
        (srv, 'if (src["repo"] or "").split("/")[-1] == APP_NAME:',
         "添加应用时拒绝把自身加进列表"),
        (srv, "        migrate_self_entry()", "启动时真的会做那次迁移"),
        (js, "function renderSelf", "界面把自身画成列表上方独立的一块"),
        (js, "api('/api/self/update'", "自身更新按钮打的是自身接口（不是列表接口）"),
        (js, "  renderSelf(s);", "render() 里真的画了那一块"),
        # ── v1.3 完整安装 / 安装参数 ──
        (srv, "def _do_first_install", "首次完整安装的实现（未安装的应用在这里装）"),
        (srv, "install-fpk", "走应用中心 CLI 的完整安装链路"),
        (srv, "def _cli_error_line", "CLI 失败原因从输出里抓（退出码是 0，不能靠它）"),
        (srv, "def wizard_spec_for", "安装参数表的四层来源（本地/头窗/仓库/预览下载）"),
        (srv, "def read_local_fpk_wizard", "从本地包里读向导定义"),
        (srv, "def default_volume", "系统默认安装卷（读 appcenter-cli default-volume）"),
        (js, "function doFirstInstall", "前端完整安装流程（参数表 + 存储空间 + 入队）"),
        (js, "cf-field-err", "安装对话框的字段级校验错误提示"),
    ]
    print("▸ 修复点必须还在（%d 项）" % len(REQUIRED))
    for text, needle, label in REQUIRED:
        chk(needle in text, label, "缺少片段：%s" % needle)

    # 必须**不存在**的东西（改回去就是回归）
    ABSENT = [
        (js, "confirm(", "不要再调用浏览器原生 confirm（要用应用内 askDialog）"),
        (js, "btn('下载并更新'", "不要再出现「下载并更新」按钮（合成一个「更新」）"),
        (js, "btn('下载'", "不要再出现独立的「下载」按钮（点「更新」时自动按需下载）"),
    ]
    for text, needle, label in ABSENT:
        chk(needle not in text, label, "出现了：%s" % needle)

    # 确认弹窗的骨架必须在页面里（只有 JS 没有 DOM 会是个点不开的空壳）
    html = read(os.path.join(app, "..", "www", "index.html")) or \
        read(os.path.join(outer, "www", "index.html"))
    if html:
        chk('id="confirm-modal"' in html and 'id="cf-ok"' in html,
            "页面里有确认弹窗骨架（#confirm-modal / #cf-ok）")
        chk('id="selfbar"' in html and 'id="sb-update"' in html and 'id="sb-state"' in html,
            "页面里有自身区块骨架（#selfbar / #sb-update / #sb-state）")
        chk('id="self-repo-input"' in html,
            "设置里能改自身更新来源（#self-repo-input）")
        # 交付的默认列表里**不能**有本应用自己（等于把自己当普通应用管理）
        apps_json = read(os.path.join(outer, "config", "apps.json")) or \
            read(os.path.join(app, "..", "config", "apps.json"))
        chk("fpk-update" not in apps_json,
            "内置默认列表里没有本应用自己（自身更新走独立链路）")
        chk("ekko-studio-fpk" not in apps_json and "cliproxyapi-fpk" not in apps_json and "wild-work-fpk" not in apps_json,
            "内置默认列表里没有预置作者个人仓库（保持纯净初始状态）")
        import json as _j
        _aj = _j.loads(apps_json) if apps_json else {}
        chk(_aj.get("apps") == [], "内置默认 apps 数组为空列表")

    # 密码类残留：wizard/ 与 cmd/ 都不许出现肯定式「密码」表述
    hits = []
    for sub in ("wizard", "cmd"):
        d = os.path.join(outer, sub)
        if not os.path.isdir(d):
            continue
        for nm in sorted(os.listdir(d)):
            p = os.path.join(d, nm)
            if not os.path.isfile(p):
                continue
            t = read(p)
            for kw in FORBIDDEN_PHRASES:
                if kw in t:
                    hits.append("%s/%s:%s" % (sub, nm, kw))
    chk(not hits, "向导与生命周期脚本里没有肯定式的「密码」文案", hits)

    # 打包脚本：外层顺序必须是显式确定的（不许按字典序漂）
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    san = read(os.path.join(root, "scripts", "sanitize-fpk.py")) or \
        read(os.path.join(repo, "scripts", "sanitize-fpk.py"))
    if san:
        chk("MEDIA" in san and "_rank" in san,
            "sanitize 的外层条目顺序是显式的（不再按字典序）")

    print()
    if BAD:
        print("✘ 发布守卫未通过：%d 处" % BAD)
        return 1
    print("✅ 发布守卫通过（%d 项修复点都在，文案自洽）" % len(REQUIRED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
