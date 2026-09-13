#!/usr/bin/env bash
# =========================================================================
# git_sync.sh — 把代码 / 配置 / 数据同步到 GitHub
#
# 用法:
#   scripts/git_sync.sh                # 提交并推送（含 parquet 数据，走 LFS）
#   scripts/git_sync.sh --code-only    # 只提交代码与配置（不含 data/ 原始数据）
#   scripts/git_sync.sh -m "message"   # 自定义 commit message
#
# 安全说明:
#   - 不会在代码里保存任何 GitHub Token；
#   - 认证来自本地 git credential（或运行时环境变量），本脚本不做任何凭据写入。
# =========================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

MSG="chore: sync data & code $(date -u '+%Y-%m-%d %H:%M UTC')"
CODE_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --code-only) CODE_ONLY=1; shift ;;
    -m) MSG="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

# Git LFS（数据文件走 LFS，如可用）
if command -v git-lfs >/dev/null 2>&1; then
  git lfs install --local >/dev/null 2>&1 || true
  echo "[lfs] enabled"
else
  echo "[warn] git-lfs not found; large parquet may bloat the repo"
fi

if [[ "$CODE_ONLY" == "1" ]]; then
  git add -- . ':(exclude)data/raw' ':(exclude)data/scanner.duckdb'
else
  git add -A
fi

if git diff --cached --quiet; then
  echo "[git] nothing to commit"
else
  git commit -m "$MSG"
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git push origin "$BRANCH"
echo "[git] pushed to origin/$BRANCH"
