#!/usr/bin/env bash
# post-commit-trigger.sh
#
# PostToolUse(Bash) hook for agent-org plugin.
# tool_input.command が `git commit` を含み、exit_code == 0 だった場合に
# ~/.claude/agent-org/state/<proj-hash>/last-commit.json を更新する。
# regression-watcher が次 /loop iteration で読んで、新規 commit 以降を
# smoke check する起点として使う。
#
# 入力 JSON (stdin) PostToolUse:
#   - common: session_id, transcript_path, cwd, hook_event_name
#   - tool_name (例: "Bash")
#   - tool_input.command (string)
#   - tool_response.exit_code (int) or .exitCode
#
# proj-hash:
#   - hook input の cwd を canonicalize (`cd && pwd -P`) して sha256 した
#     先頭 8 桁。$CLAUDE_PROJECT_DIR より hook input cwd の方が確実 (env が
#     未設定でも cwd は必ず渡る)
#
# 動作:
#   - jq 不在で fail-open
#   - tool_name が "Bash" 以外で skip
#   - command が `git commit` を含まなければ skip
#   - exit_code != 0 で skip (失敗 commit は記録しない)
#   - 成功 commit なら HEAD の sha / branch を取得し JSON 書込
#   - PostToolUse は decision 制御不要 (常に exit 0)

set -euo pipefail
trap 'exit 0' ERR

INPUT="$(cat)"

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

tool_name="$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)"

if [ "$tool_name" != "Bash" ]; then
  exit 0
fi

command_str="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || true)"

if [ -z "$command_str" ]; then
  exit 0
fi

# `git commit` を含む command を検出する。
# chained command (`a; git commit -m x`, `a && git commit -m x`, パイプ等) にも
# 対応するため、separator (`;`, `&`, `|`) を改行に置換した上で各行を独立に
# 検査する形に倒す (regex の separator + space の組合せ問題回避)。
# 各行で「`git` token の直後、または `git -C path` / `git --opt` 等のオプション
# を挟んだ後に `commit` token が来る」パターンを探す。
is_git_commit=0
if printf '%s' "$command_str" | tr ';|&' '\n' \
   | grep -qE '(^|[[:space:]])git([[:space:]]+(-[CcPp][[:space:]]+[^[:space:]]+|--?[a-zA-Z][^[:space:]]*(=[^[:space:]]*)?))*[[:space:]]+commit([[:space:]]|$)'; then
  is_git_commit=1
fi

if [ "$is_git_commit" != "1" ]; then
  exit 0
fi

# exit_code 確認
exit_code="$(printf '%s' "$INPUT" | jq -r '.tool_response.exit_code // .tool_response.exitCode // empty' 2>/dev/null || true)"

# exit_code が取れなかった場合は stderr に "error" 系の文字が無ければ成功とみなす
# (実装依存だが安全側: 不明な場合は記録しておく方が watcher の起点として有用)
if [ -n "$exit_code" ] && [ "$exit_code" != "0" ]; then
  exit 0
fi

cwd="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"
if [ -z "$cwd" ]; then
  cwd="$(pwd)"
fi

canonical_cwd="$(cd "$cwd" 2>/dev/null && pwd -P)" || canonical_cwd="$cwd"

# command が `git -C <path>` を含む場合、その path を target dir として扱う。
# 別 repo への commit を hook input の cwd に誤って記録するのを防ぐ (P2)。
# resolve できない / 同一 path なら cwd フォールバック。
target_canon="$canonical_cwd"
git_c_path=""
if printf '%s' "$command_str" | grep -qE '(^|[[:space:];|&])git[[:space:]]+-C[[:space:]]+[^[:space:]]+'; then
  git_c_path="$(printf '%s' "$command_str" \
    | grep -oE '(^|[[:space:];|&])git[[:space:]]+-C[[:space:]]+[^[:space:]]+' \
    | head -1 \
    | sed -E 's/.*git[[:space:]]+-C[[:space:]]+//')"
fi
if [ -n "$git_c_path" ]; then
  case "$git_c_path" in
    /*) target_resolve="$git_c_path" ;;
    *)  target_resolve="${canonical_cwd}/${git_c_path}" ;;
  esac
  resolved="$(cd "$target_resolve" 2>/dev/null && pwd -P 2>/dev/null)" || resolved=""
  if [ -n "$resolved" ]; then
    target_canon="$resolved"
  fi
fi

# proj-hash: target_canon を sha256 して先頭 8 桁
# (git -C で別 repo を指定された場合、その別 repo の proj-hash になる)
proj_hash=""
if command -v python3 >/dev/null 2>&1; then
  proj_hash="$(python3 -c '
import hashlib, sys
print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8])
' "$target_canon" 2>/dev/null || echo "")"
elif command -v shasum >/dev/null 2>&1; then
  proj_hash="$(printf '%s' "$target_canon" | shasum -a 256 | cut -c1-8)"
elif command -v sha256sum >/dev/null 2>&1; then
  proj_hash="$(printf '%s' "$target_canon" | sha256sum | cut -c1-8)"
fi

if [ -z "$proj_hash" ]; then
  exit 0
fi

# HEAD の sha / branch を target_canon (= git -C で指定された path or cwd) から取得
head_sha="$(cd "$target_canon" && git rev-parse HEAD 2>/dev/null || echo "unknown")"
branch="$(cd "$target_canon" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"

state_dir="${HOME}/.claude/agent-org/state/${proj_hash}"
mkdir -p "$state_dir"
out="${state_dir}/last-commit.json"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# command_excerpt は先頭 200 文字に切り詰め (秘密混入抑制 + ファイルサイズ抑制)
cmd_excerpt="$(printf '%s' "$command_str" | head -c 200)"

jq -n \
  --arg sha "$head_sha" \
  --arg branch "$branch" \
  --arg ts "$ts" \
  --arg cwd "$target_canon" \
  --arg hook_cwd "$canonical_cwd" \
  --arg ph "$proj_hash" \
  --arg cmd "$cmd_excerpt" \
  '{
    schema_version: "1",
    commit_sha: $sha,
    branch: $branch,
    committed_at: $ts,
    cwd: $cwd,
    hook_cwd: $hook_cwd,
    project_hash: $ph,
    triggered_by: "PostToolUse:Bash",
    command_excerpt: $cmd
  }' > "$out" 2>/dev/null || true

exit 0
