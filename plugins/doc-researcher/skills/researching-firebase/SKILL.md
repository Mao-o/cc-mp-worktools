---
name: researching-firebase
description: |
  Firebase 公式ドキュメント調査スキル。firebase.google.com/docs/llms.txt を段階的に読み込み、
  Firestore, Authentication, Cloud Functions, Hosting, FCM, Vertex AI in Firebase などの
  各プロダクトの API 仕様・設定方法・コード例を調査する。独立コンテキストで実行されメインセッションを消費しない。
  Use proactively when implementing Firebase features or needing Firebase documentation.
  Use when implementing or debugging Firebase features such as Firestore queries, Auth flows,
  Cloud Functions, FCM, Cloud Storage for Firebase, App Check, or Firebase AI.
  Triggers: "Firebase", "Firestore", "Firebase Auth", "Firebase ドキュメント",
  "firebase.google.com", "FCM", "Cloud Functions for Firebase", "Firebase AI",
  "Vertex AI in Firebase", "researching-firebase"
context: fork
model: sonnet
allowed-tools:
  - Read
  - Bash
  - WebFetch
metadata:
  author: mao
  version: "1.0.1"
---

# Firebase ドキュメント Progressive Loader

Firebase 公式ドキュメント (`firebase.google.com/docs`) を段階的に読み込むスキル。
他の researching-* スキルと異なり、**llms-full.txt が存在しない**ため、個別の `.md.txt`
ページを on-demand で fetch する設計になっている。

## 規模

| 項目 | 値 |
|------|----|
| index 行数 | ~7000 |
| ページ数 | ~6970 (Android/iOS/JS/C++ 各 SDK の API reference を含む) |
| インデックスサイズ | ~1.8MB |
| キャッシュ | `/tmp/firebase-llms.txt` (index) + `/tmp/firebase-docs/` (per-page) |

## 調査手順

### Step 1: インデックスを取得して対象ページを特定する

```bash
# 先頭 100 件 (デフォルト)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" fetch-index

# 続きを表示 (pagination)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" fetch-index --offset 100 --limit 100

# 全件表示 (注意: 7000 行近くなる)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" fetch-index --limit 0
```

ページ数の大半は Android / iOS / JS / C++ SDK の API reference。
最初は default 100 件で全体感をつかみ、必要に応じて pagination で広げる。
`grep` で目的のキーワードを含む行に絞ってから `[index]` を取得すると効率的:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" fetch-index --limit 0 \
  | grep -i 'firestore'
```

### Step 2: セクション一覧を取得 (該当ページを on-demand fetch)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" sections <doc_index>
```

該当ページの `.md.txt` が未取得ならネットワークから自動取得し、
`/tmp/firebase-docs/<docs-path>.md.txt` にキャッシュする。

### Step 3: 必要なセクションの本文を取得

```bash
# 特定セクション (heading_path または見出しテキストで指定)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" content <doc_index> "<heading_path>"

# ページ全体 (heading_path 省略)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" content <doc_index>
```

### Step 4: 複数ページが必要なら Step 2-3 を繰り返す

---

## コマンドリファレンス

| コマンド | 引数 | 説明 |
|---------|------|------|
| `fetch-index` | `[--offset N] [--limit N] [--cache-dir DIR]` | llms.txt から page index を取得 (paginated, default --limit 100) |
| `sections` | `<doc_index> [--cache-dir DIR]` | 指定ページの見出し一覧を表示 (該当ページを auto-fetch) |
| `content` | `<doc_index> [heading_path] [--cache-dir DIR]` | セクション本文を表示 (該当ページを auto-fetch) |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py`

### heading_path の指定方法

- 見出しテキストそのまま: `"Properties"`
- スラッシュ区切りの階層パス: `"Properties/generationConfig"`
- 部分一致 (大文字小文字無視) で検索される

reference ページの多くは H2 のみのフラット構造、guide ページは H2/H3 の階層を持つ。

### キャッシュ

| 種別 | パス |
|------|------|
| index | `/tmp/firebase-llms.txt` |
| 各ページ | `/tmp/firebase-docs/<docs-path>.md.txt` |

例: `firebase.google.com/docs/reference/js/ai.imagenmodelparams.md.txt`
→ `/tmp/firebase-docs/docs_reference_js_ai.imagenmodelparams.md.txt`

最新版が必要な場合は該当ファイルを `rm` してから再実行する。

---

## 制約

- **全文読み込み禁止**: 必ず fetch-index → sections → content の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **テーブル保護**: Markdown テーブルの途中分割を自動防止する
- **on-demand fetch**: ページキャッシュは初回のみネットワークから取得 (sections / content 実行時)

## ルール

- ドキュメントにない機能やオプションを捏造しない
- コード例はドキュメントから直接引用する
- 全文読み込みは禁止 — 必ず fetch-index → sections → content の順で絞り込む
- 7000 ページを盲目的に list しない (default `--limit 100` を尊重し、必要に応じ pagination する)
- 日本語で回答する
- ページ取得に失敗した場合のみ WebFetch fallback を検討する
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
