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
#   サマリには、依頼した時点の head の SHA を HTML コメント (`<!-- codex-review-head: <sha> -->`)
#   で書き添える。pr-codex-status.sh はこれと現在の head を比べ、依頼の後に push した head を
#   STALE と出す (GitHub の API からは push の時刻が取れないため)。サマリが無いときは SHA だけの
#   短いコメントを投稿する。
#
# 依存: gh
set -euo pipefail

PR="${1:?Usage: pr-codex-trigger.sh <PR_NUMBER> [SUMMARY_TEXT_OR_FILE]}"
SUMMARY="${2:-}"

HEAD_SHA=$(gh pr view "$PR" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)
MARKER=""
if [[ -n "$HEAD_SHA" ]]; then
  MARKER="<!-- codex-review-head: $HEAD_SHA -->"
fi

if [[ -n "$SUMMARY" ]]; then
  if [[ -f "$SUMMARY" ]]; then
    BODY=$(cat "$SUMMARY")
  else
    BODY="$SUMMARY"
  fi
  if [[ -n "$MARKER" ]]; then
    BODY="$BODY"$'\n\n'"$MARKER"
  fi
  gh pr comment "$PR" --body "$BODY"
elif [[ -n "$MARKER" ]]; then
  gh pr comment "$PR" --body "レビュー対象の head: \`${HEAD_SHA:0:12}\`"$'\n'"$MARKER"
fi

# @codex review は単独コメント (本文に他の内容を入れない)
gh pr comment "$PR" --body "@codex review"

echo "Triggered Codex review on PR #$PR"
