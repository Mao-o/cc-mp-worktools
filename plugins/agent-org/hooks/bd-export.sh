#!/usr/bin/env bash
# bd-export.sh
#
# Stop hook for agent-org plugin (v0.6.0+).
# beads (bd CLI) の DB を <repo>/.beads/issues.jsonl にスナップショット export
# する。git audit trail 補償として、ユーザーが選択的に `git add .beads/issues.jsonl`
# できるよう repo 内に置く opt-in workflow。
#
# v0.8.0 (ADR-007) から bd 自体が <repo>/.beads/ に repo-local 配置のため、
# export source と output は同じ親 dir (<repo>/.beads/) に並ぶ。
#
# 動作原則: fail-open
#   - bd 未 install / jq 未 install / DB 未初期化 / export 失敗
#     いずれでも warn だけ stderr に出して exit 0
#   - Stop を block しない (本 hook の責務は audit trail のみ、quality gate は
#     先に発火する stop-quality-gate.sh が担当)
#   - exit 2 (block) は決して返さない
#
# 入力 JSON (stdin) Stop:
#   - common: session_id, transcript_path, cwd, hook_event_name
#   - stop_hook_active: true なら再入 (即 exit 0)
#
# 出力:
#   - <repo>/.beads/issues.jsonl (各行 1 issue の JSON)
#   - 失敗時のメッセージは stderr に "[agent-org:bd-export] ..." prefix で
#
# bd export の検出:
#   - bd 1.0.4+ で `bd export` subcommand があれば使う (推奨)
#   - 無ければ fallback として `bd list --json | jq -c '.[]'` で同等形式に変換
#   - どちらも失敗したら warn + exit 0
#
# 依存: bd, jq, date

set -uo pipefail
trap 'exit 0' ERR

# fail-open helper
warn() {
  echo "[agent-org:bd-export] $*" >&2
}

INPUT="$(cat)"

# jq 必須 (それ以外の hook と同じ前提)
if ! command -v jq >/dev/null 2>&1; then
  warn "jq not installed; skip"
  exit 0
fi

# stop_hook_active で再入回避
stop_hook_active="$(printf '%s' "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null || echo "false")"
if [ "$stop_hook_active" = "true" ]; then
  exit 0
fi

# bd 未 install なら skip (v0.6.0 は hard dependency だが、export は補助機能なので fail-open)
if ! command -v bd >/dev/null 2>&1; then
  warn "bd CLI not installed; skip export"
  exit 0
fi

# cwd 取得 (hook input → fallback pwd)
cwd="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)"
if [ -z "$cwd" ]; then
  cwd="$(pwd)"
fi

canonical_cwd="$(cd "$cwd" 2>/dev/null && pwd -P)" || canonical_cwd="$cwd"

# repo root 確認 (git repo であること、v0.8.0 から bd は <repo>/.beads/ に配置)
# worktree 内で起動された場合 (--bg `.claude/worktrees/<id>/`) は
# git rev-parse --show-toplevel が worktree path を返す。bd は worktree-aware
# で main repo `.beads/` を共有するため、git common-dir 経由で main repo を解決。
repo_root="$(cd "$canonical_cwd" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -z "$repo_root" ]; then
  # silent skip: git repo 外での Stop は audit trail 対象外
  exit 0
fi
main_repo="$(cd "$canonical_cwd" 2>/dev/null && cd "$(dirname "$(git rev-parse --git-common-dir 2>/dev/null)")" 2>/dev/null && pwd -P)"
[ -n "$main_repo" ] || main_repo="$repo_root"

# BEADS_DIR (= <main_repo>/.beads/) が存在しなければ skip (未 /org-init プロジェクト)
BEADS_DIR="${main_repo}/.beads"
if [ ! -d "$BEADS_DIR" ]; then
  # silent skip: 多数のプロジェクトで /org-init していない状態を想定
  exit 0
fi

# output も main_repo の .beads/ に書く (worktree 内で stop しても main repo に export)
out="${BEADS_DIR}/issues.jsonl"
tmp="${out}.tmp.$$"

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "unknown")"

# bd export を試す。help にあれば使う。
# v0.8.0: cd "$repo_root" で bd 自動 resolve (worktree でも main repo .beads/ にアクセス)
# v0.8.1: exported_method 変数を分岐ごとに set し、meta の source field が
#         実際の経路を正確に伝えるよう修正
exported=0
exported_method=""
if (cd "$repo_root" && bd export --help >/dev/null 2>&1); then
  if (cd "$repo_root" && bd export >"$tmp" 2>/dev/null); then
    mv "$tmp" "$out"
    exported=1
    exported_method="bd_export"
  else
    warn "bd export failed; will try fallback"
    rm -f "$tmp"
  fi
fi

# Fallback: bd list --json でスナップショット作成 (1 行 1 issue 形式)
if [ "$exported" = "0" ]; then
  if (cd "$repo_root" && bd list --json 2>/dev/null) \
      | jq -c '.[]' >"$tmp" 2>/dev/null; then
    if [ -s "$tmp" ]; then
      mv "$tmp" "$out"
      exported=1
      exported_method="bd_list_fallback"
    else
      # 空でも 0 byte の jsonl を残しておく (snapshot されたこと自体は記録)
      mv "$tmp" "$out"
      exported=1
      exported_method="bd_list_fallback_empty"
    fi
  else
    warn "bd list fallback failed; skip"
    rm -f "$tmp"
    exit 0
  fi
fi

# 末尾に export metadata 行を追加すると jsonl の互換性を壊すため、別ファイルに
# 書く: <repo>/.beads/issues.jsonl.meta
meta="${BEADS_DIR}/issues.jsonl.meta"
issue_count="$(wc -l <"$out" 2>/dev/null | tr -d '[:space:]' || echo "0")"
{
  echo "exported_at: $ts"
  echo "main_repo: $main_repo"
  echo "invoked_from: $repo_root"
  echo "issue_count: $issue_count"
  echo "source: ${exported_method:-bd_export}"
} > "$meta" 2>/dev/null || true

exit 0
