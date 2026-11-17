"""The plugin ships three skills, and every bundled preset is reachable from one.

The Skill tool picks a skill from its description + when_to_use alone, and the
script takes any ``--source``, so nothing at run time notices a preset that no
skill mentions. These tests hold the hand-written lists to ``presets.json``.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests import _loader

PLUGIN_DIR = Path(_loader.SCRIPTS_DIR).parent
SKILLS_DIR = PLUGIN_DIR / "skills"
PRESETS_FILE = Path(_loader.SCRIPTS_DIR) / "presets.json"

CLAUDE, CLOUDFLARE, LIBRARY = ("researching-claude-docs", "researching-cloudflare-docs",
                               "researching-library-docs")
REMOVED = ("researching-ai-sdk", "researching-firebase")

# Claude Code cuts description + when_to_use at this many characters in the
# skill listing, silently dropping the Triggers at the end
LISTING_CAP = 1536
# leave room for one more preset
LISTING_MARGIN = 50
# what the listing puts between the two fields, and counts against the cap
LISTING_SEPARATOR = " - "

# skill -> the heading of the table that lists its sources
SOURCE_TABLE = {CLAUDE: "## ソース", CLOUDFLARE: "## Step 0", LIBRARY: "## Step 0"}

# preset -> (the skill that owns it, a word of that skill's listing that names it)
LISTING_WORD = {
    "claude-code": (CLAUDE, "Claude Code"),
    "claude-platform": (CLAUDE, "Claude Developer Platform"),
    "claude": (CLAUDE, "Claude Code"),
    "cloudflare-workers": (CLOUDFLARE, "Workers"),
    "cloudflare-d1": (CLOUDFLARE, "D1"),
    "cloudflare-r2": (CLOUDFLARE, "R2"),
    "cloudflare-kv": (CLOUDFLARE, "Workers KV"),
    "cloudflare-durable-objects": (CLOUDFLARE, "Durable Objects"),
    "cloudflare-pages": (CLOUDFLARE, "Pages"),
    "cloudflare-queues": (CLOUDFLARE, "Queues"),
    "cloudflare-workers-ai": (CLOUDFLARE, "Workers AI"),
    "cloudflare-vectorize": (CLOUDFLARE, "Vectorize"),
    "cloudflare-hyperdrive": (CLOUDFLARE, "Hyperdrive"),
    "cloudflare-agents": (CLOUDFLARE, "Agents"),
    "cloudflare-workflows": (CLOUDFLARE, "Workflows"),
    "cloudflare-ai-gateway": (CLOUDFLARE, "AI Gateway"),
    "cloudflare-browser-rendering": (CLOUDFLARE, "Browser Rendering"),
    "cloudflare-containers": (CLOUDFLARE, "Containers"),
    "ai-sdk": (LIBRARY, "AI SDK"),
    "firebase": (LIBRARY, "Firebase"),
    "nextjs": (LIBRARY, "Next.js"),
    "vite": (LIBRARY, "Vite"),
    "vitest": (LIBRARY, "Vitest"),
    "drizzle": (LIBRARY, "Drizzle"),
    "zod": (LIBRARY, "Zod"),
    "hono": (LIBRARY, "Hono"),
    "bun": (LIBRARY, "Bun"),
    "vercel": (LIBRARY, "Vercel"),
    "render": (LIBRARY, "Render"),
    "mcp": (LIBRARY, "MCP (Model Context Protocol)"),
    "codex": (LIBRARY, "Codex"),
    "ollama": (LIBRARY, "Ollama"),
    "pixai": (LIBRARY, "PixAI"),
    "agentskills": (LIBRARY, "Agent Skills"),
    "agent-plugins": (LIBRARY, "Agent Plugins"),
    "openai-plugins": (LIBRARY, "Codex・Plugins"),
    "openai-api-docs": (LIBRARY, "OpenAI API"),
    "acp": (LIBRARY, "ACP"),
    "cline": (LIBRARY, "Cline"),
    "factory": (LIBRARY, "Factory"),
    "devin": (LIBRARY, "Devin"),
    "gemini-cli": (LIBRARY, "Gemini CLI"),
    "kilo-code": (LIBRARY, "Kilo Code"),
    "openai-agents-js": (LIBRARY, "OpenAI Agents SDK"),
    "continue": (LIBRARY, "Continue"),
    "zed": (LIBRARY, "Zed"),
    "amp": (LIBRARY, "Amp"),
}

# Words the old dedicated skills fired on, which have to survive the move.
KEPT_TRIGGERS = {
    LIBRARY: (
        "streamText", "generateText", "useChat", "streamObject", "generateObject", "useObject",
        "embedMany", "convertToModelMessages", "@ai-sdk", "`ai`",
        "Firestore", "Firebase Auth", "Cloud Functions", "FCM", "Firebase Hosting", "App Hosting",
        "Realtime Database", "Cloud Storage", "App Check", "Remote Config", "Crashlytics",
        "Data Connect", "Genkit", "AI Logic", "security rules", "firestore.rules", "storage.rules",
        "firebase.json", "firebase-admin", "firebase-functions",
    ),
    CLOUDFLARE: ("wrangler", "wrangler.toml", "wrangler.jsonc", "cloudflare:workers"),
}


def skill_text(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def frontmatter(name: str) -> str:
    return skill_text(name).split("\n---\n", 1)[0] + "\n"


def block(front: str, key: str) -> str:
    """A ``key: |`` block of the frontmatter, flattened the way the listing reads it."""
    # A blank line does not end a block scalar: the text after it is still listed.
    m = re.search(rf"^{key}: \|\n((?:(?:  .*)?\n)+)", front, re.M)
    if m is None:
        raise AssertionError(f"{key} is not a block in the frontmatter")
    return " ".join(line.strip() for line in m.group(1).splitlines())


def listing_of(front: str) -> str:
    return block(front, "description") + LISTING_SEPARATOR + block(front, "when_to_use")


def listing(name: str) -> str:
    return listing_of(frontmatter(name))


def table_sources(name: str) -> set[str]:
    """The source names in the second column of a skill's source table."""
    table = skill_text(name).split(SOURCE_TABLE[name], 1)[1].split("\n## ", 1)[0]
    named = set()
    for row in re.findall(r"(?m)^\|[^|\n]+\|([^|\n]+)\|", table):
        # the Claude table has a third column; the name is always in the second
        named |= set(re.findall(r"`([a-z0-9-]+)`", row))
    return named - {"--source"}


def presets() -> set[str]:
    return set(json.loads(PRESETS_FILE.read_text(encoding="utf-8"))["sources"])


class SkillSetTest(unittest.TestCase):
    def test_exactly_three_skills_named_after_their_directories(self):
        dirs = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
        self.assertEqual(dirs, {CLAUDE, CLOUDFLARE, LIBRARY})
        for name in dirs:
            with self.subTest(name):
                self.assertRegex(frontmatter(name), rf"(?m)^name: {name}$")

    def test_each_listing_stays_under_the_cap_and_is_read_whole(self):
        for name in (CLAUDE, CLOUDFLARE, LIBRARY):
            with self.subTest(name):
                self.assertLess(len(listing(name)), LISTING_CAP - LISTING_MARGIN)

    def test_listing_counts_past_a_blank_line_and_the_separator(self):
        front = "---\ndescription: |\n  ab\n\n  cd\nwhen_to_use: |\n  ef\nmodel: sonnet\n"
        self.assertEqual(listing_of(front), "ab  cd - ef")

    def test_bodies_stay_short_enough_to_load(self):
        for name in (CLAUDE, CLOUDFLARE, LIBRARY):
            with self.subTest(name):
                self.assertLessEqual(len(skill_text(name).splitlines()), 500)

    def test_relative_links_of_each_skill_resolve(self):
        links = 0
        for name in (CLAUDE, CLOUDFLARE, LIBRARY):
            for target in re.findall(r"\]\((references/[^)\s]+)\)", skill_text(name)):
                links += 1
                with self.subTest(skill=name, target=target):
                    self.assertTrue((SKILLS_DIR / name / target).is_file())
        self.assertGreaterEqual(links, 1)


class SharedTablesTest(unittest.TestCase):
    """The library and Cloudflare skills run the same commands against the same
    script, so a row added to one has to reach the other."""

    @staticmethod
    def shared(name, heading):
        text = skill_text(name)
        body = text[text.index(heading):].split("\n## ", 1)[0]
        return body.split("\n### ", 1)[0]

    def test_command_and_failure_tables_are_the_same_in_both_skills(self):
        for heading in ("## コマンドリファレンス", "## 失敗時の対処"):
            with self.subTest(heading):
                self.assertEqual(self.shared(LIBRARY, heading), self.shared(CLOUDFLARE, heading))

    def test_search_content_row_lists_the_options_that_narrow_its_output(self):
        table = self.shared(LIBRARY, "## コマンドリファレンス")
        row = re.search(r"(?m)^\| `search-content` \|.*$", table).group(0)
        for option in ("--context N", "--max-snippet-chars N"):
            with self.subTest(option):
                self.assertIn(option, row)


class SourceStepTest(unittest.TestCase):
    """A name the table does not have may still be a profile the user added."""

    def test_each_table_skill_looks_at_the_sources_list_before_giving_up(self):
        for name in (CLOUDFLARE, LIBRARY):
            step = skill_text(name).split("## Step 0", 1)[1].split("\n## ", 1)[0]
            for needle in ("または `sources` の一覧にある名前", 'parse-llms-txt.py" sources',
                           "`sources.json` に足した profile も出る", "一覧にも無ければ"):
                with self.subTest(skill=name, needle=needle):
                    self.assertIn(needle, step)


class EveryPresetIsRoutedTest(unittest.TestCase):
    def test_every_preset_is_in_exactly_one_skills_table(self):
        tables = {name: table_sources(name) for name in SOURCE_TABLE}
        for preset in sorted(presets()):
            with self.subTest(preset):
                owners = [name for name, sources in tables.items() if preset in sources]
                self.assertEqual(len(owners), 1, f"{preset} is in the tables of {owners}")

    def test_no_table_names_a_source_that_is_not_a_preset(self):
        for name in SOURCE_TABLE:
            with self.subTest(name):
                self.assertEqual(table_sources(name) - presets(), set())

    def test_the_table_agrees_with_the_owner_in_listing_word(self):
        for preset, (owner, _word) in LISTING_WORD.items():
            with self.subTest(preset):
                self.assertIn(preset, table_sources(owner))

    def test_listing_word_covers_exactly_the_presets(self):
        self.assertEqual(set(LISTING_WORD) - presets(), set(), "a preset that is gone")
        self.assertEqual(presets() - set(LISTING_WORD), set(),
                         "a new preset: name it in a skill's description and add it here")

    def test_owner_listing_names_each_preset(self):
        for preset, (owner, word) in LISTING_WORD.items():
            with self.subTest(preset):
                # as a word: "Vite" inside "Vitest" does not name Vite
                self.assertRegex(listing(owner), rf"(?<!\w){re.escape(word)}(?!\w)")

    def test_cloudflare_sources_all_belong_to_the_cloudflare_skill(self):
        cloudflare = {p for p in presets() if p.startswith("cloudflare-")}
        self.assertEqual(table_sources(CLOUDFLARE), cloudflare)


class TriggerWordsTest(unittest.TestCase):
    def test_words_of_the_folded_skills_survive(self):
        for name, words in KEPT_TRIGGERS.items():
            text = listing(name)
            for word in words:
                with self.subTest(skill=name, word=word):
                    self.assertIn(word, text)

    def test_cloudflare_words_left_the_library_listing(self):
        # the point of the split: they would use up the library skill's room
        text = listing(LIBRARY)
        for word in ("Cloudflare", "wrangler", "Durable Objects", "Vectorize"):
            with self.subTest(word):
                self.assertNotIn(word, text)

    def test_library_skill_has_no_paths(self):
        # The docs read "only when working with files matching": a Firebase-only
        # glob must not gate a skill that reads every other site as well.
        self.assertNotRegex(frontmatter(LIBRARY), r"(?m)^paths:")


class FoldedBodiesTest(unittest.TestCase):
    """What only the removed skills told the reader, now in the library skill."""

    def section(self, heading):
        text = skill_text(LIBRARY)
        start = text.index(heading) + len(heading)
        following = re.search(r"^##+ ", text[start:], re.M)
        return text[start:start + following.start()] if following else text[start:]

    def test_ai_sdk_notes(self):
        body = self.section("### AI SDK (`--source ai-sdk`)")
        for needle in ("4 形式", "タイトルの部分一致が先に効く", "matched the title of [N]", "`acp`",
                       "No page found for URL", "No document found for",
                       "`URL#anchor` は付かない", "`<Snippet>`", "ai-sdk.dev/docs/<slug>",
                       "references/ai-sdk-llms-txt-structure.md"):
            with self.subTest(needle):
                self.assertIn(needle, body)

    def test_firebase_notes(self):
        body = self.section("### Firebase (`--source firebase`)")
        for needle in ("`llms-full.txt` が無い", "`.md.txt`", "on-demand", "`--top-n`",
                       "`search-content` は `--page-ref` を付ける", "7000", "--offset 100 --limit 100",
                       "`--limit 0`", "3 形式", "`_`", "#read_write_data", "best-effort",
                       "firebase-docs/", "(skip: fetch failed", "documents skipped",
                       "`--page-ref` で絞っていると", "firebase.google.com/docs/<product>"):
            with self.subTest(needle):
                self.assertIn(needle, body)

    def test_page_by_page_sources_say_search_has_no_full_text_fallback(self):
        # the general note on [body-only] does not hold for these (_fetch_search_results)
        needle = "全文検索の補い (`[body-only]`) は無く、上位 N 件の外のページは取得しない"
        self.assertIn(needle, self.section("### Firebase (`--source firebase`)"))
        self.assertIn(needle, self.section("## page_ref の指定方法"))

    def test_error_strings_the_notes_quote_are_still_printed(self):
        printed = "".join(
            (Path(_loader.SCRIPTS_DIR) / f).read_text(encoding="utf-8")
            for f in ("_common.py", "_commands.py", "parse-llms-txt.py"))
        for quoted in ("matched the title of [", "No page found for URL", "No document found for",
                       "(skip: fetch failed", "documents skipped"):
            with self.subTest(quoted):
                self.assertIn(quoted, printed)


class MigrationNoticeTest(unittest.TestCase):
    def readme(self):
        return (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")

    def removed_section(self):
        text = self.readme()
        heading = "### Renamed / removed skills"
        self.assertIn(heading, text)
        return text.split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]

    def test_readme_names_each_removed_skill_with_where_it_went(self):
        section = self.removed_section()
        for old, source in zip(REMOVED, ("ai-sdk", "firebase"), strict=True):
            with self.subTest(old):
                row = re.search(rf"(?m)^\| `{old}` \|.*$", section)
                self.assertIsNotNone(row, section)
                self.assertIn(LIBRARY, row.group(0))
                self.assertIn(f"--source {source}", row.group(0))

    def test_changelog_names_each_removed_skill_with_where_it_went(self):
        # the release that removed them (later releases add their sections above it)
        text = (PLUGIN_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
        releases = {s.split("]", 1)[0]: s for s in re.split(r"(?m)^## \[", text)[1:]}
        removal = releases["0.52.0"]
        for old in REMOVED:
            with self.subTest(old):
                self.assertIn(old, removal)
        self.assertIn(LIBRARY, removal)

    def test_session_start_points_at_the_three_skills_only(self):
        hooks = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        notice = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        for name in (CLAUDE, CLOUDFLARE, LIBRARY):
            with self.subTest(name):
                self.assertIn(f"llms-docs:{name}", notice)
        for old in REMOVED:
            with self.subTest(old):
                self.assertNotIn(old, notice)
        # each word goes with its own skill
        pieces = {}
        for piece in notice.split(" / "):
            for skill in re.findall(r"\(Skill: llms-docs:([a-z-]+)\)", piece):
                pieces[skill] = piece
        self.assertIn("Cloudflare", pieces.get(CLOUDFLARE, ""))
        self.assertIn("AI SDK", pieces.get(LIBRARY, ""))
        self.assertIn("Firebase", pieces.get(LIBRARY, ""))
        self.assertNotIn("Cloudflare", pieces.get(LIBRARY, ""))

    def test_old_names_appear_only_where_the_move_is_explained(self):
        readme_rest = self.readme().replace(self.removed_section(), "")
        places = {
            "README outside the removed section": readme_rest,
            "AGENTS.md": (PLUGIN_DIR / "AGENTS.md").read_text(encoding="utf-8"),
            "claude skill": skill_text(CLAUDE),
            "cloudflare skill": skill_text(CLOUDFLARE),
            # the library skill keeps them as triggers only: an old rule that
            # still says the old name finds the new skill through the listing
            "library skill body": skill_text(LIBRARY).split("\n---\n", 1)[1],
        }
        # the design note that says what happened to the old paths is the one exception
        allowed = "0.52.0 で畳んだ\n`researching-firebase` の `paths`"
        places["README outside the removed section"] = readme_rest.replace(allowed, "")
        for place, text in places.items():
            for old in REMOVED:
                with self.subTest(place=place, old=old):
                    self.assertNotIn(old, text)


class ReaderSetupTest(unittest.TestCase):
    """The shipped docs lean on nothing the plugin does not ship: whoever
    installs it has no rule file of the author's under their home."""

    # the paths under the home that every install has: where plugins are
    # installed, the default cache, the default sources.json
    HOME_PATHS = ("~/.claude/plugins/", "~/.cache/llms-docs", "~/.config/llms-docs/")

    @staticmethod
    def shipped_docs():
        yield PLUGIN_DIR / "README.md"
        yield PLUGIN_DIR / "AGENTS.md"
        yield from sorted(SKILLS_DIR.glob("*/SKILL.md"))
        yield from sorted(SKILLS_DIR.glob("*/references/*.md"))
        yield from sorted((PLUGIN_DIR / "docs").glob("*.md"))

    def test_home_paths_are_only_those_of_every_install(self):
        seen = 0
        for path in self.shipped_docs():
            for found in re.findall(r"~/[^\s`)'\"]*", path.read_text(encoding="utf-8")):
                seen += 1
                with self.subTest(file=path.name, path=found):
                    self.assertTrue(found.startswith(self.HOME_PATHS), found)
        self.assertGreater(seen, 0)

    def test_the_design_note_leaves_the_instruction_file_to_the_reader(self):
        readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        note = readme.split("## 設計判断: subagent fork + Sonnet は維持する", 1)[1].split("\n## ", 1)[0]
        # the second of the ways to keep light questions off WebFetch
        item = re.search(r"(?ms)^2\. (.*?)(?=^\d+\. |\n\n)", note)
        self.assertIsNotNone(item, note)
        self.assertIn("plugin はこの指示を同梱しない", item.group(1))
        # it names the skills the plugin has, by their names
        self.assertEqual(sorted(set(re.findall(r"`(researching-[a-z-]+)`", item.group(1)))),
                         sorted((CLAUDE, CLOUDFLARE, LIBRARY)))


if __name__ == "__main__":
    unittest.main()
