---
description: v0.5.x で蓄積された `~/.claude/agent-org/state/<proj-hash>/{detections,fixes}/` を beads issue に変換する one-shot migration。foreground 専用、idempotent、旧ファイルは削除せず残す。v0.8.0 (ADR-007) で bd は `<repo>/.beads/` に repo-local
---

# /migrate-to-beads

v0.5.x までの `detections/*.yaml` (regression-watcher 出力) と `fixes/*.json`
(regression-fixer 出力) を **beads issue** (`<repo>/.beads/`) に
変換する one-shot migration。

v0.6.0 で beads が hard dependency になり、v0.8.0 で bd の物理配置が
`<repo>/.beads/` (repo-local) に変わった。既存 state を持つプロジェクトは
このコマンドで bd に取り込む必要がある。foreground 専用 (一度に大量の `bd
create` を発火するため、background では auto-deny される)。

## 引数

```text
/migrate-to-beads [--dry-run]
```

| 引数 | 説明 |
|---|---|
| `--dry-run` (任意) | 実際の `bd create` / `bd dep add` を行わず、変換予定のリストのみ表示 |

## 前提条件

- `bd` CLI が install 済 (`brew install beads` / Mac)
- `<repo>/.beads/` が初期化済 (未なら `/org-init` を先に実行)
- `yq` (YAML parser、Mac: `brew install yq`) — detection YAML パースに必要
- `jq` — fix JSON パースに必要

## 手順

以下を **foreground で順次実行**してください。

### 1. 前提チェック

```bash
for tool in bd yq jq python3; do
  command -v $tool >/dev/null 2>&1 || {
    echo "FATAL: $tool not installed"; exit 1;
  }
done

PROJ_HASH=$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
")

# v0.8.0: bd は <repo>/.beads/ に配置、cd <repo> で bd 自動 resolve
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
[ -n "$REPO_ROOT" ] || { echo "FATAL: not in a git repo"; exit 1; }

BEADS_DIR="$REPO_ROOT/.beads"
STATE_DIR="$HOME/.claude/agent-org/state/$PROJ_HASH"

[ -d "$BEADS_DIR" ] || { echo "FATAL: $BEADS_DIR not initialized. Run /org-init first"; exit 1; }
[ -d "$STATE_DIR/detections" ] || { echo "info: no $STATE_DIR/detections to migrate"; }
[ -d "$STATE_DIR/fixes" ] || { echo "info: no $STATE_DIR/fixes to migrate"; }

DRY_RUN="${1:-}"
[ "$DRY_RUN" = "--dry-run" ] && echo "[DRY RUN] no bd writes will be performed"

# 以降の bd invoke はすべて (cd "$REPO_ROOT" && bd ...) パターンで動作
# (bd の git worktree-aware 自動 resolve に委ねる、ADR-007)
cd "$REPO_ROOT"
```

### 2. detection YAML → bd issue (type=detection)

```bash
MAP_FILE="/tmp/migration-map-$PROJ_HASH.json"
echo "{}" > "$MAP_FILE"

severity_to_prio() {
  case "$1" in
    critical) echo 0 ;;
    major)    echo 1 ;;
    minor)    echo 2 ;;
    flaky)    echo 3 ;;
    *)        echo 3 ;;
  esac
}

if [ -d "$STATE_DIR/detections" ]; then
  for f in "$STATE_DIR/detections"/*.yaml; do
    [ -f "$f" ] || continue
    legacy_id="$(basename "$f" .yaml)"

    # 既に migrate 済か確認 (idempotent)
    existing_id="$(bd list -l "legacy-id:$legacy_id" --json 2>/dev/null \
      | jq -r '.[0].id // empty')"
    if [ -n "$existing_id" ]; then
      echo "skip: $legacy_id already migrated to $existing_id"
      # skip 時も map に記録する (後続の fix migration で
      # legacy_detection_id → bd_detection_id 解決に使うため。
      # 入れないと再実行時に dep / close が失われる)
      if [ "$DRY_RUN" != "--dry-run" ]; then
        jq --arg k "$legacy_id" --arg v "$existing_id" '. + {($k): $v}' "$MAP_FILE" > "$MAP_FILE.tmp" \
          && mv "$MAP_FILE.tmp" "$MAP_FILE"
      fi
      continue
    fi

    title="$(yq -r '.detection.observation.summary // .detection.id // "(no title)"' "$f")"
    severity="$(yq -r '.detection.observation.severity // "minor"' "$f")"
    kind="$(yq -r '.detection.observation.kind // "test_failure"' "$f")"
    branch="$(yq -r '.detection.trigger.branch // "unknown"' "$f")"
    commit="$(yq -r '.detection.trigger.last_commit_sha // "unknown"' "$f")"
    status_yaml="$(yq -r '.detection.status // "pending_fix"' "$f")"
    body="$(yq -r '.detection' "$f")"
    prio=$(severity_to_prio "$severity")

    echo "  detection: $legacy_id → severity=$severity prio=$prio status=$status_yaml"

    if [ "$DRY_RUN" = "--dry-run" ]; then
      bd_id="(would-create)"
    else
      bd_id="$(bd create "$title" \
        -t detection -p "$prio" \
        -l "severity:$severity" \
        -l "kind:$kind" \
        -l "branch:$branch" \
        -l "commit:$commit" \
        -l "agent-org" \
        -l "legacy-id:$legacy_id" \
        -d "$body" --json | jq -r .id)"

      # status_yaml が resolved / superseded ならすぐ close
      case "$status_yaml" in
        resolved|superseded) bd close "$bd_id" ;;
      esac

      # map に記録 (fix 段階で利用)
      jq --arg k "$legacy_id" --arg v "$bd_id" '. + {($k): $v}' "$MAP_FILE" > "$MAP_FILE.tmp" \
        && mv "$MAP_FILE.tmp" "$MAP_FILE"
    fi
    echo "    → $bd_id"
  done
else
  echo "skip: $STATE_DIR/detections does not exist"
fi
```

### 3. fix JSON → bd issue (type=fix) + dep

```bash
if [ -d "$STATE_DIR/fixes" ]; then
  for f in "$STATE_DIR/fixes"/*.json; do
    [ -f "$f" ] || continue
    legacy_fix_id="$(basename "$f" .json)"

    # 既に migrate 済か確認
    existing_id="$(bd list -l "legacy-id:$legacy_fix_id" --json 2>/dev/null \
      | jq -r '.[0].id // empty')"
    if [ -n "$existing_id" ]; then
      echo "skip: $legacy_fix_id already migrated to $existing_id"
      continue
    fi

    trigger="$(jq -r '.trigger // ""' "$f")"
    branch="$(jq -r '.branch // "unknown"' "$f")"
    pr_url="$(jq -r '.pr_url // ""' "$f")"
    goal_status="$(jq -r '.goal_status // "achieved"' "$f")"
    summary="$(jq -r '.summary // "(no summary)"' "$f")"
    body="$(cat "$f")"

    # trigger の "detection:<old-id>" から legacy id を抽出 → bd id に解決
    legacy_detection_id="$(echo "$trigger" | sed -n 's/^detection:\(.*\)$/\1/p')"
    bd_detection_id=""
    if [ -n "$legacy_detection_id" ]; then
      bd_detection_id="$(jq -r --arg k "$legacy_detection_id" '.[$k] // ""' "$MAP_FILE")"
    fi

    title="fix: $summary"
    prio=2
    if [ -n "$bd_detection_id" ] && [ "$DRY_RUN" != "--dry-run" ]; then
      prio="$(bd show "$bd_detection_id" --json | jq -r .priority)"
    fi

    pr_num=""
    if [ -n "$pr_url" ]; then
      pr_num="$(echo "$pr_url" | sed -n 's|.*/pull/\([0-9]*\).*|\1|p')"
    fi

    echo "  fix: $legacy_fix_id → trigger=$trigger goal_status=$goal_status"
    echo "       detection map: $legacy_detection_id → $bd_detection_id"

    if [ "$DRY_RUN" = "--dry-run" ]; then
      bd_fix_id="(would-create)"
    else
      labels=(-l "branch:$branch" -l "agent-org" -l "legacy-id:$legacy_fix_id")
      [ -n "$bd_detection_id" ] && labels+=(-l "for-detection:$bd_detection_id")
      [ -n "$pr_num" ] && labels+=(-l "pr:$pr_num")
      [ "$goal_status" = "error" ] && labels+=(-l "outcome:error")

      bd_fix_id="$(bd create "$title" \
        -t fix -p "$prio" \
        "${labels[@]}" \
        -d "$body" --json | jq -r .id)"

      # dep: detection が fix に blocked-by (方向: child=detection, parent=fix)
      if [ -n "$bd_detection_id" ]; then
        bd dep add "$bd_detection_id" "$bd_fix_id" 2>/dev/null || true
      fi

      # close 判定 (U9 検証済の close 順序: fix が先、detection は後)
      case "$goal_status" in
        achieved)
          bd close "$bd_fix_id"
          [ -n "$bd_detection_id" ] && bd close "$bd_detection_id"
          ;;
        error)
          # fix は close (outcome:error)、detection は open のまま (別 fixer が再 claim 可能)
          bd close "$bd_fix_id"
          ;;
        turn_limit)
          # 両方 open のまま (再投入可能性)
          ;;
        *)
          ;;
      esac
    fi
    echo "    → $bd_fix_id"
  done
else
  echo "skip: $STATE_DIR/fixes does not exist"
fi
```

### 4. summary

```bash
echo ""
echo "=== migration summary ==="
echo "BEADS_DIR (auto-resolved): $BEADS_DIR"
detection_count="$(bd list -t detection -l agent-org --json 2>/dev/null | jq 'length')"
fix_count="$(bd list -t fix -l agent-org --json 2>/dev/null | jq 'length')"
echo "detection issues (with agent-org label): $detection_count"
echo "fix issues (with agent-org label): $fix_count"
echo ""
echo "旧 YAML/JSON は $STATE_DIR/{detections,fixes}/ にそのまま残存。"
echo "問題があれば '/migrate-from-beads' で rollback 可能。"
echo "確認後に旧ファイルを物理削除したい場合は Phase 9 の /cleanup-legacy-state を待つか、手動で実行。"
```

## idempotency

- `bd list -l "legacy-id:<id>"` で既存 migrate 済 issue を検出 → skip
- 同じ legacy YAML/JSON を 2 度 migrate しても重複 issue は作られない
- `--dry-run` は何も書かない、表示のみ

## 注意事項

- **foreground でのみ実行**。`--bg` セッションは permission prompt を出せず、
  大量の `bd create` が auto-deny される
- `yq` が無いと detection migration を skip する。Mac: `brew install yq`
- migration map (`/tmp/migration-map-<proj-hash>.json`) は同一セッション内のみ
  有効。fix migration を別セッションで行う場合は detection の `legacy-id:` label
  から `bd list -l "legacy-id:<id>"` で再構築可能
- 旧 YAML/JSON は **削除されない**。rollback 経路 (`/migrate-from-beads`) を
  保持するため
- `bd dep add` は `--force` なしで dep ガードが効く。fix と detection の close
  順序を間違うと exit≠0 で reject される (U9 検証済)
- 値や秘密が旧 YAML/JSON 内に格納されていた場合、そのまま bd description に
  入る。事前に grep で確認することを推奨

## 関連

- 初期化: `commands/org-init.md`
- rollback: `commands/migrate-from-beads.md`
- bd path 移行 (v0.7.x→v0.8.0): `commands/migrate-beads-to-repo-local.md`
- diagnose: `commands/bd-check.md`
- bd 規律: `skills/using-beads/SKILL.md`
- beads 公式: <https://github.com/steveyegge/beads>
