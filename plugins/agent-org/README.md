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

Phase 2 以降で `decision-keeper` / `architect-reviewer` / `regression-watcher` /
`regression-fixer` を追加予定。設計の全体像は `docs/ARCHITECTURE.md` 参照。

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

context-compressor は `memory: project` で `.claude/agent-memory/context-compressor/`
に永続学習を蓄積する。「どの content type にはどの粒度の圧縮が効いたか」を
セッション横断で学んでいく設計。

## 依存

- Claude Code v2.1.33 以上 (subagent `memory` frontmatter)
- 通常実行は標準ライブラリのみ。`jq` (PostCompact hook 内で使用)

## 関連

- 全体プラン: `~/.claude/plans/worktools-agent-org-plugin-cooperative-lamport.md`
- Phase 1 実装プラン: `~/.claude/plans/ticklish-gliding-scone.md` (この PR の根拠)
