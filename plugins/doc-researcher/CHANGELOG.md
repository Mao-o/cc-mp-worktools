# Changelog

All notable changes to this plugin will be documented here.

## [0.7.0] - 2026-05-23

### 3 script API 統一 (BREAKING)

claude-docs v3 (0.5.0) で実装した `search` 統合 + `--page-ref` + `--file` flag 化を
ai-sdk / firebase にも展開し、3 script で以下の 6 サブコマンドを共通化:

- `fetch-index` — 軽量 index 一覧
- `search-index` — title/description でランキング (候補だけ取得)
- `search-content` — 本文横断キーワード検索 (`--page-ref` で 1 ページに絞れる)
- `search` — 統合検索 (`search-index` + 本文 hits を 1 コマンドで返す、推奨入口)
- `sections` — 指定ページの見出し一覧
- `content` — ページ全体 / セクション本文

`<page_ref>` は 3 形式を受け付ける (ai-sdk は URL を持たないため int / title 部分一致のみ):

- 整数 index
- URL slug (last path component)
- 完全 URL

### `researching-ai-sdk` skill 3.2.0 (BREAKING)

- `<file>` positional 引数を全廃止 → `--file` flag 化 (省略時は cache を auto-fetch)
- `search-content` の `--doc-index` → `--page-ref` (int / title 部分一致)
- `sections <file> <doc_index>` → `sections <page_ref>`
- `content <file> <doc_index> "<heading_path>"` → `content <page_ref> "<heading_path>"`
- 旧 `search` (= `search-index` alias) を廃止し、**統合 search** に置き換え。
  top N (`--top-n`, default 5) 候補をスコアリングし、各 body を keyword 検索して
  heading_path + スニペットを返す
- SKILL.md を 2 段階フロー (`search` → `content`) に書き直し、v3.1.0 → v3.2.0 migration 例を追記

旧 → 新 置換例:

| v3.1.0 (旧) | v3.2.0 (新) |
|------------|------------|
| `search-index /tmp/ai-sdk-llms.txt "X"` | `search-index "X"` |
| `search-content /tmp/ai-sdk-llms.txt "X" --doc-index 42` | `search-content "X" --page-ref 42` |
| `sections /tmp/ai-sdk-llms.txt 42` | `sections 42` |
| `content /tmp/ai-sdk-llms.txt 42 "X"` | `content 42 "X"` |
| `search /tmp/ai-sdk-llms.txt "X"` (旧 alias) | `search "X"` (統合検索) |

### `researching-firebase` skill 2.0.0 (BREAKING)

- `sections <doc_index>` → `sections <page_ref>` (int / URL slug / 完全 URL)
- `content <doc_index>` → `content <page_ref>`
- `search-content --pages <idx,idx,...>` (REQUIRED 複数) → `--page-ref <ref>` (optional 単数)
- 新規 `search` 統合 (top N on-demand fetch + 本文 hits)。Firebase は llms-full.txt が
  ないため top N ページを順次 HTTP fetch するヒューリスティクス (初回のみ重い、cache hit 後は高速)
- 旧 `--pages` 廃止 → 複数ページ横断したいときは `search` 経由を使う
- SKILL.md を 2 段階フロー (`search` → `content`) に書き直し、v1.1.0 → v2.0.0 migration 例を追記

旧 → 新 置換例:

| v1.1.0 (旧) | v2.0.0 (新) |
|------------|------------|
| `search-content "X" --pages 42` | `search-content "X" --page-ref 42` |
| `search-content "X" --pages 42,43,44` | `search "X" --top-n 3` (search 経由が推奨) |
| (新規可能) | `sections vector-search` (URL slug で直接アクセス) |

### `researching-claude-docs` skill (変更なし)

claude-docs は 0.5.0 で既に新 API を実装済み。本 release で他 2 script が claude-docs と
揃ったため、3 script 共通の使い方が一貫した。

### scripts/_common.py

- 変更なし (既存の `score_entry` / `search_index_entries` / `search_content_in_body` /
  `normalize_doc_url` / `build_url_to_full_index` 等を ai-sdk / firebase の新規 `cmd_search`
  / `_resolve_page_ref` から再利用)

### 検証

- 3 script の `--help` および各サブコマンド の `--help` が argparse error なし
- `parse-ai-sdk.py search "streamText onFinish" --top-n 2` で top 2 候補 + 本文 hits が
  正常に取得できる (cache hit 時 ~1 秒)
- `parse-claude-docs.py search "hook" --index-limit 2` の既存挙動は維持 (regression なし)

### 設計判断記録 (subagent fork 維持)

3 SKILL の `context: fork + model: sonnet` 構成は **維持** する設計判断を明文化
(README.md の「設計判断」セクション参照)。spawn オーバーヘッドより context rot
回避と正確性を優先するため、軽量化方向 (fork 外し / 親 model 継承 / 軽量モード追加)
は採用しない。「軽い質問でも WebFetch に流れる」課題への対応は description 充実
(0.6.0) + doc-first rules (`~/.claude/rules/`) + `search` 統合 (本 release) の
3 系統で行う。

## [0.6.0] - 2026-05-23

### 3 SKILL description の統一: Triggers / Use proactively / WebFetch 優位文

- `researching-claude-docs` の frontmatter `description` に
  `Use proactively when ...` / `Use when implementing or debugging ... such as ...` /
  `Triggers: ...` 行を追加 (ai-sdk / firebase と同じパターンに揃える)。Triggers は
  "Claude Code", "hook schema", "subagent", "plugin manifest", "slash command",
  "settings.json", "permission", "MCP", "Anthropic API", "code.claude.com",
  "platform.claude.com", "Claude Code ドキュメント", "researching-claude-docs"
  の 13 個。これまで Triggers 列挙がなく ai-sdk / firebase と非対称だった状態を
  解消し、LLM 側の skill 候補マッチ率を底上げする
- 3 SKILL すべての `description` に「WebFetch ではなくこのスキルを使う（要約
  モデル経由ではないため field の抜け落とし・幻覚が起きない）」の優位文を
  統一文型で揃える。claude-docs に既存だったこの文型を ai-sdk / firebase の
  description にも追加し、3 スキル共通のパターンで LLM が「verbatim 取得が
  欲しい」「field 抜け落ちを避けたい」場面を引っかけられるようにする
- SKILL 本体 (markdown) / parse スクリプト I/F / キャッシュ動作の変更なし。
  description のみの非破壊変更

## [0.5.0] - 2026-05-14

### `researching-claude-docs` skill 3.0.0 (UX 改善 + 一部破壊的)

- **NEW** `search` subcommand: `llms.txt` のタイトル/説明ランキングと
  `llms-full.txt` の本文検索を **URL で join** して 1 コマンドで返す統合検索。
  返ってくる `doc_idx` は `content` / `sections` にそのまま渡せる
  (`search-index` / `search-content` 間の doc_idx 乖離問題を根本解決)
- **NEW** `--max-age` flag for `search` / `search-content` / `search-index` /
  `sections` / `content` / `fetch-index`: 既定では `/tmp/` キャッシュは無期限
  再利用、`--max-age N` (秒) を指定すれば期限切れで自動再 fetch
- **NEW** `--max-snippet-chars` flag for `search` / `search-content`:
  スニペット文字数上限 (既定 500 文字、`0` で無制限)
- **NEW** Changelog / Release notes ページの自動 deprioritize (`search` /
  `search-content`)。`--include-changelog-priority` で旧挙動に戻せる
- **BREAKING** `sections` / `content` / `search-content` の引数を再設計:
  - `file` positional 引数を廃止 → `--file` flag 化
  - `sections` / `content` の `doc_index` positional を `page_ref` に拡張、
    整数 / URL slug / 完全 URL を runtime で自動判別
  - `search-content` の `--doc-index` を `--page-ref` に改名 (slug/URL も受付)
  - 旧 `sections /tmp/claude-code-llms-full.txt 5` 形式は argparse error
  - 移行例: `sections 5` / `content hooks "Hook events/PreToolUse"` /
    `content https://code.claude.com/docs/en/hooks "..."`
- SKILL.md を全面書き直し: 推奨フローを 2 段階 (`search` → `content`) に簡略化、
  `page_ref` の 3 形式・slug 曖昧時の対処・v2 → v3 移行例を追記
- `next_hint` が `--source` を伝搬: `--source platform` で実行した時の follow-up
  ヒントが silently `code` (デフォルト) に落ちる事故を防止。デフォルトソース時
  はヒントを短く保つため省略
- `fetch-index` の `Next:` ヒントを v3 形式 (`sections <doc_index>`) に統一
  (旧 file positional 表記が残っていた点を修正)
- `--file` と `--source` の不整合検出: ユーザーが `--file /tmp/claude-platform-llms-full.txt`
  を渡したのに `--source` がデフォルト `code` だった場合などに、silent
  cross-population (platform 名のファイルに code docs を書き込む等) を防ぐため
  fetch 前に fail-fast。未知の `--file` (推測不能なパス) は従来通り通す
- `--include-changelog-priority` の挙動を `cmd_search` と `cmd_search_content`
  で揃える: フラグが ON のときペナルティ項だけを 0 にし、relevance ソート
  (`total_matches` 降順) は維持。以前は `search-content` 側でソート全体を
  skip していたため `--limit` が元の文書順で切られ、高 hit ページが落ちる
  リグレッションがあった
- `--file` 指定時のセマンティクスを **read-only** に変更: 既存 user ファイル
  を `--max-age` で silently 上書きする regression を排除。`--file` ありの時
  は (1) 既知 cache 名と `--source` の不整合を fetch 前に die (前出修正)、
  (2) ファイル不在も fetch せず die (`--file` を外して auto-fetch せよと案内)、
  (3) `--max-age` は無視。fetch-and-cache サイクルは `--file` を渡さない時
  にだけ走る。`--file` で渡したローカルスナップショットは絶対に上書きされない

### `_common.py` 共有ヘルパー強化

- `fetch_url` に `max_age` kwarg を追加 (既存呼び出しは backward compatible)
- `search_content_in_body` に `max_snippet_chars` kwarg を追加 (既存呼び出しは
  backward compatible)
- `normalize_doc_url` / `build_url_to_full_index` を追加 (`.md` suffix /
  trailing slash / query / fragment を剥がした正規化 URL で
  llms.txt ↔ llms-full.txt の 1:1 join を担保)

### 検証

- Claude Code llms.txt と llms-full.txt の **131 entries が 100% URL join**
  することを実機確認 (`.md` suffix strip で一致)
- `search "test"` は join 警告無しで動作

## [0.4.0] - 2026-04-15

- Add `search-index` subcommand to all three parse scripts. Replaces the
  Agent's previous habit of running `grep` over `llms.txt` to locate pages by
  keyword. Ranks pages against title / description (and tags / H1-H2 headings
  for AI SDK) with case-insensitive AND scoring. On `parse-ai-sdk.py`, the
  existing `search` subcommand is renamed and kept as an alias for backwards
  compatibility
- Add `search-content` subcommand to all three parse scripts. Performs
  section-level AND keyword search across `llms-full.txt` bodies (AI SDK /
  Claude) or lazily-fetched pages listed in `--pages` (Firebase, which has no
  `llms-full.txt`). Returns `heading_path`, a snippet with `→` markers on hit
  lines, matched keywords, per-section hit count, source URL, and a grand
  total so the Agent can jump straight to `content` without a follow-up grep
- Promote `search_index_entries` / `search_content_in_body` / `score_entry`
  to `_common.py` so all three sources share one search implementation.
  `parse-ai-sdk.py` drops its private `score_document` helper; the
  equivalent scoring weights (title 10/5, tags 4, description 2, headings 1,
  all-keyword bonus 10) now live in `score_entry`
- Section-level AND semantics: `search-content` requires every query keyword
  to appear somewhere within the same section before it's reported. Sections
  with 20+ hit lines are truncated to the first three with a trailing
  "… (N more hits in this section)" marker to keep output scannable
- Rewrite three SKILL.md files around the new entry points (search-index →
  sections/search-content → content), explicitly forbid the common
  grep/Read-lines fallbacks, and document the `llms.txt` / `llms-full.txt`
  `doc_index` divergence in Claude docs (search-content is the safe chain
  because its `doc_index` is the one sections/content use). Bump skill
  versions: ai-sdk 3.1.0 / claude-docs 2.1.0 / firebase 1.1.0
- Update README subcommand table, dev-test commands, and maintenance notes
  to reflect the new entry points and the AND semantics of `search-content`

## [0.3.0] - 2026-04-15

- Extract shared parser / fetch / output helpers into `scripts/_common.py`
  (`FenceTracker`, `extract_sections`, `extract_content`, `parse_llms_index`,
  `fetch_url`, `load_lines`, `die*` error helpers, `print_metadata_header`,
  `next_hint`, and argparse skeleton helpers). The three `parse-*.py` scripts
  are now thinner and consistent in behavior
- Fix `Next:` hint in `parse-ai-sdk.py` (3 call sites) and `parse-claude-docs.py`
  (2 call sites) which referenced the pre-rename script name
  (`parse-llms-txt.py`). Firebase was already correct; all three now derive the
  hint from `sys.argv[0]`
- No user-visible behavior change beyond the `Next:` hint fix; all other
  subcommand stdout/stderr is byte-identical to 0.2.0
- Unify 3 SKILL.md structure: add `context: fork` / `model: sonnet` frontmatter
  and "出力フォーマット" / "ルール" sections to `researching-claude-docs`;
  patch-bump SKILL versions (ai-sdk 3.0.1 / claude-docs 2.0.1 / firebase 1.0.1)
- Update README: Python requirement corrected to 3.10+ (parse-\*.py uses PEP 604
  syntax; only `_common.py` is 3.8+-compatible via `from __future__ import annotations`);
  add `scripts/_common.py` row to Components table and a paragraph on the shared
  helper layer to the maintenance notes
- Use `os.path.realpath(__file__)` (not `abspath`) when prepending the script
  directory to `sys.path` in the three `parse-*.py` scripts, so symlinked
  invocations cannot be shadowed by an unrelated `_common.py` sitting next
  to the symlink. Verified with an adversarial test (Codex Review P2 feedback
  on PR #3)
- Thread `min_level` through `_common.extract_content` (default 2) and have
  `parse-ai-sdk.py` pass `min_level=1` explicitly. The previous hardcoded
  `min_level=1` meant `cmd_sections` (H2+) and `cmd_content`'s internal
  heading lookup (H1+) disagreed in `parse-firebase.py`, which hands the raw
  page (H1 included) to `extract_content` — a Firebase page with an H1 and
  an H2 sharing the same title could have `content` match the H1 and return
  nearly the whole document instead of the intended H2 section. Claude docs
  and Firebase `content` output is now byte-identical to 0.2.0 again (Codex
  Review P2 feedback on PR #3 commit `e449a21`)

## [0.2.0] - 2026-04-15

- Add `researching-firebase` skill (Firebase docs progressive loader)
- Add `parse-firebase.py` script (per-page on-demand fetch; no llms-full.txt available)
- Use collision-resistant cache filenames (readable path + sha1 hash suffix) so
  Firebase URLs differing only by `/` vs `_` no longer share a cache file
- Update plugin description and keywords to include Firebase
- Update marketplace.json entry and root README

## [0.1.0] - 2026-04-14

- Initial release
- `researching-claude-docs` skill (Claude Code + Platform docs)
- `researching-ai-sdk` skill (Vercel AI SDK docs)
- Script paths updated to use `${CLAUDE_PLUGIN_ROOT}`
