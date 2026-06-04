---
name: researching-ai-sdk
description: |
  AI SDK (Vercel AI SDK) 公式ドキュメント調査スキル。ai-sdk.dev/llms.txt を段階的に読み込み、
  API仕様・使い方・コード例を調査する。Skill ツールで起動し、メインの会話コンテキストを消費しない。
  AI SDK の仕様確認には WebFetch ではなくこのスキルを使う（要約モデル経由ではないため field の抜け落とし・幻覚が起きない）。
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
  version: "3.2.0"
---

# AI SDK ドキュメント調査

ai-sdk.dev/llms.txt を唯一の権威ある情報源として段階的に調査する。
ドキュメントにない情報は「ドキュメントに記載なし」と明記すること。

## v3 互換性

v3 で `search` が推奨入口に統一された。旧フローの `search-index` → `sections` → `content` は引き続き動作するが非推奨。`search` 1 コマンドで title/desc/tags スコアリング + 本文 hits を取得できる。

## 調査フロー

推奨の 2 段階フロー: `search` で候補 + 本文 hits を 1 コマンドで取得 → `content` で必要セクション本文を読む。

```
  search (top N 候補 + 本文 hits 一括)       ← 推奨入口
        ↓
  content <doc_idx> "<heading_path>"        ← 該当セクションの本文
        ↑ (補助)
  sections <doc_idx>                        ← 見出し一覧を確認したいとき
        ↑ (深掘り)
  search-content --page-ref <doc_idx>       ← 特定 doc 内だけ本文検索
```

### Step 1: キーワードで候補ドキュメント + 本文 hits を取得（推奨入口）

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" search "<キーワード>"
```

スペース区切りで複数キーワード（AND）。未取得なら自動でネットワークから取得する。
title / description / tags / 見出しでスコアリングして上位 5 件（`--top-n N` で変更可）を選び、
各候補ドキュメントの body を keyword 検索して heading_path + スニペットを返す。
結果に表示される `[<doc_idx>]` は `content` / `sections` にそのまま渡せる。

### Step 2: 必要なセクションの本文を取得

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" content <page_ref> "<heading_path>"
```

`heading_path` を省略するとドキュメント全体を取得。

### 補助: セクション一覧を確認したいとき

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" sections <page_ref>
```

### 補助: 特定ドキュメント内だけ本文検索したいとき

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" search-content "<キーワード>" --page-ref <ref>
```

### フォールバック: 全ドキュメント一覧を確認

`search` で見つからない場合のみ使用する。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py" fetch-index --compact
```

---

## page_ref の指定方法

3 形式を受け付ける (claude-docs / firebase と統一):

- **整数 index** (推奨): `42` — `search` / `search-index` の結果に表示される `[<doc_idx>]` の数字
- **タイトル部分一致**: `"Event Callbacks"` — 一意に決まる場合のみ。曖昧な場合はエラーになる

AI SDK の llms.txt は URL を持たないため、URL / slug 形式は受け付けない (整数 index を使うこと)。

## コマンドリファレンス

| コマンド | 引数 | 説明 |
|---------|------|------|
| `search` | `<query> [--top-n N] [--max-hits N] [--context N] [--max-snippet-chars N]` | 推奨入口。title/desc/tags で top N 絞り込み + 本文 hits |
| `search-index` | `<query> [--file F] [--limit N] [--show-sections]` | title/description/tags/見出しで候補だけ取得 |
| `search-content` | `<query> [--page-ref REF] [--limit N] [--context N] [--max-hits N]` | 本文を横断キーワード検索、heading_path + スニペットを返す |
| `fetch-index` | `[--compact] [--cache-dir DIR]` | 全ドキュメント一覧を表示（フォールバック用） |
| `sections` | `<page_ref> [--file F] [--cache-dir DIR]` | 指定ドキュメントの見出し一覧を表示 |
| `content` | `<page_ref> [heading_path] [--file F] [--cache-dir DIR]` | セクション本文を表示 |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-ai-sdk.py`

すべてのサブコマンドで `--file <path>` 省略時は `--cache-dir`/`ai-sdk-llms.txt` を auto-fetch / 再利用する。

### heading_path の指定方法

- 見出しテキストそのまま: `"Project Setup"`
- スラッシュ区切りの階層パス: `"RAG Agent Guide/Build/Generate Chunks"`
- 部分一致（大文字小文字無視）で検索される

---

## 制約

- **全文読み込み禁止**: `search` → `content`、または `search-index` → `sections` → `content` の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **カスタムコンポーネント**: `<Snippet>`, `<Note>` 等の JSX 記法はテキストとして読む

## 失敗時の対処

| パターン | 症状 | 対処 |
|----------|------|------|
| キャッシュ期限切れ | 7 日超のキャッシュ | 自動 re-fetch (既定 `--max-age 604800`) |
| ネットワーク失敗 | fetch timeout / connection error | `--max-age 0` で cache 無視して再試行 |
| キャッシュ破損 | パースエラー / 不正なインデックス | `/tmp/ai-sdk-llms.txt` を削除して再実行 |
| 結果ゼロ | `No results found` | キーワードを変えて再試行。`fetch-index --compact` で一覧確認 |
| スクリプトエラー | Python traceback | 下記 WebFetch フォールバックへ |

### WebFetch フォールバック

スクリプトで解決できない場合のみ使用する:

1. `search` をキーワードを変えて 2-3 回試す
2. それでも失敗 → `ai-sdk.dev/docs/<slug>` を WebFetch で直接取得
3. WebFetch は要約モデル経由のため field の抜け落ちリスクあり — 取得内容を鵜呑みにしない

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
- 全文読み込みは禁止 — 必ず `search` → `content`、または `search-index` → `sections` → `content` の順で絞り込む
- 日本語で回答する
- スクリプト失敗時は「失敗時の対処」に従う。WebFetch は最終手段
- 調査は簡潔に完了させること

## リファレンス

- [llms-txt-structure.md](references/llms-txt-structure.md) — llms.txt の物理構造・frontmatter・見出しパターンの詳細
