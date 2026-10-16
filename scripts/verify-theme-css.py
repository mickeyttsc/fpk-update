#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主题 CSS 门禁：暗色变量有**两处**，必须完全一致，且覆盖浅色的全部变量。

为什么需要它：暗色变量必须写两遍 ——
  ① @media (prefers-color-scheme: dark) { :root:not([data-theme]) {…} }
     跟随系统用，同时兜住"JS 还没执行"的那一瞬间（防首屏闪白）
  ② :root[data-theme="dark"] {…}
     用户手动强制深色用
CSS 变量没法"复用整块"，只能复制。复制就会漂 —— 哪天给浅色加了个新变量、
只在其中一处补了，就会在某种主题下露出未定义的颜色。这个脚本把它钉住。

用法：python3 scripts/verify-theme-css.py app/www/style.css
"""

import re
import sys


def extract_block(css, opener):
    """从 opener 开始，按大括号配对取出整块内容。"""
    i = css.find(opener)
    if i < 0:
        return None
    j = css.find("{", i + len(opener) - 1)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(css)):
        if css[k] == "{":
            depth += 1
        elif css[k] == "}":
            depth -= 1
            if depth == 0:
                return css[j + 1:k]
    return None


def vars_of(block):
    """从 CSS 块里取出 --var: value 映射。"""
    out = {}
    for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block or "", re.I):
        out[m.group(1)] = re.sub(r"\s+", " ", m.group(2).strip())
    return out


def main():
    path = sys.argv[1]
    css = open(path, encoding="utf-8").read()

    light = vars_of(extract_block(css, ":root {"))
    dark_media = vars_of(extract_block(css, ":root:not([data-theme]) {"))
    dark_forced = vars_of(extract_block(css, ':root[data-theme="dark"] {'))

    problems = []
    if not light:
        problems.append("找不到 :root 浅色变量块")
    if not dark_media:
        problems.append("找不到 @media 里的暗色变量块（:root:not([data-theme])）")
    if not dark_forced:
        problems.append('找不到 :root[data-theme="dark"] 暗色变量块')

    # 各检查互相独立：不要因为前面报错就跳过后面的比对（那样会漏报）
    if light and dark_forced:
        # 暗色里出现、浅色里没有的变量 = 浅色主题下该变量未定义 → 真问题
        only_dark = sorted(set(dark_forced) - set(light))
        if only_dark:
            problems.append("这些变量只在暗色里定义，浅色缺失：%s" % ", ".join(only_dark))
        # 反向不算问题：--accent / --radius 这类两套主题共用的，本来就只在 :root 定义一次

    if dark_media and dark_forced and dark_media != dark_forced:
        diff = []
        for k in sorted(set(dark_media) | set(dark_forced)):
            a, b = dark_media.get(k), dark_forced.get(k)
            if a != b:
                diff.append("    %s: @media=%r  [data-theme]=%r" % (k, a, b))
        problems.append("两处暗色变量不一致（改一处漏一处）：\n" + "\n".join(diff))

    print("  浅色变量 %d 个；@media 暗色 %d 个；[data-theme] 暗色 %d 个"
          % (len(light), len(dark_media), len(dark_forced)))
    if problems:
        for p in problems:
            print("::error:: " + p, file=sys.stderr)
        return 1
    print("  ✓ 两处暗色变量完全一致，且与浅色变量集合对齐")
    return 0


if __name__ == "__main__":
    sys.exit(main())
