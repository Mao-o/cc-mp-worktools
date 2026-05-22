---
description: v0.7.x で `~/.beads/<proj-hash>/.beads/` に蓄積された bd issue を v0.8.0 (ADR-007) の新 path `<repo>/.beads/` に移行する one-shot migration。bd export → init → bd import 経路で issue ID / labels / deps / priority / memories を完全保持。foreground 専用、idempotent
---

# /migrate-beads-to-repo-local

v0.7.x までの `~/.beads/<proj-hash>/.beads/` に格納された bd issue を、
v0.8.0 (ADR-007) で採用した repo-local 配置 **`<repo>/.beads/`** に移行する
one-shot migration。

`bd export` (旧 path) → `bd init` (新 path = `<repo>/`) → `bd import` 経路で
動作する。bd 1.0.4 の round-trip サポートにより、issue ID / labels / deps /
priority / memories が完全に保持される。dolt history (旧 path 配下の
`<old>/.beads/embeddeddolt/.git/`) は新 path に移行されない (`bd export` の
仕様)。これ以降の history は `<repo>/.beads/` 配下に新規蓄積される。

## 引数

```text
/migrate-beads-to-repo-local [--dry-run] [--keep-old]
```

| 引数 | 説明 |
|---|---|
| `--dry-run` (任意) | 実際の `bd init` / `bd import` / 旧 path 削除を行わず、操作予定のみ表示 |
| `--keep-old` (任意) | migration 完了後も旧 `~/.beads/<proj-hash>/` を残す (default は削除)。rollback 検討中なら指定 |

## 前提条件

- `bd` CLI が install 済 (`brew install beads` / Mac)
- `jq`, `python3` が install 済
- 現在 cwd が **git repo 内** (`<repo>/.git/` が存在)
- 旧 path `~/.beads/<proj-hash>/.beads/` が存在
- 新 path `<repo>/.beads/` が **まだ存在しない** (既存の場合は fail-closed で abort)

未充足なら command 側で abort し、対処手順を表示する。

## 手順

以下を **foreground で順次実行**してください。

### 1. 前提チェック

```bash
for tool in bd jq python3; do
  command -v $tool >/dev/null 2>&1 || {
    echo "FATAL: $tool not installed"; exit 1;
  }
done

# git repo 内であること
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -z "$REPO_ROOT" ]; then
  echo "FATAL: not in a git repository. cwd: $(pwd)"
  exit 1
fi

# proj-hash 計算 (旧 path 検出用)
PROJ_HASH=$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
")
echo "proj-hash:  $PROJ_HASH"
echo "repo root:  $REPO_ROOT"
echo "cwd:        $(pwd -P)"

OLD_PARENT="$HOME/.beads/$PROJ_HASH"
OLD_BEADS_DIR="$OLD_PARENT/.beads"
NEW_BEADS_DIR="$REPO_ROOT/.beads"

# 旧 path 存在チェック
if [ ! -d "$OLD_BEADS_DIR" ]; then
  echo "FATAL: $OLD_BEADS_DIR が存在しません (migration 対象なし)"
  echo "    cwd と proj-hash が一致しているか確認 ('/bd-check' で診断)"
  exit 1
fi

# 新 path 既存チェック (fail-closed)
if [ -d "$NEW_BEADS_DIR" ]; then
  echo "FATAL: $NEW_BEADS_DIR が既に存在します。"
  echo "    手動マージしてから再実行するか、別 path で対応してください。"
  echo "    例: mv $NEW_BEADS_DIR ${NEW_BEADS_DIR}.bak && /migrate-beads-to-repo-local"
  exit 1
fi

DRY_RUN=""
KEEP_OLD=""
for arg in "$@"; do
  [ "$arg" = "--dry-run" ] && DRY_RUN=1
  [ "$arg" = "--keep-old" ] && KEEP_OLD=1
done

[ -n "$DRY_RUN" ] && echo "[DRY RUN] no bd writes / rm will be performed"
[ -n "$KEEP_OLD" ] && echo "[--keep-old] 旧 $OLD_PARENT は migration 後も残します"

# 旧 path の summary (移行前確認用)
OLD_COUNT="$(BEADS_DIR="$OLD_BEADS_DIR" bd list --json 2>/dev/null | jq 'length' 2>/dev/null || echo 0)"
echo "源 ($OLD_BEADS_DIR) の issue 数: $OLD_COUNT"
```

### 2. bd export from 旧 path

```bash
EXPORT_FILE="/tmp/migration-beads-${PROJ_HASH}-$(date +%s).jsonl"

if [ -n "$DRY_RUN" ]; then
  echo "[DRY RUN] would: BEADS_DIR=$OLD_BEADS_DIR bd export --all --include-memories > $EXPORT_FILE"
else
  if ! BEADS_DIR="$OLD_BEADS_DIR" bd export --all --include-memories > "$EXPORT_FILE" 2>/dev/null; then
    echo "FATAL: bd export from $OLD_BEADS_DIR failed"
    rm -f "$EXPORT_FILE"
    exit 1
  fi

  EXPORTED_COUNT="$(wc -l < "$EXPORT_FILE" | tr -d ' ')"
  echo "exported: $EXPORTED_COUNT records to $EXPORT_FILE"

  # 件数差分チェック (memories 含む場合は OLD_COUNT < EXPORTED_COUNT もあり得る)
  if [ "$EXPORTED_COUNT" -lt "$OLD_COUNT" ]; then
    echo "WARN: exported ($EXPORTED_COUNT) < issue count ($OLD_COUNT)。export が完全か確認"
  fi
fi
```

`--all` を付ける理由: agents / rigs / roles / messages 等 bd 内部 issue も
含めて移行する (デフォルトは regular issue のみ)。
`--include-memories` で `bd remember` の memory record も移行 (将来 Phase 7+
で利用)。

### 3. <repo>/ で bd init

```bash
if [ -n "$DRY_RUN" ]; then
  echo "[DRY RUN] would: (cd $REPO_ROOT && bd init --stealth --skip-agents --non-interactive --prefix $PROJ_HASH)"
else
  # v0.8.0: --stealth で `.git/info/exclude` に `.beads/` を追加 (個人 git exclude)
  if ! (cd "$REPO_ROOT" && bd init --stealth --skip-agents --non-interactive --prefix "$PROJ_HASH"); then
    echo "FATAL: bd init at $REPO_ROOT failed"
    rm -f "$EXPORT_FILE"
    exit 1
  fi
  echo "initialized: $NEW_BEADS_DIR (stealth mode)"

  # beads.role 設定 (warning 抑制、org-init と同じ)
  (cd "$REPO_ROOT" && git config beads.role maintainer 2>/dev/null || true)
fi
```

### 4. custom type 登録 (org-init と同じ)

```bash
if [ -n "$DRY_RUN" ]; then
  echo "[DRY RUN] would: (cd $REPO_ROOT && bd config set types.custom 'detection,fix,approval,episode,task')"
else
  (cd "$REPO_ROOT" && bd config set types.custom "detection,fix,approval,episode,task" 2>&1 | tail -1)
  # warning (`Warning: "types.custom" is not a recognized config key`) は
  # false alarm。verify は `bd types` 出力 grep で行う
  types_out="$(cd "$REPO_ROOT" && bd types 2>/dev/null || echo "")"
  missing=()
  for t in detection fix approval episode task; do
    echo "$types_out" | grep -qE "^  ${t}$" || missing+=("$t")
  done
  if [ ${#missing[@]} -ne 0 ]; then
    echo "FATAL: bd types missing after config set: ${missing[*]}"
    exit 1
  fi
  echo "verified: bd types includes detection, fix, approval, episode, task"
fi
```

### 5. bd import to 新 path

```bash
if [ -n "$DRY_RUN" ]; then
  echo "[DRY RUN] would: (cd $REPO_ROOT && bd import $EXPORT_FILE)"
else
  if ! (cd "$REPO_ROOT" && bd import "$EXPORT_FILE" 2>&1); then
    echo "FATAL: bd import to $NEW_BEADS_DIR failed"
    echo "    export file は保持: $EXPORT_FILE (手動 import 用)"
    exit 1
  fi
  NEW_COUNT="$(cd "$REPO_ROOT" && bd list --json 2>/dev/null | jq 'length' 2>/dev/null || echo 0)"
  echo "imported: $NEW_COUNT issues in $NEW_BEADS_DIR"

  # 件数 sanity check
  if [ "$OLD_COUNT" -ne "0" ] && [ "$NEW_COUNT" -lt "$OLD_COUNT" ]; then
    echo "WARN: new count ($NEW_COUNT) < old count ($OLD_COUNT)"
    echo "      import で issue が落ちている可能性。手動確認: bd list / bd show"
  fi
fi
```

### 6. stealth mode `.git/info/exclude` 確認

`bd init --stealth` が `.git/info/exclude` に `.beads/` を自動追加するため、
`.beads/` 配下のデータ (issue / dolt db) は git に commit されない。
ユーザーが手動で `.gitignore` を編集する必要もない (bd init が generic な
dolt ignore `.dolt/` `*.db` `.beads-credential-key` を `.gitignore` に
自動追記する)。verify:

```bash
GIT_EXCLUDE="$REPO_ROOT/.git/info/exclude"
if [ -f "$GIT_EXCLUDE" ] && grep -q "^\.beads/" "$GIT_EXCLUDE" 2>/dev/null; then
  echo "verified: $GIT_EXCLUDE excludes .beads/ (stealth mode)"
else
  echo "warn: $GIT_EXCLUDE does not exclude .beads/ - re-run with 'bd init --setup-exclude --stealth'"
fi
```

### 7. 旧 path 削除 (default、--keep-old で skip)

```bash
if [ -n "$KEEP_OLD" ]; then
  echo "[--keep-old] skip: $OLD_PARENT は残します (rollback 用)"
elif [ -n "$DRY_RUN" ]; then
  echo "[DRY RUN] would: rm -rf $OLD_PARENT"
else
  rm -rf "$OLD_PARENT"
  echo "removed: $OLD_PARENT"
fi
```

### 8. summary

```bash
echo ""
echo "=== migration summary ==="
echo "源 (old):  $OLD_BEADS_DIR (issue count: $OLD_COUNT)"
echo "新 (now):  $NEW_BEADS_DIR"
[ -n "$NEW_COUNT" ] && echo "        imported issue count: $NEW_COUNT"
echo ""
if [ -z "$DRY_RUN" ] && [ -z "$KEEP_OLD" ]; then
  echo "旧 path は削除されました。これ以降は <repo>/.beads/ のみで動作します。"
elif [ -n "$KEEP_OLD" ]; then
  echo "旧 path は保持されました ($OLD_PARENT)。"
  echo "確認後に削除する場合: rm -rf $OLD_PARENT"
fi
echo "export file: $EXPORT_FILE (rollback 用に保持、不要なら手動で rm)"
```

## idempotency

- 新 path が既存 → fail-closed で abort (上書きしない)
- 旧 path が無い → fail-closed で abort (no work to do)
- 同一 cwd / 同一 proj-hash で再実行: 1 回目で旧 path が削除されれば 2 回目は abort
- `--dry-run` は何も書かず削除もしない

## 失敗時の rollback

migration 途中で異常が出た場合 (`bd import` 失敗等):

1. **旧 path が残っている場合**: そのまま `BEADS_DIR=~/.beads/<proj-hash>/.beads`
   で旧 path から bd 操作を継続可能
2. **新 path に部分 import 済 + 旧 path 既に削除済の場合**:
   - export file (`/tmp/migration-beads-<hash>-<ts>.jsonl`) から手動で復旧
   - 新 path を一旦 `mv <repo>/.beads <repo>/.beads.broken`
   - `(cd <repo> && bd init --skip-agents --non-interactive --prefix <proj-hash>)`
   - `(cd <repo> && bd import /tmp/migration-beads-*.jsonl)`

`--keep-old` を指定して migration するのが安全。完了確認後に手動 `rm -rf` する。

## bd export → import の保証範囲

| 項目 | round-trip 保証 |
|---|---|
| issue ID | ✓ (export 時の id を import で復元) |
| title / description / type | ✓ |
| priority | ✓ |
| status (open/closed) | ✓ |
| labels | ✓ |
| dependencies (`bd dep add`) | ✓ |
| custom types (registered config) | ✗ (`bd config set types.custom` は手順 4 で再設定) |
| memory records (`bd remember`) | ✓ (`--include-memories` 指定時) |
| dolt history (`<old>/.beads/embeddeddolt/.git/`) | ✗ (issue-level snapshot のみ移行) |
| comments (`bd comment`) | ✓ |

dolt history (commit-level audit trail) は失われるが、これ以降は
`<repo>/.beads/` 配下に新規蓄積される。`<repo>/.git/` の git history とは
独立した bd 内部 history で、ユーザーが意識する局面は通常無い。

## 注意事項

- **foreground でのみ実行**。`--bg` セッションは permission prompt を出せず、
  大量の `bd create` / `rm` が auto-deny される
- 値や秘密が旧 issue description に格納されていた場合、そのまま新 DB に
  移行される。事前確認推奨:
  ```bash
  BEADS_DIR=~/.beads/<proj-hash>/.beads bd list --json \
    | jq -r '.[].description' | grep -i 'token\|key\|secret' || echo "no obvious secrets"
  ```
- export file (`/tmp/migration-beads-*.jsonl`) は migration 後も保持される
  (rollback の保険)。確認後に `rm /tmp/migration-beads-*.jsonl` で削除
- 新 path で `bd init` が `<repo>/.git/` を共有するため、`<repo>/.git/config`
  に `beads.role = maintainer` が追記される (git config 経由、idempotent)
- migration 中は他の bd セッション (例: regression-watcher / regression-fixer)
  を起動しない。`claude agents` で確認、必要なら一時停止

## 関連

- 設計判断: ADR-007 (`<repo>/.beads/` repo-local 配置採用、ADR-006 supersede)
- 初期化: `commands/org-init.md`
- diagnose: `commands/bd-check.md` (`[3b] legacy beads path` で残存検出)
- bd 規律: `skills/using-beads/SKILL.md`
- 他 migration: `commands/migrate-to-beads.md` (v0.5.x YAML/JSON → bd issue),
  `commands/migrate-from-beads.md` (rollback)
- beads 公式 (export/import): <https://github.com/steveyegge/beads>
