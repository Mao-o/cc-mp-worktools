---
name: researching-library-docs
description: |
  AI SDK (Vercel AI SDK) / Firebase / Next.js / Vite / Vitest / Drizzle / Zod / Hono / Bun /
  Vercel / Render / MCP / OpenAI Codex・Plugins・API・Agents SDK / Ollama / Agent Skills /
  Agent Plugins / ACP / AI コーディングツール (Cline / Factory / Devin / Gemini CLI /
  Kilo Code / Continue / Zed / Amp) の公式ドキュメント調査スキル。llms.txt を段階的に読み、
  API 仕様・設定・コード例を verbatim で取得する。Skill ツールで起動し、会話コンテキストを
  消費しない。WebFetch (要約で field が抜ける) の代わりに使う。sources.json のサイトも読める。
when_to_use: |
  Use when building with, debugging, or designing on the above, and proactively
  before spec questions about them — especially before editing code that imports
  `ai` / `@ai-sdk/*` / `firebase` / `firebase-admin` / `firebase-functions` / `next` /
  `drizzle-orm` / `zod` / `hono` / `@modelcontextprotocol/sdk`, or firebase.json / *.rules /
  next.config.* / vite.config.* / vitest.config.* / drizzle.config.* / vercel.json.
  Triggers: "AI SDK", "ai-sdk", "@ai-sdk", "streamText", "generateText", "useChat",
  "streamObject", "generateObject", "useObject", "embedMany", "convertToModelMessages",
  "Firebase", "Firestore", "Firebase Auth", "Cloud Functions", "FCM", "Firebase Hosting",
  "App Hosting", "Realtime Database", "Cloud Storage", "App Check", "Remote Config",
  "Crashlytics", "Data Connect", "Genkit", "AI Logic", "security rules", "firestore.rules",
  "storage.rules", "App Router", "MCP", "Codex CLI", "OpenAI API", "OpenAI Agents SDK",
  "agentskills.io", "SKILL.md spec", "Factory Droid", "researching-ai-sdk",
  "researching-firebase"
argument-hint: "[source] <question>"
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

# ライブラリ公式ドキュメント調査 (汎用 llms-full.txt)

各サイトの `llms-full.txt` (無いサイトは `llms.txt` と各ページ) を唯一の権威ある情報源として段階的に調査する。
ドキュメントにない情報は「ドキュメントに記載なし」と明記すること。

Claude Code / Claude API は `researching-claude-docs`、Cloudflare の各製品は `researching-cloudflare-docs` の対象。
script は同じ `parse-llms-txt.py` で、source 名 (`claude-code` / `claude-platform` / `claude` / `cloudflare-<製品>`) と
読み方はそれぞれの skill にある。AI SDK と Firebase はこの skill で読む (下の「source ごとの注意」も読む)。

## 結果の返し方 (最初に読む)

調べた本文は、この Skill の**最終メッセージ**として返す。

`StructuredOutput` ツールや「最後に `StructuredOutput` を呼ぶ」という指示が見えていても、この Skill の実行中は
呼ばない。それらは呼び出し元 (schema 付きで起動された agent など) のもので、fork に引き継がれているだけ。
呼ぶと本文がそのツールの引数に入り、呼び出し元に返る Skill の結果は `Skill execution completed` だけになる。
schema を埋めるのは呼び出し元の仕事で、呼び出し元は最終メッセージの本文からそれを埋める。

## Step 0: source を決める

全コマンドに `--source <name>` が必須。質問の対象から次の表で選ぶ。

| 対象 | `--source` |
|---|---|
| AI SDK (Vercel AI SDK、`ai` / `@ai-sdk/*`) | `ai-sdk` |
| Firebase (ページごとに取得する。下の「source ごとの注意」) | `firebase` |
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
| Agent Plugins (Skills と MCP server を束ねる plugin の共通形式、agent-plugins.org) | `agent-plugins` |
| OpenAI Plugins (ChatGPT / Codex の plugin: MCP server・UI・skills・提出) | `openai-plugins` |
| OpenAI API docs (developers.openai.com/api/docs) | `openai-api-docs` |
| Agent Client Protocol (ACP) | `acp` |
| Cline | `cline` |
| Factory (Droid) | `factory` |
| Devin | `devin` |
| Gemini CLI | `gemini-cli` |
| Kilo Code | `kilo-code` |
| OpenAI Agents SDK (TypeScript) | `openai-agents-js` |
| Continue | `continue` |
| Zed / Amp (ページごとに取得する。`search-content` は `--page-ref` が必須) | `zed` / `amp` |

- 引数の先頭語が上の source 名、または `sources` の一覧にある名前ならそれを使う
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
  緩め、`[partial match]` と表示)。未取得なら自動で取得してキャッシュする (Vercel は 10MB
  あるので初回は数秒かかる)。キーワードは英語のドキュメント用語で書く。
  index の上位 N 件に本文ヒットが 1 件も無いときは、全文検索のヒットを `--top-n` 件まで (全キーワードが揃うページを先に、部分一致のページも含めて) `[body-only]` として足す。上位 N 件がどれも部分一致 (`[partial match]`) のときは、全文検索で全キーワードが 1 セクションに揃うページを最大 2 件探して `[body-only]` として足す (既存の行は残り、`Next:` には index の最上位候補の行も残る。全キーワードが揃う候補が Changelog / Release notes だけのときも探す。全キーワードが揃う候補があっても、どの候補もページタイトルと見出しに全キーワードを語として含まなければ、含むページを同じく最大 2 件足す。キーワードが 1 語のときは足さない)。足したページは末尾に追記されるのではなく、`search` の並び順の中に入る (本文ヒットのあるページは本文ヒットの無い index の候補より上に並ぶので、1 位になることが多い。その index の候補は `(no body hits — index match only)` で下位に回る)。H3 だけで候補に入ったページ (タイトル・description・tags・H1 / H2 のどれにも当たらないページ) は、それらで全キーワードが当たった候補 (Changelog / Release notes を除く) の下に置かれる
- `content`: `heading_path` を省略するとページ全体。本文は既定 24000 文字で切り詰め、
  前後にサブセクション一覧と次の呼び出し例を出す。子見出しの無い節が切り詰められたときは、
  代わりに `Next: ... search-content --page-ref N --context 0 -- <節の見出し>` を出す (注記が名指しする仮置きの語を差し替えて使う。
  語は常に `--` の後ろに置くので、`--resume` のような語に差し替えても打てる)
- 見つからないときは、同じ論点で `search` を 3 回まで試し (言い換えより `search-index` / `sections`)、
  それでも無ければ `fetch-index --compact --source <name>` で一覧を見るか「ドキュメントに記載なし」として返す

## page_ref の指定方法

- **整数 index** (推奨): `search` / `fetch-index` に出る `[<page_idx>]`
- **タイトルの部分一致**: 一意に決まる場合のみ (曖昧ならエラーで候補を出す)
- **URL の部分一致**: ページ URL を持つ source のみ (例: `get-started`)

`zod` / `hono` / `render` / `codex` / `openai-plugins` / `openai-api-docs` / `factory` は
llms-full.txt にページ URL が無く、`llms.txt` とタイトルが一致したページにだけ URL が付く
(付く割合は source で違う。Hono は 4 分の 1 程度、`factory` は全ページ。README の presets 表)。
`agent-plugins` と `openai-agents-js` は URL が全く無い。URL の無いページの引用元はタイトル + heading_path で表す。
`openai-agents-js` は同じタイトルのページがあるので、整数 index で開く。

`zed` / `amp` は整数 index・完全 URL・URL の末尾 (slug) を受け付ける (タイトルの部分一致は無い)。`ai-sdk` / `firebase` も形が違うので、下の「source ごとの注意」を読む。

## source ごとの注意

### AI SDK (`--source ai-sdk`)

`page_ref` は 4 形式を受け付け、上から順に試す:

- **整数 index** (推奨): `42`
- **タイトルの部分一致**: `"Event Callbacks"`。一意に決まる場合のみ。曖昧ならエラーになる (候補ごとのコマンド付き)
- **完全 URL**: `https://ai-sdk.dev/docs/ai-sdk-core/generating-text`。各ページの frontmatter の `url:` (`sections` /
  `content` / `search` / `search-content` が `URL:` 行に表示する) と正規化して一致するページ。末尾の `/` や `.md` は
  無視する。一致するページが無ければ `No page found for URL` で exit 1
- **URL の末尾 (slug)**: `generating-text`, `ai-sdk-core/generating-text`。URL がその段で終わるページ。複数あれば
  候補ごとのコマンド付きで曖昧エラーになるので、長い slug か完全 URL を渡す

タイトルの部分一致が先に効くので、slug がどれかのタイトルの一部にも当たると、そのタイトルのページが開く
(同じ slug で終わる別ページがあっても開かない。例 `acp`)。ただし、その別ページがあるときは stderr に `Note: '<ref>' matched the title of [N], and the URL of [M] … also ends in it` と、
そのページを開くコマンドが出る (終了コードは 0 のまま)。Note が出たら、開いたページの `URL:` 行が目的のページかを確かめ、違えば出力のコマンドを使う。
どの形式にも当たらなければ `No document found for: <ref>` で exit 1 (整数 index を `search` / `search-index` で調べ直す)。

- **引用元**: `search` / `search-content` の結果に、ページの URL が `URL:` 行で付く。`Section:` 行の `URL#anchor` は付かない
  (AI SDK サイトの見出し id の規則を確かめていないため) ので、URL + タイトル + heading_path で表す
- **本文の読み方**: `<Snippet>`, `<Note>` 等の JSX 記法はテキストとして読む。見出しレベルの飛びが多い (`#` の直下に `###`)
- **WebFetch フォールバックの先**: `ai-sdk.dev/docs/<slug>`
- ファイルの物理構造・frontmatter・見出しの詳細: [references/ai-sdk-llms-txt-structure.md](references/ai-sdk-llms-txt-structure.md)

### Firebase (`--source firebase`)

**`llms-full.txt` が無い**ので、`llms.txt` の項目でランキングし、個別の `.md.txt` ページを on-demand で取得する。

| 項目 | 値 |
|------|----|
| ページ数 | ~6970 (Android/iOS/JS/C++ 各 SDK の API reference を含む) |
| インデックスサイズ | ~1.8MB |
| キャッシュ | `<cache-dir>/firebase-llms.txt` (index) + `<cache-dir>/firebase-docs/` (per-page) |

- `search` は上位候補 N 件 (既定 5、`--top-n`) だけを取得して本文を検索する。初回は N 件分の HTTP 取得で数秒〜十数秒かかり、
  2 回目以降はキャッシュで速い。`sections` / `content` は該当ページを自動で取得する
- **`search-content` は `--page-ref` を付ける**。省略すると 7000 ページ近くを取得する (初回は非常に重い)。7000 ページを
  盲目的に一覧せず、`search` を入口にする
- `fetch-index` は既定で先頭 100 件。続きは `--offset 100 --limit 100`。`--limit 0` は全件 (7000 行近い) なので通常は避ける
- `page_ref` は 3 形式: 整数 index (推奨)、URL の末尾 (`"vector-search"`。一意に決まる場合のみ)、完全 URL
  (`"https://firebase.google.com/docs/firestore/vector-search"`)。タイトルの部分一致は無い
- **引用元の anchor**: `Section:` 行に `[<URL>#<anchor>]` が付く。Google DevSite の規則で、単語の結合は `-` ではなく `_`
  (`.../firestore/manage-data/add-data` の見出し「Set a document」は `id="set_a_document"`。見出しに `/` を含む `## Read / write data` は `#read_write_data`)。
  slug は見出しのレンダリング後テキストから作る (小文字化・記号除去・空白を `_` で結合)。正規化するのは インライン / 参照形式リンク・画像 (alt を採用)・脚注マーカー・HTML タグ・HTML 実体参照・コードスパン (中身は逐語)・`*` `~` と単語境界の `_` 強調記号。
  同名見出しの連番、ページ側の独自 ID 指定、それ以外の記法は再現しない best-effort で、解決しないときは URL 本体 (`#` の前) でページを開いて見出しを目視で探す
- reference ページの多くは H2 のみのフラット構造、guide ページは H2/H3 の階層を持つ
- **一部のページの取得失敗** (`search` / `search-content`): 失敗したページだけ `(skip: fetch failed <url>: ...)` を stderr に出して読み飛ばし、残りで続ける (exit 0)。末尾に `(N documents skipped — fetch failed, see stderr)` が出る。全ページ失敗したときだけ結果が 0 件になる
- **結果ゼロ**: `Why nothing matched:` の語ごとのページ数は、`search` ならタイトル / 説明の index、`search-content` なら取得済みのページが対象。`--page-ref` で絞っていると数えるのはそのページだけなので、外して再実行する
- **WebFetch フォールバックの先**: `https://firebase.google.com/docs/<product>`

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
| `search-content` | `<query> [--page-ref REF] [--limit N] [--max-hits N] [--include-changelog-priority]` | 本文を横断検索 |
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
