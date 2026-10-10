# 任意の `llms.txt` サイトを source 登録できるようにする場合のコスト見積り

計測: plugin 0.24.0 / 2026-09-19。着手判断のための材料として書いた。

> **2026-09-26 追記 (0.25.0)**: Phase 0 (コマンド層の共通化、0.24.3) に続き、Phase 1 として
> 形状 profile を外部化した汎用 loader `scripts/parse-llms-txt.py` を追加した (使い方は
> README の「任意の llms-full.txt を読む」)。2 節の表のうち未計測だった Next.js / Vite /
> Vitest を実測し、Drizzle の区切り方の誤りを直した (下の「2026-09-26 実測」)。Phase 2
> (汎用 skill) は未着手。
>
> **2026-10-03 追記 (0.26.0)**: 実測済みのサイトを `scripts/presets.json` に同梱し、設定なしで
> 読めるようにした。あわせて Hono / Agent Skills / Cloudflare 製品別ファイルを実測し、そのための
> profile キー (`page_url: "link:…"` / `drop_lines` / `skip_empty`) を足した (下の「2026-10-03 実測」)。
>
> **2026-10-03 追記 (0.26.1)**: 共有の `FenceTracker` が言語名付きの行を閉じとみなす誤りを直し、
> Zod のページ数が 16 から 17 になった (下の「2026-10-03 追記 (0.26.1)」)。

## 1. 現状の共通化率 (実測)

`_common.py` は 1,734 行 / 47 関数 + 3 クラス。3 parser はそこから 24〜30 個のシンボルを
import している。それでも各 parser の **8 割超がローカル定義**のままである。

| script | 総行数 | `_common` import | ローカル定義 | うち他 parser と同名 (重複) | うち固有 |
|---|---|---|---|---|---|
| `parse-claude-docs.py` | 1,279 | 30 | 33 defs / 1,073 行 (83%) | 10 defs / 515 行 | 23 defs / 558 行 |
| `parse-ai-sdk.py` | 845 | 24 | 19 defs / 696 行 (82%) | 11 defs / 555 行 | 8 defs / 141 行 |
| `parse-firebase.py` | 705 | 26 | 16 defs / 577 行 (81%) | 9 defs / 477 行 | 7 defs / 100 行 |

読み方:

- 6 サブコマンド (`search` / `search-index` / `search-content` / `sections` / `content` /
  `fetch-index`) は **3 本すべてに別実装**があり、合計 1,014 行 (328〜355 行/本)。
  `_common` に寄っているのは検索・抽出・fetch の内側だけで、**コマンド層と表示層は 3 重化**
  している
- source 固有ロジック自体は小さい (100〜558 行)。`parse-claude-docs.py` が大きいのは
  2 source (code / platform) 対応・index↔full の URL join・anchor 生成・リンク注釈を
  持つためで、「llms.txt を読む」こと自体の複雑さではない

つまり「`SOURCES` に profile を 1 つ足すだけで動く」が成り立つのは、**既存 source と同じ形状の
サイト**に限られる。

## 2. 候補サイトの形状 (HTTP 実測 2026-09-19)

| サイト | `llms.txt` | `llms-full.txt` | full の区切り | ページ URL | 既存実装で足りるか |
|---|---|---|---|---|---|
| Cloudflare Developers | あり (**製品別 `llms.txt` へのリンク集 = 2 段 index**) | あり | YAML frontmatter | frontmatter に無い (本文の blockquote / Markdown リンク) | 不可 — 2 段 index の解決と URL 抽出の追加が必要 |
| Zod | あり | あり | H1 | **無い** | 不可 — index↔full の URL join が成立しない |
| Drizzle ORM | あり | あり | H1 (+ `Source:` 行) | `Source:` 行 | 近い — URL 抽出規則の追加で済む見込み |
| Next.js | ルートは散文ガイド / index は `/docs/llms.txt` | `/docs/llms-full.txt` | 未計測 | 未計測 | index と full の URL を別指定できれば近い |
| Vite / Vitest | あり | 未確認 | 未計測 | 未計測 | 未計測 |
| Tailwind CSS / Biome / Playwright | **404** | 404 | — | — | 対象外 (未公開) |

### 2026-09-26 実測 (Phase 1 の profile で全ページを分割して確認)

| サイト | 区切り | ページ URL | 分割したページ数 | 独立に数えた値 |
|---|---|---|---|---|
| Next.js (`/docs/llms-full.txt`) | frontmatter (`title:` / `url:` / `version:`)。先頭に frontmatter の無い前置きがある | frontmatter の `url:` (絶対) | 458 | `title:` を持つ frontmatter 461 から、コードブロック内の frontmatter 記述例 3 を除いた数 |
| Vite | frontmatter (`url:` のみ、`title:` なし) | frontmatter の `url:` (`/guide.md` 形式の相対) | 42 | `url:` を持つ frontmatter 42 (本文の水平線 `---` は数えない) |
| Vitest | 同上 | 同上 | 215 | 同上 215 |
| Drizzle ORM | **`Source: <url>` 行** (上表の「H1」は誤り。ページに H1 が無いことも多い) | `Source:` 行 | 496 | `Source: https://` 行 496 |
| Zod | H1 | なし | 16 | コードブロック外の H1 16 (ブロック内の `# ...` 2 は数えない) |

Drizzle は MDX を多用し、コードブロックの記号行が約 13,600 行ある。共有の `FenceTracker` で
追跡すると区切り行の大半を見失う (197 / 496)。原因の大半は、`FenceTracker` が言語名付きの
`` ```ts `` 行を「閉じ」とみなすこと (CommonMark では閉じない) で、残りは JSX の中で
インデントされた閉じ記号だった。`line` 区切りでは専用の追跡 (言語名付きの行では閉じない /
閉じ記号のインデントは問わない / 区切り行で状態をリセット) を使い、496 / 496 を拾う。
0.25.0 では、既存 3 script の出力を変えないよう、共有の `FenceTracker` には触れなかった
(0.26.1 で直した。下の「2026-10-03 追記 (0.26.1)」)。

いちばん効くのは **URL を持たない corpus (Zod) が実在する**こと。`_common.check_join_rate` は
index↔full の join 率が低いと失敗として扱うため、URL 無しの corpus は profile 追加だけでは
通らず「join しないモード」が必要になる。`[URL#anchor]` 出力や `--page-ref <slug>` も
同時に成立しなくなる。

### 2026-10-03 実測 (0.26.0 の同梱 presets)

同梱した presets を、取得した `llms-full.txt` に `--file` で流して確かめた。

| サイト | 区切り | ページ URL | 分割したページ数 | 独立に数えた値 |
|---|---|---|---|---|
| Next.js / Vite / Vitest / Drizzle / Zod | 2026-09-26 と同じ | 同じ | 458 / 42 / 215 / 496 / 16 | 2026-09-26 の値と一致 (サイト側の変化なし) |
| Hono | H1。先頭に `<SYSTEM>` 行と、本文の無い `# Start of Hono documentation` がある | なし | 87 (`skip_empty` で見出しだけの行を除く) | `llms.txt` の 90 項目から、ページでないリンク 3 (Full Docs / Tiny Docs / Examples) を除いた数 |
| Agent Skills (`agentskills.io`) | H1 の直後に `Source: <url>` 行 | `Source:` 行 | 9 | `Source:` 行 9 (H1 は 21 あるが、残りはコードブロック内の `SKILL.md` の見本) |
| Cloudflare D1 (`/d1/llms-full.txt`) | frontmatter (`title:` / `description:` / `image:`) | 本文の `[View as Markdown](…/index.md)` | 51 (URL あり 50) | `title:` 行 51。URL の無い 1 件は別テンプレートの API リファレンス |
| Cloudflare Workers (`/workers/llms-full.txt`, 5.1MB) | 同上 | 同上 | 455 (URL あり 455) | `title:` 行 455 |

Cloudflare の製品別ファイルは、全ページに `[Skip to content](#main-content)`、
`> Documentation Index` で始まる 3 行の blockquote、`Last updated …|Copy as Markdown|…` の行が
付く。このままでは「documentation index」などの語で全ページが検索に当たるため、profile の
`drop_lines` で除く。URL はその `Last updated` 行にあるので、除く前に読む。presets に入れた
15 製品はいずれも `/<製品>/llms-full.txt` が HTTP 200 を返すことを確かめた (`zero-trust` は 404 のため入れていない)。

Tailwind CSS / Playwright / Biome は、2026-10-03 時点でも `llms.txt` / `llms-full.txt` が 404 だった。

### 2026-10-03 追記 (0.26.1): 共有の `FenceTracker` の修正

共有の `FenceTracker` も、言語名付きの行 (`` ```ts ``) を閉じとみなさないように直した。これで
Drizzle の区切り行は共有の追跡でも 496 / 496 になる (インデントの扱いは変えていない。共有の
追跡はもともとインデントを問わない)。MDX コメントの中のブロックの終わり (`` ``` */} ``) は
閉じとして扱い、開きとしては扱わない (Drizzle / Zod に 6 行ずつある)。閉じとして扱うのは、
コメントがブロックの外で始まっている場合だけで、コメントの書き方の見本をブロックの中に載せた
ページでは、見本の `` ``` */} `` を本文として扱う (Drizzle / Zod の 12 行は、どれもブロックの外で
始まったコメントの終わり)。

0.26.0 と 0.26.1 に同じ `llms-full.txt` を `--file` で流し、全サブコマンドの出力を比べた
(CHANGELOG 0.26.1)。既存 3 script と、presets 22 件のうち 19 件は変わらない。

- ページ数が変わるのは Zod だけで、16 → 17 (`# Versioning` が前のページから独立する)。
  修正前は終盤の閉じ忘れのブロックで開閉が反転し、`# Versioning` をコードブロックの中と
  みなしていた。上の 2 つの表の「分割したページ数」16 は修正前の値。なお `# Release notes` は、
  MDX コメントの中で閉じ記号を書き損じた (記号が 2 つの) ブロックが閉じないため、修正後も
  ページにならない
- 見出しの抽出は Drizzle の 9 ページと Zod の 1 ページで良くなる (反転して隠れていた見出しが戻る)
- Cloudflare Browser Rendering の 1 ページ (WebMCP) では、見出し 5 つが本文扱いになる。外側の
  `` ``` `` のブロックの中に同じ長さの `` ```js `` のブロックを入れ子にしており、CommonMark では
  内側の閉じで外側が閉じるため。修正前は `` ```js `` を閉じと取り違えた結果、外側の閉じの位置で
  たまたま元に戻っていた。この並びは「閉じ忘れたブロックの後の、次のブロック」と行の形だけでは
  区別できないため、CommonMark に従う側に倒した。同じ形は、中に記号の行を含むコードの見本なら
  どこでも起きる (Cloudflare の製品別ファイルは 4 連の記号を使わない)。Browser Rendering で
  ページ分割が変わらないのは、各ページ末尾の `` ```json `` のブロックで状態が戻るためで、見本が
  ページ最後のコードブロックになると、frontmatter / H1 分割では次のページの区切りを見失う

### 2026-10-03 実測 (0.27.0 で足した 6 サイト)

過去の作業で参照していたサービスのうち、`llms-full.txt` を公開しているものを足した
(`docs.aws.amazon.com/llms-full.txt` は各サービスの案内ページへのリンク一覧で、本文が無いため
入れていない)。0.26.1 の `FenceTracker` で数えている。

| サイト | 区切り | ページ URL | 分割したページ数 | 独立に数えた値 |
|---|---|---|---|---|
| Bun | H1 の直後に `Source: <url>` 行 (Mintlify)。ページ本文にも H1 がある | `Source:` 行 | 319 (`h1_needs_url` で本文の H1 4 つを前のページに残す) | H1 の次の行が `Source:` の数 319 |
| MCP (`modelcontextprotocol.io`) | 同上 | 同上 | 152 (同じく 4 つ) | 同上 152 |
| Ollama (`docs.ollama.com`) | 同上 | 同上 | 69 | 同上 69 |
| Vercel (`/docs/llms-full.txt`, 10MB) | frontmatter を `-` 80 個の行で囲む。`description` が複数行の二重引用符の値になるページが 3 つ | frontmatter の `source:` | 1529 | `source:` 行 1529 (= 80 個の `-` の行 3058 の半分) |
| Render (`/docs/llms-full.txt`) | H1 (ファイル冒頭に「Each h1 designates a new page」とある) | なし | 125 | CommonMark どおりに数えたコードブロック外の H1 125。0.26.0 の `FenceTracker` では 110 (言語名付きの行 521 個で開閉が反転していた) |
| OpenAI Codex (`developers.openai.com/codex`。0.28.1 で `learn.chatgpt.com/docs` へ移転。下の「0.28.1: preset codex の移転」) | H1。カテゴリ名の H1 の直後が `---` だけ | なし | 178 (`skip_empty` でカテゴリ 9 つを除く) | コードブロック外の H1 187 − カテゴリ 9 |

`h1_needs_url` は、URL を持たない H1 を前のページの中の H1 見出しとして残す (行は失わない。
Bun / MCP / Ollama で本文の行数 + ページごとの H1 1 行がファイルの行数に一致することを確かめた)。

### 2026-10-03 実測 (0.27.1): Cloudflare 製品別 15 件

0.26.0 では Workers / D1 しか分割数を確かめていなかったため、presets の 15 製品すべてを測った。

| 製品 | ページ | URL あり | `title:` 行 | 差の理由 |
|---|---|---|---|---|
| workers | 455 | 455 | 455 | |
| d1 / durable-objects / queues / workers-ai / vectorize / hyperdrive / workflows / ai-gateway | 51 / 52 / 38 / 129 / 21 / 57 / 33 / 97 | 各 1 少ない | ページ数と同じ | URL の無い 1 件は各製品の API リファレンス (別テンプレート) |
| r2 / pages / agents / containers | 84 / 118 / 110 / 39 | 全件 | ページ数と同じ | |
| kv | 30 | 29 | 31 | API リファレンス本文の `title: string` (プロパティの説明) を数えている |
| browser-rendering | 47 | 0 | 48 | コード例の中の `title: "My First Post"`。旧テンプレートで `[View as Markdown]` が無く、URL は取れない |

あわせて分かったこと:

- **全製品の全ページ**に `Was this helpful?` と `YesNo` の行がある (0.26.0 の `drop_lines` は除いていなかった)。
  API リファレンスのページは `[Skip to content](#_top)` と別の anchor を使う。Browser Rendering の旧テンプレートは
  さらに `[ Edit page ](…)` / `Copy page` / コードブロック外の `Explain Code` / `Terminal window` を持つ。
  0.27.1 の `drop_lines` でこれらを除き、15 製品とも本文に残る定型行は 0
- Browser Rendering の `Quick Actions timeouts` は、`description:` の値をインデントせず次の行に続けて
  いる (YAML としては不正) ため、frontmatter と認識できずページが前のページに埋もれていた。キーの
  値の直後の行が Markdown らしくない (`#` `>` `[` `|` などで始まらない) ときは値の続きとして読む

### 2026-10-03 実測 (0.28.0)

**インデントされた対応のない閉じ fence** (共有の `FenceTracker`、4 script 共通): Firebase はリスト項目の
中のコードをインデントした行で書き、最後にインデント付きの `` ``` `` だけを残すことがある。0.26.1 で
インデント 4 以上の開きを認めたため、この行が新しいブロックを開き、後ろの見出しを隠していた。
インデント 4 以上の言語名の無い記号は、次の空でない行が同じ深さ以上で続いたときだけ開くようにした。
手元の 869 ファイル (278 万行。Claude / AI SDK / Firebase の corpus、presets 28 件、Firebase の個別
ページ) で、見出し・frontmatter の区切り・`Source:` 行が fence の外に見えるかを旧版と比べた:

| 変化 | 行 | 内訳 |
|---|---|---|
| 外に見えるようになった | 33 | Render 11、Firebase の個別ページ 4 件で 22 |
| 中に隠れるようになった | 0 | |

presets 28 件のページ数・URL 数は変わらない。

**`index_url`** (URL の無いページに `llms.txt` からタイトルの完全一致で URL を付ける):

| preset | URL が付いたページ | 付かなかったページ |
|---|---|---|
| zod | 12 / 17 | `llms.txt` で同じタイトルが重複 3 (`Codecs` / `Ecosystem` / `Versioning`)、ページ側で重複 2 (サイト冒頭と `packages/zod` がどちらも `Zod`) |
| hono | 21 / 87 | 完全一致する項目が無い 62 (本文の `Basic Auth Middleware` に対し `llms.txt` は `Basic Auth`、など)、`llms.txt` で重複 2、ページ側で重複 2 |
| render | 121 / 125 | 完全一致する項目が無い 4 |
| codex | 175 / 178 | 完全一致する項目が無い 1 (全体の表題)、ページ側で重複 2 |
| cloudflare-browser-rendering | 40 / 47 | 完全一致する項目が無い 7 |

- 語の前方一致まで広げると、Hono では付く 40 件のうち 4 件が別のページの URL だった。ページ側の
  重複を見ないと、Zod のサイト冒頭のページに `packages/zod` の URL が付く。どちらも付けない側に倒した
- URL にタイトルの語が含まれない対応も目視し、誤対応は無かった

**入れ子の同じ長さの fence の見本** (0.26.1 の節の最後の項目。対応していない): 見本の後ろで区切りを
見失う形は presets 28 件では起きていない。1 行ずつ見る判定 (言語名付きの行を中身として飲み込んで
閉じたブロックの直後の、言語名の無い開きを疑う) を試したが、WebMCP の見本は中の `` ```js `` が 2 つ
あるため 2 つ目を本物の開きと読んで本物のずれを見逃し、`` ```json `` で状態が戻った直後の本物の
ブロックを疑った。Cloudflare はすべてのコードブロックを言語名の無い `` ``` `` で出し (言語名は前の
行に書く)、Browser Rendering にはその中の frontmatter の見本もあるため、「言語名の無い fence の中の
区切りは採用する」という規則も使えない。

### 2026-10-03 実測 (0.28.1)

**`index_url` で別の `llms.txt` を指す項目を除く**: 2 段の索引 (他の `llms.txt` へのリンク集) を
`index_url` に書くと、別の `llms.txt` を指す項目がページとして突き合わされ、そのファイルの URL が付いた
(OpenAI のルートの `llms.txt` では、Sign in with ChatGPT のページに `siwc/llms.txt` が付いた)。
`llms.txt` の項目 (絶対 URL のもの) の URL の最後の区間を実測し、`llms.txt` と `llms-<名前>.txt` の形を
索引ファイルとして除く。

| 索引 | 項目 | 最後の区間が `llms.txt` / `llms-<名前>.txt` | 内訳 |
|---|---|---|---|
| OpenAI のルート (2 段) | 40 | 12 | すべて `llms.txt`。残り 28 は `.md` のページ 19 と拡張子の無いリンク 9 |
| Cloudflare のルート (2 段) | 113 | 113 | すべて `/<製品>/llms.txt` |
| Zod / Render / Cloudflare Browser Rendering | 326 / 359 / 52 | 0 / 0 / 0 | |
| Hono | 89 | 1 | `llms-small.txt` (Tiny Docs) |
| Codex (`developers.openai.com/codex/llms.txt` の転送先) | 189 | 2 | `llms-full.txt` (Combined ChatGPT docs)、`use-cases/llms.txt` |
| 専用 script の索引 (Claude Code / Claude Platform / Firebase) | 231 / 753 / 7209 | 0 / 0 / 0 | 専用 script は `join_index_urls` を使わず、この変更で出力は変わらない |

- `index_url` を持つ presets 5 件 (zod / hono / render / codex / cloudflare-browser-rendering) に同じ
  `llms-full.txt` と `llms.txt` を流し、0.28.0 と 0.28.1 でページごとの URL を比べた。ページ数と URL の
  付いたページ (17 中 12 / 87 中 21 / 125 中 121 / 178 中 175 / 47 中 40) もページごとの URL も変わらない
  (差の行 0)。Hono と Codex の索引ファイルの項目は、どのページのタイトルとも一致していなかった
- 除くのは `llms.txt` / `llms-full.txt` / `llms-small.txt` / `llms-ctx-full.txt` の形 (大文字小文字・
  query・fragment・末尾のスラッシュは問わない)。`llms.txt.md` (ページ)、`not-llms.txt`、
  `/llms.txt/intro` (ディレクトリ名) は除かない。除いた項目は、同じタイトルの別の項目との重複にも数えない
- 防げないもの: OpenAI のルートに残る 28 項目のうち、`url` の `llms-full.txt` に無い別の docs のページ
  (学習トラックの `Model optimization` など) の項目は、ページとタイトルが衝突すると URL が付く。
  タイトルだけでは本物の一致と区別できないため、2 段の索引を `index_url` に書かないことを README に書いた

### 2026-10-03 実測 (0.28.1): preset codex の移転

`developers.openai.com/codex/llms-full.txt` と `/codex/llms.txt` は HTTP 308 で
`learn.chatgpt.com/docs/llms-full.txt` / `/docs/llms.txt` (別ホスト) へ移った。urllib は 3.11 から 308 に
追従し (README の要件は 3.11 以上。3.14 で追従を確認)、移転元から取った内容と移転先から直接取った内容は
バイト単位で同じ (full 2,285,847 バイト、`llms.txt` 30,990 バイト)。移転元のままでも読めるが、
preset の `url` / `index_url` を移転先にし、`description` を更新した
(取得先が変わるとキャッシュのファイル名が変わるため、初回は再取得する。古いキャッシュは残る)。

- ファイルの冒頭は `# Codex — full documentation` と「Single-file Markdown export of ChatGPT docs for
  Codex across the CLI, IDE, cloud, and SDK.」。Codex のページに加えて、ChatGPT の製品ページ
  (desktop app、Work、管理者向けの設定・セキュリティ・分析など) を含む
- ページ数は 178、URL が付くのは 175 で、移転前の値 (0.28.0) と同じ。付かない 3 つは、全体の表題
  (`Codex — full documentation`) と、ページ側で重複する `Permissions` 2 つ
- `llms.txt` の死にリンクが 2 件 (どちらも HTTP 404): `use-cases/llms.txt` と `videos.md`。前者は索引
  ファイルなので除かれ (0.28.1)、後者はタイトルの合うページが無い。どちらもページの URL にはなっていない。
  別のタイトルの項目 2 つが同じ URL を指す組が 2 つある (`developer-commands.md?surface=cli` と
  `?surface=ide`)。この 4 ページは、組ごとに同じ URL を持つ。どれもサイト側の問題で、手を入れない
- `index_url` は `url` の `llms-full.txt` と同じディレクトリの `llms.txt` (`/docs/llms.txt`)。`index_url` を
  持つ presets はいずれもこの形 (この時点では 5 件。0.29.0 で cloudflare-browser-rendering から外して、
  zod / hono / render / codex の 4 件)。移転元が転送して
  いる間は片方だけ古くても動くため気付きにくく、`url` と `index_url` が別のサイト・別の範囲を指さない
  ことをテストで固定した

### 2026-10-04: Cloudflare Browser Rendering の移転 (製品名が Browser Run に)

`/browser-rendering/llms.txt` は HTTP 301 で `/browser-run/llms.txt` へ移っていた。`llms-full.txt` は
`/browser-rendering/` 側が 200 のまま残っているが、**内容は移転先と同じではなく、古い版**だった。

| | `/browser-rendering/llms-full.txt` (旧) | `/browser-run/llms-full.txt` (新) |
|---|---|---|
| 大きさ | 422 KB | 667 KB |
| ページ数 (preset で分割) | 47 | 52 |
| ページ内の `View as Markdown` の URL | 0 | 51 |
| 同じディレクトリの `llms.txt` で URL が付いたページ | 40 | 0 (必要なかった) |

- `llms.txt` は旧 URL と新 URL で内容が同じ (バイト単位で一致、52 項目)。旧の `llms-full.txt` は製品の改名前の
  テンプレートで、ページ内に URL を持たない。新は他の Cloudflare の製品別ファイルと同じテンプレートで、
  ページの URL を自分で持つ
- preset `cloudflare-browser-rendering` の `url` を `/browser-run/` に移し、`index_url` は外した。source 名は
  利用者が使っているので変えない。新しい `llms-full.txt` はページの URL を自分で持ち、`llms.txt` で URL が
  付くページは 0 だった (URL の無い 1 ページは、他の製品と同じ API リファレンス)。
  効かない `index_url` を残すと、取得と点検の対象が 1 つ増えるだけなので、他の Cloudflare 製品と同じく
  持たない形にした
- 他の 14 件の Cloudflare 製品と、`index_url` を持つ他の presets の取得先は、点検スクリプト
  (`scripts/check-preset-urls.py`) の実行で 200 のままだった

### 2026-10-04 実測 (0.30.0 で足した 7 サイト)

利用記録で、skill を使ったあとに WebFetch へ戻った先 (Cursor / Codex の plugin docs) を優先して足した。
`--file` / `--index-file` で取得したファイルを流し、独立に数えた値と突き合わせた。

| サイト | 区切り | ページ URL | 分割したページ数 | 独立に数えた値 |
|---|---|---|---|---|
| Agent Plugins (`agent-plugins.org/llms.txt`。`llms-full.txt` は 404 で、`llms.txt` が全文) | frontmatter (`title:` / `description:`) | なし | 13 | `title:` 行 13 |
| OpenAI Plugins (`developers.openai.com/plugins/llms-full.txt`) | H1 (先頭は索引の見出しだけのページ) | `index_url` で 30 | 31 | `llms.txt` の項目から、タイトルが一致する 30 |
| OpenAI API docs (`developers.openai.com/api/docs/llms-full.txt`, 5MB) | 同上 | `index_url` で 220 | 232 | 同上 220 |
| ACP (`agentclientprotocol.com`) | H1 の直後に `Source: <url>` 行 | `Source:` 行 | 126 | H1 の次の行が `Source:` の数 126 |
| Cline (`docs.cline.bot`) | 同上 | 同上 | 113 | 同上 113 |
| Devin (`docs.devin.ai`, 3MB) | 同上 | 同上 | 592 | 同上 592 |
| Factory (`docs.factory.com`) | H1 | `index_url` で 104 | 104 | `llms.txt` の項目 105 のうちタイトルが一致する 104 |

ACP の `llms-full.txt` は schema のフィールドの required 属性を落としている (サイト側の欠落。
必須かどうかは ACP の schema 本体で確かめる)。

見送ったもの: AWS Bedrock。`docs.aws.amazon.com/llms-full.txt` は各サービスの案内ページへのリンク一覧
(本文なし)、`/bedrock/latest/userguide/llms.txt` はページごとの `.md` へのリンク集で、
`llms-full.txt` は 404 (ページごとに別ファイル) のため、この loader の 3 形状のどれでも読めない。

### 2026-10-04 追記 (0.35.2): 閉じ損ねたブロックの後ろの H1 ページ

H1 で分割する source で、閉じ損ねたコードブロックが次の H1 を隠し、ページが 1 つ前のページに
吸収されていた。共有の `FenceTracker` を 2 点直した。

- **MDX コメントの中で開いたブロック**は、コメントが終わる行 (`*/}` を含み、その前に同じ行の `/*` が
  無い行。記号の行でなくてよい) で閉じる。MDX はコメントを描画しないため。Zod は `{/* ... */}` の
  中のブロックを記号 2 つで閉じたつもりになっており、上の 0.26.1 の追記で「修正後もページにならない」
  とした `# Release notes` がこれで独立する。`function f() { /* noop */}` や `<div>{/* note */}</div>`
  のように同じ行で開いて閉じるコメントはコードの一部で、ブロックを閉じない。コメントの外で開いた
  ブロックの中の `*/}` は、これまでどおり本文
- **引用 (`>`) の中のフェンス**は、行頭の `>` (と直後の空白 1 つ) を外してから判定する。外すのは
  ブロックの外と、引用の行で開いたブロックの中だけ。引用の行で開いたブロックは、引用の外の
  記号の行でも閉じる (Render は注記の中で開いて、注記の外で閉じる)。引用なしで開いたブロックの
  中の `>` 行は本文 (引用のコードブロックを見せる Markdown の見本を閉じない)。引用の行で開いた
  ブロックは、`>` の付かない空行でも閉じる (引用がそこで終わるため。CommonMark と同じ)。閉じの
  無い引用の中のブロックが、後ろの節や H1 ページを隠さない

Render は CommonMark では、引用が終わった所で引用の中のブロックも終わるため、次の行の記号が
新しいブロックを開いてしまう。上の表の「独立に数えた値」125 はこの数え方で、書き手の意図
(注記の中のコード) とずれる。`# Deploy a Prebuilt Docker Image` は `llms.txt` に独立した項目
(`deploying-an-image.md`) がある実在のページなので、書き手の意図に合わせる側に倒した。

0.35.1 と 0.35.2 で、手元のキャッシュにある全 source (presets 19 件 + 既存 3 script + Firebase の
個別ページ 56 件) のページの並びと、各ページの節を比べた。変わったのは Zod と Render だけ。

| source | ページ数 | 節の数 | 変わったページ |
|---|---|---|---|
| zod | 17 → 18 | 306 → 309 | `Release notes` が独立 (節 30)。`Migration guide` は 87 → 60 (Release notes の節が抜ける) |
| render | 125 → 126 | 2169 → 2189 | `Deploy a Prebuilt Docker Image` が独立 (節 19)。`Docker on Render` は 21 → 9 (同じページの節が抜け、隠れていた `Docker or native runtime?` など 6 つが戻る)。`Deploying on Render` は 29 → 42 (引用の中のブロックの後ろで隠れていた `Managing deploys` / `Deployment concepts` 以下が戻る) |

### 2026-10-04 追記 (0.35.3): 節アンカー項目と同じタイトルのページ

zod の `Codecs` / `Ecosystem` / `Versioning` に URL が付くようになった (18 中 13 → 16)。llms.txt で、同じタイトルの節への項目 (`?id=…`) と並んでいたため曖昧として扱われていた。`?` か `#` を含まない項目がちょうど 1 つならそれを使う。index_url を持つ他の source は変化なし (Codex の `?surface=cli` のような、クエリ付きの本物のページは同じタイトルの項目と重ならず、これまでどおり付く)。

## 3. 需要の根拠

`llms.txt` を公開していて、かつこの plugin が使われる環境で実際に依存しているもの:
**Cloudflare Workers (wrangler) / Next.js / Zod / Drizzle ORM / Vite / Vitest**。
「需要が 2 サイト以上出てから着手」という当初の着手条件は、実測で満たされている。

一方、現行 3 source (Claude / AI SDK / Firebase) は仕様の厳密さが要求されるドメインで、
`WebFetch` の要約経由では field 欠落が致命的になるため専用 skill の価値が高い。上記候補は
そこまで厳密さを要求しないものも混ざるので、**需要はあるが優先度は現行 3 source より低い**。

## 4. コスト見積り

- **Phase 0 (前提条件)**: 6 サブコマンド層を `_common` に寄せる。現状 3 重化 1,014 行で、
  これを放置して 4 本目を足すと 4 重化する。中規模リファクタ + 既存 263 テストの回帰確認込み
- **Phase 1 (profile の外部化)**: `sources.json` + `--source <name>`。ただし profile が持つ
  べきものは URL 対だけでは足りず、**splitter 種別 (H1 / frontmatter / per-page fetch)・
  URL 抽出規則・index join の有無**が必要。「URL 対だけ」の設計では上表の候補の大半で動かない
- **Phase 2 (汎用 skill)**: description ベースの auto-invoke は対象ライブラリを書けないため、
  明示起動 (`user-invocable`) の skill に限定するのが妥当。既存 3 skill の auto-invoke は温存

## 5. 推奨

**backlog 継続 (close しない)。** 着手条件 (需要 2 サイト以上) は実測で満たされたため、
残る判断はコストを払うかどうかだけになった。ただし当初案の Phase 1 は前提が甘いので、
「URL 対の外部化」から「**形状 profile の外部化 + コマンド層の共通化を前提条件に置く**」へ
書き換えてから着手すること。

未計測として残したもの: なし (2026-09-26 に Next.js / Vite / Vitest を実測済み)。
