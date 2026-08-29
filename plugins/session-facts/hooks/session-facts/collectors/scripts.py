from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from collectors.dependencies import is_python_dependency_declared
from core.constants import (
    COMPOSE_FILE_CANDIDATES,
    MAKE_TARGET_PRIORITY_PATTERNS,
    MAX_SCRIPT_COMMAND_CHARS,
    SCRIPT_PRIORITY_PATTERNS,
)
from core.context import RepoContext
from core.fs import read_text
from core.makefile import extract_targets
from core.runtime import mise_config_path, runner_prefix
from core.util import collapse_space, truncate_text


class ScriptsCollector:
    name = "scripts"
    section_title = "## Scripts"
    priority = 30

    def should_run(self, ctx: RepoContext) -> bool:
        scripts = ctx.package_json.get("scripts")
        return isinstance(scripts, dict) and len(scripts) > 0

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.config.max_script_entries
        scripts = _collect_scripts(ctx, max_items)
        if not scripts:
            return None
        lines = [self.section_title]
        for item in scripts:
            lines.append(f"- {item['name']}: {item['command']}")
        return "\n".join(lines)


class LikelyCommandsCollector:
    name = "likely_commands"
    section_title = "## Likely Commands"
    priority = 90

    def should_run(self, ctx: RepoContext) -> bool:
        return True

    def collect(self, ctx: RepoContext) -> Optional[str]:
        max_items = ctx.config.max_script_entries
        commands = _likely_commands(ctx, max_items)
        if not commands:
            return None
        lines = [self.section_title]
        for cmd in commands:
            lines.append(f"- {cmd}")
        return "\n".join(lines)


def _collect_scripts(ctx: RepoContext, max_items: int) -> List[Dict[str, str]]:
    scripts = ctx.package_json.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        return []
    scored: List[Tuple[int, str, str]] = []
    for name, command in scripts.items():
        score = 100
        for idx, pattern in enumerate(SCRIPT_PRIORITY_PATTERNS):
            if re.search(pattern, name):
                score = idx
                break
        command_text = truncate_text(collapse_space(str(command)), MAX_SCRIPT_COMMAND_CHARS)
        scored.append((score, str(name), command_text))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [{"name": name, "command": command} for _score, name, command in scored[:max_items]]


def _detect_package_manager(ctx: RepoContext) -> Optional[str]:
    return ctx.results.get("package_manager")


def _runner_prefix(ctx: RepoContext) -> Optional[str]:
    """Local-tool command prefix from the detected runtime, or None.

    venv wins (``.venv/bin/``); otherwise a mise-managed Python yields
    ``mise exec -- ``. uv/poetry manage their own environments and are left
    untouched by callers.
    """
    return runner_prefix(ctx.results.get("runtime") or {})


def _has_compose_file(root: Path) -> bool:
    return any((root / f).exists() for f in COMPOSE_FILE_CANDIDATES)


# pytest の既定 ``python_files`` (``test_*.py`` / ``*_test.py``) だけを見る。
#
# 既知の限界: ``python_files = check_*.py`` のように既定を上書きしている
# プロジェクトでは、収集可能なファイルをここが弾いて pytest の提案が出なく
# なる。設定を読むには pytest 設定 (pyproject.toml / pytest.ini / tox.ini /
# setup.cfg の 4 系統) のパーサが要り、このモジュールの責務を超えるため
# 意図的に既定のみとした。**外し方は「提案を出さない」側**で、本 collector が
# 避けたい失敗 (根拠の無いコマンドを出す) の反対方向にあたる。
_PYTEST_STYLE_NAME_RE = re.compile(r"^(test_.+|.+_test)\.py$")

# A base-class list containing a literal "TestCase" (``unittest.TestCase``,
# a bare ``TestCase`` import, or a project's own ``...TestCase`` helper
# base) -- the shape unittest's TestLoader can actually collect. Matches
# multi-line class headers too (``[^)]`` includes newlines).
_UNITTEST_CLASS_RE = re.compile(r"class\s+\w+\s*\([^)]*TestCase\b")

# `discover` が実際に収集するのは `TestCase` サブクラスの `test*` メソッド。
# 基底クラスの宣言だけでは 0 件になりうる (例: ヘルパー基底
# `class BaseTestCase(unittest.TestCase): pass` しか無いモジュール) ため、
# メソッド定義の存在も併せて要求する。既定の prefix は `test` (`test_` に
# 限らない)。
_UNITTEST_METHOD_RE = re.compile(r"^\s+(?:async\s+)?def\s+test\w*\s*\(", re.M)


def _looks_like_unittest_test(text: str) -> bool:
    """True when ``text`` defines at least one class whose base-class list
    contains a literal ``TestCase`` **and** at least one ``test*`` method.

    The class header alone is not enough: a module holding only an empty
    helper base (``class BaseTestCase(unittest.TestCase): pass``) is
    collected as "Ran 0 tests", so suggesting the command would be
    unfounded.

    A module of bare ``def test_x():`` functions (pytest's convention, not
    unittest's) is *not* discoverable by ``unittest discover``: its
    TestLoader walks ``TestCase`` subclasses, never bare module-level
    functions (verified empirically: a ``tests/test_a.py`` containing only
    ``def test_a(): assert True`` yields "Ran 0 tests" under
    ``python3 -m unittest discover tests``). This is a regex on the class
    header, not a full parse, so it will not follow indirect inheritance
    through an import alias -- a false negative there only costs a missed
    (safe) suggestion, never a wrong one.
    """
    return bool(_UNITTEST_CLASS_RE.search(text)) and bool(
        _UNITTEST_METHOD_RE.search(text)
    )


def _has_root_unittest_tests(ctx: RepoContext) -> bool:
    """True when a root-level ``tests/`` directory contains at least one
    ``test_*.py`` file that ``python3 -m unittest discover tests`` can both
    import and actually collect a test from.

    Three positive conditions, each verified empirically against a real
    interpreter, must hold together:

    1. The file's own name matches ``test_*.py`` (discover's default
       ``-p``/``--pattern``). A file living under ``spec/``/``e2e/`` or
       nested inside a subproject (e.g. ``hooks/x/tests/``) never reaches
       this scan at all -- its first path segment is not ``tests``.
    2. It is importable from the top-level ``tests/`` directory: either it
       sits directly under ``tests/`` (the start directory itself is only
       walked, never imported -- a direct child needs no ``__init__.py``
       anywhere), or every directory strictly between ``tests/`` and the
       file has its own tracked ``__init__.py`` (each intermediate package
       needs one, but ``tests/`` itself still does not -- verified across
       one- and two-level nesting). A ``test_*.py`` sitting in a non-package
       subdirectory of ``tests/`` (no ``__init__.py`` there) does not
       satisfy this -- discover reports "Ran 0 tests" for it.
    3. Its content defines a ``unittest.TestCase`` subclass (see
       :func:`_looks_like_unittest_test`) -- a module of bare pytest-style
       functions satisfies 1 and 2 but is collected as zero tests by
       unittest's TestLoader.
    """
    tracked = set(ctx.tracked_files)
    for path_str in ctx.tracked_files:
        parts = Path(path_str).parts
        if len(parts) < 2 or parts[0] != "tests":
            continue
        name = parts[-1]
        # `discover` の既定パターンは `test*.py` (`python3 -m unittest
        # discover -h` に明記)。`test_` 始まりに限定すると `testmath.py` の
        # ように実際には収集される名前を取りこぼし、収集可能なのに提案が
        # 出ない方向に外す。
        if not (name.startswith("test") and name.endswith(".py")):
            continue
        if len(parts) > 2:
            # Every directory strictly between "tests" (parts[0], excluded)
            # and the file (parts[-1], excluded) must itself be a tracked
            # package for `discover` to import it.
            importable = all(
                "/".join(parts[: depth + 1] + ("__init__.py",)) in tracked
                for depth in range(1, len(parts) - 1)
            )
            if not importable:
                continue
        if _looks_like_unittest_test(read_text(ctx.root / path_str)):
            return True
    return False


def _has_python_test_files(ctx: RepoContext) -> bool:
    """True when at least one tracked Python file matches pytest's own
    default collection name pattern (``test_*.py`` / ``*_test.py`` --
    pytest's ``python_files`` default), independent of directory.

    Deliberately filename-based rather than reused from either signal
    already in this module:

    - ``core.util.is_test_path()`` (via ``test_snapshot['test_files']``) is
      directory-marker based -- any file sitting under a dir named
      tests/spec/e2e/... counts, including a bare ``conftest.py`` or
      ``__init__.py`` that defines no test at all, which would make an
      otherwise test-less ``tests/`` scaffold look like "has Python tests".
    - ``test_snapshot['test_files']`` is also summed across *every*
      language via ``CODE_EXTENSIONS``, so a TypeScript-only test suite in
      a mixed-language repo leaves that count non-zero with zero Python
      test files (isolated-review P2-a, PR #67 round 2).
    """
    for path_str in ctx.tracked_files:
        p = Path(path_str)
        if p.suffix.lower() == ".py" and _PYTEST_STYLE_NAME_RE.match(p.name):
            return True
    return False


def _test_tool_name(ctx: RepoContext) -> Optional[str]:
    """'pytest' when python_stack.py detected it as a pyproject.toml
    dependency, OR when collectors/dependencies.py's own parsers find it
    declared in requirements*.txt / Pipfile / setup.cfg (python_stack.py's
    stack entry only ever comes from a pyproject.toml substring match, so a
    project declaring pytest solely via one of those other files would
    otherwise be invisible here) -- AND ONLY when at least one tracked file
    actually looks like a Python test module (see
    :func:`_has_python_test_files`; a mixed-language repo can have
    ``test_snapshot['test_files'] > 0`` from other languages alone while
    having zero Python tests, in which case a bare pytest dependency must
    not be enough to suggest ``pytest``). The literal ``unittest discover
    tests`` invocation when neither holds but a root-level ``tests/``
    directory grounds it (see :func:`_has_root_unittest_tests`); ``None``
    when there is no test_files signal at all, or when nothing grounds
    either command -- callers must suggest nothing rather than guess a
    command that would fail.
    """
    test_snapshot = ctx.results.get("test_snapshot") or {}
    if not test_snapshot.get("test_files"):
        return None
    if _has_python_test_files(ctx) and (
        "pytest" in ctx.stack or is_python_dependency_declared(ctx, "pytest")
    ):
        return "pytest"
    if _has_root_unittest_tests(ctx):
        return "unittest discover tests"
    return None


def _pm_run_test_command(ctx: RepoContext, run_prefix: str) -> Optional[str]:
    """uv/poetry style: ``<run_prefix>pytest`` or
    ``<run_prefix>python -m unittest discover tests``."""
    tool = _test_tool_name(ctx)
    if tool is None:
        return None
    if tool == "pytest":
        return f"{run_prefix}pytest"
    return f"{run_prefix}python -m {tool}"


def _module_test_command(ctx: RepoContext, run_prefix: Optional[str]) -> Optional[str]:
    """python-pm / bare-python style: ``<run_prefix>python -m pytest`` or
    ``<run_prefix>python -m unittest discover tests``. With no known runner,
    falls back to a bare ``python -m pytest`` (pre-existing behaviour) or
    ``python3 -m unittest discover tests`` (matches the ticket's literal
    wording, and this plugin's own SKILL.md/hooks.json convention of naming
    the interpreter explicitly as ``python3``).
    """
    tool = _test_tool_name(ctx)
    if tool is None:
        return None
    if run_prefix:
        return f"{run_prefix}python -m {tool}"
    if tool == "pytest":
        return "python -m pytest"
    return f"python3 -m {tool}"


def _make_commands(ctx: RepoContext, max_items: int) -> List[str]:
    """Surface conventional Makefile targets as ``make <target>`` commands.

    Targets are scored by MAKE_TARGET_PRIORITY_PATTERNS; targets matching no
    pattern are dropped so internal helper targets do not crowd the output.
    Falls back to a bare ``make`` when no Makefile is readable or none of its
    targets are conventional entry points.
    """
    text = ""
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = ctx.root / name
        if path.exists():
            text = read_text(path)
            break
    if not text:
        return ["make"]

    scored: List[Tuple[int, str]] = []
    for target in extract_targets(text):
        for idx, pattern in enumerate(MAKE_TARGET_PRIORITY_PATTERNS):
            if re.search(pattern, target):
                scored.append((idx, target))
                break
    scored.sort(key=lambda item: (item[0], item[1]))
    commands = [f"make {target}" for _idx, target in scored[:max_items]]
    return commands or ["make"]


def _likely_commands(ctx: RepoContext, max_items: int) -> List[str]:
    scripts = _collect_scripts(ctx, max_items=50)
    pm = _detect_package_manager(ctx)
    stack = set(ctx.stack)
    commands: List[str] = []

    # PM-based commands
    prefix = {
        "pnpm": "pnpm",
        "npm": "npm run",
        "yarn": "yarn",
        "bun": "bun run",
    }.get(pm or "")
    if prefix:
        for item in scripts:
            commands.append(f"{prefix} {item['name']}")
    elif pm == "deno":
        commands.extend(["deno task dev", "deno test"])
    elif pm == "uv":
        cmd = _pm_run_test_command(ctx, "uv run ")
        if cmd:
            commands.append(cmd)
    elif pm == "poetry":
        cmd = _pm_run_test_command(ctx, "poetry run ")
        if cmd:
            commands.append(cmd)
    elif pm == "python":
        cmd = _module_test_command(ctx, _runner_prefix(ctx))
        if cmd:
            commands.append(cmd)
    elif pm == "gradle":
        commands.extend(["./gradlew build", "./gradlew test"])
    elif pm == "maven":
        commands.extend(["mvn test", "mvn package"])
    elif pm == "go":
        commands.extend(["go test ./...", "go build ./..."])
    elif pm == "cargo":
        commands.extend(["cargo test", "cargo build"])
    elif pm == "composer":
        commands.append("composer install")

    # Bare-Python repos (.py-heavy, no PM lockfile/pyproject) get no pytest line
    # from the chain above. Only surface one when a concrete runner (venv/mise)
    # is known, so we never suggest a global ``python`` the repo may not use.
    if pm is None and "python" in stack:
        run_prefix = _runner_prefix(ctx)
        if run_prefix:
            cmd = _module_test_command(ctx, run_prefix)
            if cmd:
                commands.append(cmd)

    # Flutter/Dart toolchain
    if "flutter" in stack:
        commands.extend(["flutter pub get", "flutter run", "flutter test"])
    elif "dart" in stack:
        commands.extend(["dart pub get", "dart test"])

    # Stack-based additions (task runners, tools)
    if "makefile" in stack:
        commands.extend(_make_commands(ctx, max_items))
    if "justfile" in stack:
        commands.append("just")
    if "taskfile" in stack:
        commands.append("task")
    if "nx" in stack:
        commands.append("nx run-many --target=build")
    if "mise" in stack:
        # has_mise() (core/runtime.py) also recognises a bare .tool-versions
        # (asdf-style) file as "mise"-compatible, but `mise install` only
        # actually applies when a real mise config exists.
        if mise_config_path(ctx.root) is not None:
            commands.append("mise install")
        elif (ctx.root / ".tool-versions").exists():
            commands.append("asdf install")
    if "docker" in stack:
        # detectors/docker.py fires "docker" on a bare Dockerfile too, but
        # `docker compose up` only applies when a compose file is present.
        if _has_compose_file(ctx.root):
            commands.append("docker compose up")
        else:
            commands.append("docker build .")

    deduped: List[str] = []
    seen: Set[str] = set()
    for cmd in commands:
        if cmd not in seen:
            seen.add(cmd)
            deduped.append(cmd)
    return deduped[:max_items]


def register():
    return [ScriptsCollector(), LikelyCommandsCollector()]
