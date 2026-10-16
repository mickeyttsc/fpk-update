#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后端能力边界门禁（AST 级，不靠 grep 猜）。

为什么不用 grep：本文件所在的 server.py 在文档字符串里【警告】不要使用
`appcenter-cli install-local`（那是会连数据一起抹掉的破坏性路径），
grep 会把这个警告本身当成违规。判据必须落在「实际调用了什么」上。

本脚本断言两条硬边界：
  1. 所有 subprocess 调用只允许 appcenter-cli 的四个子命令 ——
     status / start（查状态、拉起应用）、install-fpk（完整安装，
     只用于白名单里尚未安装的应用、且包必须已校验 sha256）、
     default-volume（只读：系统默认安装卷）。
     没有卸载、没有删除、没有 install-local。
  2. 所有 daemon_call 的路由只允许升级通道那 5 条 ——
     暂存 / 暂存状态 / 升级前检查 / 升级提交 / 任务状态。

用法：python3 scripts/verify-server-safety.py <server.py>
"""

import ast
import sys

ALLOWED_SUBPROCESS = {
    ("appcenter-cli", "status"),
    ("appcenter-cli", "start"),
    ("appcenter-cli", "install-fpk"),     # 首次完整安装（v1.3）
    ("appcenter-cli", "default-volume"),  # 只读：读系统默认安装卷
}

ALLOWED_ROUTES = {
    "/rpc/v1/download/task",
    "/rpc/v1/download/status",
    "/rpc/v1/update/info",
    "/rpc/v1/update/task",
    "/rpc/v1/common/status",
}


def main():
    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    problems = []
    subprocess_calls = []
    daemon_routes = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func

        # subprocess.run([...], ...) / subprocess.Popen(...)
        is_subprocess = (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "subprocess"
        )
        if is_subprocess:
            if not node.args or not isinstance(node.args[0], ast.List):
                problems.append("第 %d 行：subprocess 调用的第一个参数不是字面量列表，"
                                "无法静态判定它执行了什么" % node.lineno)
                continue
            parts = []
            for elt in node.args[0].elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    parts.append(elt.value)
                else:
                    parts.append("<动态>")
            subprocess_calls.append((node.lineno, tuple(parts)))

        # daemon_call("/rpc/v1/...", ...)
        if isinstance(fn, ast.Name) and fn.id == "daemon_call":
            if node.args and isinstance(node.args[0], ast.Constant):
                daemon_routes.append((node.lineno, node.args[0].value))

    for lineno, parts in subprocess_calls:
        head = parts[:2]
        if head not in ALLOWED_SUBPROCESS:
            problems.append("第 %d 行：不允许的外部命令 %s（只允许 appcenter-cli "
                            "status/start）" % (lineno, " ".join(parts)))
    for lineno, route in daemon_routes:
        if route not in ALLOWED_ROUTES:
            problems.append("第 %d 行：不允许的应用中心接口 %s" % (lineno, route))

    if not subprocess_calls:
        problems.append("没有发现任何 subprocess 调用 —— 升级后拉起应用依赖 "
                        "appcenter-cli start，请确认代码没被改坏")
    if "/rpc/v1/update/task" not in [r for _, r in daemon_routes]:
        problems.append("没有发现升级提交调用（/rpc/v1/update/task）—— 功能是死的")

    print("  subprocess 调用 %d 处，应用中心接口 %d 处" % (len(subprocess_calls), len(daemon_routes)))
    for lineno, parts in subprocess_calls:
        print("    L%-4d %s" % (lineno, " ".join(parts)))
    for lineno, route in daemon_routes:
        print("    L%-4d %s" % (lineno, route))

    if problems:
        for p in problems:
            print("::error:: " + p, file=sys.stderr)
        return 1
    print("  ✓ 能力边界符合预期（只能查状态、拉起应用、走升级通道）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
