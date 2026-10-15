"""The shims at the retired script names (``parse-claude-docs.py`` /
``parse-ai-sdk.py`` / ``parse-firebase.py``).

Each one runs ``parse-llms-txt.py`` with the bundled preset that reads the
same docs and the same cache files, so a command written for an old script
keeps working. Only the arguments are translated; the exit code, stdout and
stderr are the generic loader's. ``os.execv`` replaces the process, so the
generic loader's ``sys.argv[0]`` is its own path and the ``Next:`` lines it
prints name ``parse-llms-txt.py`` (a command that can be run as printed).

Translation (``convert``):

* ``--source`` (``parse-claude-docs.py`` only): ``code`` / ``platform`` /
  ``both`` become the presets ``claude-code`` / ``claude-platform`` /
  ``claude`` (the alias that ``search`` runs on both). Any other value is
  passed on as a source name. Without ``--source``, ``--source claude-code``
  (the old default) is added; the other two scripts get ``--source ai-sdk``
  / ``--source firebase``
* ``--index-limit`` (the old name of ``search --top-n``) becomes ``--top-n``
* an option the old script accepted in an abbreviated form (``--sour both``,
  ``--ind 3``) is written in full: the generic loader has more options, so
  an abbreviation unique in the old script can be ambiguous there
  (``--sour`` is also ``--sources-file``)
* anything after ``--`` is passed on untouched (a query that starts with
  ``-``), and so is an argument list that does not start with one of the
  old script's subcommands (``-h``, a typo: the generic loader answers)
"""

import os
import sys

GENERIC_SCRIPT = "parse-llms-txt.py"

# The long options each old script's subcommands took, and whether they take
# a value. Used to expand an abbreviation the old script accepted and to
# skip an option's value when scanning; every name here is also an option of
# the generic loader's subcommand of the same name (``test_legacy_shims``),
# except ``--index-limit`` (translated) and claude-docs' ``--source`` values.
_CACHE = {"--cache-dir": True, "--max-age": True}
_SEARCH_BODY = {"--limit": True, "--context": True, "--max-hits": True, "--max-snippet-chars": True}
OLD_OPTIONS = {
    "claude-docs": {
        "fetch-index": {"--source": True, **_CACHE},
        "sections": {"--file": True, "--source": True, **_CACHE},
        "content": {"--file": True, "--source": True, **_CACHE, "--max-chars": True,
                    "--no-subsection-hints": False, "--no-link-annotations": False},
        "search-index": {"--source": True, **_CACHE, "--limit": True},
        "search-content": {"--page-ref": True, "--file": True, "--source": True, **_CACHE,
                           **_SEARCH_BODY, "--include-changelog-priority": False},
        "search": {"--source": True, "--file": True, **_CACHE, "--top-n": True, "--index-limit": True,
                   "--max-hits": True, "--context": True, "--max-snippet-chars": True,
                   "--include-changelog-priority": False},
    },
    "ai-sdk": {
        "fetch-index": {"--file": True, **_CACHE, "--compact": False},
        "search-index": {"--file": True, **_CACHE, "--limit": True, "--show-sections": False},
        "search-content": {"--page-ref": True, "--file": True, **_CACHE, **_SEARCH_BODY,
                           "--include-changelog-priority": False},
        "sections": {"--file": True, **_CACHE},
        "content": {"--file": True, **_CACHE, "--max-chars": True, "--no-subsection-hints": False},
        "search": {"--file": True, **_CACHE, "--top-n": True, "--max-hits": True, "--context": True,
                   "--max-snippet-chars": True, "--include-changelog-priority": False},
    },
    "firebase": {
        "fetch-index": {**_CACHE, "--limit": True, "--offset": True},
        "sections": {**_CACHE},
        "content": {**_CACHE, "--max-chars": True, "--no-subsection-hints": False},
        "search-index": {**_CACHE, "--limit": True},
        "search-content": {**_CACHE, "--page-ref": True, **_SEARCH_BODY},
        "search": {**_CACHE, "--top-n": True, "--max-hits": True, "--context": True,
                   "--max-snippet-chars": True, "--include-changelog-priority": False},
    },
}

# claude-docs' ``--source`` values -> preset names
CLAUDE_SOURCES = {"code": "claude-code", "platform": "claude-platform", "both": "claude"}
DEFAULT_SOURCE = {"claude-docs": "claude-code", "ai-sdk": "ai-sdk", "firebase": "firebase"}
RENAMED = {"--index-limit": "--top-n"}


def _expand(name: str, options: dict) -> str | None:
    """The old option *name* stands for: itself, or the one option it is a
    prefix of (argparse's abbreviation). ``None`` when it is none or several."""
    if name in options:
        return name
    found = [o for o in options if o.startswith(name)]
    return found[0] if len(found) == 1 else None


def convert(kind: str, argv: list[str]) -> list[str]:
    """The generic loader's arguments for the old script *kind*'s *argv*
    (without the script name)."""
    commands = OLD_OPTIONS[kind]
    if not argv or argv[0] not in commands:
        return list(argv)
    options = commands[argv[0]]
    out = [argv[0]]
    has_source = False
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            out += argv[i:]
            break
        name, eq, value = tok.partition("=")
        full = _expand(name, options) if tok.startswith("--") and len(name) > 2 else None
        if full is None:
            out.append(tok)
            i += 1
            continue
        if not options[full]:
            out.append(full + eq + value)  # a value given to a flag: the generic loader rejects it
            i += 1
            continue
        if not eq:
            if i + 1 >= len(argv):  # no value: the generic loader reports it
                out.append(full)
                break
            value = argv[i + 1]
            i += 1
        i += 1
        if full == "--source":
            has_source = True
            value = CLAUDE_SOURCES.get(value, value)
        full = RENAMED.get(full, full)
        out += [f"{full}={value}"] if eq else [full, value]
    if not has_source:
        out[1:1] = ["--source", DEFAULT_SOURCE[kind]]
    return out


def run(kind: str) -> None:
    """Replace this process with the generic loader for the old script *kind*."""
    script = os.path.join(os.path.dirname(os.path.realpath(__file__)), GENERIC_SCRIPT)
    argv = [sys.executable, script, *convert(kind, sys.argv[1:])]
    if os.name == "nt":  # no exec there: run it and pass its exit code on
        import subprocess
        sys.exit(subprocess.call(argv))
    os.execv(sys.executable, argv)
