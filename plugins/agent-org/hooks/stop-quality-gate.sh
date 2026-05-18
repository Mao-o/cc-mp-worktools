#!/usr/bin/env bash
# stop-quality-gate.sh
#
# Stop hook for agent-org plugin.
# .claude/agent-org/quality-gates.json 設定があれば各 gate を実行し、
# required gate が failing なら exit 2 で stop を block する。
#
# 入力 JSON (stdin):
#   - session_id, transcript_path, cwd, hook_event_name (common fields)
#   - stop_hook_active: true なら再入 (無限ループ回避のため即 exit 0)
#
# quality-gates.json schema (例):
#   {
#     "schema_version": "1",
#     "gates": [
#       {
#         "id": "tests-passing",
#         "description": "Unit tests must pass",
#         "kind": "command",
#         "command": "pytest -q",
#         "required": true
#       },
#       {
#         "id": "lint-clean",
#         "description": "Lint should be clean",
#         "kind": "command",
#         "command": "ruff check .",
#         "required": false
#       },
#       {
#         "id": "no-pending-rejections",
#         "description": "All approvals must be approved or conditional",
#         "kind": "approvals_clean",
#         "required": true
#       }
#     ]
#   }
#
# kind:
#   - "command" (default): command を eval、exit code 0 ならパス
#   - "approvals_clean": .claude/agent-org/approvals/*.json に
#     approval_status=rejected が無ければパス
#
# 動作:
#   1. jq 不在 / config 不在で fail-open (exit 0)
#   2. stop_hook_active=true で抜ける (再入回避)
#   3. 各 gate を実行、required=true の failing は collect
#   4. required=false の failing は warn のみ
#   5. failing があれば exit 2 (block)、無ければ exit 0
#
# 依存: jq

set -euo pipefail

# fail-open: hook が壊れても stop を妨げない (ただし明示的 exit 2 は通す)
trap 'exit 0' ERR

INPUT="$(cat)"

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

stop_hook_active="$(printf '%s' "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null || echo "false")"
cwd="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"

if [ -z "$cwd" ]; then
  cwd="$(pwd)"
fi

# 再入なら抜ける
if [ "$stop_hook_active" = "true" ]; then
  exit 0
fi

config_file="${cwd}/.claude/agent-org/quality-gates.json"

# 設定が無ければ gate 制約無し
if [ ! -f "$config_file" ]; then
  exit 0
fi

gate_count="$(jq '.gates | length' "$config_file" 2>/dev/null || echo "0")"

if [ "$gate_count" = "0" ] || [ "$gate_count" = "null" ]; then
  exit 0
fi

failures=()
warnings=()

i=0
while [ "$i" -lt "$gate_count" ]; do
  gate_id="$(jq -r ".gates[$i].id // \"gate-$i\"" "$config_file" 2>/dev/null || echo "gate-$i")"
  gate_desc="$(jq -r ".gates[$i].description // \"\"" "$config_file" 2>/dev/null || echo "")"
  gate_kind="$(jq -r ".gates[$i].kind // \"command\"" "$config_file" 2>/dev/null || echo "command")"
  # required は boolean false を真の false として扱いたいため、`// true`
  # (alternative operator) を使うと false → true に化ける。has() で
  # field 有無を判定し、未指定の場合のみ default true を採用する
  gate_required="$(jq -r ".gates[$i] | if has(\"required\") then .required else true end" "$config_file" 2>/dev/null || echo "true")"

  result="pass"
  detail=""

  case "$gate_kind" in
    command)
      gate_cmd="$(jq -r ".gates[$i].command // empty" "$config_file" 2>/dev/null || echo "")"
      if [ -z "$gate_cmd" ]; then
        result="error"
        detail="command field が未定義"
      else
        if ! out="$(cd "$cwd" && eval "$gate_cmd" 2>&1)"; then
          result="fail"
          detail="$(printf '%s' "$out" | tail -10)"
        fi
      fi
      ;;
    approvals_clean)
      approvals_dir="${cwd}/.claude/agent-org/approvals"
      rejected_count=0
      if [ -d "$approvals_dir" ]; then
        # wc -l の出力は先頭に space + 改行を含む環境がある (macOS の wc 等)。
        # [:space:] で全空白除去しないと "0\n" の文字列比較で誤判定する
        rejected_count="$(grep -l '"approval_status"[[:space:]]*:[[:space:]]*"rejected"' "$approvals_dir"/*.json 2>/dev/null | wc -l | tr -d '[:space:]')"
        if [ -z "$rejected_count" ]; then
          rejected_count=0
        fi
      fi
      if [ "$rejected_count" != "0" ]; then
        result="fail"
        detail="${rejected_count} approval(s) in rejected state (${approvals_dir}/*.json)"
      fi
      ;;
    *)
      result="error"
      detail="unknown kind: ${gate_kind}"
      ;;
  esac

  if [ "$result" = "fail" ] || [ "$result" = "error" ]; then
    line="${gate_id}"
    if [ -n "$gate_desc" ]; then
      line="${line} (${gate_desc})"
    fi
    line="${line}: ${detail}"

    if [ "$gate_required" = "true" ]; then
      failures+=("$line")
    else
      warnings+=("$line")
    fi
  fi

  i=$((i + 1))
done

# 警告 (non-blocking)
if [ ${#warnings[@]} -gt 0 ]; then
  echo "[agent-org:stop-quality-gate] warnings (non-blocking):" >&2
  for w in "${warnings[@]}"; do
    echo "  - $w" >&2
  done
fi

# 失敗あり: block
if [ ${#failures[@]} -gt 0 ]; then
  {
    echo ""
    echo "[agent-org:stop-quality-gate] BLOCK: 以下の required quality gate が failing"
    echo ""
    for f in "${failures[@]}"; do
      echo "  - $f"
    done
    echo ""
    echo "設定ファイル: ${config_file}"
    echo "解消したら再度メインセッションを停止してください。"
  } >&2
  exit 2
fi

exit 0
