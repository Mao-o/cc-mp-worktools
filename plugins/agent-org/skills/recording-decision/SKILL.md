---
name: recording-decision
description: |
  設計判断・ADR (Architecture Decision Record) を構造化形式で記録するスキル。
  decision-keeper subagent を Task ツール経由で起動し、直近の議論から
  ADR を抽出して `.claude/agent-memory/decision-keeper/MEMORY.md` に
  追記する。
  Use when: 設計判断を確定した、トレードオフを伴う選択をした、
  後で「なぜそう決めたか」を参照可能にしたい時。
  Triggers: ADR 記録, 設計判断記録, decision-keeper, recording-decision,
  ADR を残す, 決定の記録, 方針確定の保存, architecture decision record
---

# Recording Decision Skill

設計判断 (ADR, Architecture Decision Record) を構造化形式で蓄積するスキル。
`agent-org` plugin の `decision-keeper` subagent を invoke することで実現する。

## 起動条件

以下のいずれかが該当する時:

- 設計判断・方針選択を確定し、後から「なぜそう決めたか」を辿れるようにしたい
- トレードオフを伴う選択をした (採用案 + 不採用案の理由を残したい)
- アーキテクチャの大きな決定 (技術選定、層分割、責任分担等) を確定した
- ユーザーが ADR / 決定の記録を明示的に求めた

逆に以下では起動しない:

- 一時的な実装方針 (PR 内で完結する細部・bugfix の選択肢)
- 既に ADR として記録済みの内容の再確認
- 単なる事実報告 (commit メッセージ等)

## 手順

1. **議論セグメントを特定する**
   - ADR 対象の議論範囲を決める (今 turn の決定 / 直近 10 turn の設計議論 等)
   - 複数の決定が混ざっている場合は ADR を分けて起動する

2. **既存 ADR の最大連番を確認する**
   - `.claude/agent-memory/decision-keeper/MEMORY.md` を Read して既存の最大連番を
     確認する (subagent prompt に渡して連番付与をスムーズにするため)
   - ファイルが存在しない場合は連番は 0 として渡す (subagent が ADR-001 を作る)

3. **`decision-keeper` subagent を Task ツールで invoke する**
   - `subagent_type: "agent-org:decision-keeper"` を指定 (plugin scoped name)
   - prompt には以下を渡す:
     - **主題** (1 行)
     - **context** (背景・制約 3-5 文)
     - **decision** (何をどう決めたか)
     - **alternatives_considered** (各 option + why_not)
     - **consequences** (positive / negative / neutral)
     - **deciders** (例: `[Mao, Claude Opus 4.7]`)
     - **date** (今日の日付)
     - **既存 ADR 連番の最大値** (Read で確認した値)
     - tags / retrieval_keys のヒント (subagent が最終決定する)

4. **結果を受けてメインセッションを継続**
   - decision-keeper が返した ADR id / status / 保存先パスをメインセッションに通知
   - 旧 ADR の `status: superseded_by:<新 id>` 更新がかかった場合はその旧 id も通知

## 入力プロンプト例

```
以下の設計判断を ADR として記録してください。

主題: PostCompact hook の入力 fallback 設計
date: 2026-05-13
deciders: [Mao, Claude Opus 4.7]

context:
PostCompact hook の入力には trigger と compact_summary の 2 フィールドが
含まれるが、manual /compact 直後など compact_summary が空になるケースが
ある。完全に compact_summary 依存にすると一部の compact で episode 化が
失敗する。

decision:
compact_summary を優先利用、空・欠落時は transcript_path を JSONL parse
して compact イベントを探す fallback 設計に倒す。

consequences:
  positive:
    - docs / 実機差分の両方に耐える
  negative:
    - transcript JSONL parse のコードが増える
  neutral: []

alternatives_considered:
  - option: compact_summary 単独依存
    why_not: 空ケースで episode 化が失敗する
  - option: transcript 単独 parse
    why_not: compact_summary に既に圧縮された情報を再度取りに行く無駄

retrieval_keys のヒント:
  - "PostCompact compact_summary transcript fallback"
  - "agent-org context-compressor 設計"

tags のヒント: [agent-org, hooks, postcompact]

既存 ADR 連番の最大値: 0 (新規)
```

## 注意事項

- **auto-inject は scoped name dir (`.claude/agent-memory/agent-org-decision-keeper/`)
  を見るため**、subagent は過去 ADR を auto-inject 経由では読めない (実機検証
  ADR-002 参照)。skill 経由で起動するときは必ず「既存 ADR 連番の最大値」を Read で
  取得して prompt に含めること。直接 Task invoke する場合も呼び出し側で過去情報を
  渡す責任がある
- decision-keeper subagent は **`agent-org:decision-keeper`** (scoped name) で起動
- ADR の重複登録を避けるため、subagent 側でも `MEMORY.md` を Read して既存 id を
  確認する規律が組み込まれている
- 値や秘密を含む議論を ADR 化する場合、prompt に投げる前にメインセッション側で
  実値を placeholder に置換する
- 1 ターンに ADR を複数記録するときは、ADR ごとに別 Task invoke する
  (連番が前の Task の結果に依存するため、並列ではなく直列で実行)

## 関連

- subagent 定義: `agents/decision-keeper.md`
- 横断参照スキル: `skills/consulting-memory/SKILL.md`
  (別 subagent から ADR を参照するときに使う)
- 上位 command: 現状なし (skill を直接呼び出すか、recording-decision を意識した
  会話の流れから自動起動される想定)
