# agent-org

Claude Code の subagent 群を組織化する plugin。会話するだけで episode 圧縮・
設計判断記録・多視点レビュー・regression 監視/修正が動く。

## 始め方

```bash
# marketplace 経由
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install agent-org@mao-worktools

# 開発時
claude --plugin-dir ./plugins/agent-org
```

初期化:

```text
/org-init
```

`.claude/agent-memory/` / `.claude/episodes/` / `<repo>/.beads/` 等の
state ディレクトリを冪等に作成する。未実行でも各 subagent が必要時に個別に
作るが、一括しておくと挙動が予測しやすい。

## ユースケース

### Episode 化 (会話の構造化保存)

`/compact` 実行時に PostCompact hook が自動で `.claude/episodes/` に episode
YAML を生成する。手動で圧縮したい場合は会話で依頼するだけ:

> 「直近の議論を episode に圧縮して」

`compressing-context` skill が auto-trigger し、context-compressor subagent が
episode YAML を生成する。

```bash
# episode の検索
grep -l "auth" .claude/episodes/*.yaml
```

### ADR (設計判断の記録)

トレードオフを伴う設計判断を確定した時:

> 「この決定を ADR として記録して」

`recording-decision` skill が auto-trigger し、decision-keeper subagent が
`.claude/agent-memory/agent-org-decision-keeper/MEMORY.md` に ADR を追記する。
ADR は immutable。方針変更時は新 ADR を追記し旧 ADR を supersede する。

### Multi-perspective Review

PR / 設計 / 実装に対して 3-5 視点で並列レビュー:

> 「PR#42 を security, api-design, testability の観点でレビューして」

`running-review` skill が auto-trigger し、architect-reviewer subagent を
各視点で並列 spawn。verdict を集約して bd approval issue を作成する。

approval priority:

| priority | 意味 | gate 判定 |
|---|---|---|
| 0 | rejected | block |
| 1 | conditional | pass + warn |
| 2 | approved | pass |
| 3 | informational | skip |

### Regression 監視

定期 smoke check を background session で開始:

> 「このプロジェクトの regression を監視して」

`starting-watcher` skill が auto-trigger し、preflight 後に
`regression-watcher` subagent を `--bg` + `/loop` で起動する。検出結果は
`bd create -t detection` で記録される。

```bash
# detection の確認
REPO_ROOT="$(git rev-parse --show-toplevel)"
(cd "$REPO_ROOT" && bd ready -t detection)
```

### Regression 修正

検出された regression / PR / task を自律修復:

> 「テストが落ちているので修正して」
> 「PR#42 の CI を green にして」

`fixing-regression` skill が auto-trigger し、preflight 後に
`regression-fixer` subagent を `--bg` + `/goal` で起動する。修正完了時に
git push + PR 作成まで自律的に行う。

### 横断参照

別 subagent が蓄積した知識を現在の context に取り込む:

> 「過去の ADR を確認して」

`consulting-memory` skill が auto-trigger し、各 subagent の MEMORY.md や
bd の cross-session learning を取得する。

## 設定

### Quality Gate

`.claude/agent-org/quality-gates.json` を配置すると Stop hook で gate が実行される:

```json
{
  "schema_version": "1",
  "gates": [
    { "id": "tests-passing", "kind": "command", "command": "pytest -q", "required": true },
    { "id": "no-rejected-approvals", "kind": "approvals_clean", "required": true }
  ]
}
```

`required: true` が failing なら session 停止を block。

### beads (bd CLI)

regression 監視/修正 と approval は beads (bd CLI) を hard dependency とする。
bd は `<repo>/.beads/` に repo-local 配置 (ADR-007)。

```bash
brew install beads  # Mac
```

## コンポーネント一覧

### Skill (auto-trigger、会話から自動起動)

| skill | 用途 |
|---|---|
| `compressing-context` | 会話を episode YAML に圧縮 |
| `recording-decision` | 設計判断を ADR として記録 |
| `consulting-memory` | 他 subagent の memory / bd learning を横断参照 |
| `running-review` | 3-5 視点の並列レビュー + bd approval |
| `fixing-regression` | regression-fixer を `--bg` + `/goal` で起動 |
| `starting-watcher` | regression-watcher を `--bg` + `/loop` で起動 |
| `using-beads` | bd CLI の操作規律 (reference skill) |

### Subagent

| agent | model | memory | 役割 |
|---|---|---|---|
| `context-compressor` | haiku | project | 会話圧縮 |
| `decision-keeper` | sonnet | project | ADR 記録 |
| `architect-reviewer` | sonnet | project | 真 RO レビュー (Read/Glob/Grep のみ) |
| `regression-watcher` | haiku | user | `--bg` 常駐、定期 smoke check |
| `regression-fixer` | sonnet | user | `--bg` 自律修復 |

### Hook

| hook | event | 役割 |
|---|---|---|
| `postcompact-episode.sh` | PostCompact | compact 結果を episode YAML に自動転写 |
| `stop-quality-gate.sh` | Stop | quality gate 判定 |
| `task-completed-gate.sh` | TaskCompleted | approval gate 判定 |
| `post-commit-trigger.sh` | PostToolUse(Bash) | git commit 時に last-commit.json 更新 |
| `bd-export.sh` | — | bd data export |

### Command (init / diagnostic / migration のみ)

| command | 用途 |
|---|---|
| `/org-init` | state ディレクトリ + beads 初期化 |
| `/bd-check` | beads セットアップ診断 |
| `/migrate-to-beads` | v0.5.x → v0.6.0 (detection/fix を bd に変換) |
| `/migrate-from-beads` | v0.6.0 → v0.5.x (rollback) |
| `/migrate-approvals-to-beads` | v0.6.x approval JSON → bd issue |
| `/migrate-beads-to-repo-local` | v0.7.x → v0.8.0 (bd path 移行) |

## 依存

- Claude Code v2.1.139 以上
- agent teams を使う場合: `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
- regression / approval: `bd` CLI + `gh` CLI (認証済み) + `git remote origin`
- 共通: `jq`, `python3`

## 関連

- 設計: `docs/ARCHITECTURE.md`
- 変更履歴: `CHANGELOG.md`
- プラン: `~/.claude/plans/worktools-agent-org-plugin-cooperative-lamport.md`
