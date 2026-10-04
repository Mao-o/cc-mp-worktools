---
name: researching-claude-docs
description: |
  Fetch verbatim sections from Claude Code (code.claude.com) and Claude
  Developer Platform (platform.claude.com) 公式 docs — llms.txt /
  llms-full.txt を段階的に grep し、WebFetch の要約モデル経由のような
  field 欠落・幻覚を回避する。
when_to_use: |
  Use when implementing, debugging, configuring, reviewing, or **designing**
  Claude Code features (hooks, subagents, plugin manifest, slash commands,
  MCP servers, settings.json, permissions, Skills/AgentSkill, output styles)
  or Anthropic API specs. Use proactively before answering spec questions
  about the above — especially before editing SKILL.md / agent / hook files.
  Triggers: "Claude Code", "AgentSkill", "Skill", "hook schema", "subagent",
  "plugin manifest", "slash command", "settings.json", "permission", "MCP",
  "Anthropic API", "researching-claude-docs",
  "disable-model-invocation", "user-invocable", "argument-hint",
  "skill frontmatter effort", "slash command arguments", "context: fork",
  "skill frontmatter paths", "SubagentStop", "$ARGUMENTS",
  "$CLAUDE_SKILL_DIR", "output style"
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
  - "**/.claude/skills/**"
  - "**/.claude/settings*.json"
  - "**/.mcp.json"
  - "**/hooks.json"
metadata:
  author: mao
  version: "3.5.0"
---

# Claude ドキュメント Progressive Loader

Claude Code (`code.claude.com`) および Claude Developer Platform (`platform.claude.com`) の公式ドキュメントを段階的に読み込むスキル。

## Quick Start

```bash
# 1. キーワードで候補ページと本文ヒットを 1 コマンドで取得
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<キーワード>"
# 2. 返ってきた [doc_idx] と heading_path を使って本文取得
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <doc_idx> "<heading_path>"
```

迷ったら `search` から始める。両 source 横断は `--source both`、Anthropic API は `--source platform`。詳細は下記「推奨フロー」以降。
`search` / `search-content` の末尾の `Next:` は上位ヒットの `doc_idx` と heading_path が埋まっている (そのまま実行できる)。

## 調査の進め方 (手数の目安)

- **論点が複数あるとき**は、最初に論点を番号付きで列挙し、1 つずつ順に処理して、論点ごとに結論
  (または「ドキュメントに記載なし」) を返す。複数の論点を 1 回の `search` に詰めない
- 1 論点の基本は **`search` 1 回 → `content` 1〜2 回**。`search` の末尾の `Next:` 行は `doc_idx` と見出しが
  埋まったコマンドなので、見出しを手で写さずそのまま実行する (先頭の `parse-claude-docs.py` は
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py"` に置き換える)
- 同じ論点で `search` を **3 回外したら**、言い換えを続けない。`search-index` / `sections` で構造から当たるか、
  「ドキュメントに記載なし」として返す
- **`--max-chars 0`** は、出力に `... (N chars truncated; narrow with ...)` が出て、既定の上限 (24000 字) で
  切れたと確かめてからだけ使う。`| head` / `| grep` で出力を切らず、`--max-chars` と `sections` で絞る
  (パイプで切ると末尾の `Next:` が見えなくなる)
- この Skill の実行中 (fork の中) では、同じ Skill をもう呼ばない (`already executing in this forked
  context` になる)。続きは同梱のスクリプトを直接実行する

## Skill を呼べない文脈 (subagent など)

general-purpose の subagent など、Skill ツールを使えない文脈では、公式ドキュメントを WebFetch する前に、
同梱のスクリプトを直接実行する (WebFetch は要約モデル経由で field が抜ける)。

1. パスは `${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py`。`${CLAUDE_PLUGIN_ROOT}` が空の環境では、plugin の展開先 (`~/.claude/plugins/` の下) を探す。
   ディレクトリを指定して追加した marketplace は展開先へコピーされず、その場で読まれるので、見つからなければ
   `claude plugin marketplace list` で `Source: Folder (<dir>)` と出る marketplace の `<dir>` の下の
   `plugins/llms-docs/scripts/parse-claude-docs.py` を探す
2. `python3 <path> search "<キーワード>"` を実行し、出力末尾の `Next:` の先頭を `python3 <path>` に置き換えて本文を取る

## ソース

| ソース | `--source` | ドキュメント | 規模 |
|--------|-----------|-------------|------|
| Claude Code | `code` (デフォルト) | code.claude.com/docs | ~64p / 1.4MB |
| Claude Developer Platform | `platform` | platform.claude.com/docs | ~699p / 40MB |

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

# 両 source を順に (Skill や hook のように両方に解説がある topic 向け)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<キーワード>" --source both
```

`<キーワード>` は 2〜3 語のスペース区切り（例: `"PostCompact input compact_summary"`）。

出力は `[doc_idx] タイトル` + `URL` + 本文ヒットセクション (heading_path 付きスニペット)。並び順は全キーワードが揃うか → ページタイトルと見出し (祖先の見出しを含む) が全キーワードを語として含むか (キーワードが 1 語のときはページの順に使わない) → 本文 hits 数 → index score (title/description 一致) の順で、Changelog / Release notes は自動で末尾に deprioritize される (`--include-changelog-priority` で解除)。ai-sdk / firebase の `search` も同じ並び順。上位 N 件がどれも部分一致 (`[partial match]`) のときは、全文検索で全キーワードが 1 セクションに揃うページを最大 2 件探して `[body-only]` として追記する (既存の行は残り、`Next:` には index の最上位候補の行も残る。全キーワードが揃う候補が Changelog / Release notes だけのときも探す。全キーワードが揃う候補があっても、どの候補もページタイトルと見出しに全キーワードを語として含まなければ、含むページを同じく最大 2 件足す。キーワードが 1 語のときは足さない)。表示しきれなかった本文ヒットがある場合は `Other sections with hits (not shown):` として heading_path とヒット数の一覧が末尾に表示される。

各 `Section:` 行には `[<URL>#<anchor>]` が付く (末尾見出しタイトルから生成したベストエフォートの GitHub/Mintlify 互換 slug)。引用元を答えるときはこの URL#anchor をそのまま使ってよい — 同名見出しがページ内に複数ある場合の `-1`/`-2` 連番までは再現しない best-effort である点に注意。見出しタイトル自体に `/` を含む場合 (例: `## CI/CD`) も正しく slug 化される。

**anchor の正規化範囲 (これ以外は best-effort)**: slug は見出しのレンダリング後テキストから作る。英数字と `_` 以外の記号の連続は 1 つの `-` にする (`loop.md` は `loop-md`、`/security-review` は `security-review`)。アポストロフィは削る (`Can't` は `cant`)。正規化するのは インライン / 参照形式リンク (`[text](url)` / `[text][ref]`)・画像 (alt を採用)・脚注マーカー・HTML タグ・HTML 実体参照・コードスパン (中身は逐語)・`*` `~` と単語境界の `_` 強調記号。同名見出しの連番、ページ側の独自 ID 指定、上記以外の記法は再現しない。anchor が解決しない場合は URL 本体 (`#` の前) でページを開き、見出しを目視で探す。

`--source both` を受けるのは `search` だけ (`search-content` / `search-index` / `content` / `sections` / `fetch-index` は 1 source ずつ)。`--source both` のときは結果に `[code]` / `[platform]` プレフィックスが付き、`doc_idx` は **source 内でユニーク**なので、follow-up の `content` / `sections` 呼び出しには `--source <code|platform>` を明示する。

### Step 2: 該当セクションの本文を取得

```bash
# search が返した doc_idx をそのまま使う
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <doc_idx> "<heading_path>"

# ページ全体を取得（heading_path 省略）
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <doc_idx>
```

`content` は **サブセクション一覧** (`Subsections of '...'`) と次の `content` 呼び出し例を、本文の**前後両方**（metadata header 直後 と 本文末尾）に自動で出力する。長いページで本文が途中で切り詰められても（ターミナル/ツール側の出力上限）前側のヒントは必ず見える。さらに深掘りする際は `sections` を再度呼ばずに、そのまま次の `content` クエリに heading_path を渡せる。出力に含めたくない場合は `--no-subsection-hints` を付ける。

本文が長い場合は既定で 24000 文字に切り詰められ、`... (N chars truncated; narrow with ...)` を出す。子見出しの無い節 (絞る先が無い) では代わりに `Next: ... search-content <節の見出し> --page-ref N --context 0` を出す。そのまま実行でき、keyword を探したい語に差し替えて使う。`--max-chars 0` で無制限にできるが、Platform ページ (平均 ~38KB) は Bash tool の出力上限に達しやすいので通常は既定のままにする。

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
| `search` | `<query> [--source {code,platform,both}] [--file F] [--top-n N] [--max-hits N] [--context N] [--max-snippet-chars N] [--max-age S] [--include-changelog-priority]` | **推奨**: llms.txt ランキング + llms-full.txt 本文を URL で join、1 コマンドで候補ページ + 本文ヒットを返す。`--source both` で code/platform 両方を順に検索 (`--file` は単一 source 限定) |
| `content` | `<page_ref> [heading_path] [--file F] [--source S] [--max-age S] [--max-chars N] [--no-subsection-hints] [--no-link-annotations]` | セクション本文を表示。前後にサブセクション一覧、本文中の docs リンクには `→ [doc_idx N]` を付与。既定 24000 文字で切り詰め |
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

slug が複数ページに一致する場合は、`<lang>/<slug>` に完全一致するページが 1 件だけならそれに解決する (`hooks` は
`en/hooks`。`en/agent-sdk/hooks` は別のページで、stderr の `Note:` に、それを読むコマンド付きで出る)。そうでなければ曖昧エラーになり、
候補ごとに**そのまま実行できるコマンド**が付く。選んで実行するか、より長い slug (`agent-sdk/hooks`) か完全 URL を渡す。

### `heading_path` の指定方法

- **推奨**: `sections` / `search` / `content` の出力にある完全なパスをそのまま
  コピーする（スラッシュ区切りの階層パス、例: `"Hook events/PreToolUse/PreToolUse input"`）
- 見出しテキストの短い一部分だけを渡すこともできる: `"Configuration"`
  （ただし他の見出しとも部分一致し得るため、曖昧な場合はエラーになる — 下記）
- 部分一致（大文字小文字無視）で検索される。完全一致が優先され、部分一致の候補が
  2 件以上ある場合は `Error: ambiguous heading '...'. Matches: ...` で候補一覧を
  示して終了する（曖昧な入力を無言で先頭候補に解決しない）
- 見出しが無いとき (`Error: heading '...' not found.`) は、近い見出し (末尾の要素の部分一致・大文字小文字と空白の
  無視・綴りの近さ) が `Closest sections:` に、実行できるコマンド付きで先に出る。全見出しは `Available sections:` に続く
- `search` / `search-content` が本文中の見出し前ヒットを `Section: (top)` として
  返すことがある。この `(top)` をそのまま `content` の heading_path に渡すと、
  最初の見出しの直前までの本文（プリアンブル）を取得できる

---

## 制約

- **全文読み込み禁止**: search → content の順で絞り込むこと
- **コードフェンス保護**: スクリプトがコードブロックの途中分割を自動防止する
- **テーブル保護**: Markdown テーブルの途中分割を自動防止する
- **カスタムコンポーネント**: `<Note>`, `<Frame>`, `<Expandable>`, `<Card>` 等の JSX 記法はテキストとして読む

## 禁止事項

`allowed-tools` に Read/Bash があるため技術的には実行できてしまうが、
段階的絞り込みを迂回し全文読み込み相当になるため使用しない:

| 禁止 | 理由 | 代替 |
|------|------|------|
| `grep`/`rg "<kw>" <cache ファイル>` | キャッシュへの直接 grep は本文検索を迂回する | `search-content "<kw>"` |
| `Read <cache ファイル> lines X-Y` | 行番号直読みも同様に迂回する | `content <page_ref> "<heading_path>"` |
| `fetch-index \| grep` | 一覧のパイプ絞り込みも同様 | `search-index "<kw>"` |
| `cat <cache ファイル>` | キャッシュ全文の直接出力 | `fetch-index` → `search` から段階的に |

## 失敗時の対処

| パターン | 症状 | 対処 |
|----------|------|------|
| キャッシュ期限切れ | 7 日超のキャッシュ | 自動 re-fetch (既定 `--max-age 604800`) |
| ネットワーク失敗 | fetch timeout / connection error | 既存キャッシュがあれば WARNING を出して stale cache のまま継続 (exit 0)。無ければ Error で exit 1。復旧後に最新化したい場合は `--max-age 0` で強制再取得 |
| キャッシュ破損 | パースエラー / 不正なインデックス | `--max-age 0` で強制再取得 (キャッシュディレクトリは既定 `~/.cache/llms-docs`、`--cache-dir` で確認・変更可) |
| 結果ゼロ | `No matching ...` の下に `Why nothing matched:` (語ごとのページ数) | 全語が 0 件なら言い換えを続けず、別の語・`search-index`・別の `--source` に切り替える。一部の語だけ 0 件ならその語を落とす。全語が corpus にあるのに 0 件 (同じセクションに揃わない) なら語を減らす。いずれも続けて出る `Next:` がそのまま実行できる |
| `--source both` を `search` 以外に付けた | `Error: '<command>' reads one source at a time` (exit 2) | `both` を受けるのは `search` だけ。続けて出る `search ... --source both` か、同じコマンドを `--source code` / `--source platform` で 1 本ずつ打った行をそのまま実行する (`content` / `sections` の page index は source ごとに違う) |
| 曖昧な page_ref | `Ambiguous slug '...'. Matches:` | 候補ごとに実行できるコマンドが付く。選んでそのまま実行する (`<lang>/<slug>` に完全一致する 1 件があれば自動でそちらに解決) |
| heading が見つからない | `Error: heading '...' not found.` | `Closest sections:` の候補 (コマンド付き) を先に使う。全見出しは `Available sections:` に続く |
| Python バージョン不足 | 起動直後に PEP 604 のユニオン型記法が原因の `TypeError: unsupported operand type(s) for ...` | `python3 --version` を確認し 3.11 以上を用意する (`mise use python@3.11` 等)。3.11 未満では動作しない |
| スクリプトエラー (その他) | Python traceback | 下記 WebFetch フォールバックへ |

### WebFetch フォールバック

スクリプトで解決できない場合のみ使用する:

1. 同じ論点で `search` を 3 回まで試す (言い換えより `search-index` / `sections` で構造から当たる)。Skill を呼べない文脈ならまず上の「Skill を呼べない文脈」のとおりスクリプトを直接実行する
2. それでも失敗 → `code.claude.com/docs/en/<slug>` または `platform.claude.com/docs/en/<slug>` を WebFetch で直接取得
3. WebFetch は要約モデル経由のため field の抜け落ちリスクあり — 取得内容を鵜呑みにしない

## 出力フォーマット (参考)

調査の性質に応じて柔軟に構成してよい。固定セクションは強制しない — 複数フィールドの仕様調査では表組み、複数引用の比較では blockquote が読みやすい。最低限満たすべき要素:

- **発見事項**: 何が分かったか (見出し名は任意)
- **引用元**: 使用したドキュメントの URL またはタイトル + セクション (verbatim 引用は必ず出典を併記)
- **コード例**: ドキュメントから直接引用したもののみ (該当する場合)
- **注意事項**: 制約・バージョン要件・既知の罠 (該当する場合)

参考スケルトン (固定ではない):

```
### 調査結果
### コード例 *(該当する場合)*
### 情報源
### 注意事項 *(該当する場合)*
```

## ルール

- ドキュメントにない機能やオプションを捏造しない
- コード例はドキュメントから直接引用する
- 全文読み込みは禁止 — 必ず search → content の順で絞り込む
- `--source` フラグを明示する (code / platform のどちらを調査しているか明確にする)
- 日本語で回答する
- スクリプト失敗時は「失敗時の対処」に従う。WebFetch は最終手段
- 調査は簡潔に完了させること
