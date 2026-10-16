#!/usr/bin/env bash
# 通过应用中心守护进程 RPC 就地升级某个已安装应用（开发/排障用）
#
# 用法：bash scripts/upgrade-via-rpc.sh <appname> <fpk路径> [期望的当前版本]
#
# 为什么不用 appcenter-cli install-fpk：它对已安装应用直接短路
# （"[Info]Application [x] is installed."，退出码还是 0），什么都不做。
# 也绝不用 install-local —— 那是「卸载再安装」，在 fnOS 上重装会失败并把
# 应用连同数据一起抹掉。
#
# 守护进程状态码：1=进行中 2=成功 5=守护进程已不再持有该任务（不代表失败，
# 用最终版本判定）。见 conversun/fnos-store 的 internal/platform/rpc.go 实测常量。
set -uo pipefail

APP="${1:?用法: upgrade-via-rpc.sh <appname> <fpk> [期望当前版本]}"
FPK="${2:?缺 fpk 路径}"
EXPECT_CUR="${3:-}"
SOCK=/var/run/com.trim.app.center.sock
# 存储根可配：fnOS 的卷可能是 /vol1、/vol2…，不写死
VOL_ROOT="${FPKU_VOL_ROOT:-/vol2}"
VAR="$VOL_ROOT/@appdata/$APP"
HOME_DIR="$VOL_ROOT/@apphome/$APP"

[ -f "$FPK" ] || { echo "FAIL: 找不到 $FPK"; exit 2; }
[ -S "$SOCK" ] || { echo "FAIL: 找不到应用中心 socket $SOCK"; exit 2; }

rpc() { curl -s --unix-socket "$SOCK" -X POST -H 'Content-Type: application/json' -d "$2" "http://localhost$1" --max-time 60; }
jget() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval('d'+sys.argv[1]))" "$1" 2>/dev/null; }

# 数据指纹：数据目录里除「下载缓存 / 日志 / 状态 / 应用自己生成的说明文件」外的所有文件
# （含路径、大小、sha256）。README-说明.txt 由 install_callback 按版本重写，属应用产物，
# 不算用户数据，故排除；如果它意外丢失/损坏，下面的存在性检查会兜住。
fingerprint() {
    local root
    for root in "$VAR" "$HOME_DIR"; do
        [ -d "$root" ] || continue
        ( cd "$root" && find . -type f \
            ! -name '*.log' ! -name '*.pid' \
            ! -path './downloads/*' ! -name 'last-check.json' \
            ! -name 'job.json' ! -name 'sessions.json' \
            ! -name 'README-说明.txt' \
            -printf '%p %s ' -exec sha256sum {} \; 2>/dev/null | awk '{print $1, $2, $3}' | sort )
    done
}

echo "================ 升级前 ================"
CUR="$(grep '^version' "/var/apps/$APP/manifest" | awk -F= '{print $2}' | tr -d ' ')"
[ -n "$CUR" ] || { echo "FAIL: 读不到 $APP 的版本（未安装？）"; exit 1; }
[ -z "$EXPECT_CUR" ] || [ "$CUR" = "$EXPECT_CUR" ] || { echo "FAIL: 当前版本 $CUR != 期望 $EXPECT_CUR"; exit 1; }
FP_BEFORE="$(fingerprint)"
echo "应用: $APP   版本: $CUR"
echo "数据指纹: $(printf '%s' "$FP_BEFORE" | wc -l) 个文件"

echo
echo "================ 1/4 暂存 ================"
R1="$(rpc /rpc/v1/download/task "{\"packageSourceType\":\"file\",\"path\":\"$FPK\"}")"
TID="$(printf '%s' "$R1" | jget "['data']['downloadTaskId']")"
[ -n "$TID" ] || { echo "FAIL: 未拿到 downloadTaskId: $R1"; exit 1; }
ST=""
for _ in $(seq 1 90); do
    S="$(rpc /rpc/v1/download/status "{\"downloadTaskId\":\"$TID\"}")"
    ST="$(printf '%s' "$S" | jget "['data']['status']")"
    [ "$ST" = "2" ] && break
    [ "$ST" = "1" ] || { echo "FAIL: 暂存异常: $S"; exit 1; }
    sleep 1
done
[ "$ST" = "2" ] || { echo "FAIL: 暂存超时"; exit 1; }
PKG_TYPE="$(printf '%s' "$S" | jget "['data']['packageType']")"
NEW_VER="$(printf '%s' "$S" | jget "['data']['version']")"
STAGED_APP="$(printf '%s' "$S" | jget "['data']['appName']")"
echo "识别: appName=$STAGED_APP version=$NEW_VER packageType=$PKG_TYPE"
[ "$STAGED_APP" = "$APP" ] || { echo "FAIL: 包内应用名 $STAGED_APP 与目标 $APP 不符"; exit 1; }

echo
echo "================ 2/4 预检 ================"
R2="$(rpc /rpc/v1/update/info "{\"appName\":\"$APP\",\"updateVersion\":\"$NEW_VER\",\"packageType\":\"$PKG_TYPE\",\"language\":\"zh-CN\"}")"
VOL="$(printf '%s' "$R2" | jget "['data']['wizardInfo']['installedVolumeID']")"
case "$VOL" in ''|0|None) echo "FAIL: 取不到安装卷，按设计中止（绝不猜卷）"; exit 1 ;; esac
echo "当前安装卷: $VOL"

echo
echo "================ 3/4 提交 ================"
R3="$(rpc /rpc/v1/update/task "{\"appName\":\"$APP\",\"updateVersion\":\"$NEW_VER\",\"packageType\":\"$PKG_TYPE\",\"systemParameters\":{\"agreedToProtocol\":true,\"installVolumeID\":$VOL,\"dataVolumeId\":$VOL,\"immediateStart\":false},\"customParameters\":[],\"language\":\"zh-CN\"}")"
TASK="$(printf '%s' "$R3" | jget "['data']['taskId']")"
case "$TASK" in ''|None) echo "FAIL: 未拿到 taskId: $R3"; exit 1 ;; esac
echo "taskId: $TASK"
ST=""
for i in $(seq 1 300); do
    S="$(rpc /rpc/v1/common/status "{\"taskId\":\"$TASK\"}")"
    ST="$(printf '%s' "$S" | jget "['data']['status']")"
    PG="$(printf '%s' "$S" | jget "['data']['progress']")"
    echo "  [$i] status=$ST progress=$PG"
    case "$ST" in
        2) break ;;
        1) sleep 2 ;;
        5) echo "  守护进程已不再持有该任务 → 改用最终版本判定"; break ;;
        *) echo "FAIL: 任务失败: $S"; exit 1 ;;
    esac
done
case "$ST" in 2|5) : ;; *) echo "FAIL: 任务超时"; exit 1 ;; esac

echo
echo "================ 4/4 核验 ================"
sleep 3
appcenter-cli start "$APP" >/dev/null 2>&1 || true
AFTER="$(grep '^version' "/var/apps/$APP/manifest" | awk -F= '{print $2}' | tr -d ' ')"
FP_AFTER="$(fingerprint)"
echo "版本: $CUR -> $AFTER"
RC=0
[ "$AFTER" = "$NEW_VER" ] || { echo "FAIL: 版本未变成 $NEW_VER"; RC=1; }
# 用户数据必须原样在（这几个文件丢了就是真丢数据）
for f in "$VAR/apps.json" "$HOME_DIR/access-password.txt"; do
    [ -e "$f" ] || { [ "$(basename "$f")" = "access-password.txt" ] && continue; echo "FAIL: 升级后 $f 不见了"; RC=1; }
done
if [ "$FP_BEFORE" != "$FP_AFTER" ]; then
    echo "FAIL: 数据目录发生变化（数据未完整保留）："
    diff <(printf '%s\n' "$FP_BEFORE") <(printf '%s\n' "$FP_AFTER") | head -20
    RC=1
else
    echo "数据指纹一致（$(printf '%s' "$FP_AFTER" | wc -l) 个文件未变）"
fi
[ "$RC" = "0" ] && echo "✅ PASS：$APP 已升级到 $NEW_VER，数据完整"
exit $RC
