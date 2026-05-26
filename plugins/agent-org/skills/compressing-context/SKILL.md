---
name: compressing-context
description: |
  直近の会話セグメントを構造化 episode YAML に圧縮して
  `.claude/episodes/<id>.yaml` に保存する。context-compressor subagent を
  Task ツール経由で起動する。
  Use when: コンテキストが膨らんできて compact したいが構造化して残したい /
  長い議論の節目を episode 化したい / 単発の調査結果を再利用可能な形で
  蓄積したい。
  Triggers: コンテキスト圧縮, episode に保存, /compress-context,
  context-compressor, compressing-context
---

# Compressing Context Skill

直近の会話を構造化された episode YAML に圧縮するスキル。
`agent-org` plugin の `context-compressor` subagent を invoke することで実現する。

## 起動条件

以下のいずれかが該当する時:

- メインセッションのコンテキストが膨らんできて、要点だけ残して圧縮したい
- 長い議論の区切り（フェーズ完了、PR マージ、設計確定等）で節目を保存したい
- 単発の調査結果や決定事項を再利用可能な形で蓄積したい
- ユーザーが `/compress-context` を実行した

## 手順

1. **直近の会話セグメントを特定する**
   - 圧縮対象の範囲を決める（前回 `/compress-context` 実行以降 / 直近 30 turn /
     特定のトピック等、ユーザー指定または自然な区切り）

2. **`context-compressor` subagent を Task ツールで invoke する**
   - `subagent_type: "agent-org:context-compressor"` を指定 (plugin scoped name)
   - prompt には以下を渡す:
     - 圧縮対象セグメントの概要 (主題・期間・主な議論点)
     - 出力先 (`.claude/episodes/<id>.yaml`)
     - `trigger: manual`、`source.type: manual_compress`、`source.trigger: user_request` を指定
   - context-compressor は独立コンテキストで動作し、過去の圧縮戦略は
     auto-inject される MEMORY.md
     (`.claude/agent-memory/agent-org-context-compressor/MEMORY.md`) から
     参照する

3. **結果を受けてメインセッションを継続**
   - context-compressor が返した episode の id / topic / 保存先パスを
     メインセッションに通知
   - 圧縮済みなので、メインセッションから該当議論の詳細を忘れて続行可能

## 出力 episode の例

```yaml
episode:
  id: 2026-05-13T03-45-00Z
  trigger: manual
  topic: agent-org plugin Phase 1 設計の確定
  decisions:
    - "context-compressor は memory: project, model: haiku を採用"
    - "PostCompact hook で compact_summary 優先 + transcript fallback"
    - "Phase 1 では .claude/agent-org/ は使わない、.claude/episodes/ のみ"
  artifacts_changed:
    - path: plugins/agent-org/agents/context-compressor.md
      summary: "新規 subagent 定義"
  unresolved:
    - "Phase 1 の plugin subagent memory path 実機検証"
  retrieval_keys:
    - "agent-org Phase 1 context-compressor 設計"
    - "PostCompact compact_summary fallback"
    - "subagent memory project scope plugin"
  source:
    type: manual_compress
    trigger: user_request
  source_summary: |
    Phase 1 で context-compressor subagent + /compress-context skill +
    PostCompact hook を実装する設計を確定した。memory: project、model: haiku
    で軽量化、PostCompact 入力の compact_summary が空でも transcript_path
    fallback で動作するように設計。
```

## 注意事項

- subagent は **`agent-org:context-compressor`** (plugin scoped name) で起動する。
  Claude Code は scoped name の `:` を `-` に置換して memory dir
  (`.claude/agent-memory/agent-org-context-compressor/`) を解決する
- 値や秘密の文字列を含む議論を圧縮する場合、context-compressor は値そのものを
  記録しないように指示されているが、メインセッション側でも投げる prompt に
  注意する
- 同一タイムスタンプの id が衝突した場合、context-compressor が suffix を付ける
  (`-2.yaml`, `-3.yaml`)

## 関連

- subagent 定義: `agents/context-compressor.md`
- 自動圧縮経路: `hooks/postcompact-episode.sh` (PostCompact hook 経由)
- episode 検索: `.claude/episodes/*.yaml` を grep
