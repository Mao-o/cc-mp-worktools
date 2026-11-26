"""同じ beads の DB に届く登録 (br の git worktree と本体・symlink 越しの .beads・大文字小文字だけ違うパス) を 1 つの
プロジェクトとして出し、1 本の書き込みのロックで書くことのテスト (registry.project_groups・humanq.Store)。

    python3 -m unittest discover -s tests

- 組の判定は、手で作った git の形 (.git のファイル・gitdir・commondir) と、本物の git の worktree の両方で確かめる。
  製品のコードは git を打たない。テストが打つ git は外側の GIT_* (GIT_DIR など) を外す
- 読み書きは偽の br。偽の br の worktree の解決は本物の br 0.7.6 に合わせてあり、tests/test_cli_contract.py の
  BrQuirks が偽物と本物 (env HUMANQ_TEST_BR) に同じ assertion で当てる
- 同時の回答は、回答のコメントを書く CLI の手前で 2 本を待ち合わせる。ロックが別々なら 2 本とも待ち合わせを抜けて
  両方が書く。1 本なら先の 1 本が時間切れまで待って書き、後の 1 本は書く前にロックを待つ (待ち合わせは壊れる)
- 登録のファイルは前の版 (0.15.2) が書いた形のまま読む (まとめるのは読むときだけ)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_server as ts  # noqa: E402  helper だけ借りる (TestCase は名前空間に入れない)

registry = ts.registry
humanq = ts.humanq

BR_GITIGNORE = ".br_history/\n*-fsqlite-ns-gate\n"          # br init が .beads/.gitignore に書く印 (humanq が br と読む)
BR_META = {"database": "beads.db", "jsonl_export": "issues.jsonl"}
BD_META = {"database": "dolt", "backend": "dolt", "dolt_mode": "embedded"}
WAIT = 1.5                       # 待ち合わせの時間切れ (秒)。1 本ずつ書くとき、先の 1 本はこれだけ待ってから書く


def write(path: str, body) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def br_beads(root: str, *files: str, meta: dict | None = None) -> str:
    """root/.beads を br の形 (.gitignore の印と metadata.json) で作る。files は空のファイルで足す (beads.db など)。"""
    write(os.path.join(root, ".beads", ".gitignore"), BR_GITIGNORE)
    write(os.path.join(root, ".beads", "metadata.json"), meta or BR_META)
    for f in files:
        write(os.path.join(root, ".beads", f), "")
    return root


def bd_beads(root: str) -> str:
    write(os.path.join(root, ".beads", "metadata.json"), BD_META)
    os.makedirs(os.path.join(root, ".beads", "embeddeddolt"), exist_ok=True)
    return root


def main_repo(root: str) -> str:
    """本体の worktree (root/.git が dir)。"""
    os.makedirs(os.path.join(root, ".git", "worktrees"), exist_ok=True)
    return root


def linked(main: str, wt: str, *, relative: bool = False, commondir: str | None = "../..", admin: bool = True) -> str:
    """git worktree add が作る形を手で作る: wt/.git (gitdir: <main>/.git/worktrees/<名前>) と、その管理 dir の commondir。
    relative は gitdir を wt からの相対で書く (git の worktree.useRelativePaths)。admin=False は管理 dir を作らない
    (git worktree prune の後・管理 dir を消した後)。"""
    name = os.path.basename(wt)
    gitdir = os.path.join(main, ".git", "worktrees", name)
    if admin:
        os.makedirs(gitdir, exist_ok=True)
        if commondir is not None:
            write(os.path.join(gitdir, "commondir"), commondir + "\n")
    os.makedirs(wt, exist_ok=True)
    write(os.path.join(wt, ".git"), "gitdir: " + (os.path.relpath(gitdir, wt) if relative else gitdir) + "\n")
    return wt


def git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "BEADS_", "BD_", "BR_", "FAKE_BEADS_"))}
    return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                           "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True, timeout=60, env=env)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-samedb-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sd = os.path.join(self.tmp, "state")
        os.makedirs(self.sd)
        os.environ.pop(registry.CLI_ENV, None)

    def p(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def groups(self) -> list[dict]:
        try:
            return registry.project_groups(self.sd)
        except Exception as e:      # noqa: BLE001  例外が漏れる変異は ERROR ではなく FAIL に数える
            self.fail(f"project_groups が例外を上げた: {type(e).__name__}: {e}")

    def assertOneProject(self, root: str, members: list[str]):
        g = self.groups()
        self.assertEqual(len(g), 1, f"同じ DB に届く登録が 1 つにまとまっていない: {g}")
        self.assertEqual(g[0]["root"], root, g)
        self.assertEqual(g[0]["members"], sorted(members), g)
        self.assertEqual(registry.live_projects(self.sd), [root])
        for m in [root, *members]:
            self.assertEqual(registry.resolve_project(self.sd, m), root, m)

    def assertSeparate(self, *roots: str):
        g = self.groups()
        self.assertEqual(sorted(x["root"] for x in g), sorted(roots), f"別の DB を 1 つにまとめた: {g}")
        for r in roots:
            self.assertEqual(registry.resolve_project(self.sd, r), r, r)


# ---- 組の判定 (CLI は打たない) --------------------------------------------------------------------------------------

class WorktreeGroups(Base):
    def br_pair(self, **kw) -> tuple[str, str]:
        main = br_beads(main_repo(self.p("main")), "beads.db")
        wt = br_beads(linked(main, self.p("wt"), **kw))           # worktree の .beads は追跡するファイルだけ (DB が無い)
        return main, wt

    def test_worktree_and_main_are_one_project(self):
        main, wt = self.br_pair()
        registry.register(self.sd, main)
        registry.register(self.sd, wt)
        self.assertOneProject(main, [main, wt])

    def test_only_the_worktree_registered_shows_the_main(self):
        """本体のセッションを開いていなくても、worktree の登録は本体のパスで出す (br が読み書きするのは本体の DB)。"""
        main, wt = self.br_pair()
        registry.register(self.sd, wt)
        self.assertOneProject(main, [wt])

    def test_merged_layouts(self):
        """br 0.7.6 が本体の DB を読み書きする形 (tests/test_cli_contract.py と、使い捨ての repo での `br where` の実測)。"""
        def rel():
            return self.br_pair(relative=True)

        def no_admin():                                # 管理 dir を消した後も、gitdir の形から本体を決める
            return self.br_pair(admin=False)

        def no_commondir():                            # commondir のファイルが無い管理 dir
            return self.br_pair(commondir=None)

        def history_only():                            # DB 以外のファイルでは「自分の DB」にならない
            main, wt = self.br_pair()
            os.makedirs(os.path.join(wt, ".beads", ".br_history"))
            write(os.path.join(wt, ".beads", "other.db"), "")       # metadata.json の database ではない名前
            return main, wt

        def main_beads_without_db():                   # 本体の .beads は DB が無くても使う (そこに DB を作る)
            main = br_beads(main_repo(self.p("main")))
            return main, br_beads(linked(main, self.p("wt")))

        def main_beads_symlink():                      # 本体の .beads が、.beads という名前の dir への symlink
            store = br_beads(self.p("store"), "beads.db")
            main = main_repo(self.p("main"))
            os.symlink(os.path.join(store, ".beads"), os.path.join(main, ".beads"))
            return main, br_beads(linked(main, self.p("wt")))

        def nested():                                  # 本体の dir の中に置いた worktree
            main = br_beads(main_repo(self.p("main")), "beads.db")
            return main, br_beads(linked(main, os.path.join(main, "wts", "w1")))

        def main_beads_symlink_underscore():           # 本体の .beads が _beads という名前の dir への symlink (br 0.7.6 は本体の DB を使う)
            store = br_beads(self.p("store"), "beads.db")
            os.rename(os.path.join(store, ".beads"), os.path.join(store, "_beads"))
            main = main_repo(self.p("main"))
            os.symlink(os.path.join(store, "_beads"), os.path.join(main, ".beads"))
            return main, br_beads(linked(main, self.p("wt")))

        for build in (rel, no_admin, no_commondir, history_only, main_beads_without_db, main_beads_symlink,
                      main_beads_symlink_underscore, nested):
            with self.subTest(layout=build.__name__):
                shutil.rmtree(self.tmp)
                os.makedirs(self.sd)
                main, wt = build()
                registry.register(self.sd, main)
                registry.register(self.sd, wt)
                self.assertOneProject(main, [main, wt])

    def test_separate_layouts(self):
        """worktree が自分の DB を使う形・humanq が br と決められない形・linked worktree でない形は、別のプロジェクトのまま。"""
        def own_db():
            main, wt = self.br_pair()
            write(os.path.join(wt, ".beads", "beads.db"), "")
            return main, wt

        def own_named_db():                            # metadata.json の database の名前の DB
            main = br_beads(main_repo(self.p("main")), "beads.db")
            wt = br_beads(linked(main, self.p("wt")), "custom.db", meta={"database": "custom.db"})
            return main, wt

        def redirect():                                # br は redirect に従う (行き先はここでは追わない)
            main, wt = self.br_pair()
            write(os.path.join(wt, ".beads", "redirect"), "../elsewhere/.beads\n")
            return main, wt

        def main_without_beads():
            main = main_repo(self.p("main"))
            return main, br_beads(linked(main, self.p("wt")))

        def main_beads_badly_named_symlink():          # 本物の br は worktree で設定エラーになる
            store = os.path.join(br_beads(self.p("store"), "beads.db"), ".beads")
            other = self.p("store", "not-beads")
            os.rename(store, other)
            main = main_repo(self.p("main"))
            os.symlink(other, os.path.join(main, ".beads"))
            return main, br_beads(linked(main, self.p("wt")))

        def bd_worktree():                             # bd の worktree の解決は測っていない (まとめない)
            main = bd_beads(main_repo(self.p("main")))
            return main, bd_beads(linked(main, self.p("wt")))

        def main_is_bd():
            main = bd_beads(main_repo(self.p("main")))
            return main, br_beads(linked(main, self.p("wt")))

        def submodule():                               # gitdir が modules/<名前> (commondir が無い)
            main = br_beads(main_repo(self.p("main")), "beads.db")
            wt = br_beads(self.p("main", "sub"))
            os.makedirs(os.path.join(main, ".git", "modules", "sub"))
            write(os.path.join(wt, ".git"), "gitdir: ../.git/modules/sub\n")
            return main, wt

        def bare():                                    # bare の repo の worktree (common dir が .git でない。本体の worktree が無い)
            write(self.p("bare", "repo.git", "worktrees", "wt", "commondir"), "../..\n")
            wt = br_beads(self.p("wt"))
            write(os.path.join(wt, ".git"), f"gitdir: {self.p('bare', 'repo.git', 'worktrees', 'wt')}\n")
            return br_beads(self.p("bare"), "beads.db"), wt     # common dir の親の .beads は本体の worktree のものではない

        def wrong_key():                               # gitdir: ではない行 (同じ長さの別の key) は読まない
            main = br_beads(main_repo(self.p("main")), "beads.db")
            wt = br_beads(linked(main, self.p("wt")))
            write(os.path.join(wt, ".git"), "foobar: " + os.path.join(main, ".git", "worktrees", "wt") + "\n")
            return main, wt

        def own_db_without_metadata_name():            # metadata.json に database が無くても、既定の beads.db は自分の DB
            main = br_beads(main_repo(self.p("main")), "beads.db")
            return main, br_beads(linked(main, self.p("wt")), "beads.db", meta={"jsonl_export": "issues.jsonl"})

        def worktree_is_bd():                          # worktree の .beads を humanq が br と読めない
            main = br_beads(main_repo(self.p("main")), "beads.db")
            return main, bd_beads(linked(main, self.p("wt")))

        def garbage_git_file():
            main = br_beads(main_repo(self.p("main")), "beads.db")
            wt = br_beads(self.p("wt"))
            write(os.path.join(wt, ".git"), "hello\n")
            return main, wt

        def empty_gitdir():
            main = br_beads(main_repo(self.p("main")), "beads.db")
            wt = br_beads(self.p("wt"))
            write(os.path.join(wt, ".git"), "gitdir:   \n")
            return main, wt

        def subdir_beads():                            # worktree の下の dir の .beads は、その dir の DB
            main = main_repo(self.p("main"))
            br_beads(os.path.join(main, "sub"), "beads.db")
            linked(main, self.p("wt"))
            return os.path.join(main, "sub"), br_beads(self.p("wt", "sub"))

        def separate_clone():                          # .git が dir の別の clone
            return br_beads(main_repo(self.p("main")), "beads.db"), br_beads(main_repo(self.p("clone")))

        for build in (own_db, own_named_db, own_db_without_metadata_name, redirect, main_without_beads,
                      main_beads_badly_named_symlink, bd_worktree, main_is_bd, worktree_is_bd, submodule, bare, wrong_key,
                      garbage_git_file, empty_gitdir, subdir_beads, separate_clone):
            with self.subTest(layout=build.__name__):
                shutil.rmtree(self.tmp)
                os.makedirs(self.sd)
                main, wt = build()
                for r in (main, wt):
                    if os.path.isdir(os.path.join(r, ".beads")):
                        registry.register(self.sd, r)
                live = [r for r in (main, wt) if os.path.isdir(os.path.join(r, ".beads"))]
                self.assertSeparate(*live)

    def test_explicit_br_does_not_merge_into_a_main_without_beads(self):
        """HUMANQ_BEADS_CLI=br だと、.beads の無い本体も br と読めてしまう。本体に .beads の dir が無ければまとめない。"""
        for shape in ("absent", "file"):
            with self.subTest(main_beads=shape):
                shutil.rmtree(self.tmp)
                os.makedirs(self.sd)
                main = main_repo(self.p("main"))
                if shape == "file":
                    write(os.path.join(main, ".beads"), "")
                wt = br_beads(linked(main, self.p("wt")))
                registry.register(self.sd, wt, "br")
                self.assertEqual(registry.project_kind(main, "br")[0], registry.BR)     # 前提: 種別の判定は止めない
                self.assertSeparate(wt)

    def test_explicit_bd_on_the_worktree_is_not_merged(self):
        """worktree のセッションが HUMANQ_BEADS_CLI=bd で登録した (br の印と食い違う = 決められない) なら、まとめない。"""
        main, wt = self.br_pair()
        registry.register(self.sd, main)
        registry.register(self.sd, wt, "bd")
        self.assertSeparate(main, wt)
        self.assertEqual(registry.project_kind(wt, registry.project_cli(self.sd, wt))[0], "conflict")

    def test_explicit_cli_on_the_main_is_checked_too(self):
        """本体の登録の HUMANQ_BEADS_CLI でも本体を br と読めなければ、まとめない (組の設定は最後に登録した一員のものなので、
        まとめると、本体の登録の順で組のカードと worktree の hook が「読めない」になる)。どちらの順に登録しても、前の版
        (0.16.0: 登録ごとに 1 枚) と同じに、worktree のカードと hook は worktree を br で読み、本体のカードは本体の設定で読む。"""
        cases = [  # (本体の設定, 本体の .beads に br の印があるか, 本体のカードの種別 (前の版と同じ))
            ("bd", True, "conflict"),      # 本体だけの誤設定 (印と食い違う)
            ("br", False, "br"),           # 本体だけ br の明示で、印の無い本体 (auto の worktree の設定では本体を br と読めない)
        ]
        for mcli, marked, main_kind in cases:
            for last in ("wt", "main"):
                with self.subTest(main_cli=mcli, last=last):
                    shutil.rmtree(self.tmp)
                    os.makedirs(self.sd)
                    main = main_repo(self.p("main"))
                    if marked:
                        br_beads(main, "beads.db")
                    else:
                        write(os.path.join(main, ".beads", "metadata.json"), BR_META)
                        write(os.path.join(main, ".beads", "beads.db"), "")
                    wt = br_beads(linked(main, self.p("wt")))
                    t_main, t_wt = (("2026-11-20T00:00:00Z", "2026-11-21T00:00:00Z") if last == "wt"
                                    else ("2026-11-22T00:00:00Z", "2026-11-21T00:00:00Z"))
                    write(os.path.join(self.sd, "projects.json"),
                          {"projects": {main: {"last_seen": t_main, "cli": mcli}, wt: {"last_seen": t_wt, "cli": None}}})
                    self.assertSeparate(main, wt)
                    for r, want in ((wt, registry.BR), (main, main_kind)):
                        self.assertEqual(registry.project_kind(r, registry.project_cli(self.sd, r))[0], want, r)


class PathIdentity(Base):
    def test_symlinked_beads_is_one_project_in_either_order(self):
        for order in ("link-first", "real-first"):
            with self.subTest(order=order):
                shutil.rmtree(self.tmp)
                os.makedirs(self.sd)
                real = br_beads(self.p("real"), "beads.db")
                link = self.p("link")
                os.makedirs(link)
                os.symlink(os.path.join(real, ".beads"), os.path.join(link, ".beads"))
                for r in ((link, real) if order == "link-first" else (real, link)):
                    registry.register(self.sd, r)
                self.assertOneProject(real, [real, link])          # 代表は .beads が symlink でない方

    def test_symlinked_parent_dir_is_one_project(self):
        """登録のパスの親が symlink (hook は realpath で登録するが、前の版・手で書いた登録も同じに読む)。"""
        real = br_beads(self.p("real", "proj"), "beads.db")
        os.symlink(self.p("real"), self.p("alias"))
        registry.register(self.sd, real)
        registry.register(self.sd, self.p("alias", "proj"))
        g = self.groups()
        self.assertEqual(len(g), 1, g)
        self.assertEqual(g[0]["members"], sorted([real, self.p("alias", "proj")]), g)

    def test_case_only_difference_is_one_project(self):
        real = br_beads(self.p("Proj"), "beads.db")
        other = self.p("proj")
        if not os.path.exists(other):
            self.skipTest("大文字小文字を区別する FS (この形は起きない)")
        registry.register(self.sd, real)
        registry.register(self.sd, other)
        g = self.groups()
        self.assertEqual(len(g), 1, g)
        self.assertEqual(g[0]["members"], sorted([real, other]), g)

    def test_different_dirs_with_equal_contents_stay_separate(self):
        """中身が同じでも別の dir (inode が違う) は別のプロジェクト (名前やファイルの一致でまとめない)。"""
        a = br_beads(self.p("a"), "beads.db")
        b = br_beads(self.p("b"), "beads.db")
        registry.register(self.sd, a)
        registry.register(self.sd, b)
        self.assertSeparate(a, b)


class RegistrationFile(Base):
    def entries(self, data: dict) -> None:
        write(os.path.join(self.sd, "projects.json"), {"projects": data})

    def test_old_file_with_both_registrations_reads_as_one(self):
        """0.15.2 までの版が worktree と本体を別々に登録したファイルを、そのまま 1 つのプロジェクトとして読む。書き換えない。
        entry の形は 2 つ: cli の無い {last_seen} (cli を足す前の版) と {last_seen, cli}。"""
        main = br_beads(main_repo(self.p("main")), "beads.db")
        wt = br_beads(linked(main, self.p("wt")))
        self.entries({main: {"last_seen": "2026-11-20T00:00:00Z"},
                      wt: {"last_seen": "2026-11-21T00:00:00Z", "cli": None}})
        before = Path(self.sd, "projects.json").read_bytes()
        self.assertOneProject(main, [main, wt])
        self.assertEqual(Path(self.sd, "projects.json").read_bytes(), before, "読むだけで登録のファイルを書き換えた")

    def test_odd_entries_do_not_break_reading(self):
        main = br_beads(main_repo(self.p("main")), "beads.db")
        wt = br_beads(linked(main, self.p("wt")))
        other = br_beads(self.p("other"), "beads.db")
        self.entries({main: "str", wt: {"cli": 5}, other: {}, self.p("gone"): {"cli": "br"}, "": None})
        g = self.groups()
        self.assertEqual(sorted(x["root"] for x in g), sorted([main, other]), g)
        for x in g:
            self.assertIsNone(x["cli"], x)
        for bad in ("[]", "null", '{"projects": []}', '{"projects": "x"}', "{"):
            with self.subTest(file=bad):
                Path(self.sd, "projects.json").write_text(bad, encoding="utf-8")
                self.assertEqual(self.groups(), [])
                self.assertIsNone(registry.resolve_project(self.sd, main))

    def test_register_keeps_the_entry_format(self):
        main = br_beads(main_repo(self.p("main")), "beads.db")
        wt = br_beads(linked(main, self.p("wt")))
        registry.register(self.sd, wt, "br")
        data = json.loads(Path(self.sd, "projects.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["projects"]), [wt], "worktree の登録を本体のパスに書き換えた (前の版が読めなくなる)")
        self.assertEqual(sorted(data["projects"][wt]), ["cli", "last_seen"])

    def test_group_cli_is_the_latest_registration(self):
        main = br_beads(main_repo(self.p("main")), "beads.db")
        wt = br_beads(linked(main, self.p("wt")))
        cases = [
            ({main: ("2026-11-20T00:00:00Z", "br"), wt: ("2026-11-21T00:00:00Z", None)}, None),
            ({main: ("2026-11-22T00:00:00Z", "br"), wt: ("2026-11-21T00:00:00Z", None)}, "br"),
            ({main: ("2026-11-20T00:00:00Z", None), wt: ("2026-11-21T00:00:00Z", "br")}, "br"),
            ({main: ("2026-11-21T00:00:00Z", None), wt: ("2026-11-21T00:00:00Z", "br")}, "br"),   # 同じ秒はパスの名前順の後ろ
        ]
        for data, want in cases:
            with self.subTest(data=data):
                self.entries({k: {"last_seen": t, "cli": c} for k, (t, c) in data.items()})
                g = self.groups()
                self.assertEqual(len(g), 1, g)
                self.assertEqual(g[0]["cli"], want, g)
                for r in (main, wt):
                    self.assertEqual(registry.project_cli(self.sd, r), want, r)

    def test_project_cli_of_an_unlisted_entry_is_its_own(self):
        """組に無い (.beads が消えた) 登録の設定は、今までどおりその entry から読む。"""
        self.entries({self.p("gone"): {"cli": "br"}})
        self.assertEqual(registry.project_cli(self.sd, self.p("gone")), "br")
        self.assertIsNone(registry.project_cli(self.sd, self.p("none")))


class GitDirs(Base):
    def test_git_main_worktree_and_common_dir(self):
        main = main_repo(self.p("main"))
        wt = linked(main, self.p("wt"))
        rel = linked(main, self.p("rel"), relative=True)
        self.assertEqual(registry.git_main_worktree(wt), main)
        self.assertEqual(registry.git_main_worktree(rel), main)
        self.assertIsNone(registry.git_main_worktree(main))
        self.assertIsNone(registry.git_main_worktree(self.p("nogit")))
        for r in (main, wt, rel):
            self.assertEqual(registry.git_common_dir(r), os.path.join(main, ".git"), r)
        self.assertIsNone(registry.git_common_dir(self.p("nogit")))
        os.makedirs(self.p("odd"))
        write(self.p("odd", ".git"), "not a gitdir line\n")
        self.assertIsNone(registry.git_common_dir(self.p("odd")))
        self.assertIsNone(registry.git_main_worktree(self.p("odd")))
        os.makedirs(self.p("empty"))
        write(self.p("empty", ".git"), "gitdir:   \n")            # 空の gitdir を root そのものと読まない
        self.assertIsNone(registry._git_file_dir(self.p("empty")))
        self.assertEqual(registry._git_file_dir(wt), os.path.join(main, ".git", "worktrees", "wt"))

    def test_real_git_worktrees(self):
        """本物の git が作る形でも同じに読む (絶対パスの gitdir・相対パスの gitdir・bare の repo の worktree (名前が .git でない
        bare と、<dir>/.git に置いた bare))。"""
        if shutil.which("git") is None:
            self.skipTest("git が無い")
        main = self.p("main")
        os.makedirs(main)
        for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "i")):
            p = git(main, *args)
            self.assertEqual(p.returncode, 0, p.stderr)
        p = git(main, "worktree", "add", "-q", self.p("wt"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(registry.git_main_worktree(self.p("wt")), main)
        self.assertEqual(registry.git_common_dir(self.p("wt")), os.path.join(main, ".git"))
        p = git(main, "worktree", "add", "-q", "--relative-paths", self.p("relwt"))
        if p.returncode == 0:                         # --relative-paths は git 2.48 から
            self.assertFalse(Path(self.p("relwt"), ".git").read_text(encoding="utf-8").split(":", 1)[1].strip().startswith("/"))
            self.assertEqual(registry.git_main_worktree(self.p("relwt")), main)
        p = git(self.tmp, "clone", "-q", "--bare", main, self.p("bare.git"))
        self.assertEqual(p.returncode, 0, p.stderr)
        p = git(self.p("bare.git"), "worktree", "add", "-q", self.p("barewt"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIsNone(registry.git_main_worktree(self.p("barewt")), "bare の repo には本体の worktree が無い")
        self.assertEqual(registry.git_common_dir(self.p("barewt")), self.p("bare.git"))
        p = git(self.tmp, "clone", "-q", "--bare", main, self.p("proj", ".git"))      # <dir>/.git に置いた bare の repo
        self.assertEqual(p.returncode, 0, p.stderr)
        p = git(self.p("proj", ".git"), "worktree", "add", "-q", self.p("projwt"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(registry.git_main_worktree(self.p("projwt")), self.p("proj"), "br 0.7.6 と同じに <dir> を本体とする")

    def test_outer_git_env_does_not_change_the_reading(self):
        """製品のコードは git を打たないので、外側の GIT_DIR / GIT_WORK_TREE があっても同じに読む。"""
        main = br_beads(main_repo(self.p("main")), "beads.db")
        wt = br_beads(linked(main, self.p("wt")))
        other = main_repo(self.p("other"))
        registry.register(self.sd, main)
        registry.register(self.sd, wt)
        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(other, ".git"), "GIT_WORK_TREE": other,
                                          "GIT_COMMON_DIR": os.path.join(other, ".git")}):
            self.assertOneProject(main, [main, wt])
            self.assertEqual(registry.git_common_dir(wt), os.path.join(main, ".git"))


class BrokenGitFile(Base):
    """1 件の登録の .git・commondir が壊れていても (os の呼び出しが例外を上げる NUL など)、ほかの登録の読みを止めない。"""

    def build(self, kind: str) -> tuple[str, str]:
        good = br_beads(main_repo(self.p("good")), "beads.db")
        bad = br_beads(self.p("bad"))
        if kind == "gitdir-nul":
            write(os.path.join(bad, ".git"), "gitdir: /x\0y/.git/worktrees/w\n")
        else:                                          # commondir に NUL (.git で終わる形と、終わらない形)
            adm = self.p("m", ".git", "worktrees", "bad")
            write(os.path.join(adm, "commondir"), "a\0b/.git\n" if kind == "commondir-nul" else "a\0b\n")
            write(os.path.join(bad, ".git"), f"gitdir: {adm}\n")
        registry.register(self.sd, good)
        registry.register(self.sd, bad)
        return good, bad

    def call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001  例外が漏れる変異は ERROR ではなく FAIL に数える
            self.fail(f"{fn.__name__} が例外を上げた: {type(e).__name__}: {e}")

    def test_a_broken_git_file_does_not_hide_the_other_projects(self):
        for kind in ("gitdir-nul", "commondir-nul", "commondir-nul-plain"):
            with self.subTest(kind=kind):
                shutil.rmtree(self.tmp)
                os.makedirs(self.sd)
                good, bad = self.build(kind)
                self.assertSeparate(good, bad)

    def test_nul_in_the_git_files_is_not_read(self):
        """NUL の入った gitdir の行は読まない。NUL の入った commondir は読まずに、gitdir の形 (<common>/worktrees/<名前>) から決める。
        書き込みのロックの key (git_common_dir) も例外を上げない。"""
        _, bad = self.build("gitdir-nul")
        for fn in (registry._git_file_dir, registry.git_main_worktree, registry.git_common_dir):
            self.assertIsNone(self.call(fn, bad), fn.__name__)
        for kind in ("commondir-nul", "commondir-nul-plain"):
            with self.subTest(kind=kind):
                shutil.rmtree(self.tmp)
                os.makedirs(self.sd)
                _, bad = self.build(kind)
                self.assertEqual(self.call(registry.git_common_dir, bad), self.p("m", ".git"))
                self.assertEqual(self.call(registry.git_main_worktree, bad), self.p("m"))

    def test_a_read_error_in_one_registration_keeps_it_as_its_own_project(self):
        """1 件の判定で os の呼び出しが例外を上げたら (今後足す読みも含む)、その登録は自分のパスのまま出し、ほかの組は変えない。"""
        main = br_beads(main_repo(self.p("main")), "beads.db")
        wt = br_beads(linked(main, self.p("wt")))
        bad = br_beads(linked(main, self.p("bad")))
        for r in (main, wt, bad):
            registry.register(self.sd, r)
        self.assertOneProject(main, [main, wt, bad])          # 前提: 読めれば 3 件とも本体にまとまる
        real = registry.git_main_worktree
        for exc in (OSError, ValueError):
            with self.subTest(exc=exc.__name__):
                def broken(root, exc=exc):
                    if root == bad:
                        raise exc("壊れた読み")
                    return real(root)
                with mock.patch.object(registry, "git_main_worktree", broken):
                    g = self.groups()
                    self.assertEqual([(x["root"], x["members"]) for x in g], [(bad, [bad]), (main, sorted([main, wt]))], g)
                    self.assertEqual(registry.resolve_project(self.sd, bad), bad)
                    self.assertEqual(registry.resolve_project(self.sd, wt), main)


# ---- 読み書き (偽の br・偽の bd) ---------------------------------------------------------------------------------

class FakeCli(Base):
    @classmethod
    def setUpClass(cls):
        cls.bin_tmp = tempfile.mkdtemp(prefix="humanq-samedb-bin-")
        cls.bin = os.path.join(cls.bin_tmp, "bin")
        os.makedirs(cls.bin)
        for prog in ("bd", "br"):
            ts.install_fake(cls.bin, prog)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.bin_tmp, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.log = self.p("calls.jsonl")
        env = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
                                           "FAKE_BEADS_LOG": self.log, "CLAUDE_PLUGIN_DATA": self.sd})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FAKE_BEADS_FAIL", None)
        self.n = 0

    def cli(self, prog: str, cwd: str, *args: str) -> str:
        argv = [prog, *args] if prog == "br" else [prog, "-C", cwd, *args]
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, f"{prog} {args}: {p.stderr or p.stdout}")
        return p.stdout

    def br_main(self) -> str:
        main = main_repo(self.p("main"))
        self.cli("br", main, "init", "--prefix", "hq")
        write(os.path.join(main, ".beads", ".gitignore"), BR_GITIGNORE)
        return main

    def br_question(self, root: str) -> str:
        self.n += 1
        created = json.loads(self.cli("br", root, "create", f"q{self.n}", "-l", "human", "--json"))
        self.assertIsInstance(created, dict, created)
        ctx = self.p(f"ctx{self.n}.json")
        write(ctx, ts.QUESTION)
        self.cli("br", root, "update", created["id"], "--agent-context", "@" + ctx)
        return created["id"]

    def humanq_comments(self, prog: str, root: str, iid: str) -> list[str]:
        """[humanq] のコメントの本文 (書いた順)。偽の br は新しい順で返すので、単調に増える id で並べ直す (製品の並べ替えは使わない)。"""
        cs = json.loads(self.cli(prog, root, "comments", iid, "--json") or "[]")
        self.assertIsInstance(cs, list, cs)
        cs = [c for c in cs if isinstance(c, dict)]
        if prog == "br":
            cs.sort(key=lambda c: c["id"])
        return [c["text"] for c in cs if str(c.get("text")).startswith(humanq.MARK)]

    def concurrent_answers(self, store, prog: str, calls: list[tuple[str, str]]) -> tuple[list[str], int]:
        """calls の (登録のパス, id) を 2 本のスレッドで同時に Store.answer する。回答のコメントを書く CLI の手前で
        待ち合わせる (WAIT 秒で時間切れ)。(結果の型の名前の list, 待ち合わせが時間切れ・壊れた回数)。"""
        self.assertEqual(len(calls), 2)
        barrier = threading.Barrier(2, timeout=WAIT)
        broken = [0]
        real = getattr(humanq, prog)

        def gated(repo, *args):
            if "comments" in args and "add" in args:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    broken[0] += 1
            return real(repo, *args)

        results: list[str] = []
        start = threading.Barrier(2, timeout=10)

        def go(path: str, iid: str):
            start.wait()
            try:
                store.answer(path, {"id": iid, "option": "a"})
                results.append("ok")
            except Exception as e:  # noqa: BLE001
                results.append(type(e).__name__)

        with mock.patch.object(humanq, prog, gated):
            ts_ = [threading.Thread(target=go, args=c) for c in calls]
            for t in ts_:
                t.start()
            for t in ts_:
                t.join(timeout=60)
        self.assertEqual(len(results), 2, results)
        return sorted(results), broken[0]


class BrWorktreeStore(FakeCli):
    def pair(self) -> tuple[str, str, str]:
        main = self.br_main()
        iid = self.br_question(main)
        wt = linked(main, self.p("wt"))
        os.makedirs(os.path.join(wt, ".beads"))
        for n in (".gitignore", "metadata.json"):                # worktree に来るのは追跡するファイルだけ (DB は来ない)
            shutil.copy2(os.path.join(main, ".beads", n), os.path.join(wt, ".beads", n))
        registry.register(self.sd, main)
        registry.register(self.sd, wt)
        return main, wt, iid

    def test_fake_br_in_the_worktree_reads_the_main_db(self):
        """前提: 偽の br も、worktree で打つと本体の DB を読む (本物の br 0.7.6 と同じ。まとめないと二重に書ける形)。"""
        main, wt, iid = self.pair()
        out = json.loads(self.cli("br", wt, "list", "-l", "human", "--limit", "0", "--json"))
        self.assertIsInstance(out, dict, out)
        self.assertEqual([i.get("id") for i in out.get("issues", [])], [iid])

    def test_one_card_for_the_worktree_and_the_main(self):
        main, wt, iid = self.pair()
        store = humanq.Store(self.sd)
        ps = store.all()
        self.assertEqual([p["path"] for p in ps], [main], "同じ問いが 2 つのプロジェクトに出た")
        self.assertEqual([i["id"] for i in ps[0]["human"]], [iid])
        self.assertEqual(ps[0]["cli"], registry.BR)

    def test_concurrent_answers_from_both_cards_write_one_comment(self):
        main, wt, iid = self.pair()
        store = humanq.Store(self.sd)
        results, broken = self.concurrent_answers(store, "br", [(main, iid), (wt, iid)])
        self.assertEqual(self.humanq_comments("br", main, iid), ["[humanq] 回答: 案A (a)\n"], "回答のコメントが 1 件でない")
        self.assertEqual(results, ["Gone", "ok"], results)
        self.assertGreaterEqual(broken, 1, "2 本の書き込みが同時にロックの中にいた")
        shown = json.loads(self.cli("br", main, "show", iid, "--json"))
        self.assertEqual(len(shown), 1, shown)
        self.assertEqual(sorted(shown[0].get("labels") or []), ["answered"])
        calls = [json.loads(x) for x in Path(self.log).read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual({c["cwd"] for c in calls if c["prog"] == "br" and "comments" in c["argv"] and "add" in c["argv"]},
                         {main}, "回答を本体ではない dir で書いた")

    def test_undo_through_the_worktree_path_uses_the_main(self):
        main, wt, iid = self.pair()
        store = humanq.Store(self.sd)
        Path(self.log).unlink(missing_ok=True)
        sig = store.answer(wt, {"id": iid, "option": "a"})
        store.undo(wt, {"id": iid, "asig": sig})
        calls = [json.loads(x) for x in Path(self.log).read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertGreaterEqual(len(calls), 10, calls)                 # 回答 5 プロセス + 取り消し 5 プロセス
        self.assertEqual({c["cwd"] for c in calls}, {main}, "worktree のパスの回答・取り消しを、本体ではない dir で打った")
        self.assertEqual(self.humanq_comments("br", main, iid), ["[humanq] 回答: 案A (a)\n", humanq.UNDO_HEAD + "\n"])

    def test_http_summary_and_answer_by_the_worktree_path(self):
        """hook は worktree の登録のパスで summary を問い合わせ、まとめる前の画面は worktree のパスで回答を送る。"""
        main, wt, iid = self.pair()
        port = ts.free_port()
        srv = ts.start_server(self.sd, port)
        self.addCleanup(ts.stop, srv)
        base = f"http://127.0.0.1:{port}"
        code, _, body = ts.fetch(f"{base}/api/items")
        self.assertEqual(code, 200)
        self.assertEqual([p["path"] for p in json.loads(body)["projects"]], [main])
        for path in (wt, main):
            code, _, body = ts.fetch(f"{base}/api/summary?project={urllib.parse.quote(path, safe='')}&fresh=1")
            self.assertEqual(code, 200, (path, body))
            self.assertEqual(json.loads(body).get("human"), 1, (path, body))
        code, _, _ = ts.fetch(f"{base}/api/summary?project={urllib.parse.quote(self.p('nope'), safe='')}")
        self.assertEqual(code, 404)
        tok = ts.page_token(port)
        hdr = {"Content-Type": "application/json", "X-Humanq-Token": tok}
        code, _, body = ts.fetch(f"{base}/api/answer", json.dumps({"project": wt, "id": iid, "option": "a"}).encode(), hdr)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.humanq_comments("br", main, iid), ["[humanq] 回答: 案A (a)\n"])
        code, _, body = ts.fetch(f"{base}/api/summary?project={urllib.parse.quote(wt, safe='')}&fresh=1")
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body).get("answered"), [iid], body)
        code, _, body = ts.fetch(f"{base}/api/answer", json.dumps({"project": self.p("nope"), "id": iid, "option": "a"}).encode(), hdr)
        self.assertEqual(code, 400, body)
        self.assertIn("未登録", json.loads(body).get("error", ""))


class SymlinkStore(FakeCli):
    def test_concurrent_answers_through_a_symlinked_beads_write_one_comment(self):
        real = self.p("real")
        os.makedirs(real)
        self.cli("br", real, "init", "--prefix", "hq")
        write(os.path.join(real, ".beads", ".gitignore"), BR_GITIGNORE)
        iid = self.br_question(real)
        link = self.p("link")
        os.makedirs(link)
        os.symlink(os.path.join(real, ".beads"), os.path.join(link, ".beads"))
        registry.register(self.sd, link)
        registry.register(self.sd, real)
        store = humanq.Store(self.sd)
        self.assertEqual([p["path"] for p in store.all()], [real])
        results, broken = self.concurrent_answers(store, "br", [(link, iid), (real, iid)])
        self.assertEqual(self.humanq_comments("br", real, iid), ["[humanq] 回答: 案A (a)\n"], "回答のコメントが 1 件でない")
        self.assertEqual(results, ["Gone", "ok"], results)
        self.assertGreaterEqual(broken, 1)


class OddFilesApi(FakeCli):
    def test_odd_files_in_other_registrations_keep_the_api_answering(self):
        """組の判定が新しく読むファイル (.git・commondir・worktree の metadata.json・.beads/) と登録のパスに、NUL・非 UTF-8・
        長すぎるパス・読めない権限を入れた登録が並んでいても、/api/items・/api/summary・/api/answer は 200 で返り、
        まともなプロジェクトの問いを読み書きできる。"""
        main = self.br_main()
        iid = self.br_question(main)
        locked: list[str] = []
        self.addCleanup(lambda: [os.chmod(x, 0o700) for x in locked if os.path.lexists(x)])

        def wt(name: str) -> str:                     # 本体に linked した、DB の無い worktree
            return br_beads(linked(main, self.p(name)))

        def adm(name: str) -> str:
            return os.path.join(main, ".git", "worktrees", name)

        def unreadable(path: str) -> None:
            os.chmod(path, 0)
            locked.append(path)

        odd: list[str] = []
        for name, body in (("gitdir-nul", "gitdir: /x\0y/.git/worktrees/w\n"),
                           ("gitdir-long", "gitdir: /" + "a" * 5000 + "/.git/worktrees/w\n")):
            odd.append(br_beads(self.p(name)))
            write(os.path.join(odd[-1], ".git"), body)
        odd.append(br_beads(self.p("gitdir-non-utf8")))
        Path(odd[-1], ".git").write_bytes(b"gitdir: /x\xff\xfe/.git/worktrees/w\n")
        for name, body in (("commondir-nul", "a\0b/.git\n"), ("commondir-long", "a" * 5000 + "/.git\n")):
            odd.append(wt(name))
            write(os.path.join(adm(name), "commondir"), body)
        odd.append(wt("commondir-non-utf8"))
        Path(adm("commondir-non-utf8"), "commondir").write_bytes(b"\xff\xfe/../..\n")
        for name, meta in (("meta-escaped-nul", {"database": "a\0b.db"}), ("meta-long", {"database": "x" * 5000})):
            odd.append(br_beads(linked(main, self.p(name)), meta=meta))
        for name, raw in (("meta-raw-nul", b'{"database": "a\0b.db"}'), ("meta-non-utf8", b'{"database": "\xff.db"}')):
            odd.append(wt(name))
            Path(odd[-1], ".beads", "metadata.json").write_bytes(raw)
        if os.geteuid() != 0:                         # root は権限で読めなくならない
            for name, target in (("git-unreadable", ".git"), ("meta-unreadable", os.path.join(".beads", "metadata.json")),
                                 ("beads-unreadable", ".beads")):
                odd.append(wt(name))
                unreadable(os.path.join(odd[-1], target))
            odd.append(wt("commondir-unreadable"))
            unreadable(os.path.join(adm("commondir-unreadable"), "commondir"))
        for r in odd:
            registry.register(self.sd, r)
        for bad in (self.p("nul\0path"), "/" + "a" * 5000):    # 登録のパスそのもの
            registry.register(self.sd, bad)
        self.assertIn(main, [g["root"] for g in self.groups()])

        port = ts.free_port()
        srv = ts.start_server(self.sd, port)
        self.addCleanup(ts.stop, srv)
        base = f"http://127.0.0.1:{port}"

        def fetch(url: str, *args):
            try:
                return ts.fetch(url, *args)
            except Exception as e:  # noqa: BLE001  応答なしに切れる (サーバーの handler の例外) のは ERROR ではなく FAIL
                self.fail(f"{url[:120]}: {type(e).__name__}: {e}")

        code, _, body = fetch(f"{base}/api/items")
        self.assertEqual(code, 200, body)
        ps = {p["path"]: p for p in json.loads(body)["projects"]}
        self.assertIn(main, ps)
        self.assertEqual([i["id"] for i in ps[main]["human"]], [iid])
        for r in (main, *odd):
            with self.subTest(project=os.path.basename(r)):
                code, _, body = fetch(f"{base}/api/summary?project={urllib.parse.quote(r, safe='')}&fresh=1")
                self.assertEqual(code, 200, body)
        tok = ts.page_token(port)
        hdr = {"Content-Type": "application/json", "X-Humanq-Token": tok}
        code, _, body = fetch(f"{base}/api/answer", json.dumps({"project": main, "id": iid, "option": "a"}).encode(), hdr)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.humanq_comments("br", main, iid), ["[humanq] 回答: 案A (a)\n"])


class BdWorktreeStore(FakeCli):
    def test_bd_worktree_stays_separate_but_writes_one_at_a_time(self):
        """bd の worktree は画面ではまとめない (bd の解決は測っていない)。同じ repo の 2 枚からの同時の回答は 1 本ずつ書く。"""
        main = main_repo(self.p("main"))
        wt = linked(main, self.p("wt"))
        ids = {}
        for root in (main, wt):
            self.cli("bd", root, "init", "--non-interactive", "--quiet", "--skip-agents", "--skip-hooks", "-p", "hq")
            os.makedirs(os.path.join(root, ".beads", "embeddeddolt"), exist_ok=True)
            meta = self.p(f"meta-{os.path.basename(root)}.json")
            write(meta, ts.QUESTION)
            ids[root] = json.loads(self.cli("bd", root, "create", "q", "-l", "human", "--metadata", "@" + meta, "--json"))["id"]
            registry.register(self.sd, root)
        store = humanq.Store(self.sd)
        self.assertEqual(sorted(p["path"] for p in store.all()), sorted([main, wt]))
        self.assertIs(store._write_lock(main), store._write_lock(wt))
        results, broken = self.concurrent_answers(store, "bd", [(main, ids[main]), (wt, ids[wt])])
        self.assertEqual(results, ["ok", "ok"], results)
        self.assertGreaterEqual(broken, 1, "同じ repo の 2 つの登録の書き込みが同時にロックの中にいた")
        for root in (main, wt):
            self.assertEqual(self.humanq_comments("bd", root, ids[root]), ["[humanq] 回答: 案A (a)\n"], root)

    def test_projects_outside_git_do_not_share_a_lock(self):
        a = br_beads(self.p("a"), "beads.db")
        b = br_beads(self.p("b"), "beads.db")
        store = humanq.Store(self.sd)
        self.assertIsNot(store._write_lock(a), store._write_lock(b))
        self.assertIs(store._write_lock(a), store._write_lock(a))


if __name__ == "__main__":
    unittest.main()
