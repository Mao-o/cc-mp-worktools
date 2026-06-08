# ai-sdk.dev/llms-full.txt の構造

## 概要

`https://ai-sdk.dev/llms-full.txt` は AI SDK の公式ドキュメントを LLM 向けに集約した単一テキストファイル。
約 150,000 行 / 5MB / 530 ドキュメントで構成される（2026-06-08 時点）。

上流の `llms.txt` は ~2KB / 46 行のインデックスファイルに分離されており、本体は `llms-full.txt` に
収録される構成へ変更された。`parse-ai-sdk.py` は `llms-full.txt` を直接 fetch する。

ファイル先頭には `---` で始まらない contributing guide (TypeScript コード) ブロックが存在するが、
`split_documents` は最初の `---` までを無視するため、ドキュメント分割には影響しない。

## 物理構造

見かけ上は1本の巨大テキストだが、実体は **YAML frontmatter 付き Markdown ドキュメントの連結**。

```
---
title: Document Title
description: Short description
tags: [tag1, tag2, tag3]
---
# Heading

Body text...

---
title: Next Document
...
```

## frontmatter フィールド

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|:---:|------|
| `title` | string | ✅ | ドキュメントタイトル |
| `description` | string | ほぼ | 簡潔な説明文 |
| `tags` | string[] | 任意 | `[tag1, tag2]` 形式のタグ配列 |

一部のドキュメントには `tags` がない場合がある。

## 見出し構造

- 見出しレベル `#` 〜 `####` が使用される
- **レベルの飛びが頻発**: `#` の直下に `###` が来ることがある（`##` をスキップ）
- 見出し階層を機械的に仮定してはならない

## コードブロック

- `tsx`, `ts`, `json`, `bash` 等の言語タグ付き fenced code block が大量に存在
- `filename` 属性付きのコードブロックあり: `` ```tsx filename="lib/ai/embedding.ts" ``
- コードブロックの途中で分割すると意味が破壊される

## カスタムコンポーネント

Markdown 内に JSX 風のカスタムコンポーネントが含まれる:

- `<Snippet text="command" />` — コマンドスニペット
- `<Note>...</Note>` — 注記
- その他の React コンポーネント記法

これらは Markdown パーサーでは解釈できないが、テキストとしては読める。

## ドキュメントのカテゴリ

| カテゴリ | doc_index 範囲（目安） | 内容 |
|---------|----------------------|------|
| チュートリアル | 0-5 | RAG Agent, Multi-Modal Agent 等の実践ガイド |
| Getting Started | 6-17 | 各モデルプロバイダーの導入ガイド |
| Guides | 18-30 | API サーバー、ストリーミング、エラー処理等 |
| API Reference | 30-100 | generateText, streamText, useChat 等のAPI詳細 |
| Providers | 100+ | OpenAI, Anthropic, Google 等のプロバイダー設定 |

※ インデックス範囲は llms-full.txt の更新により変動する。`fetch-index` で最新を確認すること。
