# 任意の `llms.txt` サイトを source 登録できるようにする場合のコスト見積り

計測: plugin 0.24.0 / 2026-09-19。着手判断のための材料として書いた。

> **2026-09-26 追記 (0.25.0)**: Phase 0 (コマンド層の共通化、0.24.3) に続き、Phase 1 として
> 形状 profile を外部化した汎用 loader `scripts/parse-llms-txt.py` を追加した (使い方は
> README の「任意の llms-full.txt を読む」)。2 節の表のうち未計測だった Next.js / Vite /
> Vitest を実測し、Drizzle の区切り方の誤りを直した (下の「2026-09-26 実測」)。Phase 2
> (汎用 skill) は未着手。

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

Drizzle は MDX を多用し、コードブロックの記号行が約 13,600 行あって開閉が JSX の中で崩れて
いる。コードブロックを追跡すると区切り行の大半を見失う (197 / 496) ため、`line` 区切りでは
追跡せず「接頭辞のあとに URL が 1 つだけ続く行」を区切りにした。

いちばん効くのは **URL を持たない corpus (Zod) が実在する**こと。`_common.check_join_rate` は
index↔full の join 率が低いと失敗として扱うため、URL 無しの corpus は profile 追加だけでは
通らず「join しないモード」が必要になる。`[URL#anchor]` 出力や `--page-ref <slug>` も
同時に成立しなくなる。

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
