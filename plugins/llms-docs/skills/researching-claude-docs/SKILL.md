---
name: researching-claude-docs
description: |
  Claude Code (code.claude.com) / Claude Developer Platform (platform.claude.com)
  公式ドキュメントから hook schema、subagent frontmatter、plugin manifest、
  Skill 仕様、slash command 仕様等を verbatim で取得する。llms.txt /
  llms-full.txt を段階的に grep するため、WebFetch の要約モデル経由のような
  field 欠落・幻覚が起きない。
when_to_use: |
  Use proactively before answering Claude Code / Anthropic API の仕様質問。
  Use when implementing or debugging hooks, subagents, plugin manifest,
  slash commands, MCP servers, settings.json, permissions, Skills, or any
  code.claude.com / platform.claude.com API.
  Triggers: "Claude Code", "AgentSkill", "Skill", "hook schema", "subagent",
  "plugin manifest", "slash command", "settings.json", "permission", "MCP",
  "Anthropic API", "Claude Code ドキュメント", "code.claude.com",
  "platform.claude.com", "researching-claude-docs"
context: fork
model: sonnet
allowed-tools:
  - Read
  - Bash
  - WebFetch
paths:
  - "**/SKILL.md"
  - "**/.claude-plugin/**"
  - "**/.claude/agents/**.md"
  - "**/.claude/commands/**.md"
  - "**/.claude/hooks/**"
  - "**/.claude/settings*.json"
  - "**/.mcp.json"
  - "**/hooks.json"
metadata:
  author: mao
  version: "3.2.0"
---

# Claude ドキュメント Progressive Loader

Claude Code (`code.claude.com`) および Claude Developer Platform (`platform.claude.com`) の公式ドキュメントを段階的に読み込むスキル。

## ソース

| ソース | `--source` | ドキュメント | 規模 |
|--------|-----------|-------------|------|
| Claude Code | `code` (デフォルト) | code.claude.com/docs | ~64p / 1.4MB |
| Claude Developer Platform | `platform` | platform.claude.com/docs | ~617p / 23.8MB |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py`

## 推奨フロー (2 ステップ)

```
  search "<キーワード>"                ← Phase 1: ページ候補 + 本文ヒット
        ↓
  content <doc_idx> "<heading_path>"   ← Phase 2: 該当セクション取得
```

`search` は `llms.txt` (タイトル/説明スコア) と `llms-full.txt` (本文 AND 検索) を **URL で join** して 1 コマンドで候補ページ + 本文ヒットを返す。返ってきた `doc_idx` は `content` / `sections` にそのまま渡せる。

### Step 1: 統合検索でページと本文を絞り込む

```bash
# Claude Code（デフォルト）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<キーワード>"

# Claude Developer Platform
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<キーワード>" --source platform

# 両 source を並列に (Skill や hook のように両方に解説がある topic 向け)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<キーワード>" --source both
```

`<キーワード>` は 2〜3 語のスペース区切り（例: `"PostCompact input compact_summary"`）。

出力は `[doc_idx] タイトル` + `URL` + 本文ヒットセクション (heading_path 付きスニペット)。Changelog / Release notes は自動で deprioritize される。表示しきれなかった本文ヒットがある場合は `Other sections with hits (not shown):` として heading_path とヒット数の一覧が末尾に表示される。

`--source both` のときは結果に `[code]` / `[platform]` プレフィックスが付き、`doc_idx` は **source 内でユニーク**なので、follow-up の `content` / `sections` 呼び出しには `--source <code|platform>` を明示する。

### Step 2: 該当セクションの本文を取得

```bash
# search が返した doc_idx をそのまま使う
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <doc_idx> "<heading_path>"

# ページ全体を取得（heading_path 省略）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <doc_idx>
```

`content` は本文の末尾に **サブセクション一覧** (`Subsections of '...'`) を自動で出力する。さらに深掘りする際は `sections` を再度呼ばずに、そのまま次の `content` クエリに heading_path を渡せる。出力に含めたくない場合は `--no-subsection-hints` を付ける。

本文中の Markdown リンク (`[Text](/en/...)` や `[Text](https://code.claude.com/...)`) のうち同 source 内の既知ページを指すものには、自動で `→ [doc_idx N]` のアノテーションが付く。follow-up の `content` で page を切り替える時の手数を減らす。コードフェンス内と Markdown テーブル行は対象外。抑制したい場合は `--no-link-annotations`。

### サブフロー

| ケース | コマンド |
|------|----------|
| ページが既に分かっていて、その中だけ検索 | `search-content "<query>" --page-ref <ref>` |
| ページの heading 一覧のみ確認 | `sections <page_ref>` |
| 全ページ一覧 (`search` で見つからない時) | `fetch-index` |

---

## リファレンス

### コマンド一覧

| コマンド | 引数 | 説明 |
|---------|------|------|
| `search` | `<query> [--source {code,platform,both}] [--index-limit N] [--max-hits N] [--context N] [--max-snippet-chars N] [--max-age S] [--include-changelog-priority]` | **推奨**: llms.txt ランキング + llms-full.txt 本文を URL で join、1 コマンドで候補ページ + 本文ヒットを返す。`--source both` で code/platform 両方を並列検索 |
| `content` | `<page_ref> [heading_path] [--file F] [--source S] [--max-age S] [--no-subsection-hints] [--no-link-annotations]` | セクション本文を表示。末尾にサブセクション一覧、本文中の docs リンクには `→ [doc_idx N]` を付与 |
| `sections` | `<page_ref> [--file F] [--source S] [--max-age S]` | 指定ページの見出し一覧を表示 |
| `search-content` | `<query> [--page-ref R] [--file F] [--source S] [--limit N] [--context N] [--max-hits N] [--max-snippet-chars N] [--max-age S] [--include-changelog-priority]` | llms-full.txt 本文のみキーワード検索。`--page-ref` で 1 ページに絞れる |
| `search-index` | `<query> [--source S] [--limit N] [--max-age S]` | llms.txt のタイトル/説明だけをスコアリング (本文ヒット無し) |
| `fetch-index` | `[--source S] [--max-age S]` | 軽量 llms.txt を取得してページ一覧を表示 |

### `page_ref` の指定方法

`sections` / `content` の第 1 引数、`search-content --page-ref` で受け付ける形式:

| 形式 | 例 | 解決方法 |
|------|---|----------|
| 整数 | `53` | `llms-full.txt` 内の doc_idx として直接利用 |
| URL slug | `hooks`, `agent-sdk/hooks` | `source_url` の末尾パス成分と一致するページを検索 |
| 完全 URL | `https://code.claude.com/docs/en/hooks` | `source_url` を正規化して厳密一致 |

slug が複数ページに一致する場合は曖昧エラーで候補リストが表示される。より長い slug (`agent-sdk/hooks`) か完全 URL を渡して曖昧性を解消する。

### `heading_path` の指定方法

- 見出しテキストそのまま: `"Configuration"`
- スラッシュ区切りの階層パス: `"Hook events/PreToolUse/PreToolUse input"`
- 部分一致（大文字小文字無視）で検索される

---

## 制約

- **全文読み込み禁止**: search → content の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **テーブル保護**: Markdown テーブルの途中分割を自動防止する
- **カスタムコンポーネント**: `<Note>`, `<Frame>`, `<Expandable>`, `<Card>` 等の JSX 記法はテキストとして読む

## 失敗時の対処

| パターン | 症状 | 対処 |
|----------|------|------|
| キャッシュ期限切れ | 7 日超のキャッシュ | 自動 re-fetch (既定 `--max-age 604800`) |
| ネットワーク失敗 | fetch timeout / connection error | `--max-age 0` で cache 無視して再試行 |
| キャッシュ破損 | パースエラー / 不正なインデックス | `/tmp/` 配下の `claude-*-llms*.txt` を削除して再実行 |
| 結果ゼロ | `No results found` | キーワードを変えて再試行。`--source` を切り替えて code/platform 両方を確認 |
| スクリプトエラー | Python traceback | 下記 WebFetch フォールバックへ |

### WebFetch フォールバック

スクリプトで解決できない場合のみ使用する:

1. `search` をキーワードを変えて 2-3 回試す
2. それでも失敗 → `code.claude.com/docs/en/<slug>` または `platform.claude.com/docs/en/<slug>` を WebFetch で直接取得
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
- 全文読み込みは禁止 — 必ず search → content の順で絞り込む
- `--source` フラグを明示する (code / platform のどちらを調査しているか明確にする)
- 日本語で回答する
- スクリプト失敗時は「失敗時の対処」に従う。WebFetch は最終手段
- 調査は簡潔に完了させること
