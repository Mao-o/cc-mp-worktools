---
name: researching-cloudflare-docs
description: |
  Cloudflare の製品別公式ドキュメント調査スキル (Workers / wrangler / D1 / R2 / Workers KV /
  Durable Objects / Pages / Queues / Workers AI / Vectorize / Hyperdrive / Agents / Workflows /
  AI Gateway / Browser Run (旧 Browser Rendering) / Containers)。製品ごとの llms-full.txt を
  段階的に読み込み、API 仕様・設定・コード例を verbatim で取得する。Skill ツールで起動し、
  メインの会話コンテキストを消費しない。これらの仕様確認には WebFetch ではなくこのスキルを使う
  (要約モデル経由ではないため field の抜け落とし・幻覚が起きない)。
when_to_use: |
  Use when building with, debugging, reviewing, or designing on Cloudflare, and
  proactively before answering spec questions about it — especially before
  editing wrangler.toml / wrangler.jsonc, or code that imports from
  `cloudflare:workers` / `@cloudflare/*`.
  Triggers: "Cloudflare", "Cloudflare Workers", "Workers", "wrangler", "D1", "R2",
  "Workers KV", "KV", "Durable Objects", "Cloudflare Pages", "Queues", "Workers AI",
  "Vectorize", "Hyperdrive", "Cloudflare Agents", "Cloudflare Workflows", "AI Gateway",
  "Browser Run", "Browser Rendering", "Cloudflare Containers", "bindings",
  "researching-cloudflare-docs"
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

# Cloudflare 公式ドキュメント調査 (製品別 llms-full.txt)

Cloudflare の製品ごとの `llms-full.txt` を唯一の権威ある情報源として段階的に調査する。
ドキュメントにない情報は「ドキュメントに記載なし」と明記すること。

他のサイトは別の skill の対象: Claude Code / Claude API は `researching-claude-docs`、AI SDK・Firebase・
Next.js・Hono・Drizzle などは `researching-library-docs`。script は同じ `parse-llms-txt.py`。

## 結果の返し方 (最初に読む)

調べた本文は、この Skill の**最終メッセージ**として返す。

`StructuredOutput` ツールや「最後に `StructuredOutput` を呼ぶ」という指示が見えていても、この Skill の実行中は
呼ばない。それらは呼び出し元 (schema 付きで起動された agent など) のもので、fork に引き継がれているだけ。
呼ぶと本文がそのツールの引数に入り、呼び出し元に返る Skill の結果は `Skill execution completed` だけになる。
schema を埋めるのは呼び出し元の仕事で、呼び出し元は最終メッセージの本文からそれを埋める。

## Step 0: source を決める

全コマンドに `--source <name>` が必須。製品ごとに別の source で、質問の対象から次の表で選ぶ。

| 製品 | `--source` |
|---|---|
| Workers (ランタイム API・バインディング・wrangler の設定) | `cloudflare-workers` |
| D1 | `cloudflare-d1` |
| R2 | `cloudflare-r2` |
| Workers KV | `cloudflare-kv` |
| Durable Objects | `cloudflare-durable-objects` |
| Pages | `cloudflare-pages` |
| Queues | `cloudflare-queues` |
| Workers AI | `cloudflare-workers-ai` |
| Vectorize | `cloudflare-vectorize` |
| Hyperdrive | `cloudflare-hyperdrive` |
| Agents | `cloudflare-agents` |
| Workflows | `cloudflare-workflows` |
| AI Gateway | `cloudflare-ai-gateway` |
| Browser Run (旧 Browser Rendering。source 名は旧名のまま) | `cloudflare-browser-rendering` |
| Containers | `cloudflare-containers` |

- 引数の先頭語が上の source 名、または `sources` の一覧にある名前ならそれを使う
- 質問が複数製品にまたがる (例: Workers から D1 を使う) ときは、主題の製品から始め、足りなければ
  別の source で `search` し直す。wrangler の設定は `cloudflare-workers`
- 表に無い製品は、まず一覧を確かめる (利用者が `sources.json` に足した profile も出る):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-llms-txt.py" sources
```

一覧にも無ければ「末尾: 表に無い製品」に従う。

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

## 調査の進め方 (手数の目安)

- **論点が複数あるとき**は、最初に論点を番号付きで列挙し、1 つずつ順に処理して、論点ごとに結論
  (または「ドキュメントに記載なし」) を返す。複数の論点を 1 回の `search` に詰めない
- 1 論点の基本は **`search` 1 回 → `content` 1〜2 回**。`search` の末尾の `Next:` 行は `doc_idx` と見出しが
  埋まったコマンドなので、見出しを手で写さずそのまま実行する (先頭の `parse-llms-txt.py` は
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-llms-txt.py"` に置き換える。`--source` などは `Next:` が引き継いでいる)
- 同じ論点で `search` を **3 回外したら**、言い換えを続けない。`search-index` / `sections` で構造から当たるか、
  「ドキュメントに記載なし」として返す
- **`--max-chars 0`** は、出力に `... (N chars truncated; ...)` が出て、既定の上限 (24000 字) で
  切れたと確かめてからだけ使う。`| head` / `| grep` で出力を切らず、`--max-chars` と `sections` で絞る
  (パイプで切ると末尾の `Next:` が見えなくなる)
- この Skill の実行中 (fork の中) では、同じ Skill をもう呼ばない (`already executing in this forked
  context` になる)。続きは同梱のスクリプトを直接実行する

## Skill を呼べない文脈 (subagent など)

general-purpose の subagent など、Skill ツールを使えない文脈では、公式ドキュメントを WebFetch する前に、
同梱のスクリプトを直接実行する (WebFetch は要約モデル経由で field が抜ける)。

1. パスは `${CLAUDE_PLUGIN_ROOT}/scripts/parse-llms-txt.py`。`${CLAUDE_PLUGIN_ROOT}` が空の環境では、plugin の展開先 (`~/.claude/plugins/` の下) を探す。
   ディレクトリを指定して追加した marketplace は展開先へコピーされず、その場で読まれるので、見つからなければ
   `claude plugin marketplace list` で `Source: Folder (<dir>)` と出る marketplace の `<dir>` の下の
   `plugins/llms-docs/scripts/parse-llms-txt.py` を探す
2. `python3 <path> search "<キーワード>" --source <name>` を実行し、出力末尾の `Next:` の先頭を `python3 <path>` に置き換えて本文を取る

schema 付きで起動された agent (`StructuredOutput` を持つ呼び出し元) は、Skill の結果に本文が無く
`Skill execution completed` だけのことがある。そのときは同じ Skill を呼び直さず、上の手順でスクリプトを直接実行する
(README の「既知の制約: `StructuredOutput` を持つ呼び出し元」)。

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
  緩め、`[partial match]` と表示)。未取得なら自動で取得してキャッシュする (Workers は 5MB
  あるので初回は数秒かかる)。キーワードは英語のドキュメント用語で書く。
  index の上位 N 件に本文ヒットが 1 件も無いときは、全文検索のヒットを `--top-n` 件まで (全キーワードが揃うページを先に、部分一致のページも含めて) `[body-only]` として足す。上位 N 件がどれも部分一致 (`[partial match]`) のときは、全文検索で全キーワードが 1 セクションに揃うページを最大 2 件探して `[body-only]` として足す (既存の行は残り、`Next:` には index の最上位候補の行も残る。全キーワードが揃う候補が Changelog / Release notes だけのときも探す。全キーワードが揃う候補があっても、どの候補もページタイトルと見出しに全キーワードを語として含まなければ、含むページを同じく最大 2 件足す。キーワードが 1 語のときは足さない)。足したページは末尾に追記されるのではなく、`search` の並び順の中に入る (本文ヒットのあるページは本文ヒットの無い index の候補より上に並ぶので、1 位になることが多い。その index の候補は `(no body hits — index match only)` で下位に回る)。H3 だけで候補に入ったページ (タイトル・description・tags・H1 / H2 のどれにも当たらないページ) は、それらで全キーワードが当たった候補 (Changelog / Release notes を除く) の下に置かれる
- `content`: `heading_path` を省略するとページ全体。本文は既定 24000 文字で切り詰め、
  前後にサブセクション一覧と次の呼び出し例を出す。子見出しの無い節が切り詰められたときは、
  代わりに `Next: ... search-content --page-ref N --context 0 -- <節の見出し>` を出す (注記が名指しする仮置きの語を差し替えて使う。
  語は常に `--` の後ろに置くので、`--resume` のような語に差し替えても打てる)
- 見つからないときは、同じ論点で `search` を 3 回まで試し (言い換えより `search-index` / `sections`)、
  それでも無ければ `fetch-index --compact --source <name>` で一覧を見るか「ドキュメントに記載なし」として返す
- 索引の点数 (`search-index` と `search` の候補) は語・語の部分 (`-` / `_` の後、camelCase の段) の頭で照合するので、語全体で打つ (`agents` ではなく `subagents`)。本文の検索は部分一致のまま

## page_ref の指定方法

- **整数 index** (推奨): `search` / `fetch-index` に出る `[<page_idx>]`
- **タイトルの部分一致**: 一意に決まる場合のみ (曖昧ならエラーで候補を出す)
- **URL の部分一致**: 例 `get-started`。ページ URL は `.../index.md` の形

## heading_path の指定方法

`sections` / `search` / `content` の出力にある完全なパス (スラッシュ区切り) をそのままコピーする
(`search` の `Next:` には最上位ヒットのパスが埋まっている)。
長すぎて `content` 1 回に収まらない節 (24000 文字超) で、見出しが検索語を含まないときは、`Next:` は `content` ではなく `search-content --page-ref N` を出す (一致する行を一覧する。スニペットも語が多く揃う行の前後だけを示す)。
部分一致も受け付けるが、候補が 2 件以上なら `ambiguous heading` で候補一覧を出して止まる。
`Section: (top)` は最初の見出しより前の本文を指す。

## コマンドリファレンス

全コマンドに `--source <name>` が必須。`--file <path>` で手元の llms-full.txt を読み、
省略時はキャッシュ (既定 `~/.cache/llms-docs`、`--cache-dir` で変更) を auto-fetch / 再利用する。

| コマンド | 主な引数 | 説明 |
|---|---|---|
| `search` | `<query> [--top-n N] [--max-hits N] [--context N] [--max-snippet-chars N] [--include-changelog-priority]` | 推奨入口。候補 top N + 本文 hits。changelog / release notes は既定で末尾 (`--include-changelog-priority` で解除) |
| `search-index` | `<query> [--limit N] [--show-sections]` | タイトル・見出しで候補だけ取得 |
| `search-content` | `<query> [--page-ref REF] [--limit N] [--context N] [--max-hits N] [--max-snippet-chars N] [--include-changelog-priority]` | 本文を横断検索 |
| `sections` | `<page_ref>` | ページの見出し一覧 |
| `content` | `<page_ref> [heading_path] [--max-chars N] [--no-subsection-hints]` | 本文を表示。前後にサブセクション一覧、既定 24000 文字で切り詰め |
| `fetch-index` | `[--compact] [--offset N] [--limit N]` | 全ページ一覧 (フォールバック) |
| `sources` | | 使える source の一覧 (presets と利用者の sources.json) |

スクリプトパス: `${CLAUDE_PLUGIN_ROOT}/scripts/parse-llms-txt.py`

## 禁止事項

`allowed-tools` の `Bash` を完全一致の `Bash(<コマンド>)` に絞らず単独で付与しているのは、打つコマンドの語・見出し・ページ番号・`--cache-dir` などが質問ごとに変わり、事前に列挙できないため (絞ると `context: fork` の手順が確認待ちで止まる)。

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
| ネットワーク失敗 | 既存キャッシュがあれば WARNING を出して続行。無ければ Error。復旧後は `--max-age 0` で再取得。例外: preset の取得 URL が変わった直後 (キャッシュの `.meta.json` の URL と違う) は、既存キャッシュがあっても Error |
| 結果ゼロ (`No matching ...` の下に `Why nothing matched:` で語ごとのページ数) | 全語が 0 件なら言い換えを続けず、別の語・`search-index`・別の `--source` に切り替える。一部の語だけ 0 件ならその語を落とす。全語があるのに 0 件 (同じセクションに揃わない) なら語を減らす。続けて出る `Next:` がそのまま実行できる |
| キャッシュ破損 (パースエラー / 不正なインデックス) | `--max-age 0` で強制再取得 (キャッシュは既定 `~/.cache/llms-docs`、`--cache-dir` で変更可。7 日超のキャッシュは自動で取り直す) |
| 曖昧な page_ref (`Ambiguous title substring` / `Ambiguous url substring` / `Ambiguous slug`) | 候補ごとに実行できるコマンドが付く。選んでそのまま実行する |
| どのページの URL でもない URL を page_ref に渡した (`No page found for URL: <URL>`) | URL の綴りを確かめる。分からなければ URL の最後の段の語で `search` する |
| `Error: heading '...' not found.` | `Closest sections:` の候補 (コマンド付き) を先に使う。全見出しは `Available sections:` に続く |
| 起動直後の `TypeError: unsupported operand type(s)` | Python 3.11 以上が必要 |
| `Error: <path>/sources.json: …`、または同じファイルを名指す `WARNING: …` | `Error` と、`left out` / `not used` を含む `WARNING` は利用者の `sources.json` の誤りなので、そのファイルを直す (README「任意の llms-full.txt を読む」の `sources.json` の節)。`replaces the bundled preset` の `WARNING` は同梱 preset の上書きで、利用者が意図したものなら対処不要 (意図しないときだけ直す)。直せなければ下の WebFetch フォールバック |
| その他のスクリプトエラー | 下の WebFetch フォールバック |

### WebFetch フォールバック

スクリプトで解決できない場合のみ、ページ URL (`URL:` 行。`content` では `# source:` 行) を WebFetch で取得する。要約モデル
経由のため field の抜け落ちがありうる — 取得内容を鵜呑みにせず、その旨を回答に書く。

### 表に無い製品

Cloudflare 全体の `https://developers.cloudflare.com/llms.txt` は製品別の `llms.txt` へのリンク集で、読めない。
表に無い製品も `/<製品>/llms-full.txt` を公開していれば、利用者が `~/.config/llms-docs/sources.json` に
profile を足すと読めるようになる (書き方は plugin の README「任意の llms-full.txt を読む」。同じ形の profile は
`split: frontmatter` + `page_url: "link:View as Markdown"` + `drop_lines`)。回答では、調べられなかったことと、足すべき
profile の案を示す。自分で sources.json を作らない。

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
