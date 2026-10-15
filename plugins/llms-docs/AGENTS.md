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
  Agent Plugins / OpenAI Plugins・API docs / ACP / Cline / Factory / Devin /
  Cloudflare 製品別ファイルと同じ形のもの)。
  本文の水平線やコードブロック内の見本を区切りと誤認する、`drop_lines` がコードブロック内の
  行を消す、ページ URL を読む前に行を消す、など
- `index_url` の突き合わせで、別のページの URL が付く (完全一致・一意以外で付けている、別の
  `llms.txt` / `llms-full.txt` などのファイルを指す項目の URL を付けている、など)
- 同梱 presets が壊れている (検証で落ちる・取得先が `llms-full.txt` でない。ただし `body: "fetch"`
  の preset は `llms-full.txt` を持たないので `url` が無く、取得先は `index_url` の `llms.txt` と、
  そこに並ぶ各ページ)。`sources.json` が
  無いときに presets まで使えなくなる、明示したファイルが無いのに黙って presets だけで動く
- `body: "fetch"` の source で、取得するページが増える (`fetch-index` / `search-index` がページを
  取る、`search` が候補の外のページを取る・全文 fallback をする、`search-content` を並行にして
  最初の結果が全件の取得を待つ)。ページの cache で、別の URL のページの本文を返す
- `Next:` ヒントが `--source` / `--sources-file` / `--file` / `--cache-dir` / `--max-age` を
  落とし、別の corpus を指すコマンドになる
- `sources.json` の検証漏れで、source 名がキャッシュディレクトリの外を指せる・不正な値で
  クラッシュする
- 0.45.0 までの専用 script を置き換えた preset (`claude-code` / `claude-platform` / `claude` / `ai-sdk` /
  `firebase`) の出力が、専用 script の実装 (`scripts/tests/legacy/`) の出力から変わる。ただし移行の比較
  (`scripts/tests/test_migration_parity.py` の `normalize`) が除く行 (1 行目のヘッダの括弧と直後の
  `(file: …)` などの行、`Next:` の script 名と既定の corpus・ファイルを指すオプション、末尾の件数行、
  行頭の `Tip:` / `Note:`、単位の語 `page` / `document`、`URL:` / `url:` の大文字小文字) と、
  `GENERIC_INTENDED_DIFF` に理由を書いた意図した差は除く
- 旧 script 名の shim (`scripts/parse-claude-docs.py` / `parse-ai-sdk.py` / `parse-firebase.py`、実体は
  `scripts/_shim.py`) が、引数の読み替え (`--source` の値と既定、`--index-limit`、省略形の展開) 以外の
  ことをする、読み替えで別の source・別のオプションになる、`parse-llms-txt.py` 以外の出力を出す

指摘しないもの (対象外として README に明記済み):

- `llms.txt` が別の `llms.txt` へのリンク集になっている 2 段 index (Cloudflare のルート `/llms.txt`)
  を読めないこと。製品別の `/<製品>/llms-full.txt` は presets で読む
- `index_url` に 2 段 index を書いたとき、`url` の `llms-full.txt` に含まれない別の docs のページ
  (学習トラックなど) の項目とタイトルが衝突して、その項目の URL が付くこと (タイトルだけでは区別
  できない。README で 2 段 index を `index_url` に書かないよう案内済み)
- presets に入っていないサイト・製品があること (追加は改善提案 P3)
- ページごとに別ファイルで公開し、`llms.txt` に全ページの絶対 URL が並ばないサイト (並ぶサイトは
  `body: "fetch"` で読む)
- `index_url` で URL が付かないページがあること (タイトルが完全一致しない・`llms.txt` の項目かページのどちらかでタイトルが重複する。付けないのは意図した動作)
- 3 種のどれにも当てはまらない形状のサイトを読めないこと。新しい形状への対応は改善提案 (P3)
- `scripts/check-preset-urls.py` が suite に入っていないこと (ネットワークに出る手動の点検道具。
  取得先の転送を見つけるために手で流す)
- 利用者が `sources.json` に書いた URL を取得すること (設定した本人が意図した動作。取得するのは
  profile の `url` と、あれば `index_url` だけ)
- `researching-library-docs` の description に、利用者が `sources.json` に足したサイトが出ないこと
  (description は静的。明示起動で読む)

### 優先度の目安

- P1: 対応形状での誤分割・誤った URL、別 corpus を指すヒント、キャッシュ外への書き込み、
  専用 script を置き換えた preset の出力の変化 (上の除外を除く)、shim の読み替えの誤り
- P2: エラーメッセージの誤り、`sources.json` の検証漏れ (クラッシュしないもの)
- P3: 対応形状を広げる提案、可読性
