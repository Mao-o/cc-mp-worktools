---
name: running-review
description: |
  architect-reviewer subagent を複数視点 (3-5 名) で並列に起動し、
  PR / 設計 / 実装に対する多角的レビューを実行するスキル。
  agent teams 機能 (experimental, CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1)
  で teammate を spawn する手順を提供する。verdict 集約と
  bd approval issue (`bd create -t approval`、v0.7.0 から bd 一本化) への
  書込は呼び出し側 command (`/run-review`) の責務 (このスキルは spawn と
  verdict 回収まで)。
  Use when: PR / 設計を multi-perspective でレビューしたい、
  approval workflow に乗せて quality gate を効かせたい時。
  Triggers: running-review, /run-review, multi-perspective review,
  並列レビュー, agent teams レビュー, architect-reviewer 起動,
  reviewer spawn, 多視点レビュー
---

# Running Review Skill

`architect-reviewer` subagent を複数視点で並列起動して、多角的なレビューを
実行するスキル。agent teams 機能を活用する。

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

呼び出し側 command がそのまま使えるよう、以下を skill 出力としてまとめる:

- `<task-id>`
- `target` (type / ref)
- 各 perspective の verdict 全文 (YAML として valid なまま)
- 集約サマリ:
  - 全 reviewer の `overall` の最重 (`reject` > `request_changes` >
    `approve_with_conditions` > `approve` の順) を `aggregate_overall` として
  - `critical` 件数合計
  - `major` 件数合計
  - 各 reviewer の confidence の最低値

bd approval issue への書込 (`bd create -t approval` + label/priority + dep) は
呼び出し側 command (`/run-review`) が行う (このスキルは行わない)。詳細な
schema は `commands/run-review.md` を参照。

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
  しないため、bd approval issue 作成は必ず呼び出し側 command (`/run-review`)
  で行う
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
- 上位 command: `commands/run-review.md` (verdict 集約 + bd approval issue 作成)
- 横断参照: `skills/consulting-memory/SKILL.md`
  (reviewer が過去 ADR を参照する手順)
- 公式 docs:
  - <https://code.claude.com/docs/en/agent-teams>
  - <https://code.claude.com/docs/en/sub-agents>
