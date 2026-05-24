---
name: using-beads
description: |
  beads (bd CLI、Steve Yegge 製 git-backed graph issue tracker) を agent-org
  plugin 内で扱う際の規律を提供する。v0.8.0 から bd は `<repo>/.beads/` に
  repo-local 配置 (ADR-007、git worktree-aware)、`BEADS_DIR` の明示指定は不要
  (bd 自動 resolve、ただし `--bg` 等で cwd が不確実なら明示指定可)。
  `bd prime` 必須、query は `--json` + `jq`、`bd update --claim` atomic、
  description body は v0.5.x 互換 YAML/JSON、Bash 直接 invoke (plugin slash
  command 経由禁止) 等のルールを参照する。
  Use when: agent-org の subagent (regression-watcher / regression-fixer /
  decision-keeper / architect-reviewer) で bd CLI を呼ぶ時、または手動で
  `<repo>/.beads/` を操作する時。
  Triggers: using-beads, bd CLI 規律, beads operations, bd create, bd list,
  bd update --claim, bd ready, bd dep add, bd prime, BEADS_DIR
---

# Using Beads Skill

agent-org plugin v0.6.0+ は **beads (`bd` CLI)** を detection / fix の単一情報源
として使う。v0.8.0 (ADR-007) で bd の物理配置を `<repo>/.beads/` に変更した。
本 skill は subagent / main session が `bd` を呼ぶ際の規律を集約する (各
subagent 個別の prompt で全部書くと重複するため共通化)。

## 起動条件

- regression-watcher / regression-fixer 等の subagent が `bd create` /
  `bd list` / `bd update` を呼ぶ前
- main session が `bd ready` / `bd show` で issue 状態を確認する時
- migration script (`/migrate-to-beads`, `/migrate-from-beads`,
  `/migrate-beads-to-repo-local`) を実装 / 修正する時
- 並列 fixer の atomic claim を実装する時

## 基本ルール

### 1. v0.8.0: bd は `<repo>/.beads/` から自動 resolve される (BEADS_DIR 明示指定は optional)

```bash
# v0.8.0 以降: cd <repo> または repo 内であれば bd が自動 resolve
cd "$(git rev-parse --show-toplevel)"
bd list -t detection --json
```

- bd は git worktree-aware に設計されており、repo root (`.beads/` の親) で
  invoke すれば自動的に `<repo>/.beads/` を見つける
- `--bg` セッションの worktree 隔離下でも、bd は main repo の `.beads/` を
  共有する (ADR-007 evidence)
- `cd` が現実的でない場合 (script で path が変動する等) のみ明示指定:
  `BEADS_DIR="$(git rev-parse --show-toplevel)/.beads" bd <subcmd>`

#### v0.7.x までとの互換ノート

- v0.7.x: `BEADS_DIR=~/.beads/<proj-hash>/.beads` を全 invoke で明示指定する規律
- v0.8.0: 明示指定不要 (bd 自動 resolve)。旧 path (`~/.beads/<proj-hash>/`) を
  持つプロジェクトは `/migrate-beads-to-repo-local` で新 path に移行する

#### BEADS_DIR を明示指定する場合の落とし穴 (v0.7.x の知見、v0.8.0 でも有効)

`BEADS_DIR` を明示する場合、**親 dir ではなく `.beads/` 自体**を指す必要がある:

```bash
# OK
BEADS_DIR="$(git rev-parse --show-toplevel)/.beads" bd list

# NG (Error: no beads database found)
BEADS_DIR="$(git rev-parse --show-toplevel)" bd list
```

### 2. subagent 起動冒頭で `bd prime` を実行する

```bash
cd "$(git rev-parse --show-toplevel)"
bd prime 2>&1 | head -50
```

- `bd prime` は「open issue 一覧 + 直近活動 + **cross-session learning (bd
  memories)**」をテキスト出力する公式手段。subagent の作業文脈に bd 状態を
  ロードするための起動手順
- **Phase 7+ (v0.10.0、ADR-010)**: bd 1.0.4 から `bd prime` は memory を
  **default で auto-inject** する (`bd remember --help` 公式: "Memories are
  injected at prime time (bd prime) so you have them in every session without
  manual loading")。これにより 4 subagent
  (`architect-reviewer` / `regression-fixer` / `regression-watcher` /
  `decision-keeper`) が書いた cross-session learning
  (`review-heuristic-*` / `fix-pattern-*` / `watch-heuristic-*` /
  `false-positive-*` / `decision-meta-*`) は `bd prime` 1 回で context に
  到達する — **追加 hook / Bash 呼出は不要**
- memory だけ inject したい場合 (hook context 用) は `bd prime --memories-only`
- 出力を `head -50` で切るのは、bd activity が長大な場合のコンテキスト保護
  (memory 件数が増えた場合は学習量に応じて bump 検討)
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
- `bd --json` の出力は **stderr に warning が混入することがある** ため、
  python3 / jq parse 時は `2>/dev/null` で stderr を抑制

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
| `approval` (Phase 6, v0.7.0) | YAML schema (集約 verdict、`schema_version` / `task_id` / `target` / `aggregate_overall` / `verdicts[]` / `concerns_summary`)。status は label + priority で encode (詳細: `commands/run-review.md`) |
| `task` (Phase 6, v0.7.0) | 任意 (title `task: <id>` + label `task:<id>` で識別、description は人間可読の補足) |
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
- `/bd-check` / `/migrate-to-beads` / `/migrate-from-beads` /
  `/migrate-beads-to-repo-local` は **main session 限定** (foreground)

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
| approval (Phase 6) | `-l "approval"` (type 識別用 marker) / `-l "task:<id>"` (task 紐付け、必須) / `-l "aggregate:<approve\|approve_with_conditions\|request_changes\|reject>"` / `-l "perspective:<persp>"` (per reviewer、複数付与可) |
| task (Phase 6) | `-l "task:<id>"` (human-readable id、approval 検索の primary key) |

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
- bd 1.0.4+ は `--type supersedes` 公式サポート (再 review 等で旧 approval を
  履歴管理する用途、`commands/run-review.md` 参照)

### 9. learning store の使い方 (Phase 7+、v0.10.0、ADR-010)

bd 1.0.4 から `bd remember` / `bd recall` / `bd memories` / `bd forget` の
4 コマンドで cross-session learning store を提供する。agent-org plugin
v0.10.0 (Phase 7+) でこれを 4 subagent に展開した。

| 用途 | コマンド | 規律 |
|---|---|---|
| 書込 | `bd remember "<prefix>: <summary>" --key <prefix>-<slug>` | 同 key 再 invoke で **update in place**。失敗時は `\|\| true` で iteration を継続 (curate は best-effort) |
| 単発 fetch | `bd recall <key>` | 検索ではなく **key 指定の単発取得**。ondemand のみ (起動冒頭は `bd prime` で auto-inject されるため不要) |
| list / 検索 | `bd memories [keyword]` | 引数なし = 全 list、prefix で絞込 (例: `bd memories fix-pattern`)、phrase 検索 (`bd memories "race flag"`) |
| 明示削除 | `bd forget <key>` | retention 無期限 (bd default) のため陳腐化 learning は forget で除去。実害が出るまで急がない |

```bash
# 書込 (subagent / handler 側)
REPO_ROOT="$(git rev-parse --show-toplevel)"
(cd "$REPO_ROOT" && bd remember "fix-pattern: JSONL parse fallback で EOF 改行欠落を救済" \
  --key fix-pattern-jsonl-parse-eof 2>/dev/null) || true

# main session / consulting-memory から検索
(cd "$REPO_ROOT" && bd memories fix-pattern)
(cd "$REPO_ROOT" && bd recall fix-pattern-jsonl-parse-eof)

# 陳腐化したら明示削除
(cd "$REPO_ROOT" && bd forget fix-pattern-jsonl-parse-eof)
```

#### key 命名規約 (subagent prefix で書き手を識別)

| subagent | key prefix | 書き方 |
|---|---|---|
| `architect-reviewer` | `review-heuristic-<slug>` | verdict YAML の `learnings_to_persist:`、`/run-review` が `bd remember` |
| `regression-fixer` | `fix-pattern-<slug>` | 完了 report の `learnings_to_persist:`、`/fix-regression` が `bd remember` |
| `regression-watcher` | `watch-heuristic-<slug>` / `false-positive-<slug>` | subagent prompt 内 Bash で **直接** `bd remember` (`--bg` 常駐性質、handler 不在) |
| `decision-keeper` | `decision-meta-<slug>` | 会話出力 `learnings_to_persist:`、`recording-decision` skill が `bd remember` |

`<slug>` は kebab-case、英数字 + ハイフンのみ。`bd memories <prefix>` で絞込
検索する想定の規約 (機械検証は無し、subagent / skill / handler が convention
で守る)。詳細は `consulting-memory` skill の「key 命名規約」table 参照。

#### `--global` (`beads_global` 共通 store) は使わない (ADR-010)

bd の `bd remember --global` で全 project 共通 memory store に書けるが、
agent-org plugin v0.10.0 では **使わない**。理由:

- ADR-007 (`<repo>/.beads/` repo-local 配置) と整合
- cross-project leak 回避 (project 固有 fix-pattern が他 repo に漏れない)
- worktree-aware 動作の維持 (`--bg` 隔離下でも main repo `.beads/` を共有)
- YAGNI: 全 project 共通学習の実需要が現状ない

将来 ADR-011 等で `--global` 利用を再検討する余地はある。

### 10. close 順序の規律

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
- `<repo>/.beads/` の rm -rf
- `bd config set` (rollback できる設定変更も含む)
- `/migrate-to-beads` / `/migrate-from-beads` / `/migrate-beads-to-repo-local`
  (大量の write、または旧 path 削除)

subagent (特に `--bg` の watcher/fixer) は上記を実行しない。

## エラーハンドリング

- `bd` invoke が exit≠0 のとき: stderr を会話に surface してから `goal_status:
  error` で session を畳む。runtime fallback (旧 YAML/JSON 書込) は禁止
  (split-brain 防止)
- `bd doctor` が FAIL: 何もせず main session に通知。DB 破損の可能性があるため
  自動修復は試みない
- `bd update --claim` の conflict: retry 上限 3 回、それを超えたら別 issue
  を選ぶか session を畳む

## トラブルシュート

| 症状 | 確認 |
|---|---|
| `Error: no beads database found` | cwd が repo 外か、`<repo>/.beads/` が未初期化 (`/org-init` を実行)。BEADS_DIR を明示指定している場合は `.beads/` 自体を指しているか (親 dir 指してないか) |
| `invalid issue type: detection` / `approval` / `task` | `cd <repo> && bd config set types.custom "detection,fix,approval,episode,task"` を実行したか (`/org-init` 内に含まれる)。bd 1.0.4 は `Warning: "types.custom" is not a recognized config key` を吐くが **設定は effective**、`bd types` 出力に反映される (v0.7.1 hotfix で実機確認、v0.7.0 で試した `custom.types` は逆に無視される)。verify は `bd types \| grep -E "^  (detection\|fix\|approval\|episode\|task)$"` で done 判定 |
| `cannot close X: blocked by open issues [Y]` | dep が正常動作している。先に Y を close する (順序: fix → detection) |
| `Warning: beads.role not configured` | `cd <repo> && git config beads.role maintainer` (`/org-init` 内に含まれる) |
| `bd q` で description を渡せない | `bd q` は title のみ。description 必須なら `bd create -d "..."` を使う |
| 旧 `~/.beads/<proj-hash>/.beads/` が残っている | v0.7.x からの移行が未完了。`/migrate-beads-to-repo-local` で新 path に統合 |

## 関連

- 公式 (Steve Yegge beads): <https://github.com/steveyegge/beads>
- 初期化: `commands/org-init.md`
- diagnose: `commands/bd-check.md`
- migration: `commands/migrate-to-beads.md`, `commands/migrate-from-beads.md`,
  `commands/migrate-beads-to-repo-local.md`
- 利用 subagent: `agents/regression-watcher.md`, `agents/regression-fixer.md`
- 設計判断: ADR-007 (`<repo>/.beads/` repo-local 配置採用)
