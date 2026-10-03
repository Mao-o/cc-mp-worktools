# llms-docs

Claude 公式ドキュメント、AI SDK 公式ドキュメント、Firebase 公式ドキュメントを `llms.txt` 経由で段階的に調査するスキル集。
全文読み込みを避け、**キーワード検索 → セクション特定 → コンテンツ取得**の順で必要な部分だけを取得する。

## Skills

| スキル | 対象 | 推奨エントリポイント |
|--------|------|---------------------|
| `researching-claude-docs` | Claude Code / Claude Developer Platform | `search` (URL-join 統合検索) → `content <page_ref> "<heading_path>"` |
| `researching-ai-sdk` | Vercel AI SDK (ai-sdk.dev) | `search` (top N 候補 + 本文 hits) → `content <page_ref> "<heading_path>"` |
| `researching-firebase` | Firebase (firebase.google.com) | `search` (top N on-demand fetch + 本文 hits) → `content <page_ref> "<heading_path>"` |
| `researching-library-docs` | 同梱 presets のサイト (Next.js / Vite / Vitest / Drizzle / Zod / Hono / Bun / Vercel / Render / MCP / Codex / Ollama / Agent Skills / Agent Plugins / OpenAI Plugins・API docs / ACP / Cline / Factory / Devin / Cloudflare 製品別) と利用者の `sources.json` のサイト | `search --source <name>` → `content <page_ref> "<heading_path>" --source <name>` |

3 script で `search` / `search-index` / `search-content` / `sections` / `content` / `fetch-index` の
サブコマンド名・引数・`<page_ref>` 形式が統一されている (0.7.0)。

### 命名メモ: `researching-claude-docs` が `claude` を含むのは意図的

skill 名に `claude` / `anthropic` を含めないという lint 規約を持つ環境では、
`researching-claude-docs` が指摘対象になる。これは**意図的な命名で変更しない**:

- この skill の対象そのものが Claude Code / Claude Developer Platform の公式ドキュメントで、
  名前から対象を外すと description ベースの auto-invoke の発見性が落ちる
- plugin skill の frontmatter `name` は起動コマンドの末尾セグメントになるため
  (`/llms-docs:researching-claude-docs`)、rename は利用者の呼び出しと外部 rule からの参照を
  壊す破壊的変更になる
- **公式仕様に skill 名の予約語制約は無い。** 公式ドキュメントが挙げる予約名はスキル
  フォルダ名 `synced` の 1 件のみで、`claude` / `anthropic` は対象外。
  `claude plugin validate plugins/llms-docs` は warning ゼロで通る (CLI 2.1.276 実測)

## Skill を呼べない文脈 (subagent など) から使う

Skill ツールが使えない文脈 (general-purpose の subagent など) でも、同梱のスクリプトを直接実行すれば
同じ調査ができる。公式ドキュメントを WebFetch する前に、こちらを使う (WebFetch は要約モデル経由で
field が抜ける)。

1. スクリプトのパスを決める: `${CLAUDE_PLUGIN_ROOT}/scripts/<script>`。`${CLAUDE_PLUGIN_ROOT}` が空の
   環境では、plugin の展開先 (`~/.claude/plugins/` の下) を探す。
   ディレクトリを指定して追加した marketplace は展開先へコピーされず、その場で読まれるので、見つからなければ
   `claude plugin marketplace list` で `Source: Folder (<dir>)` と出る marketplace の `<dir>` の下の
   `plugins/llms-docs/scripts/` を探す
2. `python3 <path> search "<キーワード>"` を実行し、出力末尾の `Next:` の先頭の script 名を
   `python3 <path>` に置き換えて、本文 (`content`) を取る

| 調べる対象 | script | 備考 |
|---|---|---|
| Claude Code / Claude Developer Platform | `parse-claude-docs.py` | `--source platform` / `--source both` |
| AI SDK | `parse-ai-sdk.py` | |
| Firebase | `parse-firebase.py` | |
| 同梱 presets・利用者の `sources.json` のサイト | `parse-llms-txt.py` | 全コマンドに `--source <name>` が必須 |

調査の手数の目安 (1 論点は `search` 1 回 → `content` 1〜2 回、3 回外したら言い換えずに構造から当たる、
`--max-chars 0` は上限で切れたと確かめてから) は各 skill の「調査の進め方」にある。

### エラー・0 件のあとの出力

4 script とも、失敗や 0 件のあとに「次に打つコマンド」をそのまま出す (エラーと 0 件の exit code は
変わらない。claude-docs で下の規則により 1 ページに解決した slug は、曖昧エラーではなく本文を返す):

- **曖昧な page_ref**: 候補ごとに実行できるコマンドを付ける。claude-docs は slug が `<lang>/<slug>`
  に完全一致するページが 1 件だけならそれに解決し、他の候補を stderr の `Note:` で、それを読むコマンド
  付きで知らせる (`hooks` は `en/hooks`。`en/agent-sdk/hooks` は別のページ)。候補のコマンドは
  `--max-chars` / `--limit` などの既定以外の値も引き継ぐ
- **heading が見つからない**: 全見出しの前に `Closest sections:` として近い見出し (最大 5 件) を、
  実行できるコマンド付きで出す
- **`search` / `search-content` の `Next:`**: 上位ヒットの `doc_idx` と heading_path を埋めたコマンドを
  最大 3 行出す (`--source` などは引き継ぐ)。ヒットが無いときはプレースホルダを出さない
- **同じ heading_path が 1 ページに 2 回以上ある見出し**: コマンドは 1 つ目を読む (2 つ目以降を指す手段は
  無い)。そうした見出しのコマンドには `# heading appears N times; this reads the first` (shell のコメントなので、行ごと打てる) が付く
- **0 件**: `Why nothing matched:` に語ごとのページ数を出し、「語が corpus に無い」「語は有るが同じ
  セクションに揃わない」「`--page-ref` で外した」を区別して、語を減らす / `search-index` / 別 source の
  コマンドを示す

## Components

| 種類 | パス |
|------|------|
| Skill | `skills/researching-claude-docs/SKILL.md` |
| Skill | `skills/researching-ai-sdk/SKILL.md` |
| Skill | `skills/researching-firebase/SKILL.md` |
| Skill | `skills/researching-library-docs/SKILL.md` (`parse-llms-txt.py` を使う) |
| Script | `scripts/parse-claude-docs.py` |
| Script | `scripts/parse-ai-sdk.py` |
| Script | `scripts/parse-firebase.py` |
| Script | `scripts/parse-llms-txt.py` (任意サイトの `llms-full.txt`。skill は `researching-library-docs`) |
| Data | `scripts/presets.json` (`parse-llms-txt.py` の同梱 profile) |
| Dev tool | `scripts/check-preset-urls.py` (presets の `url` / `index_url` を HEAD で点検する。ネットワークに出るので suite には入れない) |
| Shared | `scripts/_common.py` (FenceTracker / extract_sections / fetch_url ほか共通ヘルパー) |
| Shared | `scripts/_commands.py` (`sections` / `content` / 検索結果・index 行の出力テンプレートと、検索後の `Next:` 行・0 件の診断。各 script は page を `PageView` に詰めて渡す) |
| Docs | `docs/paths-and-fork-context.md` (`paths` 自動ロードと `context: fork` の実測) |
| Docs | `docs/generic-llms-txt-source.md` (任意の `llms.txt` サイト対応のコスト見積り) |

## 前提条件

- `python3` (**3.11+ 必須**) — `parse-*.py` は PEP 604 記法 (`list[X]` / `X | None`) を定義時に評価するため、3.11 未満 (例: macOS 標準の `/usr/bin/python3` = 3.9 系) では起動直後に `TypeError: unsupported operand type(s) for ...` で失敗する。marketplace 横断方針により 3.9 互換 shim は追加しない — `mise use python@3.11` 等で新しい `python3` を用意すること。`scripts/_common.py` の `from __future__ import annotations` は既存のまま (3.9 互換化が目的ではない)
- ネットワーク到達性（初回取得時に外部 llms.txt をダウンロード）
- キャッシュ保存先 (既定 `~/.cache/llms-docs`、`$XDG_CACHE_HOME` / `$LLMS_DOCS_CACHE_DIR` で上書き可) への書込み権限

## 動作確認

リポジトリルートから:
```bash
claude --plugin-dir ./plugins/llms-docs
```

スクリプト単体テスト:
```bash
# python3 <script> の形式で呼び出す（直接実行 ./script.py は非対応）

# search (推奨入口: 候補絞り込み + 本文 hits を 1 コマンド)
python3 plugins/llms-docs/scripts/parse-claude-docs.py search "hook matcher"
python3 plugins/llms-docs/scripts/parse-ai-sdk.py search "streamText onFinish"
python3 plugins/llms-docs/scripts/parse-firebase.py search "Firestore query limit"

# search-index (候補だけ取得したいとき; 軽量 llms.txt ベース)
python3 plugins/llms-docs/scripts/parse-claude-docs.py search-index "hook matcher"
python3 plugins/llms-docs/scripts/parse-ai-sdk.py search-index "streamText onFinish"
python3 plugins/llms-docs/scripts/parse-firebase.py search-index "Firestore query limit"

# search-content (特定ページ内だけ本文検索)
python3 plugins/llms-docs/scripts/parse-claude-docs.py search-content "matcher PreToolUse" --page-ref hooks
python3 plugins/llms-docs/scripts/parse-ai-sdk.py search-content "useChat onFinish" --page-ref 153
python3 plugins/llms-docs/scripts/parse-firebase.py search-content "orderBy limit" --page-ref 2972

# fetch-index (フォールバック)
python3 plugins/llms-docs/scripts/parse-claude-docs.py fetch-index
python3 plugins/llms-docs/scripts/parse-firebase.py fetch-index --limit 10
```

## 回帰テストスイート

```bash
cd plugins/llms-docs
python3 -m unittest discover scripts/tests
```

標準ライブラリのみで動く (pytest / pip install 不要)。

クラス単位・メソッド単位で 1 件だけ指定して実行することもできる:

```bash
python3 -m unittest scripts.tests.test_common.ParseLlmsIndexTest -v
python3 -m unittest scripts.tests.test_common.ParseLlmsIndexTest.test_colon_description_form -v
# pytest がインストールされていれば node id 指定も可能
pytest scripts/tests/test_common.py::ParseLlmsIndexTest::test_colon_description_form -q
```

## キャッシュ

既定のキャッシュディレクトリは `~/.cache/llms-docs` (`$XDG_CACHE_HOME/llms-docs` があればそちら、`$LLMS_DOCS_CACHE_DIR` で完全上書き可)。`--cache-dir` で個別指定も可能。

| スキル | キャッシュファイル (`<cache-dir>` 配下) |
|--------|-------------------|
| claude-docs (Code) | `claude-code-llms.txt`, `claude-code-llms-full.txt` |
| claude-docs (Platform) | `claude-platform-llms.txt`, `claude-platform-llms-full.txt` |
| ai-sdk | `ai-sdk-llms-full.txt` |
| firebase | `firebase-llms.txt` (index), `firebase-docs/` (per-page) |
| 任意サイト (`parse-llms-txt.py`、presets 含む) | `generic-<source 名>-<url のハッシュ 12 桁>-llms-full.txt` (profile の URL を変えると別ファイルになる) |

最新版が必要な場合は `--max-age 0` で強制再取得する（`rm` でも良いが、`fetch_url` は取得失敗時に既存キャッシュを stale なまま使い続けるフォールバックを持つため、`--max-age 0` の方が「取得できなければ既存キャッシュのまま」という安全側の挙動になる）。

## 任意の llms-full.txt を読む

`scripts/parse-llms-txt.py` は、profile に従って任意のサイトの `llms-full.txt` を読む
(0.25.0)。サブコマンド (`search` / `search-index` / `search-content` / `sections` / `content` /
`fetch-index`) と `<page_ref>` の形は 3 つの専用 script と同じで、加えて使える profile を
一覧する `sources` がある。skill `researching-library-docs` (0.27.0) がこの script を使い、
description に同梱 presets のサイト名を並べて auto-invoke の対象にしている。presets に無い
サイトを `sources.json` に足した場合は、skill を `/llms-docs:researching-library-docs <source> <質問>`
で明示的に起動するか、CLI を直接呼ぶ。

```bash
python3 plugins/llms-docs/scripts/parse-llms-txt.py sources
python3 plugins/llms-docs/scripts/parse-llms-txt.py search "<query>" --source <name>
python3 plugins/llms-docs/scripts/parse-llms-txt.py content <page_ref> "<heading_path>" --source <name>
```

### 同梱 presets

次のサイトは `scripts/presets.json` に profile が同梱されており、設定なしで `--source <name>` で読める。
各サイトのページ数と確かめ方は `docs/generic-llms-txt-source.md` の「2026-10-03 実測」にある。

| source 名 | サイト | ページ URL |
|---|---|---|
| `nextjs` | Next.js (`/docs/llms-full.txt`) | あり |
| `vite` / `vitest` | Vite / Vitest | あり (`.md`) |
| `drizzle` | Drizzle ORM | あり |
| `zod` | Zod | 一部 (`llms.txt` とタイトルで突き合わせ。17 中 14) |
| `hono` | Hono | 一部 (同上。87 中 21) |
| `bun` | Bun | あり |
| `vercel` | Vercel | あり |
| `render` | Render | 一部 (同上。125 中 121) |
| `mcp` | Model Context Protocol | あり |
| `codex` | OpenAI Codex + ChatGPT docs (`learn.chatgpt.com/docs/llms-full.txt`。Codex の CLI / IDE / cloud / SDK に加え、ChatGPT の desktop app / Work / 管理のページを含む) | 一部 (同上。178 中 175) |
| `ollama` | Ollama | あり |
| `agentskills` | Agent Skills (`SKILL.md` の仕様) | あり |
| `agent-plugins` | Agent Plugins (Skills と MCP server を束ねる plugin の共通形式。`agent-plugins.org/llms.txt` が全文を含む) | なし (13 ページ) |
| `openai-plugins` | OpenAI Plugins (ChatGPT / Codex の plugin: MCP server・UI・skills・提出。`developers.openai.com/plugins`) | 一部 (`llms.txt` とタイトルで突き合わせ。31 中 30) |
| `openai-api-docs` | OpenAI API docs (`developers.openai.com/api/docs`) | 一部 (同上。232 中 220) |
| `acp` | Agent Client Protocol (ACP) | あり |
| `cline` | Cline | あり |
| `factory` | Factory (Droid) | 一部 (同上。104 中 104) |
| `devin` | Devin | あり |
| `cloudflare-<製品>` | Cloudflare の製品別 `/<製品>/llms-full.txt`。製品は `workers` / `d1` / `r2` / `kv` / `durable-objects` / `pages` / `queues` / `workers-ai` / `vectorize` / `hyperdrive` / `agents` / `workflows` / `ai-gateway` / `browser-rendering` / `containers` | あり (`index.md`)。`browser-rendering` は製品名が Browser Run に変わり、取得先は `/browser-run/llms-full.txt` (52 ページ中 51 でページ内に URL。URL の無い 1 件は他の製品と同じ API リファレンスのページ)。source 名は変えていない |

**取得先のホスト** (`scripts/presets.json` の `url` / `index_url` から出した一覧。ネットワークの出口を決める根拠にする。
1 行に 1 ホスト、次のコードブロックはテストが読む):

```text
agent-plugins.org
agentclientprotocol.com
agentskills.io
bun.com
developers.cloudflare.com
developers.openai.com
docs.cline.bot
docs.devin.ai
docs.factory.com
docs.ollama.com
hono.dev
learn.chatgpt.com
modelcontextprotocol.io
nextjs.org
orm.drizzle.team
render.com
vercel.com
vite.dev
vitest.dev
zod.dev
```

preset の追加・移転のたびにこの一覧を presets.json に合わせる (`presets.json` との集合の一致は suite が確かめる。
ルートの README の Privacy 表はこの一覧を指しており、ホストを列挙していない)。
専用の 3 skill の取得先 (`code.claude.com` / `platform.claude.com` / `ai-sdk.dev` / `firebase.google.com`) は
この一覧に含まない。

他の製品や他のサイトは、下の `sources.json` に profile を書けば読める。

### 自分の profile を書く (`sources.json`)

`sources.json` は同梱 presets に重ねて読み、**同名の preset は置き換える**。ファイルが無ければ
presets だけを使う。場所は `--sources-file` > `$LLMS_DOCS_SOURCES_FILE` >
`$XDG_CONFIG_HOME/llms-docs/sources.json` > `~/.config/llms-docs/sources.json`
(キャッシュを消しても profile が消えないよう、キャッシュとは別の設定ディレクトリに置く)。
`--sources-file` か `$LLMS_DOCS_SOURCES_FILE` で明示したファイルが無いときはエラーにする。

```json
{
  "sources": {
    "example-fm": {
      "url": "https://docs.example.com/llms-full.txt",
      "split": "frontmatter",
      "frontmatter_key": "url",
      "page_url": "frontmatter:url",
      "url_base": "https://docs.example.com",
      "description": "任意。sources の一覧に出る"
    },
    "example-line": {"url": "https://example.org/llms-full.txt", "split": "line", "line_prefix": "Source: "},
    "example-h1": {"url": "https://example.net/llms-full.txt", "split": "h1", "skip_empty": true}
  }
}
```

| キー | 必須 | 意味 |
|---|---|---|
| `url` | ○ | サイトの `llms-full.txt` (http / https)。取得するのはこれと `index_url` だけ |
| `split` | ○ | ページの区切り方。`h1` = コードブロック外の H1 ごと / `frontmatter` = YAML frontmatter ごと / `line` = `line_prefix` で始まる行ごと |
| `frontmatter_key` | | `split: frontmatter` のとき、ページの frontmatter に必ずあるキー (既定 `title`)。これが無い `---` の組は区切りとみなさない (本文の水平線を誤認しないため) |
| `frontmatter_delimiter` | | `split: frontmatter` のとき、frontmatter を囲む行 (既定 `---`。`-` か `+` を 3 個以上)。Vercel は `-` 80 個 |
| `line_prefix` | `split: line` で必須 | 区切り行の接頭辞。接頭辞のあとに URL が 1 つだけ続く行を区切りにする |
| `page_url` | | ページ URL の取り方。`none` (既定) / `frontmatter:<key>` / `line:<接頭辞>` (本文の先頭 10 行から探す) / `link:<リンク文字列>` (本文の先頭 20 行にある Markdown リンク `[<リンク文字列>](<url>)` の URL)。`split: line` では区切り行の URL を使う |
| `url_base` | | ページ URL が相対 (`/guide.md`) のときに前に付ける基点 |
| `drop_lines` | | 本文から除く行の正規表現のリスト (行頭から照合したいときは `^` を付ける)。全ページに付く定型行 (「Skip to content」など) が検索に当たらないようにする。コードブロック内の行は除かない。ページ URL は除く前に読む |
| `skip_empty` | | `true` で、本文が空 (空行と水平線だけ) のページを捨てる (Hono の `# Start of Hono documentation`、Codex のカテゴリ見出しのような見出しだけの行) |
| `index_url` | | `url` の `llms-full.txt` と同じ範囲の `llms.txt` (他の `llms.txt` へのリンク集になっている 2 段の索引は指さない。下の「対象外」)。URL を持たないページに、タイトルが完全に一致する (大文字小文字・空白・`*_` の記号は無視) 項目の URL を付ける。同じタイトルの項目が 2 つ以上あるページ、同じタイトルのページが 2 つ以上あるとき (Zod はサイト冒頭と `packages/zod` のページがどちらも `Zod`)、近いだけのタイトル (`Basic Auth` と `Basic Auth Middleware`) には付けない (誤った URL は URL が無いより悪いため)。`llms.txt` は絶対 URL の項目だけを読み、`llms.txt` や `llms-<名前>.txt` (`llms-full.txt` / `llms-small.txt` など) を指す項目はページではないので除く。取得に失敗しても警告だけで本文は読める |
| `h1_needs_url` | | `split: h1` で `true` のとき、`page_url` (`line:` / `link:`) の URL が見つからない H1 をページの区切りにせず、前のページの見出しとして残す (ページ本文の中で H1 を使うサイト向け) |

source 名は `^[a-z0-9][a-z0-9-]*$` (キャッシュのファイル名になるため)。未知のキーや不正な値は
エラーにする。profile を試すときは `--file <手元の llms-full.txt>` で取得せずに読める (`--file` のときは
`index_url` も取得しない。手元の `llms.txt` を `--index-file` で渡せば突き合わせる)。

実測した形状 (詳細は `docs/generic-llms-txt-source.md`):

| 形状 | profile | 例 |
|---|---|---|
| frontmatter に `title:` と絶対 URL | `split: frontmatter` + `page_url: frontmatter:url` | Next.js (`/docs/llms-full.txt`) |
| frontmatter に相対 `url:` のみ | 上に加えて `frontmatter_key: url` + `url_base` | Vite / Vitest |
| frontmatter に `title:`、URL は本文のリンク、定型行つき | `split: frontmatter` + `page_url: "link:View as Markdown"` + `drop_lines` | Cloudflare の製品別ファイル |
| `Source: <url>` 行で区切る | `split: line` + `line_prefix: "Source: "` | Drizzle ORM |
| H1 で区切り、直後に `Source: <url>` 行 | `split: h1` + `page_url: "line:Source: "` (本文にも H1 があれば `h1_needs_url`) | Agent Skills / Bun / MCP / Ollama / ACP / Cline / Devin |
| frontmatter を長い横線で囲む | `split: frontmatter` + `frontmatter_delimiter` | Vercel |
| H1 で区切り URL なし | `split: h1` (見出しだけのページがあれば `skip_empty`) | Zod / Hono / Render / Codex / OpenAI Plugins・API docs / Factory |
| frontmatter (`title:` / `description:`) で区切り URL なし | `split: frontmatter` (既定の `frontmatter_key: title`) | Agent Plugins |

対象外: `llms.txt` が別の `llms.txt` へのリンク集になっている 2 段 index (Cloudflare のルート
`/llms.txt`。製品別の `llms-full.txt` は上のとおり読める)、ページごとに別ファイルで公開する
サイト、`llms.txt` の index と本文のタイトル以外での突き合わせ (`index_url` は完全一致のタイトルだけ)。URL を持たないページでは `URL:` 行と `# source:` 行を
出さない。

2 段 index は `index_url` にも書かない。別の `llms.txt` を指す項目は突き合わせから除くので、そのファイルの URL が
ページに付くことはないが、`url` の `llms-full.txt` に含まれない別の docs のページ (学習トラックなど) の項目が、ページと
タイトルを共有すると、その項目の URL が付く (OpenAI のルートの `llms.txt` を書いたとき、API ガイドの
`Model optimization` に学習トラックのページの URL が付いた)。タイトルだけでは区別できないため防げない。`index_url` には、
`url` の `llms-full.txt` と同じ範囲の `llms.txt` (製品別の `/<製品>/llms.txt` など) を書く。

## 既知の制約

- 全文読み込み禁止: 必ず段階的に絞り込むこと
- コードフェンス保護: コードブロックの途中分割を自動防止
- テーブル保護 (claude-docs / firebase): Markdown テーブルの途中分割を自動防止

## 設計判断: subagent fork + Sonnet は維持する

3 SKILL とも `context: fork` + `model: sonnet` で起動する。これは spawn
オーバーヘッドと引き換えに以下を保証する設計判断:

- **context rot 防止**: llms.txt / llms-full.txt は数 MB あり、親 context に流すと
  数回の調査でメインセッションが肥大化 → 後続作業の精度が劣化する (context rot)。
  fork で隔離することで親には「調査結果の要約」だけが返る
- **正確性の優先**: 親 (例: Opus 4.7) の作業と SKILL の調査を分離し、要約モデルが
  混在しない決定論的な出力を得る (`researching-claude-docs` description で
  「verbatim 取得・幻覚回避」と明示している通り)
- **コスト分離**: Sonnet で動かすことで Opus 親セッションのトークン消費に乗らない

**軽量化方向 (fork 外し / 親 model 継承 / 軽量モード追加) は採用しない。**
spawn オーバーヘッドは受け入れ、低品質回答や context rot を避ける方を優先する。

`paths` による自動ロードも親 context を太らせない。`paths` がマッチしても SKILL.md 本文は
親に注入されず (実測値と手順は [docs/paths-and-fork-context.md](docs/paths-and-fork-context.md))、
`paths` を絞る最適化は不要と判定済み。

「軽い質問でも WebFetch に流れる」課題への対応策は次の 3 系統で、いずれも
fork + Sonnet 構成を崩さない:

1. **description の充実** (`Use proactively` / `Triggers:` を 3 SKILL で揃える、0.6.0)
2. **`~/.claude/rules/` の `*-doc-first.md` rule** で「自身の知識より先に
   `researching-*` を呼ぶ」を明示 (ai-sdk / claude-docs / firebase)
3. **`search` 統合サブコマンドの提供** (0.7.0) で 1 コマンド完結の調査体験

WebFetch との比較メモ: WebFetch は要約モデル経由のため field 抜け落ち・幻覚の
リスクがあり、Anthropic API / Firebase / AI SDK のような schema を厳密に扱う
ドメインでは fork + Sonnet + 決定論的 grep の方が信頼できる。spawn 数秒 vs
要約幻覚で半日 debug を比較すれば、前者の方が安い。

## 保守メモ

`parse-claude-docs.py`、`parse-ai-sdk.py`、`parse-firebase.py` はドキュメント分割方式が
根本的に異なるため独立したスクリプトとして管理している。特に `parse-firebase.py` は
Firebase 側に `llms-full.txt` が存在しないため、index + per-page on-demand fetch 方式を
採用している。そのため Firebase の `search` は top N 件 (default 5) を順次 fetch する
ヒューリスティクスを持つ。`search-content` の `--page-ref` は単数指定 (省略時は全ページ
横断、ただし重いので明示指定推奨)。バグ修正や機能改善を行う際は 3 本すべてを確認すること。

3 script で API を 0.7.0 で揃えた: `search` / `search-index` / `search-content` /
`sections` / `content` / `fetch-index` の 6 サブコマンドが共通、`<page_ref>` は
int / URL slug / 完全 URL を受け付ける (ai-sdk のみ URL がないため int / title 部分一致)、
`--file` flag は省略時に cache を auto-fetch する。

共通ロジック (code-fence scanner / section & content extraction / llms.txt index parser /
HTTP fetch / エラーヘルパー / metadata header / Next hint / argparse skeleton /
**keyword search (search_index_entries, search_content_in_body, score_entry)**) は
`scripts/_common.py` に集約済み。サブコマンドの**出力テンプレート** (`sections` /
`content` の本体、`search` / `search-content` のページ block、`fetch-index` /
`search-index` の行) は `scripts/_commands.py` に集約済み。新しい doc source を
追加する際は、source 固有の `split_documents` と、page を `PageView` に詰める
小さな adapter だけを書き、共通部分は `_common` / `_commands` から import すること。
`Next:` ヒントの corpus 引数 (`corpus_hint_args(args)`) は呼び出し側で組み立てて
`hint_args=` で渡す (`_commands` 側では組み立てない。`tests/test_hint_wiring.py` が検査)。

**presets の取得先の点検**: サイトが `llms-full.txt` / `llms.txt` を移す (製品名の変更、docs の
ホスト移転) と、ローダーは転送に追従するため読めたまま気付かない。`scripts/check-preset-urls.py` を手で
流すと、同梱 presets の全 URL を HEAD で点検し、3xx は転送先まで辿って、200 以外を一覧する
(`python3 plugins/llms-docs/scripts/check-preset-urls.py`。200 以外があれば exit 1)。一覧に出た preset は、
転送先と内容を比べてから直す: 同じ内容なら URL を移す。内容が違うとき (旧い版が残っているだけ、など) は
ページ数・ページ内の URL・`index_url` の突き合わせを測って、新しい側へ移すかを決める。直したら
description と、`researching-library-docs` の対応表・この README の presets 表を合わせる。
ネットワークに出る点検なので CI の suite には入れず、サーベイのときに流す。

`search-content` はセクション単位の AND 検索が既定 — 指定した全キーワードが同じセクション内に
揃って出現するセクションのみを返す。単純な OR 挙動（どれか 1 つでもマッチすれば hit）ではない。
ただし完全 AND が 1 件も無い場合（複数キーワード時のみ）、キーワードの半分以上が揃うセクションへ
自動でフォールバックし、出力に `[partial match]` と一致キーワードを明示する（soft-AND）。
