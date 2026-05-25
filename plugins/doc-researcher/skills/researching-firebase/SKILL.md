---
name: researching-firebase
description: |
  Firebase 公式ドキュメント調査スキル。firebase.google.com/docs/llms.txt を段階的に読み込み、
  Firestore, Authentication, Cloud Functions, Hosting, FCM, Vertex AI in Firebase などの
  各プロダクトの API 仕様・設定方法・コード例を調査する。独立コンテキストで実行されメインセッションを消費しない。
  Firebase の仕様確認には WebFetch ではなくこのスキルを使う（要約モデル経由ではないため field の抜け落とし・幻覚が起きない）。
  Use proactively when implementing Firebase features or needing Firebase documentation.
  Use when implementing or debugging Firebase features such as Firestore queries, Auth flows,
  Cloud Functions, FCM, Cloud Storage for Firebase, App Check, or Firebase AI.
  Triggers: "Firebase", "Firestore", "Firebase Auth", "Firebase ドキュメント",
  "firebase.google.com", "FCM", "Cloud Functions for Firebase", "Firebase AI",
  "Vertex AI in Firebase", "Cloud Storage for Firebase", "Cloud Storage",
  "Realtime Database", "Firebase Hosting", "App Check", "Remote Config",
  "Crashlytics", "Dynamic Links", "A/B Testing", "Performance Monitoring",
  "Test Lab", "researching-firebase"
context: fork
model: sonnet
allowed-tools:
  - Read
  - Bash
  - WebFetch
metadata:
  author: mao
  version: "2.0.0"
---

# Firebase ドキュメント Progressive Loader

Firebase 公式ドキュメント (`firebase.google.com/docs`) を段階的に読み込むスキル。
他の researching-* スキルと異なり、**llms-full.txt が存在しない**ため、個別の `.md.txt`
ページを on-demand で fetch する設計になっている。

## v2 互換性

v2 で `search` が推奨入口に統一された。旧フローの `search-index` → `sections` → `content` は引き続き動作するが非推奨。`search` 1 コマンドで候補ページ + 本文 hits を取得できる。

## 規模

| 項目 | 値 |
|------|----|
| index 行数 | ~7000 |
| ページ数 | ~6970 (Android/iOS/JS/C++ 各 SDK の API reference を含む) |
| インデックスサイズ | ~1.8MB |
| キャッシュ | `/tmp/firebase-llms.txt` (index) + `/tmp/firebase-docs/` (per-page) |

## 調査フロー

推奨の 2 段階フロー: `search` で候補 + 本文 hits を 1 コマンドで取得 → `content` で必要セクション本文を読む。
`search` は llms.txt の上位候補 N 件 (default 5) を on-demand fetch するため、cache hit 後は高速。

```
  search (top N 候補 + on-demand fetch + 本文 hits)   ← 推奨入口
        ↓
  content <page_ref> "<heading_path>"                ← 該当セクションの本文
        ↑ (補助)
  sections <page_ref>                                ← 見出し一覧を確認したいとき
        ↑ (深掘り)
  search-content --page-ref <ref>                    ← 特定ページ内だけ本文検索
```

### Step 1: キーワードで候補ページ + 本文 hits を取得（推奨入口）

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" search "<キーワード>"
```

スペース区切りで複数キーワード（AND）。
title + description でスコアリングして上位 5 件（`--top-n N` で変更可）を選び、
各候補ページを on-demand fetch して本文を keyword 検索、heading_path + スニペットを返す。
結果に表示される `[<doc_idx>]` は `content` / `sections` にそのまま渡せる。

初回は top N 件分の HTTP fetch が走るため数秒～十数秒かかる。2 回目以降は cache hit で高速。

### Step 2: 必要なセクションの本文を取得

```bash
# 特定セクション (heading_path で指定)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" content <page_ref> "<heading_path>"

# ページ全体 (heading_path 省略)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" content <page_ref>
```

### 補助: セクション一覧を確認したいとき

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" sections <page_ref>
```

### 補助: 特定ページ内だけ本文検索したいとき

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" search-content "<キーワード>" --page-ref <ref>
```

`--page-ref` を省略すると全ページ横断 fetch (7000 ページ近い、初回は非常に重い) になるため、
明示指定を推奨する。

### フォールバック: pagination でインデックスを手動閲覧

`search` で適切な候補が出ない場合のみ使用する。

```bash
# 先頭 100 件
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" fetch-index

# 続きを表示
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" fetch-index --offset 100 --limit 100
```

`--limit 0` で全件表示できるが、7000 行近くなるため通常は避ける。

---

## page_ref の指定方法

3 形式を受け付ける (claude-docs / ai-sdk と統一):

- **整数 index** (推奨): `42` — `search` / `search-index` の結果に表示される `[<doc_idx>]` の数字
- **URL slug**: `"vector-search"` — Firebase docs の URL 末尾 path component。一意に決まる場合のみ
- **完全 URL**: `"https://firebase.google.com/docs/firestore/vector-search"`

## コマンドリファレンス

| コマンド | 引数 | 説明 |
|---------|------|------|
| `search` | `<query> [--top-n N] [--max-hits N] [--context N] [--max-snippet-chars N]` | 推奨入口。title/desc 上位 N 件 fetch + 本文 hits |
| `search-index` | `<query> [--limit N]` | title/description でキーワード検索（候補だけ取得） |
| `search-content` | `<query> [--page-ref REF] [--limit N] [--context N] [--max-hits N]` | 指定ページ (省略時は全ページ) の本文を横断検索 |
| `fetch-index` | `[--offset N] [--limit N]` | page index を paginated 表示（default --limit 100、フォールバック用） |
| `sections` | `<page_ref>` | 指定ページの見出し一覧を表示 (該当ページを auto-fetch) |
| `content` | `<page_ref> [heading_path]` | セクション本文を表示 (該当ページを auto-fetch) |

すべて `--cache-dir DIR` を受け付ける (default: `/tmp`)。
スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py`

### heading_path の指定方法

- 見出しテキストそのまま: `"Properties"`
- スラッシュ区切りの階層パス: `"Properties/generationConfig"`
- 部分一致 (大文字小文字無視) で検索される

reference ページの多くは H2 のみのフラット構造、guide ページは H2/H3 の階層を持つ。

---

## 制約

- **全文読み込み禁止**: `search` → `content`、または `search-index` → `sections`/`search-content` → `content` の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **テーブル保護**: Markdown テーブルの途中分割を自動防止する
- **on-demand fetch**: ページキャッシュは初回のみネットワークから取得 (sections / content / search-content / search 実行時)
- **search-content の全ページ横断は重い**: `--page-ref` 省略すると 7000 ページ近い HTTP fetch を発火する (初回のみ)。明示指定を推奨

## 失敗時の対処

| パターン | 症状 | 対処 |
|----------|------|------|
| ネットワーク失敗 | fetch timeout / connection error | `--max-age 0` で cache 無視して再試行 |
| キャッシュ破損 | パースエラー / 不正なインデックス | `/tmp/firebase-llms.txt` と `/tmp/firebase-docs/` を削除して再実行 |
| 結果ゼロ | `No results found` | キーワードを変えて再試行。`fetch-index` で一覧確認 |
| スクリプトエラー | Python traceback | 下記 WebFetch フォールバックへ |

### WebFetch フォールバック

スクリプトで解決できない場合のみ使用する:

1. `search` をキーワードを変えて 2-3 回試す
2. それでも失敗 → `https://firebase.google.com/docs/<product>` を WebFetch で直接取得
3. WebFetch は要約モデル経由のため field の抜け落ちリスクあり — 取得内容を鵜呑みにしない

## ルール

- ドキュメントにない機能やオプションを捏造しない
- コード例はドキュメントから直接引用する
- 全文読み込みは禁止 — 必ず `search` → `content`、または `search-index` → `sections`/`search-content` → `content` の順で絞り込む
- 7000 ページを盲目的に list しない (`search` を必ず入口にする)
- 日本語で回答する
- スクリプト失敗時は「失敗時の対処」に従う。WebFetch は最終手段
- 調査は簡潔に完了させること

## 出力フォーマット

### 調査結果
[主な発見事項]

### コード例 *(該当する場合)*
[ドキュメントからの直接引用のみ]

### 情報源
[使用したドキュメントのタイトルとセクション]

### 注意事項 *(該当する場合)*
[制約、バージョン要件、既知の問題]
