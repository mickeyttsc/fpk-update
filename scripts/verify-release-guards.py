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
        (srv, '"app_version"', "后端把本应用版本回给界面"),
        (js, "app-ver-chip", "顶栏常驻显示本应用版本"),
        (js, "app-ver-kv", "设置「运行信息」里也显示版本"),
    ]
    print("▸ 修复点必须还在（%d 项）" % len(REQUIRED))
    for text, needle, label in REQUIRED:
        chk(needle in text, label, "缺少片段：%s" % needle)

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
