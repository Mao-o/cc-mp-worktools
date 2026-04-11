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
  version: "1.1.0"
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

## 調査フロー

ページ数が膨大（~7000）なので **必ず search-index を入口にする**。
全件 `fetch-index` で list するのは非現実的。

```
  search-index (llms.txt, title/desc)       ← 必須の入口
        ↓
  [候補ページ (idx) を 2〜5 件特定]
        ↓ ──────────────────────┐
  sections <idx>          search-content --pages <idx,idx,...>
        ↓                         ↓  (本文キーワード検索、lazy fetch)
  content <idx> "<heading_path>"  heading_path が返る → content に渡す
```

### Step 1: キーワードでページを絞り込む（必須の入口）

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" search-index "<キーワード>"
```

スペース区切りで複数キーワード（AND）。7000 ページから title + description でスコアリングして上位 15 件程度を返す。

### Step 2a: 候補ページのセクション一覧を取得

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" sections <doc_index>
```

該当ページの `.md.txt` が未取得ならネットワークから自動取得し、
`/tmp/firebase-docs/<docs-path>-<hash>.md.txt` にキャッシュする。

### Step 2b: 候補ページの本文をキーワード横断検索

複数ページを横断して本文キーワードで絞りたい時:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" search-content "<キーワード>" --pages <idx1,idx2,idx3>
```

`--pages` は **search-index で絞った候補の doc_index をカンマ区切り**で指定する必須引数。
未 fetch のページは自動で取得する。Firebase は `llms-full.txt` がないため全ページ横断はできない —
必ず candidate を絞ってから使う。

### Step 3: 必要なセクションの本文を取得

```bash
# 特定セクション (heading_path または見出しテキストで指定)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" content <doc_index> "<heading_path>"

# ページ全体 (heading_path 省略)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" content <doc_index>
```

### Step 4: 複数ページが必要なら Step 2-3 を繰り返す

### フォールバック: pagination でインデックスを手動閲覧

search-index で適切な候補が出ない場合のみ使用する。

```bash
# 先頭 100 件
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" fetch-index

# 続きを表示
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-firebase.py" fetch-index --offset 100 --limit 100
```

`--limit 0` で全件表示できるが、7000 行近くなるため通常は避ける。

---

## コマンドリファレンス

| コマンド | 引数 | 説明 |
|---------|------|------|
| `search-index` | `<query> [--limit N] [--cache-dir DIR]` | title/description でキーワード検索（推奨入口） |
| `search-content` | `<query> --pages <idx,idx,...> [--limit N] [--context N] [--max-hits N] [--cache-dir DIR]` | 指定ページのみ本文横断キーワード検索（lazy fetch） |
| `fetch-index` | `[--offset N] [--limit N] [--cache-dir DIR]` | page index を paginated 表示（default --limit 100、フォールバック用） |
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
| 各ページ | `/tmp/firebase-docs/<docs-path>-<hash>.md.txt` |

例: `firebase.google.com/docs/reference/js/ai.imagenmodelparams.md.txt`
→ `/tmp/firebase-docs/docs_reference_js_ai.imagenmodelparams-<sha1>.md.txt`

最新版が必要な場合は該当ファイルを `rm` してから再実行する。

---

## 制約

- **全文読み込み禁止**: search-index → (sections / search-content) → content の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **テーブル保護**: Markdown テーブルの途中分割を自動防止する
- **on-demand fetch**: ページキャッシュは初回のみネットワークから取得 (sections / content / search-content 実行時)
- **search-content は候補ページ必須**: `--pages` で明示指定する (Firebase は llms-full.txt がないため全ページ横断不可)

## 禁止事項（効率の悪いフォールバックを避ける）

以下は本スキルのコマンドで代替できるため使わないこと:

- ❌ `grep -n <keyword> /tmp/firebase-llms.txt`
  → ✅ `search-index` を使う
- ❌ `fetch-index --limit 0 | grep ...` で全件 list を grep
  → ✅ `search-index` でスコアリング済みの候補を得る
- ❌ `Read /tmp/firebase-docs/<file>.md.txt (lines X-Y)` の直接行指定読み
  → ✅ `search-content --pages <idx>` で heading_path を取得してから `content` で取り出す

## ルール

- ドキュメントにない機能やオプションを捏造しない
- コード例はドキュメントから直接引用する
- 全文読み込みは禁止 — 必ず search-index → sections/search-content → content の順で絞り込む
- 7000 ページを盲目的に list しない (search-index を必ず入口にする)
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
