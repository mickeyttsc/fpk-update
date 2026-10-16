#!/usr/bin/env bash
# 构建 FPK更新 的 fpk 安装包
#
# 用法：bash scripts/build-fpk.sh [输出目录]
#
# 与其它自建仓的差别：本应用没有外部上游载荷（纯 Python 标准库 + 自写前端），
# 所以构建过程不下载任何东西，天然离线、可复现。唯一需要"生成"的是图标。
#
# 版本号来源：仓库根 manifest 的 version 字段。
set -euo pipefail

APPNAME="fpk-update"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT_DIR="${1:-.}"
mkdir -p "$OUT_DIR"

VERSION="$(grep '^version' manifest | awk -F'=' '{print $2}' | tr -d ' ')"
printf '%s' "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$' \
    || { echo "::error:: manifest version 非法：'$VERSION'（期望形如 1.0.0-1）"; exit 1; }

echo "=== FPK更新 构建 ==="
echo "版本: $VERSION"

# ── 1. 图标（一份几何定义生成全部图标；与仓库里的文件比对，不一致就刷新并**明说**）──
#     生成器是图标的唯一来源（脚本头部写着「请勿手改」）。这里不静默覆盖：
#     凡是被刷新的槽位都会打印出来，避免"我改了图标怎么没生效"这种糊涂账。
echo "── 生成图标（4 个 PNG 槽位 + favicon + 页头标志 + 回退图）──"
ICON_GEN="$(mktemp -d)"
python3 scripts/make-icons.py "$ICON_GEN" > /dev/null
REFRESHED=""
for rel in ICON.PNG ICON_256.PNG app/ui/images/icon_64.png app/ui/images/icon_256.png \
           app/www/favicon.svg app/www/icon.svg app/www/default-icon.svg; do
    if ! cmp -s "$ICON_GEN/$rel" "$ROOT/$rel"; then
        cp "$ICON_GEN/$rel" "$ROOT/$rel"
        REFRESHED="$REFRESHED $rel"
    fi
done
rm -rf "$ICON_GEN"
if [ -n "$REFRESHED" ]; then
    echo "── 以下图标与生成器不一致，已按生成器刷新（原文件被人手改过？）：$REFRESHED"
fi
echo "   图标一致（或已刷新）"

# ── 2. 组装构建根 ──
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BUILD_ROOT="$WORK/pkg"
mkdir -p "$BUILD_ROOT/app/bin" "$BUILD_ROOT/app/config" "$BUILD_ROOT/app/ui/images" \
         "$BUILD_ROOT/app/www" "$BUILD_ROOT/cmd" "$BUILD_ROOT/config" "$BUILD_ROOT/wizard"

# 载荷
cp app/bin/server.py        "$BUILD_ROOT/app/bin/server.py"
cp app/config/apps.json     "$BUILD_ROOT/app/config/apps.json"
cp app/ui/config            "$BUILD_ROOT/app/ui/config"
cp app/ui/images/icon_64.png  "$BUILD_ROOT/app/ui/images/icon_64.png"
cp app/ui/images/icon_256.png "$BUILD_ROOT/app/ui/images/icon_256.png"
for f in index.html app.js style.css favicon.svg icon.svg default-icon.svg; do
    cp "app/www/$f" "$BUILD_ROOT/app/www/$f"
done

# 生命周期脚本
for f in main install_init install_callback upgrade_init upgrade_callback \
         config_init config_callback uninstall_init uninstall_callback; do
    cp "cmd/$f" "$BUILD_ROOT/cmd/$f"
    chmod 755 "$BUILD_ROOT/cmd/$f"
done

# 外层 config / wizard
cp config/privilege "$BUILD_ROOT/config/privilege"
cp config/resource  "$BUILD_ROOT/config/resource"
for w in install upgrade config; do
    cp "wizard/$w" "$BUILD_ROOT/wizard/$w"
done

# 图标
cp ICON.PNG     "$BUILD_ROOT/ICON.PNG"
cp ICON_256.PNG "$BUILD_ROOT/ICON_256.PNG"

# manifest（checksum 留空，由后处理填）
grep -v '^checksum' manifest > "$BUILD_ROOT/manifest"
printf 'checksum              =\n' >> "$BUILD_ROOT/manifest"

# ── 3. 打包（fnpack 优先，tar+gzip 兜底）──
# 打包实现默认用 tar+gzip 兜底那条 —— 理由：CI（ubuntu-latest）上没有 fnpack，
# 而「本机用 fnpack、CI 用 tar」意味着两个打包实现各自漂移，谁都没被完整验证过。
# 统一走同一条路，本机测过的就是 CI 出的。要用官方 fnpack 时显式：
#   FPKU_USE_FNPACK=1 bash scripts/build-fpk.sh
FNPACK=""
if [ "${FPKU_USE_FNPACK:-0}" = "1" ]; then
    for cand in fnpack "$ROOT/fnpack"; do
        if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then FNPACK="$cand"; break; fi
    done
    [ -n "$FNPACK" ] || echo "（要求用 fnpack 但没找到，回落 tar+gzip）"
fi

TMP_FPK="$WORK/out.fpk"
if [ -n "$FNPACK" ]; then
    echo "── 使用 fnpack 构建（FPKU_USE_FNPACK=1）：$FNPACK ──"
    (cd "$BUILD_ROOT" && "$FNPACK" build)
    [ -f "$BUILD_ROOT/${APPNAME}.fpk" ] || { echo "::error:: fnpack 未生成 ${APPNAME}.fpk"; ls -la "$BUILD_ROOT"; exit 1; }
    cp "$BUILD_ROOT/${APPNAME}.fpk" "$TMP_FPK"
else
    echo "── 使用 tar+gzip 构建（与本仓库 CI 同一条路）──"
    (
        cd "$BUILD_ROOT"
        APP_ENTRIES="$(cd app && ls -A)"
        [ -n "$APP_ENTRIES" ] || { echo "::error:: app/ 目录为空"; exit 1; }
        # shellcheck disable=SC2086
        tar --owner=root --group=root --mtime='@0' -cf - -C app ${APP_ENTRIES} | gzip -n > app.tgz
        # 注意：不要写 `tar tzf ... | head -1` —— head 先退出会让 tar 吃到 SIGPIPE，
        # 在 `set -o pipefail` 下整个构建会以 141 静默死掉（实测踩过）。
        # sed -n '1p' 会把输入读完，不会提前关闭管道。
        FIRST_ENTRY="$(tar tzf app.tgz | sed -n '1p')"
        case "$FIRST_ENTRY" in
            app/*|./*|app|.) echo "::error:: 内层 app.tgz 首条为 '${FIRST_ENTRY}'，带前缀"; exit 1 ;;
        esac
        SUM="$(md5sum app.tgz | awk '{print $1}')"
        sed "s/^checksum[[:space:]]*=.*/checksum              = ${SUM}/" manifest > manifest.tmp && mv manifest.tmp manifest
        echo "$SUM" > manifest.checksum
        tar --owner=root --group=root --mtime='@0' -cf - manifest manifest.checksum cmd config wizard ICON.PNG ICON_256.PNG app.tgz | gzip -n > "${APPNAME}.fpk"
        rm -f app.tgz manifest.checksum
    )
    cp "$BUILD_ROOT/${APPNAME}.fpk" "$TMP_FPK"
fi

# ── 4. 后处理：属主归零 / 权限正规化 / 去软链 / 重算 checksum ──
python3 scripts/sanitize-fpk.py "$TMP_FPK" "$TMP_FPK"

# ── 5. 交付 ──
OUT_FPK="$OUT_DIR/${APPNAME}-${VERSION}.fpk"
cp "$TMP_FPK" "$OUT_FPK"
echo "── 产物 ──"
ls -la "$OUT_FPK"
sha256sum "$OUT_FPK" | awk '{print "sha256: "$1}'

# ── 6. 前端渲染自测（有 node 就跑；CI 与本机同一份）──
if command -v node >/dev/null 2>&1; then
    echo "── 前端渲染自测 ──"
    node scripts/render-selftest.js app/www/app.js \
        || { echo "::error:: 前端渲染自测未通过"; exit 1; }
else
    echo "（本机没有 node，跳过前端渲染自测）"
fi

# ── 7. 门禁（本机与 CI 共用同一份）──
bash scripts/verify-fpk-gates.sh "$OUT_FPK"

echo "✅ 构建完成：$OUT_FPK"
