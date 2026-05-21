#!/usr/bin/env bash
# task-completed-gate.sh
#
# TaskCompleted hook for agent-org plugin (v0.7.0+).
#
# 公式 schema (https://code.claude.com/docs/en/hooks.md, TaskCompleted hook):
#   入力 JSON は全 field が top-level フラット。task 関連 field:
#     - task_id          (string)
#     - task_subject     (string)
#     - task_description (string, optional)
#   common:
#     - session_id / transcript_path / cwd / permission_mode / hook_event_name
#
# v0.7.0 から approval は bd issue (type=approval) として管理。
# label `task:<task_id>` で approval を紐付け、priority=0 を rejected として
# 検出する:
#
#   bd list -l "task:${task_id}" -t approval --status open --json \
#     | jq '[.[] | select(.priority==0)] | length'
#
# opt-in 設計 (v0.6.0 と同じ): task に approval 1 件も無ければ通常 task 扱いで pass。
# /run-review を回した task のみ gate される。
#
# 動作:
#   - bd CLI / jq 不在で fail-open
#   - task_id 不在 (公式 schema 違反) で fail-open
#   - BEADS_DIR 解決不能で fail-open
#   - approval 0 件 → pass (opt-in)
#   - rejected (priority=0 かつ open) > 0 → exit 2 で block
#   - rejected == 0 だが conditional (priority=1) が残存 → pass + warn
#   - all approved (closed or priority>=2 open) → pass
#
# 依存: bd CLI, jq, python3

set -euo pipefail

# fail-open: hook が壊れても task 完了を妨げない (ただし明示的 exit 2 は通す)
trap 'exit 0' ERR

INPUT="$(cat)"

# 必須コマンド不在で fail-open
command -v jq >/dev/null 2>&1 || exit 0
command -v bd >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

# task_id は top-level フラット (公式 schema)
task_id="$(printf '%s' "$INPUT" | jq -r '.task_id // empty' 2>/dev/null || true)"
cwd="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"

[ -z "$cwd" ] && cwd="$(pwd)"
[ -z "$task_id" ] && exit 0

# BEADS_DIR を cwd ベースで解決
proj_hash="$(python3 -c "
import hashlib, os, sys
try:
    print(hashlib.sha256(os.path.realpath('$cwd').encode()).hexdigest()[:8])
except Exception:
    sys.exit(1)
" 2>/dev/null || true)"
[ -z "$proj_hash" ] && exit 0

beads_dir="$HOME/.beads/$proj_hash/.beads"
[ -d "$beads_dir" ] || exit 0

# rejected approval (priority=0 かつ open) の件数を取得
rejected_count="$(BEADS_DIR="$beads_dir" bd list -l "task:${task_id}" -t approval --status open --json 2>/dev/null \
  | jq '[.[] | select(.priority==0)] | length' 2>/dev/null || echo 0)"
[ -z "$rejected_count" ] && rejected_count=0

if [ "$rejected_count" -gt 0 ]; then
  cat >&2 <<EOF
[agent-org:task-completed-gate] BLOCK: rejected approval が ${rejected_count} 件残存

  task_id:    ${task_id}
  BEADS_DIR:  ${beads_dir}

reviewer が request_changes / reject (priority=0) を出しています。
concern を解消してから下記で再レビューを実行し、approved または conditional に
なるまで完了できません:

  /run-review ${task_id}

詳細を確認するには:
  BEADS_DIR="${beads_dir}" bd list -l "task:${task_id}" -t approval --status open
  BEADS_DIR="${beads_dir}" bd show <approval-id>
EOF
  exit 2
fi

# conditional (priority=1) が残っているなら warn
conditional_count="$(BEADS_DIR="$beads_dir" bd list -l "task:${task_id}" -t approval --status open --json 2>/dev/null \
  | jq '[.[] | select(.priority==1)] | length' 2>/dev/null || echo 0)"
[ -z "$conditional_count" ] && conditional_count=0

if [ "$conditional_count" -gt 0 ]; then
  echo "[agent-org:task-completed-gate] PASS (conditional): ${task_id} — ${conditional_count} 件の conditional approval が残存。BEADS_DIR=${beads_dir} bd list -l \"task:${task_id}\" -t approval --status open で確認" >&2
fi

exit 0
