#!/usr/bin/env bash
# 修正サマリ + @codex review トリガーを **別コメント** で投稿する。
#
# Usage:
#   pr-codex-trigger.sh <PR_NUMBER>
#   pr-codex-trigger.sh <PR_NUMBER> "サマリ本文"
#   pr-codex-trigger.sh <PR_NUMBER> /path/to/summary.md
#
# 設計理由:
#   Codex bot は @codex review メンションをトリガとして走り、コメント本文の自然言語は吸わない。
#   サマリと混ぜると review trigger が機能するか/しないかの境界が曖昧になるため、必ず別コメントで送る。
#
# 依存: gh
set -euo pipefail

PR="${1:?Usage: pr-codex-trigger.sh <PR_NUMBER> [SUMMARY_TEXT_OR_FILE]}"
SUMMARY="${2:-}"

if [[ -n "$SUMMARY" ]]; then
  if [[ -f "$SUMMARY" ]]; then
    gh pr comment "$PR" --body-file "$SUMMARY"
  else
    gh pr comment "$PR" --body "$SUMMARY"
  fi
fi

# @codex review は単独コメント (本文に他の内容を入れない)
gh pr comment "$PR" --body "@codex review"

echo "Triggered Codex review on PR #$PR"
