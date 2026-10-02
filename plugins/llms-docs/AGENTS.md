# llms-docs — エージェント向けメモ

## Code Review Rules

### 汎用 loader (`scripts/parse-llms-txt.py`)

利用者が `sources.json` に書いたサイトの `llms-full.txt` を、profile の形状どおりに分割して
読む CLI。**対応する形状は README の「任意の llms-full.txt を読む」に列挙した 3 種 (`h1` /
`frontmatter` / `line`) だけ**で、あらゆる `llms.txt` サイトを読めることは目標にしていない。
実測したサイトの profile は `scripts/presets.json` に同梱し、利用者の `sources.json` が同名の
preset を置き換える。

指摘すべきもの:

- 対応する形状のサイトで、ページの分割・タイトル・URL が誤る (実測した Next.js / Vite /
  Vitest / Drizzle / Zod / Hono / Bun / Vercel / Render / MCP / Codex / Ollama / Agent Skills /
  Cloudflare 製品別ファイルと同じ形のもの)。
  本文の水平線やコードブロック内の見本を区切りと誤認する、`drop_lines` がコードブロック内の
  行を消す、ページ URL を読む前に行を消す、など
- 同梱 presets が壊れている (検証で落ちる・取得先が `llms-full.txt` でない)。`sources.json` が
  無いときに presets まで使えなくなる、明示したファイルが無いのに黙って presets だけで動く
- `Next:` ヒントが `--source` / `--sources-file` / `--file` / `--cache-dir` / `--max-age` を
  落とし、別の corpus を指すコマンドになる
- `sources.json` の検証漏れで、source 名がキャッシュディレクトリの外を指せる・不正な値で
  クラッシュする
- 既存の 3 script (`parse-claude-docs.py` / `parse-ai-sdk.py` / `parse-firebase.py`) の出力が
  変わる

指摘しないもの (対象外として README に明記済み):

- `llms.txt` が別の `llms.txt` へのリンク集になっている 2 段 index (Cloudflare のルート `/llms.txt`)
  を読めないこと。製品別の `/<製品>/llms-full.txt` は presets で読む
- presets に入っていないサイト・製品があること (追加は改善提案 P3)
- ページごとに別ファイルで公開するサイト、`llms.txt` の index と本文の join
- 3 種のどれにも当てはまらない形状のサイトを読めないこと。新しい形状への対応は改善提案 (P3)
- 利用者が `sources.json` に書いた URL を取得すること (設定した本人が意図した動作。取得するのは
  profile の `url` 1 つだけ)
- `researching-library-docs` の description に、利用者が `sources.json` に足したサイトが出ないこと
  (description は静的。明示起動で読む)

### 優先度の目安

- P1: 対応形状での誤分割・誤った URL、別 corpus を指すヒント、キャッシュ外への書き込み、
  既存 3 script の出力の変化
- P2: エラーメッセージの誤り、`sources.json` の検証漏れ (クラッシュしないもの)
- P3: 対応形状を広げる提案、可読性
