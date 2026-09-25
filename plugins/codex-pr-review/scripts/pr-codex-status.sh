#!/usr/bin/env bash
# PR の Codex レビュー状態を 1 コマンドで集計表示する。
#
# Usage:
#   pr-codex-status.sh <PR_NUMBER> [SINCE_ISO8601]
#
# 出力:
#   - reviews 一覧 (state, commit_id, submitted_at)
#   - inline comments (オプションで SINCE 以降のみ)
#   - リアクション要約 (issue body / 最新 @codex review コメント)
#   - PR mergeability + check status
#
# 依存: gh, jq (gh の --jq で代替)
#
# 罠: gh api は既定で 1 ページ (30 件) しか返さず、切られるのは常に最新 =
#     今判定すべき応答。往復の多い PR で「指摘ゼロ」を誤検出する。--paginate 必須。
#     集約 jq (last/length) は 100 件までしか正しくない (--paginate は per_page を
#     100 に自動指定するため)。101 件目から静かに壊れるので、集約は shell 側で行う。
set -euo pipefail

PR="${1:?Usage: pr-codex-status.sh <PR_NUMBER> [SINCE_ISO8601]}"
SINCE="${2:-}"

REPO=$(gh repo view --json owner,name --jq '"\(.owner.login)/\(.name)"')

echo "=== reviews (PR #$PR) ==="
gh api --paginate "repos/$REPO/pulls/$PR/reviews?per_page=100" \
  --jq '.[] | "\(.submitted_at) state=\(.state) commit=\(.commit_id[:10])"'

echo
echo "=== inline comments ${SINCE:+(since $SINCE)}==="
JQ='.[]'
if [[ -n "$SINCE" ]]; then
  JQ="$JQ | select(.created_at >= \"$SINCE\")"
fi
JQ="$JQ | \"\\n--- \\(.created_at) commit=\\(.commit_id[:10]) path=\\(.path):\\(.line) ---\\n\\(.body)\""
gh api --paginate "repos/$REPO/pulls/$PR/comments?per_page=100" --jq "$JQ"

echo
echo "=== reactions ==="
echo "[PR body]"
gh api --paginate "repos/$REPO/issues/$PR/reactions?per_page=100" \
  --jq '.[] | "  \(.user.login): \(.content) @ \(.created_at)"' || true

echo "[issue comments]"
gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
  --jq '.[] | select(.body != "@codex review") | .id' 2>/dev/null | while read -r CID; do
  REACTIONS=$(gh api "repos/$REPO/issues/comments/$CID/reactions" --jq 'length' 2>/dev/null || echo 0)
  if [[ "$REACTIONS" -gt 0 ]]; then
    echo "  comment $CID:"
    gh api "repos/$REPO/issues/comments/$CID/reactions" \
      --jq '.[] | "    \(.user.login): \(.content) @ \(.created_at)"' || true
  fi
done

echo "[review comments]"
gh api --paginate "repos/$REPO/pulls/$PR/comments?per_page=100" \
  --jq '.[] | .id' 2>/dev/null | while read -r CID; do
  REACTIONS=$(gh api "repos/$REPO/pulls/comments/$CID/reactions" --jq 'length' 2>/dev/null || echo 0)
  if [[ "$REACTIONS" -gt 0 ]]; then
    echo "  review comment $CID:"
    gh api "repos/$REPO/pulls/comments/$CID/reactions" \
      --jq '.[] | "    \(.user.login): \(.content) @ \(.created_at)"' || true
  fi
done

# 最新の @codex review コメントの reaction (Codex が処理開始したか確認)
# 集約 (last) は shell 側で行う (--paginate はページごとに jq を適用するため)
LAST_TRIGGER_ID=$(
  gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
    --jq '.[] | select(.body == "@codex review") | .id' | tail -1
)
if [[ -n "$LAST_TRIGGER_ID" ]]; then
  echo "[last @codex review comment $LAST_TRIGGER_ID]"
  gh api "repos/$REPO/issues/comments/$LAST_TRIGGER_ID/reactions" \
    --jq '.[] | "  \(.user.login): \(.content) @ \(.created_at)"' || true
fi

# Codex bot の issue comment。利用上限・内部エラーは review ではなく issue comment で届き、
# reaction も付かないため、ここを見ないと「NO REACTION」のまま待ち続けることになる。
LAST_BOT_COMMENT=$(
  gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
    --jq '.[] | select(.user.login | endswith("[bot]")) | "\(.created_at)\t\(.body | gsub("[\r\n]+"; " ") | .[0:200])"' \
    | tail -1
)
LAST_TRIGGER_AT=$(
  gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
    --jq '.[] | select(.body == "@codex review") | .created_at' | tail -1
)
BOT_ERROR=""
if [[ -n "$LAST_BOT_COMMENT" ]]; then
  echo "[last bot issue comment] $LAST_BOT_COMMENT"
  BOT_AT="${LAST_BOT_COMMENT%%$'\t'*}"
  if [[ -z "$LAST_TRIGGER_AT" || "$BOT_AT" > "$LAST_TRIGGER_AT" ]] \
    && grep -qiE 'something went wrong|usage limit|rate limit|reached your|try again later' <<<"$LAST_BOT_COMMENT"; then
    BOT_ERROR="${LAST_BOT_COMMENT#*$'\t'}"
  fi
fi

echo
echo "=== reactions summary ==="
ALL_REACTIONS=$(gh api "repos/$REPO/issues/$PR/reactions" --jq '[.[] | .content] | group_by(.) | map("\(.[0]) x\(length)") | join(", ")' 2>/dev/null || true)
echo "PR body: ${ALL_REACTIONS:-none}"
# Codex bot の reaction から verdict を導出
CODEX_REACTION=$(gh api "repos/$REPO/issues/$PR/reactions" \
  --jq '[.[] | select(.user.login | endswith("[bot]")) | .content] | last // empty' 2>/dev/null || true)
if [[ -n "$BOT_ERROR" ]]; then
  echo "Codex verdict: ERROR (最新の @codex review 以降に bot がエラーを返した。内容を確認して再 trigger する)"
  echo "  $BOT_ERROR"
else
case "$CODEX_REACTION" in
  "+1")     echo "Codex verdict: PASSED (👍 = レビュー完了・問題なし)" ;;
  "eyes")   echo "Codex verdict: PROCESSING (👀 = レビュー受理・処理中、結果待ち)" ;;
  "")       echo "Codex verdict: NO REACTION (未着手 or 未設定)" ;;
  *)        echo "Codex verdict: $CODEX_REACTION (unknown)" ;;
esac
fi

echo
echo "=== PR meta ==="
gh pr view "$PR" --json state,mergeable,mergeStateStatus,reviewDecision \
  --jq '"state=\(.state) mergeable=\(.mergeable) mergeStateStatus=\(.mergeStateStatus) reviewDecision=\(.reviewDecision // "(none)")"'

echo
echo "=== checks ==="
gh pr checks "$PR" 2>&1 || true
