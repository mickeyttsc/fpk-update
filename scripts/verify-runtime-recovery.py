#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时健壮性验证 —— 真跑函数，覆盖本轮 QA 修掉的几类问题。

每一项都对应一个实测踩到的故障，改完必须能证明「确实修好了」并且不会退回去：
  1. 服务重启后残留的「进行中」任务会永久卡死（入队去重吃掉新请求、按钮永久禁用）
  2. GitHub 瞬时故障被报成「仓库没有可用的 Release」，且没有重试
  3. 用户钉的 tag / 指定的附件不存在时静默成功（之后每次检测都失败）
  4. 下载中断留下的 `<包名>.fpk.partN` 孤儿永不清、还清不掉
  5. 设置 / 台账的读改写没有锁 → 并发写会丢更新
  6. save_json 的临时文件名不唯一 → 同进程并发写同一文件会互相覆盖

用法：python3 scripts/verify-runtime-recovery.py [app/bin/server.py]
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time

BAD = 0


def chk(cond, label, detail=""):
    global BAD
    if cond:
        print("  ✔ %s" % label)
    else:
        BAD += 1
        print("  ✘ %s%s" % (label, ("  -> " + str(detail)) if detail else ""))


def load(src, tmp):
    os.environ.update({
        "FPKU_VAR": tmp, "FPKU_HOME": tmp, "FPKU_DOWNLOAD_DIR": os.path.join(tmp, "dl"),
        "FPKU_APPS_ROOT": os.path.join(tmp, "apps"), "FPKU_STATIC": os.path.join(tmp, "www"),
        "FPKU_APPS_USER": os.path.join(tmp, "apps.json"),
        "FPKU_APPS_DEFAULT": os.path.join(tmp, "apps-default.json"),
    })
    os.makedirs(os.path.join(tmp, "dl"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "apps"), exist_ok=True)
    spec = importlib.util.spec_from_file_location("fpku_runtime_gate", src)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log = lambda *a, **k: None
    m._worker_thread = None
    return m


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "app/bin/server.py"
    tmp = tempfile.mkdtemp(prefix="fpku-runtime-gate-")
    s = load(src, tmp)
    dl = os.path.join(tmp, "dl")
    REAL_GH_RELEASES = s.gh_releases   # 后面的用例会把它换成桩，测缓存时要换回来
    REAL_LOAD_WL = s.load_whitelist     # 同上：有些用例把列表也换成了桩，用完要换回来

    # ── 1. 残留任务自愈 ──
    print("▸ 服务重启后的残留任务自愈")
    s.update_task("seed", status="running")          # 造一条不存在的，只为确认不炸
    d = {"seq": 3, "tasks": [
        {"id": "t1", "appname": "a", "action": "download", "status": "running",
         "message": "分段下载中…", "percent": 42},
        {"id": "t2", "appname": "b", "action": "download", "status": "queued",
         "message": "排队中…", "percent": None},
        {"id": "t3", "appname": "c", "action": "download", "status": "ok",
         "message": "已下载", "percent": 100},
    ]}
    s._save_tasks(d)
    n = s._recover_stale_tasks()
    tasks = {t["id"]: t for t in s.list_tasks()}
    chk(n == 1, "自愈了 1 条 running 任务", n)
    chk(tasks["t1"]["status"] == "failed" and "重启" in tasks["t1"]["message"],
        "残留 running → failed 且给了可读原因", tasks["t1"]["message"])
    chk(tasks["t2"]["status"] == "queued", "排队中的任务不受影响（重启后应当继续跑）")
    chk(tasks["t3"]["status"] == "ok", "已完成的任务不受影响")
    chk(s._recover_stale_tasks() == 0, "再跑一次是幂等的")
    # 卡死场景复现：修好之后，同一动作的新请求应当能重新入队
    d = {"seq": 9, "tasks": [{"id": "tStale", "appname": "wildwork", "action": "download",
                              "status": "running", "message": "分段下载中…"}]}
    s._save_tasks(d)
    with open(os.path.join(tmp, "apps.json"), "w", encoding="utf-8") as f:
        json.dump({"apps": [{"repo": "mickeyttsc/wild-work-fpk", "appname": "wildwork",
                             "display": "Wild Work"}]}, f)
    s._recover_stale_tasks()
    tid = s.enqueue("wildwork", "download")
    chk(tid != "tStale", "自愈后新请求不会再被旧任务吃掉", tid)

    # ── 2. GitHub 瞬时故障要重试 ──
    print("▸ GitHub 瞬时故障重试")
    import http.server
    state = {"n": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                            # noqa: N802
            state["n"] += 1
            if state["n"] < 3:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"boom")
            else:
                body = json.dumps([{"tag_name": "v1", "draft": False, "prerelease": False,
                                    "assets": [{"name": "a.fpk", "size": 1}],
                                    "published_at": "x", "body": ""}]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s.GH_RETRY_WAIT = 0.01
    try:
        ok = s.gh_get("http://127.0.0.1:%d/x" % port)
        chk(isinstance(ok, list) and state["n"] == 3,
            "5xx 会被重试直到成功（共 %d 次请求）" % state["n"])
    except Exception as exc:                          # noqa: BLE001
        chk(False, "5xx 重试", exc)
    # 404 不该重试（确定性错误，重试没意义）
    state["n"] = 0

    class H404(H):
        def do_GET(self):                             # noqa: N802
            state["n"] += 1
            self.send_response(404)
            self.end_headers()

    srv2 = http.server.HTTPServer(("127.0.0.1", 0), H404)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    try:
        s.gh_get("http://127.0.0.1:%d/x" % srv2.server_address[1])
        chk(False, "404 应当直接抛出")
    except Exception:                                 # noqa: BLE001
        chk(state["n"] == 1, "404 不重试（只请求 1 次）", state["n"])
    # find_release：空列表要给出「可重试」的说法，而不是含糊的「没有可用 Release」
    # 注意：列表路径现在走 gh_releases（带 15 秒缓存），要替的是它 ——
    # 替 gh_get 会被缓存挡在前面（实测踩过：第一条用例把空列表缓存住，后面的用例全被它污染）
    s._gh_cache.clear()
    s.gh_releases = lambda repo, force=False: []
    try:
        s.find_release("owner/name", False, "")
        chk(False, "空列表应当报错")
    except Exception as exc:                          # noqa: BLE001
        msg = str(exc)
        chk("重试" in msg and "瞬时" in msg, "空列表的错误提示带重试指引", msg[:70])
    # 有 Release 但都没 .fpk 附件：要说清三种「找不到」的区别
    s.gh_releases = lambda repo, force=False: [
        {"tag_name": "v1", "draft": True, "prerelease": False, "assets": []},
        {"tag_name": "v2", "draft": False, "prerelease": True, "assets": []},
        {"tag_name": "v3", "draft": False, "prerelease": False,
         "assets": [{"name": "readme.txt", "size": 1}]},
    ]
    s._gh_cache.clear()
    try:
        s.find_release("owner/name", False, "")
        chk(False, "无 .fpk 附件应当报错")
    except Exception as exc:                          # noqa: BLE001
        msg = str(exc)
        chk("3 个 Release" in msg and "draft 1" in msg, "报错说清总数/draft/预发布数量", msg[:90])
    # 指定 tag：现在直接问 /releases/tags/<tag>（不再在分页列表里找），
    # 所以「不存在」对应的是 404 —— 要说清是哪个 tag，而不是笼统的「没有可用的 Release」。
    import urllib.error

    def _404(url, retries=0):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    s.gh_get = _404
    try:
        s.find_release("owner/name", True, "v999")
        chk(False, "指定的 tag 不存在应当报错")
    except Exception as exc:                          # noqa: BLE001
        chk("v999" in str(exc), "说清是哪个 tag 不存在", str(exc)[:70])

    # 指定 tag 且存在 → 走单个对象分支，不再依赖列表分页
    def _one(url, retries=0):
        chk("releases/tags/" in url, "按 tag 精确取（走 /releases/tags/ 而不是列表）", url[-40:])
        return {"tag_name": "v1.2.3", "draft": False, "prerelease": True,
                "assets": [{"name": "a.fpk", "size": 1}], "published_at": "x", "body": ""}

    s.gh_get = _one
    r = s.find_release("owner/name", True, "v1.2.3")
    chk(r and r["tag"] == "v1.2.3", "指定 tag 能取到（预发布也允许，回滚是正经用途）")

    # ── 2b. Release 列表短缓存 ──
    print("▸ Release 列表短缓存（界面上的「获取版本 → 预览 → 添加」是一串操作）")
    s._gh_cache.clear()
    calls2 = []

    def _fake_get(url, retries=0):
        calls2.append(url)
        return [{"tag_name": "v1", "draft": False, "prerelease": False,
                 "assets": [{"name": "a.fpk", "size": 1}], "published_at": None, "body": ""}]

    s.gh_releases = REAL_GH_RELEASES    # 换回真实现（前面几个用例把它替成桩了）
    s.gh_get = _fake_get
    s.gh_releases("o/cached")
    s.gh_releases("o/cached")
    chk(len(calls2) == 1,
        "同一仓库十几秒内只打一次 GitHub（第 2 次命中缓存）", "上游调用 %d 次" % len(calls2))
    s._gh_cache["o/cached"] = (time.time() - 9999, s._gh_cache["o/cached"][1])
    s.gh_releases("o/cached")
    chk(len(calls2) == 2, "缓存过期后会重新拉（不会一直吃旧数据）", "上游调用 %d 次" % len(calls2))
    s._gh_cache.clear()

    # ── 3. 指定附件对不上时必须报错，不能静默换成别的包 ──
    print("▸ 指定的安装包对不上时不许静默替换")
    assets = [{"name": "x-x86.fpk", "size": 10}, {"name": "x-arm.fpk", "size": 20}]
    try:
        s._pick_asset(assets, want_arch="x86", pattern="不存在的.fpk", strict=True)
        chk(False, "strict 模式应当报错")
    except Exception as exc:                          # noqa: BLE001
        chk("不存在" in str(exc) and "x-x86.fpk" in str(exc), "报错并列出可选项", str(exc)[:80])
    got = s._pick_asset(assets, want_arch="x86", pattern="x-*.fpk")
    chk(got["name"] == "x-x86.fpk", "通配符命中时仍按架构挑", got.get("name"))

    # ── 4. 下载残留（孤儿分片）──
    print("▸ 下载残留清理")
    old_part = os.path.join(dl, "big.fpk.part0")
    fresh_part = os.path.join(dl, "busy.fpk.part1")
    merge = os.path.join(dl, "x.fpk.merge")
    keep = os.path.join(dl, "keep.fpk")
    for p in (old_part, fresh_part, merge, keep):
        open(p, "wb").write(b"y" * 16)
    past = time.time() - 7200
    for p in (old_part, merge):
        os.utime(p, (past, past))
    listed = {i["name"] for i in s.orphan_parts() if i["old_enough"]}
    chk(listed == {"big.fpk.part0", "x.fpk.merge"}, "只把闲置够久的中间文件算作孤儿", listed)
    ok, why = s._safe_unlink_part(fresh_part)
    chk((not ok) and os.path.isfile(fresh_part), "正在下载的分片拒绝删除", why)
    ok, why = s._safe_unlink_part(keep)
    chk((not ok) and os.path.isfile(keep), "普通 .fpk 不走分片清理通道", why)
    ok, why = s._safe_unlink_part(os.path.join(tmp, "..", "etc", "passwd.fpk.part0"))
    chk(not ok, "目录外拒绝删除", why)
    res = s.clean_packages()
    chk(sorted(res["parts"]) == ["big.fpk.part0", "x.fpk.merge"],
        "一键清理把孤儿分片也清掉", res["parts"])
    chk(not os.path.isfile(old_part) and os.path.isfile(fresh_part),
        "孤儿删了、在下的没动")
    chk(res["parts_freed"] == 32, "残留释放字节数正确", res["parts_freed"])

    # ── 5. 并发的读改写不许丢更新 ──
    print("▸ 读改写的原子性（并发写不丢更新）")
    s.save_settings({"download_dir": dl, "concurrency": 4, "mirror": "",
                     "auto_download": False, "auto_clean": True, "theme": "auto"})

    # 直接检测**互斥**，而不是"碰运气看有没有丢"：
    # 给落盘函数套一层探针，记录进出次数；只要临界区里有第二个线程进来就算重叠。
    # 这样即使某次并发正好没丢，也不会假绿 —— 没有锁时是必然被抓到。
    class Probe:
        def __init__(self, target_name):
            self.name = target_name
            self.active = 0
            self.overlap = 0
            self.calls = 0
            self.guard = threading.Lock()

        def wrap(self, real):
            def inner(*a, **k):
                with self.guard:
                    self.active += 1
                    self.calls += 1
                    if self.active > 1:
                        self.overlap += 1
                time.sleep(0.004)          # 放大窗口：没锁的话必然被抓到
                try:
                    return real(*a, **k)
                finally:
                    with self.guard:
                        self.active -= 1
            return inner

    p_set = Probe("save_json")
    real_save_json = s.save_json
    s.save_json = p_set.wrap(real_save_json)
    errs = []

    def wset(i):
        try:
            for _ in range(5):
                s.save_settings({"concurrency": 1 + (i % 8)})
        except Exception as exc:                      # noqa: BLE001
            errs.append(exc)

    ths = [threading.Thread(target=wset, args=(i,)) for i in range(6)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    s.save_json = real_save_json
    chk(not errs, "并发 save_settings 不抛异常", errs[:2])
    chk(p_set.calls >= 30, "并发写确实发生了（探针计数 %d）" % p_set.calls)
    chk(p_set.overlap == 0,
        "设置读改写的临界区互斥（没有两次落盘重叠 → 不会丢更新）",
        "重叠 %d 次" % p_set.overlap)
    st = s.load_settings()
    chk(1 <= st["concurrency"] <= 8 and st["download_dir"] == dl,
        "并发写之后设置仍完好", {k: st[k] for k in ("concurrency", "download_dir")})

    p_reg = Probe("_save_registry")
    real_save_reg = s._save_registry
    s._save_registry = p_reg.wrap(real_save_reg)

    def wreg(i):
        for k in range(20):
            s.record_package(os.path.join(dl, "p%d_%d.fpk" % (i, k)), "app%d" % i, "1.%d" % k,
                             100, "d" * 64)

    ths = [threading.Thread(target=wreg, args=(i,)) for i in range(6)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    s._save_registry = real_save_reg
    chk(p_reg.overlap == 0, "台账读改写的临界区互斥",
        "重叠 %d 次" % p_reg.overlap)
    reg = s._load_registry()
    chk(len(reg) == 120, "并发写台账一条不丢（期望 120 条）", len(reg))

    # ── 6. save_json 的临时名唯一 ──
    print("▸ 临时文件名唯一")
    names = []
    orig_open = s.open if hasattr(s, "open") else open
    s.save_json(os.path.join(tmp, "t.json"), {"a": 1})
    leftovers = [n for n in os.listdir(tmp) if n.startswith("t.json.tmp")]
    chk(not leftovers, "写完不留临时文件", leftovers)
    caught = []
    real_open = __builtins__.open if isinstance(__builtins__, dict) else __builtins__.open

    def spy(path, *a, **k):
        if ".tmp." in str(path):
            caught.append(str(path))
        return real_open(path, *a, **k)

    import builtins
    builtins.open = spy
    try:
        ths = [threading.Thread(target=lambda: s.save_json(os.path.join(tmp, "c.json"), {"i": 1}))
               for _ in range(6)]
        [t.start() for t in ths]
        [t.join() for t in ths]
    finally:
        builtins.open = real_open
    chk(len(set(caught)) == len(caught), "并发写同一文件时临时名互不相同",
        "%d 次里有 %d 个不同名" % (len(caught), len(set(caught))))
    chk(json.load(open(os.path.join(tmp, "c.json"), encoding="utf-8")) == {"i": 1},
        "并发写之后目标文件是完整 JSON")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    # ── 6b. 检测的并发闸门 ──
    print("▸ 检测的并发闸门")
    calls = []
    real_check_one = s.check_one
    def _slow_check(a, force=False):
        # 必须让每轮检测有实际耗时：否则 4 个线程会「一个接一个」瞬间跑完，
        # 根本不会重叠 —— 那样测出来的绿是假的（我第一版就是这么写的）。
        calls.append(a.get("appname"))
        time.sleep(0.05)
        # 字段要够 run_check 后续逻辑用（_auto_download 会读 installed/latest）
        return {"appname": a.get("appname"), "display": a.get("appname"),
                "installed": "1.0", "latest": "1.0", "update_available": False,
                "error": None, "sha256": "x" * 64, "downloaded": None}

    s.check_one = _slow_check
    s.load_whitelist = lambda: [{"repo": "o/n", "appname": "a%d" % i} for i in range(3)]
    done = []

    errs_gate = []

    def _rc():
        try:
            done.append(s.run_check("gate-probe"))
        except Exception as exc:                      # noqa: BLE001
            errs_gate.append("%s: %s" % (type(exc).__name__, exc))

    ths = [threading.Thread(target=_rc) for _ in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    chk(len(calls) == 4,
        "4 个并发 /api/check 只真跑了一轮（共 %d 次 per-app 调用，期望 3 个被管理 + 自身 1）"
        % len(calls))
    chk(len(done) == 4, "每个请求都有返回（没有把别人晾着）",
        "done=%d errs=%s" % (len(done), errs_gate[:1]))
    s.check_one = real_check_one

    # ── 6c. 检测并行 + 进度 ──
    print("▸ 检测并行与进度（串行 6 个应用实测 6.1 秒；并行应 ≈ 最慢的那个）")
    apps6 = [{"repo": "o/n%d" % i, "appname": "p%d" % i} for i in range(6)]
    s.load_whitelist = lambda: apps6

    def _slow_check(a, force=False):
        time.sleep(0.25)
        return {"appname": a["appname"], "installed": "1.0", "latest": "1.0",
                "update_available": False, "error": None, "sha256": "x" * 64,
                "downloaded": None}

    s.check_one = _slow_check
    t0 = time.time()
    res6 = s.run_check("parallel-probe")
    el6 = time.time() - t0
    self_n = sum(1 for r in res6 if r.get("self"))
    chk(len(res6) == 7 and self_n == 1,
        "6 个被管理应用 + 本应用自身都出了结果（自身单独打标）",
        "%d 条，self 标 %d 条" % (len(res6), self_n))
    chk(el6 < 0.6, "6 个各 0.25 秒的应用并行跑完（串行要 1.5 秒）", "%.2f 秒" % el6)
    st6 = s.check_state()
    chk(st6["done"] == 7 and st6["total"] == 7 and st6["running"] is False,
        "进度字段正确收尾（done/total/running）", st6)
    chk(st6["started_at"] and st6["finished_at"], "记录了起止时间（便于排障）")
    s.check_one = real_check_one
    s.load_whitelist = lambda: [{"repo": "o/n", "appname": "a%d" % i} for i in range(3)]

    # ── 6d. 检查期间要有「在跑」的进度可读（界面靠它显示 x/y）──
    print("▸ 检查期间的进度可观测")
    seen = []
    orig_slow = _slow_check

    def _probe(a, force=False):
        seen.append(s.check_state()["running"])
        time.sleep(0.05)
        return orig_slow(a)

    s.check_one = _probe
    s.run_check("progress-probe")
    chk(seen and all(seen), "检查进行期间 running=True（界面才能显示进度）", seen)
    chk(s.check_state()["running"] is False, "结束后 running 复位")
    s.check_one = real_check_one

    # ── 6e. 本地包按内容认领（谁下载的不重要，字节对不对才重要）──
    print("▸ 本地包按内容认领（归档目录里的包常常是别的脚本下的）")
    import hashlib as _hl
    # 自带临时目录：上面的用例跑完后 tmp 已经被清掉了（rmtree），这里不能复用它
    dl2 = tempfile.mkdtemp(prefix="fpku-claim-")
    s.save_settings({"download_dir": dl2})
    real = b"A" * 1234
    good = os.path.join(dl2, "app-a.fpk")
    with open(good, "wb") as f:
        f.write(real)
    rel = {"appname": "installed-one", "version": "9.9", "version_source": "manifest",
           "display_name": "X", "platform": "all", "tag": "v9.9", "asset_name": "app-a.fpk",
           "asset_url": "http://x/a.fpk", "sha256": _hl.sha256(real).hexdigest(),
           "size": len(real), "published_at": None, "notes": "", "source_kind": "repo"}
    s.resolve_source = lambda entry, force=False: rel
    s.installed_version = lambda n: "9.0"
    s.installed_display = lambda n: "Installed One"
    s.device_arch = lambda: "x86"
    entry = {"repo": "o/n", "url": None, "tag": None, "appname": "installed-one",
             "display": "X", "note": None}
    item = s.check_one(entry)
    chk(bool(item.get("downloaded")) and not item.get("local_unknown"),
        "内容与 Release 一致 → 认领（即使不是本应用下载的）",
        (item.get("downloaded"), item.get("local_unknown")))
    chk((item.get("downloaded") or {}).get("verified_by") == "sha256",
        "标明是按内容核对过的（verified_by=sha256）",
        (item.get("downloaded") or {}).get("verified_by"))

    with open(good, "wb") as f:          # 内容不一致（大小也不同，避免缓存误命中）
        f.write(b"B" * 4321)
    item2 = s.check_one(entry)
    lu = item2.get("local_unknown") or {}
    chk(bool(lu) and "不一致" in (lu.get("reason") or ""),
        "内容不一致 → 才提示，且说清是内容对不上", lu)
    chk(not item2.get("downloaded"), "内容不一致时不会认领它（安装按钮不会误导）")

    # 内容一致但要确认缓存不会把「改过的文件」也算成旧的
    with open(good, "wb") as f:
        f.write(real)
    item3 = s.check_one(entry)
    chk(bool(item3.get("downloaded")), "改回一致的内容后又认领（缓存会随大小/mtime 失效）",
        item3.get("local_unknown"))
    import shutil as _sh2
    _sh2.rmtree(dl2, ignore_errors=True)

    # ── 6g. 本应用自身：独立成一块（不进列表、不进「全部更新」）──
    print("▸ 本应用自身独立成块（不进列表、不进「全部更新」、跨版本过渡期仍认它）")
    s.load_whitelist = REAL_LOAD_WL     # 前面几节把列表换成了桩，这一节要测真的过滤逻辑
    with open(os.environ["FPKU_APPS_USER"], "w", encoding="utf-8") as f:
        json.dump({"apps": [
            {"appname": "cliproxyapi", "repo": "mickeyttsc/cliproxyapi-fpk"},
            {"appname": "fpk-update", "repo": "mickeyttsc/fpk-update"},   # 自己（写了 appname）
            {"appname": "", "repo": "someone/fpk-update"},                # 自己（只按仓库名认）
        ]}, f)
    chk(s.migrate_self_entry() is True, "迁移：把「把自己当普通应用」的旧记录移出列表")
    wl = s.load_whitelist()
    chk([a["appname"] for a in wl] == ["cliproxyapi"],
        "列表里不再有自身（只剩别的应用）", [a["appname"] for a in wl])
    chk(s.load_settings().get("self_repo") == "mickeyttsc/fpk-update",
        "移出时把它填的仓库记成 self_repo（用户当时填的地址不丢）",
        s.load_settings().get("self_repo"))

    with open(os.environ["FPKU_APPS_USER"], "w", encoding="utf-8") as f:
        json.dump({"apps": [{"appname": "fpk-update", "repo": "mickeyttsc/fpk-update"}]}, f)
    chk(s.load_whitelist() == [], "手工写回列表也不会混进来（load_whitelist 兜底过滤）")

    s.save_settings({"self_repo": "https://github.com/someone/fpk-update"})
    _sf = s.self_source()
    chk(_sf["appname"] == "fpk-update" and _sf["repo"] == "someone/fpk-update",
        "self_source：接受完整链接、归一化成 owner/name", _sf)
    chk(s._resolve_app("fpk-update") is not None,
        "动作链路认自身（跨版本过渡期页面发的就是 /api/update?appname=fpk-update）")
    chk(s._resolve_app("不存在的应用") is None, "其它不存在的 appname 仍然拒绝")
    s.save_settings({"self_repo": ""})
    chk(s.self_source()["repo"] == s.SELF_DEFAULT_REPO,
        "留空 → 回到内置默认仓库", s.SELF_DEFAULT_REPO)

    with open(os.environ["FPKU_APPS_USER"], "w", encoding="utf-8") as f:
        json.dump({"apps": [{"repo": "o/n", "appname": "a0"}]}, f)
    s.check_one = lambda a, force=False: {
        "appname": a["appname"], "installed": "1.0", "latest": "1.0",
        "update_available": False, "error": None, "sha256": "x" * 64, "downloaded": None}
    s.run_check("split-probe")
    _st = s.cached_state()
    chk(all(not r.get("self") for r in _st["apps"]), "检测结果 apps 里没有自身",
        [r.get("appname") for r in _st["apps"]])
    chk((_st.get("self") or {}).get("appname") == "fpk-update",
        '自身结果单独放在 state["self"]（界面据此画独立区块）', _st.get("self"))
    chk((_st.get("self") or {}).get("self") is True, "自身那条带 self 标")
    s.check_one = real_check_one

    # ── 6f. 「已是最新就不下载」──
    print("▸ 已是最新就不下载（实测白下过 400MB）")
    latest_ok = {"appname": "x", "installed": "1.2.3", "latest": "1.2.3",
                 "update_available": False, "error": None,
                 "downloaded": {"version": "1.2.3", "complete": True},
                 "local_unknown": None}
    chk(s.download_needed(latest_ok) is False,
        "已是最新且本地包核对过 → 不需要下载", s.download_needed(latest_ok))
    newer = dict(latest_ok, latest="1.2.4", update_available=True)
    chk(s.download_needed(newer) is True, "有新版本 → 要下载")
    chk(s.download_needed(dict(latest_ok, local_unknown={"name": "a.fpk", "size": 1,
                                                        "reason": "内容与 Release 不一致"})) is True,
        "本地同名包核对不过 → 允许重下（修包）")
    chk(s.download_needed(dict(latest_ok, downloaded={"version": "1.2.3", "complete": False})) is True,
        "本地包不完整 → 允许重下")
    chk(s.download_needed(dict(latest_ok, error="检查失败：限流")) is True,
        "状态不明（限流等）→ 不在这里拦")
    chk(s.download_needed(None) is True, "没有检测结果 → 不拦")
    chk(s.download_needed(dict(latest_ok, installed="1.2.0", downloaded=None)) is True,
        "装的比最新旧 → 要下载")
    chk(s.download_needed(dict(latest_ok, installed="9.9.9")) is True,
        "钉了旧版本（比最新还旧的目标）→ 要下载（回滚是正经用途）")

    # ── 6g. 清理默认保留「当前版本」的包 ──
    print("▸ 清理默认保留当前版本的包（实测被一键清掉过 383MB 的回滚包）")
    dl3 = tempfile.mkdtemp(prefix="fpku-keep-")
    s.save_settings({"download_dir": dl3, "clean_keep_current": True})
    _sh3 = os.path.join(dl3, "cur.fpk")
    _old3 = os.path.join(dl3, "old.fpk")
    for _p in (_sh3, _old3):
        with open(_p, "wb") as f:
            f.write(b"z" * 64)
    s.record_package(_sh3, "installed-one", "9.9", 64, "a" * 64)     # 当前装的就是 9.9
    s.record_package(_old3, "installed-one", "9.8", 64, "b" * 64)    # 旧版本
    s.installed_version = lambda n: "9.9"
    items = {i["name"]: i for i in s.list_packages()}
    chk(items["cur.fpk"]["current"] is True, "标出「这是当前版本的包」", items["cur.fpk"])
    summary = s.package_summary()
    chk(summary["keep_count"] == 1 and summary["count"] == 1,
        "汇总：可清 1 个（旧版本）、保留 1 个（当前版本）",
        {k: summary[k] for k in ("count", "keep_count")})
    res = s.clean_packages()
    chk(res["deleted"] == ["old.fpk"], "默认只删旧版本", res["deleted"])
    chk(res["kept"] == ["cur.fpk"] and os.path.isfile(_sh3), "当前版本的包被保留（回滚/修复用）", res["kept"])
    res2 = s.clean_packages(include_current=True)
    chk(res2["deleted"] == ["cur.fpk"] and not os.path.isfile(_sh3),
        "显式要求时可连当前版本一起清（这是用户的明确选择）", res2["deleted"])
    _sh4 = _sh3 + ".x"
    import shutil as _sh4mod
    _sh4mod.rmtree(dl3, ignore_errors=True)

    # ── 6h. 自更新友好：重启后目标版本已装上 → 记成功 ──
    print("▸ 升级自己时进程会被重启，那条任务不能记成「中断失败」")
    s.installed_version = lambda n: "9.9"          # 假装现在装的就是 9.9
    s._save_tasks({"seq": 4, "tasks": [
        {"id": "tUpd", "appname": "installed-one", "display": "X", "action": "update",
         "status": "running", "message": "正在升级…", "version": "9.9"},
        {"id": "tBad", "appname": "installed-one", "display": "X", "action": "update",
         "status": "running", "message": "正在升级…", "version": "8.8"},
        {"id": "tDl", "appname": "installed-one", "display": "X", "action": "download",
         "status": "running", "message": "下载中…", "version": "9.9"},
    ]})
    s._recover_stale_tasks()
    tk = {t["id"]: t for t in s.list_tasks()}
    chk(tk["tUpd"]["status"] == "ok" and "装好" in tk["tUpd"]["message"],
        "目标版本已装上 → 记为「更新完成」而不是失败",
        (tk["tUpd"]["status"], tk["tUpd"]["message"]))
    chk(tk["tBad"]["status"] == "failed", "版本没对上 → 仍是中断失败", tk["tBad"]["status"])
    chk(tk["tDl"]["status"] == "failed", "下载任务不套用这条判定（它不改已装版本）",
        tk["tDl"]["status"])

    # ── 6i. 入队时就记下目标版本（自更新容错更强）──
    print("▸ 入队即记目标版本（比「下载完再记」更早，进程更早掉也不怕）")
    s.load_whitelist = lambda: [{"repo": "o/n", "appname": "installed-one", "display": "X"}]
    import json as _j
    with open(os.path.join(tmp, "last-check.json"), "w", encoding="utf-8") as f:
        _j.dump({"at": "2026-01-01T00:00:00+08:00", "trigger": "manual",
                 "apps": [{"appname": "installed-one", "installed": "1.2.3",
                           "latest": "1.2.4", "update_available": True,
                           "error": None, "sha256": "a" * 64, "downloaded": None,
                           "local_unknown": None}]}, f)
    tid = s.enqueue("installed-one", "update")
    t = [x for x in s.list_tasks() if x["id"] == tid][0]
    chk(t.get("version") == "1.2.4", "update 任务入队时就带上目标版本", t.get("version"))
    s.clear_finished_tasks()

    # ── 6j. 两条自愈路径必须同一判定 ──
    print("▸ 两条自愈路径同一判定（否则一条认成功、另一条改回失败）")
    s._save_tasks({"seq": 9, "tasks": [
        {"id": "tJ", "appname": "installed-one", "display": "X", "action": "update",
         "status": "running", "message": "正在升级…", "version": "9.9"}]})
    s.installed_version = lambda n: "9.9"
    st, stage, msg, pct = s.judge_interrupted(s.list_tasks()[0])
    chk(st == "ok" and "装好" in msg, "共享判定认得出「其实装好了」", (st, msg))
    st2, _s2, msg2, _p2 = s.judge_interrupted({"appname": "installed-one", "action": "update",
                                               "version": "8.8"})
    chk(st2 == "failed", "版本没对上仍判失败", st2)
    s._recover_stale_tasks()
    chk({x["id"]: x["status"] for x in s.list_tasks()}["tJ"] == "ok", "启动自愈与它一致")
    s.clear_finished_tasks()

    # ── 6k. 单按钮「更新」：有校验过的本地包就不重复下载 ──
    print("▸ 有校验过的本地包就不重复下载（卡片上只有一个「更新」按钮）")
    dl4 = tempfile.mkdtemp(prefix="fpku-localpkg-")
    s.save_settings({"download_dir": dl4})

    def _make_fpk(path, appname, version):
        """造一个**最小的合法 fpk**（tar.gz 里含 manifest）并返回其 sha256。"""
        import tarfile as _tf, io as _io, gzip as _gz
        mf = ("appname               = %s\nversion               = %s\n"
              % (appname, version)).encode()
        buf = _io.BytesIO()
        with _tf.open(fileobj=buf, mode="w") as t:
            info = _tf.TarInfo("manifest"); info.size = len(mf)
            t.addfile(info, _io.BytesIO(mf))
        data = _gz.compress(buf.getvalue())
        with open(path, "wb") as f:
            f.write(data)
        return _hl.sha256(data).hexdigest()

    good4 = os.path.join(dl4, "app-local.fpk")
    sha4 = _make_fpk(good4, "installed-one", "9.9")
    rel4 = {"appname": "installed-one", "version": "9.9", "asset_name": "app-local.fpk",
            "sha256": sha4}
    # 内容正确的包（台账里没有、不是本应用下载的）→ 也应被复用
    path4, mf4 = s.local_package_for("installed-one", rel4)
    chk(path4 == good4 and (mf4 or {}).get("version") == "9.9",
        "内容一致 + 包内 manifest 对得上 → 复用（不再下载）", (path4, mf4))
    _make_fpk(good4, "installed-one", "9.9")
    with open(good4, "ab") as f:              # 追加一个字节 → 内容不再一致
        f.write(b"X")
    p4b, _ = s.local_package_for("installed-one", rel4)
    chk(p4b is None, "内容不一致 → 不复用（会重新下载）", p4b)
    chk(s.local_package_for("installed-one", {"appname": "x", "version": "1", "sha256": ""})[0] is None,
        "来源没有摘要 → 一律不复用（无法核对内容）")
    import shutil as _sh5
    _sh5.rmtree(dl4, ignore_errors=True)

    # ── 7. 自愈必须真的接在启动流程里（只定义不调用等于没修）──
    print("▸ 启动流程接线")
    import ast
    tree = ast.parse(open(src, encoding="utf-8").read())
    main_fn = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    called = []
    if main_fn is not None:
        for node in ast.walk(main_fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.append(node.func.id)
    chk("_recover_stale_tasks" in called, "main() 里调用了 _recover_stale_tasks", called[:8])
    # 而且必须在起 worker 之前调用（反过来会让新 worker 先跑，自愈可能漏掉）
    src_text = open(src, encoding="utf-8").read()
    i_call = src_text.index("_recover_stale_tasks()")
    i_worker = src_text.index("Thread(target=worker_loop")
    chk(i_call < i_worker, "自愈在启动 worker 之前（顺序不能反）",
        "call@%d worker@%d" % (i_call, i_worker))

    # 光有「能力函数」不够，还要确认它被接在了正确的地方 ——
    # 否则把 strict 去掉、把校验删掉，函数级测试照样全绿（实测过这种假绿）。
    print("▸ 关键修复点的接线")
    src_text = open(src, encoding="utf-8").read()
    chk('pattern=src["asset"] or None, strict=True)' in src_text,
        "resolve_source 挑选附件时用了 strict（对不上要报错，不许悄悄换包）")
    chk("find_release(target_repo, True, tag)" in src_text and "改钉失败" in src_text,
        "钉版本前会校验 tag / 附件真实存在")
    chk("_safe_unlink_part(" in src_text and "orphan_parts(" in src_text,
        "清理把下载残留也纳入（中间分片不会永久残留）")

    print()
    print("=" * 64)
    if BAD:
        print("✘ 运行时健壮性验证未通过：%d 处" % BAD)
        return 1
    print("✅ 运行时健壮性验证通过（残留自愈 / 瞬时重试 / 不静默替换 / 残留清理 / 并发不丢）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
