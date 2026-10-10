"""Generate the synthetic cache directories used by ``test_migration_parity.py``.

Every page here is made up: filler sentences come from a fixed lorem word
list and product names are invented (Quillon, Vantry, Loomkit, Zentor). The
URL hosts are the real ones because the three dedicated parsers hard-code
them (``firebase.google.com`` page cache names, the default source URLs).
A few labels are kept because the local full-corpus gate runs the same
cases on the real cache and they have to resolve there too: the page slug
``ip-addresses`` with its heading ``Inbound IP addresses``, the page title
``useCompletion`` and the heading ``API Signature``.

What the fixtures do reproduce is the *shape* of the upstream files,
including the edge cases a parser has to get right:

* claude-code (``# Title`` + ``Source:``): an index with entries that have no
  page in the full text, relative and absolute links between pages
  (``→ [doc_idx N]`` annotations), pages whose slugs or titles are close
  (``snapshotting`` / ``kit/file-snapshotting``, ``hub`` / ``hubs`` /
  ``hub-rollout``), indented fences inside steps, a heading in quotes, and a
  last page whose final code fence is never closed (the headings after it
  are fenced text). That page is longer than 3000 characters, so
  ``--max-chars 3000`` truncates it.
* claude-platform (frontmatter): a preamble before the first page,
  navigation headings that sit between pages (they dangle at the tail of
  the previous page), ``# Title`` lines outside fences in a body, two pages
  with the same title, a ``(Beta)`` variant group and index entries with no
  page, index titles that differ from the page titles, a quoted description
  with ``:`` in it, and namesakes (``folding`` / ``folding-background``).
* ai-sdk (frontmatter with quoted ``url:``): ``tags:`` block lists, titles
  with ``\\"`` escapes, the per-page footer after a ``---`` rule.
* firebase: an index whose entries are partly in the page cache (the rest
  are fetched through the harness's failing network mock), HTML entities in
  titles, ``> [!NOTE]`` blocks.
* body lines that start with ``Note:`` / ``Tip:`` and use the words
  ``page`` / ``document`` in pages the ``content`` cases print, so the
  comparator's exclusions are exercised on page bodies too.

To regenerate (from ``scripts/tests``)::

    python3 fixtures/parity/build_fixtures.py
    python3 test_migration_parity.py --update-golden

then review the diff of both directories. The golden is what the old
scripts print for these fixtures, so it is regenerated together with them.
"""

import hashlib
import shutil
from pathlib import Path

OUT = Path(__file__).resolve().parent

_WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed eiusmod tempor "
    "incididunt labore dolore magna aliqua enim minim veniam quis nostrud "
    "exercitation ullamco laboris nisi aliquip commodo consequat duis aute irure "
    "reprehenderit voluptate velit esse cillum fugiat nulla pariatur excepteur "
    "sint occaecat cupidatat proident sunt culpa officia deserunt mollit anim "
    "laborum"
).split()


def lorem(seed: int, n: int = 14) -> str:
    """A deterministic filler sentence of *n* words."""
    x = (seed * 2654435761 + 97) % 2**31
    words = []
    for _ in range(n):
        x = (x * 1103515245 + 12345) % 2**31
        words.append(_WORDS[(x >> 8) % len(_WORDS)])
    text = " ".join(words)
    return text[0].upper() + text[1:] + "."


def para(seed: int, sentences: int = 2, *, lead: str = "", tail: str = "") -> str:
    parts = [lorem(seed * 7 + k) for k in range(sentences)]
    return " ".join(p for p in (lead, *parts, tail) if p)


def write(rel: str, lines: list) -> None:
    path = OUT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# claude-code (H1 + Source:)
# ---------------------------------------------------------------------------

CODE = "https://code.claude.com/docs/en/"


def code_page(title: str, slug: str, desc: str, body: list) -> list:
    return [f"# {title}", f"Source: {CODE}{slug}", "", desc, "", *body, "", ""]


def build_claude_code() -> None:
    pages = [
        ("Quillon Tag", "quillon-tag",
         "Bring Quillon into a shared chat channel and find where its setup lives.",
         [para(1, lead="Quillon Tag answers in a team channel under one shared identity."),
          "",
          "Note: Quillon Tag is distinct from the earlier [Quillon in Chat Relay](/docs/en/chat-relay),"
          " which runs each session under one account. See the next page for that setup.",
          "",
          "Tip: tag `@quillon` in a thread to hand it a task.",
          "",
          para(2, tail="Read the [relay page](https://code.claude.com/docs/en/chat-relay) or the"
               " [vendor notes](https://example.invalid/quillon/tag) for the document history.")]),
        ("Quillon in Chat Relay", "chat-relay",
         "Hand tasks over from a chat workspace. The earlier relay stays the setup path on personal plans.",
         ["<Warning>", "  " + para(3), "</Warning>", "", para(4),
          "", "## Typical uses", "", f"* **Lorem**: {lorem(5)}", f"* **Ipsum**: {lorem(6)}",
          "", "## Before you begin", "", "| Need | Notes |", "| :- | :- |",
          f"| Plan | {lorem(7, 6)} |", f"| Account | {lorem(8, 6)} |",
          "", "## Under the hood", "", "### Intent spotting", "", para(9),
          "", "### Session lifecycle", "", "1. " + lorem(10), "2. " + lorem(11),
          "", "## Fixing problems", "", '### "Quillon is switched off for this seat"', "", para(12),
          "", "### Sessions stay idle", "", para(13),
          "", "## See also", "", "* [Quillon Tag](/docs/en/quillon-tag)",
          "* [Run workers side by side](/docs/en/workers)"]),
        ("Route Quillon through a hub", "hubs",
         "Send Quillon traffic through a self-hosted hub for shared keys and usage totals.",
         [para(14, lead="A hub sits between Quillon and the model vendor."),
          "", "## What a hub does", "", para(15, lead="Each request reaches the hub first."),
          "", "## Pick a hub", "", "### Managed hub", "", para(16),
          "", "### Other hubs", "", "See [Other hubs](/docs/en/hub) and"
          " [Roll out a hub for your crew](/docs/en/hub-rollout).",
          "", "## Where to go next", "", "* " + lorem(17, 8)]),
        ("Roll out a hub for your crew", "hub-rollout",
         "Stand up a hub product for Quillon: forward requests, hand out keys, and check the rollout.",
         [para(18, lead="This guide rolls a hub out in four steps."),
          "", "## Before you begin", "", para(19), "", "### What the hub needs", "",
          "* The hub passes every header Quillon sends.", "* " + lorem(20, 9),
          "", "## Rollout", "", "<Steps>", "  <Step title=\"Check that the hub reaches the vendor\">",
          "", "### Check that the hub reaches the vendor", "",
          "    The hub should already answer at its base address.", "",
          "    ```bash theme={null}", "    curl \"$HUB_URL/v1/ping\" -H \"x-key: <hub-key>\"", "    ```",
          "", "    ```powershell theme={null}", "    Invoke-RestMethod \"$env:HUB_URL/v1/ping\"", "    ```",
          "  </Step>", "</Steps>",
          "", "### Hand out keys", "", para(21, lead="Each person gets a hub key."),
          "", "### Ship the settings", "", "#### Which settings to ship", "",
          "| Setting | Purpose | When |", "| :- | :- | :- |",
          "| `QUILLON_BASE_URL` | Points requests at the hub | Always |",
          "| `keyHelper` | Signs each request to the hub | Always |",
          "", "#### Ship them as managed settings", "", "```json theme={null}",
          "{", "  \"env\": { \"QUILLON_BASE_URL\": \"https://hub.example.invalid\" }", "}", "```",
          "", "### Check the rollout", "", para(22, lead="Ask one person to run a session through the hub."),
          "", "## Keep the hub healthy", "", para(23),
          "", "## See also", "", "* [Route Quillon through a hub](/docs/en/hubs)"]),
        ("Restore file edits with snapshotting", "kit/file-snapshotting",
         "Record file edits during kit sessions and put files back to any earlier snapshot",
         [para(24, lead="File snapshotting lets a kit session restore the files it edited."),
          "", "## Snapshot basics", "", para(25, lead="Each edit made by a tool call records a snapshot."),
          "", "## Turn snapshots on", "", "  ```python Python theme={null}",
          "  # lorem: enable snapshots", "  opts = KitOptions(snapshots=True)",
          "  await kit.restore_files(turn_id)", "  ```", "",
          "  ```typescript TypeScript theme={null}", "  const opts = { snapshots: true };",
          "  await kit.restoreFiles(turnId);", "  ```",
          "", "## Recipes", "", "### Snapshot ahead of bulk edits", "", para(26),
          "", "### Several restore marks", "", para(27, lead="Restore to any earlier snapshot."),
          "", "## Limits", "", para(28),
          "", "## Fixing problems", "", '### "Snapshot missing for turn" error', "",
          para(29, lead="The restore target has no snapshot."),
          "", "## Where to go next", "", "* [Snapshotting](/docs/en/snapshotting)"]),
        ("Samples", "kit/samples",
         "Find a sample kit project or a walkthrough close to what you want to make.",
         [para(30), "", "## Start with a tiny kit", "", para(31),
          "", "## Browse a showcase app", "", para(32)]),
        ("Quillon Desktop in a sandbox VM", "desktop-vm",
         "Run sessions inside a sandbox VM on the desktop app",
         [para(33), "", "## Needs", "", "* " + lorem(34, 7),
          "", "## Open a sandbox session", "", para(35)]),
        ("Week 21 · Lorem 18–22, 2001", "news/2001-w21",
         "Quick mode on more plans, a usage breakdown, and smaller fixes.",
         ["<Update label=\"Week 21\">", "  <div>" + lorem(36, 10) + "</div>",
          "  <div>The add-on pane lists every command, widgets included, before you add it</div>",
          "", "  ```bash terminal theme={null}", "  quillon addon list", "  ```",
          "  <div>" + lorem(37, 10) + "</div>", "</Update>"]),
        ("Other hubs", "hub",
         "Use a hub your crew already runs with Quillon, and what it has to pass along.",
         [para(38, lead="Quillon can use a hub you already operate."),
          "", "## What a hub gives you", "", para(39),
          "", "## Roll out a hub", "",
          "To roll out a hub across your crew, follow [Roll out a hub for your crew](/docs/en/hub-rollout).",
          para(40),
          "", "## Plans and hubs", "", para(41),
          "", "## See also", "", "* [Route Quillon through a hub](/docs/en/hubs)"]),
        ("Run workers side by side", "workers",
         "Compare how Quillon handles several jobs at once: helpers, worker crews, and pipelines.",
         [para(42, lead="Quillon can run workers side by side in a few ways."),
          "", "## Pick a style", "", "| Style | When |", "| :- | :- |",
          "| Helpers | " + lorem(43, 6) + " |", "| Worker crews | " + lorem(44, 6) + " |",
          "", "## Watch running jobs", "", para(45),
          "", "## Read on", "", "* [Samples](/docs/en/kit/samples)"]),
    ]
    out = []
    for title, slug, desc, body in pages:
        out += code_page(title, slug, desc, body)
    # The last page: its final fence is never closed (a truncated download),
    # so the headings after it are fenced text.
    last = [
        para(46, lead="Quillon records its own edits so you can restore a session."),
        "", "## How snapshots work", "", para(47),
        "", "### Automatic capture", "", para(48, 3, lead="Every prompt takes a snapshot."),
        "", "### Restore and condense", "",
        "Type `/restore`, or tap `Esc` three times, to open the restore list.", "",
        "<Note>", "  With text in the input box, `Esc` wipes that text and the restore list stays shut.", "</Note>",
        "", para(49, 3, lead="Pick a snapshot to restore files, chat, or both."),
        "", "#### Restore past a wiped chat", "",
        "If you wiped the chat earlier, the restore list shows one more row at the top.", "",
        para(50, 3),
        "", "#### Steer a condensation", "", para(51, 3, lead="A condensation stands in for the turns after the snapshot."),
        "", "## Common uses", "", "* **Lorem**: " + lorem(52), "* **Ipsum**: " + lorem(53),
        "* **Dolor**: " + lorem(54),
        "", "## Limits", "", para(55, 3),
        "", "### Shell edits are not captured", "",
        "Snapshotting skips files that shell commands touch, so a restore leaves them as they are:", "",
        "```bash theme={null}", "rm lorem.txt", "mv ipsum.txt dolor.txt", "cp sit.txt amet.txt",
        para(56, 3), para(57, 3), para(58, 3),
        "", "## See also", "", "* [Restore file edits with snapshotting](/docs/en/kit/file-snapshotting)",
        "", "## Related", "", para(59)]
    out += code_page("Snapshotting", "snapshotting",
                     "Record, restore, and condense edits and chat to manage session state.", last)
    write("claude-docs/claude-code-llms-full.txt", out)

    def entry(title, slug, desc):
        return f"- [{title}]({CODE}{slug}.md): {desc}"

    index = [
        "# Quillon Docs", "",
        "> " + lorem(60, 18), "",
        "## First steps", "", "### First steps", "",
        entry("Overview", "overview", "Quillon is a made-up coding helper that reads, edits, and runs things."),
        entry("Quickstart", "quickstart", "Say hello to Quillon."),
        entry("Quillon changelog", "changelog", "Version-by-version notes for Quillon."),
        "", "### Places Quillon runs", "",
        entry("Quillon in Chat Relay", "chat-relay", pages[1][2]),
        entry("Quillon Tag", "quillon-tag", pages[0][2]),
        "", "#### Quillon on the desktop", "",
        entry("Quillon Desktop in a sandbox VM", "desktop-vm", pages[6][2]),
        "", "### Workers", "",
        entry("Run workers side by side", "workers", pages[9][2]),
        "", "### Hubs", "",
        entry("Route Quillon through a hub", "hubs", pages[2][2]),
        "", "#### Other hubs", "",
        entry("Other hubs", "hub", pages[8][2]),
        entry("Roll out a hub for your crew", "hub-rollout", pages[3][2]),
        "", "### Sessions", "",
        entry("Snapshotting", "snapshotting",
              "Record, restore, and condense edits and chat to manage session state."),
        "", "## Reference", "",
        entry("Widgets reference", "widgets", "Reference for widget events, their settings, and exit codes."),
        "", "## Kit", "",
        entry("Samples", "kit/samples", pages[5][2]),
        entry("Steer kit sessions with widgets", "kit/widgets",
              "Step into a kit session at fixed points with widgets"),
        entry("Restore file edits with snapshotting", "kit/file-snapshotting", pages[4][2]),
        "", "## News", "",
        entry("Week 21 · Lorem 18–22, 2001", "news/2001-w21", pages[7][2]),
        "",
    ]
    write("claude-docs/claude-code-llms.txt", index)


# ---------------------------------------------------------------------------
# claude-platform (frontmatter)
# ---------------------------------------------------------------------------

PLAT = "https://platform.claude.com/docs/en/"


def plat_page(title: str, slug: str, desc: str | None, body: list, nav: tuple = ()) -> list:
    fm = ["---", f"title: {title}", f"url: {PLAT}{slug}"]
    if desc is not None:
        fm.append(f"description: {desc}")
    tail = []
    for heading in nav:  # navigation labels the upstream emits between pages
        tail += [heading, ""]
    return [*fm, "---", "", *body, "", *tail]


def build_claude_platform() -> None:
    blob_example = ["## Example", "", "```bash", "curl https://api.example.invalid/v1/blobs/$BLOB_ID/body \\",
                    "    -H 'vantry-version: 2001-01-01' \\", "    -H \"X-Token: $VANTRY_TOKEN\"", "```"]
    pages = [
        ("Get your Vantry access token", "get-access-token",
         "Find, mint, and rotate your Vantry access tokens in the console.",
         [para(61, lead="An access token signs every request."),
          "", "## Pick a token kind", "", para(62),
          "", "## Mint an access token", "", "1. " + lorem(63), "2. " + lorem(64),
          "", "## Send your access token", "", "```bash", "export VANTRY_TOKEN='<your-token>'", "```",
          "", "## Tokens and the admin side", "", para(65)],
         ("### Building on Vantry",)),
        ("Memory spans", "build/memory-spans",
         "See how the memory span fills up and how to keep long chats inside it.",
         [para(66), "", "## How a span fills", "", para(67),
          "", "## Span hints", "", "### Hint format", "", "```xml", "<span:lorem>1000</span:lorem>", "```",
          "", "# lorem: an unfenced sample comment line", "sample_call(lorem=1)",
          "", "## Trim spans by folding", "",
          "See [Folding overview](https://platform.claude.com/docs/en/build/folding).",
          "", "## Where to go next", "", "* " + lorem(68, 8)]),
        ("Folding in the background", "build/folding-background",
         "Ask for a fold while the chat goes on, then swap it in once it lands.",
         [para(69), "", "## Supported setups", "", para(70),
          "", "## Swapping while the chat goes on", "", para(71, 3),
          "", "## Asking for a background fold", "", para(72, 3)]),
        ("Folding overview", "build/folding",
         "What folding does and which page fits your task.",
         [para(73, lead="Folding swaps older turns for a short digest."),
          "", "## Pick a folding mode", "", "| | On request | At a limit |", "| - | - | - |",
          "| **Who triggers it** | " + lorem(74, 5) + " | " + lorem(75, 5) + " |",
          "", "## Folding on request", "",
          "* [Folding in the background](https://platform.claude.com/docs/en/build/folding-background):"
          " " + lorem(76, 8),
          "* [Folding on request](https://platform.claude.com/docs/en/build/folding-on-request): "
          + lorem(77, 8)],
         ("## Good habits",)),
        ("Guides to sample tasks", "about/task-guides/overview",
         '"Walkthroughs for sample tasks: sorting, help desks, and review queues."',
         [para(78), "", "## Picking a path", "", para(79)]),
        ("Prompt shaping overview", "build/prompt-shaping/overview",
         "When shaping a prompt is the right tool.",
         [para(80), "", "## Before you shape", "", para(81), "", "## Shaping steps", "", para(82)]),
        ("Glossary", "about/glossary",
         "Terms used across Vantry. This page is a short list of them.",
         [para(83), "", "## Span", "", para(84),
          "", "## Lag", "", para(85),
          "", "Note: a term may appear on more than one page of this document.", "",
          "## Warmth", "", para(86), "", "Tip: lower warmth gives steadier output.",
          "", "## Units", "", para(87)],
         ("## Models & prices", "### Models")),
        ("Migration notes", "about/models/migration-notes",
         "Notes for moving to newer Vantry models",
         [para(88), "", "## Asking for help", "", para(89)]),
        ("IP addresses", "api/ip-addresses",
         "Vantry answers from fixed address ranges in both directions.",
         [para(90), "", "## Inbound IP addresses", "", para(91), "", "### Range A", "", "`192.0.2.0/24`",
          "", "### Range B", "", "`2001:db8::/32`",
          "", "## Outbound ranges", "", "### Range C", "", "`198.51.100.0/24`",
          "", "### Retired ranges", "", "```text wrap", "203.0.113.0/24", "```"]),
        ("Releases", "api/releases",
         '"Every request carries a `vantry-version` header. For example: `vantry-version: 2001-01-01`."',
         [para(92), "", "## Release log", "", "* `2001-01-01`: " + lorem(93, 8)],
         ("### Vantry CLI",)),
        ("Fetch Blob", "api/blobs/fetch", None,
         ["# Fetch Blob", "", para(94), "", "## Path fields", "", "* `blob_id`: " + lorem(95, 6),
          "", "## Header fields", "", "* `vantry-version`: " + lorem(96, 6), "", *blob_example]),
        ("Fetch Blob", "api/beta/blobs/fetch", None,
         ["# Fetch Blob", "", para(97), "", "## Path fields", "", "* `blob_id`: " + lorem(98, 6),
          "", "## Header fields", "", "* `vantry-beta`: " + lorem(99, 6), "* `vantry-version`: " + lorem(100, 6),
          "", *blob_example]),
    ]
    out = ["# Vantry Developer Docs - Full Text", "",
           "This file holds the full rendered text.", "",
           "## Root URL", "", "Vantry Console (sign-in needed)", "", "https://platform.claude.com", "",
           "## Languages on the site", "",
           "- English (en) - 12 pages - /docs - Text below",
           "- Lorem (lo) - 4 pages - /docs/lo - Site only", "",
           "---", "", "# English Docs - Full Text", "", "## Start here", ""]
    for p in pages:
        out += plat_page(*p)
    write("claude-docs/claude-platform-llms-full.txt", out)

    def entry(title, slug, desc=None):
        return f"- [{title}]({PLAT}{slug}.md)" + (f" - {desc}" if desc else "")

    index = [
        "# Vantry Developer Docs", "", "This file gives an overview of the Vantry docs.", "",
        "## Root URL", "", "https://platform.claude.com", "",
        "## Languages on the site", "",
        "- English (en) - 12 pages - /docs - Text below",
        "- Lorem (lo) - 4 pages - /docs/lo - Site only", "",
        "---", "", "## English", "", "### Start here", "",
        entry("Documentation", "home"),
        "", "### Messages", "",
        entry("Folding", "build/folding"),
        entry("Memory spans", "build/memory-spans"),
        entry("Get your access token", "get-access-token", "Get your Vantry access token"),
        "", "### Good habits", "",
        entry("Overview", "about/task-guides/overview", "Guides to sample tasks"),
        entry("Overview", "build/prompt-shaping/overview", "Prompt shaping overview"),
        entry("Glossary", "about/glossary"),
        "", "### Models & prices", "",
        entry("Move between model releases", "about/models/migration-notes", "Migration notes"),
        "", "### API reference", "",
        entry("IP addresses", "api/ip-addresses"),
        entry("Releases", "api/releases"),
        "", "### API Reference", "",
        entry("Fetch Blob", "api/blobs/fetch"),
        entry("Fetch Blob (Beta)", "api/beta/blobs/fetch"),
        entry("Releases (Beta)", "api/beta/crews/releases"),
        entry("Releases (Beta)", "api/beta/kits/releases"),
        "",
    ]
    write("claude-docs/claude-platform-llms.txt", index)


# ---------------------------------------------------------------------------
# ai-sdk (frontmatter, quoted url)
# ---------------------------------------------------------------------------

AS = "https://ai-sdk.dev/"
_AS_FOOTER = ["---", "", "Sitemap of every page: [/sitemap.md](/sitemap.md)", "",
              "Page list: [/llms.txt](/llms.txt)", ""]


def as_page(title: str, desc: str, path: str, body: list, tags: tuple = ()) -> list:
    fm = ["---", f"title: {title}", f"description: {desc}", f'url: "{AS}{path}"', "docs_index: /llms.txt"]
    if tags:
        fm += ["tags:", *(f"  - {t}" for t in tags)]
    return [*fm, "---", "", "> Page list: [/llms.txt](/llms.txt).", "",
            *body, "", *_AS_FOOTER]


def build_ai_sdk() -> None:
    pages = [
        ("Troubleshooting", "Fixes for common Loomkit problems.", "docs/fixes",
         [para(101)]),
        ("Migrate Loomkit 3.3 to 3.4", "Move a project from Loomkit 3.3 to 3.4.",
         "docs/upgrades/upgrade-3-4",
         ["Read the [Loomkit 3.4 release", "notes](https://example.invalid/loomkit-3-4) for more.", "",
          "Nothing breaks in this release.", "", para(102, lead="Partial results now stream to the client.")]),
        ("Experimental_StdioLoomTransport", "Make a transport for Loom clients over standard input and output",
         "docs/reference/loomkit-core/loom-stdio-transport",
         [para(103), "", "## Install", "", "## API Signature", "", "### Arguments", "", "* `command`: " + lorem(104, 6)]),
        ("useCompletion", "Reference for the useCompletion helper in Loomkit.",
         "docs/reference/loomkit-ui/completion-helper",
         [para(105, lead="Streams text completions into your UI."), "", "## Install", "",
          "## API Signature", "", "### Generics", "", "### Arguments", "", "* `api`: " + lorem(106, 6),
          "", "### Result", "", "* `completion`: " + lorem(107, 6)]),
        ("Weave Text", "Weave text with Loomkit on Node", "recipes/node/weave-text",
         ["The simplest case is weaving text from a prompt.",
          "Call `weaveText` with a prompt to get woven text back.", "",
          "```ts title='index.ts'", "import { weaveText } from 'loomkit';", "",
          "const result = await weaveText({ prompt: 'lorem ipsum' });", "console.log(result.text);", "```"],
         ("node",)),
        ("Embed Phrase", "Embed a phrase with Loomkit on Node", "recipes/node/embed-phrase",
         [para(108, lead="An embedding turns a phrase into a vector."), "",
          "```ts", "import { embed } from 'loomkit';", "",
          "const { embedding } = await embed({ value: 'lorem ipsum' });", "console.log(embedding);", "```"],
         ("node", "embedding")),
        ('"TypeScript error \\"Cannot find module \'LoomJSX\'\\""', "Fixes for TypeScript errors around JSX in Loomkit.",
         "docs/fixes/typescript-cannot-find-loomjsx",
         ["## Issue", "", "The build stops with `Cannot find module 'LoomJSX'`.", "",
          "## Background", "", para(109), "", "## Solution", "", "Add the JSX types to your config:", "",
          "```json", '{ "compilerOptions": { "types": ["lorem-jsx"] } }', "```"]),
        ('"Loom is not assignable to type \\"LoomModelV1\\""', "Fixing a type error with older loom models.",
         "docs/fixes/loom-type-mismatch",
         ["## Issue", "", "The build stops with `Loom is not assignable to type \"LoomModelV1\"`.", "",
          "Note: this shows up after an upgrade. See the [upgrade document](/docs/upgrades/upgrade-3-4).",
          "", "Tip: pin both packages to the same release.", "",
          "## Background", "", para(110), "", "## Solution", "", para(111)]),
        ("Stream Shape", "Stream a shape with Loomkit on Node", "recipes/node/stream-shape",
         [para(112, lead="Weaving a big shape can take a while."), "",
          "You can use `weaveText` with `Output.shape` to stream partial shapes.", "",
          "```ts title='index.ts'", "import { weaveText, Output } from 'loomkit';", "",
          "const { partialOutputStream } = weaveText({ output: Output.shape({}) });",
          "for await (const part of partialOutputStream) console.log(part);", "```"],
         ("node", "streaming")),
    ]
    out = ["# Loomkit docs", "", "> " + lorem(113, 10), "", "## When to reach for Loomkit", "",
           para(114), "", "## Docs", "", "- [First steps](/docs/first-steps)", ""]
    for title, desc, path, body, *tags in pages:
        out += as_page(title, desc, path, body, tags[0] if tags else ())
    write("ai-sdk/ai-sdk-llms-full.txt", out)


# ---------------------------------------------------------------------------
# firebase (llms.txt index + per-page cache)
# ---------------------------------------------------------------------------

FB = "https://firebase.google.com/"


def fb_cache_name(url: str) -> str:
    """Same naming as ``parse-firebase.py`` (``_url_to_cache_filename``)."""
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    base = url[len(FB):]
    base = base[:-len(".md.txt")] if base.endswith(".md.txt") else base
    return f"{base.replace('/', '_')[:180]}-{h}.md.txt"


def build_firebase() -> None:
    sdk = "The Zentor web kit holds the client code for apps built on Zentor."
    admin = "The Zentor server kit lets your backend talk to Zentor."
    # (title, path, description, cached page body or None). The list order
    # is the index order: [1] is uncached (fb-content-dead), [14] is the
    # arcade page (fb-sections / fb-content-heading), [17] the labels page.
    entries = [
        ("Documentation", "docs", "Developer docs for Zentor",
         ["## Zentor developer docs", "", para(115),
          "", "### [Kits that help you MAKE things](http://firebase.google.com/docs/make)", "",
          "- " + lorem(116, 8), "- Make and deploy small web apps with no fuss.",
          "- " + lorem(117, 8), "", "## Docs and learning", "", "### Guides", "", para(118)]),
        ("LoomImageParams interface", "docs/reference/js/zt.loomimageparams", sdk, None),
        ("LoomSafetySettings interface", "docs/reference/js/zt.loomsafetysettings", sdk, None),
        ("WholeNumberShape class", "docs/reference/js/zt.wholenumbershape", sdk, None),
        ("LoomExpected interface", "docs/reference/js/zt.loomexpected", sdk, None),
        ("LoomMessage interface", "docs/reference/js/zt.loommessage", sdk, None),
        ("LoomMessageContent interface", "docs/reference/js/zt.loommessagecontent", sdk, None),
        ("LoomPromptOptions interface", "docs/reference/js/zt.loompromptoptions", sdk, None),
        ("Link a Zentor project", "docs/studio/zentor-projects",
         "How to link a studio workspace to a Zentor project.",
         [para(119), "", "## What a Zentor project is", "", para(120, lead="A project groups your apps."),
          "", "## Link a studio app to a Zentor project", "", "### Link a Zentor project for me",
          "", para(121), "", "### Link a Zentor project by hand", "", para(122),
          "", "## Where to go next", "", "* " + lorem(123, 7)]),
        ("Start with the sketching helper", "docs/studio/sketch-helper", "How to use the studio helper.", None),
        ("Push your app to a code host", "docs/studio/code-host", "How to publish a studio app.", None),
        ("Project Lumen joins Zentor Studio", "docs/studio/lumen-joins-zentor-studio",
         "Project Lumen now lives inside Zentor Studio.",
         [para(124), "", "## What happens to my Project Lumen workspaces and projects?", "", para(125),
          "", "## Can I use Zentor Studio on its own?", "", para(126)]),
        ("Make an import button for Zentor Studio", "docs/studio/import-button",
         "How to add an &#39;Open in Zentor Studio&#39; button.", None),
        ("Link Loom servers", "docs/studio/loom-servers", "How to link the studio to Loom servers.", None),
        ("Use Zentor with your Arcade Hub project", "docs/android/integrate-arcade-hub",
         "How to use Zentor with an Arcade Hub project.",
         [para(127, lead="Zentor works with your Arcade Hub project."),
          "", "## Get started", "", "> [!NOTE]", "> Keep in mind:",
          "> - If you already linked an arcade project, reuse it when you get started.", "",
          "1. " + lorem(128), "2. [Link your Zentor app to your", "   arcade account](https://example.invalid/link).",
          "   Your Arcade Hub project reuses that link.", "",
          "## Track arcade events with Zentor Stats", "",
          "After you add the stats kit, Arcade Hub events start to flow.", "", para(129),
          "", "## Fixing frequent problems", "",
          "### Arcade Hub events missing from the dashboard", "", para(130),
          "", "### The arcade console will not link to Zentor", "", para(131),
          "", "### The Zentor console will not open from the arcade console", "", para(132)]),
        ("Zentor on Android in depth", "docs/android/in-depth", "Core Zentor ideas on Android.", None),
        ("Get ready for Arcade&#39;s data disclosure rules", "docs/android/arcade-data-disclosure",
         "How to fill in Arcade&#39;s data disclosure form.", None),
        ("Ship labels", "docs/cli/labels", "Developer docs for Zentor",
         ["***Ship labels*** are short names you pick for the resources in a Zentor project.", "",
          "Ship labels help when you run", "[several sites](https://firebase.google.com/docs/hosting/sites),",
          "or several buckets. " + lorem(133, 8), "",
          "> [!NOTE]", "> **Note:** The CLI applies ship labels to the **project in use right now**.", "",
          "Note: ship labels live in the project, not in this document.", "",
          "Tip: name labels after what they serve.", "",
          "## Add ship labels to your Zentor resources", "", para(134),
          "", "### Add ship labels for sites", "", "```", "zentor label:apply site blog lorem-blog", "```",
          "", "## Point zentor.json at your ship labels", "",
          "```", '{ "site": [{ "label": "blog" }] }', "```",
          "", "## Edit ship labels", "", para(135), "", "## Ship one labelled resource", "",
          "```", "zentor ship --only site:blog", "```"]),
        ("Zentor Messaging", "docs/messaging", "Zentor Messaging sends messages across platforms.", None),
        ("Insights for message campaigns", "docs/messaging/insights", "Insights for message campaigns.", None),
        ("Pick", "docs/datastore/flows/steps/reshape/pick",
         "The pick step in a data flow.",
         ["## Description", "", para(136), "", "## Examples", "", "### Web", "", "```javascript",
          "const rows = flow.pick('lorem', 'ipsum');", "```", "", "##### Swift", "", "```swift",
          "let rows = flow.pick(\"lorem\")", "```", "", "## Behavior", "", "### Record meta fields", "",
          para(137)]),
        ("Merge", "docs/datastore/flows/steps/reshape/merge", "The merge step.", None),
        ("Flatten", "docs/datastore/flows/steps/reshape/flatten", "The flatten step.", None),
        ("ZentorProjectError", "docs/reference/server/java/com/zentor/projects/ZentorProjectError", admin,
         ["# ZentorProjectError", "", "public class ZentorProjectError", "",
          "### Inherited members", "", para(138)]),
        ("PearApp", "docs/reference/server/java/com/zentor/projects/PearApp", admin, None),
        ("SealPrint", "docs/reference/server/java/com/zentor/projects/SealPrint", admin, None),
        ("Good habits for Zentor projects", "docs/projects/workflows/good-habits",
         "High-level habits for laying out Zentor projects.",
         [para(139), "", "## How Zentor projects nest", "", para(140),
          "", "### Where does a parent project sit?", "", para(141),
          "Note that every app in one project shares its resources.", "",
          "## App variants and Zentor projects", "", para(142),
          "", "## Where to go next", "", "* " + lorem(143, 6)]),
        ("Safety habits per stage", "docs/projects/workflows/safety-habits", "Safety habits per stage.", None),
        ("The stages at a glance", "docs/projects/workflows/stages", "A look at the usual stages.", None),
        ("ZentorProjectFault class", "docs/reference/server/node/zentor-server.projects.zentorprojectfault", admin,
         [para(144), "", "## Constructors", "", "| Constructor | Description |", "| --- | --- |",
          "| (constructor)(code, message) | " + lorem(145, 5) + " |", "",
          "## ZentorProjectFault.(constructor)", "", "### Parameters", "", para(146)]),
        ("PearAppMeta interface", "docs/reference/server/node/zentor-server.projects.pearappmeta", admin, None),
        ("SealPrint class", "docs/reference/server/node/zentor-server.projects.sealprint", admin, None),
    ]
    index = ["# Documentation", "", "Developer docs for Zentor", "", "## Docs", ""]
    pages_dir = OUT / "firebase" / "firebase-docs"
    if pages_dir.exists():
        shutil.rmtree(pages_dir)
    pages_dir.mkdir(parents=True)
    for title, path, desc, body in entries:
        url = f"{FB}{path}.md.txt"
        index.append(f"- [{title}]({url}): {desc}")
        if body is not None:
            write(f"firebase/firebase-docs/{fb_cache_name(url)}", body)
    index.append("")
    write("firebase/firebase-llms.txt", index)


def main() -> None:
    for sub in ("claude-docs", "ai-sdk"):
        d = OUT / sub
        if d.exists():
            shutil.rmtree(d)
    build_claude_code()
    build_claude_platform()
    build_ai_sdk()
    build_firebase()
    print(f"fixtures written under {OUT}")


if __name__ == "__main__":
    main()
