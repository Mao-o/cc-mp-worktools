"""hook の経路 (core/・services/) に、止まらない読み (core.cli_config) を迂回したファイルの読みが無い (v0.36.4)。

対象が書き手のいない FIFO だと、普通に開く読みは戻らず、hook が時間切れで打ち切られて検証が飛ぶ。
0.35.0 で 2 か所、0.36.3 で 3 か所が、人の grep の棚卸しで見つかった。docs/DEVELOPMENT.md の「hook の経路の
読みは cli_config を通す」を、ここで機械的に守る。

AST で `core/` と `services/` の全 `.py` を走査し、ファイルを開く呼び出し (`open` / `os.open` /
`os.fdopen` / `io.open` / `.open(` / `.read_text(` / `.read_bytes(`) を数える。受け手が `cli_config` の呼び出し
(`cli_config.read_text(...)` など) は通す側なので数えない。例外は `_ALLOWED` の表だけで、
(相対パス, 外側の関数名) ごとに回数まで固定する (許可した関数に足された新しい読みを素通しにしないため)。
表に実在しない項目や、回数の合わない項目があれば落ちる。

走査そのものも `TestScanner` で確かめる (走査が空振りしていても本体のテストは green のままなので)。
"""
from __future__ import annotations

import ast
import collections
import unittest
from pathlib import Path

import _testutil  # noqa: F401  (sys.path を通す)

_PKG_DIR = Path(__file__).resolve().parent.parent
_SUBDIRS = ("core", "services")

# 名前の綴り (Name の呼び出し `open(...)` と、Attribute の呼び出し `x.open(...)` の末尾) で拾う。
# `import os as o` の `o.open` や `from os import fdopen` の `fdopen(...)` も、綴りだけで拾える。
_OPENERS = frozenset({"open", "fdopen", "read_text", "read_bytes"})

# 例外の表: (相対パス, 外側の関数名) -> (回数, 理由)。回数はその関数の中の、数える呼び出しの総数。
_ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("core/cli_config.py", "_read_without_blocking"): (
        2,
        "止まらない読みの本体。os.open (O_NONBLOCK) と、fstat で種類を確かめた後の os.fdopen",
    ),
    ("core/paths.py", "_inspect_dot_git"): (
        1,
        ".git ファイルの open。is_file() で通常のファイルと確かめてから開く (docs/DEVELOPMENT.md)",
    ),
    ("services/firebase.py", "_physical_dir"): (
        1,
        "ディレクトリを O_NONBLOCK で開く os.open (中身は読まない)",
    ),
    ("core/unverified.py", "claim"): (
        1,
        "知らせの記録を O_WRONLY | O_CREAT | O_EXCL で作る os.open (書き込みで、読まない)",
    ),
}

_MODULE = "<module>"


def scan_source(source: str, rel: str = "x.py") -> list[tuple[str, int, str]]:
    """(外側の関数名, 行, 綴り) の一覧。cli_config.<名前>(...) は通す側なので数えない。"""
    found: list[tuple[str, int, str]] = []

    def is_cli_config(node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id == "cli_config") or (
            isinstance(node, ast.Attribute) and node.attr == "cli_config"
        )

    def walk(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Name) and f.id in _OPENERS:
                    found.append((func, child.lineno, f.id))
                elif isinstance(f, ast.Attribute) and f.attr in _OPENERS and not is_cli_config(f.value):
                    found.append((func, child.lineno, f.attr))
            walk(child, inner)

    walk(ast.parse(source, filename=rel), _MODULE)
    return found


def _scan_package() -> dict[tuple[str, str], list[int]]:
    """(相対パス, 外側の関数名) -> 行の一覧。"""
    result: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    for sub in _SUBDIRS:
        for path in sorted((_PKG_DIR / sub).rglob("*.py")):
            rel = path.relative_to(_PKG_DIR).as_posix()
            for func, lineno, _name in scan_source(path.read_text(encoding="utf-8"), rel):
                result[(rel, func)].append(lineno)
    return result


def excess(found: dict, table: dict) -> list:
    """表の回数を超えた (表に無い関数は 0 回) 読み。"""
    out = []
    for key, lines in sorted(found.items()):
        allowed = table.get(key, (0, ""))[0]
        if len(lines) > allowed:
            out.append((key, f"行 {lines} (許可 {allowed} 回、実際 {len(lines)} 回)"))
    return out


def stale(found: dict, table: dict) -> list:
    """表の回数と実際の回数が合わない項目 (実在しない項目を含む)。"""
    out = []
    for key, (count, _reason) in sorted(table.items()):
        actual = len(found.get(key, []))
        if actual != count:
            out.append((key, f"表は {count} 回、実際 {actual} 回"))
    return out


class TestHookPathReads(unittest.TestCase):
    """今の core/・services/ に、cli_config を迂回した読みが増えていない。"""

    def test_scan_covers_the_modules(self):
        # 走査が空振り (対象の dir が無い・glob が外れる) していると、以降のテストは何も見ない
        for sub in _SUBDIRS:
            self.assertGreater(len(list((_PKG_DIR / sub).rglob("*.py"))), 3, sub)
        self.assertIn(("core/cli_config.py", "_read_without_blocking"), _scan_package())

    def test_no_read_beyond_the_table(self):
        self.assertEqual(
            excess(_scan_package(), _ALLOWED), [],
            "cli_config の止まらない読み (read_text / read_regular_bytes / read_bounded_text) を通さない、"
            "ファイルを開く呼び出しがある。FIFO で止まり hook が時間切れになる。本当に例外にしてよいなら "
            "_ALLOWED に理由つきで足し、docs/DEVELOPMENT.md の例外の記述も揃える",
        )

    def test_table_has_no_stale_entry(self):
        self.assertEqual(
            stale(_scan_package(), _ALLOWED), [], "表の項目が、実在しないか回数が合わない。実際の読みに揃える"
        )

    def test_table_entries_have_reasons(self):
        for key, (count, reason) in _ALLOWED.items():
            self.assertGreater(count, 0, key)
            self.assertTrue(reason.strip(), key)


class TestComparison(unittest.TestCase):
    """表との突き合わせ (今の tree は clean なので、本体のテストだけでは比較の向きを固定できない)。"""

    TABLE = {("core/a.py", "f"): (2, "理由")}

    def test_excess(self):
        ok = {("core/a.py", "f"): [1, 2]}
        self.assertEqual(excess(ok, self.TABLE), [])
        self.assertEqual([k for k, _ in excess({("core/a.py", "f"): [1, 2, 3]}, self.TABLE)], [("core/a.py", "f")])
        # 表に無い関数は 1 回でも超過
        self.assertEqual([k for k, _ in excess({("core/b.py", "g"): [5]}, self.TABLE)], [("core/b.py", "g")])
        # 同じ関数でも別ファイルは別の項目
        self.assertEqual(len(excess({("services/a.py", "f"): [1]}, self.TABLE)), 1)

    def test_stale(self):
        self.assertEqual(stale({("core/a.py", "f"): [1, 2]}, self.TABLE), [])
        self.assertEqual(len(stale({("core/a.py", "f"): [1]}, self.TABLE)), 1)  # 少ない
        self.assertEqual(len(stale({("core/a.py", "f"): [1, 2, 3]}, self.TABLE)), 1)  # 多い
        self.assertEqual(len(stale({}, self.TABLE)), 1)  # 実在しない


class TestScanner(unittest.TestCase):
    """走査が、迂回した読みの綴りを拾い、通す側を数えない (走査の空振りを許さない)。"""

    def _names(self, source: str) -> list[str]:
        return [name for _func, _line, name in scan_source(source)]

    def test_detects_each_opening_form(self):
        cases = {
            "open(p)": "open",
            "os.open(p, os.O_RDONLY)": "open",
            "io.open(p)": "open",
            "os.fdopen(fd)": "fdopen",
            "fdopen(fd)": "fdopen",
            "p.open('rb')": "open",
            "Path(p).read_text()": "read_text",
            "Path(p).read_bytes()": "read_bytes",
            "o.open(p)": "open",
            "paths.cli_config_dir().read_text()": "read_text",
        }
        for expr, name in cases.items():
            with self.subTest(expr=expr):
                self.assertEqual(self._names(f"def f():\n    return {expr}\n"), [name])

    def test_cli_config_receiver_is_not_counted(self):
        for expr in (
            "cli_config.read_text(p)",
            "cli_config.read_regular_bytes(p)",
            "core.cli_config.read_text(p)",
            "cli_config.read_bounded_text(p)",
        ):
            with self.subTest(expr=expr):
                self.assertEqual(self._names(f"def f():\n    return {expr}\n"), [])

    def test_other_receiver_named_like_cli_config_is_counted(self):
        self.assertEqual(self._names("def f():\n    return my_cli_config.read_text()\n"), ["read_text"])

    def test_non_reads_are_not_counted(self):
        self.assertEqual(self._names("def f():\n    x = p.is_file()\n    return handle.read(10)\n"), [])

    def test_outer_function_name_and_count(self):
        source = (
            "def a():\n    open(p)\n    open(q)\n"
            "def b():\n    def inner():\n        open(r)\n    return inner\n"
            "class C:\n    def m(self):\n        p.read_text()\n"
            "open(top)\n"
        )
        found = scan_source(source)
        self.assertEqual(len(found), 5)
        counts = collections.Counter(func for func, _line, _name in found)
        self.assertEqual(counts, {"a": 2, "inner": 1, "m": 1, _MODULE: 1})

    def test_async_function_name_is_the_outer_name(self):
        found = scan_source("def a():\n    pass\nasync def b():\n    open(p)\n")
        self.assertEqual([func for func, _l, _n in found], ["b"])

    def test_call_in_default_or_nested_expression_is_found(self):
        found = scan_source("def f(x=open(p)):\n    pass\nvalue = [open(q) for _ in range(2)]\n")
        self.assertEqual(len(found), 2)


if __name__ == "__main__":
    unittest.main()
