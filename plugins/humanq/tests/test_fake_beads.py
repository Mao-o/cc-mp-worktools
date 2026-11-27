"""偽の br (tests/fake_beads.py を br の名前で呼ぶ) の癖を、1 件ずつ押さえる。

偽の br は、後の案 (bd と br の種別判定、br の読み書き) が CI で br と bd の差を確かめるための道具。
ここで押さえる癖は、br 0.7.6 の実測 (使い捨ての一時 dir で打った結果)。偽物が癖を落とすと、
その癖に頼る後の案のテストが、本物の br では起きる失敗を見逃す。humanq の製品コードは br の list・show・comments add・label を使う。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_beads.py"
ISO_MICRO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
ISO_SEC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class FakeBrCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-fakebr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.br = self.install("br")
        self.bd = self.install("bd")
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        self.log = os.path.join(self.tmp, "calls.log")

    def install(self, prog: str) -> str:
        """test_server.install_fake と同じ作り (名前を変えた写しに、今の python の shebang)。"""
        _, _, body = FAKE.read_text(encoding="utf-8").partition("\n")
        shebang = "#!/usr/bin/env python3" if re.search(r"\s", sys.executable) else "#!" + sys.executable
        dst = os.path.join(self.bin, prog)
        Path(dst).write_text(shebang + "\n" + body, encoding="utf-8")
        os.chmod(dst, 0o755)
        return dst

    def env(self) -> dict:
        e = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_BEADS_")}
        e["PYTHONDONTWRITEBYTECODE"] = "1"
        return e

    def run_br(self, *args: str, cwd: str | None = None, prog: str | None = None, env: dict | None = None):
        return subprocess.run([prog or self.br, *args], cwd=cwd or self.proj, capture_output=True, text=True,
                              timeout=60, env=env or self.env())

    def ok(self, *args: str, **kw):
        p = self.run_br(*args, **kw)
        self.assertEqual(p.returncode, 0, f"br {args}: {p.stderr or p.stdout}")
        return p

    def load(self, text: str):
        """JSON でない出力は、例外ではなく assertion の失敗にする (形が崩れた変異を ERROR にしない)。"""
        try:
            return json.loads(text)
        except ValueError:
            self.fail(f"JSON ではない出力: {text!r}")

    def jok(self, *args: str, **kw):
        out = self.load(self.ok(*args, "--json", **kw).stdout)
        if args and args[0] == "list":       # 添字の前に形を確かめる (崩れた形は ERROR ではなく FAIL にする)
            self.assertIsInstance(out, dict)
            for k in ("issues", "total", "limit", "offset", "has_more"):
                self.assertIn(k, out)
        if args and args[0] == "show":
            self.assertIsInstance(out, list)
            self.assertEqual(len(out), len(args) - 1)
            for item in out:
                self.assertIsInstance(item, dict)       # comments の key はコメントがあるときだけ (BrShow が押さえる)
        if "comments" in args and "add" in args:
            self.assertIsInstance(out, dict)
            for k in ("id", "author", "text"):
                self.assertIn(k, out)
        return out

    def one_row(self) -> dict:
        rows = self.jok("list")["issues"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def init(self) -> None:
        self.ok("init")

    def create(self, title: str, *flags: str) -> str:
        return self.jok("create", title, *flags)["id"]

    def beads_files(self) -> list[str]:
        return sorted(os.listdir(os.path.join(self.proj, ".beads")))


class BrUsageErrors(FakeBrCase):
    """bd にあって br に無いものは、usage エラー (exit 2) で落ちる。"""

    def setUp(self):
        super().setUp()
        self.init()
        self.i1 = self.create("q1")
        self.before = Path(self.proj, ".beads", "fake-state.json").read_text(encoding="utf-8")

    def assert_usage(self, args, needle: str):
        p = self.run_br(*args)
        self.assertEqual(p.returncode, 2, f"br {args}: rc={p.returncode} {p.stderr!r} {p.stdout!r}")
        self.assertIn(needle, p.stderr)
        self.assertEqual(p.stdout, "", "usage エラーは stdout に何も出さない")
        self.assertEqual(Path(self.proj, ".beads", "fake-state.json").read_text(encoding="utf-8"), self.before,
                         "usage エラーで状態が変わった")

    def test_dash_C(self):
        self.assert_usage(["-C", self.proj, "list", "--json"], "error: unexpected argument '-C' found")

    def test_dash_C_after_subcommand(self):
        self.assert_usage(["list", "-C", self.proj], "error: unexpected argument '-C' found")

    def test_human_subcommand(self):
        self.assert_usage(["human", "list", "--json"], "error: unrecognized subcommand 'human'")

    def test_list_flat(self):
        self.assert_usage(["list", "--flat", "--json"], "error: unexpected argument '--flat' found")

    def test_list_dash_n(self):
        self.assert_usage(["list", "-n", "10", "--json"], "error: unexpected argument '-n' found")

    def test_update_metadata(self):
        self.assert_usage(["update", self.i1, "--metadata", "{}"], "error: unexpected argument '--metadata' found")

    def test_update_set_metadata(self):
        self.assert_usage(["update", self.i1, "--set-metadata", "k=v"],
                          "error: unexpected argument '--set-metadata' found")


class BrLocatesProject(FakeBrCase):
    def test_searches_upward_for_beads(self):
        self.init()
        self.create("q1")
        deep = os.path.join(self.proj, "a", "b")
        os.makedirs(deep)
        out = self.jok("list", cwd=deep)
        self.assertEqual(out["total"], 1)

    def test_no_project_fails(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        p = self.run_br("list", "--json", cwd=empty)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no .beads directory found", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_call_is_logged_under_br_name(self):
        self.init()
        env = {**self.env(), "FAKE_BEADS_LOG": self.log}
        self.ok("list", "--json", env=env)
        lines = Path(self.log).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        rec = self.load(lines[0])
        self.assertEqual(rec["prog"], "br")
        self.assertEqual(rec["argv"], ["list", "--json"])


class BrList(FakeBrCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_comma_status_is_validation_error_on_stdout(self):
        self.create("q1")
        p = self.run_br("list", "-s", "open,in_progress", "--json")
        self.assertNotEqual(p.returncode, 0)
        out = self.load(p.stdout)
        self.assertIn("error", out)
        self.assertEqual(out["error"]["code"], "VALIDATION_FAILED")
        self.assertIn("unknown status 'open,in_progress'", out["error"]["message"])

    def test_repeated_status_is_accepted(self):
        a = self.create("a")
        b = self.create("b")
        c = self.create("c")
        self.ok("update", b, "--status", "in_progress")
        self.ok("close", c)
        out = self.jok("list", "-s", "open", "-s", "in_progress")
        self.assertEqual(sorted(x["id"] for x in out["issues"]), sorted([a, b]))

    def test_empty_envelope_has_same_shape(self):
        out = self.jok("list")
        self.assertEqual(out, {"issues": [], "total": 0, "limit": 0, "offset": 0, "has_more": False})

    def test_envelope_keys_with_items(self):
        self.create("a")
        out = self.jok("list")
        self.assertEqual(sorted(out), ["has_more", "issues", "limit", "offset", "total"])
        self.assertEqual((out["total"], out["limit"], out["offset"], out["has_more"]), (1, 0, 0, False))
        self.assertIsInstance(out["issues"], list)

    def test_default_and_limit_zero_are_unlimited(self):
        for n in range(60):                       # bd の既定 (50) を超える
            self.create(f"q{n}")
        self.assertEqual(len(self.jok("list")["issues"]), 60)
        out = self.jok("list", "--limit", "0")
        self.assertEqual((len(out["issues"]), out["total"]), (60, 60))

    def test_limit_n_truncates_and_reports(self):
        for n in range(3):
            self.create(f"q{n}")
        out = self.jok("list", "--limit", "2")
        self.assertEqual((len(out["issues"]), out["total"], out["limit"], out["has_more"]), (2, 3, 2, True))
        out = self.jok("list", "--limit", "3")
        self.assertEqual((len(out["issues"]), out["total"], out["limit"], out["has_more"]), (3, 3, 3, False))

    def test_default_excludes_only_closed(self):
        ids = {s: self.create(s) for s in ("open", "in_progress", "blocked", "deferred", "closed")}
        for s in ("in_progress", "blocked", "deferred"):
            self.ok("update", ids[s], "--status", s)
        self.ok("close", ids["closed"])
        got = {x["id"]: x["status"] for x in self.jok("list")["issues"]}
        self.assertEqual(sorted(got.values()), ["blocked", "deferred", "in_progress", "open"])
        self.assertNotIn(ids["closed"], got)

    def test_label_filter_keeps_deferred(self):
        a = self.create("a", "-l", "human")
        b = self.create("b", "-l", "human")
        c = self.create("c", "-l", "other")
        self.ok("update", b, "--status", "deferred")
        got = sorted(x["id"] for x in self.jok("list", "-l", "human")["issues"])
        self.assertEqual(got, sorted([a, b]))
        self.assertNotIn(c, got)

    def test_row_fields(self):
        a = self.create("a", "-l", "zeta,alpha", "-p", "1")
        row = self.one_row()
        for k in ("id", "labels", "priority", "created_at"):
            self.assertIn(k, row)
        self.assertEqual(row["id"], a)
        self.assertEqual(row["labels"], ["alpha", "zeta"])
        self.assertEqual(row["priority"], 1)
        self.assertRegex(row["created_at"], ISO_MICRO)
        for k in ("metadata", "owner", "agent_context"):
            self.assertNotIn(k, row, f"{k} は値が無いとき (metadata と owner は常に) 出ない")

    def test_labels_key_is_present_even_when_empty(self):
        self.create("a")
        row = self.one_row()
        self.assertIn("labels", row)
        self.assertEqual(row["labels"], [])

    def test_agent_context_only_on_issues_that_have_it(self):
        a = self.create("a")
        b = self.create("b")
        ctx = Path(self.tmp, "ctx.json")
        ctx.write_text('{"q":1}', encoding="utf-8")
        self.ok("update", a, "--agent-context", f"@{ctx}")
        rows = {x["id"]: x for x in self.jok("list")["issues"]}
        self.assertEqual(len(rows), 2)
        self.assertIn(a, rows)
        self.assertIn(b, rows)
        self.assertIn("agent_context", rows[a])
        self.assertEqual(rows[a]["agent_context"], '{"q":1}')
        self.assertIsInstance(rows[a]["agent_context"], str)
        self.assertNotIn("agent_context", rows[b])

    def test_description_and_notes_only_when_set(self):
        # br 0.7.6 の実測: description と notes は値のあるチケットにだけ、list と show の両方に出る
        a = self.create("a", "-d", "body")
        b = self.create("b")
        self.ok("update", b, "--notes", "memo")
        rows = {x["id"]: x for x in self.jok("list")["issues"]}
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[a].get("description"), "notes" in rows[a]), ("body", False))
        self.assertEqual(("description" in rows[b], rows[b].get("notes")), (False, "memo"))
        shown = {x["id"]: x for x in self.jok("show", a, b)}
        self.assertEqual(len(shown), 2)
        self.assertEqual((shown[a].get("description"), "notes" in shown[a]), ("body", False))
        self.assertEqual(("description" in shown[b], shown[b].get("notes")), (False, "memo"))


class BrShow(FakeBrCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_array_with_comments_and_agent_context_string(self):
        a = self.create("a")
        ctx = Path(self.tmp, "ctx.json")
        ctx.write_text(json.dumps({"options": ["x", "y"]}), encoding="utf-8")
        self.ok("update", a, "--agent-context", f"@{ctx}")
        out = self.jok("show", a)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 1)
        item = out[0]
        self.assertIn("agent_context", item)
        self.assertIsInstance(item["agent_context"], str)
        self.assertEqual(self.load(item["agent_context"]), {"options": ["x", "y"]})
        self.assertNotIn("metadata", item)

    def test_comments_key_is_absent_until_there_is_a_comment(self):
        """br 0.7.6 の実測: コメントの無いチケットの show は comments の key を出さない ([] ではない)。"""
        a = self.create("a")
        b = self.create("b")
        self.ok("comments", "add", b, "c1")
        shown = {x["id"]: x for x in self.jok("show", a, b)}
        self.assertEqual(sorted(shown), sorted([a, b]))
        self.assertNotIn("comments", shown[a], "コメントが 0 件なのに comments の key がある")
        self.assertEqual([c["text"] for c in shown[b].get("comments", [])], ["c1"])
        self.assertNotIn("comments", self.jok("show", a)[0], "1 件だけ打っても同じ")

    def test_without_ids_it_shows_the_last_touched_ticket(self):
        """本物は id を書かないと最後に触ったチケットを出す (exit 0)。create・update・close・label・comments add が触れ、
        show・list・comments の読み・delete は触れない。"""
        a = self.create("a")
        b = self.create("b")
        c = self.create("c")

        def last() -> list[str]:
            out = self.load(self.ok("show", "--json").stdout)      # jok は id の個数と戻りの個数を比べるので使わない
            self.assertIsInstance(out, list)
            for x in out:
                self.assertIsInstance(x, dict)
            return [x.get("id") for x in out]
        self.assertEqual(last(), [c], "create が触れる")
        self.ok("update", a, "--title", "a2")
        self.assertEqual(last(), [a], "update が触れる")
        self.ok("label", "add", b, "x")
        self.assertEqual(last(), [b], "label add が触れる")
        self.ok("comments", "add", a, "t")
        self.assertEqual(last(), [a], "comments add が触れる")
        self.ok("close", c)
        self.assertEqual(last(), [c], "close が触れる")
        self.ok("label", "remove", b, "nolabel")
        self.assertEqual(last(), [b], "label remove (not_found) も触れる")
        self.ok("show", c, "--json")
        self.ok("list", "--json")
        self.ok("comments", a, "--json")
        self.ok("delete", c)
        self.assertEqual(last(), [b], "show・list・comments の読み・delete は触れない")

    def test_without_ids_and_without_a_last_touched_ticket_is_a_validation_error(self):
        p = self.run_br("show", "--json")
        self.assertEqual(p.returncode, 4, p.stderr)
        out = self.load(p.stdout)
        self.assertEqual(out.get("error", {}).get("code"), "VALIDATION_FAILED")
        self.assertEqual(out["error"].get("message"),
                         "Validation failed: ids: no issue IDs provided and no last-touched issue")      # 本物の文 (0.7.6)

    def test_many_ids_in_one_call(self):
        a = self.create("a")
        b = self.create("b")
        out = self.jok("show", a, b)
        self.assertEqual([x["id"] for x in out], [a, b])

    def test_missing_id_is_not_found_exit_3(self):
        p = self.run_br("show", "bd-999", "--json")
        self.assertEqual(p.returncode, 3)
        out = self.load(p.stdout)
        self.assertIn("error", out)
        self.assertEqual(out["error"]["code"], "ISSUE_NOT_FOUND")

    def test_one_missing_among_existing_fails_all(self):
        a = self.create("a")
        p = self.run_br("show", a, "bd-999", "--json")
        self.assertEqual(p.returncode, 3)
        out = self.load(p.stdout)
        self.assertNotIsInstance(out, list, "存在する id の分を返してはいけない (全体が失敗する)")
        self.assertIn("error", out)
        self.assertEqual(out["error"]["code"], "ISSUE_NOT_FOUND")

    def test_comments_in_show_are_not_chronological(self):
        a = self.create("a")
        for t in ("one", "two", "three"):
            self.ok("comments", "add", a, t)
        texts = [c["text"] for c in self.jok("show", a)[0]["comments"]]
        self.assertEqual(sorted(texts), ["one", "three", "two"])
        self.assertNotEqual(texts, ["one", "two", "three"], "偽の br は古い順で返してはいけない")


class BrComments(FakeBrCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.a = self.create("a")

    def test_order_is_not_chronological_and_ids_are_monotonic_ints(self):
        for t in ("one", "two", "three"):
            self.ok("comments", "add", self.a, t)
        out = self.jok("comments", self.a)
        self.assertEqual(len(out), 3)
        self.assertNotEqual([c["text"] for c in out], ["one", "two", "three"])
        ids = [c["id"] for c in out]
        for i in ids:
            self.assertIs(type(i), int)
        by_text = {c["text"]: c["id"] for c in out}
        self.assertLess(by_text["one"], by_text["two"])
        self.assertLess(by_text["two"], by_text["three"])
        for c in out:
            self.assertRegex(c["created_at"], ISO_SEC)
            self.assertEqual(c["issue_id"], self.a)

    def test_add_with_actor_and_file_keeps_trailing_newline(self):
        body = Path(self.tmp, "body.txt")
        body.write_text("answer\n\n", encoding="utf-8")
        added = self.jok("--actor", "user/humanq", "comments", "add", self.a, "-f", str(body))
        self.assertIsInstance(added, dict)
        self.assertEqual(added.get("author"), "user/humanq")
        self.assertEqual(added.get("text"), "answer\n\n")
        self.assertIs(type(added.get("id")), int)
        stored_all = self.jok("comments", self.a)
        self.assertEqual(len(stored_all), 1)
        stored = stored_all[0]
        self.assertEqual(stored["text"], "answer\n\n")
        self.assertEqual(stored["author"], "user/humanq")

    def test_ids_increase_across_issues(self):
        b = self.create("b")
        c1 = self.jok("comments", "add", self.a, "x")
        c2 = self.jok("comments", "add", b, "y")
        self.assertGreater(c2["id"], c1["id"])

    def test_unknown_issue_is_not_found(self):
        p = self.run_br("comments", "bd-999", "--json")
        self.assertEqual(p.returncode, 3)
        out = self.load(p.stdout)
        self.assertIn("error", out)
        self.assertEqual(out["error"]["code"], "ISSUE_NOT_FOUND")

    def test_file_wins_over_extra_positionals_and_the_id_can_come_after_it(self):
        """br 0.7.6 の実測: -f があると id のほかの位置引数 (本文) は無視され、本文はファイルのもの。id の位置は問わない。"""
        body = Path(self.tmp, "body.txt")
        body.write_text("from-file\n", encoding="utf-8")
        forms = {"id 本文 -f": ["comments", "add", self.a, "x", "-f", str(body)],
                 "id -f 本文": ["comments", "add", self.a, "-f", str(body), "zzz"],
                 "id 本文 本文 -f": ["comments", "add", self.a, "x", "y", "-f", str(body)],
                 "-f id": ["comments", "add", "-f", str(body), self.a]}
        for name, form in forms.items():
            with self.subTest(form=name):
                added = self.jok(*form)
                self.assertEqual(added.get("text"), "from-file\n")
        texts = sorted(c["text"] for c in self.jok("comments", self.a))
        self.assertEqual(texts, ["from-file\n"] * len(forms), "どの形も -f の本文で書き、本文の位置引数は使わない")

    def test_file_without_an_id_is_a_usage_error(self):
        body = Path(self.tmp, "body.txt")
        body.write_text("x\n", encoding="utf-8")
        p = self.run_br("comments", "add", "-f", str(body))
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("<ID>", p.stderr)
        self.assertEqual(self.jok("comments", self.a), [], "id の無い書き込みでコメントができた")


class BrClose(FakeBrCase):
    """close の理由 (-r・--reason) は、次の語が `-` で始まっても値に取る。--json が理由になる。"""

    def setUp(self):
        super().setUp()
        self.init()

    def status_of(self, iid: str) -> str:
        shown = self.jok("show", iid)
        self.assertEqual(len(shown), 1)
        return shown[0].get("status")

    def test_json_after_a_reason_flag_becomes_the_reason(self):
        for flag in ("-r", "--reason"):
            with self.subTest(flag=flag):
                iid = self.create(f"c{flag}")
                p = self.run_br("close", iid, flag, "--json")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(self.status_of(iid), "closed", "--json を理由に取らず、値が無い扱いにした")

    def test_a_dash_word_after_a_reason_flag_is_the_reason(self):
        iid = self.create("c")
        self.assertEqual(self.run_br("close", iid, "-r", "-x").returncode, 0)
        self.assertEqual(self.status_of(iid), "closed")

    def test_reason_flag_as_the_last_word_is_a_usage_error(self):
        iid = self.create("c")
        for flag in ("-r", "--reason"):
            with self.subTest(flag=flag):
                p = self.run_br("close", iid, flag)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn("a value is required", p.stderr)
                self.assertEqual(self.status_of(iid), "open")

    def test_json_before_a_reason_flag_is_still_a_global(self):
        """`--json` が -r の前にあれば globals で、-r は最後の語になる (本物も exit 2)。"""
        iid = self.create("c")
        p = self.run_br("close", iid, "--json", "-r")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertEqual(self.status_of(iid), "open")

    def test_other_value_flags_do_not_take_json(self):
        """-p・--title・--status などは `--json` を値に取らない (本物は値が無いと exit 2)。"""
        iid = self.create("c")
        for args in (["create", "x", "-p", "--json"], ["update", iid, "--title", "--json"],
                     ["update", iid, "--status", "--json"], ["list", "-s", "--json"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_br(*args).returncode, 2)

    def test_notes_and_description_take_a_dash_word_too(self):
        iid = self.create("c")
        self.assertEqual(self.run_br("update", iid, "--notes", "--json").returncode, 0)
        shown = self.jok("show", iid)[0]
        self.assertEqual(shown.get("notes"), "--json")
        for flag, title in (("-d", "dd"), ("--description", "dl")):
            with self.subTest(flag=flag):
                made = self.run_br("create", title, flag, "--json")
                self.assertEqual(made.returncode, 0, made.stderr)
        rows = {r["title"]: r for r in self.jok("list")["issues"]}
        for title in ("dd", "dl"):
            self.assertEqual(rows.get(title, {}).get("description"), "--json", title)


class BrLabels(FakeBrCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.a = self.create("a")

    def test_several_labels_are_processed_one_by_one(self):
        """br 0.7.6 の実測: label add / remove は <id> のあとに label を何個でも取り、label ごとに 1 行を返す。"""
        self.assertEqual(self.jok("label", "add", self.a, "x1", "x2"),
                         [{"status": "added", "issue_id": self.a, "label": "x1"},
                          {"status": "added", "issue_id": self.a, "label": "x2"}])
        self.assertEqual(self.jok("label", "add", self.a, "x2", "x3", "x3"),
                         [{"status": "exists", "issue_id": self.a, "label": "x2"},
                          {"status": "added", "issue_id": self.a, "label": "x3"},
                          {"status": "exists", "issue_id": self.a, "label": "x3"}])
        self.assertEqual(self.jok("show", self.a)[0].get("labels"), ["x1", "x2", "x3"])
        self.assertEqual(self.jok("label", "remove", self.a, "x1", "zz"),
                         [{"status": "removed", "issue_id": self.a, "label": "x1"},
                          {"status": "not_found", "issue_id": self.a, "label": "zz"}])
        self.assertEqual(self.jok("show", self.a)[0].get("labels"), ["x2", "x3"])

    def test_several_labels_on_an_unknown_issue_change_nothing(self):
        p = self.run_br("label", "add", "bd-999", "x1", "x2", "--json")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(self.load(p.stdout).get("error", {}).get("code"), "ISSUE_NOT_FOUND")
        self.assertEqual(self.jok("list")["issues"][0].get("labels"), [])

    def test_text_output_has_one_line_per_label(self):
        p = self.ok("label", "add", self.a, "x1", "x2")
        self.assertEqual(len(p.stdout.strip().splitlines()), 2)

    def test_add_then_exists(self):
        self.assertEqual(self.jok("label", "add", self.a, "human"),
                         [{"status": "added", "issue_id": self.a, "label": "human"}])
        self.assertEqual(self.jok("label", "add", self.a, "human"),
                         [{"status": "exists", "issue_id": self.a, "label": "human"}])

    def test_remove_then_not_found_both_exit_0(self):
        self.ok("label", "add", self.a, "human")
        self.assertEqual(self.jok("label", "remove", self.a, "human"),
                         [{"status": "removed", "issue_id": self.a, "label": "human"}])
        self.assertEqual(self.jok("label", "remove", self.a, "human"),
                         [{"status": "not_found", "issue_id": self.a, "label": "human"}])

    def test_label_changes_are_visible_in_list(self):
        self.ok("label", "add", self.a, "human")
        rows = self.jok("list", "-l", "human")["issues"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["labels"], ["human"])
        self.ok("label", "remove", self.a, "human")
        self.assertEqual(self.jok("list", "-l", "human")["issues"], [])


class BrAgentContext(FakeBrCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.a = self.create("a")
        self.f = Path(self.tmp, "ctx.txt")

    def put(self, text: str, *flags: str):
        self.f.write_text(text, encoding="utf-8")
        return self.run_br("update", self.a, "--agent-context", f"@{self.f}", *flags)

    def current(self) -> str:
        return self.jok("show", self.a)[0].get("agent_context", "")

    def test_replaces_whole_value(self):
        self.assertEqual(self.put("x" * 40).returncode, 0)
        self.assertEqual(self.put("y" * 30).returncode, 0)
        self.assertEqual(self.current(), "y" * 30)

    def test_shorter_than_half_is_refused(self):
        self.put("x" * 40)
        p = self.put("y" * 19)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refusing to overwrite non-empty 'agent_context'", p.stderr)
        self.assertIn("without --force", p.stderr)
        self.assertEqual(self.current(), "x" * 40, "断られた書き換えで値が変わった")

    def test_exactly_half_is_accepted(self):
        self.put("x" * 40)
        self.assertEqual(self.put("y" * 20).returncode, 0)
        self.assertEqual(self.current(), "y" * 20)

    def test_emptying_is_refused(self):
        self.put("x" * 40)
        p = self.put("")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refusing to overwrite non-empty 'agent_context'", p.stderr)
        self.assertEqual(self.current(), "x" * 40)

    def test_force_allows_shrinking(self):
        self.put("x" * 40)
        self.assertEqual(self.put("y", "--force").returncode, 0)
        self.assertEqual(self.current(), "y")

    def test_first_write_to_empty_is_accepted(self):
        self.assertEqual(self.put("z").returncode, 0)
        self.assertEqual(self.current(), "z")

    # ---- JSON は詰めて保存する (br 0.7.6 の実測。inline と @file のどちらも、show と list の両方に詰めた値が出る) ----

    COMPACT = {
        "空白入りの object": ('{"q": "x"}', '{"q":"x"}'),
        "key の順は保つ (並べ替えない)": ('{"b": 1, "a": 2, "c": {"z": 1, "y": [1, 2, {"k": "v"}]}}',
                                       '{"b":1,"a":2,"c":{"z":1,"y":[1,2,{"k":"v"}]}}'),
        "前後の空白": ('  {"a": 1}  ', '{"a":1}'),
        "改行つきの整形": ('{\n  "q": "x",\n  "o": [\n    1,\n    2\n  ]\n}\n', '{"q":"x","o":[1,2]}'),
        "空の object": ("{ }", "{}"),
        "配列": ("[1, 2]", "[1,2]"),
        "生の日本語はそのまま": ('{"q": "日本語 é"}', '{"q":"日本語 é"}'),
        "\\u の escape は文字に戻り、\\/ は / になる": ('{"q": "\\u65e5\\u672c\\u00e9 \\n tab\\t quote\\" slash\\/ "}',
                                              '{"q":"日本é \\n tab\\t quote\\" slash/ "}'),
        "数": ('{"a": 1.0, "b": 1e3, "e": 0.1}', '{"a":1.0,"b":1000.0,"e":0.1}'),
        "重複した key は後の値": ('{"a": 1, "a": 2}', '{"a":2}'),
        "null": ("null", "null"),
    }

    def test_json_is_stored_compact(self):
        for name, (given, stored) in self.COMPACT.items():
            with self.subTest(form=name, via="inline"):
                self.assertEqual(self.run_br("update", self.a, "--agent-context", given, "--force").returncode, 0)
                self.assertEqual(self.current(), stored)
                rows = self.jok("list")["issues"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].get("agent_context"), stored, "list にも詰めた値が出る")
            with self.subTest(form=name, via="@file"):
                self.assertEqual(self.put(given, "--force").returncode, 0)
                self.assertEqual(self.current(), stored)

    def test_the_shrink_check_compares_the_compact_length(self):
        """半分未満の判定は、詰めた後の長さで行う (空白で水増しした短い値で、断りをすり抜けられない)。"""
        long_ctx = json.dumps({"q": "x" * 50}, separators=(",", ":"))
        self.assertEqual(self.put(long_ctx).returncode, 0)
        padded = '{"q":' + " " * 200 + '"a"}'
        p = self.put(padded)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("refusing to overwrite non-empty 'agent_context'", p.stderr)
        self.assertEqual(self.current(), long_ctx, "断られた書き換えで値が変わった")

    def test_a_value_that_is_not_json_is_kept_as_written(self):
        """本物は JSON でない inline の値を exit 7 で断るが、偽物は寛容にしてある (今の食い違いを固定する)。"""
        deep = "[" * 100000                      # json の parser が RecursionError を上げる深さ (閉じない形と、閉じる形)
        for raw in ("plain  text", '{"a": ', "NaN", '{"a": Infinity}', '"\\ud800"', deep, deep + "]" * 100000):
            with self.subTest(raw=raw[:30]):
                self.assertEqual(self.put(raw, "--force").returncode, 0)
                self.assertEqual(self.current(), raw)


class BdCommentsAdd(FakeBrCase):
    """bd の comments add は --json で書いたコメントを JSON で返す (--json が無ければ 1 行の案内文)。"""

    def setUp(self):
        super().setUp()
        self.ok("init", prog=self.bd)
        made = self.ok("create", "t", "--json", prog=self.bd)
        self.iid = self.load(made.stdout)["id"]
        self.body = Path(self.tmp, "body.txt")
        self.body.write_text("answer\n", encoding="utf-8")

    def test_json_returns_the_written_comment(self):
        p = self.ok("--actor", "user/humanq", "comments", "add", self.iid, "-f", str(self.body), "--json", prog=self.bd)
        out = self.load(p.stdout)
        self.assertIsInstance(out, dict, p.stdout)
        self.assertEqual((out.get("author"), out.get("text"), out.get("issue_id")), ("user/humanq", "answer\n", self.iid))
        self.assertIsInstance(out.get("id"), str)
        stored = self.load(self.ok("comments", self.iid, "--json", prog=self.bd).stdout)
        self.assertEqual([(c.get("id"), c.get("text")) for c in stored], [(out["id"], "answer\n")])

    def test_without_json_it_prints_one_line(self):
        p = self.ok("comments", "add", self.iid, "-f", str(self.body), prog=self.bd)
        self.assertEqual(p.stdout.strip(), f"Comment added to {self.iid}")


class BrOnDoltProject(FakeBrCase):
    """bd の形 (database が dolt) の dir で br を打った事故の再現。エラーにならず、空の結果が返り、
    .beads に br 用のファイルができる。後の案は、このファイルが作られないことを確かめる。"""

    def setUp(self):
        super().setUp()
        self.ok("init", prog=self.bd)
        meta = json.loads(Path(self.proj, ".beads", "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["database"], "dolt")
        self.before = self.beads_files()

    def test_list_returns_empty_envelope_and_creates_files(self):
        out = self.jok("list")
        self.assertEqual(out, {"issues": [], "total": 0, "limit": 0, "offset": 0, "has_more": False})
        created = sorted(set(self.beads_files()) - set(self.before))
        self.assertEqual(created, ["dolt", "dolt-shm", "dolt-wal"])

    def test_other_commands_also_succeed_and_create_files(self):
        p = self.ok("show", "bd-1", "--json")
        self.assertEqual(p.returncode, 0)
        created = sorted(set(self.beads_files()) - set(self.before))
        self.assertEqual(created, ["dolt", "dolt-shm", "dolt-wal"])

    def test_usage_errors_still_come_first(self):
        p = self.run_br("-C", self.proj, "list")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.beads_files(), self.before, "usage エラーでファイルを作ってはいけない")


class FakeNames(FakeBrCase):
    def test_unknown_name_still_exits_2(self):
        other = self.install("xx")
        p = self.run_br("list", prog=other)
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown program name 'xx'", p.stderr)

    def test_bd_name_keeps_bd_behavior(self):
        self.ok("init", prog=self.bd)
        p = self.run_br("human", "list", "--json", prog=self.bd)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "null")


if __name__ == "__main__":
    unittest.main()
