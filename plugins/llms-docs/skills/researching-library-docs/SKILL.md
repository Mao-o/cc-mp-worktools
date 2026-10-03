---
name: researching-library-docs
description: |
  Next.js / Vite / Vitest / Drizzle ORM / Zod / Hono / Bun / Vercel / Render /
  MCP (Model Context Protocol) / OpenAI Codex / Ollama / Agent Skills (SKILL.md 仕様) / Cloudflare (Workers / D1 / R2 / KV / Durable Objects / Pages / Queues / Workers AI /
  Vectorize / Hyperdrive / Agents / Workflows / AI Gateway / Browser Rendering / Containers)
  の公式ドキュメント調査スキル。各サイトの llms-full.txt を段階的に読み込み、API 仕様・
  設定・コード例を verbatim で取得する。Skill ツールで起動し、メインの会話コンテキストを
  消費しない。これらの仕様確認には WebFetch ではなくこのスキルを使う (要約モデル経由では
  ないため field の抜け落とし・幻覚が起きない)。利用者が sources.json に追加したサイトも読める。
when_to_use: |
  Use when building with, debugging, or designing on the services above, and
  proactively before answering spec questions about them — especially before
  editing next.config.*, vite.config.*, vitest.config.*,
  drizzle.config.*, wrangler.toml / wrangler.jsonc, or code that imports from
  `next` / `vite` / `vitest` / `drizzle-orm` / `zod` / `hono` /
  `@modelcontextprotocol/sdk`, or vercel.json / render.yaml / bunfig.toml.
  Triggers: "Next.js", "App Router", "Vite", "Vitest", "Drizzle", "drizzle-orm",
  "Zod", "Hono", "Bun", "Vercel", "Render", "MCP", "Model Context Protocol",
  "Codex CLI", "Ollama", "Cloudflare Workers", "wrangler", "D1", "R2", "Workers KV",
  "Durable Objects", "Cloudflare Pages", "Queues", "Workers AI", "Vectorize",
  "Hyperdrive", "Cloudflare Agents", "Cloudflare Workflows", "AI Gateway",
  "Browser Rendering", "Cloudflare Containers", "agentskills.io", "SKILL.md spec",
  "researching-library-docs"
argument-hint: "[source] <question>"
context: fork
model: sonnet
allowed-tools:
  - Read
  - Bash
  - WebFetch
metadata:
  author: mao
  version: "1.0.0"
---

# ライブラリ公式ドキュメント調査 (汎用 llms-full.txt)

各サイトの `llms-full.txt` を唯一の権威ある情報源として段階的に調査する。
ドキュメントにない情報は「ドキュメントに記載なし」と明記すること。

Claude Code / Claude API・AI SDK・Firebase は専用 skill (`researching-claude-docs` /
`researching-ai-sdk` / `researching-firebase`) の対象で、この skill では扱わない。

## Step 0: source を決める

全コマンドに `--source <name>` が必須。質問の対象から次の表で選ぶ。

| 対象 | `--source` |
|---|---|
| Next.js | `nextjs` |
| Vite / Vitest | `vite` / `vitest` |
| Drizzle ORM | `drizzle` |
| Zod | `zod` |
| Hono | `hono` |
| Bun | `bun` |
| Vercel | `vercel` |
| Render | `render` |
| MCP (Model Context Protocol) の仕様・SDK | `mcp` |
| OpenAI Codex (CLI / IDE / cloud / SDK) と ChatGPT の docs (desktop app / Work / 管理) | `codex` |
| Ollama | `ollama` |
| Agent Skills (`SKILL.md` の仕様、agentskills.io) | `agentskills` |
| Cloudflare の各製品 | `cloudflare-<製品>`: `workers` / `d1` / `r2` / `kv` / `durable-objects` / `pages` / `queues` / `workers-ai` / `vectorize` / `hyperdrive` / `agents` / `workflows` / `ai-gateway` / `browser-rendering` / `containers` |

- 引数の先頭語が上の source 名、または `sources` の一覧にある名前ならそれを使う
- Cloudflare の質問が複数製品にまたがる (例: Workers から D1 を使う) ときは、主題の製品から
  始め、足りなければ別の source で `search` し直す。wrangler の設定は `cloudflare-workers`
- 表に無いサイトは、まず一覧を確かめる (利用者が `sources.json` に足した profile も出る):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-llms-txt.py" sources
```

一覧にも無ければ、この skill では読めない。「末尾: 対象外のサイト」に従う。

## Quick Start

```bash
# 1. キーワードで候補ページ + 本文 hits を取得
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-llms-txt.py" search "<キーワード>" --source <name>
# 2. 返ってきた [page_idx] と heading_path で本文を取得
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-llms-txt.py" content <page_ref> "<heading_path>" --source <name>
```

出力末尾の `Next:` 行は次のコマンド例で、`--source` / `--sources-file` / `--file` / `--cache-dir` などの
引数を引き継いでいる。ただし script 名だけで書かれているので、実行するときは先頭を
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-llms-txt.py"` に置き換える。

## 調査フロー

```
  search (top N 候補 + 本文 hits 一括)        ← 推奨入口
        ↓
  content <page_ref> "<heading_path>"        ← 該当セクションの本文
        ↑ (補助)
  sections <page_ref>                        ← 見出し一覧を確認したいとき
        ↑ (深掘り)
  search-content --page-ref <page_ref>       ← 特定ページ内だけ本文検索
```

- `search`: スペース区切りの複数キーワード (AND。揃わなければ半分以上が揃うセクションへ
  緩め、`[partial match]` と表示)。未取得なら自動で取得してキャッシュする (Vercel は 10MB、
  Cloudflare Workers は 5MB あるので初回は数秒かかる)。キーワードは英語のドキュメント用語で書く
- `content`: `heading_path` を省略するとページ全体。本文は既定 24000 文字で切り詰め、
  前後にサブセクション一覧と次の呼び出し例を出す
- 見つからないときはキーワードを言い換えて 2-3 回 `search` し、それでも無ければ
  `fetch-index --compact --source <name>` で一覧を見る

## page_ref の指定方法

- **整数 index** (推奨): `search` / `fetch-index` に出る `[<page_idx>]`
- **タイトルの部分一致**: 一意に決まる場合のみ (曖昧ならエラーで候補を出す)
- **URL の部分一致**: ページ URL を持つ source のみ (例: `get-started`)

`zod` / `hono` / `render` / `codex` / `cloudflare-browser-rendering` は llms-full.txt にページ URL が無く、`llms.txt` と
タイトルが一致したページにだけ URL が付く (Hono は 4 分の 1 程度)。URL の無いページの引用元はタイトル +
heading_path で表す。

## heading_path の指定方法

`sections` / `search` / `content` の出力にある完全なパス (スラッシュ区切り) をそのままコピーする。
部分一致も受け付けるが、候補が 2 件以上なら `ambiguous heading` で候補一覧を出して止まる。
`Section: (top)` は最初の見出しより前の本文を指す。

## コマンドリファレンス

全コマンドに `--source <name>` が必須。`--file <path>` で手元の llms-full.txt を読み、
省略時はキャッシュ (既定 `~/.cache/llms-docs`、`--cache-dir` で変更) を auto-fetch / 再利用する。

| コマンド | 主な引数 | 説明 |
|---|---|---|
| `search` | `<query> [--top-n N] [--max-hits N] [--context N]` | 推奨入口。候補 top N + 本文 hits |
| `search-index` | `<query> [--limit N] [--show-sections]` | タイトル・見出しで候補だけ取得 |
| `search-content` | `<query> [--page-ref REF] [--limit N] [--max-hits N]` | 本文を横断検索 |
| `sections` | `<page_ref>` | ページの見出し一覧 |
| `content` | `<page_ref> [heading_path] [--max-chars N]` | 本文を表示 |
| `fetch-index` | `[--compact]` | 全ページ一覧 (フォールバック) |
| `sources` | | 使える source の一覧 (presets と利用者の sources.json) |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-llms-txt.py`

## 禁止事項

`allowed-tools` に Read/Bash があるため技術的には実行できてしまうが、段階的絞り込みを迂回し
全文読み込み相当になるため使用しない:

| 禁止 | 代替 |
|---|---|
| キャッシュファイルへの `grep` / `rg` / `cat` / 行番号指定の Read | `search-content` / `content` |
| `fetch-index \| grep` | `search-index` |
| `sources.json` の作成・編集 (利用者の設定ファイル) | 必要な profile を回答で提案する |

## 失敗時の対処

| 症状 | 対処 |
|---|---|
| `unknown --source` | `sources` で一覧を確認して選び直す |
| ネットワーク失敗 | 既存キャッシュがあれば WARNING を出して続行。無ければ Error。復旧後は `--max-age 0` で再取得 |
| 結果ゼロ | キーワードを変えて再試行。`fetch-index --compact` で一覧確認 |
| 起動直後の `TypeError: unsupported operand type(s)` | Python 3.11 以上が必要 |
| その他のスクリプトエラー | 下の WebFetch フォールバック |

### WebFetch フォールバック

スクリプトで解決できない場合のみ、ページ URL (`url:` 行) を WebFetch で取得する。要約モデル
経由のため field の抜け落ちがありうる — 取得内容を鵜呑みにせず、その旨を回答に書く。

### 対象外のサイト

`sources` の一覧に無いサイトは読めない。そのサイトが `llms-full.txt` を公開していれば、利用者が
`~/.config/llms-docs/sources.json` に profile を足すと読めるようになる (書き方は plugin の
README「任意の llms-full.txt を読む」)。回答では、調べられなかったことと、足すべき profile の
案 (`url` と `split`) を示す。自分で sources.json を作らない。

## 出力フォーマット (参考)

固定セクションは強制しない。最低限:

- **発見事項**: 何が分かったか
- **引用元**: ページ URL (あれば) またはタイトル + heading_path。verbatim 引用には必ず出典を併記
- **コード例**: ドキュメントから直接引用したもののみ
- **注意事項**: バージョン要件・制約 (該当する場合)

## ルール

- ドキュメントにない機能やオプションを捏造しない
- コード例はドキュメントから直接引用する
- 全文読み込みは禁止 — `search` → `content` の順で絞り込む
- 日本語で回答する
- 調査は簡潔に完了させること
