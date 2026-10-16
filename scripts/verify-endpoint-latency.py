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
    m.gh_releases = lambda repo: [REL]
    m.resolve_source = lambda entry: {
        "appname": "installed-one", "version": "1.2.4", "version_source": "manifest",
        "display_name": "Installed One", "platform": "all", "tag": "v1.2.4",
        "asset_name": "a.fpk", "asset_url": "http://x/a.fpk", "sha256": "a" * 64,
        "size": 1000, "published_at": None, "notes": "", "source_kind": "repo"}
    m.find_release = lambda repo, allow_pre=False, want_tag="": REL
    m.check_one = lambda a: {"appname": a.get("appname"), "display": a.get("display"),
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
        ("POST /api/check(必须立刻返回)", "/api/check", {"force": True}),
    ]

    print("▸ 每个接口的耗时上限 %.0f ms（网络调用已换瞬时桩）" % BUDGET_MS)
    slow = []
    for label, path, body in CASES:
        worst, code, hd, raw = call(path, body)
        ok = worst < BUDGET_MS
        if not ok:
            slow.append((label, worst))
        print("  %s %-30s %7.1f ms  HTTP %s" % ("✔" if ok else "✘", label, worst, code))
    chk(not slow, "所有接口都在 %.0f ms 内" % BUDGET_MS,
        ["%s=%.0fms" % (a, b) for a, b in slow])

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
