#!/usr/bin/env python3
"""安装包清理功能的安全性验证 —— 真跑函数，不是 grep 关键字。

为什么单独做这一项：清理 = 删除文件。而本应用的下载目录是用户自己选的，很可能
就是他放安装包归档的目录 —— 里面除了本应用下的包，还躺着他自己归档的旧版本、
从别处拷进来的包。一旦「清理」退化成「把这个目录里的 *.fpk 全删了」，就是数据事故。

所以这里逐条把护栏跑一遍，每条都同时要求两件事：**该删的删掉了** 且 **不该删的一个没动**。

用法：python3 scripts/verify-clean-safety.py [app/bin/server.py]
"""
import importlib.util
import os
import sys
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
    chk([i["name"] for i in s.foreign_packages()] == [asset],
        "外来包被如实列为「不会动」")
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
