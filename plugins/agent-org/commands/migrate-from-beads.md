---
description: beads issue を旧 `~/.claude/agent-org/state/<proj-hash>/{detections,fixes}/` の YAML/JSON 形式に書き戻す rollback。v0.6.0 から v0.5.x に pin したいときに使う。foreground 専用、idempotent。v0.8.0 (ADR-007) で bd は `<repo>/.beads/` に repo-local
---

# /migrate-from-beads

`<repo>/.beads/` に格納された detection / fix issue を、v0.5.x の
`~/.claude/agent-org/state/<proj-hash>/{detections,fixes}/` YAML/JSON 形式に
書き戻す rollback コマンド。

v0.6.0/v0.8.0 で何らかの blocker が出た場合、本コマンドで旧形式に戻し
`claude plugin update agent-org -v 0.5.0` で v0.5.x に pin できる。

## 引数

```text
/migrate-from-beads [--dry-run] [--include-closed]
```

| 引数 | 説明 |
|---|---|
| `--dry-run` (任意) | 実際のファイル書込は行わず、出力予定パスのみ表示 |
| `--include-closed` (任意) | closed 済 issue も export する (default は open のみ。closed を含めると `status: resolved` 付き YAML/JSON が大量生成される) |

## 前提条件

- `bd` CLI が install 済
- `<repo>/.beads/` が初期化済
- `jq` が install 済

## 手順

### 1. 前提チェック

```bash
for tool in bd jq python3; do
  command -v $tool >/dev/null 2>&1 || {
    echo "FATAL: $tool not installed"; exit 1;
  }
done

PROJ_HASH=$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
")

# v0.8.0: bd は <repo>/.beads/ に配置
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
[ -n "$REPO_ROOT" ] || { echo "FATAL: not in a git repo"; exit 1; }

BEADS_DIR="$REPO_ROOT/.beads"
STATE_DIR="$HOME/.claude/agent-org/state/$PROJ_HASH"

[ -d "$BEADS_DIR" ] || { echo "FATAL: $BEADS_DIR not initialized"; exit 1; }

DRY_RUN=""
INCLUDE_CLOSED=""
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRY_RUN=1
  [ "$arg" = "--include-closed" ] && INCLUDE_CLOSED=1
done

mkdir -p "$STATE_DIR/detections" "$STATE_DIR/fixes"

# 以降の bd invoke はすべて cd "$REPO_ROOT" で bd 自動 resolve
cd "$REPO_ROOT"
```

### 2. detection issue → YAML

```bash
status_to_yaml() {
  case "$1" in
    open)   echo "pending_fix" ;;
    closed) echo "resolved" ;;
    *)      echo "$1" ;;
  esac
}

# filter (default: open のみ、--include-closed で全て)
if [ -n "$INCLUDE_CLOSED" ]; then
  detection_filter=""
else
  detection_filter="--status open"
fi

bd list -t detection -l agent-org $detection_filter --json \
  | jq -c '.[]' \
  | while read -r issue; do
      bd_id="$(echo "$issue" | jq -r .id)"
      status_bd="$(echo "$issue" | jq -r .status)"
      status_yaml=$(status_to_yaml "$status_bd")

      # legacy-id: label があれば元のファイル名を復元、無ければ bd_id を使う
      legacy_id="$(echo "$issue" | jq -r '.labels[] | select(startswith("legacy-id:"))' \
        | sed 's/^legacy-id://' | head -1)"
      out_name="${legacy_id:-$bd_id}"
      out_path="$STATE_DIR/detections/$out_name.yaml"

      # description body は元の YAML 形式 (migrate-to-beads が `bd create -d` で
      # 入れた YAML テキスト)。末尾に status を追記する形で書き戻す。
      body="$(bd show "$bd_id" --json | jq -r .description)"

      echo "  detection: $bd_id → $out_path (status=$status_yaml)"
      if [ -z "$DRY_RUN" ]; then
        {
          echo "# Restored from bd issue $bd_id"
          echo "# bd_status: $status_bd → yaml_status: $status_yaml"
          echo "$body"
          echo ""
          echo "status: $status_yaml"
        } > "$out_path"
      fi
    done
```

### 3. fix issue → JSON

```bash
if [ -n "$INCLUDE_CLOSED" ]; then
  fix_filter=""
else
  fix_filter="--status open"
fi

bd list -t fix -l agent-org $fix_filter --json \
  | jq -c '.[]' \
  | while read -r issue; do
      bd_id="$(echo "$issue" | jq -r .id)"
      status_bd="$(echo "$issue" | jq -r .status)"

      legacy_id="$(echo "$issue" | jq -r '.labels[] | select(startswith("legacy-id:"))' \
        | sed 's/^legacy-id://' | head -1)"
      out_name="${legacy_id:-$bd_id}"
      out_path="$STATE_DIR/fixes/$out_name.json"

      # description は migrate-to-beads が入れた JSON 形式 schema
      body="$(bd show "$bd_id" --json | jq -r .description)"

      echo "  fix: $bd_id → $out_path (status=$status_bd)"
      if [ -z "$DRY_RUN" ]; then
        # body が JSON として valid なら整形、そうでなければそのまま書く
        if echo "$body" | jq . >/dev/null 2>&1; then
          echo "$body" | jq --arg id "$bd_id" --arg s "$status_bd" \
            '. + {bd_id: $id, bd_status: $s}' > "$out_path"
        else
          # YAML テキストとして保存されているケース (古い fix migration)
          echo "$body" > "$out_path"
        fi
      fi
    done
```

### 4. summary

```bash
echo ""
echo "=== rollback summary ==="
echo "STATE_DIR=$STATE_DIR"
det_files="$(ls -1 "$STATE_DIR/detections"/*.yaml 2>/dev/null | wc -l | tr -d ' ')"
fix_files="$(ls -1 "$STATE_DIR/fixes"/*.json 2>/dev/null | wc -l | tr -d ' ')"
echo "detections/*.yaml: $det_files files"
echo "fixes/*.json:      $fix_files files"
echo ""
echo "v0.5.x に pin したい場合:"
echo "  claude plugin update agent-org@mao-worktools -v 0.5.0"
echo ""
echo "完全に beads を捨てたい場合:"
echo "  rm -rf $BEADS_DIR (<repo>/.beads/ ごと削除、v0.8.0+ path)"
echo "  /org-init を再実行すれば v0.5.x 互換の state dir のみが残る"
```

## idempotency

- 同じ bd issue を 2 度 export しても上書きされるだけ (内容は同一)
- export 元の bd issue は **削除されない**。両形式を併存させた状態になる
- 旧 state dir に既存ファイルがあると上書きされる。`--dry-run` で事前確認推奨

## 制約

- description が **migrate-to-beads で入れた形式と異なる** issue (e.g. watcher
  が直接 `bd create` で書いた v0.6.0+ ネイティブの detection) は YAML/JSON
  構造が一致しない可能性。`# Restored from bd issue` コメント + 生 description
  そのままを書く形になる
- `bd dep add` で張った依存関係は YAML/JSON では復元されない (旧形式に dep
  概念が無いため)
- 並列 fixer の `--claim` 状態も復元されない (旧形式は claim 概念なし)

## 注意事項

- **foreground でのみ実行**。大量の `bd show` / file write が走るため
- bd issue 側は **削除されない**。完全に v0.5.x に戻したい場合は手順 4 の
  案内に従い `<repo>/.beads/` を削除
- `--include-closed` は closed 済 issue も書き戻すため、大量のファイルが生成
  される可能性がある。状態保全目的なら default (open のみ) を推奨
- 値や秘密が bd description に格納されていた場合、そのまま file に書かれる。
  事前に `bd list ... --json | jq -r '.[].description' | grep -i 'token\|key'`
  等で確認すると安全

## 関連

- forward migration: `commands/migrate-to-beads.md`
- bd path 移行 (v0.7.x→v0.8.0): `commands/migrate-beads-to-repo-local.md`
- 初期化: `commands/org-init.md`
- diagnose: `commands/bd-check.md`
- beads 公式: <https://github.com/steveyegge/beads>
