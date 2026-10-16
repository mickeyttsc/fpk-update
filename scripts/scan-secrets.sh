#!/usr/bin/env bash
# 敏感信息 / 私人环境门禁 —— 这个仓库是**公开**的，凭据或内网信息一旦进历史就撤不回来。
# 打包是全自动的，不能指望每次有人手工过一眼，所以放在 CI 最前面，命中就 fail。
#
# 用法：bash scripts/scan-secrets.sh
# 只扫「会被提交的文件」——已构建的产物（dist/）本身另有门禁，不在这里重复。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail() { echo "::error::$*"; exit 1; }

# 取要扫的文件清单：优先 git（尊重 .gitignore），不在 git 仓库里就退回到遍历
if git rev-parse --git-dir > /dev/null 2>&1; then
    mapfile -t FILES < <(git ls-files)
else
    mapfile -t FILES < <(find . -type f \
        -not -path './.git/*' -not -path './dist*/*' \
        -not -path '*/__pycache__/*' -not -name '*.fpk' | sed 's|^\./||')
fi
[ "${#FILES[@]}" -gt 0 ] || fail "没有可扫描的文件（脚本自身出问题了）"

echo "▸ 扫描 ${#FILES[@]} 个文件"

# 逐类扫描。每条都写成「命中即列出文件与行号再 fail」，方便定位。
scan() {
    local label="$1" pattern="$2" hits
    hits="$(grep -nEI "$pattern" "${FILES[@]}" 2>/dev/null || true)"
    if [ -n "$hits" ]; then
        echo "$hits" | head -20
        fail "$label"
    fi
    echo "  ✓ 无 $label"
}

# ① 各类令牌 / 私钥
scan "GitHub 令牌字面量" 'ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}'
scan "私钥文件头" 'BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY'
scan "常见凭据赋值（password/secret/token = 实际值）" \
     '(password|passwd|secret|api[_-]?key|access[_-]?token)[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"']{6,}["'"'"']'

# ② 内网地址与个人域名
scan "内网 IP（192.168/10./172.16-31）" \
     '\b(192\.168\.[0-9]{1,3}\.|10\.[0-9]{1,3}\.[0-9]{1,3}\.|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.)'
scan "个人域名 / 内网域名" 'mttao\.cn|[a-z0-9-]+\.(local|lan|internal)\b'

# ③ 个人存储路径（fnOS 的用户存储是 /vol<N>/<uid>/…，uid 属私人信息）
scan "个人存储路径 /volN/<uid>/" '/vol[0-9]+/[0-9]{3,}/'

echo "✅ 敏感信息扫描通过：没有凭据、内网地址或私人路径"
