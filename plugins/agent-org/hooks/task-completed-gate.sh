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
# v0.8.0 (ADR-007) で bd は <repo>/.beads/ に repo-local 配置。
# label `task:<task_id>` で approval を紐付け、priority=0 を rejected として
# 検出する:
#
#   (cd "$repo_root" && bd list -l "task:${task_id}" -t approval --status open --json) \
#     | jq '[.[] | select(.priority==0)] | length'
#
# opt-in 設計 (v0.6.0 と同じ): task に approval 1 件も無ければ通常 task 扱いで pass。
# /run-review を回した task のみ gate される。
#
# 動作:
#   - bd CLI / jq 不在で fail-open
#   - task_id 不在 (公式 schema 違反) で fail-open
#   - cwd が git repo 外 / <repo>/.beads/ 不在で fail-open
#   - approval 0 件 → pass (opt-in)
#   - rejected (priority=0 かつ open) > 0 → exit 2 で block
#   - rejected == 0 だが conditional (priority=1) が残存 → pass + warn
#   - all approved (closed or priority>=2 open) → pass
#
# 依存: bd CLI, jq

set -euo pipefail

# fail-open: hook が壊れても task 完了を妨げない (ただし明示的 exit 2 は通す)
trap 'exit 0' ERR

INPUT="$(cat)"

# 必須コマンド不在で fail-open
command -v jq >/dev/null 2>&1 || exit 0
command -v bd >/dev/null 2>&1 || exit 0

# task_id は top-level フラット (公式 schema)
task_id="$(printf '%s' "$INPUT" | jq -r '.task_id // empty' 2>/dev/null || true)"
cwd="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"

[ -z "$cwd" ] && cwd="$(pwd)"
[ -z "$task_id" ] && exit 0

# v0.8.0: bd は <repo>/.beads/ に配置 (ADR-007)
# --bg 隔離下では cwd が worktree path (`.claude/worktrees/<id>/`) なので
# git rev-parse --show-toplevel が worktree root を返す。bd は worktree-aware
# で main repo `.beads/` を共有するため、git common-dir 経由で main_repo を解決
repo_root="$(cd "$cwd" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || echo "")"
[ -n "$repo_root" ] || exit 0

main_repo="$(cd "$cwd" 2>/dev/null && cd "$(dirname "$(git rev-parse --git-common-dir 2>/dev/null)")" 2>/dev/null && pwd -P)"
[ -n "$main_repo" ] || main_repo="$repo_root"

beads_dir="$main_repo/.beads"
[ -d "$beads_dir" ] || exit 0

# rejected approval (priority=0 かつ open) の件数を取得
# cd "$repo_root" で bd 自動 resolve (worktree でも main repo .beads/ にアクセス、ADR-007)
rejected_count="$(cd "$repo_root" && bd list -l "task:${task_id}" -t approval --status open --json 2>/dev/null \
  | jq '[.[] | select(.priority==0)] | length' 2>/dev/null || echo 0)"
[ -z "$rejected_count" ] && rejected_count=0

if [ "$rejected_count" -gt 0 ]; then
  cat >&2 <<EOF
[agent-org:task-completed-gate] BLOCK: rejected approval が ${rejected_count} 件残存

  task_id:    ${task_id}
  invoked_from: ${repo_root}
  main_repo:    ${main_repo}
  beads_dir:    ${beads_dir}

reviewer が request_changes / reject (priority=0) を出しています。
concern を解消してから下記で再レビューを実行し、approved または conditional に
なるまで完了できません:

  /run-review ${task_id}

詳細を確認するには:
  (cd "${main_repo}" && bd list -l "task:${task_id}" -t approval --status open)
  (cd "${main_repo}" && bd show <approval-id>)
EOF
  exit 2
fi

# conditional (priority=1) が残っているなら warn
conditional_count="$(cd "$repo_root" && bd list -l "task:${task_id}" -t approval --status open --json 2>/dev/null \
  | jq '[.[] | select(.priority==1)] | length' 2>/dev/null || echo 0)"
[ -z "$conditional_count" ] && conditional_count=0

if [ "$conditional_count" -gt 0 ]; then
  echo "[agent-org:task-completed-gate] PASS (conditional): ${task_id} — ${conditional_count} 件の conditional approval が残存。(cd ${main_repo} && bd list -l \"task:${task_id}\" -t approval --status open) で確認" >&2
fi

exit 0
