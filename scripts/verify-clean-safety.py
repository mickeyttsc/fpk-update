#!/usr/bin/env python3
"""安装包清理功能的安全性验证 —— 真跑函数，不是 grep 关键字。

为什么单独做这一项：清理 = 删除文件。而本应用的下载目录是用户自己选的，很可能
就是他放安装包归档的目录 —— 里面除了本应用下的包，还躺着他自己归档的旧版本、
从别处拷进来的包。一旦「清理」退化成「把这个目录里的 *.fpk 全删了」，就是数据事故。

所以这里逐条把护栏跑一遍，每条都同时要求两件事：**该删的删掉了** 且 **不该删的一个没动**。

用法：python3 scripts/verify-clean-safety.py [app/bin/server.py]
"""
import importlib.util
import io
import os
import sys
import tarfile
import tempfile

BAD = 0


def chk(cond, label, detail=""):
    global BAD
    if cond:
        print("  ✔ %s" % label)
    else:
        BAD += 1
        print("  ✘ %s%s" % (label, ("  -> " + str(detail)) if detail else ""))


def load_server(path, var, dl, apps_root):
    os.environ.update({
        "FPKU_VAR": var,
        "FPKU_DOWNLOAD_DIR": dl,
        "FPKU_APPS_ROOT": apps_root,
        "FPKU_STATIC": os.path.join(var, "www"),
        "FPKU_HOME": var,
    })
    spec = importlib.util.spec_from_file_location("fpku_server_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def touch(p, size=32):
    with open(p, "wb") as f:
        f.write(b"x" * size)
    return p


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "app/bin/server.py"
    root = tempfile.mkdtemp(prefix="fpku-clean-gate-")
    var = os.path.join(root, "var")
    dl = os.path.join(root, "dl")
    other = os.path.join(root, "other")
    apps_root = os.path.join(root, "apps")
    for d in (var, dl, other, apps_root):
        os.makedirs(d, exist_ok=True)
    s = load_server(src, var, dl, apps_root)
    s.log = lambda *a, **k: None      # 门禁输出保持干净；断言看返回值不看日志

    print("▸ 台账之外的文件一个都不能碰（核心保护）")
    stray = touch(os.path.join(dl, "我自己归档的旧版本.fpk"))
    mine = touch(os.path.join(dl, "cliproxyapi-8.0.31-1.fpk"))
    s.record_package(mine, "cliproxyapi", "8.0.31-1", os.path.getsize(mine), "a" * 64)
    got = [i["name"] for i in s.list_packages()]
    chk(got == ["cliproxyapi-8.0.31-1.fpk"], "可清理清单只含本应用下载的包", "实际 %s" % got)
    chk(all(i["in_download_dir"] for i in s.list_packages()), "清单里的包都在下载目录内")

    print("▸ 同名但非本应用下载的包：看见了也不许删")
    # 这是真机上的实际情形：下载目录同时是用户的归档目录，里面躺着他的回滚包。
    # 这些包的**文件名恰好等于某个应用的当前版本附件**，但对本应用来说是外来文件。
    import json as _json
    asset = "cleanfnos_1.7.0_x86.fpk"
    foreign = touch(os.path.join(dl, asset))
    with open(os.path.join(var, "last-check.json"), "w", encoding="utf-8") as f:
        _json.dump({"at": "x", "trigger": "t", "apps": [
            {"appname": "cleanfnos", "asset_name": asset, "latest": "1.7.0"}]}, f)
    chk([i["name"] for i in s.list_packages()] == ["cliproxyapi-8.0.31-1.fpk"],
        "外来包不进可清理清单")
    # 外来包清单现在是**直接扫下载目录**（不再从检测结果反推）：
    # 检测失败时 asset_name 会变 None，之前那版会让整包从两个清单里一起消失。
    fgn = [i["name"] for i in s.foreign_packages()]
    chk(asset in fgn and "我自己归档的旧版本.fpk" in fgn,
        "外来包被如实列为「不会动」（扫目录，不依赖检测结果）", fgn)
    chk("cliproxyapi-8.0.31-1.fpk" not in fgn, "台账里认领的包不算外来", fgn)
    s.save_settings({"download_dir": dl})
    ok, why = s._safe_unlink(foreign)
    chk((not ok) and os.path.isfile(foreign), "同名外来包拒绝删除", why)
    r = s.clean_packages()
    chk(foreign not in (r["deleted"] or []) and os.path.isfile(foreign),
        "一键清理不会顺手删掉外来包")
    os.remove(os.path.join(var, "last-check.json"))
    # 上面那次清理已经把台账里的包删了 —— 重新放回去，后面的用例还要用它
    mine = touch(os.path.join(dl, "cliproxyapi-8.0.31-1.fpk"))
    s.record_package(mine, "cliproxyapi", "8.0.31-1", os.path.getsize(mine), "a" * 64)

    print("▸ 本应用自己的安装包（台账之外的那批）：该删的删、冒名的拒删")
    def _mk_fpk(path, appname):
        """造一只最小 fpk（tar.gz 里只放一条 manifest 成员）。"""
        with tarfile.open(path, "w:gz") as tf:
            md = ("appname               = %s\nversion               = 9.9.9\n"
                  % appname).encode()
            ti = tarfile.TarInfo("manifest")
            ti.size = len(md)
            tf.addfile(ti, io.BytesIO(md))

    own = os.path.join(dl, "fpk-update-9.9.9.fpk")
    _mk_fpk(own, "fpk-update")
    s._SELF_PKG_CACHE.update({"at": 0.0, "data": [], "paths": set()})
    chk([i["name"] for i in s.self_owned_packages()] == ["fpk-update-9.9.9.fpk"],
        "自己的安装包被认领（台账之外也算）")
    chk(own not in [i["name"] for i in s.foreign_packages()],
        "自己的包不算「不会动」的外来包")
    r = s.clean_packages()
    chk("fpk-update-9.9.9.fpk" in (r["self_deleted"] or []) and not os.path.isfile(own),
        "一键清理会删掉本应用自己的安装包", r.get("self_deleted"))
    # 反例：名字像我们的，但包里 appname 不是本应用 → 必须拒绝
    fake = os.path.join(dl, "fpk-update-8.0.0.fpk")
    _mk_fpk(fake, "someone-else")
    s._SELF_PKG_CACHE.update({"at": 0.0, "data": [], "paths": set()})
    chk("fpk-update-8.0.0.fpk" not in [i["name"] for i in s.self_owned_packages()],
        "冒名包不被认领为「自己的」")
    ok, why = s._safe_unlink_self(fake)
    chk((not ok) and os.path.isfile(fake), "冒名包拒删（包内 appname 不是本应用）", why)
    os.remove(fake)
    # 刚才那次 clean_packages 顺带把台账里的包也删了 —— 按上面的约定重新放回去，
    # 后面的「一键清理：删对的、留别的」还要用它
    mine = touch(os.path.join(dl, "cliproxyapi-8.0.31-1.fpk"))
    s.record_package(mine, "cliproxyapi", "8.0.31-1", os.path.getsize(mine), "a" * 64)

    print("▸ 护栏逐条（每条都必须拒绝）")
    outside = touch(os.path.join(other, "x.fpk"))
    ok, why = s._safe_unlink(outside)
    chk((not ok) and os.path.isfile(outside), "下载目录之外的文件拒绝删除", why)

    link = os.path.join(dl, "link.fpk")
    os.symlink(mine, link)
    ok, why = s._safe_unlink(link)
    chk((not ok) and os.path.islink(link), "符号链接拒绝删除", why)

    txt = touch(os.path.join(dl, "note.txt"))
    ok, why = s._safe_unlink(txt)
    chk((not ok) and os.path.isfile(txt), "非 .fpk 文件拒绝删除", why)

    ok, why = s._safe_unlink(os.path.join(dl, "根本不存在.fpk"))
    chk(not ok, "不存在的文件拒绝删除", why)

    ok, why = s._safe_unlink(os.path.join(dl, "..", "..", "etc", "passwd"))
    chk(not ok, "穿越路径拒绝删除", why)

    ok, why = s._safe_unlink(dl)
    chk((not ok) and os.path.isdir(dl), "目录本身拒绝删除", why)

    print("▸ 一键清理：删对的、留别的")
    res = s.clean_packages()
    chk(res["deleted"] == ["cliproxyapi-8.0.31-1.fpk"], "只删了台账里的那一个", res["deleted"])
    chk(not os.path.isfile(mine), "台账里的包确实删掉了")
    chk(os.path.isfile(stray), "同目录里用户自己归档的包还在")
    chk(os.path.isfile(outside), "目录外的包还在")
    chk(os.path.isfile(txt), "非 fpk 文件还在")
    chk(res["freed"] == 32, "释放字节数正确", res["freed"])
    chk(not s.list_packages(), "重复清理为空（幂等）")

    print("▸ 装后自动清理：只在该删的时候删")
    p1 = touch(os.path.join(dl, "cliproxyapi-9.0.0-1.fpk"))
    s.record_package(p1, "cliproxyapi", "9.0.0-1", os.path.getsize(p1), "b" * 64)
    r = s.clean_after_install("cliproxyapi", p1, "9.0.0-1")      # 本机没有这个应用
    chk(r == "" and os.path.isfile(p1), "版本核验不通过时保留安装包（它是回滚包）", repr(r))

    # 伪造一个「已装成目标版本」的应用，走一次成功路径
    adir = os.path.join(apps_root, "cliproxyapi")
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "manifest"), "w", encoding="utf-8") as f:
        f.write("appname               = cliproxyapi\nversion               = 9.0.0-1\n")
    r = s.clean_after_install("cliproxyapi", p1, "9.0.0-1")
    chk(r != "" and not os.path.isfile(p1), "核验通过后才清理", repr(r))

    print("▸ 开关关掉时一个都不许删")
    s.save_settings({"auto_clean": False})
    p2 = touch(os.path.join(dl, "cliproxyapi-9.0.0-2.fpk"))
    s.record_package(p2, "cliproxyapi", "9.0.0-2", os.path.getsize(p2), "c" * 64)
    r = s.clean_after_install("cliproxyapi", p2, "9.0.0-1")
    chk(r == "" and os.path.isfile(p2), "auto_clean 关 → 装完不删", repr(r))

    print("▸ 设置默认值与写入口")
    st = s.load_settings()
    chk("auto_clean" in st, "设置里有 auto_clean")
    s.save_settings({})
    with open(os.path.join(var, "settings.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    chk(s.load_settings().get("auto_clean") is True, "未设置过时默认开")

    print("▸ 下载完必须登记（否则会产生「清理不掉的包」）")
    body = open(src, encoding="utf-8").read()
    chk("record_package(fpk_path" in body, "下载成功后写入台账")

    print("")
    print("────────────────────────────────────────")
    if BAD:
        print("✘ 安装包清理安全验证未通过：%d 处" % BAD)
        return 1
    print("✅ 安装包清理安全验证通过（该删的删掉、不该删的一个没动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
