#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fpk 后处理：属主归零、权限正规化、去软链、重算 checksum、按固定顺序重打外层。

为什么必须有这一步：fnpack 与 tar 都会把构建机的属主/时间戳带进包里，
而 manifest.checksum 必须等于【最终】app.tgz 的 md5 —— 任何对 app.tgz 的
后处理都会让先前算好的值失效。所以顺序是：先净化 app.tgz，再算 md5，
再写回 manifest 与 manifest.checksum，最后按 manifest → manifest.checksum →
其余 → app.tgz 的顺序重打外层（fnOS 安装器按这个顺序读取）。

用法：python3 scripts/sanitize-fpk.py <输入 fpk> <输出 fpk>
"""

import hashlib
import io
import os
import sys
import tarfile
import tempfile


def sanitize_app_tgz(data):
    in_tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    out_buf = io.BytesIO()
    out_tar = tarfile.open(fileobj=out_buf, mode="w:gz", compresslevel=9)
    removed = kept = 0
    for m in in_tar.getmembers():
        if m.issym() or m.islnk():
            removed += 1
            continue
        m.uid = 0
        m.gid = 0
        m.uname = "root"
        m.gname = "root"
        m.mode = 0o755 if m.isdir() or (m.mode & 0o111) else 0o644
        m.mode &= ~0o7000
        m.mtime = 0
        try:
            f = in_tar.extractfile(m)
        except Exception as exc:  # noqa: BLE001
            print("WARN: 读取 %s 失败: %s" % (m.name, exc), file=sys.stderr)
            continue
        if m.isreg() and f is None:
            print("WARN: %s 没有数据" % m.name, file=sys.stderr)
            continue
        out_tar.addfile(m, f)
        kept += 1
    out_tar.close()
    print("  app.tgz: 去掉软链 %d 个，保留 %d 项" % (removed, kept))
    return out_buf.getvalue()


def main():
    src, dst = sys.argv[1], sys.argv[2]
    tmp = tempfile.mkdtemp()
    outer = tarfile.open(src, "r:gz")
    names = outer.getnames()
    app_data = outer.extractfile("app.tgz").read()
    for m in outer.getmembers():
        if m.name == "app.tgz":
            continue
        try:
            outer.extract(m, tmp, filter="fully_trusted")
        except TypeError:
            outer.extract(m, tmp)
    outer.close()

    new_app = sanitize_app_tgz(app_data)
    checksum = hashlib.md5(new_app).hexdigest()

    # manifest：回填 checksum
    mpath = os.path.join(tmp, "manifest")
    with open(mpath, "rb") as f:
        content = f.read().replace(b"\r\n", b"\n").decode("utf-8")
    wrote = False
    out_lines = []
    for line in content.splitlines(keepends=True):
        if line.startswith("checksum"):
            out_lines.append("checksum              = %s\n" % checksum)
            wrote = True
        else:
            out_lines.append(line)
    if not wrote:
        out_lines.append("checksum              = %s\n" % checksum)
    with open(mpath, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(out_lines))

    with open(os.path.join(tmp, "manifest.checksum"), "w", encoding="utf-8", newline="\n") as f:
        f.write(checksum + "\n")

    # 重打外层（固定顺序）
    out_buf = io.BytesIO()
    out_tar = tarfile.open(fileobj=out_buf, mode="w")
    # 固定顺序，与 build-fpk.sh 里声明的顺序一致：
    #   manifest → manifest.checksum → cmd → config → wizard → ICON.PNG → ICON_256.PNG → app.tgz
    # 原来这里按字典序排（ICON.PNG 的 I=0x49 排在 cmd 的 c=0x63 之前），于是产物顺序
    # 既不等于构建脚本声明、也不等于「先 cmd/config/wizard 后图标」的约定 —— 构建链
    # 自相矛盾，而门禁只校验第一条是 manifest，其余顺序没人管。这里消掉这个缺口。
    MEDIA = ("cmd", "config", "wizard", "ICON.PNG", "ICON_256.PNG")

    def _rank(n):
        head = n.split("/")[0]
        return (MEDIA.index(head) if head in MEDIA else len(MEDIA), n)

    order = ["manifest", "manifest.checksum"]
    order.extend(sorted((n for n in names
                         if n not in ("manifest", "manifest.checksum", "app.tgz")),
                        key=_rank))
    order.append("app.tgz")
    for name in order:
        if name == "app.tgz":
            info = tarfile.TarInfo(name="app.tgz")
            info.size = len(new_app)
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            out_tar.addfile(info, io.BytesIO(new_app))
            continue
        path = os.path.join(tmp, name)
        if not os.path.exists(path):
            continue
        info = tarfile.TarInfo(name=name)
        if os.path.isdir(path):
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.size = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            out_tar.addfile(info)
            continue
        with open(path, "rb") as f:
            data = f.read()
        info.size = len(data)
        info.mode = 0o755 if (os.stat(path).st_mode & 0o111) else 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = 0
        out_tar.addfile(info, io.BytesIO(data))
    out_tar.close()

    import gzip
    with open(dst, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(out_buf.getvalue())
    print("  manifest.checksum = md5(app.tgz) = %s" % checksum)


if __name__ == "__main__":
    main()
