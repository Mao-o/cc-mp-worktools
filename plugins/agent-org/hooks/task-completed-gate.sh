#!/usr/bin/env bash
# task-completed-gate.sh
#
# TaskCompleted hook for agent-org plugin.
#
# 公式 schema (https://code.claude.com/docs/en/hooks.md, TaskCompleted hook):
#   入力 JSON は全 field が top-level フラット。`task` / `metadata` key は
#   存在しない。task 関連 field:
#     - task_id          (string)
#     - task_subject     (string)
#     - task_description (string, optional)
#     - teammate_name    (string, optional)
#     - team_name        (string, optional)
#   common:
#     - session_id / transcript_path / cwd / permission_mode / hook_event_name
#
# 公式 schema に `review_required` のような field は存在しないため、
# **approval JSON opt-in** で gate 判定する設計に倒す:
#   - .claude/agent-org/approvals/<task_id>.json が **不在** なら pass
#     (= 通常 task、review 不要)
#   - approval_status=approved / conditional なら pass
#   - approval_status=rejected なら exit 2 で block
#   - approval_status が不明な値 / 読み込み不能なら fail-open (exit 0)
#
# 動作:
#   - jq 不在で fail-open
#   - task_id 不在 (公式 schema 違反) で fail-open
#   - approval JSON 不在で pass (opt-in 設計)
#   - approval_status=rejected の時のみ exit 2 (block)
#
# TaskCompleted は matcher 非対応・全件発火なので、すべての task 完了で
# この hook が呼ばれる。だからこそ approval JSON 不在を「制約なし」と扱う。
#
# 依存: jq

set -euo pipefail

# fail-open: hook が壊れても task 完了を妨げない (ただし明示的 exit 2 は通す)
trap 'exit 0' ERR

INPUT="$(cat)"

# jq が無ければ gate skip (fail-open)
if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

# task_id は **top-level フラット** で渡る (公式 schema)
task_id="$(printf '%s' "$INPUT" | jq -r '.task_id // empty' 2>/dev/null || true)"

cwd="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"
if [ -z "$cwd" ]; then
  cwd="$(pwd)"
fi

# task_id が取れない (公式 schema 違反 / 古い payload 形式) なら fail-open
if [ -z "$task_id" ]; then
  exit 0
fi

approval_file="${cwd}/.claude/agent-org/approvals/${task_id}.json"

# approval JSON 不在 → 通常 task として pass (opt-in 設計)
# /run-review <task-id> を実行していない task は gate されない
if [ ! -f "$approval_file" ]; then
  exit 0
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
