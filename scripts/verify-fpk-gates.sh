#!/usr/bin/env bash
# FPK更新 的 fpk 硬门禁（CI 与本机共用同一份，避免两处漂移）
# 用法：bash scripts/verify-fpk-gates.sh <fpk 路径>
# 任何一条不过 → exit 1（不交付、不发版）。
set -euo pipefail

FPK="${1:?用法: verify-fpk-gates.sh <fpk>}"
fail() { echo "::error::$*"; exit 1; }

[ -f "$FPK" ] || fail "找不到 $FPK"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
GATE_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 0. 门禁自检：证明 grep 通路是活的（防写法坏掉后恒真恒假）──
printf 'SENTINEL_CANARY\n' > "$TMP/canary"
grep -q SENTINEL_CANARY "$TMP/canary" || fail "门禁自检失败：grep 通路异常"

# ── 0b. 门禁自身不是空转：逐条破坏修复点，确认会被拦住（约 13 秒）──
#     放在最前面：如果门禁逻辑被改坏了，后面所有断言都可能是假绿。
python3 "$GATE_DIR/verify-gates-reverse.py" \
    || fail "门禁反向验证未通过：有修复点被破坏后门禁仍放行"

# ── 1. 外层结构 ──
tar tzf "$FPK" > "$TMP/outer.list" || fail "外层不是有效 tar.gz"
for f in manifest manifest.checksum cmd config wizard app.tgz ICON.PNG ICON_256.PNG; do
    grep -qE "^${f}(/|$)" "$TMP/outer.list" || fail "外层缺条目: $f"
done
head -1 "$TMP/outer.list" | grep -qx 'manifest' || fail "外层第一条不是 manifest（安装器依赖顺序）"

# ── 2. checksum 自洽 ──
tar xzf "$FPK" -C "$TMP" app.tgz manifest manifest.checksum ICON.PNG ICON_256.PNG cmd wizard
SUM_ACT="$(md5sum "$TMP/app.tgz" | awk '{print $1}')"
SUM_FILE="$(tr -d ' \r\n' < "$TMP/manifest.checksum")"
[ "$SUM_FILE" = "$SUM_ACT" ] || fail "manifest.checksum($SUM_FILE) != md5(app.tgz)($SUM_ACT)"
SUM_MF="$(grep '^checksum' "$TMP/manifest" | awk -F'=' '{print $2}' | tr -d ' ')"
[ "$SUM_MF" = "$SUM_ACT" ] || fail "manifest 的 checksum 字段($SUM_MF) != md5(app.tgz)($SUM_ACT)"

# ── 3. 内层结构 ──
tar tzf "$TMP/app.tgz" > "$TMP/inner.list"
FIRST="$(head -1 "$TMP/inner.list")"
case "$FIRST" in
    app|app/*|./*|.|'') fail "内层 app.tgz 首条 '$FIRST' 带前缀（装完路径全错位）" ;;
esac
for f in bin/server.py config/apps.json ui/config \
         ui/images/icon_64.png ui/images/icon_256.png \
         www/index.html www/app.js www/style.css www/favicon.svg www/icon.svg \
         www/default-icon.svg; do
    grep -qE "^${f}$" "$TMP/inner.list" || fail "内层缺必需条目: $f"
done

# 禁构建机路径痕迹
if grep -qE 'runner|/home/[a-z]|\.agent-node-work' "$TMP/inner.list"; then
    fail "内层条目含构建机路径"
fi
# 禁把源码目录、测试、脚本混进载荷
if grep -qE '^(scripts|tests?|\.github)/' "$TMP/inner.list"; then
    fail "内层混入了不该进包的目录"
fi

# ── 4. 可执行位 ──
tar tvzf "$FPK" > "$TMP/outer.tv"
BAD="$(grep -E ' cmd/[a-z_]+$' "$TMP/outer.tv" | awk '$1 !~ /^-rwx/ {print $NF}' || true)"
[ -z "$BAD" ] || fail "cmd/* 缺可执行位: $BAD"

# ── 5. manifest 字段完整性 / 身份 / 端口 ──
DUP="$(awk -F= '/^[a-z_]+[[:space:]]*=/{gsub(/[[:space:]]/,"",$1); print $1}' "$TMP/manifest" | sort | uniq -d)"
[ -z "$DUP" ] || fail "manifest 存在重复字段: $DUP"
for f in appname version display_name platform source maintainer maintainer_url \
         distributor distributor_url desktop_uidir desktop_applaunchname service_port ctl_stop; do
    grep -qE "^${f}[[:space:]]*=" "$TMP/manifest" || fail "manifest 缺必需字段: $f"
done
getf() { grep -E "^$1[[:space:]]*=" "$TMP/manifest" | sed -n '1p' | sed 's/.*=[[:space:]]*//' | tr -d '"' | tr -d ' '; }
[ "$(getf appname)" = "fpk-update" ] || fail "appname 必须为 fpk-update，实际=$(getf appname)"
[ "$(getf distributor)" = "Mickey" ] || fail "distributor(打包者) 必须为 Mickey，实际=$(getf distributor)"
[ "$(getf service_port)" = "18600" ] || fail "service_port 必须为 18600，实际=$(getf service_port)"
[ "$(getf ctl_stop)" = "true" ] || fail "ctl_stop 必须为 true（否则应用中心停止按钮不生效）"
MF_VER="$(getf version)"
printf '%s' "$MF_VER" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9]+)?$' || fail "version 格式非法: $MF_VER（期望 1.0.0 或 1.0.0-1）"

# 桌面入口端口必须与 manifest 的 service_port 一致（不一致 → 桌面图标打不开）
tar xzf "$TMP/app.tgz" -C "$TMP"
UI_PORT="$(python3 -c "
import json,sys
d=json.load(open('$TMP/ui/config'))
urls=d.get('.url',{})
for k,v in urls.items():
    print(v.get('port',''))
" | sed -n '1p')"
[ "$UI_PORT" = "18600" ] || fail "ui/config 里的入口端口($UI_PORT) 与 manifest service_port(18600) 不一致"

# ── 6. 权限与能力边界（本应用最关键的专项门禁）──
tar xzf "$FPK" -C "$TMP" config/privilege config/resource
RUN_AS="$(python3 -c "
import json;d=json.load(open('$TMP/config/privilege'));print(d.get('defaults',{}).get('run-as',''))")"
[ "$RUN_AS" = "root" ] || fail "privilege 的 run-as 必须为 root（否则连不上应用中心升级通道），实际=$RUN_AS"

# 后端必须只通过白名单工作，且能力边界必须可静态判定
grep -q 'def load_whitelist' "$TMP/bin/server.py" || fail "后端缺少白名单加载逻辑"
python3 "$GATE_DIR/verify-server-safety.py" "$TMP/bin/server.py" \
    || fail "后端能力边界门禁未通过（见上面的 ::error:: 明细）"
# 清理 = 删文件，是本应用唯一有破坏力的动作 —— 必须真跑一遍护栏
python3 "$GATE_DIR/verify-clean-safety.py" "$TMP/bin/server.py" \
    || fail "安装包清理安全门禁未通过（见上面的 ::error:: 明细）"
# 运行时健壮性：残留任务自愈 / 瞬时重试 / 不静默换包 / 残留清理 / 并发不丢更新
python3 "$GATE_DIR/verify-runtime-recovery.py" "$TMP/bin/server.py" \
    || fail "运行时健壮性门禁未通过（见上面的 ::error:: 明细）"
# 接口延迟：前端会调的每个接口都必须 < 200ms（网络调用换瞬时桩）；检查类接口必须立刻返回
python3 "$GATE_DIR/verify-endpoint-latency.py" "$TMP/bin/server.py" \
    || fail "接口延迟门禁未通过（见上面的 ::error:: 明细）"

# 语法必须能过（装机前就拦住写坏的代码）
python3 -m py_compile "$TMP/bin/server.py" || fail "bin/server.py 语法错误"
python3 -c "import json;json.load(open('$TMP/config/apps.json'))" || fail "config/apps.json 不是合法 JSON"

# 三个动作必须都在（下载 / 安装 / 下载并更新）—— 少一个就是功能回退
for a in download install update; do
    grep -q "def do_$a" "$TMP/bin/server.py" || fail "后端缺少动作实现: do_$a"
    grep -q "\"/api/$a\"" "$TMP/bin/server.py" || fail "后端缺少接口: /api/$a"
    grep -q "/api/$a" "$TMP/www/app.js" || fail "前端没有调用 /api/$a"
done
# 设置项必须存在（令牌 + 下载目录）
grep -q '"/api/settings"' "$TMP/bin/server.py" || fail "后端缺少设置接口"
grep -q '"/api/token"' "$TMP/bin/server.py" || fail "后端缺少令牌接口"
grep -q 'download_dir' "$TMP/bin/server.py" || fail "后端缺少下载目录设置"
# 通用性相关：新增/移除应用的接口与「读 fpk manifest 定版本」的能力必须在
for k in 'def read_fpk_manifest' 'def read_local_fpk_manifest' 'def resolve_source' 'def find_release'; do
    grep -q "$k" "$TMP/bin/server.py" || fail "后端缺少通用化能力: $k"
done
for e in '/api/apps/options' '/api/apps/preview' '/api/apps/add' '/api/apps/pin' '/api/apps/remove' '/api/apps/reset'; do
    grep -q "\"$e\"" "$TMP/bin/server.py" || fail "后端缺少接口: $e"
    grep -q "$e" "$TMP/www/app.js" || fail "前端没有调用 $e"
done
# 手动选版本/安装包的能力必须在（多版本可选）
grep -q 'def list_options' "$TMP/bin/server.py" || fail "后端缺少「列出可选版本/安装包」"
grep -q 'ver-select' "$TMP/www/index.html" || fail "前端缺少版本选择器"
grep -q 'asset-select' "$TMP/www/index.html" || fail "前端缺少安装包选择器"
# 内建下载服务：后台队列 + 真实进度 + 加速
for k in 'def worker_loop' 'def enqueue' 'class Downloader' 'def download_urls' 'def _dl_message'; do
    grep -q "$k" "$TMP/bin/server.py" || fail "后端缺少下载服务能力: $k"
done
grep -q 'segmented' "$TMP/bin/server.py" || fail "后端缺少分段下载"
grep -q 'concurrency' "$TMP/bin/server.py" || fail "后端缺少并发设置"
grep -q 'mirror' "$TMP/bin/server.py" || fail "后端缺少加速镜像设置"
for e in '/api/tasks' '/api/tasks/clear'; do
    grep -q "\"$e\"" "$TMP/bin/server.py" || fail "后端缺少接口: $e"
done
grep -q 'task-list' "$TMP/www/index.html" || fail "前端缺少任务列表"
grep -q 'conc-input' "$TMP/www/index.html" || fail "前端缺少并发设置项"
grep -q 'mirror-select' "$TMP/www/index.html" || fail "前端缺少加速镜像设置项"
grep -q 'auto-dl-input' "$TMP/www/index.html" || fail "前端缺少自动下载开关"
grep -q 'auto_download' "$TMP/bin/server.py" || fail "后端缺少自动下载设置"
grep -q 'def _auto_download' "$TMP/bin/server.py" || fail "后端缺少自动下载实现"
grep -q 'progressBlock' "$TMP/www/app.js" || fail "前端缺少进度块"
grep -q 'function resultStrip' "$TMP/www/app.js" || fail "前端缺少结果条"
grep -q 'function versionRow' "$TMP/www/app.js" || fail "前端缺少版本行"
# 卡片不许再出现「同一件事说三遍」的旧写法
if grep -q 'dl-hint' "$TMP/www/app.js"; then
    fail "前端仍有旧的重复下载提示（dl-hint），会把同一件事说多遍"
fi
if grep -q 'resultRow' "$TMP/www/app.js"; then
    fail "前端仍有旧的 resultRow（带进度条的完成态），会与结果条重复表达"
fi
# 响应式：桌面多列网格 + 手机端全屏设置
grep -q 'grid-template-columns' "$TMP/www/style.css" || fail "缺少桌面多列布局"
grep -q 'max-width: 700px' "$TMP/www/style.css" || fail "缺少手机端断点"
# 主题：设置项 + 界面选择器 + 两处暗色变量一致
grep -q 'theme' "$TMP/bin/server.py" || fail "后端缺少主题设置"
grep -q 'THEME_CHOICES' "$TMP/bin/server.py" || fail "后端缺少主题取值校验"
grep -q 'theme-seg' "$TMP/www/index.html" || fail "前端缺少主题选择器"
grep -q 'applyTheme' "$TMP/www/app.js" || fail "前端缺少主题应用逻辑"
grep -q 'data-theme' "$TMP/www/style.css" || fail "CSS 缺少手动主题通道"
python3 "$GATE_DIR/verify-theme-css.py" "$TMP/www/style.css" \
    || fail "主题 CSS 门禁未通过（见上面的 ::error:: 明细）"
# 设置分页
grep -q 'settings-tabs' "$TMP/www/index.html" || fail "设置缺少分页"
grep -q 'data-pane' "$TMP/www/index.html" || fail "设置缺少分页面板"
grep -q 'switchTab' "$TMP/www/app.js" || fail "前端缺少分页切换逻辑"
grep -q '\.tabs \.tab' "$TMP/www/style.css" || fail "CSS 缺少分页样式"
# 按钮不许再被清空（手机端 span 被隐藏，替换 innerHTML 会变空按钮）
if grep -q 'btn\.innerHTML' "$TMP/www/app.js"; then
    fail "前端仍在替换按钮的 innerHTML —— 手机端会把按钮变成空白（应改成 .loading 转圈）"
fi
grep -q '\.btn\.loading' "$TMP/www/style.css" || fail "CSS 缺少按钮加载态（转圈）"
grep -q 'btn-spin' "$TMP/www/style.css" || fail "CSS 缺少转圈动画"
# 定位必须写清楚（这是"这个应用到底是什么"的唯一说明，别改成含糊的营销话）
grep -q 'GitHub 上 fpk 的下载更新器' "$TMP/manifest" || fail "manifest 缺少一句话定位"
grep -q 'GitHub 上 fpk 的下载更新器' "$TMP/wizard/install" || fail "安装向导缺少一句话定位"
grep -q '只更新本机已安装的应用' "$TMP/manifest" || fail "manifest 缺少「只更新已安装应用」这条边界"
# 自审修的那几条也要钉住（否则以后会被改回去）
grep -q 'Cache-Control' "$TMP/bin/server.py" || fail "静态资源缺少 no-cache（升级后可能用旧界面）"
grep -q 'AUTO_DL_RETRY_SEC' "$TMP/bin/server.py" || fail "自动下载缺少重试退避（坏包会被反复重试）"
grep -q 'throttled' "$TMP/bin/server.py" || fail "检查缺少节流（连点会烧掉 GitHub 额度）"
grep -q 'fnOS 应用私有目录' "$TMP/bin/server.py" || fail "下载目录缺少私有目录护栏"
grep -q 'badge.warn\|\.badge\.warn' "$TMP/www/style.css" || fail "CSS 缺少 warn 徽章"
grep -q "有新版 · 不能自动更新" "$TMP/www/app.js" || fail "前端缺少「有新版但不可自动更新」的徽章"
grep -q 'const busy = running' "$TMP/www/app.js" \
    || fail "按钮仍被全局忙碌禁用（应该只禁用该应用自己，否则没法排队多个）"
grep -q 'function syncInput' "$TMP/www/app.js" || fail "设置项缺少回填同步"
if grep -q 'const job = s.job' "$TMP/www/app.js"; then
    fail "render() 里还留着没用到的 job 变量"
fi
# 对标手机应用商店的四项优化，别被改回去
grep -q '"/api/all"' "$TMP/bin/server.py" || fail "后端缺少批量接口 /api/all"
grep -q '/api/all' "$TMP/www/app.js" || fail "前端没有调用批量接口"
grep -q 'id="summary"' "$TMP/www/index.html" || fail "缺少顶部汇总条"
grep -q 'all-update' "$TMP/www/index.html" || fail "缺少「全部更新」按钮"
grep -q 'function renderSummary' "$TMP/www/app.js" || fail "缺少汇总条渲染"
grep -q 'function sortApps' "$TMP/www/app.js" || fail "缺少卡片排序（有更新的排前面）"
grep -q 'c-notes' "$TMP/www/app.js" || fail "缺少更新说明（changelog）展示"
grep -q 'c-notes' "$TMP/www/style.css" || fail "更新说明缺少样式"
grep -q "'重试'" "$TMP/www/app.js" || fail "失败结果条缺少「重试」"
grep -q '\.c-state \.mini' "$TMP/www/style.css" || fail "重试按钮缺少样式"
# 安装包清理：一键清理 + 装后自动清理 + 台账（别退化成「把下载目录里的 fpk 全删了」）
grep -q '"/api/clean"' "$TMP/bin/server.py" || fail "后端缺少一键清理接口 /api/clean"
grep -q '"/api/packages"' "$TMP/bin/server.py" || fail "后端缺少安装包清单接口 /api/packages"
grep -q '/api/clean' "$TMP/www/app.js" || fail "前端没有调用清理接口"
grep -q 'function doClean' "$TMP/www/app.js" || fail "前端缺少一键清理逻辑"
grep -q 'def clean_packages' "$TMP/bin/server.py" || fail "后端缺少清理实现"
grep -q 'def clean_after_install' "$TMP/bin/server.py" || fail "后端缺少「装完自动清理」"
grep -q 'def record_package' "$TMP/bin/server.py" || fail "后端缺少安装包台账登记（会产生清理不掉、也认不出的包）"
grep -q 'record_package(fpk_path' "$TMP/bin/server.py" || fail "下载成功后没有登记台账"
grep -q 'clean_after_install(appname' "$TMP/bin/server.py" || fail "安装/更新成功后没有走自动清理"
grep -q 'auto_clean' "$TMP/bin/server.py" || fail "后端缺少 auto_clean 设置"
grep -q 'id="auto-clean-input"' "$TMP/www/index.html" || fail "设置里缺少自动清理开关"
grep -q 'id="clean-btn"' "$TMP/www/index.html" || fail "顶栏缺少清理按钮"
grep -q 'id="clean-now-btn"' "$TMP/www/index.html" || fail "设置里缺少一键清理按钮"
grep -q 'id="pkg-state"' "$TMP/www/index.html" || fail "设置里缺少安装包状态显示"
grep -q 'only .fpk\|只清理 .fpk' "$TMP/bin/server.py" || fail "清理缺少「只删 .fpk」护栏"
grep -q '拒绝删除' "$TMP/bin/server.py" || fail "清理缺少删除护栏"
grep -q '#clean-btn span' "$TMP/www/style.css" || fail "清理按钮缺少手机端适配（会长出文字撑破顶栏）"
# 本轮 QA 修掉的几类故障：接线与提示文案都要在，别被改回去
grep -q '_recover_stale_tasks()' "$TMP/bin/server.py" || fail "缺少启动自愈（重启后任务会永久卡住）"
grep -q 'GH_RETRIES' "$TMP/bin/server.py" || fail "GitHub 请求缺少瞬时故障重试"
grep -q 'def orphan_parts' "$TMP/bin/server.py" || fail "缺少下载残留清点"
grep -q 'def _safe_unlink_part' "$TMP/bin/server.py" || fail "缺少下载残留删除护栏"
grep -q 'parts_count' "$TMP/bin/server.py" || fail "后端没有把下载残留报给界面"
grep -q 'parts_count' "$TMP/www/app.js" || fail "界面没有呈现下载残留"
grep -q '瞬时抖动' "$TMP/bin/server.py" || fail "读不到 Release 时没有给出可重试的说明"
grep -q '改钉失败' "$TMP/bin/server.py" || fail "钉版本前没有校验 tag/附件是否存在"
grep -q 'strict=True' "$TMP/bin/server.py" || fail "指定附件对不上时会被静默替换成别的包"
grep -q 'percent": None\|percent. *: *None' "$TMP/bin/server.py" || fail "排队中的任务没有用「进度未知」表达"
# 独立审计（第二双眼睛）发现的另外几处，逐条钉住
grep -q '出于安全不会安装' "$TMP/bin/server.py" || fail "「安装」没有强制要求 sha256 摘要（无摘要来源会被跳过校验）"
grep -q 'MAX_BODY_BYTES' "$TMP/bin/server.py" || fail "缺少请求体上限常量"
grep -q 'close_connection = True' "$TMP/bin/server.py" || fail "请求体读不完时没有断开连接（残留字节会污染同连接的下一条请求）"
grep -q '安装包内的应用名不合法' "$TMP/bin/server.py" || fail "添加应用时没有校验包内 appname 合法性"
grep -q 'releases/tags/' "$TMP/bin/server.py" || fail "指定 tag 时没有按 tag 精确取（会被列表分页误判为不存在）"
grep -q 'a.downloaded && a.downloaded.version' "$TMP/www/app.js" || fail "确认框里的 downloaded 没有兜底（重试会静默无反应）"
# 下载完必须用本地包复核身份（防止「检测用启发式、装的是别的包」）
grep -q 'read_local_fpk_manifest(fpk_path)' "$TMP/bin/server.py" \
    || fail "下载/安装前缺少本地包身份复核"

# 必须没有登录/密码残留（1.0.0-3 起改为无密码）
if grep -qE 'access-password|/api/login|valid_session' "$TMP/bin/server.py" "$TMP/cmd/main" "$TMP/cmd/install_callback"; then
    fail "产物里仍有登录/密码相关残留（本应用应为无密码设计）"
fi

# ── 7. 零隐私 / 零机器绑定 ──
# 通用判据（不写打包者的具体值，避免门禁本身变成泄露）
SCAN_FILES="$TMP/bin/server.py $TMP/config/apps.json $TMP/ui/config $TMP/www/index.html $TMP/www/app.js $TMP/www/style.css $TMP/www/favicon.svg $TMP/www/icon.svg $TMP/www/default-icon.svg"
if grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}' $SCAN_FILES | grep -vE '0\.0\.0\.0|127\.0\.0\.1|(0\.0\.0\.0)' | grep -q .; then
    grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}' $SCAN_FILES | grep -vE '0\.0\.0\.0|127\.0\.0\.1' || true
    fail "产物里出现 IP 字面量（可能是打包者的内网地址）"
fi
if grep -nE 'mttao\.cn|\.local\b|/vol[0-9]+/[0-9]{4}/' $SCAN_FILES | grep -q .; then
    fail "产物里出现机器相关域名或个人路径"
fi
if grep -nE 'github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}' $SCAN_FILES | grep -q .; then
    fail "产物里出现疑似 GitHub 令牌字面量"
fi
# 前端不得**加载**任何外部资源（离线也要能开）。
# 判据要精确到「真实引用」——占位文案里出现示例网址（如 placeholder="https://github.com/..."）
# 是给人看的文字，不是加载；用宽泛的 https:// 匹配会把它误判成违规。
EXT_REF='(src|href)[[:space:]]*=[[:space:]]*["'"'"']https?://|url\(["'"'"']?https?://|fetch\(["'"'"']https?://|@import[[:space:]]+["'"'"']?https?://'
if grep -nE "$EXT_REF" "$TMP/www/index.html" "$TMP/www/app.js" "$TMP/www/style.css" \
        "$TMP/www/favicon.svg" "$TMP/www/icon.svg" "$TMP/www/default-icon.svg" | grep -q .; then
    grep -nE "$EXT_REF" "$TMP/www/index.html" "$TMP/www/app.js" "$TMP/www/style.css" \
        "$TMP/www/favicon.svg" "$TMP/www/icon.svg" "$TMP/www/default-icon.svg" || true
    fail "前端加载了外部资源（应完全自包含，离线可用）"
fi

# ── 8. 图标 ──
python3 - "$TMP/ICON_256.PNG" "$TMP/ICON.PNG" "$TMP/ui/images/icon_64.png" "$TMP/ui/images/icon_256.png" <<'PY' || fail "图标不合规"
import struct, sys
for p in sys.argv[1:]:
    with open(p, 'rb') as f:
        d = f.read(33)
    if d[:8] != b'\x89PNG\r\n\x1a\n':
        print('不是 PNG:', p); raise SystemExit(1)
    w, h = struct.unpack('>II', d[16:24])
    if (w, h) != (512, 512):
        print('尺寸不是 512x512:', p, w, h); raise SystemExit(1)
print('  图标 4 个槽位均为 512×512 PNG')
PY

# 图标必须**全部出自同一个生成器**：PNG 与 SVG 同源。
# 这条挡的是「改了 PNG 忘了改 favicon」这类漂移 —— 以前就发生过（圆角 16% vs 59%）。
ICON_TMP="$(mktemp -d)"
python3 "$GATE_DIR/make-icons.py" "$ICON_TMP" > /dev/null || fail "图标生成器跑不起来"
for pair in "ICON.PNG:$TMP/ICON.PNG" "ICON_256.PNG:$TMP/ICON_256.PNG" \
            "app/ui/images/icon_64.png:$TMP/ui/images/icon_64.png" \
            "app/ui/images/icon_256.png:$TMP/ui/images/icon_256.png" \
            "app/www/favicon.svg:$TMP/www/favicon.svg" \
            "app/www/icon.svg:$TMP/www/icon.svg" \
            "app/www/default-icon.svg:$TMP/www/default-icon.svg"; do
    src="${pair%%:*}"; dst="${pair#*:}"
    cmp -s "$ICON_TMP/$src" "$dst" || { rm -rf "$ICON_TMP"; fail "图标与生成器不一致（被手改了？）：$src"; }
done
rm -rf "$ICON_TMP"
# 同一个应用图标，四个槽位必须是同一张图
cmp -s "$TMP/ICON.PNG" "$TMP/ICON_256.PNG" || fail "ICON.PNG 与 ICON_256.PNG 不是同一张图"
cmp -s "$TMP/ICON.PNG" "$TMP/ui/images/icon_64.png" || fail "桌面图标与 ICON.PNG 不是同一张图"
cmp -s "$TMP/ICON.PNG" "$TMP/ui/images/icon_256.png" || fail "桌面图标(256) 与 ICON.PNG 不是同一张图"
cmp -s "$TMP/www/favicon.svg" "$TMP/www/icon.svg" || fail "favicon.svg 与页头 icon.svg 不一致"
# 页头标志必须引用共享文件，不许再内联手写一套 SVG
grep -q 'src="/favicon.svg"' "$TMP/www/index.html" \
    || fail "页头标志没有引用共享图标文件（又内联手写了一份，迟早和真图标不一样）"
if grep -q 'url(#g2)' "$TMP/www/index.html"; then
    fail "index.html 里还留着旧的内联手写 logo（gradient id=g2）"
fi
echo "  图标全部出自同一生成器，四槽位同一张图，页头引用共享文件"

echo "✅ 全部门禁通过: $FPK"
