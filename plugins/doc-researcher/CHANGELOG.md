# Changelog

All notable changes to this plugin will be documented here.

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
