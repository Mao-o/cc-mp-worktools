#!/usr/bin/env bash
# postcompact-episode.sh
#
# PostCompact hook for agent-org plugin.
# compact 完了後に compact_summary を .claude/episodes/compact-<ts>.yaml に
# 構造化保存する。
#
# 入力 JSON (stdin) の仕様 (公式 docs hooks.md#PostCompact-input):
#   - session_id, transcript_path, cwd, hook_event_name (common fields)
#   - trigger:         "manual" | "auto"
#   - compact_summary: compact 結果の文字列要約
#
# 動作:
#   1. compact_summary が非空ならそれを使用
#   2. 空・欠落時は transcript_path の JSONL を tail して compact イベントを探す
#      fallback ロジック (docs / 実機差分への耐性)
#   3. {cwd}/.claude/episodes/compact-<ISO ts>.yaml に書き出し
#   4. PostCompact は decision control 不可なので exit 0 で常に終了
#
# 依存: jq (worktools の他 plugin と同じ前提)

set -euo pipefail

# fail-open: hook が壊れても compact 本体の挙動を妨げない
trap 'exit 0' ERR

INPUT="$(cat)"

# jq が無ければ何もしないで終了 (PostCompact は side effect なので fail-open)
if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

trigger="$(printf '%s' "$INPUT" | jq -r '.trigger // "unknown"' 2>/dev/null || echo "unknown")"
compact_summary="$(printf '%s' "$INPUT" | jq -r '.compact_summary // empty' 2>/dev/null || true)"
transcript_path="$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
cwd="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"

# cwd が取れなければ pwd で fallback (hook が working dir を CDして起動する想定)
if [ -z "$cwd" ]; then
  cwd="$(pwd)"
fi

# fallback: transcript_path から compact イベントを探す
if [ -z "$compact_summary" ] && [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
  # transcript JSONL の末尾近くから compact_summary を含む event を探す
  # 正確な event type は docs に明記されていないため、複数候補を試す
  compact_summary="$(tail -200 "$transcript_path" 2>/dev/null | \
    jq -rs '
      [.[]
        | select(
            .type == "compact_summary"
            or .compact_summary != null
            or (.content? | type == "string" and (test("compact"; "i")))
          )
      ]
      | last
      | (.compact_summary // .content // "")
    ' 2>/dev/null || true)"
fi

# それでも取れなければ「取れなかった」旨を記録して終了 (空文字記録は無意味)
if [ -z "$compact_summary" ]; then
  exit 0
fi

# 出力先準備
episodes_dir="${cwd}/.claude/episodes"
mkdir -p "$episodes_dir"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="${episodes_dir}/compact-${ts}.yaml"

# 衝突回避: 同名が存在したら数字 suffix
i=2
while [ -e "$out" ]; do
  out="${episodes_dir}/compact-${ts}-${i}.yaml"
  i=$((i + 1))
done

# YAML 用に compact_summary の各行を 4 スペースインデント
indented_summary="$(printf '%s\n' "$compact_summary" | sed 's/^/    /')"

cat > "$out" <<EOF
episode:
  id: compact-${ts}
  trigger: post_compact
  topic: |
    PostCompact hook 自動転写 (trigger: ${trigger})
  decisions: []
  artifacts_changed: []
  unresolved: []
  retrieval_keys:
    - "post_compact"
    - "trigger:${trigger}"
  source:
    type: post_compact
    trigger: ${trigger}
  source_summary: |
${indented_summary}
EOF

exit 0
