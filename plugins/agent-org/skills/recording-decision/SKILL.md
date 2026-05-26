---
name: recording-decision
description: |
  設計判断を ADR (Architecture Decision Record) として構造化記録するスキル。
  ADR の作成を依頼されたら、調査や事前確認なしに直接このスキルを invoke する
  こと — decision-keeper subagent が既存 ADR 確認・YAML 作成・MEMORY.md 索引
  追記を一貫して行う。
  Use proactively when: 設計判断を記録したい、ADR を作成したい、
  トレードオフを伴う決定を残したい時。
  Triggers: recording-decision, ADR 記録, ADR を作成, ADR を残す,
  設計判断記録, 決定の記録, この判断を記録, 方針確定の保存,
  architecture decision record, decision-keeper
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

2. **`decision-keeper` subagent を Task ツールで invoke する**
   - `subagent_type: "agent-org:decision-keeper"` を指定 (plugin scoped name)
   - prompt には以下を渡す:
     - **主題** (1 行)
     - **context** (背景・制約 3-5 文)
     - **decision** (何をどう決めたか)
     - **alternatives_considered** (各 option + why_not)
     - **consequences** (positive / negative / neutral)
     - **deciders** (例: `[Mao, Claude Opus 4.7]`)
     - **date** (今日の日付)
     - tags / retrieval_keys のヒント (subagent が最終決定する)
   - **既存 ADR 連番は subagent 側で auto-inject される MEMORY.md から取得する**
     ため、skill 側で渡す必要はない (ADR-003 で採用した scoped name dir 設計)

3. **結果を受けてメインセッションを継続**
   - decision-keeper が返した ADR id / status / 保存先パスをメインセッションに通知
   - 旧 ADR の `status: superseded_by:<新 id>` 更新がかかった場合はその旧 id も通知

4. **learnings_to_persist を `bd remember` で永続化する** (Phase 7+、v0.10.0)
   - decision-keeper は ADR 起草直後に「ADR 自体ではなく **ADR のメタ知見**」を
     会話出力 YAML として返す (詳細は `agents/decision-keeper.md` の
     `learnings_to_persist の curate 規律` section)
   - skill 側がこれを回収し、各行を `bd remember "decision-meta: <summary>"
     --key decision-meta-<slug>` で永続化する (ADR-010、`/run-review` /
     `/fix-regression` と同 pattern):

   ```bash
   REPO_ROOT="$(git rev-parse --show-toplevel)"

   # decision-keeper 会話出力の learnings_to_persist から各行を抽出して書込
   (cd "$REPO_ROOT" && bd remember "decision-meta: 公式 docs Warning が CLI 実装と矛盾する場合 CLI 側に倒す ADR は半年以内に supersede される傾向" \
     --key decision-meta-docs-vs-cli-pattern 2>/dev/null) || true
   (cd "$REPO_ROOT" && bd remember "decision-meta: retrieval_keys は『3 ヶ月後にこの問題に戻ったとき何を打つか』を起点に選ぶと grep 成功率が高い" \
     --key decision-meta-retrieval-keys-heuristic 2>/dev/null) || true
   ```

   - **key prefix は `decision-meta-` 固定** (ADR-010 規約)。同 key 再 invoke
     で update in place
   - **失敗許容**: `bd remember` 未サポート / 一時 error でも ADR 保存自体は
     完了させる (`|| true`、curate は best-effort)
   - **横断 retrieval**: `bd memories decision-meta` で list、`bd recall <key>`
     で個別 fetch (詳細は `consulting-memory` skill)
   - **無効化**: `bd forget <key>` で明示削除
   - **auto-inject**: `bd prime` default で memory は次セッションに inject される
     (`using-beads` skill 参照)

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
```

## 注意事項

- decision-keeper subagent は **`agent-org:decision-keeper`** (scoped name) で
  起動する。Claude Code は scoped name の `:` を `-` に置換して memory dir
  (`.claude/agent-memory/agent-org-decision-keeper/`) を解決する
- subagent は **auto-inject された MEMORY.md** から既存 ADR 連番カウンタを
  取得できるため、skill 側で「既存 ADR 連番の最大値」を Read で渡す必要は
  ない (ADR-003 で旧設計を supersede)
- ADR の重複登録を避けるため、subagent 側で auto-inject された MEMORY.md
  index を確認する規律が組み込まれている
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
