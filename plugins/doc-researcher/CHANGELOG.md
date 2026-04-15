# Changelog

All notable changes to this plugin will be documented here.

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

## [0.2.0] - 2026-04-15

- Add `researching-firebase` skill (Firebase docs progressive loader)
- Add `parse-firebase.py` script (per-page on-demand fetch; no llms-full.txt available)
- Use collision-resistant cache filenames (readable path + sha1 hash suffix) so
  Firebase URLs differing only by `/` vs `_` no longer share a cache file
- Update plugin description and keywords to include Firebase
- Update marketplace.json entry and root README

## [0.1.0] - 2026-04-14

- Initial release: migrated from global skills (`~/.claude/skills/`)
- `researching-claude-docs` skill (Claude Code + Platform docs)
- `researching-ai-sdk` skill (Vercel AI SDK docs)
- Script paths updated to use `${CLAUDE_PLUGIN_ROOT}`
