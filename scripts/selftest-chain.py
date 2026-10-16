#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开发期自测：用应用自己的代码跑完整链路（不经过网页，但走同一批函数）。

为什么要有它：网页点一下"更新"依赖「仓库里确实有新版本」这一前提，而平时三个
应用都是最新的，没法随时触发。这个脚本把链路拆成两段，可以按需执行：

  --chain   只跑到「预检」为止：下载 → 校验 sha256 → 交给应用中心暂存 →
            读取当前安装卷。全程不改动任何东西（不提交升级任务）。
  --commit  在 --chain 的基础上继续：提交升级任务 → 轮询 → 核验版本 → 拉起应用。
            ⚠️ 会真的升级目标应用。

用法（在 NAS 上以 root 运行）：
    python3 scripts/selftest-chain.py --app cliproxyapi --chain
    python3 scripts/selftest-chain.py --app cliproxyapi --commit --fpk /path/to/x.fpk

环境变量需与 cmd/main 启动服务时一致（脚本会自己补默认值）。
"""

import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "app", "bin", "server.py")

# fnOS 的存储根不一定是 /vol2（可能是 /vol1…），别写死：用 FPKU_VOL_ROOT 覆盖。
VOL = os.environ.get("FPKU_VOL_ROOT", "/vol2")
HOME = os.path.join(VOL, "@apphome", "fpk-update")


def load_server():
    """按 cmd/main 的环境契约加载 server.py 为模块。"""
    defaults = {
        "FPKU_HOST": "127.0.0.1",
        "FPKU_PORT": "18600",
        "FPKU_STATIC": os.path.join(ROOT, "app", "www"),
        "FPKU_APPS_DEFAULT": os.path.join(ROOT, "app", "config", "apps.json"),
        "FPKU_APPS_USER": VOL + "/@appdata/fpk-update/apps.json",
        "FPKU_VAR": VOL,
        "FPKU_HOME": HOME,
        "FPKU_DOWNLOAD_DIR": VOL + "/downloads",
        "FPKU_APPS_ROOT": "/var/apps",
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)
    spec = importlib.util.spec_from_file_location("fpku_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, help="白名单里的 appname")
    ap.add_argument("--chain", action="store_true", help="只跑到预检（不提交升级）")
    ap.add_argument("--commit", action="store_true", help="继续提交升级并核验")
    ap.add_argument("--fpk", default="", help="指定本地 fpk（不给则从 GitHub 下载最新）")
    args = ap.parse_args()

    srv = load_server()
    apps = {a["appname"]: a for a in srv.load_whitelist()}
    app = apps.get(args.app)
    if not app:
        print("::error:: %s 不在白名单里（白名单：%s）" % (args.app, list(apps)))
        return 2

    print("=" * 66)
    print("目标应用: %s (%s)  仓库: %s" % (app["display"], app["appname"], app["repo"]))
    print("当前已装: %s" % srv.installed_version(app["appname"]))
    print("=" * 66)

    fpk_path = args.fpk
    target = None

    if not fpk_path:
        rel = srv.latest_release(app["repo"])
        if not rel:
            print("::error:: 仓库没有可用的正式 Release")
            return 1
        target = rel["version"]
        print("最新版本: %s  附件: %s (%s)" % (target, rel["asset_name"], srv._human(rel["size"])))
        print("sha256  : %s" % (rel["sha256"] or "（缺失，将拒绝）"))
        if not rel["sha256"]:
            print("::error:: 附件没有 sha256 摘要，拒绝继续")
            return 1
        os.makedirs(srv.DOWNLOAD_DIR, exist_ok=True)
        fpk_path = os.path.join(srv.DOWNLOAD_DIR, rel["asset_name"])
        tmp = fpk_path + ".part"
        print("\n[1/4] 下载并校验 …")
        import hashlib
        import urllib.request
        req = urllib.request.Request(rel["asset_url"])
        req.add_header("User-Agent", "fpk-update-selftest/1.0")
        h = hashlib.sha256()
        n = 0
        with urllib.request.urlopen(req, timeout=srv.DOWNLOAD_TIMEOUT) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
                n += len(chunk)
        got = h.hexdigest()
        if got != rel["sha256"]:
            os.unlink(tmp)
            print("::error:: sha256 校验失败：期望 %s 实际 %s" % (rel["sha256"], got))
            return 1
        os.replace(tmp, fpk_path)
        print("      ✓ %s 字节，sha256 一致" % n)
    else:
        print("使用本地 fpk: %s" % fpk_path)
        print("注意：此路径下 sha256 由调用方负责保证")

    print("\n[2/4] 交给应用中心暂存（只识别，不改动）…")
    staged = srv.stage_fpk(fpk_path)
    target = staged.get("version")
    print("      ✓ 识别为 appName=%s version=%s packageType=%s installed=%s"
          % (staged.get("appName"), staged.get("version"),
             staged.get("packageType"), staged.get("installed")))
    if staged.get("appName") != args.app:
        print("::error:: 包内应用名 %s 与目标 %s 不符" % (staged.get("appName"), args.app))
        return 1

    print("\n[3/4] 升级前预检（取当前安装卷）…")
    info = srv.daemon_call("/rpc/v1/update/info", {
        "appName": args.app,
        "updateVersion": target,
        "packageType": staged.get("packageType") or "app",
        "language": "zh-CN",
    })
    wiz = info.get("wizardInfo") or {}
    vol = wiz.get("installedVolumeID")
    print("      ✓ 当前版本=%s  目标版本=%s  安装卷=%s  installType=%s"
          % (wiz.get("version"), wiz.get("version") and target, vol, wiz.get("installType")))
    if not isinstance(vol, int) or vol <= 0:
        print("::error:: 取不到安装卷，按设计中止（绝不猜卷）")
        return 1

    if args.chain and not args.commit:
        print("\n[4/4] --chain 模式：到此为止，未提交任何升级任务（系统无改动）")
        print("✅ 链路自测通过（下载/校验/暂存/预检）")
        return 0

    print("\n[4/4] 提交升级任务并核验 …")
    before_cfg = _sha256(VOL + "/@appdata/%s/config.yaml" % args.app)
    before_ver = srv.installed_version(args.app)

    def progress(stage, msg):
        print("      [%s] %s" % (stage, msg))

    srv.upgrade_via_daemon(fpk_path, args.app, target, progress=progress)

    after_ver = srv.installed_version(args.app)
    after_cfg = _sha256(VOL + "/@appdata/%s/config.yaml" % args.app)
    print("\n版本: %s → %s" % (before_ver, after_ver))
    print("配置: %s → %s" % (before_cfg[:16] if before_cfg else "—",
                             after_cfg[:16] if after_cfg else "—"))
    ok = (srv.norm_version(after_ver or "") == srv.norm_version(target or ""))
    if before_cfg and after_cfg and before_cfg != after_cfg:
        print("::error:: 配置文件发生变化（数据未完整保留！）")
        ok = False
    if not ok:
        print("::error:: 升级未达到目标版本")
        return 1
    print("✅ 升级链路端到端通过")
    return 0


def _sha256(path):
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


if __name__ == "__main__":
    sys.exit(main())
