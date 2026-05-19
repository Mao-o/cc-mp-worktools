---
name: using-beads
description: |
  beads (bd CLI、Steve Yegge 製 git-backed graph issue tracker) を agent-org
  plugin 内で扱う際の規律を提供する。`BEADS_DIR` の指し方、`bd prime` 必須、
  query は `--json` + `jq`、`bd update --claim` atomic、description body は
  v0.5.x 互換 YAML/JSON、Bash 直接 invoke (plugin slash command 経由禁止) 等の
  ルールを参照する。
  Use when: agent-org の subagent (regression-watcher / regression-fixer /
  decision-keeper / architect-reviewer) で bd CLI を呼ぶ時、または手動で
  `~/.beads/<proj-hash>/.beads/` を操作する時。
  Triggers: using-beads, bd CLI 規律, beads operations, bd create, bd list,
  bd update --claim, bd ready, bd dep add, bd prime, BEADS_DIR
---

# Using Beads Skill

agent-org plugin v0.6.0+ は **beads (`bd` CLI)** を detection / fix の単一情報源
として使う。本 skill は subagent / main session が `bd` を呼ぶ際の規律を
集約する (各 subagent 個別の prompt で全部書くと重複するため共通化)。

## 起動条件

- regression-watcher / regression-fixer 等の subagent が `bd create` /
  `bd list` / `bd update` を呼ぶ前
- main session が `bd ready` / `bd show` で issue 状態を確認する時
- migration script (`/migrate-to-beads`, `/migrate-from-beads`) を実装 / 修正する時
- 並列 fixer の atomic claim を実装する時

## 基本ルール

### 1. `BEADS_DIR` は `.beads/` を直接指す

```bash
PROJ_HASH=$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
")
export BEADS_DIR="$HOME/.beads/$PROJ_HASH/.beads"
```

- `~/.beads/<proj-hash>` (親 dir) を指すと `Error: no beads database found`
  (2026-05-20 U8 検証済)
- 全 `bd` 呼出の前に `export BEADS_DIR=...` するか、`BEADS_DIR=... bd <subcmd>`
  形式で 1 行ずつ指定

### 2. subagent 起動冒頭で `bd prime` を実行する

```bash
bd prime 2>&1 | head -50
```

- `bd prime` は「open issue 一覧 + 直近活動」をテキスト出力する公式手段。subagent の作業文脈に
  bd 状態をロードするための起動手順
- 出力を `head -50` で切るのは、bd activity が長大な場合のコンテキスト保護
- `bd setup claude` を実行済の repo なら SessionStart hook で自動 inject される
  可能性があるが、agent-org plugin は `--skip-agents` で bd init するため
  **subagent prompt 内で明示 invoke** する規律

### 3. query は必ず `--json` + `jq` で parse する

```bash
# OK: 機械可読
bd ready -t detection --json | jq -r 'sort_by(.priority)[0].id'

# NG: 人間向け text 出力は format が version で変わる
bd ready -t detection | grep ...
```

- `bd list` / `bd show` / `bd ready` / `bd create` 全て `--json` flag 対応
- 改行のある field (description 等) は `jq -r .description` で raw string 取得

### 4. `bd update --claim` で atomic claim、conflict は retry

```bash
if ! bd update "$DETECTION_ID" --claim 2>/dev/null; then
  # 他 fixer が claim 済 → 別 detection を再選択
  next_id="$(bd ready -t detection --json | jq -r '.[0].id // empty')"
  [ -n "$next_id" ] && bd update "$next_id" --claim
fi
```

- 並列 fixer の race condition を bd 側に委譲する核心機構
- `--claim` 失敗時 (exit≠0) は別 issue を `bd ready` で取り直す
- 1 つの fixer session 内では max 3 retry を目安 (それを超える conflict は
  watcher が detection を量産しすぎている兆候)

### 5. description body は v0.5.x 互換形式を踏襲する

| issue type | description format |
|---|---|
| `detection` | YAML schema (`observation:` / `evidence:` / `reproducible:` / `suggested_fix_perspective:` 等) |
| `fix` | JSON schema (`schema_version` / `fix_id` / `goal_status` / `commits` / `pr_url` 等) |
| `approval` (Phase 6) | JSON schema (Phase 6 で確定) |
| `episode` (Phase 8) | YAML schema (Phase 8 で確定) |

理由: `/migrate-from-beads` で v0.5.x 形式に書き戻せる互換性を維持する。bd の
description 領域は plain text として保存されるため、形式は agent-org 側の
規律で守る (bd 側は format 強制しない)。

### 6. bd は Bash で直接 invoke、plugin slash command 経由は禁止

```bash
# OK
bd create "..." -t detection -p 1 -d "..."

# NG (`--bg` セッションでは plugin slash command 未解決)
/bd-check
```

- 本 skill / 各 subagent prompt 内では **Bash 経由のみ**
- `/bd-check` / `/migrate-to-beads` / `/migrate-from-beads` は **main session
  限定** (foreground)

### 7. label / priority の規約

| 用途 | 表現 |
|---|---|
| severity | `-l "severity:critical"` / `:major` / `:minor` / `:flaky` |
| issue kind | `-l "kind:test_failure"` / `:build_failure` / `:lint_regression` / `:runtime_error` / `:behavioral_drift` / `:flaky` |
| branch | `-l "branch:<branch-name>"` |
| commit | `-l "commit:<short-sha>"` |
| 紐付け | `-l "for-detection:<bd-id>"` / `-l "pr:<n>"` |
| migration | `-l "legacy-id:<old-yaml-or-json-filename>"` |
| 結果 | `-l "outcome:error"` / `:no-op` (`outcome:achieved` は付けない、`bd status=closed` で十分) |
| 共通 | 全 agent-org 由来 issue に `-l "agent-org"` を必ず付ける |

priority マッピング:

| severity | priority |
|---|---|
| critical | 0 |
| major | 1 |
| minor | 2 |
| flaky / その他 | 3 |

- `bd ready` は priority 昇順で並ぶため、`sort_by(.priority)[0]` で最優先取得
- approval (Phase 6) は priority 別 semantic (0=rejected, 1=conditional, 2=approved, 3=info)

### 8. 依存関係 (`bd dep add`)

```bash
bd dep add <child> <parent>
# child が parent に blocked-by
```

- semantic: child は parent が close されるまで close 不可 + ready から除外
- `bd link <child> <parent>` は `bd dep add` の alias (`--type blocks` がデフォルト)
- `bd dep relate <id1> <id2>` で bidirectional な関連 (loose link)、Phase 6/7 で使う

### 9. close 順序の規律

```bash
# fix を先に close、その後 detection を close
bd close $FIX_ID
bd close $DETECTION_ID
```

- 逆順 (`bd close $DETECTION_ID` を先) は `exit=1` で reject される (dep ガード)
- `--force` でも override 可能だが **使わない** (dep ガードは設計の核心)
- close 後 description を update したい場合は `bd update <closed-id> -d "..."` で OK

## 不可逆操作の扱い

以下は **main session の foreground 承認後のみ**:

- `bd close --force` (dep ガード bypass、データ整合性を壊す)
- `~/.beads/<proj-hash>/` の rm -rf
- `bd config set` (rollback できる設定変更も含む)
- `/migrate-to-beads` / `/migrate-from-beads` (大量の write)

subagent (特に `--bg` の watcher/fixer) は上記を実行しない。

## エラーハンドリング

- `bd` invoke が exit≠0 のとき: stderr を会話に surface してから `goal_status:
  error` で session を畳む。runtime fallback (旧 YAML/JSON 書込) は禁止
  (split-brain 防止)
- `BEADS_DIR=... bd doctor` が FAIL: 何もせず main session に通知。DB 破損の
  可能性があるため自動修復は試みない
- `bd update --claim` の conflict: retry 上限 3 回、それを超えたら別 issue
  を選ぶか session を畳む

## トラブルシュート

| 症状 | 確認 |
|---|---|
| `Error: no beads database found` | `BEADS_DIR` が `.beads/` を直接指しているか (親 dir 指してないか) |
| `invalid issue type: detection` | `bd config set types.custom "detection,fix,approval,episode"` を実行したか (`/org-init` 内に含まれる) |
| `cannot close X: blocked by open issues [Y]` | dep が正常動作している。先に Y を close する (順序: fix → detection) |
| `Warning: beads.role not configured` | `cd ~/.beads/<proj-hash> && git config beads.role maintainer` (`/org-init` 内に含まれる) |
| `bd q` で description を渡せない | `bd q` は title のみ。description 必須なら `bd create -d "..."` を使う |

## 関連

- 公式 (Steve Yegge beads): <https://github.com/steveyegge/beads>
- 初期化: `commands/org-init.md`
- diagnose: `commands/bd-check.md`
- migration: `commands/migrate-to-beads.md`, `commands/migrate-from-beads.md`
- 利用 subagent: `agents/regression-watcher.md`, `agents/regression-fixer.md`
