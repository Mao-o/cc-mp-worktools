"""bd・br の CLI の癖を、偽物 (tests/fake_beads.py) と本物の両方に同じ assertion で当てる契約テスト。

    python3 -m unittest discover tests           # 偽物 2 通りだけ走る。本物は env を立てたときだけ走る (既定は skip)
    HUMANQ_TEST_BD=bd HUMANQ_TEST_BR=br python3 -m unittest tests.test_cli_contract   # 本物も回す
    python3 -m unittest tests.test_cli_contract  # このファイルだけ

humanq は CLI の出力の形に強く依る。偽物は実測した癖を写したものなので、bd / br の版が上がって本物の形が
変わると、偽物のテストは緑のまま本番だけが壊れる。ここは「偽物と本物が同じ癖を持つ」ことを確かめ、
手元に本物があるときに食い違いを検出する。bd / br を上げたら、手元でこれを回す。

- 対象は 4 通り: 偽の bd・偽の br (常に走る)、本物の bd・本物の br (env を立てたときだけ走る)
- 本物は遅い (bd は 1 回の起動が重く、契約テスト全体で 30 秒近い) ので、PATH に bd / br があっても既定の
  suite では起動しない。env HUMANQ_TEST_BD / HUMANQ_TEST_BR が無ければ skip (理由に変数名を出す)
- env の値は起動コマンドの文字列。例: "bd"、"br"、起動の前置き付きの "<launcher> <args> -- br"。
  "1" なら PATH の bd / br を使う。立てても --version が通らなければ skip
- 本物は一時 dir の使い捨てのプロジェクトでだけ打つ (利用者のプロジェクトには触れない)。
  BEADS_* / BD_* / BR_* の env は外して、手元の設定が別のプロジェクトを指さないようにする
- 失敗の文には、どの CLI のどの版でどの癖が食い違ったかを出す
- コメントの並びは、CLI の生の並びを assert しない (本物の br は並びが決まっていない)。bd は backend の
  comments() が (created_at, id) に依らず書いた順で返すことを、br は CLI の値 (id・created_at) を
  (created_at, id) で並べると書いた順になることを確かめる。br の backend の並べ替え (backend.br_thread) は
  tests/test_backend.py の BrThread と tests/test_br_read.py が偽の br で固定している
- br の読み (BrBackend.load) は、偽物にも本物にも通し、thread が書いた順 (同じ秒の 3 件・時刻をずらした 1 件・
  本物は JSONL から取り込み直した後の 1 件)・コメント 0 件が []・空のプロジェクトが ([], []) になることを確かめる。
  偽の br に sync は無いので、取り込み直しの脚は本物だけ
- 偽の br が断る形 (REFUSED) は、本物の br も非 0 で断る。偽物の方が厳しい形は表に入れず、REFUSED の上のコメントに残す
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_beads.py"
SERVER_DIR = Path(os.environ.get("HUMANQ_SERVER_DIR") or HERE.parent / "server")
sys.path.insert(0, str(SERVER_DIR))
import backend  # noqa: E402

sys.path.insert(0, str(HERE))                   # real_cli (`-m unittest tests.test_cli_contract` のように親から指したときも読む)
from real_cli import clean_env, real_command, skip_reason  # noqa: E402

SUBJECTS = ("fake-bd", "fake-br", "real-bd", "real-br")


def install_fake(bin_dir: str, prog: str) -> str:
    """偽物を prog の名前で置く (偽物は argv[0] の basename で振る舞いを決める)。"""
    _, _, body = FAKE.read_text(encoding="utf-8").partition("\n")
    shebang = "#!/usr/bin/env python3" if re.search(r"\s", sys.executable) else "#!" + sys.executable
    dst = os.path.join(bin_dir, prog)
    Path(dst).write_text(shebang + "\n" + body, encoding="utf-8")
    os.chmod(dst, 0o755)
    return dst


class ContractBase(unittest.TestCase):
    SUBJECT = ""          # SUBJECTS のどれか (サブクラスが決める)
    cmd: list[str]
    version = "?"

    @classmethod
    def setUpClass(cls):
        if not cls.SUBJECT:
            raise unittest.SkipTest("基底クラス")
        kind = cls.SUBJECT.split("-")[1]
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-contract-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        if cls.SUBJECT.startswith("fake"):
            bin_dir = os.path.join(cls.tmp, "bin")
            os.makedirs(bin_dir)
            cls.cmd = [install_fake(bin_dir, kind)]
        else:
            real = real_command(kind)
            if real is None:
                raise unittest.SkipTest(skip_reason(kind))
            cls.cmd = real
        if cls.SUBJECT.startswith("fake"):
            cls.version = "偽物 (tests/fake_beads.py)"
        else:
            v = subprocess.run([*cls.cmd, "--version"], capture_output=True, text=True, timeout=60, env=clean_env())
            cls.version = (v.stdout or v.stderr).strip().splitlines()[0]
        cls.kind = kind
        cls.proj = cls.new_project("proj")
        cls._empty: str | None = None
        cls.n = 0

    @classmethod
    def new_project(cls, name: str) -> str:
        proj = os.path.join(cls.tmp, name)
        os.makedirs(proj)
        if cls.kind == "bd":
            subprocess.run(["git", "init", "-q", proj], check=True, env=clean_env())
            args = ["init", "--non-interactive", "--quiet", "--skip-agents", "--skip-hooks", "-p", "hq"]
        else:
            args = ["init"]
        p = subprocess.run([*cls.cmd, *args], cwd=proj, capture_output=True, text=True, timeout=120, env=clean_env())
        if p.returncode != 0:
            raise RuntimeError(f"[{cls.SUBJECT} {cls.version}] init に失敗: {p.stderr or p.stdout}")
        return proj

    @property
    def empty(self) -> str:
        """チケットが 1 件も無いプロジェクト (遅延で作る)。"""
        cls = type(self)
        if cls._empty is None:
            cls._empty = cls.new_project("empty")
        return cls._empty

    # ---- 呼び出し -------------------------------------------------------------------------

    def m(self, quirk: str) -> str:
        """失敗の文の頭。どの CLI のどの版でどの癖が食い違ったか。"""
        return f"[{self.SUBJECT} / {self.version}] 癖「{quirk}」が食い違った"

    def run_cli(self, *args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([*self.cmd, *args], cwd=cwd or self.proj, capture_output=True, text=True,
                              timeout=120, env=clean_env())

    def load(self, text: str, quirk: str):
        """JSON でない出力は、例外ではなく assertion の失敗にする (形が崩れた変異を ERROR にしない)。"""
        try:
            return json.loads(text)
        except ValueError:
            self.fail(f"{self.m(quirk)}: JSON ではない出力 {text[:200]!r}")

    def ok(self, quirk: str, *args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
        p = self.run_cli(*args, cwd=cwd)
        self.assertEqual(p.returncode, 0, f"{self.m(quirk)}: {self.kind} {args} が rc={p.returncode}: {p.stderr or p.stdout}")
        return p

    def jok(self, quirk: str, *args: str, cwd: str | None = None):
        return self.load(self.ok(quirk, *args, "--json", cwd=cwd).stdout, quirk)

    def create(self, title: str = "q", *, labels: str = "", prio: int | None = None, metadata: dict | None = None,
               cwd: str | None = None) -> str:
        args = ["create", title]
        if prio is not None:
            args += ["-p", str(prio)]
        if labels:
            args += ["-l", labels]
        if metadata is not None:
            type(self).n += 1
            path = os.path.join(self.tmp, f"meta-{type(self).n}.json")
            Path(path).write_text(json.dumps(metadata), encoding="utf-8")
            args += ["--metadata", "@" + path]
        out = self.jok("create", *args, cwd=cwd)
        item = out[0] if isinstance(out, list) and len(out) == 1 else out
        self.assertIsInstance(item, dict, self.m("create --json の形"))
        self.assertIn("id", item, self.m("create --json の形"))
        return item["id"]

    def write_file(self, text: str) -> str:
        type(self).n += 1
        path = os.path.join(self.tmp, f"body-{type(self).n}.txt")
        Path(path).write_text(text, encoding="utf-8")
        return path

    def list_rows(self, quirk: str, *args: str) -> list:
        """`list --json` の行 (bd は配列、br は envelope の issues)。形が違えば assertion の失敗。"""
        out = self.jok(quirk, "list", *args)
        if self.kind == "br":
            self.assertIsInstance(out, dict, self.m(quirk))
            self.assertIn("issues", out, self.m(quirk))
            out = out["issues"]
        self.assertIsInstance(out, list, self.m(quirk))
        return out

    def row_of(self, quirk: str, iid: str, *args: str) -> dict:
        rows = [r for r in self.list_rows(quirk, *args) if isinstance(r, dict) and r.get("id") == iid]
        self.assertEqual(len(rows), 1, f"{self.m(quirk)}: {iid} の行が {len(rows)} 件")
        return rows[0]

    def comment_rows(self, quirk: str, iid: str) -> list[dict]:
        out = self.jok(quirk, "comments", iid)
        self.assertIsInstance(out, list, self.m(quirk))
        for c in out:
            self.assertIsInstance(c, dict, self.m(quirk))
        return out

    def add_comment(self, iid: str, text: str, actor: str = "user/humanq", cwd: str | None = None) -> dict:
        """bd は humanq と同じく --json 無しで書く。br は --json で書いた comment を返す (bd の -f 付きの --json は、
        本物は JSON を返すが偽物は返さないので、bd では --json を付けない)。"""
        path = self.write_file(text)
        if self.kind == "bd":
            self.ok("comments add", "--actor", actor, "comments", "add", iid, "-f", path, cwd=cwd)
            return {}
        out = self.jok("comments add", "--actor", actor, "comments", "add", iid, "-f", path, cwd=cwd)
        self.assertIsInstance(out, dict, self.m("comments add --json の形"))
        return out


# ---- bd の癖 ---------------------------------------------------------------------------------

class BdQuirks:
    def test_human_list_is_null_when_empty(self):
        p = self.ok("human list が 0 件で null", "human", "list", "--json", cwd=self.empty)
        self.assertEqual(p.stdout.strip(), "null", self.m("human list が 0 件で null"))

    def test_plain_list_is_empty_array_when_empty(self):
        p = self.ok("list が 0 件で []", "list", "--json", cwd=self.empty)
        self.assertEqual(self.load(p.stdout, "list が 0 件で []"), [], self.m("list が 0 件で []"))

    def test_human_list_rejects_label(self):
        q = "human list --label の拒否"
        self.create("x", labels="human")
        p = self.run_cli("human", "list", "--label", "human", "--json")
        self.assertNotEqual(p.returncode, 0, self.m(q))
        self.assertIn("unknown flag: --label", p.stderr, self.m(q))
        self.assertEqual(p.stdout.strip(), "", self.m(q))

    def test_human_list_returns_array_with_labels(self):
        q = "human list の行の形"
        iid = self.create("x", labels="zeta,human")
        rows = self.load(self.ok(q, "human", "list", "--json").stdout, q)
        self.assertIsInstance(rows, list, self.m(q))
        mine = [r for r in rows if isinstance(r, dict) and r.get("id") == iid]
        self.assertEqual(len(mine), 1, self.m(q))
        self.assertEqual(mine[0].get("labels"), ["human", "zeta"], self.m("labels は名前の順"))

    def test_metadata_is_object(self):
        q = "metadata が object"
        meta = {"humanq": {"v": 1, "ask": "どちら?"}}
        iid = self.create("x", labels="human", metadata=meta)
        rows = self.load(self.ok(q, "human", "list", "--json").stdout, q)
        mine = [r for r in rows if isinstance(r, dict) and r.get("id") == iid]
        self.assertEqual(len(mine), 1, self.m(q))
        self.assertIsInstance(mine[0].get("metadata"), dict, self.m(q))
        self.assertEqual(mine[0]["metadata"], meta, self.m(q))
        row = self.row_of(q, iid)
        self.assertEqual(row.get("metadata"), meta, self.m(q))
        self.assertEqual(backend.BdBackend.question_raw(row.get("metadata")), meta["humanq"], self.m(q))

    def test_metadata_key_absent_without_value(self):
        q = "値が無い metadata の key は出ない"
        iid = self.create("x", labels="human")
        self.assertNotIn("metadata", self.row_of(q, iid), self.m(q))

    def test_dash_C_points_at_the_project(self):
        q = "-C でプロジェクトを指す"
        iid = self.create("x", labels="human")
        p = self.ok(q, "-C", self.proj, "list", "--json", cwd=self.tmp)
        ids = [r.get("id") for r in self.load(p.stdout, q) if isinstance(r, dict)]
        self.assertIn(iid, ids, self.m(q))

    def test_answered_list_argv_is_accepted(self):
        q = "回答済みの一覧の argv (カンマ区切りの status・--flat・-n)"
        iid = self.create("x", labels="human,answered")
        self.ok(q, "update", iid, "--status", "deferred")
        argv = ["list", "--status", backend.ANSWERED_STATUSES, "--label", "answered", "--flat", "-n", "0"]
        rows = self.load(self.ok(q, *argv, "--json").stdout, q)
        self.assertIsInstance(rows, list, self.m(q))
        self.assertIn(iid, [r.get("id") for r in rows if isinstance(r, dict)], self.m(q))

    def test_comments_are_oldest_first_and_have_string_ids(self):
        q = "comments が古い順 (id は文字列)"
        iid = self.create("x")
        texts = ["one", "two", "three"]
        for t in texts:
            self.add_comment(iid, t)
        rows = self.comment_rows(q, iid)
        self.assertEqual([c.get("text") for c in rows], texts, self.m(q))
        for c in rows:
            for k in ("id", "issue_id", "author", "text", "created_at"):
                self.assertIn(k, c, self.m(q))
            self.assertIsInstance(c["id"], str, self.m(q))
            self.assertEqual(c["issue_id"], iid, self.m(q))

    def test_actor_becomes_author(self):
        q = "--actor が comment の author に入る"
        iid = self.create("x")
        self.add_comment(iid, "answer\n", actor="user/humanq")
        rows = self.comment_rows(q, iid)
        self.assertEqual([c.get("author") for c in rows], ["user/humanq"], self.m(q))
        self.assertEqual([c.get("text") for c in rows], ["answer\n"], self.m(q))

    def test_backend_comments_follow_written_order(self):
        """同じ秒に 2 件・時刻をずらして 1 件を書き、backend の comments() が書いた順で返す。"""
        q = "backend の comments() が書いた順"

        def run(repo, *args):
            p = self.run_cli("-C", repo, *args)
            if p.returncode != 0:
                raise backend.BdError((p.stderr or p.stdout).strip())
            return p.stdout

        be = backend.BdBackend(run=run)
        written: dict[str, list[str]] = {}
        same_second = False
        for attempt in range(3):               # 同じ秒に収まる組を最大 3 回試す (遅い CLI では収まらないことがある)
            iid = self.create(f"x{attempt}")
            texts = [f"a{attempt}", f"b{attempt}"]
            for t in texts:
                path = self.write_file(t)
                be.add_comment(self.proj, iid, path)
            written[iid] = texts
            got = self.comment_rows(q, iid)
            if len(got) == 2 and got[0].get("created_at") == got[1].get("created_at"):
                same_second = True
                break
        iid = next(reversed(written))
        time.sleep(1.1)
        late = self.write_file("late")
        be.add_comment(self.proj, iid, late)
        written[iid].append("late")
        got = be.comments(self.proj, iid)
        self.assertEqual(len(got), len(written[iid]), self.m(q))
        self.assertEqual([c["text"] for c in got], written[iid],
                         f"{self.m(q)} (同じ秒の組が取れたか: {same_second})")


# ---- br の癖 ---------------------------------------------------------------------------------

ISO_SEC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")


class BrQuirks:
    def assert_usage(self, quirk: str, args: list[str], needle: str):
        p = self.run_cli(*args)
        self.assertEqual(p.returncode, 2, f"{self.m(quirk)}: rc={p.returncode} {p.stderr!r} {p.stdout!r}")
        self.assertIn(needle, p.stderr, self.m(quirk))
        self.assertEqual(p.stdout.strip(), "", self.m(quirk))

    def test_dash_C_is_rejected(self):
        self.assert_usage("-C の拒否", ["-C", self.proj, "list", "--json"], "unexpected argument '-C' found")

    def test_human_subcommand_is_rejected(self):
        self.assert_usage("human の拒否", ["human", "list", "--json"], "unrecognized subcommand 'human'")

    def test_list_flat_is_rejected(self):
        self.assert_usage("list --flat の拒否", ["list", "--flat", "--json"], "unexpected argument '--flat' found")

    def test_list_dash_n_is_rejected(self):
        self.assert_usage("list -n の拒否", ["list", "-n", "10", "--json"], "unexpected argument '-n' found")

    def test_update_metadata_is_rejected(self):
        iid = self.create("x")
        self.assert_usage("update --metadata の拒否", ["update", iid, "--metadata", "{}"],
                          "unexpected argument '--metadata' found")

    def test_update_set_metadata_is_rejected(self):
        iid = self.create("x")
        self.assert_usage("update --set-metadata の拒否", ["update", iid, "--set-metadata", "k=v"],
                          "unexpected argument '--set-metadata' found")

    def test_empty_list_is_an_envelope(self):
        q = "list --json が 0 件でも envelope"
        out = self.jok(q, "list", cwd=self.empty)
        self.assertEqual(out, {"issues": [], "total": 0, "limit": 0, "offset": 0, "has_more": False}, self.m(q))

    def test_list_envelope_keys(self):
        q = "list --json の envelope の key"
        self.create("x")
        out = self.jok(q, "list")
        self.assertIsInstance(out, dict, self.m(q))
        self.assertEqual(sorted(out), ["has_more", "issues", "limit", "offset", "total"], self.m(q))
        self.assertIsInstance(out["issues"], list, self.m(q))

    def test_comma_status_is_a_validation_error_on_stdout(self):
        q = "list -s のカンマ区切りの拒否"
        self.create("x")
        p = self.run_cli("list", "-s", "open,in_progress", "--json")
        self.assertNotEqual(p.returncode, 0, self.m(q))
        out = self.load(p.stdout, q)
        self.assertIsInstance(out, dict, self.m(q))
        self.assertIn("error", out, self.m(q))
        self.assertEqual(out["error"].get("code"), "VALIDATION_FAILED", self.m(q))

    def test_repeated_status_is_accepted(self):
        q = "list -s の繰り返しは通る"
        a = self.create("a", labels="rs")
        b = self.create("b", labels="rs")
        self.ok(q, "update", b, "--status", "in_progress")
        ids = [r["id"] for r in self.list_rows(q, "-l", "rs", "-s", "open", "-s", "in_progress")]
        self.assertEqual(sorted(ids), sorted([a, b]), self.m(q))

    def test_default_list_excludes_only_closed(self):
        q = "list が既定で closed だけを除く"
        ids = {s: self.create(s, labels="dflt") for s in ("open", "in_progress", "blocked", "deferred", "closed")}
        for s in ("in_progress", "blocked", "deferred"):
            self.ok(q, "update", ids[s], "--status", s)
        self.ok(q, "close", ids["closed"])
        got = {r["id"]: r["status"] for r in self.list_rows(q) if r.get("id") in ids.values()}
        self.assertEqual(sorted(got.values()), ["blocked", "deferred", "in_progress", "open"], self.m(q))
        self.assertNotIn(ids["closed"], got, self.m(q))

    def test_label_filter_keeps_deferred(self):
        q = "list -l が deferred を含む"
        a = self.create("a", labels="lf")
        b = self.create("b", labels="lf")
        c = self.create("c", labels="other-lf")
        self.ok(q, "update", b, "--status", "deferred")
        got = sorted(r["id"] for r in self.list_rows(q, "-l", "lf"))
        self.assertEqual(got, sorted([a, b]), self.m(q))
        self.assertNotIn(c, got, self.m(q))

    def test_default_label_list_shows_every_unfinished_status(self):
        """humanq の br の一覧は -s を付けずに読む。既定が「closed と tombstone だけを除く」ことに頼っている。"""
        q = "list -l の既定が closed と tombstone だけを除き、draft・pinned・独自の status を含む"
        shown = ("open", "deferred", "draft", "pinned", "review", "in-progress")
        ids = {s: self.create(s, labels="unf") for s in (*shown, "closed", "tombstone")}
        for s in shown[1:]:
            self.ok(q, "update", ids[s], "--status", s)
        self.ok(q, "close", ids["closed"])
        self.ok(q, "delete", ids["tombstone"])
        got = {r["id"]: r.get("status") for r in self.list_rows(q, "-l", "unf", "--limit", "0")}
        self.assertEqual(sorted(got), sorted(ids[s] for s in shown), self.m(q))
        self.assertEqual(sorted(got.values()), sorted(shown), self.m(q))
        stored = {r["id"]: r.get("status") for r in self.list_rows(q, "-l", "unf", "-s", "tombstone")}
        self.assertEqual(stored, {ids["tombstone"]: "tombstone"}, f"{self.m(q)}: delete が tombstone にしていない")

    def test_update_takes_any_status_but_the_terminal_ones(self):
        q = "update --status は任意の文字列を受け付け、closed と tombstone は断る (exit 4)"
        iid = self.create("x")
        type(self).n += 1
        custom = f"custom-{type(self).n}"
        self.ok(q, "update", iid, "--status", custom)
        self.assertEqual(self.row_of(q, iid).get("status"), custom, self.m(q))
        for terminal in ("closed", "tombstone"):
            p = self.run_cli("update", iid, "--status", terminal, "--json")
            self.assertEqual(p.returncode, 4, f"{self.m(q)}: {terminal}: rc={p.returncode} {p.stdout!r} {p.stderr!r}")
            out = self.load(p.stdout, q)
            self.assertIsInstance(out, dict, self.m(q))
            self.assertIsInstance(out.get("error"), dict, self.m(q))
            self.assertEqual(out["error"].get("code"), "VALIDATION_FAILED", self.m(q))
            self.assertIn(f"refusing to {'close' if terminal == 'closed' else 'tombstone'} via", out["error"].get("message", ""),
                          self.m(q))
        self.assertEqual(self.row_of(q, iid).get("status"), custom, f"{self.m(q)}: 断った update で status が変わった")

    def test_status_filter_takes_builtins_statuses_in_use_and_all(self):
        q = "list -s は組み込み・使われている status・all を受け付け、ほかは VALIDATION_FAILED (exit 4)"
        type(self).n += 1
        custom = f"unused-{type(self).n}"
        p = self.run_cli("list", "-s", custom, "--json")
        self.assertEqual(p.returncode, 4, f"{self.m(q)}: rc={p.returncode} {p.stdout!r}")
        out = self.load(p.stdout, q)
        self.assertIsInstance(out, dict, self.m(q))
        self.assertIsInstance(out.get("error"), dict, self.m(q))
        self.assertEqual(out["error"].get("code"), "VALIDATION_FAILED", self.m(q))
        self.assertIn(f"unknown status '{custom}'", out["error"].get("message", ""), self.m(q))
        for builtin in ("draft", "pinned", "tombstone", "closed"):          # 組み込みは、どのチケットにも無くても通る
            out = self.jok(q, "list", "-s", builtin, cwd=self.empty)        # 共有のプロジェクトは他のテストが status を使う
            self.assertIsInstance(out, dict, f"{self.m(q)}: -s {builtin}")
            self.assertEqual(out.get("issues"), [], f"{self.m(q)}: -s {builtin}")
        a = self.create("a", labels="sf")
        self.ok(q, "update", a, "--status", custom)
        self.assertEqual([r["id"] for r in self.list_rows(q, "-s", custom)], [a], f"{self.m(q)}: 使われた status で絞れない")
        b = self.create("b", labels="sf")
        c = self.create("c", labels="sf")
        self.ok(q, "close", b)
        self.ok(q, "delete", c)
        for flag in (("-s", "all"), ("--all",)):
            got = sorted(r["id"] for r in self.list_rows(q, "-l", "sf", *flag))
            self.assertEqual(got, sorted([a, b]), f"{self.m(q)}: {' '.join(flag)} は closed を含み tombstone を含まない")

    def test_show_is_an_array_with_comments_and_agent_context(self):
        q = "show <ids> --json が comments と agent_context を含む"
        iid = self.create("x")
        ctx = {"options": ["x", "y"]}
        self.ok(q, "update", iid, "--agent-context", "@" + self.write_file(json.dumps(ctx)))
        self.add_comment(iid, "c1")
        out = self.jok(q, "show", iid)
        self.assertIsInstance(out, list, self.m(q))
        self.assertEqual(len(out), 1, self.m(q))
        item = out[0]
        self.assertIsInstance(item, dict, self.m(q))
        self.assertIsInstance(item.get("agent_context"), str, self.m(q))
        self.assertEqual(self.load(item["agent_context"], q), ctx, self.m(q))
        self.assertEqual([c.get("text") for c in item.get("comments", [])], ["c1"], self.m(q))
        self.assertNotIn("metadata", item, self.m("show に metadata が無い"))

    def test_show_without_comments_has_none_or_empty(self):
        """コメントも agent_context も無いチケット。本物は comments の key を出さず、偽物は [] を出す。読む側は
        どちらも「無い」と読むので、ここでは「無いか空」までを契約にする。"""
        q = "コメントの無い show の comments"
        iid = self.create("x")
        item = self.jok(q, "show", iid)[0]
        self.assertIsInstance(item, dict, self.m(q))
        self.assertEqual(item.get("comments", []), [], self.m(q))
        self.assertNotIn("agent_context", item, self.m(q))

    def test_show_many_ids_in_one_call(self):
        q = "show に複数の id"
        a = self.create("a")
        b = self.create("b")
        out = self.jok(q, "show", a, b)
        self.assertIsInstance(out, list, self.m(q))
        self.assertEqual([x.get("id") for x in out], [a, b], self.m(q))

    def test_show_missing_id_is_not_found_exit_3(self):
        q = "show の無い id で exit 3"
        p = self.run_cli("show", "zz-999", "--json")
        self.assertEqual(p.returncode, 3, f"{self.m(q)}: {p.stderr!r} {p.stdout!r}")
        out = self.load(p.stdout, q)
        self.assertIsInstance(out, dict, self.m(q))
        self.assertEqual(out.get("error", {}).get("code"), "ISSUE_NOT_FOUND", self.m(q))

    def test_one_missing_among_existing_fails_all(self):
        q = "id が 1 つでも無ければ全体が失敗する"
        a = self.create("a")
        p = self.run_cli("show", a, "zz-999", "--json")
        self.assertEqual(p.returncode, 3, self.m(q))
        out = self.load(p.stdout, q)
        self.assertNotIsInstance(out, list, self.m(q))
        self.assertEqual(out.get("error", {}).get("code"), "ISSUE_NOT_FOUND", self.m(q))

    def test_comment_ids_are_increasing_ints_and_order_is_recoverable(self):
        """生の並びは assert しない。(created_at, id) で並べると書いた順になることだけを確かめる。"""
        q = "comments の id が int で単調に増え、(created_at, id) で書いた順に戻せる"
        iid = self.create("x")
        texts = ["one", "two"]
        for t in texts:
            self.add_comment(iid, t)
        time.sleep(1.1)
        texts.append("three")
        self.add_comment(iid, "three")
        rows = self.comment_rows(q, iid)
        self.assertEqual(len(rows), 3, self.m(q))
        for c in rows:
            self.assertIs(type(c.get("id")), int, self.m(q))
            self.assertRegex(c.get("created_at", ""), ISO_SEC, self.m(q))
            self.assertEqual(c.get("issue_id"), iid, self.m(q))
        by_text = {c["text"]: c["id"] for c in rows}
        self.assertLess(by_text["one"], by_text["two"], self.m(q))
        self.assertLess(by_text["two"], by_text["three"], self.m(q))
        ordered = sorted(rows, key=lambda c: (c["created_at"], c["id"]))
        self.assertEqual([c["text"] for c in ordered], texts, self.m(q))

    def test_comment_ids_increase_across_issues(self):
        q = "comments の id が issue をまたいで増える"
        a = self.create("a")
        b = self.create("b")
        c1 = self.add_comment(a, "x")
        c2 = self.add_comment(b, "y")
        self.assertGreater(c2.get("id", 0), c1.get("id", 0), self.m(q))

    # ---- BrBackend.load を通した読み (偽物にも本物にも同じ assertion) ------------------------------------

    def br_backend(self) -> "backend.BrBackend":
        """この CLI (偽物か本物) を 1 回ずつ打つ BrBackend。cwd はプロジェクトの dir。"""
        def run(repo: str, *args: str) -> str:
            p = self.run_cli(*args, cwd=repo)
            if p.returncode != 0:
                raise backend.CliExit((p.stderr or p.stdout).strip()[:800] or f"br exit {p.returncode}", p.returncode)
            return p.stdout
        return backend.BrBackend(run=run)

    def read(self, proj: str, quirk: str):
        """BrBackend.load。読みの失敗 (BdError) は例外ではなく assertion の失敗にする (変異で CLI の打ち方が崩れても ERROR にしない)。"""
        try:
            return self.br_backend().load(proj)
        except backend.BdError as e:
            self.fail(f"{self.m(quirk)}: BrBackend.load が失敗: {e}")

    def fresh_project(self) -> str:
        """読みの全体 (human・answered の一覧) を assert するので、共有のプロジェクトではなく専用のプロジェクトを使う。"""
        type(self).n += 1
        return self.new_project(f"load-{type(self).n}")

    def real_br(self) -> bool:
        return not self.SUBJECT.startswith("fake")

    def test_backend_load_thread_follows_written_order(self):
        """同じ秒に 3 件・時刻をずらして 1 件・(本物は) JSONL から取り込み直した後に 1 件を書き、BrBackend.load の
        thread が書いた順で返る。CLI の生の並びは決まっていない (偽物は新しい順、本物は取り込み直すと崩れる)。"""
        q = "BrBackend.load の thread が書いた順"
        proj = self.fresh_project()
        iid, texts, same_second = "", [], False
        for attempt in range(5):                # 3 件が同じ秒に収まる組を最大 5 回試す (遅い CLI では収まらないことがある)
            iid = self.create(f"x{attempt}", labels="answered", cwd=proj)
            texts = [f"a{attempt}", f"b{attempt}", f"c{attempt}"]
            for t in texts:
                self.add_comment(iid, t, cwd=proj)
            raw = self.load(self.ok(q, "comments", iid, "--json", cwd=proj).stdout, q)
            self.assertIsInstance(raw, list, self.m(q))
            self.assertEqual(len(raw), 3, self.m(q))
            for c in raw:
                self.assertIsInstance(c, dict, self.m(q))
            if len({c.get("created_at") for c in raw}) == 1:
                same_second = True
                break
        if not self.real_br():                  # 偽物は速いので、同じ秒の組が取れないなら偽物の側が壊れている
            self.assertTrue(same_second, f"{self.m(q)}: 5 回試しても 3 件が同じ秒に収まらなかった")
        note = f"{self.m(q)} (同じ秒の組が取れたか: {same_second})"

        def thread_of_load() -> list[str | None]:
            human, answered = self.read(proj, q)
            self.assertEqual(human, [], note)
            mine = [r for r in answered if r["id"] == iid]
            self.assertEqual(len(mine), 1, note)
            thread = mine[0].get("thread")
            self.assertIsInstance(thread, list, note)
            return [c.get("text") for c in thread]

        self.assertEqual(thread_of_load(), texts, note)
        time.sleep(1.1)                         # 時刻をずらす
        texts.append("late")
        self.add_comment(iid, "late", cwd=proj)
        self.assertEqual(thread_of_load(), texts, note)
        if self.real_br():                      # 偽物に sync は無い。本物だけ JSONL に書き出して取り込み直す (DB を作り直す)
            self.ok(q, "sync", "--flush-only", cwd=proj)
            self.ok(q, "sync", "--import-only", "--rebuild", cwd=proj)
            self.assertEqual(thread_of_load(), texts, f"{note}: 取り込み直した後")
            texts.append("after-import")
            self.add_comment(iid, "after-import", cwd=proj)
            self.assertEqual(thread_of_load(), texts, f"{note}: 取り込み直した後に書いた 1 件")

    def test_backend_load_reads_threads_and_questions_of_each_ticket(self):
        """コメント 0 件のチケットの thread は []、3 件のチケットは書いた順。human のチケットは thread を持たない。"""
        q = "BrBackend.load の thread (3 件・0 件)"
        proj = self.fresh_project()
        with_comments = self.create("with", labels="answered", cwd=proj)
        without = self.create("without", labels="answered", cwd=proj)
        asked = self.create("asked", labels="human", cwd=proj)
        ctx = {"humanq": {"ask": "どちら?"}}
        self.ok(q, "update", asked, "--agent-context", "@" + self.write_file(json.dumps(ctx)), cwd=proj)
        texts = ["first", "second", "third"]
        for t in texts:
            self.add_comment(with_comments, t, cwd=proj)
        human, answered = self.read(proj, q)
        self.assertEqual([r["id"] for r in human], [asked], self.m(q))
        self.assertEqual(human[0]["question_raw"], ctx["humanq"], self.m(q))
        self.assertNotIn("thread", human[0], self.m(q))
        by = {r["id"]: r for r in answered}
        self.assertEqual(sorted(by), sorted([with_comments, without]), self.m(q))
        self.assertEqual(len(by), 2, self.m(q))
        self.assertIsInstance(by[with_comments].get("thread"), list, self.m(q))
        self.assertEqual([c.get("text") for c in by[with_comments]["thread"]], texts, self.m(q))
        self.assertEqual(by[without].get("thread"), [], self.m(q))

    def test_backend_load_on_an_empty_project_is_empty(self):
        q = "BrBackend.load が空のプロジェクトで ([], [])"
        self.assertEqual(self.read(self.empty, q), ([], []), self.m(q))

    # ---- 偽物が断る形は、本物も断る ---------------------------------------------------------------------

    # 偽の br (tests/fake_beads.py) が usage エラー・VALIDATION・ISSUE_NOT_FOUND などで断る形。偽物が断るのに本物は通す
    # 形があると、偽物の上で緑のテストが本物では別の経路に入る。{id} は専用のプロジェクトのチケット、{nofile} は無いファイル。
    # 偽物の方が厳しい形 (偽物は断るが本物 0.7.6 は通す) はここに入れない (偽物の側の食い違いで、偽物を直す段で
    # 揃える): show に id 無し (本物は exit 0 でチケットを出す)・label add に余分な引数 (本物は複数の label を付ける)・
    # comments add -f に余分な位置引数 (本物は -f の本文で書く)・close -r に値無し (本物は通す)
    REFUSED = {
        "subcommand 無し": [],
        "--actor に値無し": ["--actor"],
        "create にタイトル無し": ["create"],
        "create にタイトル 2 つ": ["create", "a", "b"],
        "create の知らない flag": ["create", "a", "--nope"],
        "create -p に値無し": ["create", "a", "-p"],
        "update に id 無し": ["update"],
        "update に id 2 つ": ["update", "{id}", "{id}", "--title", "t"],
        "update の知らない flag": ["update", "{id}", "--nope"],
        "update の無い id": ["update", "zz-999", "--title", "t"],
        "update --agent-context @無いファイル": ["update", "{id}", "--agent-context", "@{nofile}"],
        "close に id 無し": ["close"],
        "close に id 2 つ": ["close", "{id}", "{id}"],
        "close の知らない flag": ["close", "{id}", "--nope"],
        "close の無い id": ["close", "zz-999"],
        "delete に id 無し": ["delete"],
        "delete の無い id": ["delete", "zz-999"],
        "list に位置引数": ["list", "x"],
        "list の知らない flag": ["list", "--nope"],
        "list -s に値無し": ["list", "-s"],
        "list --limit に値無し": ["list", "--limit"],
        "show の知らない flag": ["show", "{id}", "--nope"],
        "label の知らない op": ["label", "zap", "{id}", "x"],
        "label に op 無し": ["label"],
        "label add に label 無し": ["label", "add", "{id}"],
        "label add の無い id": ["label", "add", "zz-999", "a"],
        "comments add -f の無いファイル": ["comments", "add", "{id}", "-f", "{nofile}"],
        "comments add -f に値無し": ["comments", "add", "{id}", "-f"],
        "comments add の無い id": ["comments", "add", "zz-999", "t"],
        "comments add に id 無し": ["comments", "add"],
        "comments add に本文無し": ["comments", "add", "{id}"],
        "comments に id 無し": ["comments"],
        "comments に id 2 つ": ["comments", "{id}", "{id}"],
        "comments の無い id": ["comments", "zz-999"],
        "init に位置引数": ["init", "zz"],
        "init の知らない flag": ["init", "--nope"],
    }

    def test_every_form_the_fake_refuses_is_refused_by_the_real_one(self):
        q = "偽物が断る形は本物も断る (非 0 で終わり、チケットを増やさない)"
        proj = self.fresh_project()
        iid = self.create("x", cwd=proj)
        nofile = os.path.join(self.tmp, "no-such-file.txt")
        for name, form in self.REFUSED.items():
            argv = [a.format(id=iid, nofile=nofile) for a in form]
            with self.subTest(form=name):
                p = self.run_cli(*argv, "--json", cwd=proj)
                self.assertNotEqual(p.returncode, 0, f"{self.m(q)}: 「{name}」 {argv} が通った: {p.stdout[:200]!r}")
        out = self.jok(q, "list", cwd=proj)
        self.assertIsInstance(out, dict, self.m(q))
        self.assertEqual([r.get("id") for r in out.get("issues", [])], [iid],
                         f"{self.m(q)}: 断った形のどれかがチケットを作った・消した")

    def test_actor_becomes_author(self):
        q = "--actor が comment の author に入る"
        iid = self.create("x")
        added = self.add_comment(iid, "answer\n", actor="user/humanq")
        self.assertEqual(added.get("author"), "user/humanq", self.m(q))
        self.assertEqual([c.get("author") for c in self.comment_rows(q, iid)], ["user/humanq"], self.m(q))

    def test_label_add_and_remove_report_status_with_exit_0(self):
        q = "label add / remove の exists / not_found が exit 0"
        iid = self.create("x")
        row = {"issue_id": iid, "label": "human"}
        self.assertEqual(self.jok(q, "label", "add", iid, "human"), [{"status": "added", **row}], self.m(q))
        self.assertEqual(self.jok(q, "label", "add", iid, "human"), [{"status": "exists", **row}], self.m(q))
        self.assertEqual(self.jok(q, "label", "remove", iid, "human"), [{"status": "removed", **row}], self.m(q))
        self.assertEqual(self.jok(q, "label", "remove", iid, "human"), [{"status": "not_found", **row}], self.m(q))

    def test_agent_context_shrinking_needs_force(self):
        q = "agent_context の半分未満への書き換えは --force が要る"
        iid = self.create("x")
        # 本物の br は JSON の agent_context を詰めて (空白を除いて) 保存するので、最初から詰めた形で書く
        long_ctx = json.dumps({"q": "a long enough context string for the shrink check"}, separators=(",", ":"))
        self.ok(q, "update", iid, "--agent-context", long_ctx)
        p = self.run_cli("update", iid, "--agent-context", '{"q":"a"}')
        self.assertNotEqual(p.returncode, 0, self.m(q))
        # 理由の置き場 (stdout か stderr) は --json の有無で変わるので、両方を合わせて読む
        said = p.stdout + p.stderr
        self.assertIn("refusing to overwrite non-empty 'agent_context'", said, self.m(q))
        self.assertIn("--force", said, self.m(q))
        self.assertEqual(self.row_of(q, iid).get("agent_context"), long_ctx, f"{self.m(q)}: 拒否されたのに値が変わった")
        self.ok(q, "update", iid, "--agent-context", '{"q":"a"}', "--force")
        self.assertEqual(self.row_of(q, iid).get("agent_context"), '{"q":"a"}', self.m(q))

    def test_agent_context_is_a_string_in_list_only_where_set(self):
        q = "list --json の agent_context は文字列で、持つチケットだけに出る"
        a = self.create("a", labels="acx")
        b = self.create("b", labels="acx")
        self.ok(q, "update", a, "--agent-context", '{"q":1}')
        rows = {r["id"]: r for r in self.list_rows(q, "-l", "acx")}
        self.assertEqual(sorted(rows), sorted([a, b]), self.m(q))
        self.assertEqual(rows[a].get("agent_context"), '{"q":1}', self.m(q))
        self.assertNotIn("agent_context", rows[b], self.m(q))


for _subject in SUBJECTS:
    _mixin = BdQuirks if _subject.endswith("bd") else BrQuirks
    _name = _mixin.__name__ + "_" + _subject.replace("-", "_")
    globals()[_name] = type(_name, (_mixin, ContractBase), {"SUBJECT": _subject})
del _subject, _mixin, _name


if __name__ == "__main__":
    unittest.main()
