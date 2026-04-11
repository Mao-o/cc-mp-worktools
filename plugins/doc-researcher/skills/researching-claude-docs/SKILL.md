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
  version: "2.1.0"
---

# Claude ドキュメント Progressive Loader

Claude Code (`code.claude.com`) および Claude Developer Platform (`platform.claude.com`) の公式ドキュメントを段階的に読み込むスキル。

## ソース一覧

| ソース | `--source` | ドキュメント | 規模 |
|--------|-----------|-------------|------|
| Claude Code | `code` (デフォルト) | code.claude.com/docs | ~64p / 1.4MB |
| Claude Developer Platform | `platform` | platform.claude.com/docs | ~617p / 23.8MB |

## 調査フロー

2 つの入口から選ぶ。ページが 1 つに特定できそうなら **search-index**、
本文の具体的なキーワードで絞り込みたいなら **search-content** を使う。

```
  search-index (llms.txt, title/desc)       ← 軽量: ページを絞り込む
        ↓                                         ↓
  [ページ特定できた？]                 [複数ページ横断で本文検索したい]
     yes ↓                                       ↓
  sections (llms-full.txt)            search-content (llms-full.txt)
        ↓                                         ↓
  content (heading_path 指定)        content (heading_path 指定)
```

### Step 1a: 軽量インデックスでページを絞り込む

```bash
# Claude Code（デフォルト）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search-index "<キーワード>"

# Claude Developer Platform
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search-index "<キーワード>" --source platform
```

`llms.txt` のタイトル + description をスコアリング。件数が多い Platform では必須の入口。

### Step 1b: 本文キーワードで横断検索（トピック横断の時）

```bash
# 全ページ横断
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search-content /tmp/claude-code-llms-full.txt "<キーワード>"

# 特定ページ内のみ
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search-content /tmp/claude-code-llms-full.txt "<キーワード>" --doc-index <idx>
```

セクション単位で AND 検索。ヒット箇所の heading_path と前後スニペットを返す。
`<キーワード>` は 2〜3 語のスペース区切り（例: `"hook matcher VSCode"`）。
`llms-full.txt` が未取得なら自動で fetch される（パスからソース推定）。

Platform を検索する場合:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search-content /tmp/claude-platform-llms-full.txt "<キーワード>"
```

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

### フォールバック: 全ページ一覧を確認

search-index / search-content で見つからない場合のみ使用する。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" fetch-index
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" fetch-index --source platform
```

---

## コマンドリファレンス

| コマンド | 引数 | 説明 |
|---------|------|------|
| `search-index` | `[--source {code,platform}] <query> [--limit N]` | 軽量 llms.txt からキーワード検索してページをランキング |
| `search-content` | `<file> <query> [--doc-index N] [--limit N] [--context N] [--max-hits N]` | llms-full.txt 本文を横断キーワード検索、heading_path + スニペットを返す |
| `fetch-index` | `[--source {code,platform}] [--cache-dir DIR]` | 軽量 llms.txt を取得してページ一覧を表示 |
| `sections` | `<file> <doc_index>` | 指定ページの見出し一覧を表示 |
| `content` | `<file> <doc_index> [heading_path]` | セクション本文を表示 |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py`

### heading_path の指定方法

- 見出しテキストそのまま: `"Configuration"`
- スラッシュ区切りの階層パス: `"Hook events/PreToolUse/PreToolUse input"`
- 部分一致（大文字小文字無視）で検索される

### doc_index の乖離に注意

`llms.txt` (search-index が返す) と `llms-full.txt` (sections/content が使う) では
ページ順序が異なる場合がある。**search-index の doc_index をそのまま sections に渡すと別ページが返ることがある**。

安全な chain は以下の 2 通り:
1. **search-index で URL を確認** → `fetch-index` でタイトル一致するページを手動で特定
2. **search-content を起点にする** → 返された doc_index は `llms-full.txt` ベースなので sections/content にそのまま渡せる

`search-content` を推奨する理由はこの一貫性のため。

### キャッシュ（2段階 × 2ソース）

| ソース | インデックス | 全文 |
|--------|-------------|------|
| Claude Code | `/tmp/claude-code-llms.txt` | `/tmp/claude-code-llms-full.txt` |
| Platform | `/tmp/claude-platform-llms.txt` | `/tmp/claude-platform-llms-full.txt` |

- 最新版が必要な場合: `rm /tmp/claude-code-llms*.txt` または `rm /tmp/claude-platform-llms*.txt` してから再実行

---

## 制約

- **全文読み込み禁止**: search-index/search-content → sections → content の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **テーブル保護**: Markdown テーブルの途中分割を自動防止する
- **カスタムコンポーネント**: `<Note>`, `<Frame>`, `<Expandable>`, `<Card>` 等の JSX 記法はテキストとして読む

## 禁止事項（効率の悪いフォールバックを避ける）

以下は本スキルのコマンドで代替できるため使わないこと:

- ❌ `grep -n <keyword> /tmp/claude-code-llms*.txt`、`grep /tmp/claude-platform-llms*.txt`
  → ✅ `search-index` または `search-content` を使う
- ❌ `Read /tmp/claude-*-llms-full.txt (lines X-Y)` の直接行指定読み
  → ✅ `search-content` で heading_path を取得してから `content` で取り出す
- ❌ `fetch-index` の出力を Bash の grep/awk で再フィルタ
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
- `--source` フラグを明示する (code / platform のどちらを調査しているか明確にする)
- 日本語で回答する
- ページ取得に失敗した場合のみ WebFetch fallback を検討する
- 調査は簡潔に完了させること
