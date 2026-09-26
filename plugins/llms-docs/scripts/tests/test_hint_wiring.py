"""Static guard: every ``next_hint()`` call propagates ``corpus_hint_args``.

``next_hint()`` prints a copy-pasteable ``Next: ...`` follow-up command. When
the current invocation selected a non-default corpus (``--file`` /
``--cache-dir`` / ``--max-age``), a hint that omits those flags re-resolves
against the *default* corpus, so the same numeric page index can land on a
different document (see ``_common.corpus_hint_args``' docstring).

This is a wiring obligation spread over every ``cmd_*`` in all three
``parse-*.py`` scripts, and forgetting it produces no error — just a subtly
wrong hint. An internal-backlog audit found nine such call sites across two of
the three scripts, sitting next to already-wired siblings in the same file, so
per-command output assertions clearly do not cover the class.

Rather than assert on rendered output for every subcommand (which needs a
fixture and a fetch path per command), this checks the source tree: parse each
script with ``ast`` and require that each ``next_hint(...)`` call passes a
starred argument that carries ``corpus_hint_args(args)``. The check is
deliberately structural so a *newly added* call site fails here at test time
instead of shipping a hint pointed at the wrong corpus.

``corpus_hint_args`` was kept an explicit argument at each call site (rather
than folded into ``next_hint``'s own signature) because the two scripts that
need ``_source_hint_args`` compose the tuples locally, and because a required
``args`` parameter would turn a cosmetic omission into a ``TypeError`` at
runtime in any command path a test does not exercise.
"""

import ast
import unittest
from pathlib import Path

import _loader  # noqa: F401  (side effect: adds scripts/ to sys.path)

SCRIPTS_DIR = Path(_loader.SCRIPTS_DIR)

# Expected number of next_hint() call sites per script. Kept as a floor, not
# an exact count: a new subcommand may legitimately add one, and the point of
# this file is that the new one gets checked. The floor only guards against
# the AST walk silently matching nothing (a vacuously passing test) if
# next_hint is ever renamed or the call shape changes.
EXPECTED_MIN_CALLS = {
    "parse-claude-docs.py": 7,
    "parse-ai-sdk.py": 5,
    "parse-firebase.py": 5,
    "parse-llms-txt.py": 6,
}

HINT_FUNC = "next_hint"
REQUIRED_HELPER = "corpus_hint_args"

# The shared command layer (``_commands.py``, 2wd.25 Phase 0) prints the
# ``Next:`` hints on the scripts' behalf. A call to one of its renderers is a
# hint site too: it must pass ``hint_args=`` carrying ``corpus_hint_args``.
# ``_commands.py`` itself is checked separately — every hint it prints must
# forward the ``hint_args`` parameter it was given, never build its own.
COMMANDS_MODULE = "_commands.py"
RENDER_PREFIX = "render_"
HINT_PARAM = "hint_args"


def _called_names(node: ast.AST) -> set:
    """Names of every function called anywhere inside *node*."""
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _names_carrying_helper(scope: ast.AST) -> set:
    """Local names in *scope* assigned from an expression calling the helper.

    Lets a call site hoist the tuple into a variable
    (``hint_args = corpus_hint_args(args)`` ... ``next_hint(..., *hint_args)``)
    without tripping the guard — ``cmd_content`` already does this for the
    truncation hint, so the pattern is expected to spread.
    """
    carriers = set()
    for sub in ast.walk(scope):
        if isinstance(sub, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if sub.value is None or REQUIRED_HELPER not in _called_names(sub.value):
                continue
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    carriers.add(target.id)
    return carriers


def _is_render_call(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Name) and call.func.id.startswith(RENDER_PREFIX)


def _hint_calls(scope: ast.AST) -> list:
    return [
        sub for sub in ast.walk(scope)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and (sub.func.id == HINT_FUNC or _is_render_call(sub))
    ]


def _carries_helper(value: ast.AST, carriers: set) -> bool:
    if REQUIRED_HELPER in _called_names(value):
        return True
    return isinstance(value, ast.Name) and value.id in carriers


def _call_is_wired(call: ast.Call, carriers: set) -> bool:
    if _is_render_call(call):
        return any(
            kw.arg == HINT_PARAM and _carries_helper(kw.value, carriers)
            for kw in call.keywords
        )
    for arg in call.args:
        if not isinstance(arg, ast.Starred):
            continue
        if REQUIRED_HELPER in _called_names(arg.value):
            return True
        if isinstance(arg.value, ast.Name) and arg.value.id in carriers:
            return True
    return False


class NextHintCorpusArgsWiringTest(unittest.TestCase):
    def _check_script(self, filename: str):
        path = SCRIPTS_DIR / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        all_calls = _hint_calls(tree)
        self.assertGreaterEqual(
            len(all_calls), EXPECTED_MIN_CALLS[filename],
            f"{filename}: found only {len(all_calls)} {HINT_FUNC}() call sites "
            f"(expected at least {EXPECTED_MIN_CALLS[filename]}). If the hint "
            f"helper was renamed, update this guard — do not lower the floor "
            f"to make it pass.",
        )

        # Every call must live inside a function (that is where an ``args``
        # namespace exists). Attribute calls to their enclosing scope so a
        # hoisted ``hint_args`` variable is visible.
        checked = set()
        unwired = []
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            carriers = _names_carrying_helper(scope)
            for call in _hint_calls(scope):
                checked.add(id(call))
                if not _call_is_wired(call, carriers):
                    unwired.append(f"{filename}:{call.lineno} (in {scope.name}())")

        self.assertEqual(
            unwired, [],
            f"{HINT_FUNC}() call sites missing *{REQUIRED_HELPER}(args): "
            f"{unwired}. A follow-up hint that drops --file/--cache-dir/"
            f"--max-age re-resolves against the default corpus, so the same "
            f"page index can point at a different document.",
        )

        orphans = [
            f"{filename}:{call.lineno}"
            for call in all_calls if id(call) not in checked
        ]
        self.assertEqual(
            orphans, [],
            f"{HINT_FUNC}() called outside any function ({orphans}); such a "
            f"call has no ``args`` to propagate and cannot be checked here.",
        )

    def test_generic_hints_propagate_corpus_args(self):
        self._check_script("parse-llms-txt.py")

    def test_generic_hints_propagate_source(self):
        # parse-llms-txt.py has no default source: a hint without --source
        # (and a non-default --sources-file) is a command that fails outright.
        path = SCRIPTS_DIR / "parse-llms-txt.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        missing = []
        for call in _hint_calls(tree):
            values = [a.value for a in call.args if isinstance(a, ast.Starred)]
            values += [kw.value for kw in call.keywords if kw.arg == HINT_PARAM]
            if not any("_source_hint_args" in _called_names(v) for v in values):
                missing.append(call.lineno)
        self.assertEqual(missing, [], f"parse-llms-txt.py hint calls without _source_hint_args: lines {missing}")

    def test_claude_docs_hints_propagate_corpus_args(self):
        self._check_script("parse-claude-docs.py")

    def test_ai_sdk_hints_propagate_corpus_args(self):
        self._check_script("parse-ai-sdk.py")

    def test_firebase_hints_propagate_corpus_args(self):
        self._check_script("parse-firebase.py")

    def test_shared_renderers_forward_their_hint_args(self):
        """``_commands.py`` never builds corpus flags; it forwards ``hint_args``.

        Every ``render_*`` must take a ``hint_args`` parameter, and every
        ``next_hint`` / ``print_subsection_hints`` call inside it must pass
        that parameter through (``*hint_args`` / ``extra_hint_args=hint_args``).
        Otherwise the scripts' wiring checked above would be lost one level down.
        """
        path = SCRIPTS_DIR / COMMANDS_MODULE
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        renderers = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith(RENDER_PREFIX)
        ]
        self.assertGreaterEqual(len(renderers), 2, "no render_* found — guard would be vacuous")
        problems = []
        forwarded = 0
        for fn in renderers:
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            if HINT_PARAM not in params:
                problems.append(f"{fn.name}() has no {HINT_PARAM} parameter")
                continue
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                if call.func.id == HINT_FUNC:
                    ok = any(
                        isinstance(a, ast.Starred) and isinstance(a.value, ast.Name)
                        and a.value.id == HINT_PARAM
                        for a in call.args
                    )
                elif call.func.id == "print_subsection_hints":
                    ok = any(
                        kw.arg == "extra_hint_args" and isinstance(kw.value, ast.Name)
                        and kw.value.id == HINT_PARAM
                        for kw in call.keywords
                    )
                else:
                    continue
                forwarded += 1
                if not ok:
                    problems.append(f"{COMMANDS_MODULE}:{call.lineno} ({fn.name}) drops {HINT_PARAM}")
        self.assertEqual(problems, [])
        self.assertGreater(forwarded, 0, "no hint call found in render_* — guard would be vacuous")

    def test_every_parse_script_is_covered(self):
        """The guard must not silently skip a newly added parse-*.py."""
        found = sorted(p.name for p in SCRIPTS_DIR.glob("parse-*.py"))
        self.assertEqual(found, sorted(EXPECTED_MIN_CALLS))


if __name__ == "__main__":
    unittest.main()
