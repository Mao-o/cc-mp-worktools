# agent-org

AI organizational engineering toolkit for Claude Code. ChatGPT 議論で出てきた
「双方向通信可能な SA 群を組織化する」アイデアを、Claude Code v2.1.139+ の
subagent memory / agent teams / `/goal` / agent view で実装するプラグイン。

## Phase 1 (v0.1.0) で提供する機能

| コンポーネント | 役割 |
|---|---|
| `context-compressor` subagent | 直近会話を構造化 episode YAML に圧縮する専門エージェント (`memory: project`, `model: haiku`) |
| `/compress-context` command | context-compressor を呼び出して `.claude/episodes/<id>.yaml` を生成 |
| PostCompact hook | 通常の `/compact` 後に `compact_summary` を自動的に episode YAML に転写 |

## Phase 2 (v0.2.0) で追加された機能

| コンポーネント | 役割 |
|---|---|
| `decision-keeper` subagent | 設計判断を ADR YAML として `.claude/agent-memory/agent-org-decision-keeper/MEMORY.md` に immutable に蓄積 (`memory: project`, `model: sonnet`) |
| `recording-decision` skill | decision-keeper を Task ツール経由で invoke する手順 |
| `consulting-memory` skill | 別 subagent の `MEMORY.md` / learnings を横断参照 (subagent memory は isolation されているため Read で明示的に取り込む) |
| `/org-init` command | agent-org が使うディレクトリ群を冪等に作成 |

## Phase 3 (v0.4.0) で追加された機能 (approval 永続化は v0.7.0 で bd 化)

| コンポーネント | 役割 |
|---|---|
| `architect-reviewer` subagent | 真 RO (`tools: Read,Glob,Grep`) の review 専門家。verdict YAML を会話に返すのみで approval 書込は呼び出し側 command が担当 (`memory: project`, `model: sonnet`) |
| `running-review` skill | `architect-reviewer` を 3-5 perspective で並列 spawn する手順 (agent teams default、Task tool sequential fallback) |
| `/run-review` command | skill 起動 + verdict 集約 + bd approval issue 作成 (v0.7.0 から `bd create -t approval` + label/priority + dep。v0.6.x までは `.claude/agent-org/approvals/<task-id>.json` JSON 書込) |
| Stop hook (`stop-quality-gate.sh`) | `.claude/agent-org/quality-gates.json` 設定がある場合、required gate が failing なら session 停止を block。`kind: approvals_clean` は v0.7.0 から bd 経由で rejected approval (priority=0、open) を検出 |
| TaskCompleted hook (`task-completed-gate.sh`) | task 完了時に bd 上の `task:<task_id>` ラベル付き approval (priority=0、open) があれば block。approval 0 件は通常 task として pass (opt-in) |

## Phase 4 (v0.5.0) で追加された機能

| コンポーネント | 役割 |
|---|---|
| `regression-watcher` subagent | `--bg` + `/loop` で常駐し定期 smoke check を実行。検出結果を `~/.claude/agent-org/state/<proj-hash>/detections/` に YAML 書込 (`memory: user`, `model: haiku`) |
| `regression-fixer` subagent | `--bg` + `/goal` で自律修復ループ。修正完了時は git push + `gh pr create`/update + `~/.claude/agent-org/state/<proj-hash>/fixes/` 書込が必須 (`memory: user`, `model: sonnet`) |
| `/start-watcher [interval]` command | foreground preflight 後に `claude --agent agent-org:regression-watcher --bg "/loop <interval> smoke check"` を発射 |
| `/fix-regression <target> [condition] [--turn-cap N]` command | foreground preflight (gh auth + git remote + branch 衝突等) 後に `claude --agent agent-org:regression-fixer --bg '/goal <condition> or stop after N turns'` を発射 |
| PostToolUse(Bash) hook (`post-commit-trigger.sh`) | `git commit` 成功時に `~/.claude/agent-org/state/<proj-hash>/last-commit.json` を更新。watcher の次 loop iteration が新規 commit 以降を検査の起点にする |

v0.5.0 で親プランの全コンポーネントが揃う。実機運用フィードバックを反映後
v1.0.0 で機能セット凍結予定。設計の全体像は `docs/ARCHITECTURE.md` 参照。

## インストール

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install agent-org@mao-worktools
```

開発時:

```bash
claude --plugin-dir ./plugins/agent-org
```

## 使い方

### 初期化 (推奨)

agent-org の各機能を本格的に使う前に、必要なディレクトリを冪等に作成:

```text
/org-init
```

`.claude/agent-memory/{各 agent}/` や `~/.claude/agent-org/state/<proj-hash>/`
等の state ディレクトリを一括で作成する。何度実行しても安全 (`mkdir -p` ベース)。
未実行でも各 subagent が必要なタイミングで個別に dir を作るが、`/org-init`
で一括しておくと挙動が予測しやすい。

### 自動 episode 化 (PostCompact hook)

通常の `/compact` を実行すると、PostCompact hook が compact 結果を
`.claude/episodes/compact-<timestamp>.yaml` に自動転写。何もしなくても
セッションを跨いだ episode 蓄積が始まる。

### 手動 episode 化

直近の会話セグメントを明示的に圧縮したい場合:

```text
/compress-context
```

context-compressor subagent が起動し、現セッションの主題・決定事項・成果物・
未解決事項を YAML 形式で `.claude/episodes/<id>.yaml` に保存する。

### Episode の検索

蓄積された episode は `.claude/episodes/*.yaml` を `grep` するだけで検索可能。
各 YAML の `retrieval_keys:` フィールドに、将来呼び戻すキーワードが格納される。

```bash
grep -l "auth" .claude/episodes/*.yaml
```

### ADR (設計判断) の記録

設計判断・トレードオフを伴う選択を確定した時、`recording-decision` skill
経由で `decision-keeper` subagent を起動して ADR (Architecture Decision
Record) として蓄積する。skill は自然な会話の流れから auto-invoke される
(例: 「この決定を ADR として記録して」)。明示的に呼び出すなら conversation
で trigger 語 (`recording-decision`, `ADR 記録`, `設計判断記録` 等) を含める。

ADR は `.claude/agent-memory/decision-keeper/MEMORY.md` に追記される。
既存 ADR は immutable で、方針変更時は新 ADR を追記し旧 ADR の
`status: superseded_by:<新 id>` を更新する形にする。

### ADR / 他 subagent memory の横断参照

別 subagent (例: `decision-keeper`) が蓄積した知識を現在の subagent
context に取り込みたい場合、`consulting-memory` skill 経由で対象の
`MEMORY.md` を Read で取り込む。subagent memory は agent 間で isolation
されているため、横断参照には明示的な Read が必要。

```bash
grep -l "PostCompact" .claude/agent-memory/agent-org-decision-keeper/MEMORY.md
```

### Multi-perspective review (Phase 3、v0.7.0 で bd 化)

PR / 設計 / 実装に対して 3-5 perspective で並列レビューを実行し、bd approval
issue を生成する:

```text
/run-review PR-42 security,api-design,testability
```

省略時は perspective が自動選定される (3-5 個):

```text
/run-review design-auth-rewrite
```

`agent-org:architect-reviewer` (真 RO subagent) が各 perspective を担当し、
verdict YAML を会話に返す。`/run-review` command が集約して `bd create -t
approval` + dep で記録する。

approval status は **priority + label** で encode:

| priority | semantic | gate 判定 |
|---|---|---|
| 0 | rejected (request_changes/reject) | block |
| 1 | conditional (approve_with_conditions) | pass + warn |
| 2 | approved | pass |
| 3 | informational | gate skip |

label `task:<task_id>` で approval を task に紐付け、後続の TaskCompleted /
Stop gate がこの label で検索する。v0.6.x までの JSON 形式から移行する場合は
`/migrate-approvals-to-beads` を実行。

### Quality gate (Phase 3)

`.claude/agent-org/quality-gates.json` を配置すると、メインセッション停止時
(Stop hook) に各 gate が実行される:

```json
{
  "schema_version": "1",
  "gates": [
    { "id": "tests-passing", "kind": "command", "command": "pytest -q", "required": true },
    { "id": "lint-clean",    "kind": "command", "command": "ruff check .", "required": false },
    { "id": "no-rejected-approvals", "kind": "approvals_clean", "required": true }
  ]
}
```

`required: true` の gate が failing なら停止が block される。`required: false`
は warn のみ。設定ファイルが無ければ gate 制約は適用されない。

### Approval gate on task completion (Phase 3, opt-in)

TaskCompleted hook は **matcher 非対応・全件発火** (公式 hooks docs)。task
payload は全 field が top-level フラット (`task_id` / `task_subject` /
`task_description` / `teammate_name` / `team_name`) で `review_required` の
ような nested 設定 field は存在しない。

このため gate は **bd approval opt-in 方式** (v0.7.0):

- task に紐付いた approval (label `task:<task_id>`, type=approval) が **0 件**
  → pass (通常 task、review 不要)
- 全 approval が closed または priority=2/3 (approved/informational) → pass
- priority=1 (conditional) が open で残存 → pass + warn
- priority=0 (rejected) が 1 件でも open → block (exit 2)

つまり `/run-review <task-id>` を実行した task だけが gate 対象になり、未実行
の task は通常通り完了できる。「approval を必須にしたい」場合は単に
`/run-review` を回せばよく、明示的な `review_required` 設定は不要。

### Background regression watch (Phase 4)

定期 smoke check を background session で開始:

```text
/start-watcher 30m
```

preflight (gh auth / git remote / claude CLI / python3) が通ると、
`claude --agent agent-org:regression-watcher --bg "/loop 30m smoke check"`
が発射される。検出は `~/.claude/agent-org/state/<proj-hash>/detections/*.yaml`
に蓄積される (`<proj-hash>` は cwd を sha256 した先頭 8 桁、プロジェクト識別)。

watcher が走っている間に `git commit` をすると、PostToolUse(Bash) hook が
`last-commit.json` を更新し、watcher の次 iteration が新規 commit 以降を
重点的に検査する。

### Autonomous regression fix (Phase 4)

検出された regression / PR / task を `/goal` 駆動で自律修復:

```text
/fix-regression PR#42
/fix-regression detection:detection-2026-05-18T03Z
/fix-regression task:fix-auth-flow --turn-cap 40
```

preflight (gh auth / git remote / 作業ツリー clean / branch 衝突 /
gh repo view 疎通) が通ると、`claude --agent agent-org:regression-fixer
--bg '/goal <condition> or stop after N turns'` が発射される。修正完了時
fixer が:

1. `git commit` する
2. `git push origin <branch>` する
3. `gh pr create` または既存 PR への push で更新する
4. `~/.claude/agent-org/state/<proj-hash>/fixes/<ts>.json` に PR URL を記録

main session 側は `gh pr view <URL>` で内容確認できる。

`/goal` の暴走防止のため、condition には**必ず `or stop after N turns`**
句が含まれる (`/fix-regression` が自動付与)。default turn-cap は target
規模に応じて 25 (small) / 50 (medium) / 80 (large)、`--turn-cap N` で上書き。

### Worktree 隔離と統合経路 (Phase 4 設計の中核)

`claude --bg` で起動された session は working directory 配下への書込が
`.claude/worktrees/<id>/` に自動隔離される。agent-org plugin はこれを以下で
回避する:

- watcher / fixer の **memory は `user` scope** (`~/.claude/agent-memory/...`、
  working dir 外なので隔離されない)
- watcher の **detection state** は `~/.claude/agent-org/state/<proj-hash>/`
  (working dir 外)
- fixer の **修正成果統合は git remote 経由** (`git push` + `gh pr` は worktree
  隔離の影響を受けない)

これにより `--bg` で書いた fix は確実に main session から見える状態になる。

## 設計の重要点

### Episode YAML 形式

```yaml
episode:
  id: 2026-05-13T03-45-00Z
  trigger: manual | auto | post_compact
  topic: <主題>
  decisions:
    - <決定 1>
  artifacts_changed:
    - path: <ファイル>
      summary: <変更要約>
  unresolved:
    - <持ち越し>
  retrieval_keys: [<キーワード>]
  source_summary: |
    <元の compact_summary または手動圧縮の本文>
```

### PostCompact hook の入力 schema

公式 docs (`https://code.claude.com/docs/en/hooks.md#PostCompact-input`) より:

```json
{
  "session_id": "abc123",
  "transcript_path": "/Users/.../transcript.jsonl",
  "cwd": "/Users/...",
  "hook_event_name": "PostCompact",
  "trigger": "manual",
  "compact_summary": "Summary of the compacted conversation..."
}
```

hook は `compact_summary` を優先的に使い、空または欠落時は `transcript_path`
を JSONL parse する fallback ロジックで動作する。

### Subagent memory の前提

context-compressor は `memory: project` で
`.claude/agent-memory/agent-org-context-compressor/` (scoped name dir) に
永続学習を蓄積する。「どの content type にはどの粒度の圧縮が効いたか」を
セッション横断で学んでいく設計 (Claude Code v2.1.33+ の auto-inject 機能)。

### approval bd issue schema (Phase 3、v0.7.0 で bd 化)

`bd create -t approval` で生成される approval issue の構造:

| 項目 | 表現 |
|---|---|
| type | `approval` |
| priority | `0`=rejected / `1`=conditional / `2`=approved / `3`=informational |
| 必須 label | `approval` / `task:<task_id>` / `agent-org` / `aggregate:<verdict>` |
| 追加 label | `perspective:<persp>` (per reviewer、複数付与可) |
| description body | 集約 verdict YAML (`schema_version` / `task_id` / `target` / `aggregate_overall` / `verdicts[]` / `concerns_summary`) |
| dep | `bd dep add <task> <approval>` で approval が task を blocks |

description body 内の verdict YAML 主要フィールド:

- `schema_version`: `"1"`
- `task_id`: kebab-case 識別子
- `target`: `{ type, ref }` (PR / commit_range / design_doc / implementation)
- `aggregate_overall`: `approve` / `approve_with_conditions` /
  `request_changes` / `reject` (全 reviewer の最重)
- `concerns_summary`: severity 別件数
- `verdicts[]`: 各 reviewer の verdict 全文

v0.6.x までの `.claude/agent-org/approvals/<task-id>.json` JSON schema からの
migration は `/migrate-approvals-to-beads` を実行。詳細は `commands/run-review.md`
の schema セクション参照。

## 依存

- Claude Code v2.1.33 以上 (subagent `memory` frontmatter)
- Phase 3 の `running-review` skill を agent teams 経路で使う場合は
  Claude Code v2.1.32 以上 + `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
  (未設定なら Task tool sequential 経路に fallback)
- Phase 4 (`/start-watcher` / `/fix-regression`) を使う場合は Claude Code
  **v2.1.139 以上** (agent view / `--bg` / `/goal`)
- Phase 4 では `gh` CLI (認証済み) と `git remote origin` 設定が必須
  (preflight でチェック)
- 通常実行は標準ライブラリのみ。`jq` (全 hook で使用)、`python3` または
  `shasum`/`sha256sum` (proj-hash 計算)

## 関連

- 全体プラン: `~/.claude/plans/worktools-agent-org-plugin-cooperative-lamport.md`
- Phase 1 実装プラン: `~/.claude/plans/ticklish-gliding-scone.md` (この PR の根拠)
