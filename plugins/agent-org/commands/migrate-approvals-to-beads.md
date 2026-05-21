---
description: v0.6.x で蓄積された `.claude/agent-org/approvals/*.json` を bd issue (type=approval) に変換する one-shot migration。foreground 専用、idempotent、旧 JSON は `.claude/agent-org/approvals.legacy/` に mv
---

# /migrate-approvals-to-beads

v0.6.x までの `.claude/agent-org/approvals/<task-id>.json` (旧 `/run-review`
出力) を **beads issue** (type=approval) に変換する one-shot migration。

v0.7.0 で approval が bd に一本化されるため、既存 approval JSON を持つ
プロジェクトはこのコマンドで bd に取り込む必要がある。foreground 専用 (一度に
大量の `bd create` を発火するため、background では auto-deny される)。

## 引数

```text
/migrate-approvals-to-beads [--dry-run]
```

| 引数 | 説明 |
|---|---|
| `--dry-run` (任意) | 実際の `bd create` / `bd dep add` / mv を行わず、変換予定のリストのみ表示 |

## 前提条件

- `bd` CLI install 済 (`brew install beads` / Mac)
- `~/.beads/<proj-hash>/.beads/` 初期化済 (未なら `/org-init` を先に実行)
- `jq` install 済 (旧 JSON パース + bd JSON パースに必要)
- `python3` (proj-hash 計算)

## 手順

以下を **foreground で順次実行**してください。

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

BEADS_PARENT="$HOME/.beads/$PROJ_HASH"
export BEADS_DIR="$BEADS_PARENT/.beads"
APPROVALS_DIR="$(pwd)/.claude/agent-org/approvals"
LEGACY_DIR="$(pwd)/.claude/agent-org/approvals.legacy"

[ -d "$BEADS_DIR" ] || { echo "FATAL: $BEADS_DIR not initialized. Run /org-init first"; exit 1; }
[ -d "$APPROVALS_DIR" ] || { echo "info: no $APPROVALS_DIR to migrate"; exit 0; }

DRY_RUN="${1:-}"
[ "$DRY_RUN" = "--dry-run" ] && echo "[DRY RUN] no bd writes / mv will be performed"
```

### 2. approval JSON → bd issue (type=approval) + task issue find-or-create

```bash
aggregate_to_prio() {
  case "$1" in
    reject|rejected)              echo 0 ;;
    request_changes)              echo 0 ;;
    approve_with_conditions|conditional) echo 1 ;;
    approve|approved)             echo 2 ;;
    *)                             echo 3 ;;
  esac
}

migrated=0
skipped=0

for f in "$APPROVALS_DIR"/*.json; do
  [ -f "$f" ] || continue
  legacy_id="$(basename "$f" .json)"

  # 既に migrate 済か確認 (idempotent)
  # --status all を必ず付ける: bd list のデフォルトは --status open のため、
  # priority=2 (approved) で migration 後 close された approval を検出できず、
  # 再 migrate で重複 issue が作られる (v0.7.2 hotfix で修正)
  existing_id="$(bd list -l "legacy-id:$legacy_id" -t approval --status all --json 2>/dev/null \
    | jq -r '.[0].id // empty')"
  if [ -n "$existing_id" ]; then
    echo "skip: $legacy_id already migrated to $existing_id"
    skipped=$((skipped + 1))
    continue
  fi

  # 旧 JSON から field 抽出
  task_id="$(jq -r '.task_id // empty' "$f")"
  if [ -z "$task_id" ]; then
    echo "warn: $f has no .task_id, using filename '$legacy_id'"
    task_id="$legacy_id"
  fi

  aggregate="$(jq -r '.aggregate_overall // .approval_status // "unknown"' "$f")"
  approval_status="$(jq -r '.approval_status // empty' "$f")"

  # aggregate_overall が空で approval_status が approved/conditional/rejected の
  # ケース (古い field 名) は逆引きで補正
  if [ "$aggregate" = "unknown" ] && [ -n "$approval_status" ]; then
    case "$approval_status" in
      approved)    aggregate="approve" ;;
      conditional) aggregate="approve_with_conditions" ;;
      rejected)    aggregate="reject" ;;
    esac
  fi

  prio=$(aggregate_to_prio "$aggregate")

  # description body は旧 JSON 全文 (情報を失わないため)
  # G3: heredoc / 直書きを避け変数代入経由で渡す
  body="$(cat "$f")"

  # perspectives_reviewed から label を生成
  perspective_labels=()
  while IFS= read -r p; do
    [ -n "$p" ] && perspective_labels+=(-l "perspective:$p")
  done < <(jq -r '.perspectives_reviewed[]? // empty' "$f")

  # task issue find-or-create (G4 規律: skip 時も map 更新だが、approval は
  # legacy_id 単一なので map 不要)
  task_bd="$(bd list -l "task:${task_id}" -t task --json 2>/dev/null \
    | jq -r '.[0].id // empty')"
  if [ -z "$task_bd" ]; then
    if [ "$DRY_RUN" = "--dry-run" ]; then
      task_bd="(would-create-task)"
    else
      task_bd="$(bd create "task: ${task_id}" \
        -t task -p 2 \
        -l "task:${task_id}" \
        -l "agent-org" \
        --json | jq -r .id)"
      echo "  task: ${task_id} → ${task_bd}"
    fi
  fi

  echo "  approval: $legacy_id → task=$task_id aggregate=$aggregate prio=$prio"

  if [ "$DRY_RUN" = "--dry-run" ]; then
    appr_bd="(would-create-approval)"
  else
    appr_bd="$(bd create "approval: ${task_id} (${aggregate})" \
      -t approval -p "$prio" \
      -l "approval" \
      -l "task:${task_id}" \
      -l "agent-org" \
      -l "aggregate:${aggregate}" \
      -l "legacy-id:$legacy_id" \
      "${perspective_labels[@]}" \
      -d "$body" --json | jq -r .id)"

    # dep: approval が task を blocks
    bd dep add "$task_bd" "$appr_bd" 2>/dev/null || true

    # approved (priority=2) なら approval を close (blocker 解除)
    if [ "$prio" = "2" ]; then
      bd close "$appr_bd" 2>/dev/null || true
    fi
  fi
  echo "    → $appr_bd"
  migrated=$((migrated + 1))
done
```

### 3. 旧 JSON を `.claude/agent-org/approvals.legacy/` に mv

```bash
if [ "$DRY_RUN" = "--dry-run" ]; then
  echo "[DRY RUN] would mv $APPROVALS_DIR → $LEGACY_DIR"
else
  if [ "$migrated" -gt 0 ] || [ "$skipped" -gt 0 ]; then
    mkdir -p "$LEGACY_DIR"
    for f in "$APPROVALS_DIR"/*.json; do
      [ -f "$f" ] || continue
      mv "$f" "$LEGACY_DIR/"
    done
    echo "moved: $APPROVALS_DIR/*.json → $LEGACY_DIR/"
    echo "(Phase 9 の /cleanup-legacy-state で物理削除予定。それまでは rollback 用に保持)"
  fi
fi
```

### 4. summary

```bash
echo ""
echo "=== migration summary ==="
echo "BEADS_DIR=$BEADS_DIR"
approval_count="$(bd list -t approval -l agent-org --json 2>/dev/null | jq 'length')"
task_count="$(bd list -t task -l agent-org --json 2>/dev/null | jq 'length')"
echo "approval issues (with agent-org label): $approval_count"
echo "task issues (with agent-org label):     $task_count"
echo "migrated this run: $migrated"
echo "skipped (already migrated): $skipped"
echo ""
echo "旧 JSON は $LEGACY_DIR/ に保持されています。"
echo "問題があれば手動で $LEGACY_DIR/<id>.json → $APPROVALS_DIR/ に戻すことで rollback 可能。"
```

## idempotency

- `bd list -l "legacy-id:<id>" -t approval` で既存 migrate 済 approval を検出 → skip
- 同じ legacy JSON を 2 度 migrate しても重複 issue は作られない
- `--dry-run` は何も書かず mv もしない
- 旧 JSON の mv 後に再実行した場合: `APPROVALS_DIR` が空 / 存在しないので即 exit 0

## 注意事項

- **foreground でのみ実行**。`--bg` セッションは permission prompt を出せず、
  大量の `bd create` が auto-deny される
- 旧 JSON は **削除されない**、`.claude/agent-org/approvals.legacy/` に mv される。
  rollback 経路を保持するため。Phase 9 の `/cleanup-legacy-state` で物理削除予定
- migration map は `legacy-id:<basename>` label に格納されるため、別セッションでの
  再実行でも一貫した idempotency が効く
- 値や秘密が旧 JSON 内に格納されていた場合、そのまま bd description に入る。
  事前に grep で確認することを推奨:
  ```bash
  grep -rE '(api[_-]?key|secret|token|password)' .claude/agent-org/approvals/ || echo "no obvious secrets"
  ```
- `bd dep add` は dep ガードが効く。task が既に存在し別の close 制約に
  blocked されている場合、approval dep 追加自体は成功するが task close 時に
  exit≠0 で reject される (これは正しい挙動)
- task issue が空の場合 (任意の text 描写なし) は title が `task: <task_id>` に
  なる。後で `bd update <task_bd> -d "..."` で description を補完可能

## 関連

- 旧 approval JSON 形式の発行元: `commands/run-review.md` (v0.6.x まで)
- 新 approval bd issue 形式: `commands/run-review.md` (v0.7.0+)
- 初期化: `commands/org-init.md`
- diagnose: `commands/bd-check.md`
- 他 migration: `commands/migrate-to-beads.md` (detection/fix 用)
- bd 規律: `skills/using-beads/SKILL.md`
- beads 公式: <https://github.com/steveyegge/beads>
