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
>
> **2026-10-16 追記 (0.46.0)**: 専用 parser 3 本 (Claude docs / AI SDK / Firebase) を汎用 loader に統合した。
> 当時の 4 skill とも `parse-llms-txt.py … --source <name>` (`claude-code` / `claude-platform` / `claude` / `ai-sdk` /
> `firebase`) を使う。専用 script の名前は 0.46.0〜0.47.x の間だけ shim として残り、0.48.0 で旧実装とともに
> 削除した。以下の表や本文で専用 script を名指ししている箇所は、計測した時点の状態の記録として残している
> (旧実装は 0.47.3 以前の `scripts/tests/legacy/` で読める)。
>
> **2026-11-03 追記 (0.52.0)**: skill を 3 本に再編した (`researching-claude-docs` / `researching-cloudflare-docs` /
> `researching-library-docs`)。`ai-sdk` / `firebase` は `researching-library-docs` が読む。
>
> **2026-11-17 追記 (0.53.0)**: PixAI API の docs を preset `pixai` で足した。3 言語を 1 ファイルに連結した
> `llms-full.txt` で、見出しが `#` の無い行になっている (下の「2026-11-17 実測」)。

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
  `?surface=ide`)。この 4 ページは、組ごとに同じ URL を持つ。どれもサイト側の問題で、手を入れない。
  URL の `page_ref` (と anchor) は、この組では先頭のページを黙って開かず候補を列挙する (0.47.3)
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

### 2026-11-03 追記 (0.49.0): frontmatter 区切りの分割が怪しいときの警告

frontmatter 区切りはコードブロックの中の `---` を境界にしない (コード中の frontmatter の見本をページにしないため)。
その裏返しで、閉じ損ねたブロックがあると後ろのページの境界をすべて前のページに吸収する。Next.js の `llms-full.txt` は
2026-10-15 の取得版で閉じ記号と次の行が改行なしで連結しており (`` ```## … ``)、474 ページ中 9 ページしか分かれず、何も
警告が出なかった (翌日の取得版は正常で、上流の一過性の不具合とみられる)。

分割の規則は変えず、検出だけ足した。stderr に WARNING を出すのは次の 3 つ:

- ファイルがコードブロックの中で終わる (この Next.js の例はこれで捕まる。`nextjs` preset には `index_url` が無い)
- `index_url` の `llms.txt` が手元にあり (`--index-file`、ページの対応づけで読むもの、または取得し直さず使えるキャッシュ)、
  ページ数がその異なる URL 数の半分を下回る。同梱 preset の手元のキャッシュでの比は 0.97〜1.06 で、Next.js の例は 0.02
- 必須のキーを持つ frontmatter が `_FM_LOOKAHEAD` (400 行) までに閉じない (上限で打ち切られたブロックはページにならない)

同梱 preset の手元のキャッシュ (18 source) では、どれも発火しない。捕まえられないのは、`index_url` の無い source で、閉じ損ねの
後でファイルの末尾までに閉じ直している場合 (Next.js のキャッシュの閉じ記号を 1/4 だけ連結した実験で、473 ページが 363 ページになる例)。

### 2026-11-03 実測 (0.51.0): ページごとのサイトと、形の小さな違い

**ページごとに取得するサイト** (`body: fetch`)。Zed と Amp だけに対応する。

| サイト | `robots.txt` | `llms.txt` の項目 | ページの形 |
|---|---|---|---|
| Zed (`zed.dev/docs/llms.txt`) | `Allow: /`。拒否は `/_next/` の一部だけ | 190 (すべて `.md`) | 各ページの先頭に同じ blockquote が 1 行 (`drop_lines` で除く) |
| Amp (`ampcode.com/llms.txt`) | `Allow: /`。`/auth/`・`/api/`・`/admin` などを拒否 | 64 (すべて `/docs/markdown/…`) | 先頭に `lastModified` の frontmatter と `<script>`。H2 の一部が `<h2 id="…">` の行 (`heading_line_pattern` で節にする) |

- 対応しないもの: Kiro (`robots.txt` が `/llms.txt` と `/docs/*.md` を拒否)、Cursor (全件の取得が前提で、上限が決まるまで見送り)
- Amp は未知のパスを `/auth/sign-in` へ転送して 200 の HTML を返す。転送先は `robots.txt` が拒否する場所なので
  `respect_robots` が転送の前に止める。HTML の本文は全 source で取得の失敗として扱う
- `urllib.robotparser` は使えない。最初に一致した規則で決めるため、1 行目が `Allow: /` の両サイトではすべて許可になる
- エンコードは比べる前に揃える。ASCII の文字の escape は復号する。`%25` / `%2A` / `%24` は記号として読まないよう escape のまま残す
- 予約文字の escape (`%2F` / `%3F` など) は規則の種類で分ける。`Disallow` では復号して比べ (RFC 9309 の表の `https%3A%2F%2F` と同じ扱い)、拒否は広めに当たる。`Allow` では規則とパスのどちらも復号せず、綴りが同じときだけ当たる。`Allow: /a%2Fb` は `/a/b` の拒否を打ち消さない。逆に `Disallow: /` + `Allow: /?u=https://x` は `/?u=https%3A%2F%2Fx` を拒否する (取りこぼしの側に倒す)。一致の長さはどちらも復号した綴りで数える
- 通信は逐次 (`page_fetch_workers: 1`)。全ページを走査する `search-content` は `search_content_needs_page_ref` で止める

**`sitemap.xml` は索引として採用しない。** どのサイトの sitemap にもタイトルが無く (`<title>` の類は 0 件)、タイトルを
得るには全ページの取得が要る。`llms.txt` があるサイトでは増えるものも無い。

| サイト | sitemap の URL | `llms.txt` / ページとの対応 | タイトル |
|---|---|---|---|
| Zed (`/docs/sitemap.xml`) | 190 (`.html`) | 190 / 190 が一致 (`.html` ↔ `.md`)。増えるページ 0 | 0 |
| Amp (`/sitemap.xml` の `/docs`) | 90 | 64 / 64 を含む (`/docs/…` ↔ `/docs/markdown/…`)。残り 26 は `/docs/using-amp/…` で、`.md` の所在が不明 | 0 |
| Gemini CLI (`sitemap-0.xml`) | 104 | ページ 97 / 97 を含む。残り 7 は docs の外 (`/brand-kit`、`/terms` など) | 0 |
| Kilo Code (`/docs/sitemap.xml`) | 210 | ページ 212 のうち 209 と一致 | 0 |
| A2A (`/latest/sitemap.xml`) | 38 | `<file>` 158 件のうち SDK の 124 件などは対応なし | 0 |
| OpenAI Agents SDK JS (`sitemap-0.xml`) | 5024 (4 言語 × 1256) | 英語の 1256 はページ数と同じ。タイトルが無いので、ページに URL を付けられない | 0 |
| Continue (`sitemap.xml`) | 92 | ページ 92 と同数 | 0 |

**形の小さな違い** (`--file` で全ページを分割して確かめた)。

| サイト | 区切り | ページ URL | ページ | 独立に数えた値 |
|---|---|---|---|---|
| Gemini CLI | H1 全体がリンク (`page_url: h1-link`)。リンクでない H1 は前のページの見出し | リンク先 97 | 97 | リンクの H1 97 |
| Kilo Code | `## Source: /<path>` (`url_prefix`)。21 ページのタイトルは Markdoc の式のままなので frontmatter の `title:` から読む (1 ページは先頭に BOM) | 212 | 212 | `## Source:` 行 212 |
| OpenAI Agents SDK JS | H1 | なし | 1256 (タイトルの重複 517 種) | sitemap の英語の URL 1256 |
| Continue | H1 の直後に `Source:` 行 | 92 | 92 | H1 の次の行が `Source:` の数 92 |

**`ai-sdk` の索引に H3 を読む** (手元の `ai-sdk` のキャッシュ 541 ページ。`index_heading_level` 2 と 3 の `search` の上位 5 件を比べた):

| クエリ | 件数 | 持ち主のページが上位 5 件にある (2 → 3) | 順位が良くなった / 悪くなった |
|---|---|---|---|
| H3 にだけ書かれた API 名 | 71 | 44 → 63 | 悪化で消えたもの 0 (`maxOutputTokens` の `Settings` など 19 件が新たに届く) |
| H3 の見出し (無作為) | 250 | 194 → 206 | 18 / 1 |
| H2 の見出し (無作為) | 250 | 227 → 227 | 0 / 0 |
| ページのタイトル | 501 | 496 → 496 | 5 / 3 (悪化はどれも 1 つ下がるだけ) |

- 旧版の上位 3 件にあり新版の上位 5 件から消えたページは、1,282 クエリで 139 件 (16 件は 6 行目以降に残り、123 件は一覧から消える)。133 件はクエリを見出しに持たないページで、本文のヒットで入っていたもの。
  残り 6 件は複数のページが持つ見出し (`Anthropic` / `Methods` / `Node.js` / `Set Up Your Application` / `Utilities`) で、そのページの H2 にある
- 悪化の型 (1): タイトルが 1 語に一致し、もう 1 語が本文にだけある `useChat transport` は、`useChat` が 4 位から 5 位に下がる (消えない)
- `h3_only_keeps_full_search` (`ai-sdk` だけが使う): 候補に本文のヒットがあると全文検索の補いが走らないので、H3 だけの候補を除いて補いの要否を判定する。`citations` は `Generating Text` (10 ヒット) が 1 位のまま、`Anthropic` (10) / `OpenAI` (5) / `xAI Grok` (3) が一覧に残る
- 悪化の型 (2): 補いの行はヒット数によらず候補の下に付く (1 位を変えないため)。`citations` の並びは `Generating Text` (10) / `Amazon Bedrock` (3) / `Anthropic` (10) / `OpenAI` (5) / `xAI Grok` (3) で、H2 までの索引では 2 位だった `Anthropic` が 3 位になる
- 悪化の型 (3): 押し出し。H3 の +1 で上がったページが候補の上位 5 件の枠を取る。本文のヒットがあるページが一覧から消えるクエリは 1,282 のうち 189、ページは 286。すべて押し出しで、それ以外の経路は 0 件。
  265 ページは索引の上位 5 件にいた候補そのもの (枠を取ったのが H1 / H2 にも当たるページ 173、H3 だけのページ 86、両方 6)。21 ページ (8 クエリ) は補いで入っていたページで、押し出しで入った H1 / H2 の候補にヒットがあって補いが止まる。56 クエリ (80 ページ) では、ヒット 0 の行を出しながらヒットのあるページを失う
- H2 までの索引の 1 位を失うクエリは 11。どれも同点か 1 点差の順で枠から外れたもの:
  - 1 位が良くなる: `AI SDK UI` / `Streaming Custom Data` はタイトルそのもののページが 1 位に上がる。`` `prepareStep` callback `` は H3 の持ち主が 1 位に入る。`Frameworks` は H3 の持ち主が 4 位に入り、旧 1 位 (`Navigating the Library`、見出しにその語が無いページ) が外れる
  - 旧 1 位がもともと外れ: `How It Works` (大文字小文字違いの 3 クエリ) の旧 1 位は、タイトルの部分一致で入った `Fireworks`
  - 悪くなる可能性があるもの: `Result` の `Batch`、`Settings` の `Configuring Call Options`、`UI` の `UIMessage` は、本文のヒットが多いページが同点の順で外れる (`Settings` と `UI` のページ自身は残る)。`Chatbot Tool Usage` は `Tool Calling` が補いから外れ、`Chatbot` (3 ヒット) が 1 位に入る (タイトルそのもののページ (61 ヒット) は 3 位から 2 位)
- 1 位が本文ヒット 0 のページになるクエリは増えない。`_demote_h3_only` は本文ヒットが 0 の候補を基準にしないので、H3 だけで候補に入ったヒットのあるページが、その下に下がらない。基準にしていると `citations` の 1 位が MCP のページ (0 ヒット) になる。`ai-sdk` は 3 件 → 3 件で同じクエリ
- この規則は `index_heading_level` が 3 の他の preset の順位も変える。手元のキャッシュの 14 source、3,968 クエリのうち 49 件で順位が変わり、どれも本文ヒットが 0 のページが、ヒットのあるページの下に移るだけ (上位 5 件の顔ぶれは同じ)。1 位がヒット 0 のクエリは 20 件から 5 件に減る

見送ったもの: A2A。`<file path="…">` で囲んだファイルの連結で、158 件のうち 127 件に H1 が無く、新しい区切り方と、
ファイルのパスからページ URL を作る規則が要る。この形は 1 サイトだけなので、コストに見合わない。

### 2026-11-17 実測 (0.53.0): PixAI (3 言語を 1 ファイルに連結)

`platform.pixai.art/llms-full.txt` (494 KB、10,843 行) と `/llms.txt` を、preset `pixai` で `--file` を使わずに
取得して (キャッシュは作業用のディレクトリ) 確かめた。`robots.txt` は `Allow: /` で、拒否は `/api/search` だけ。

**3 言語**: 英語 (1〜3,616 行)・日本語 (3,617〜7,232 行)・韓国語 (7,233 行〜) の順に、同じ 39 ページが同じ順で並ぶ
(39 組すべてで、3 言語のページの `##` の行とコードブロックの記号の行の数が一致する)。言語別の `llms-full.txt` は無い。
言語で絞る仕組みは足さず、3 言語のまま読む (日本語・韓国語の語でも引けるため)。

**分割** (`split: h1`):

| 数えたもの | 値 |
|---|---|
| H1 の形の行 | 126 |
| うちコードブロックの中 | 9 (言語ごとに 3。`Get an upload URL for an image` の例の `# 1. Get an upload URL` など、シェルのコメント) |
| コードブロックの外 (= ページ) | 117 (言語ごとに 39)。preset で分割したページ数・タイトルの並びと一致 |
| 最初の H1 の前の行 | 0 |
| 本文が空のページ | 0 (`skip_empty` は要らない) |

コードブロックは、開きのインデントを 0〜3 に限る数え方と、インデントを問わない数え方の両方で数えて、どちらも同じ値になる。
ページの本文の行数と H1 の行数の和は、ファイルの行数 (10,843) に一致する。

**URL** (`index_url`): ファイルにページの URL は無い。`llms.txt` の項目は 43 で、ページ 38、OpenAPI の spec 2 (`.yaml`)、
索引のファイル 3 (`/ja/llms.txt`・`/ko/llms.txt`・`/llms-full.txt`。突き合わせから除く)。タイトルの突き合わせで URL が付くのは
英語の 39 ページのうち 37 (117 ページ中 37)。付かないのは、題が 3 言語で同じ `Webhook` (ページ側の重複) と、`llms.txt` に
載っていない旧 v1 の `Create a generation task`。日・韓のページには付かない。付いた 37 の URL は、どれも同じタイトルの項目の URL。

**見出しの 2 つの形**:

- API リファレンスのページ (言語ごとに 23): 通常の見出し。`## POST https://api.pixai.art/v2/image/create` の下に
  `### Request body (…)` / `### Example (cURL)` / `### Responses`。コードブロックの外の H2 は 69 行
- 文章のページ: `#` の無い 1 行 `見出し [#id]` (fumadocs の見出しの id の書き方から `#` だけが落ちた形。各ページの `.md` 版も同じ形)。
  198 行 (言語ごとに 66)。`heading_line_pattern` (`^\s*(.+?) \[#[^\]\s]+\]\s*$`) で H2 として読む
  (0.54.0 で `[#id]` ごと H2 にする形に変えた。下の「2026-11-17 実測 (0.54.0)」)

`見出し [#id]` の 198 行は、どれも見出しだった:

| 確かめたこと | 値 |
|---|---|
| コードブロックの中でこの形に一致する行 | 0 |
| 直後の行 | 198 行すべて空行 |
| 直前の行 | 空行 138、Steps の `<div className="fd-step">` 60 |
| Steps の 60 行 | すべてインデント 4、id は `-step` で終わる |
| コードブロックの外で `[#id]` を含むのにこの形に一致しない行 | 0 |
| 行頭が `#` `*` `-` `\|` `>` の行、`[#` を 2 つ含む行 | 0 |

日・韓のページの id は、同じ位置の英語の行と同じものが言語ごとに 9 (`#claude-code` / `#cursor` / `#vs-code` / `#steps` /
`#recommended-models` とモデル名)。残りは訳した見出しから作られている (`#エディターやエージェントに接続する`)。

見送ったキー:

- `anchor_style`: 見出しの文字から作る推定の anchor (`github`) が明示の id と一致するのは 198 行中 122 (英 44 / 日 36 / 韓 42)。
  Steps の `-step` の付いた id (`Submit a Task` は `#submit-a-task-step`)、`Tsubaki.3` の `#tsubaki3`、日本語の全角の記号の
  扱いが合わない。誤った anchor は付けない方がよいので付けない (0.54.0 で明示の id を anchor に使うようにしたので測り直し、
  やはり付けない。下の「2026-11-17 実測 (0.54.0)」)
- `rank_from: index`: `llms.txt` の項目 (英語のページの説明) でランキングすると、本文にだけある語のページが候補に入らない
  (`contextImages` は、説明にその語を持つ `Get an upload URL for an image` だけが出る)。`Webhook` は項目と本文の対応づけを
  失い、`search webhook` が `(skip: no full-text …)` になる

**3 言語の写しと順位** (識別子と語の 30 クエリで `search` の上位 5 件を数えた): 22 クエリで日・韓の写しが入る。写しが同じ
ページの英語の行より上に来たのは 0、英語の行が無いのに写しだけが入ったのも 0 (写しは本文のヒットが英語のページと同じに
なりやすく、同じなら index のスコア (英語のタイトル) とページ番号の順で英語のページが上に並ぶ)。

見出しはすべて H2 として読むので、元のページの階層 (`Connect your editor or agent` の下の `Claude Code` / `Cursor`) は残らない。
子の見出しを持つ見出しの節は本文が空になる。

### 2026-11-17 実測 (0.54.0): 見出しに書かれた id

0.54.0 は見出しの末尾の id (`{#id}` などの属性ブロックと fumadocs の `[#id]` / `\[#id]`) を節の名前から外す (README の
「見出しに書かれた id」)。0.53.0 と 0.54.0 を 1 つのプロセスに並べ、同じキャッシュ (取得は 2026-10-03〜11-17。比べる間の
ネットワークの呼び出しは 0) を読ませて比べた。

**データの差** (キャッシュのある 23 source。ページのタイトル・URL・本文、各ページの節、索引の見出し):

| source | id の形 | 節が変わったページ | 変わった節 / 全節 | 本文 | 索引の見出しが変わったページ |
|---|---|---|---|---|---|
| Zed (キャッシュの 14 ページ) | `{#id}` | 10 | 97 / 471 | 差 0 | 10 |
| Agent Plugins (13 ページ) | `[#id]` | 11 | 95 / 108 | 差 0 | 11 |
| Zod (18 ページ) | `\[#id]` | 1 | 5 / 309 | 差 0 | 1 |
| PixAI (117 ページ) | `[#id]` (`heading_line_pattern` で H2 にする行) | 36 | 198 / 438 (名前は同じ) | 36 ページの 198 行が `## 見出し [#id]` に | 0 |
| ほかの 19 source | なし | 0 | 0 | 差 0 | 0 |

Zed / Agent Plugins / Zod の差は、節の名前から id が外れたことと、id のある節に書かれたままの値と id のキーが増えたことだけ。
ページのタイトルの差はどの source も 0 (Zed は `llms.txt` の項目、Agent Plugins は frontmatter の `title:` で、Zod の H1 は
id を持たない)。id の無い 19 source のうち 11 (Claude Code / Claude Platform / AI SDK / Cloudflare Workers / D1 / Codex /
Hono / Next.js / Gemini CLI / Vercel / Firebase) では、`search` / `search-index --show-sections` / `search-content` /
`fetch-index` / `sections` / `content` (見つからない見出しを含む) の 308 コマンドの出力も 0.53.0 と 1 バイトも違わない。

**前の版で開けた引数**: id を書く 5 つの corpus (上の 4 つと Vitest の suite の fixture) の各節を、新しいパス・新しい題・
書かれたままのパス・書かれたままの題・id・`#id` (それぞれ小文字にしたものも) で `content` に渡した:

| 確かめたこと | 値 |
|---|---|
| 新しいパスが、その節 (同じパスの節が前にあるときはその最初) を開く | 818 / 818 |
| 0.53.0 で開けた引数が、0.54.0 でも同じ節を開く | 6,300 / 6,300 |
| id と `#id` が、その id を持つ最初の節を開く | 784 / 784 |

0.53.0 では、id を外したパス (ページに見える見出しをつないだもの) でその節が開かなかったのが、Zed で 409 中 38、Agent Plugins で
106 中 53 (`Overview` は子の節と曖昧になり、`Overview/Checkpoints` は見つからない)。

**部分一致の段**: 部分一致を id を外した名前だけで探すと、0.53.0 で開けた部分文字列が開けなくなる。各節の書かれたままの題と
パスから作った部分文字列 (末尾の 1 文字を落としたもの、ランダムな 6 個、先頭か末尾の語を落としたもの) で比べた:

| 0.53.0 → 0.54.0 | 外した名前だけで探す | 外した名前で探し、何にも当たらないときだけ書かれたままの見出しで探す (採用) |
|---|---|---|
| 開く → 同じ節を開く | 3,481 | 5,479 |
| 開く → 見つからない | 1,998 | 0 |
| 開く → 曖昧 | 4 | 4 |
| 曖昧 → 見つからない | 429 | 0 |
| 曖昧 → 開く | 42 | 42 |

「開く → 曖昧」の 4 は、外した名前では 2 つの節の部分文字列になるもの (Zed の設定のページの `Panel/Default Width` など)。
候補の一覧を出して止まり、別の節は開かない。

**順位**: 4 つの source の 2,044 の対照クエリ (見出しの文字 / id の語 / 1 語 / 書かれたままの見出し (id 付き) / コードスパン)
を `search` と `search-content` に渡し、期待のページが先頭でその最初の `Section:` が期待の節かを比べた。悪化は Zed の 1 件で、
id の語だけのクエリ (`editor ai`、見出しは `Edit Predictions {#editor-ai}`)。id の語が見出しの一致に数えられなくなったためで、
その節は `content` に id を渡せば開く。改善は PixAI の 145 件 (書かれたままの見出しや id の語を渡したクエリが、本文の見出し行に
残った `[#id]` で語が揃うようになった)。Agent Plugins と Zod は変化 0。0.53.0 の上位 3 にあって 0.54.0 の上位 5 に無いページは 5 件で、
Agent Plugins の `Plugin loader [#plugin-loader]` で索引の見出しから id の語が消えたページと、PixAI で期待のページが先頭に来て
本文だけの補いのページが付かなくなったもの。終了コードの変化は 0。

**PixAI の anchor**: URL の付く英語の 37 ページの HTML (2026-11-17 取得) の `id` 属性と照合した:

| 節 | ページの id と合う | 合わない |
|---|---|---|
| 明示の id を持つ節 (`[#id]`) の、その id | 66 | 0 |
| id の無い節 (API リファレンスのページ) の、見出しの文字から作る `github` の anchor | 0 | 76 |

API リファレンスのページの id は `parameters-path` / `request-body` / `response-body` のような形で、見出しの文字からは作れない。
`anchor_style` を付けると 76 節に誤った anchor が付くので、0.54.0 でも付けない。

### 2026-11-17 実測 (0.54.0、マージ前レビューの指摘への修正): 外した名前が別の節を指す見出し

マージ前レビューの指摘で、0.54.0 の最初の版では、id を外した名前が次の 2 つの形で別の節を指していた (同梱 presets の実データ
では 0 件):

- 外した名前で `content` が別の節を開く見出し: `## Default Width {#panel-default-width}` の後に `## Other` の下の id の無い
  `### Default Width` があるページでは、一覧の 1 つ目の名前 `Default Width` が 2 つ目を開く。`## Setup {#a}` の後に id の無い
  `## Setup` があるページでは、`Setup` が注記 (`this reads the first`) と逆に 2 つ目を開き、1 つ目は出力に出るどの名前でも
  開けない
- 外すと同じ名前になる見出し (`## Setup {#a}` / `## Setup {#b}`): `#b` で 2 つ目を開いても前後の subsection hints は 1 つ目の
  子のもので、2 つ目の本文にヒットした `Next:` 行と、2 つ目の子を指す hints の行は 1 つ目の側を開く

直し方は README の「見出しに書かれた id」(「書かれたままの名前で出す見出し」と「外すと同じ名前になる見出し」)。照合の順は
変えていない。

**合成ページの総当たり** (suite のテスト。関数を直接呼ぶ): 9 種の見出し行 (`## Setup` / `## setup` / `## Setup {#a}` /
`## Setup {#b}` / `## Setup {.c}` / `## Other` / `### Setup` / `### Setup {#c}` / `### Child`。各行の下に本文) を 3 つか 4 つ
並べたすべてのページ (7,290) の、すべての節で確かめた:

| 確かめたこと | 崩れたページ (修正前) | 修正後 |
|---|---|---|
| 出力に出る節の名前を `content` に渡すと、その節 (同じ名前の節があるときはその最初) を開く | 3,328 | 0 |
| 0.53.0 の書き方の引数が同じ節を開き、`#<id>` はその id を持つ最初の節を開く | 0 | 0 |
| 節をどの名前で開いても、前後の subsection hints と、`--max-chars` で切ったときの子の判断はその節のもの | 1,668 | 0 |
| `Next:` 行と hints の名前が、パス → `#<id>` → 書かれたままのパスのうちその節を開く最初のもの (同じ名前の最初の節と、どれでも開けない節は今までどおり) | 5,720 | 0 |

`{.c}` だけの見出し (id は無いが属性ブロックは外れる) の節も、書かれたままの題とパスを持つ。このテストは約 2 秒で、
llms-docs の suite は 1,247 テスト 16.5 秒から 1,255 テスト 19.3 秒になった。

**実データ** (上と同じキャッシュ。ネットワークの呼び出しは 0): id を書く 5 つの corpus で、

| 確かめたこと | 値 |
|---|---|
| 出力に出る節の名前が、その節 (同じ名前の節が前にあるときはその最初) を開く | 818 / 818 |
| 0.53.0 で開けた引数が同じ節を開く (同じ節と同じ引数の組は 1 回に数える) | 2,912 / 2,912 |
| id と `#id` が、その id を持つ最初の節を開く | 784 / 784 |
| `content` の subsection hints が、開いた節の子 | 1,608 / 1,608 |
| 最初の版と名前が変わった節 / 書かれたままの名前で出る節 / `Next:` 行と hints がパスでない名前で指す節 | 0 / 0 / 0 |

id を書かない source のキャッシュの全ページ (5,217 ページ、61,326 節) で節は最初の版と同じで、上の 308 コマンドの出力も
0.53.0 と 1 バイトも違わない。

**時間** (`extract_sections`。3 回の呼び出しの平均を 7 回測った最良):

| ページ | 修正前 | 修正後 |
|---|---|---|
| キャッシュで最大のページ (Claude Platform、288,741 行、8 節、id 0。名前を戻す処理は走らない) | 85.0 ms (`sections` の出力まで 85.3 ms) | 85.2 ms (86.5 ms) |
| id を書くページで最大 (Zed、6,465 行、302 節、id 12) | 1.92 ms (2.00 ms) | 2.11 ms (2.16 ms) |
| id を持つかその下にある節が最も多いページ (Agent Plugins、57 節中 56) | 0.33 ms | 0.39 ms |
| 合成 (1,500 節、うち 500 を書かれたままの名前で出す) | 2.35 ms | 3.47 ms |
| 合成 (6,000 節、うち 2,000) | 9.66 ms | 14.26 ms |

増える分は節の数に比例する (名前を戻す処理は、ページの節で引く表を 1 回作り、各節を 1 回ずつ引く)。

### 2026-11-18 実測 (0.55.0): 索引の点数を語・語の部分の先頭の一致に限る

0.55.0 は索引の点数 (`score_entry` のタイトル・description・tags・H1-H2・H3) の照合を、キーワードが語、または語の部分
(`-` / `_` の後、camelCase の段) の先頭から始まる一致に限る。後ろ側は自由 (`auth` → Authentication)。語の文字は ASCII の
英小文字と数字だけで数え、記号で始まるキーワード (`/hooks`) と日本語は今までどおり部分文字列で照合する。本文の検索は部分文字列
のまま。0.54.0 と 0.55.0 を別のプロセスで同じキャッシュ (22 source、取得は 2026-10-03〜11-17) に当てて比べた。比べる間の
ネットワークの呼び出しは 0 で、両版が読んだ索引の項目は同じ。

**`search-index` の対照クエリ** (実行前に期待のページを決めて生成した 6,890 クエリ。期待のページの上位 5 件での順位):

| 型 | クエリ | 良くなった | 悪くなった | 同じ | 1 位 (旧 → 新) | 上位 5 件 (旧 → 新) |
|---|---|---|---|---|---|---|
| タイトルそのまま | 841 | 1 | 0 | 840 | 810 → 810 | 840 → 840 |
| タイトルの 1 語 | 841 | 5 | 0 | 836 | 605 → 606 | 770 → 773 |
| タイトルの 2 語 | 814 | 4 | 0 | 810 | 640 → 643 | 778 → 779 |
| タイトルの 1 語 + 本文にだけある語 | 681 | 9 | 1 | 671 | 362 → 364 | 569 → 573 |
| 1 ページだけの見出しをそのまま | 674 | 6 | 0 | 668 | 599 → 603 | 653 → 655 |
| 識別子 | 576 | 1 | 0 | 575 | 544 → 545 | 576 → 576 |
| 識別子の部分 (`-` / `_` / `.` の後、camelCase の段) | 498 | 23 | 0 | 475 | 160 → 167 | 323 → 330 |
| 長い語の先頭 4〜6 字 | 750 | 9 | 0 | 741 | 384 → 387 | 605 → 611 |
| 大文字の略語 | 533 | 43 | 0 | 490 | 146 → 166 | 308 → 318 |
| 別の語の途中にも現れる語 | 491 | 71 | 4 | 416 | 126 → 148 | 284 → 299 |
| CJK の語の一部 | 40 | 0 | 0 | 40 | 19 → 19 | 38 → 38 |
| 小文字の閉じた複合語の後ろ側 (反例の型。受け入れる損失) | 151 | 0 | 67 | 84 | 29 → 2 | 69 → 5 |
| 計 | 6,890 | 172 | 72 | 6,646 | 4,424 → 4,460 | 5,813 → 5,797 |

- 反例の型は、期待のページのタイトルの閉じた語の後ろ側を打つクエリ (`view` → preview、`forms` → platforms、`down` →
  markdown、`rate` → migrate)。規則では `eli|citation` と区別できないので、悪くなるのは設計どおり
- 反例の型の外の悪化は 5 件で、どれも期待のページが閉じた複合語・接頭辞付きの語の中の一致で 1〜2 点を失い、同点の並びで
  下がったもの (codex の `Work` は H2 の networking、gemini-cli の `search` は H2 の research、cloudflare-ai-gateway の
  `Set` は dataset、claude-code の `out` は description の rollout、ai-sdk の `Cloud include` は H2 の `@ai-sdk/gmicloud`)
- 旧の上位 3 件にいて新の上位 5 件に無いページは 945 件で、すべてキーワードが別の語の途中にあるだけの一致で点を得ていた
  (新で 0 点が 800 件)。多い組は data ⊂ metadata、get ⊂ budgets、text ⊂ context、agent ⊂ subagents、base ⊂ database、
  view ⊂ review。失った一致はタイトル 725・description 626・H1-H2 481・H3 180・tags 10。このうちクエリ自身の期待の
  ページは 46 件で、46 件とも反例の型
- 点が上がった組と、結果が 0 件になったクエリは 0
- 症状の例は手元のキャッシュでも同じ: ai-sdk の `citations` は Generating Text / Amazon Bedrock の 2 件になり
  (旧の 1 位は MCP Elicitation の 12 点)、claude-platform の `hooks` の 1 位は Organization webhooks (beta) から
  Configure Inference hooks に、`text` の上位は Context editing / Context windows から Text editor tool / Create a Text
  Completion に変わり、`Delete file` の上位 5 件から Delete User Profile の 2 件が消える。firebase の `struct` に点が付く
  項目は 7,320 中 3,054 から 112 になる

**`search` の端から端まで** (索引の上位 10 件が変わった 976 クエリから source ごとに 30 まで選んだ 486 クエリ。ページごとに
取得する source (Firebase / Zed / Amp) は、比べる間にページを取得できないので除いた):

| 型 | クエリ | 良くなった | 悪くなった | 同じ |
|---|---|---|---|---|
| 反例の型 (閉じた複合語の後ろ側) | 101 | 0 | 62 | 39 |
| ほかの型 | 385 | 56 | 11 | 318 |

- 反例の型の外の悪化 11 件は、期待のページが上位 5 件に残ったまま 1〜3 つ下がったもの。語の途中の一致で候補 (索引の上位
  5 件) に入っていたページ (workspace の space、subagents の agent、Analog の log、Email の ai など) が外れて空いた枠に
  索引の 6 位以下のページが入り、そのページに本文のヒットがあるので上に並んだ
- 上位 5 行のうち本文のヒットが 0 の索引の行 (`(no body hits — index match only)`) は 137 → 113
- 行が 0 になったクエリ・終了コードが変わったクエリは 0。1 ページだけの見出しをそのまま打ったクエリで、期待のページの
  最初の `Section:` がその見出しで終わるものは 33 → 33

**速度** (`search-index` の 1 クエリの順位付け。索引の大きい vercel / firebase と tags を持つ ai-sdk の対照クエリ
(400 / 320 / 361) を、旧新を交互に 2 回ずつ、各回 3 周流した最良): vercel 44.5 → 48.3 ms (+9%)、firebase 96.5 → 102.1 ms
(+6%)、ai-sdk 12.9 → 13.9 ms (+8%)。語の部分の位置は、キーワードが語の途中に見つかったフィールドでだけ求める。

**採らなかった案** (方針を決めたときの比較): 語全体の一致は前方一致を失う (`auth` → Authentication、`install` → Installation)。camelCase と
`-` / `_` を空白にした別の文字列での語頭一致は、`_` の後から camel の段をまたぐキーワード (`AI_APICallError` の
`apicallerror`、`experimental_listBatches` の `listb`) を引けない。語の途中の一致を残して点だけ下げる案は、Elicitation のように
4 つのフィールドで当たるページが本来の持ち主 (1 点) より上に残る。

**同じ版の `Next:` 行の修正** (`fetch-index` の `sections <page_ref>` / `<slug>` を `sections <opts> -- <page_ref>` に):
22 source のキャッシュと `--file` の 44 行を出力のまま取り、一覧の ref (先頭・末尾・ランダムに 4 つずつ) に差し替えて
`sections` を実行した。開いたページは旧新とも 497 (開けない 8 は claude-code の `llms.txt` が別言語の `llms.txt` を指す
項目で、旧新同じ。ページごとに取得する source でキャッシュに無いページ 23 は、取得しないよう飛ばした)。`-step` / `--x` に
差し替えた 88 行は、旧ではすべて argparse の終了コード 2 で、新では 0 行 (`-1` と出力のままの行は旧新とも 2 にならない)。

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
