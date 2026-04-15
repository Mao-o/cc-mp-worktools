---
name: researching-claude-docs
description: |
  Claude 公式ドキュメント（Claude Code + Claude Developer Platform）を段階的に読み込み、
  必要な部分だけを取得するスキル。--source フラグで対象を切り替える。
  Claude Code: Hooks、Skills、Settings、MCP、サブエージェント等の Claude Code 機能。
  Platform: Claude API、Agent SDK、Tool Use、Prompt Engineering 等の開発者向け機能。
  Triggers: "Claude Code docs", "Claude API docs", "公式ドキュメント", "researching-claude-docs",
  "Claude Code 仕様", "code.claude.com", "platform.claude.com", "Claude Platform docs"
context: fork
model: sonnet
allowed-tools:
  - Read
  - Bash
  - WebFetch
metadata:
  author: mao
  version: "2.0.1"
---

# Claude ドキュメント Progressive Loader

Claude Code (`code.claude.com`) および Claude Developer Platform (`platform.claude.com`) の公式ドキュメントを段階的に読み込むスキル。

## ソース一覧

| ソース | `--source` | ドキュメント | 規模 |
|--------|-----------|-------------|------|
| Claude Code | `code` (デフォルト) | code.claude.com/docs | ~64p / 1.4MB |
| Claude Developer Platform | `platform` | platform.claude.com/docs | ~617p / 23.8MB |

## 調査手順

### Step 1: インデックスを取得して対象ページを特定する

```bash
# Claude Code（デフォルト）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" fetch-index

# Claude Developer Platform
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" fetch-index --source platform
```

出力されたページ一覧（タイトル + 説明文）から、調査対象に関連する `[index]` を特定する。
このステップでは軽量な `llms.txt` のみを取得する。全文 `llms-full.txt` は Step 2 初回実行時に自動取得される。

### Step 2: セクション一覧を取得

```bash
# Claude Code
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" sections /tmp/claude-code-llms-full.txt <doc_index>

# Claude Developer Platform
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" sections /tmp/claude-platform-llms-full.txt <doc_index>
```

### Step 3: 必要なセクションの本文を取得

```bash
# 特定セクションを取得（heading_path または見出しテキストで指定）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <file> <doc_index> "<heading_path>"

# ページ全体を取得（heading_path 省略）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <file> <doc_index>
```

### Step 4: 複数ページが必要なら Step 2-3 を繰り返す

---

## コマンドリファレンス

| コマンド | 引数 | 説明 |
|---------|------|------|
| `fetch-index` | `[--source {code,platform}] [--cache-dir DIR]` | 軽量 llms.txt を取得してページ一覧を表示 |
| `sections` | `<file> <doc_index>` | 指定ページの見出し一覧を表示 |
| `content` | `<file> <doc_index> [heading_path]` | セクション本文を表示 |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py`

### heading_path の指定方法

- 見出しテキストそのまま: `"Configuration"`
- スラッシュ区切りの階層パス: `"Hook events/PreToolUse/PreToolUse input"`
- 部分一致（大文字小文字無視）で検索される

### キャッシュ（2段階 × 2ソース）

| ソース | インデックス | 全文 |
|--------|-------------|------|
| Claude Code | `/tmp/claude-code-llms.txt` | `/tmp/claude-code-llms-full.txt` |
| Platform | `/tmp/claude-platform-llms.txt` | `/tmp/claude-platform-llms-full.txt` |

- 最新版が必要な場合: `rm /tmp/claude-code-llms*.txt` または `rm /tmp/claude-platform-llms*.txt` してから再実行

---

## 制約

- **全文読み込み禁止**: 必ず fetch-index → sections → content の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **テーブル保護**: Markdown テーブルの途中分割を自動防止する
- **カスタムコンポーネント**: `<Note>`, `<Frame>`, `<Expandable>`, `<Card>` 等の JSX 記法はテキストとして読む

## 出力フォーマット

### 調査結果
[主な発見事項]

### コード例 *(該当する場合)*
[ドキュメントからの直接引用のみ]

### 情報源
[使用したドキュメントのタイトルとセクション]

### 注意事項 *(該当する場合)*
[制約、バージョン要件、既知の問題]

## ルール

- ドキュメントにない機能やオプションを捏造しない
- コード例はドキュメントから直接引用する
- 全文読み込みは禁止 — 必ず fetch-index → sections → content の順で絞り込む
- `--source` フラグを明示する (code / platform のどちらを調査しているか明確にする)
- 日本語で回答する
- ページ取得に失敗した場合のみ WebFetch fallback を検討する
- 調査は簡潔に完了させること
