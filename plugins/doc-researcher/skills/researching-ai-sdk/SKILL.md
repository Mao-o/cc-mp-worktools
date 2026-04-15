---
name: researching-ai-sdk
description: |
  AI SDK (Vercel AI SDK) 公式ドキュメント調査スキル。ai-sdk.dev/llms.txt を段階的に読み込み、
  API仕様・使い方・コード例を調査する。独立コンテキストで実行されメインセッションを消費しない。
  Use proactively when implementing AI SDK features or needing AI SDK documentation.
  Use when implementing or debugging AI SDK features such as useChat, streamText, generateText, or any ai-sdk.dev API.
  Triggers: "AI SDK", "Vercel AI SDK", "ai-sdk", "streamText", "generateText",
  "useChat", "AI SDK ドキュメント", "researching-ai-sdk"
context: fork
model: sonnet
allowed-tools:
  - Read
  - Bash
  - WebFetch
metadata:
  author: mao
  version: "3.0.1"
---

# AI SDK ドキュメント調査

ai-sdk.dev/llms.txt を唯一の権威ある情報源として段階的に調査する。
ドキュメントにない情報は「ドキュメントに記載なし」と明記すること。

## 調査手順

### Step 1: キーワード検索で対象ドキュメントを特定する

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" search /tmp/ai-sdk-llms.txt "<キーワード>"
```

スペース区切りで複数キーワードを指定可能（AND検索）。ファイルが未取得なら自動でネットワークから取得する。

オプション:
- `--limit N` — 最大表示件数（デフォルト: 15）
- `--show-sections` — マッチしたドキュメントの H1/H2 見出しも表示

### Step 2: セクション一覧を取得

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" sections /tmp/ai-sdk-llms.txt <doc_index>
```

### Step 3: 必要なセクションの本文を取得

```bash
# 特定セクションを取得（heading_path または見出しテキストで指定）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" content /tmp/ai-sdk-llms.txt <doc_index> "<heading_path>"

# ドキュメント全体を取得（heading_path 省略）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" content /tmp/ai-sdk-llms.txt <doc_index>
```

### Step 4: 複数ドキュメントが必要なら Step 2-3 を繰り返す

### フォールバック: 全ドキュメント一覧を確認

search で見つからない場合のみ使用する。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" fetch-index --compact
```

`--compact` を省略すると description/tags 付きの詳細表示になる。

---

## コマンドリファレンス

| コマンド | 引数 | 説明 |
|---------|------|------|
| `search` | `<file> <query> [--limit N] [--show-sections]` | キーワードでドキュメントを検索（推奨） |
| `fetch-index` | `[--compact] [--cache-dir DIR]` | 全ドキュメント一覧を表示（フォールバック用） |
| `sections` | `<file> <doc_index>` | 指定ドキュメントの見出し一覧を表示 |
| `content` | `<file> <doc_index> [heading_path]` | セクション本文を表示 |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py`

### heading_path の指定方法

- 見出しテキストそのまま: `"Project Setup"`
- スラッシュ区切りの階層パス: `"RAG Agent Guide/Build/Generate Chunks"`
- 部分一致（大文字小文字無視）で検索される

### キャッシュ

- `/tmp/ai-sdk-llms.txt` にキャッシュされる（セッション内で再利用）
- `search` はファイル未存在時に自動取得する
- 最新版が必要な場合: `rm /tmp/ai-sdk-llms.txt` してから再実行

---

## 制約

- **全文読み込み禁止**: 必ず search → sections → content の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **カスタムコンポーネント**: `<Snippet>`, `<Note>` 等の JSX 記法はテキストとして読む

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
- 全文読み込みは禁止 — 必ず search → sections → content の順で絞り込む
- 日本語で回答する
- `/tmp/ai-sdk-llms.txt` が存在しない場合は search が自動取得する。それでも失敗する場合は WebFetch で取得する
- 調査は簡潔に完了させること

## リファレンス

- [llms-txt-structure.md](references/llms-txt-structure.md) — llms.txt の物理構造・frontmatter・見出しパターンの詳細
