#!/usr/bin/env bash
# =========================================================================
# git_sync.sh — 把代码 / 配置 / 数据包同步到 GitHub（无 Git LFS 依赖）
#
# 用法:
#   scripts/git_sync.sh                # 提交并推送（代码 + data_pack 数据包）
#   scripts/git_sync.sh --pack         # 先重新生成 data_pack（数据有更新时用）
#   scripts/git_sync.sh --code-only    # 只提交代码与配置（不含数据包）
#   scripts/git_sync.sh -m "message"   # 自定义 commit message
#
# 安全说明:
#   - 不会在代码里保存任何 GitHub Token；
#   - 认证来自本地 git credential（或运行时环境变量），本脚本不做任何凭据写入。
# =========================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

MSG="chore: sync code & data_pack $(date -u '+%Y-%m-%d %H:%M UTC')"
CODE_ONLY=0
DO_PACK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --code-only) CODE_ONLY=1; shift ;;
    --pack) DO_PACK=1; shift ;;
    -m) MSG="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

if [[ "$DO_PACK" == "1" ]]; then
  echo "[pack] regenerating data_pack/ ..."
  python3 scripts/pack_data.py
fi

if [[ "$CODE_ONLY" == "1" ]]; then
  git add -- . ':(exclude)data_pack' ':(exclude)data' ':(exclude)data/scanner.duckdb'
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
