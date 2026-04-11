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
  version: "3.1.0"
---

# AI SDK ドキュメント調査

ai-sdk.dev/llms.txt を唯一の権威ある情報源として段階的に調査する。
ドキュメントにない情報は「ドキュメントに記載なし」と明記すること。

## 調査フロー

```
  search-index (title/desc/tags)        ← 軽量: ドキュメントを絞り込む
        ↓
  [ドキュメント特定できた？]
     yes ↓       no → search-content (本文横断) ← 本文キーワードで追加絞り込み
  sections                ↓
        ↓           該当セクションを取得
  content (heading_path 指定)
```

### Step 1a: キーワードでドキュメントを絞り込む（推奨の入り口）

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" search-index /tmp/ai-sdk-llms.txt "<キーワード>"
```

スペース区切りで複数キーワード（AND）。未取得なら自動でネットワークから取得する。
title / description / tags / 見出しを対象にスコアリングする。

### Step 1b: 本文キーワードで横断検索（search-index で絞りきれないとき）

```bash
# 全ドキュメント横断
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" search-content /tmp/ai-sdk-llms.txt "<キーワード>"

# 特定ドキュメント内のみ
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" search-content /tmp/ai-sdk-llms.txt "<キーワード>" --doc-index <idx>
```

セクション単位で AND 検索し、ヒット箇所の heading_path と前後数行のスニペットを返す。
そのまま Step 3 の `content` コマンドに heading_path を渡せる。

### Step 2: セクション一覧を取得

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" sections /tmp/ai-sdk-llms.txt <doc_index>
```

### Step 3: 必要なセクションの本文を取得

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" content /tmp/ai-sdk-llms.txt <doc_index> "<heading_path>"
```

`heading_path` を省略するとドキュメント全体を取得。

### フォールバック: 全ドキュメント一覧を確認

search-index / search-content で見つからない場合のみ使用する。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" fetch-index --compact
```

---

## コマンドリファレンス

| コマンド | 引数 | 説明 |
|---------|------|------|
| `search-index` | `<file> <query> [--limit N] [--show-sections]` | title/description/tags/見出しでドキュメントを絞り込む（推奨入口） |
| `search-content` | `<file> <query> [--doc-index N] [--limit N] [--context N] [--max-hits N]` | 本文を横断キーワード検索、heading_path + スニペットを返す |
| `fetch-index` | `[--compact] [--cache-dir DIR]` | 全ドキュメント一覧を表示（フォールバック用） |
| `sections` | `<file> <doc_index>` | 指定ドキュメントの見出し一覧を表示 |
| `content` | `<file> <doc_index> [heading_path]` | セクション本文を表示 |
| `search` | (alias of `search-index`) | 後方互換用エイリアス |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py`

### heading_path の指定方法

- 見出しテキストそのまま: `"Project Setup"`
- スラッシュ区切りの階層パス: `"RAG Agent Guide/Build/Generate Chunks"`
- 部分一致（大文字小文字無視）で検索される

### キャッシュ

- `/tmp/ai-sdk-llms.txt` にキャッシュされる（セッション内で再利用）
- `search-index` / `search-content` はファイル未存在時に自動取得する
- 最新版が必要な場合: `rm /tmp/ai-sdk-llms.txt` してから再実行

---

## 制約

- **全文読み込み禁止**: search-index → sections → content、または search-content → content の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **カスタムコンポーネント**: `<Snippet>`, `<Note>` 等の JSX 記法はテキストとして読む

## 禁止事項（効率の悪いフォールバックを避ける）

以下は本スキルのコマンドで代替できるため使わないこと:

- ❌ `grep -n <keyword> /tmp/ai-sdk-llms.txt`
  → ✅ `search-index` / `search-content` を使う
- ❌ `cat /tmp/ai-sdk-llms.txt | head` や `Read /tmp/ai-sdk-llms.txt (lines X-Y)` の直接読み
  → ✅ `sections` で見出しを特定してから `content` で取り出す
- ❌ `fetch-index` の出力を Bash で grep して絞り込む
  → ✅ `search-index` でスコアリング済みの候補を得る

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
- 全文読み込みは禁止 — 必ず search-index/search-content → sections → content の順で絞り込む
- 日本語で回答する
- `/tmp/ai-sdk-llms.txt` が存在しない場合は search-* が自動取得する。それでも失敗する場合は WebFetch で取得する
- 調査は簡潔に完了させること

## リファレンス

- [llms-txt-structure.md](references/llms-txt-structure.md) — llms.txt の物理構造・frontmatter・見出しパターンの詳細
