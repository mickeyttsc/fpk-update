#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接口延迟门禁：前端会调的每个接口都必须在预算内返回。

为什么要有它：实测发现「点一下卡一秒」的不是轮询（/api/state 只要 2ms），而是四个
**同步打 GitHub** 的接口（0.7~1.4 秒），以及一个「白跑一趟才知道重复」的添加。
这类问题光靠代码审查看不出来（行数不多、逻辑也对），必须**量**。

做法：在临时目录里起一个**真的 HTTP 服务**（同 server.py），把网络调用（gh_get）
换成瞬时桩 —— 这样量到的是「我们自己的代码」的耗时，不含 GitHub 的网络时间。
网络时间的那部分用「接口必须立刻返回、重活交给后台」来保证（见 /api/check）。
"""
import http.client
import importlib.util
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

BUDGET_MS = 200.0          # 单个接口的预算（本地处理，不含外网）
BAD = 0


def chk(cond, label, detail=""):
    global BAD
    if cond:
        print("  ✔ %s" % label)
    else:
        BAD += 1
        print("  ✘ %s%s" % (label, ("  -> " + str(detail)) if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "app/bin/server.py"
    tmp = tempfile.mkdtemp(prefix="fpku-perf-gate-")
    var = os.path.join(tmp, "var")
    www = os.path.join(tmp, "www")
    apps_root = os.path.join(tmp, "apps")
    dl = os.path.join(tmp, "dl")
    for d in (var, www, apps_root, dl, os.path.join(apps_root, "installed-one")):
        os.makedirs(d, exist_ok=True)
    # 一个「已安装」的应用，让 check/state 有真东西可处理
    with open(os.path.join(apps_root, "installed-one", "manifest"), "w", encoding="utf-8") as f:
        f.write("appname               = installed-one\nversion               = 1.2.3\n")
    with open(os.path.join(apps_root, "installed-one", "ICON.PNG"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
    with open(os.path.join(var, "apps.json"), "w", encoding="utf-8") as f:
        json.dump({"apps": [{"repo": "owner/repo", "appname": "installed-one",
                             "display": "Installed One"}]}, f)
    for name in ("index.html", "app.js", "style.css", "favicon.svg"):
        with open(os.path.join(www, name), "w", encoding="utf-8") as f:
            f.write("<!-- perf gate -->\n")
    with open(os.path.join(var, "fpk-update.log"), "w", encoding="utf-8") as f:
        f.write("\n".join("日志行 %d" % i for i in range(400)))

    os.environ.update({
        "FPKU_VAR": var, "FPKU_HOME": var, "FPKU_STATIC": www,
        "FPKU_DOWNLOAD_DIR": dl, "FPKU_APPS_ROOT": apps_root,
        "FPKU_APPS_USER": os.path.join(var, "apps.json"),
        "FPKU_APPS_DEFAULT": os.path.join(var, "apps-default.json"),
        "FPKU_HOST": "127.0.0.1", "FPKU_PORT": "0",
    })
    spec = importlib.util.spec_from_file_location("fpku_perf_gate", src)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log = lambda *a, **k: None

    # ★ 把网络调用换成瞬时桩：量的是我们自己的代码，不是 GitHub 的延迟
    REL = {"tag": "v1.2.4", "prerelease": False, "published_at": "2026-01-01T00:00:00Z",
           "notes": "x", "assets": [{"name": "a.fpk", "size": 1000,
                                     "sha256": "a" * 64, "browser_download_url": "http://x/a.fpk",
                                     "digest": "sha256:" + "a" * 64}]}
    m.gh_get = lambda url, retries=0: [REL]
    m.gh_releases = lambda repo, force=False: [REL]
    m.resolve_source = lambda entry, force=False: {
        "appname": "installed-one", "version": "1.2.4", "version_source": "manifest",
        "display_name": "Installed One", "platform": "all", "tag": "v1.2.4",
        "asset_name": "a.fpk", "asset_url": "http://x/a.fpk", "sha256": "a" * 64,
        "size": 1000, "published_at": None, "notes": "", "source_kind": "repo"}
    m.find_release = lambda repo, allow_pre=False, want_tag="", force=False: REL
    m.wizard_spec_for = lambda app, rel: {
        "fields": [{"type": "text", "field": "wizard_app_port", "label": "端口",
                    "initValue": "47939",
                    "rules": [{"required": True, "message": "请填端口"}]}],
        "source": "stub", "defer": False, "local_ready": False}
    m.check_one = lambda a, force=False: {"appname": a.get("appname"), "display": a.get("display"),
                             "installed": "1.2.3", "latest": "1.2.4",
                             "update_available": True, "error": None, "sha256": "a" * 64,
                             "downloaded": None, "local_unknown": None}

    port = free_port()
    httpd = m.ThreadingHTTPServer(("127.0.0.1", port), m.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    m._worker_thread = None            # 不起下载 worker：本门禁只看「请求处理有多快」

    HDR = {"X-Requested-With": "fpk-update", "Content-Type": "application/json"}

    def call(path, body=None, headers=HDR, reps=5):
        data = json.dumps(body).encode() if body is not None else None
        worst = 0.0
        code = None
        hd = {}
        for _ in range(reps):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            t0 = time.time()
            c.request("POST" if data is not None else "GET", path, body=data, headers=headers)
            r = c.getresponse()
            raw = r.read()
            ms = (time.time() - t0) * 1000.0
            code, hd = r.status, dict(r.getheaders())
            c.close()
            worst = max(worst, ms)
        return worst, code, hd, raw

    CASES = [
        ("GET  /api/health", "/api/health", None),
        ("GET  /api/state", "/api/state", None),
        ("GET  /api/volumes", "/api/volumes", None),
        ("GET  /api/wizard(已装)", "/api/wizard?appname=installed-one", None),
        ("GET  /api/tasks", "/api/tasks", None),
        ("GET  /api/packages", "/api/packages", None),
        ("GET  /api/log", "/api/log?lines=200", None),
        ("GET  /api/icon/<已装>", "/api/icon/installed-one", None),
        ("GET  / 页面", "/", None),
        ("GET  /app.js", "/app.js", None),
        ("POST /api/settings", "/api/settings", {"concurrency": 4}),
        ("POST /api/clean(dry)", "/api/clean", {"dry": True}),
        ("POST /api/all", "/api/all", {"action": "download"}),
        ("POST /api/apps/options", "/api/apps/options", {"source": "owner/repo"}),
        ("POST /api/apps/preview", "/api/apps/preview", {"source": "owner/repo"}),
        ("POST /api/apps/add(重复)", "/api/apps/add", {"source": "owner/repo"}),
        ("POST /api/apps/pin", "/api/apps/pin", {"source": "owner/repo", "tag": "v1.2.4"}),
        ("POST /api/apps/pin(没变)", "/api/apps/pin", {"source": "owner/repo", "tag": "v1.2.4"}),
        ("POST /api/download(入队)", "/api/download", {"appname": "installed-one"}),
    
        # /api/check 放最后、而且**只打一次**（第四个元素 = reps）。
        # 它会在后台真跑一轮检测、重写 last-check.json；默认的 5 次调用会因为
        # 「第一轮已经跑完」而连着起第二轮，最后一轮落在后面刚写的种子状态之后，
        # 把那两条 409 断言变成看运气（实测 14 次里红 2 次、CI 上也红过一次）。
        ("POST /api/check(必须立刻返回)", "/api/check", {"force": True}, 1),
    ]

    print("▸ 每个接口的耗时上限 %.0f ms（网络调用已换瞬时桩）" % BUDGET_MS)
    slow = []
    for _c in CASES:
        label, path, body = _c[0], _c[1], _c[2]
        worst, code, hd, raw = call(path, body, reps=(_c[3] if len(_c) > 3 else 5))
        ok = worst < BUDGET_MS
        if not ok:
            slow.append((label, worst))
        print("  %s %-30s %7.1f ms  HTTP %s" % ("✔" if ok else "✘", label, worst, code))
    chk(not slow, "所有接口都在 %.0f ms 内" % BUDGET_MS,
        ["%s=%.0fms" % (a, b) for a, b in slow])

    # ★ 等一下：/api/check 是异步的，它会在后台重写 last-check.json。
    # 判据用「状态文件真的被写过」，不要用 running：只要有一次判早了（检查线程还没
    # 复位、或复位了但文件还没落地），后面刚写的种子就会被它覆盖，两条 409 断言
    # 就变成看运气 —— 实测 14 次里红 2 次、CI 上也红过一次。文件是唯一可靠的凭据。
    _lc = os.path.join(var, "last-check.json")
    for _ in range(600):
        if not m.check_state()["running"] and os.path.exists(_lc):
            break
        time.sleep(0.05)
    # 再把「检测」冻住：下面测的是**接口行为**，状态必须是静止的。
    # 留一个能写 last-check.json 的后台线程，断言就还是看运气。
    m.run_check = lambda *a, **k: None

    # ★ 接口行为：「已是最新就不下载」必须由**接口**保证，不能只靠界面禁用按钮
    #
    # 先等后台检查跑完：前面的用例触发过 /api/check，它会异步重写 last-check.json。
    # 不等就写会被它覆盖 —— 这条断言会变成看运气的（实测构建里输掉了这次竞态、本地侥幸赢了）。
    for _ in range(200):
        if not m.check_state()["running"]:
            break
        time.sleep(0.05)
    os.makedirs(os.path.join(apps_root, "old-one"), exist_ok=True)
    with open(os.path.join(apps_root, "old-one", "manifest"), "w", encoding="utf-8") as f:
        f.write("appname               = old-one\nversion               = 1.0.0\n")
    with open(os.path.join(var, "apps.json"), "w", encoding="utf-8") as f:
        json.dump({"apps": [{"repo": "owner/repo", "appname": "installed-one"},
                            {"repo": "owner/repo2", "appname": "old-one"}]}, f)
    _up2date = {"appname": "installed-one", "display": "已最新", "installed": "1.2.3",
                "latest": "1.2.3", "update_available": False, "error": None,
                "sha256": "a" * 64, "downloaded": {"version": "1.2.3", "complete": True},
                "local_unknown": None}
    _older = {"appname": "old-one", "display": "有新版", "installed": "1.0.0",
              "latest": "1.2.4", "update_available": True, "error": None,
              "sha256": "a" * 64, "downloaded": None, "local_unknown": None}
    with open(os.path.join(var, "last-check.json"), "w", encoding="utf-8") as f:
        json.dump({"at": "2026-01-01T00:00:00+08:00", "trigger": "manual",
                   "apps": [_up2date, _older]}, f)
    _, code1, _, raw1 = call("/api/download", {"appname": "installed-one"}, reps=1)
    chk(code1 == 409, "已是最新 → 接口拒绝下载（409，不只靠界面禁用）", code1)
    chk("已经是最新" in raw1.decode("utf-8", "replace"),
        "拒绝时给出可读原因", raw1.decode("utf-8", "replace")[:80])
    _, code2, _, _ = call("/api/download", {"appname": "old-one"}, reps=1)
    chk(code2 == 200, "有更新 → 接口放行", code2)

    # ★ 未实现的方法不许回 501（Python 默认行为是 501 + HTML 错误页，
    #   前端只会报「请求失败（HTTP 501）」，实测用户就是这么撞上的）
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("OPTIONS", "/api/log", headers=HDR)
    r = c.getresponse(); r.read(); code_opt = r.status; c.close()
    chk(code_opt in (200, 204), "OPTIONS 预检有应答（不是 501）", code_opt)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("PUT", "/api/log", body=b"{}", headers=HDR)
    r = c.getresponse(); body_put = r.read(); code_put = r.status; c.close()
    chk(code_put == 405, "其它方法回 405（不是 501 HTML）", code_put)
    chk(b"ok" in body_put and "不支持".encode() in body_put,
        "405 也是 JSON 且说明原因", body_put[:60])

    # ★ 长连接上的请求体必须被消费干净（2026-10-16 实测的 501 根因）：
    #   POST 带 `{}` 体而某个路由没读 → 残留字节和下一个请求粘连，被解析成
    #   方法 `{}GET` → 501。手机上「清理已完成」后的 /api/state 就这么挂的。
    #   这一组用同一条连接连发：清理 → 设置改主题 → GET /api/state，三段全部必须干净。
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/api/tasks/clear", body=b"{}", headers=HDR)
    r = c.getresponse(); r.read(); code_clr = r.status
    c.request("POST", "/api/settings", body=b'{"theme": "dark"}', headers=HDR)
    r = c.getresponse(); body_set = r.read(); code_set = r.status
    c.request("GET", "/api/state", headers=HDR)
    r = c.getresponse(); r.read(); code_st = r.status
    c.close()
    chk(code_clr == 200, "POST /api/tasks/clear 正常", code_clr)
    chk(code_set == 200 and b'"theme": "dark"' in body_set,
        "同一连接上下一个 POST 的 body 被正确读取（缓存按请求隔离）",
        (code_set, body_set[:80]))
    chk(code_st == 200, "POST 后同连接 GET 不 501（请求体被消费，无残留字节）", code_st)

    # 403 早退路径也不能留残余字节
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", "/api/tasks/clear", body=b"{}",
              headers={"Content-Type": "application/json"})      # 故意缺自定义头 → 403
    r = c.getresponse(); r.read(); code_403 = r.status
    c.request("GET", "/api/state", headers=HDR)
    r = c.getresponse(); r.read(); code_after = r.status
    c.close()
    chk(code_403 == 403, "缺自定义头的 POST 回 403", code_403)
    chk(code_after == 200, "403 拒绝后同连接下一个请求不受污染", code_after)

    # 垃圾体（json null）不得把接口打成 500、也不得让连接挂起：
    # 归一成 {} 后，后续请求必须照常（改主题 → 看是否生效 → GET）。
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.request("POST", "/api/settings", body=b"null", headers=HDR)
        r = c.getresponse(); r.read(); code_null = r.status
        c.request("POST", "/api/settings", body=b'{"theme": "light"}', headers=HDR)
        r = c.getresponse(); body_ok = r.read(); code_ok = r.status
        hang = False
    except (socket.timeout, TimeoutError):
        code_null = code_ok = None
        hang = True
    finally:
        c.close()
    chk(not hang and code_null == 200, "请求体为 null 不炸（归一成 {}）", "挂起" if hang else code_null)
    chk(not hang and code_ok == 200 and b'"theme": "light"' in (body_ok if not hang else b""),
        "null 体之后的正常请求不受影响", code_ok)

    # ── Scheme B：未安装的应用（添加放行 / 更新拒绝 / 下载放行 / 批量跳过）──
    print("▸ 未安装的应用（Scheme B）")
    _rs_stub = m.resolve_source
    m.resolve_source = (lambda entry, force=False:
                        dict(_rs_stub(entry, force=force),
                             appname="not-yet-app", display_name="NotYet"))
    try:
        _, c_add2, _, _ = call("/api/apps/add", {"source": "owner/newrepo"}, reps=1)
    finally:
        m.resolve_source = _rs_stub
    chk(c_add2 == 200, "未安装的应用仓库可以直接添加（不再 400 硬边界）", c_add2)
    _wl = json.load(open(os.path.join(var, "apps.json"), encoding="utf-8"))
    chk(any(a.get("appname") == "not-yet-app" for a in _wl["apps"]),
        "添加结果写进了列表（等装好自动转正）")

    _, c_upd2, _, raw_upd2 = call("/api/update", {"appname": "not-yet-app"}, reps=1)
    chk(c_upd2 == 409, "未安装的应用点「update」被接口拒绝（语义只对已装应用）", c_upd2)
    chk("安装" in raw_upd2.decode(), "拒绝信息里给正确的指路（点「安装」）",
        raw_upd2.decode()[:80])
    _, c_ins2, _, raw_ins2 = call("/api/install", {"appname": "not-yet-app", "volume": 2}, reps=1)
    chk(c_ins2 == 200, "未安装的应用「安装」（完整安装）放行（入队）", c_ins2)
    with open(os.path.join(var, "tasks.json"), encoding="utf-8") as f:
        _tasks = json.load(f).get("tasks") or []
    _t2 = next((t for t in _tasks
                if t.get("appname") == "not-yet-app" and t.get("action") == "install"), {})
    chk(_t2.get("volume") == 2,
        "安装任务带着存储空间编号（volume）入队", (_t2.get("action"), _t2.get("volume")))
    _, c_vol_bad, _, raw_vol_bad = call("/api/install",
                                        {"appname": "not-yet-app", "volume": 999}, reps=1)
    chk(c_vol_bad == 400, "非法存储空间编号被拒（不是照单全收）", c_vol_bad)
    # 清掉上面入队的安装任务：下面参数用例要重新入队（同应用同动作会被去重合并）
    with open(os.path.join(var, "tasks.json"), "w", encoding="utf-8") as f:
        json.dump({"seq": 0, "tasks": []}, f)
    # 安装参数（wizard 字段）边界：参数会写进安装环境，名字必须像变量名、值不能带换行
    _, c_p1, _, _ = call("/api/install", {"appname": "not-yet-app", "volume": 2,
                                          "params": {"bad name!": "x"}}, reps=1)
    chk(c_p1 == 400, "安装参数名不合法被拒（挡环境变量注入）", c_p1)
    _, c_p2, _, _ = call("/api/install", {"appname": "not-yet-app", "volume": 2,
                                          "params": {"wizard_a": "x\nwizard_b=1"}}, reps=1)
    chk(c_p2 == 400, "安装参数值含换行被拒（挡 env 文件注入）", c_p2)
    _, c_p3, _, _ = call("/api/install", {"appname": "not-yet-app", "volume": 2,
                                          "params": {"wizard_app_port": "48080",
                                                     "wizard_on": True}}, reps=1)
    chk(c_p3 == 200, "合法安装参数随任务入队", c_p3)
    with open(os.path.join(var, "tasks.json"), encoding="utf-8") as f:
        _tasks3 = json.load(f).get("tasks") or []
    _t3 = next((x for x in _tasks3
                if x.get("appname") == "not-yet-app" and x.get("action") == "install"), {})
    chk(_t3.get("params") == {"wizard_app_port": "48080", "wizard_on": "true"},
        "参数原样带进任务（布尔归一成 true/false）", _t3.get("params"))
    # ── 一键清理：本应用自己的安装包也要删（台账之外的 fpk-update-*.fpk）──
    import tarfile as _tf2
    _self_pkg = os.path.join(dl, "fpk-update-9.9.9.fpk")
    with _tf2.open(_self_pkg, "w:gz") as tf:
        _md = b"appname               = fpk-update\nversion               = 9.9.9\n"
        _ti = _tf2.TarInfo("manifest")
        _ti.size = len(_md)
        tf.addfile(_ti, io.BytesIO(_md))
    m._SELF_PKG_CACHE.update({"at": 0.0, "data": [], "paths": set()})   # 让缓存立刻失效
    _, c_dry, _, raw_dry = call("/api/clean", {"dry": True}, reps=1)
    _dr = json.loads(raw_dry)
    chk(c_dry == 200 and _dr.get("self_count", 0) >= 1
        and "fpk-update-9.9.9.fpk" in (_dr.get("self") or []),
        "清理清单里能看到本应用自己的安装包", (_dr.get("self_count"), _dr.get("self")))
    _, c_cl, _, raw_cl = call("/api/clean", {}, reps=1)
    _cl = json.loads(raw_cl)
    chk(c_cl == 200 and "fpk-update-9.9.9.fpk" in (_cl.get("self_deleted") or []),
        "一键清理真的删掉了本应用自己的安装包", _cl.get("self_deleted"))
    chk(not os.path.exists(_self_pkg), "那个文件确实没了")

    # 排序设置：只能 added / alpha（写进设置、界面按它重排）
    _, c_sort1, _, raw_sort1 = call("/api/settings", {"sort": "alpha"}, reps=1)
    _s1 = json.loads(raw_sort1).get("settings") or {}
    chk(c_sort1 == 200 and _s1.get("sort") == "alpha", "排序设置 alpha 生效", _s1.get("sort"))
    _, c_sort2, _, _ = call("/api/settings", {"sort": "bogus"}, reps=1)
    chk(c_sort2 == 400, "非法排序值被拒（只能 added / alpha）", c_sort2)
    call("/api/settings", {"sort": "added"}, reps=1)      # 还原默认，别影响后面的用例

    # /api/wizard：参数表接口（stub 掉网络层，验证可渲染结构）
    _, c_wz, _, raw_wz = call("/api/wizard?appname=not-yet-app", reps=1)
    _wzs = json.loads(raw_wz)
    chk(c_wz == 200 and isinstance(_wzs.get("fields"), list) and _wzs["fields"]
        and _wzs["fields"][0].get("field") == "wizard_app_port",
        "/api/wizard 返回可渲染的参数表", (c_wz, str(_wzs)[:80]))
    # 未列出白名单的应用：wizard 接口拒绝
    _, c_wz2, _, _ = call("/api/wizard?appname=definitely-not-listed", reps=1)
    chk(c_wz2 == 400, "wizard 接口只服务白名单里的应用", c_wz2)

    # ── read_local_fpk_wizard：真读一只临时 fpk（函数级）──
    import tarfile as _tf
    _wiz_fpk = os.path.join(tmp, "wiz-probe.fpk")
    _doc = json.dumps([
        {"stepTitle": "配置", "items": [
            {"type": "text", "field": "wizard_app_port", "label": "端口",
             "initValue": "47939",
             "rules": [{"required": True, "message": "请填端口"},
                       {"pattern": "^[0-9]+$", "message": "必须是数字"}]},
            {"type": "password", "field": "wizard_token", "label": "访问密码", "initValue": ""},
            {"type": "tips", "helpText": "装完后看帮助"}]}])
    with _tf.open(_wiz_fpk, "w:gz") as tf:
        data = _doc.encode("utf-8")
        ti = _tf.TarInfo("wizard/install"); ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
        mdata = b"appname = x\nversion = 1.0.0\n"
        tm = _tf.TarInfo("manifest"); tm.size = len(mdata)
        tf.addfile(tm, io.BytesIO(mdata))
    _wf = m.read_local_fpk_wizard(_wiz_fpk)
    chk(isinstance(_wf, list) and len(_wf) == 3,
        "read_local_fpk_wizard 读到 3 条（含 tips）", _wf)
    chk(_wf[0]["field"] == "wizard_app_port" and _wf[0]["initValue"] == "47939"
        and _wf[0]["rules"][0].get("required") is True,
        "字段名/初值/规则都保留", _wf[0])
    chk(_wf[1]["type"] == "password" and _wf[2]["type"] == "tips",
        "类型原样保留（password / tips）", (_wf[1]["type"], _wf[2]["type"]))
    _wiz_none = os.path.join(tmp, "wiz-none.fpk")
    with _tf.open(_wiz_none, "w:gz") as tf:
        mdata = b"appname = y\nversion = 1.0.0\n"
        tm = _tf.TarInfo("manifest"); tm.size = len(mdata)
        tf.addfile(tm, io.BytesIO(mdata))
    chk(m.read_local_fpk_wizard(_wiz_none) == [],
        "没有 wizard 成员的包返回 []（不是 None）")
    chk(m.read_local_fpk_wizard(os.path.join(tmp, "不存在的包.fpk")) is None,
        "读不了的包返回 None（调用方降级）")
    _, c_vols, _, raw_vols = call("/api/volumes", reps=1)
    _vols = json.loads(raw_vols)
    chk(c_vols == 200 and isinstance(_vols.get("volumes"), list),
        "/api/volumes 返回存储空间列表", c_vols)
    _, c_dl2, _, raw_dl2 = call("/api/download", {"appname": "not-yet-app"}, reps=1)
    chk(c_dl2 == 200, "未安装的应用「下载」仍可单独用（API 层保留）", c_dl2)

    # 批量动作跳过未安装的（包是安装用的，单独下载更符合预期）
    with open(os.path.join(var, "last-check.json"), "w", encoding="utf-8") as f:
        json.dump({"at": "2026-01-01T00:00:00+08:00", "trigger": "seed",
                   "apps": [
                       {"appname": "installed-one", "display": "已最新", "installed": "1.2.3",
                        "latest": "1.2.3", "update_available": False, "error": None,
                        "sha256": "a" * 64, "downloaded": None, "local_unknown": None},
                       {"appname": "not-yet-app", "display": "NotYet", "installed": None,
                        "latest": "1.0.0", "update_available": False, "error": None,
                        "uninstalled": True, "sha256": "a" * 64,
                        "downloaded": None, "local_unknown": None}],
                   "self": None}, f)
    _, c_all2, _, raw_all2 = call("/api/all", {"action": "download"}, reps=1)
    _body_all2 = json.loads(raw_all2)
    chk(c_all2 == 200 and any("未安装" in s for s in (_body_all2.get("skipped") or [])),
        "批量下载跳过未安装的应用并给出原因", (_body_all2.get("skipped"), _body_all2.get("queued")))
    chk("not-yet-app" not in (_body_all2.get("queued") or []),
        "未安装的应用绝不出现在批量排队里", _body_all2.get("queued"))
    # 清掉这条排队任务，别干扰后面的用例
    with open(os.path.join(var, "tasks.json"), "w", encoding="utf-8") as f:
        json.dump({"seq": 0, "tasks": []}, f)

    # ★ /api/state 的兜底自愈也要认「其实装好了」（进程没起 worker 时走这条）
    with open(os.path.join(var, "tasks.json"), "w", encoding="utf-8") as f:
        json.dump({"seq": 1, "tasks": [
            {"id": "tG", "appname": "installed-one", "display": "X", "action": "update",
             "status": "running", "message": "正在升级…", "version": "1.2.3"}]}, f)
    _, _, _, raw_st = call("/api/state", reps=1)
    _tsk = (json.loads(raw_st).get("tasks") or [{}])[0]
    chk(_tsk.get("status") == "ok", "/api/state 兜底把「版本已对上」的任务认成完成",
        (str(_tsk.get("status")), str(_tsk.get("message"))[:40]))
    os.remove(os.path.join(var, "tasks.json"))

    # /api/check 必须是「立刻返回、后台跑」，不能等整轮检查
    t0 = time.time()
    call("/api/check", {"force": True}, reps=1)
    el = (time.time() - t0) * 1000.0
    chk(el < BUDGET_MS, "「检查更新」立刻返回（重活在后台）", "%.0f ms" % el)

    # 图标必须可缓存，否则每次重绘都会重拉
    _, _, hd, _ = call("/api/icon/installed-one", reps=1)
    cc = hd.get("Cache-Control", "")
    chk("max-age" in cc, "图标带可缓存头（不再 no-cache）", cc)
    chk(hd.get("ETag"), "图标带 ETag（可 304 复用）", hd.get("ETag"))

    httpd.shutdown()
    httpd.server_close()
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("=" * 70)
    if BAD:
        print("✘ 接口延迟门禁未通过：%d 处" % BAD)
        return 1
    print("✅ 接口延迟门禁通过（%d 个接口全部 < %.0f ms；检查类接口立刻返回）"
          % (len(CASES), BUDGET_MS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
