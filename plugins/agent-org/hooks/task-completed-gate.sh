#!/usr/bin/env bash
# task-completed-gate.sh
#
# TaskCompleted hook for agent-org plugin.
# matcher 非対応・全件発火のため hook 内部で task.metadata.review_required を
# 見て review-required task のみ approval JSON 検査を行う。
#
# 入力 JSON (stdin):
#   - session_id, transcript_path, cwd, hook_event_name (common fields)
#   - task object / metadata.review_required (推測)
#
# 動作:
#   1. jq 不在で fail-open (gate skip、exit 0)
#   2. task.metadata.review_required (or top-level metadata.review_required) が
#      true でなければ exit 0 (review-required でない普通の task)
#   3. task_id を取得 (task.id / task.task_id / id / task_id を順に試す)
#   4. .claude/agent-org/approvals/<task-id>.json が存在しない → exit 2 (block)
#   5. approval_status を読む:
#      - approved / conditional → exit 0 (pass)
#      - rejected → exit 2 (block)
#      - unknown → fail-open (exit 0)
#
# 依存: jq

set -euo pipefail

# fail-open: hook が壊れても task 完了を妨げない
# ただし明示的に exit 2 を返した場合はブロック
trap 'exit 0' ERR

INPUT="$(cat)"

# jq が無ければ gate skip (fail-open)
if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

# task_id を複数候補から取得
task_id="$(printf '%s' "$INPUT" | jq -r '
  .task.id // .task.task_id // .task.taskId // .id // .task_id // .taskId // empty
' 2>/dev/null || true)"

# review_required を複数候補から取得
review_required="$(printf '%s' "$INPUT" | jq -r '
  .task.metadata.review_required
  // .task.metadata.reviewRequired
  // .metadata.review_required
  // .metadata.reviewRequired
  // false
' 2>/dev/null || echo "false")"

cwd="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"
if [ -z "$cwd" ]; then
  cwd="$(pwd)"
fi

# review_required が true 以外ならゲート skip
if [ "$review_required" != "true" ]; then
  exit 0
fi

# review_required=true だが task_id が取れない → 警告して fail-open
if [ -z "$task_id" ]; then
  echo "[agent-org:task-completed-gate] warning: review_required=true but task id not found in input; skipping gate (fail-open)" >&2
  exit 0
fi

approval_file="${cwd}/.claude/agent-org/approvals/${task_id}.json"

if [ ! -f "$approval_file" ]; then
  cat >&2 <<EOF
[agent-org:task-completed-gate] BLOCK: approval が存在しません

  task_id:       ${task_id}
  expected file: ${approval_file}

このタスクは review_required=true としてマークされています。完了前に
レビューを実行してください:

  /run-review ${task_id}

approval JSON が生成されたら task を再度完了できます。
EOF
  exit 2
fi

approval_status="$(jq -r '.approval_status // "unknown"' "$approval_file" 2>/dev/null || echo "unknown")"

case "$approval_status" in
  approved)
    exit 0
    ;;
  conditional)
    echo "[agent-org:task-completed-gate] PASS (conditional): ${task_id} — concerns が残存しています。jq '.concerns_summary' ${approval_file} を確認してください" >&2
    exit 0
    ;;
  rejected)
    cat >&2 <<EOF
[agent-org:task-completed-gate] BLOCK: approval が rejected

  task_id:       ${task_id}
  approval file: ${approval_file}
  approval_status: rejected

reviewer が request_changes / reject を出しています。concern を解消してから
下記で再レビューを実行し、approved または conditional になるまで完了できません:

  /run-review ${task_id}

approval JSON の concerns_summary を確認するには:
  jq '.concerns_summary' "${approval_file}"
EOF
    exit 2
    ;;
  *)
    echo "[agent-org:task-completed-gate] warning: unknown approval_status='${approval_status}' in ${approval_file}; allowing complete (fail-open)" >&2
    exit 0
    ;;
esac
