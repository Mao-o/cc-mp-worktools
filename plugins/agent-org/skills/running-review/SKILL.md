---
name: running-review
description: |
  architect-reviewer subagent を複数視点 (3-5 名) で並列に起動し、
  PR / 設計 / 実装に対する多角的レビューを実行するスキル。
  agent teams 機能 (experimental, CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1)
  で teammate を spawn する手順を提供する。verdict 集約から
  bd approval issue 作成・dep 連携・learnings 永続化まで skill 内で
  完結する (v0.10.1, V9)。bd 未設定環境では persist を best-effort skip。
  Use when: PR / 設計を multi-perspective でレビューしたい、
  approval workflow に乗せて quality gate を効かせたい時。
  Triggers: running-review, /run-review, multi-perspective review,
  並列レビュー, agent teams レビュー, architect-reviewer 起動,
  reviewer spawn, 多視点レビュー
---

# Running Review Skill

`architect-reviewer` subagent を複数視点で並列起動して、多角的レビューから
bd approval issue 作成まで一貫して実行するスキル。agent teams 機能を活用する。

## 起動条件

以下のいずれかが該当する時:

- PR / 設計ドキュメント / 実装に対して、複数の専門視点から並列レビューを得たい
- approval workflow に乗せて (`/run-review` 経由で bd approval issue を作る)
  task の quality gate (`hooks/task-completed-gate.sh` / `hooks/stop-quality-gate.sh`)
  を効かせたい
- 単独 reviewer では見落としそうな視点 (security / performance /
  api-design / dx / testability / architecture 等) を網羅したい

逆に以下では起動しない:

- 単発の質問・確認 (architect-reviewer を直接 Task で呼べばよい)
- 既に他のスキルでレビュー済みの内容の再確認

## 前提

- **Claude Code v2.1.32 以上** で `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
  が設定されていること
- agent teams 機能は experimental。spawn / mailbox の挙動は本番運用での
  安定性が未検証
- **Agent Teams は worktree 非隔離** (公式 `code.claude.com/docs/en/agents`:
  "Agent teams don't isolate teammates in worktrees, so partition the work
  so each teammate owns a different set of files")。さらに 2026-05-23 実機
  PoC (ADR-008、Claude Code 2.1.150 環境) で Agent tool の
  `isolation:"worktree"` parameter も agent definition frontmatter の
  `isolation: worktree` も teammate spawn では **silent ignore** されること
  を確定。teammate は main lead と同じ working directory で起動し、teammate
  が write すると同一 checkout で**ファイル上書き競合**が発生する。本 skill
  は architect-reviewer の真 RO 規律 (`tools: Read,Glob,Grep`) で原理的に
  回避している。**reviewer 以外の用途 (並列 write) に Agent Teams を流用
  してはいけない** (isolation parameter / frontmatter で救済不可、ADR-008 確定)。
  並列 write が必要な場合は複数の `claude --bg` セッションを独立起動すること
  (各セッションが `.claude/worktrees/<id>/` に自動隔離される)
- 環境変数が無い場合は **fallback: Task tool で sequential に invoke** する
  手順 (本 SKILL の「fallback 手順」セクション参照)

## 手順 (agent teams 経路, default)

### 1. レビュー対象とタスク識別子を特定する

- レビュー対象: PR 番号 / commit range / 設計 doc path / 実装 path 等
- `<task-id>`: 後で bd approval issue の label `task:<id>` に使う識別子
  (例: `PR-42` / `design-2026-05-18` / `pr-42-perf-review`)
- レビュー範囲が広すぎる場合は呼び出し側で複数 task に分割する

### 2. 視点 (perspective) を 3-5 個選ぶ

レビュー対象の性質に応じて、`architect-reviewer.md` の perspective 表から
3-5 個を選択する。典型的な選び方:

| 対象の性質 | 推奨 perspective |
|---|---|
| 新規 API / エンドポイント追加 | `api-design`, `security`, `testability`, `architecture` |
| パフォーマンス改善 PR | `performance`, `testability`, `dx`, `architecture` |
| 大規模リファクタ | `architecture`, `api-design`, `testability`, `dx` |
| security-sensitive 変更 (auth / 認可 / secret 取扱) | `security`, `api-design`, `architecture`, `testability` |
| 設計ドキュメント | `architecture`, `api-design`, `dx`, `testability` |

最少 3 個、最多 5 個に収める (5 を超えると集約コストが上がる)。

### 3. teammate を spawn する

agent teams で teammate を spawn する一般的な形:

- 各 teammate は同じ subagent type (`agent-org:architect-reviewer`) を使う
- 各 teammate に perspective を 1 つ割り当てる
- メッセージ payload に「対象 / perspective / 期待する verdict 形式」を含める

```
（agent teams 用の spawn 呼び出しは Claude Code 実装側で SendMessage /
TeamCreate 等の deferred tool 経由で行う。具体的な手順は呼び出し側
command が責任を持つ。本スキルは「spawn して verdict を YAML で受け取れ」と
いう手順抽象だけ提供する）
```

各 teammate に渡す prompt の典型形:

```
あなたは architect-reviewer subagent として下記をレビューしてください。

レビュー対象:
  type: pr
  ref: PR#42
  paths:
    - src/auth/jwt.ts
    - src/auth/middleware.ts
  diff: |
    （diff の要約または PR URL）

perspective: <security | performance | api-design | dx | testability | architecture>

返す verdict YAML は architect-reviewer.md に定義された形式に厳密に従って
ください。verdict 以外の自由文は返さないでください。
```

### 4. 各 teammate から verdict YAML を回収する

teammate が会話に返した YAML をそれぞれ取り出し、メモリ内 (skill 出力) で
リスト化する。

各 verdict は以下のフィールドを必ず含む (architect-reviewer.md 仕様):

- `verdict.perspective`
- `verdict.overall` (`approve` / `approve_with_conditions` /
  `request_changes` / `reject`)
- `verdict.confidence`
- `verdict.concerns[]` (各 severity 付き)

### 5. verdict 集約サマリを返す

以下を集約サマリとしてまとめる (step 6 の persist に使用):

- `<task-id>`
- `target` (type / ref)
- 各 perspective の verdict 全文 (YAML として valid なまま)
- 集約サマリ:
  - 全 reviewer の `overall` の最重 (`reject` > `request_changes` >
    `approve_with_conditions` > `approve` の順) を `aggregate_overall` として
  - `critical` 件数合計
  - `major` 件数合計
  - 各 reviewer の confidence の最低値

### 6. 結果を永続化する (bd, best-effort)

bd CLI と `<repo>/.beads/` が利用可能な場合のみ実行する。
未設定環境では skip し、verdict サマリのみ返す (レビュー結果は会話内で確認可能)。

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
if ! command -v bd &>/dev/null || ! [ -d "$REPO_ROOT/.beads" ]; then
  # persist skip → step 7 に進む
fi
```

**6a. task issue find-or-create**

```bash
task_bd="$(cd "$REPO_ROOT" && bd list -l "task:${task_id}" -t task --json 2>/dev/null \
  | jq -r '.[0].id // empty')"
[ -z "$task_bd" ] && task_bd="$(cd "$REPO_ROOT" && bd create "task: ${task_id}" \
  -t task -p 2 -l "task:${task_id}" -l "agent-org" --json | jq -r .id)"
```

**6b. approval issue 作成**

aggregate → priority マッピング:

| aggregate_overall | priority |
|---|---|
| `reject` / `request_changes` | 0 (blocker) |
| `approve_with_conditions` | 1 (conditional) |
| `approve` | 2 (approved) |

description body は step 5 の集約 verdict YAML。秘密が含まれていれば
`***REDACTED***` に置換する。

```bash
appr_bd="$(cd "$REPO_ROOT" && bd create "approval: ${task_id} (${aggregate})" \
  -t approval -p "${prio}" \
  -l "approval" -l "task:${task_id}" -l "agent-org" -l "aggregate:${aggregate}" \
  "${perspective_labels[@]}" \
  -d "$verdict_body" --json | jq -r .id)"
```

再 review 時 (同一 `<task-id>` で再実行): 既存 open approval を
`bd dep add <new> <old> --type supersedes` + close してから新 approval を作成。

**6c. dep を張る**

```bash
(cd "$REPO_ROOT" && bd dep add "$task_bd" "$appr_bd")
```

approved (priority=2) は即 `bd close "$appr_bd"` で blocker 解除。
informational (priority=3) は dep 不要。

**6d. learnings 永続化** (best-effort)

各 reviewer が verdict YAML 内に `learnings_to_persist:` を付けた場合、
`bd remember "review-heuristic: <summary> [keys: <k1>,<k2>]"` で保存する。
失敗しても approval 作成は完了させる。

### 7. ユーザーに結果を通知する

- approval status / concern 件数 / approval bd id / task bd id を表示
- 各 reviewer の overall を 1 つのテーブルとして表示
- bd 未設定で persist skip した場合はその旨を警告

## fallback 手順 (Task tool で sequential)

`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` が未設定 / 1 でない場合、または agent
teams が利用できない環境では、Task tool で順次 invoke する形にフォールバック
する:

1. 上記「視点を 3-5 個選ぶ」までは同じ
2. perspective ごとに Task tool で `subagent_type: "agent-org:architect-reviewer"`
   を順次起動 (並列ではなく直列)
3. 各起動の prompt は agent teams 経路と同じ形 (perspective を明示)
4. 起動結果の YAML を一つずつ収集
5. 集約サマリ作成は agent teams 経路と同じ

sequential 経路は遅いが、experimental 機能を使わないため安定する。

## 注意事項

- **reviewer subagent は真 RO** (Read/Glob/Grep のみ)。verdict はファイル書込
  しない。bd approval issue 作成は skill の step 6 で main session が実行する
- 各 perspective を同じ reviewer 役 (`agent-org:architect-reviewer`) に
  割り当てる設計。perspective ごとに別 subagent 定義を作らない
  (architect-reviewer 内で perspective 切替して観点を変える)
- agent teams は **per-teammate permission mode 設定不可** (全 teammate が
  lead と同じ permission mode で起動)。reviewer は tool allowlist が
  frontmatter で固定されているので permission mode 差は影響しない
- spawn に失敗した teammate がいた場合、その perspective は欠落として
  集約サマリに `missing_perspectives: [<list>]` を含めて返す
- 5 perspectives を超える場合は task を分割する (例: pre-merge review と
  post-merge security audit を分ける)

## 関連

- subagent 定義: `agents/architect-reviewer.md`
- subagent 定義: `agents/architect-reviewer.md`
- 横断参照: `skills/consulting-memory/SKILL.md`
  (reviewer が過去 ADR を参照する手順)
- 公式 docs:
  - <https://code.claude.com/docs/en/agent-teams>
  - <https://code.claude.com/docs/en/sub-agents>
