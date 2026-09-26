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

# --- verdict ---------------------------------------------------------------
# 判定は「最新の @codex review 以降」の Codex の応答だけで行う。PR body の 👍 は前の
# サイクルのものが残り続ける (同じ人の同じ reaction は 1 つしか付かない) ため、再レビュー中に
# 古い 👍 を見て PASSED と誤判定しないようにする。trigger が無い (初回の自動レビュー) ときは
# PR 作成以降が対象。
# trigger の後に commit を push した場合、それまでの応答は古い head へのものなので STALE と出す
# (最新の head に対する Codex の review があれば別)。依頼時の head は pr-codex-trigger.sh が
# サマリに書き添える `codex-review-head` で知る。GitHub の API からは push の時刻が取れないため、
# 書き添えが無いときだけ commit の日時で代用する (前に作った commit を後から push すると見逃す)。
# 他の bot (CI・Dependabot など) の応答を拾わないよう、Codex の connector に絞る。
CODEX_BOT="${CODEX_BOT_LOGIN:-chatgpt-codex-connector[bot]}"
LAST_TRIGGER_AT=$(
  gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
    --jq '.[] | select(.body == "@codex review") | .created_at' | tail -1
)
PR_INFO=$(gh api "repos/$REPO/pulls/$PR" --jq '"\(.created_at)\t\(.head.sha)"')
PR_CREATED_AT="${PR_INFO%%$'\t'*}"
HEAD_SHA="${PR_INFO#*$'\t'}"
HEAD_AT=$(gh api "repos/$REPO/commits/$HEAD_SHA" --jq '.commit.committer.date' 2>/dev/null || true)
T="${LAST_TRIGGER_AT:-$PR_CREATED_AT}"
BY_BOT="select(.user.login == \"$CODEX_BOT\")"

# 利用上限・内部エラー・環境未設定は review ではなく issue comment で届き、reaction も付かない。
# trigger と同じ秒に届くことがあるので、trigger より後かは comment の id (単調増加) で見る
if [[ -n "$LAST_TRIGGER_ID" ]]; then
  AFTER_TRIGGER="select(.id > $LAST_TRIGGER_ID)"
else
  AFTER_TRIGGER="select(.created_at > \"$T\")"
fi
LAST_BOT_COMMENT=$(
  gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
    --jq ".[] | $BY_BOT | $AFTER_TRIGGER | \"\\(.created_at)\\t\\(.body | gsub(\"[\\r\\n]+\"; \" \") | .[0:200])\"" \
    | tail -1
)
BOT_ERROR=""
if [[ -n "$LAST_BOT_COMMENT" ]]; then
  echo "[last Codex issue comment] $LAST_BOT_COMMENT"
  if grep -qiE 'something went wrong|unknown error|usage limit|rate limit|reached your|try again later|create an environment' <<<"$LAST_BOT_COMMENT"; then
    BOT_ERROR="${LAST_BOT_COMMENT#*$'\t'}"
  fi
fi
REVIEWS_AFTER=$(
  gh api --paginate "repos/$REPO/pulls/$PR/reviews?per_page=100" \
    --jq ".[] | $BY_BOT | select(.submitted_at > \"$T\") | .id" | grep -c . || true
)
REVIEWS_ON_HEAD=$(
  gh api --paginate "repos/$REPO/pulls/$PR/reviews?per_page=100" \
    --jq ".[] | $BY_BOT | select(.commit_id == \"$HEAD_SHA\") | .id" | grep -c . || true
)
REQUESTED_HEAD=""
if [[ -n "$LAST_TRIGGER_ID" ]]; then
  REQUESTED_HEAD=$(
    gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" \
      --jq ".[] | select(.id < $LAST_TRIGGER_ID) | .body | capture(\"codex-review-head: (?<sha>[0-9a-f]{7,40})\") | .sha" \
      | tail -1
  )
fi
STALE=""
if [[ "${REVIEWS_ON_HEAD:-0}" -eq 0 ]]; then
  if [[ -n "$REQUESTED_HEAD" ]]; then
    [[ "$REQUESTED_HEAD" != "$HEAD_SHA" ]] && STALE="依頼時 ${REQUESTED_HEAD:0:12} → 現在 ${HEAD_SHA:0:12}"
  elif [[ -n "$HEAD_AT" && "$HEAD_AT" > "$T" ]]; then
    STALE="commit 日時 $HEAD_AT"
  fi
fi
# 👍 / 👀 は PR body・issue comment (trigger を含む)・review comment のどれにも付きうる。
# どの surface でも T 以降に付いたものだけ数える。trigger コメント自体への reaction は必ず
# trigger より後なので、同じ秒 (created_at は秒単位) でも数える
REACT_JQ=".[] | $BY_BOT | select(.created_at > \"$T\") | .content"
REACT_JQ_TRIGGER=".[] | $BY_BOT | .content"
REACTS_AFTER=$(
  {
    gh api --paginate "repos/$REPO/issues/$PR/reactions?per_page=100" --jq "$REACT_JQ" || true
    gh api --paginate "repos/$REPO/issues/$PR/comments?per_page=100" --jq '.[] | .id' 2>/dev/null \
      | while read -r CID; do
          if [[ "$CID" == "$LAST_TRIGGER_ID" ]]; then Q="$REACT_JQ_TRIGGER"; else Q="$REACT_JQ"; fi
          gh api --paginate "repos/$REPO/issues/comments/$CID/reactions?per_page=100" --jq "$Q" || true
        done
    gh api --paginate "repos/$REPO/pulls/$PR/comments?per_page=100" --jq '.[] | .id' 2>/dev/null \
      | while read -r CID; do
          gh api --paginate "repos/$REPO/pulls/comments/$CID/reactions?per_page=100" --jq "$REACT_JQ" || true
        done
  } | sort -u | tr '\n' ' '
)

echo
echo "=== reactions summary ==="
ALL_REACTIONS=$(gh api "repos/$REPO/issues/$PR/reactions" --jq '[.[] | .content] | group_by(.) | map("\(.[0]) x\(length)") | join(", ")' 2>/dev/null || true)
echo "PR body: ${ALL_REACTIONS:-none}"
echo "latest cycle: since ${LAST_TRIGGER_AT:-(PR 作成)} / Codex reviews=${REVIEWS_AFTER:-0} / reactions=${REACTS_AFTER:-none}"
if [[ -n "$BOT_ERROR" ]]; then
  echo "Codex verdict: ERROR (最新の @codex review 以降に Codex がエラーを返した。内容を確認して再 trigger する)"
  echo "  $BOT_ERROR"
elif [[ -n "$STALE" ]]; then
  echo "Codex verdict: STALE (最新の review 依頼より後に head が更新された: ${STALE}。pr-codex-trigger.sh で再 trigger する)"
elif [[ "${REVIEWS_AFTER:-0}" -gt 0 ]]; then
  echo "Codex verdict: REVIEWED (最新サイクルの review あり。inline 指摘を確認する)"
elif [[ " $REACTS_AFTER " == *" +1 "* ]]; then
  echo "Codex verdict: PASSED (👍 = レビュー完了・問題なし)"
elif [[ " $REACTS_AFTER " == *" eyes "* ]]; then
  echo "Codex verdict: PROCESSING (👀 = レビュー受理・処理中、結果待ち)"
else
  echo "Codex verdict: NO REACTION (最新サイクルで未着手 or 未設定)"
fi

echo
echo "=== PR meta ==="
gh pr view "$PR" --json state,mergeable,mergeStateStatus,reviewDecision \
  --jq '"state=\(.state) mergeable=\(.mergeable) mergeStateStatus=\(.mergeStateStatus) reviewDecision=\(.reviewDecision // "(none)")"'

echo
echo "=== checks ==="
gh pr checks "$PR" 2>&1 || true
