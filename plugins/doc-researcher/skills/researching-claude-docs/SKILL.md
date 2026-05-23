---
name: researching-claude-docs
description: |
  Claude Code (code.claude.com) / Claude Developer Platform (platform.claude.com)
  公式ドキュメントから hook schema、subagent frontmatter、plugin manifest、
  slash command 仕様等を verbatim で返す。独立コンテキストで実行されメインセッションを消費しない。
  Claude Code / Anthropic API の仕様確認には WebFetch ではなくこのスキルを使う（要約モデル経由ではないため field の抜け落とし・幻覚が起きない）。
  Use proactively when implementing or debugging Claude Code features or needing Claude Code / Anthropic API documentation.
  Use when implementing or debugging Claude Code features such as hooks, subagents, plugin manifest, slash commands, MCP servers, settings.json, permissions, or any code.claude.com / platform.claude.com API.
  Triggers: "Claude Code", "hook schema", "subagent", "plugin manifest", "slash command",
  "settings.json", "permission", "MCP", "Anthropic API", "Claude Code ドキュメント",
  "code.claude.com", "platform.claude.com", "researching-claude-docs"
context: fork
model: sonnet
allowed-tools:
  - Read
  - Bash
  - WebFetch
metadata:
  author: mao
  version: "3.0.0"
---

# Claude ドキュメント Progressive Loader

Claude Code (`code.claude.com`) および Claude Developer Platform (`platform.claude.com`) の公式ドキュメントを段階的に読み込むスキル。

## ソース一覧

| ソース | `--source` | ドキュメント | 規模 |
|--------|-----------|-------------|------|
| Claude Code | `code` (デフォルト) | code.claude.com/docs | ~64p / 1.4MB |
| Claude Developer Platform | `platform` | platform.claude.com/docs | ~617p / 23.8MB |

## 推奨フロー (2 ステップ)

```
  search "<キーワード>"                ← Phase 1: ページ候補 + 本文ヒット
        ↓
  content <doc_idx> "<heading_path>"   ← Phase 2: 該当セクション取得
```

`search` は `llms.txt` (タイトル/説明スコア) と `llms-full.txt` (本文 AND 検索) を
**URL で join** して 1 コマンドにまとめたサブコマンド。返ってくる `doc_idx` は
`content` / `sections` にそのまま渡せる。

### Step 1: 統合検索でページと本文を一度に絞り込む

```bash
# Claude Code（デフォルト）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<キーワード>"

# Claude Developer Platform
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<キーワード>" --source platform
```

`<キーワード>` は 2〜3 語のスペース区切り（例: `"PostCompact input compact_summary"`）。
出力は `[doc_idx] タイトル` + `URL` + 本文ヒットセクション (heading_path 付きスニペット)。
Changelog / Release notes は自動で deprioritize されるので、本物の解説ページが上に来る。

### Step 2: 該当セクションの本文を取得

```bash
# search が返した doc_idx をそのまま使う
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <doc_idx> "<heading_path>"

# ページ全体を取得（heading_path 省略）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <doc_idx>
```

`page_ref` (第 1 引数) は **整数 / URL slug / 完全 URL** のいずれも受け付ける:

```bash
# 整数 doc_idx
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content 53 "Hook events/PreToolUse"

# URL slug (末尾パス成分)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content agent-sdk/hooks
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content settings

# 完全 URL
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content \
  https://code.claude.com/docs/en/hooks "Hook events/PreToolUse"
```

slug が複数ページに一致する場合 (例: `hooks` は `docs/en/hooks` と `docs/en/agent-sdk/hooks`
の両方にマッチ) は曖昧エラーになるので、より長い slug (`agent-sdk/hooks`) か完全 URL を渡す。

### サブフロー: 特定ページに絞った本文検索

ページが既に分かっていて、その中だけを検索したい場合は `search-content` を使う:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search-content \
  "<キーワード>" --page-ref hooks
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search-content \
  "<キーワード>" --page-ref 53
```

### サブフロー: ページ単独のセクション一覧

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" sections <page_ref>
```

### フォールバック: 全ページ一覧

`search` で見つからない時のみ:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" fetch-index
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" fetch-index --source platform
```

---

## コマンドリファレンス

| コマンド | 引数 | 説明 |
|---------|------|------|
| `search` | `<query> [--source {code,platform}] [--index-limit N] [--max-hits N] [--context N] [--max-snippet-chars N] [--max-age S] [--include-changelog-priority]` | **推奨**: llms.txt ランキング + llms-full.txt 本文を URL で join、1 コマンドで候補ページ + 本文ヒットを返す |
| `content` | `<page_ref> [heading_path] [--file F] [--source S] [--max-age S]` | セクション本文を表示。`page_ref` は int / slug / URL |
| `sections` | `<page_ref> [--file F] [--source S] [--max-age S]` | 指定ページの見出し一覧を表示 |
| `search-content` | `<query> [--page-ref R] [--file F] [--source S] [--limit N] [--context N] [--max-hits N] [--max-snippet-chars N] [--max-age S] [--include-changelog-priority]` | llms-full.txt 本文のみキーワード検索。`--page-ref` で 1 ページに絞れる |
| `search-index` | `<query> [--source S] [--limit N] [--max-age S]` | llms.txt のタイトル/説明だけをスコアリング (本文ヒット無し) |
| `fetch-index` | `[--source S] [--max-age S]` | 軽量 llms.txt を取得してページ一覧を表示 |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py`

### `page_ref` の指定方法

`sections` / `content` の第 1 引数、`search-content --page-ref` で受け付ける形式:

| 形式 | 例 | 解決方法 |
|------|---|----------|
| 整数 | `53` | `llms-full.txt` 内の doc_idx として直接利用 |
| URL slug | `hooks`, `agent-sdk/hooks` | `source_url` の末尾パス成分と一致するページを検索 |
| 完全 URL | `https://code.claude.com/docs/en/hooks` | `source_url` を正規化して厳密一致 |

slug が複数ページに一致する場合は曖昧エラーで候補リストが表示される。
より長い slug か完全 URL を渡して曖昧性を解消する。

### `heading_path` の指定方法

- 見出しテキストそのまま: `"Configuration"`
- スラッシュ区切りの階層パス: `"Hook events/PreToolUse/PreToolUse input"`
- 部分一致（大文字小文字無視）で検索される

### `--max-age` (キャッシュ TTL)

`/tmp/` 配下のキャッシュは既定では無期限に再利用される。新しい docs を取り直したい時:

```bash
# 24 時間より古ければ再 fetch
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<query>" --max-age 86400

# 強制再 fetch (キャッシュ削除)
rm /tmp/claude-code-llms*.txt
```

### `--max-snippet-chars`

スニペットが長くなりすぎる時は文字数制限で打ち切れる (既定 500 文字)。`0` で無制限。

### `search` と `search-content` の使い分け

- `search`: **最初に使うべきデフォルト**。ページ候補 + 本文ヒットを 1 回で取れる
- `search-content`: 既に対象ページが分かっていて、その中だけを検索したい時 (`--page-ref` で 1 ページに絞る)
- `search-index`: タイトル/説明だけで十分な軽量検索 (本文を見ない)

### キャッシュ（2 段階 × 2 ソース）

| ソース | インデックス | 全文 |
|--------|-------------|------|
| Claude Code | `/tmp/claude-code-llms.txt` | `/tmp/claude-code-llms-full.txt` |
| Platform | `/tmp/claude-platform-llms.txt` | `/tmp/claude-platform-llms-full.txt` |

ファイルが存在しない場合は自動で fetch される。

---

## 制約

- **全文読み込み禁止**: search → content の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **テーブル保護**: Markdown テーブルの途中分割を自動防止する
- **カスタムコンポーネント**: `<Note>`, `<Frame>`, `<Expandable>`, `<Card>` 等の JSX 記法はテキストとして読む

## 禁止事項（効率の悪いフォールバックを避ける）

以下は本スキルのコマンドで代替できるため使わないこと:

- ❌ `grep -n <keyword> /tmp/claude-code-llms*.txt`、`grep /tmp/claude-platform-llms*.txt`
  → ✅ `search` を使う
- ❌ `Read /tmp/claude-*-llms-full.txt (lines X-Y)` の直接行指定読み
  → ✅ `search` で heading_path を取得してから `content` で取り出す
- ❌ `fetch-index` の出力を Bash の grep/awk で再フィルタ
  → ✅ `search-index` でスコアリング済みの候補を得る

## v2 → v3 の移行

旧 (v2 までの形式) は破壊的に変わったので注意:

```bash
# v2 (動かない)
sections /tmp/claude-code-llms-full.txt 5
content /tmp/claude-code-llms-full.txt 5 "Heading"
search-content /tmp/claude-code-llms-full.txt "query"

# v3 (新)
sections 5
content 5 "Heading"
search-content "query"
# 旧来のファイル指定を残したいなら --file flag を明示
sections 5 --file /tmp/claude-code-llms-full.txt
```

`doc_index` positional は廃止され、`page_ref` (int / slug / URL を自動判別) に統一された。
整数を渡せばこれまで通り `doc_idx` として動く。

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
- 全文読み込みは禁止 — 必ず search → content の順で絞り込む
- `--source` フラグを明示する (code / platform のどちらを調査しているか明確にする)
- 日本語で回答する
- ページ取得に失敗した場合のみ WebFetch fallback を検討する
- 調査は簡潔に完了させること
