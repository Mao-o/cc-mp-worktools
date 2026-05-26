#!/usr/bin/env bash
# lint-bd-keys.sh — bd memory key の命名規約違反を検出する
# Usage: ./scripts/lint-bd-keys.sh [--fix-suggestions]
#
# ADR-010 key 命名規約:
#   architect-reviewer  → review-heuristic-*
#   regression-fixer    → fix-pattern-*
#   regression-watcher  → watch-heuristic-* / false-positive-*
#   decision-keeper     → decision-meta-*
#
# Exit codes:
#   0 — 違反なし (or bd memories が空)
#   1 — 違反あり
#   2 — 前提条件エラー (bd CLI / .beads/ 未初期化)

set -euo pipefail

VALID_PREFIXES=(
  "review-heuristic-"
  "fix-pattern-"
  "watch-heuristic-"
  "false-positive-"
  "decision-meta-"
)

show_fix=false
if [[ "${1:-}" == "--fix-suggestions" ]]; then
  show_fix=true
fi

if ! command -v bd >/dev/null 2>&1; then
  echo "ERROR: bd CLI が見つかりません" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [[ -z "$REPO_ROOT" ]]; then
  echo "ERROR: git repository 外です" >&2
  exit 2
fi

MAIN_REPO="$(cd "$(dirname "$(git rev-parse --git-common-dir 2>/dev/null)")" 2>/dev/null && pwd -P)"
[[ -n "$MAIN_REPO" ]] || MAIN_REPO="$REPO_ROOT"

if [[ ! -d "$MAIN_REPO/.beads" ]]; then
  echo "ERROR: $MAIN_REPO/.beads が未初期化" >&2
  exit 2
fi

if ! keys_raw="$(cd "$REPO_ROOT" && bd memories 2>&1)"; then
  echo "ERROR: bd memories 実行失敗:" >&2
  echo "$keys_raw" >&2
  exit 2
fi

if [[ -z "$keys_raw" ]]; then
  echo "OK: bd memories が空です (key なし)"
  exit 0
fi

violations=0
valid=0
total=0

while IFS= read -r key; do
  [[ -z "$key" ]] && continue
  key="${key#"${key%%[![:space:]]*}"}"
  key="${key%"${key##*[![:space:]]}"}"
  [[ -z "$key" ]] && continue

  total=$((total + 1))
  matched=false

  for prefix in "${VALID_PREFIXES[@]}"; do
    if [[ "$key" == ${prefix}* ]]; then
      matched=true
      break
    fi
  done

  if $matched; then
    slug="${key#*-*-}"
    if [[ "$slug" =~ [^a-z0-9-] ]]; then
      echo "WARN: slug に非 kebab-case 文字: $key"
      violations=$((violations + 1))
      if $show_fix; then
        suggested="$(echo "$slug" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]/-/g' | sed 's/--*/-/g' | sed 's/-$//')"
        echo "  → 提案: ${key%%"$slug"}$suggested"
      fi
    else
      valid=$((valid + 1))
    fi
  else
    echo "FAIL: 不明な prefix: $key"
    violations=$((violations + 1))
    if $show_fix; then
      echo "  → 有効な prefix: ${VALID_PREFIXES[*]}"
    fi
  fi
done <<< "$keys_raw"

echo ""
echo "合計: $total keys, 適合: $valid, 違反: $violations"

if [[ $violations -gt 0 ]]; then
  exit 1
fi
exit 0
