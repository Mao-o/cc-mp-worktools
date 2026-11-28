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
- 偽の br が断る形 (REFUSED・REFUSED_AS_LAST_WORD) は、本物の br も非 0 で断る。偽物の方が厳しい形 (本物は通す) は、
  偽物を直して ACCEPTED (本物が通す形) に移す。まだ直していないものは REFUSED の上のコメントに残す。偽物の `raise Usage(`
  の分岐を全部踏む形が REFUSED にあることは、FakeBrUsageBranches (偽物だけ) が確かめる
- 偽物の形 (comments の key・agent_context の詰め方・show に id 無し・label の複数・comments add -f・close -r) は、
  偽物と本物に同じ assertion を当てる。偽物だけが寛容な食い違い (JSON でない agent_context を本物は exit 7 で断る) は残っている
- br を git worktree で打ったときにどの DB に届くか (本体の DB・自分の DB) を、偽物と本物に同じ assertion で当て、humanq の
  判定 (registry._br_home) が同じ dir を返すことも確かめる。bd の worktree の解決は測っていないので、bd の側には無い
- 保留と保留の解除の書き込み (humanq.Store.snooze / unsnooze) を 4 通りに通し、差し替え口 (humanq.bd / humanq.br) から
  ラベルの段階の失敗を注入した送り直しで、コメントが二重にならず・ラベルが付く (外れる)・status と担当が変わらないことを確かめる
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
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
from datetime import timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_beads.py"
SERVER_DIR = Path(os.environ.get("HUMANQ_SERVER_DIR") or HERE.parent / "server")
sys.path.insert(0, str(SERVER_DIR))
import backend  # noqa: E402
import humanq  # noqa: E402
import registry  # noqa: E402

sys.path.insert(0, str(HERE))                   # real_cli (`-m unittest tests.test_cli_contract` のように親から指したときも読む)
from real_cli import clean_env, real_command, skip_reason  # noqa: E402

SUBJECTS = ("fake-bd", "fake-br", "real-bd", "real-br")


def git(cwd: str, *args: str) -> str:
    """使い捨ての repo で git を打つ。外側の GIT_* (GIT_DIR など) は外す (残っていると別の repo に worktree を足す)。
    手元の設定の hook と署名は効かせない。"""
    env = {k: v for k, v in clean_env().items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, capture_output=True, text=True, timeout=60, env=env)
    if p.returncode != 0:
        raise AssertionError(f"git {args}: {p.stderr or p.stdout}")
    return p.stdout


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
        """bd は humanq と同じく --json 無しで書く。br は --json で書いた comment を返す (bd の --json 付きの戻りは
        BdQuirks.test_comments_add_with_json_returns_the_written_comment が押さえる)。"""
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
        q = "回答済みの一覧の argv (status で絞らない・--flat・-n)"
        iid = self.create("x", labels="human,answered")
        self.ok(q, "update", iid, "--status", "deferred")
        argv = ["list", "--label", "answered", "--flat", "-n", "0"]
        rows = self.load(self.ok(q, *argv, "--json").stdout, q)
        self.assertIsInstance(rows, list, self.m(q))
        self.assertIn(iid, [r.get("id") for r in rows if isinstance(r, dict)], self.m(q))

    def test_default_list_shows_the_statuses_human_list_shows(self):
        """humanq の bd の回答済みの一覧は --status を付けずに読む。bd list の既定が bd human list と同じ status を出すことに頼る
        (bd 1.3.4 で実測: hooked・deferred・wip か未指定の独自の status は出る。closed・pinned・done か frozen の独自の status は出ない)。"""
        q = "list の既定が human list と同じ status を出す"
        proj = self.fresh_project()
        self.ok(q, "config", "set", "status.custom", "wip-x:wip,plain-x,done-x:done,frozen-x:frozen", cwd=proj)
        shown = ("open", "in_progress", "blocked", "deferred", "hooked", "wip-x", "plain-x")
        hidden = ("pinned", "done-x", "frozen-x", "closed")
        ids = {s: self.create(s, labels="human,answered", cwd=proj) for s in (*shown, *hidden)}
        for s in (*shown[1:], *hidden[:-1]):
            self.ok(q, "update", ids[s], "--status", s, cwd=proj)
        self.ok(q, "close", ids["closed"], cwd=proj)
        listed = self.jok(q, "list", "--label", "answered", "--flat", "-n", "0", cwd=proj)
        human = self.jok(q, "human", "list", cwd=proj)
        self.assertIsInstance(listed, list, self.m(q))
        self.assertIsInstance(human, list, self.m(q))
        want = sorted(ids[s] for s in shown)
        self.assertEqual(sorted(r.get("id") for r in listed if isinstance(r, dict)), want, self.m(q))
        self.assertEqual(sorted(r.get("id") for r in human if isinstance(r, dict)), want, self.m(q))

    def test_list_status_filter_rejects_an_unknown_status(self):
        q = "list --status に知らない status を書けない (exit 1)"
        p = self.run_cli("list", "--status", "no-such-status", "--json")
        self.assertEqual(p.returncode, 1, f"{self.m(q)}: rc={p.returncode} {p.stdout!r} {p.stderr!r}")
        self.assertIn('invalid status "no-such-status"', p.stderr, self.m(q))

    def fresh_project(self) -> str:
        """status.custom を設定するので、共有のプロジェクトではなく専用のプロジェクトを使う。"""
        type(self).n += 1
        return self.new_project(f"bd-fresh-{type(self).n}")

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

    def test_comments_add_with_json_returns_the_written_comment(self):
        """humanq は bd の comments add に --json を付けない (add_comment も付けない)。付けると本物は JSON を返す。
        偽物の戻りが 1 行の案内文だった食い違いを、ここで固定する。戻りは書いたコメント 1 件の object
        (bd 1.3.4 で実測。要素の key に schema_version が加わる。偽物は出さない)。"""
        q = "comments add -f --json が JSON を返す"
        iid = self.create("x")
        out = self.jok(q, "--actor", "user/humanq", "comments", "add", iid, "-f", self.write_file("answer\n"))
        self.assertIsInstance(out, dict, self.m(q))
        self.assertEqual((out.get("author"), out.get("text")), ("user/humanq", "answer\n"), self.m(q))
        self.assertEqual([c.get("text") for c in self.comment_rows(q, iid)], ["answer\n"], self.m(q))

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

    def test_show_without_comments_has_no_comments_key(self):
        """コメントも agent_context も無いチケット。本物は comments の key を出さない ([] ではない)。読む側
        (backend.br_thread) が key の無い show を 0 件と読むことは、下の test_backend_load_* が偽物にも本物にも通す。"""
        q = "コメントの無い show の comments"
        iid = self.create("x")
        shown = self.jok(q, "show", iid)
        self.assertIsInstance(shown, list, self.m(q))
        self.assertEqual(len(shown), 1, self.m(q))
        item = shown[0]
        self.assertIsInstance(item, dict, self.m(q))
        self.assertNotIn("comments", item, self.m(q))
        self.assertNotIn("agent_context", item, self.m(q))
        self.add_comment(iid, "c1")
        self.assertEqual([c.get("text") for c in self.jok(q, "show", iid)[0].get("comments", [])], ["c1"],
                         f"{self.m(q)}: コメントを書いた後は comments が出る")

    def test_agent_context_json_is_stored_compact(self):
        """本物の br は JSON の agent_context を詰めて保存する ({"q": "x"} は {"q":"x"})。show にも list にも詰めた値が出て、
        key の順は保つ。inline と @file のどちらでも同じ (偽物は書いたまま返さない)。"""
        q = "agent_context は詰めて返る"
        iid = self.create("x")
        given = '{"b": 1, "a": [1, 2], "c": {"k": "v"}}'
        stored = '{"b":1,"a":[1,2],"c":{"k":"v"}}'
        for how, args in (("inline", ["--agent-context", given]), ("@file", ["--agent-context", "@" + self.write_file(given)])):
            self.ok(q, "update", iid, *args, "--force")
            self.assertEqual(self.jok(q, "show", iid)[0].get("agent_context"), stored, f"{self.m(q)}: {how} の show")
            self.assertEqual(self.row_of(q, iid).get("agent_context"), stored, f"{self.m(q)}: {how} の list")
            self.ok(q, "update", iid, "--agent-context", "", "--force")       # 次の書き方の前に空にする (半分未満の書き換えは --force)
            self.assertNotIn("agent_context", self.row_of(q, iid), self.m(q))

    def test_show_without_ids_shows_the_last_touched_ticket(self):
        """br は id を書かない show で、最後に触ったチケットを出す (exit 0)。create・update・label add が触れる。
        まだ何も触れていないプロジェクトは VALIDATION_FAILED (exit 4)。"""
        q = "show に id 無しは最後に触ったチケット"
        proj = self.fresh_project()
        p = self.run_cli("show", "--json", cwd=proj)
        self.assertEqual(p.returncode, 4, f"{self.m(q)}: 空のプロジェクト rc={p.returncode} {p.stderr!r} {p.stdout!r}")
        self.assertEqual(self.load(p.stdout, q).get("error", {}).get("code"), "VALIDATION_FAILED", self.m(q))
        a = self.create("a", cwd=proj)
        b = self.create("b", cwd=proj)

        def last() -> list:
            out = self.jok(q, "show", cwd=proj)
            self.assertIsInstance(out, list, self.m(q))
            return [x.get("id") for x in out if isinstance(x, dict)]
        self.assertEqual(last(), [b], f"{self.m(q)}: create の後")
        self.ok(q, "update", a, "--title", "a2", cwd=proj)
        self.assertEqual(last(), [a], f"{self.m(q)}: update の後")
        self.ok(q, "label", "add", b, "x", cwd=proj)
        self.assertEqual(last(), [b], f"{self.m(q)}: label add の後")
        self.ok(q, "delete", a, "--force", cwd=proj)                         # 最後に触ったのでないチケットの delete は記録を変えない
        self.assertEqual(last(), [b], f"{self.m(q)}: ほかのチケットの delete の後")
        self.ok(q, "delete", b, "--force", cwd=proj)                         # 最後に触ったチケットを delete すると記録が消える
        p = self.run_cli("show", "--json", cwd=proj)
        self.assertEqual(p.returncode, 4, f"{self.m(q)}: 最後に触ったチケットの delete の後 rc={p.returncode} {p.stdout[:200]!r}")

    def test_label_add_and_remove_take_several_labels(self):
        q = "label add / remove は label を何個でも取る"
        iid = self.create("x")
        rows = [{"status": "added", "issue_id": iid, "label": n} for n in ("l1", "l2")]
        self.assertEqual(self.jok(q, "label", "add", iid, "l1", "l2"), rows, self.m(q))
        self.assertEqual(self.row_of(q, iid).get("labels"), ["l1", "l2"], self.m(q))
        self.assertEqual(self.jok(q, "label", "remove", iid, "l1", "zz"),
                         [{"status": "removed", "issue_id": iid, "label": "l1"},
                          {"status": "not_found", "issue_id": iid, "label": "zz"}], self.m(q))
        self.assertEqual(self.row_of(q, iid).get("labels"), ["l2"], self.m(q))

    def test_comments_add_with_a_file_ignores_the_extra_text(self):
        q = "comments add -f は本文の位置引数を無視してファイルの本文で書く"
        iid = self.create("x")
        path = self.write_file("from-file\n")
        added = self.jok(q, "--actor", "user/humanq", "comments", "add", iid, "ignored text", "-f", path)
        self.assertIsInstance(added, dict, self.m(q))
        self.assertEqual(added.get("text"), "from-file\n", self.m(q))
        self.assertEqual([c.get("text") for c in self.comment_rows(q, iid)], ["from-file\n"], self.m(q))

    def test_close_reason_flag_takes_the_next_word_even_if_it_is_json(self):
        """`close <id> -r --json` は --json が理由になって閉じる (理由の無い -r の拒否ではない)。-r が最後の語なら断る。"""
        q = "close -r の次の語は値になる"
        iid = self.create("x")
        p = self.run_cli("close", iid, "-r", "--json")
        self.assertEqual(p.returncode, 0, f"{self.m(q)}: rc={p.returncode} {p.stderr!r} {p.stdout!r}")
        self.assertEqual(self.jok(q, "show", iid)[0].get("status"), "closed", self.m(q))
        last = self.create("y")
        p = self.run_cli("close", last, "-r")
        self.assertNotEqual(p.returncode, 0, f"{self.m(q)}: -r が最後の語なのに通った")
        self.assertEqual(self.jok(q, "show", last)[0].get("status"), "open", self.m(q))

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
    # 形があると、偽物の上で緑のテストが本物では別の経路に入る。{id} は専用のプロジェクトのチケット、{nofile} は無いファイル、
    # {file} は在るファイル (id 無しの形は、ファイルが無いことでも断られるので、在るファイルで試す)。
    # 後ろに --json を付けて試す (--reason・--notes・--description は --json を値に取るので、値の無い形は付けずに試す:
    # REFUSED_AS_LAST_WORD)。偽物の方が厳しい形 (偽物は断るが本物 0.7.6 は通す) は、この表ではなく ACCEPTED に入れ、
    # 偽物を直して揃える。偽物が断る分岐 (`raise Usage(`) の全部を、この 2 つの表の形のどれかが踏むことは
    # FakeBrUsageBranches が確かめる。
    # まだ偽物が断る側の食い違い (本物は通す。humanq は使わない): 後ろに何も付けない `update` と `close` (id 無し) は、
    # 本物では最後に触ったチケットに効く (--json を付けると本物も断る)
    REFUSED = {
        "-C": ["-C", "{nofile}", "list"],
        "知らない subcommand": ["zap"],
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
        "delete --reason に値無し (後ろの --json を値に取らない)": ["delete", "{id}", "--reason"],
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
        "comments add -f に id 無し": ["comments", "add", "-f", "{file}"],
        "comments add の無い id": ["comments", "add", "zz-999", "t"],
        "comments add に id 無し": ["comments", "add"],
        "comments add に本文無し": ["comments", "add", "{id}"],
        "comments に id 無し": ["comments"],
        "comments に id 2 つ": ["comments", "{id}", "{id}"],
        "comments の無い id": ["comments", "zz-999"],
        "init に位置引数": ["init", "zz"],
        "init の知らない flag": ["init", "--nope"],
        "ready に位置引数": ["ready", "x"],
        "ready の知らない flag": ["ready", "--nope"],
        "ready --limit に数でない値": ["ready", "--limit", "x"],
        "count に位置引数": ["count", "x"],
        "count --by に知らない値": ["count", "--by", "bogus"],
    }

    # 偽の br が断る形と対になる、本物が通す形 (偽物が断っていた 4 つ。偽物は通すようにした)。後ろに --json を付けて試す。
    # {id} は専用のプロジェクトの、形ごとに新しいチケット。効果 (どのチケットに何が起きたか) は、下の個別のテストが確かめる
    ACCEPTED = {
        "show に id 無し (最後に触ったチケットを出す)": ["show"],
        "label add に label が 2 つ": ["label", "add", "{id}", "a", "b"],
        "comments add -f に余分な位置引数 (本文はファイルのもの)": ["comments", "add", "{id}", "x", "-f", "{file}"],
        "close -r に値無し (後ろの --json が理由になる)": ["close", "{id}", "-r"],
    }

    # 後ろに何も付けず、最後の語が値を取る option のときだけ断られる形 (--json を付けると、--json が値になって通るか、
    # 別の語が値の無い扱いになる)。--reason は --json を値に取るので REFUSED には入れられず、--actor は globals の位置の都合で
    # REFUSED では「subcommand 無し」の分岐に落ちる。偽物の分岐を全部踏むために、FakeBrUsageBranches もこの表を流す
    REFUSED_AS_LAST_WORD = {
        "--actor に値無し": ["--actor"],
        "close -r に値無し": ["close", "{id}", "-r"],
        "close --reason に値無し": ["close", "{id}", "--reason"],
    }

    def test_every_form_the_fake_refuses_is_refused_by_the_real_one(self):
        q = "偽物が断る形は本物も断る (非 0 で終わり、チケットを増やさない)"
        proj = self.fresh_project()
        iid = self.create("x", cwd=proj)
        nofile = os.path.join(self.tmp, "no-such-file.txt")
        body = self.write_file("x\n")
        for name, form in self.REFUSED.items():
            argv = [a.format(id=iid, nofile=nofile, file=body) for a in form]
            with self.subTest(form=name):
                p = self.run_cli(*argv, "--json", cwd=proj)
                self.assertNotEqual(p.returncode, 0, f"{self.m(q)}: 「{name}」 {argv} が通った: {p.stdout[:200]!r}")
        for name, form in self.REFUSED_AS_LAST_WORD.items():
            argv = [a.format(id=iid, nofile=nofile, file=body) for a in form]
            with self.subTest(form=name, last_word=True):
                p = self.run_cli(*argv, cwd=proj)
                self.assertNotEqual(p.returncode, 0, f"{self.m(q)}: 「{name}」 {argv} (後ろに何も付けない) が通った: {p.stdout[:200]!r}")
        out = self.jok(q, "list", cwd=proj)
        self.assertIsInstance(out, dict, self.m(q))
        self.assertEqual([r.get("id") for r in out.get("issues", [])], [iid],
                         f"{self.m(q)}: 断った形のどれかがチケットを作った・消した・閉じた")

    def test_every_form_the_fake_used_to_refuse_is_accepted_by_the_real_one(self):
        q = "偽物が断っていた形は本物が通す (exit 0)"
        proj = self.fresh_project()
        body = self.write_file("from-file\n")
        for name, form in self.ACCEPTED.items():
            iid = self.create(name, cwd=proj)                    # 形ごとに新しいチケット (close が閉じる・最後に触るのを分ける)
            argv = [a.format(id=iid, file=body) for a in form]
            with self.subTest(form=name):
                p = self.run_cli(*argv, "--json", cwd=proj)
                self.assertEqual(p.returncode, 0, f"{self.m(q)}: 「{name}」 {[*argv, '--json']} が rc={p.returncode}: {p.stderr or p.stdout}")

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

    # ---- git worktree で打ったときに読み書きする DB (humanq は同じ DB に届く登録を 1 つにまとめる: registry._br_home) ----
    # 偽物と本物の両方で、どの DB に届くかを「後から本体にだけ足したチケットが見えるか」で確かめ、humanq の判定
    # (registry._br_home) がその DB の持ち主の dir を返すことを同じ assertion で確かめる (本物の br が解決を変えたら落ちる)

    TRACKED = (".gitignore", "config.yaml", "issues.jsonl", "metadata.json")      # 本物の br の .beads/ で git が追跡するもの

    def git_project(self, name: str, sub: str = "") -> tuple[str, str]:
        """git の repo の下 (sub) に br のプロジェクトを作り、追跡するファイルだけを commit する。(repo, プロジェクトの dir)。"""
        type(self).n += 1
        repo = os.path.join(self.tmp, f"{name}-{type(self).n}")
        proj = os.path.join(repo, sub) if sub else repo
        os.makedirs(proj)
        git(repo, "init", "-q")
        self.ok("init", "init", cwd=proj)
        gi = Path(proj, ".beads", ".gitignore")
        if not gi.exists():                     # 偽の init は .gitignore を書かない。本物が書く印を足す (humanq が br と読む)
            gi.write_text(".br_history/\n*-fsqlite-ns-gate\n", encoding="utf-8")
        self.create("committed", cwd=proj)
        names = [os.path.join(sub, ".beads", n) for n in self.TRACKED if os.path.exists(os.path.join(proj, ".beads", n))]
        git(repo, "add", *names)
        git(repo, "commit", "-q", "-m", "beads")
        return repo, proj

    def add_worktree(self, repo: str) -> str:
        wt = repo + "-wt"
        git(repo, "worktree", "add", "-q", wt)
        return wt

    def titles(self, quirk: str, cwd: str) -> list:
        out = self.jok(quirk, "list", "--limit", "0", cwd=cwd)
        self.assertIsInstance(out, dict, self.m(quirk))
        self.assertIsInstance(out.get("issues"), list, self.m(quirk))
        return sorted(str(r.get("title")) for r in out["issues"] if isinstance(r, dict))

    def test_worktree_without_its_own_db_uses_the_main_worktree_db(self):
        q = "worktree の .beads/ に DB が無ければ、本体の worktree の DB を読み書きする"
        main, _ = self.git_project("wt-main")
        wt = self.add_worktree(main)
        self.create("main-after", cwd=main)                       # commit した JSONL には無い。本体の DB にだけある
        deep = os.path.join(wt, "deep", "er")
        os.makedirs(deep)
        for cwd in (wt, deep):
            with self.subTest(cwd=os.path.relpath(cwd, self.tmp)):
                self.assertIn("main-after", self.titles(q, cwd), self.m(q))
        iid = self.create("from-wt", cwd=wt)
        self.assertIn("from-wt", self.titles(q, main), f"{self.m(q)}: worktree で作ったチケットが本体に無い")
        self.add_comment(iid, "c-from-wt\n", cwd=wt)
        rows = self.jok(q, "comments", iid, cwd=main)
        self.assertIsInstance(rows, list, self.m(q))
        self.assertIn("c-from-wt", [str(c.get("text") if isinstance(c, dict) else c).strip() for c in rows],
                      f"{self.m(q)}: worktree で書いたコメントが本体に無い")
        self.assertEqual(registry._br_home(wt, None), os.path.realpath(main), f"{self.m(q)}: humanq の判定が食い違った")

    def test_worktree_with_its_own_db_uses_it(self):
        q = "worktree の .beads/ に DB があれば、本体ではなくその DB を使う"
        main, _ = self.git_project("wt-own")
        wt = self.add_worktree(main)
        for n in os.listdir(os.path.join(main, ".beads")):         # DB のファイル (本物は beads.db*、偽物は beads.db と中身)
            src = os.path.join(main, ".beads", n)
            if os.path.isfile(src) and (n.startswith("beads.db") or n == "fake-state.json"):
                shutil.copy2(src, os.path.join(wt, ".beads", n))
        self.create("main-after", cwd=main)
        self.assertNotIn("main-after", self.titles(q, wt), self.m(q))
        self.assertEqual(registry._br_home(wt, None), wt, f"{self.m(q)}: humanq の判定が食い違った")

    def test_worktree_subdir_beads_is_a_separate_db(self):
        q = "worktree の下の dir の .beads/ は、本体の同じ dir の DB を使わない"
        main, proj = self.git_project("wt-sub", sub="sub")
        wt = self.add_worktree(main)
        self.create("main-after", cwd=proj)
        self.assertNotIn("main-after", self.titles(q, os.path.join(wt, "sub")), self.m(q))
        self.assertEqual(registry._br_home(os.path.join(wt, "sub"), None), os.path.join(wt, "sub"), self.m(q))

    def test_main_beads_without_a_db_is_still_used(self):
        q = "本体の .beads/ は DB が無くても (dir なら) 使い、そこに DB を作る"
        main, _ = self.git_project("wt-nodb")
        wt = self.add_worktree(main)
        keep = {".gitignore", "metadata.json"}
        for n in os.listdir(os.path.join(main, ".beads")):
            p = os.path.join(main, ".beads", n)
            if n not in keep:
                shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
        self.assertEqual(registry._br_home(wt, None), os.path.realpath(main), f"{self.m(q)}: humanq の判定が食い違った")
        self.create("wt-t", cwd=wt)
        self.assertIn("wt-t", self.titles(q, main), f"{self.m(q)}: worktree で作ったチケットが本体に無い")

    def test_main_without_beads_keeps_the_worktree_db(self):
        q = "本体に .beads/ が無ければ、worktree の .beads/ に DB を作る"
        main, _ = self.git_project("wt-nobeads")
        wt = self.add_worktree(main)
        shutil.rmtree(os.path.join(main, ".beads"))
        self.assertEqual(registry._br_home(wt, None), wt, f"{self.m(q)}: humanq の判定が食い違った")
        self.create("wt-t", cwd=wt)
        self.assertIn("wt-t", self.titles(q, wt), self.m(q))
        self.assertFalse(os.path.exists(os.path.join(main, ".beads")), f"{self.m(q)}: 本体に .beads/ を作った")

    def test_agent_context_is_a_string_in_list_only_where_set(self):
        q = "list --json の agent_context は文字列で、持つチケットだけに出る"
        a = self.create("a", labels="acx")
        b = self.create("b", labels="acx")
        self.ok(q, "update", a, "--agent-context", '{"q":1}')
        rows = {r["id"]: r for r in self.list_rows(q, "-l", "acx")}
        self.assertEqual(sorted(rows), sorted([a, b]), self.m(q))
        self.assertEqual(rows[a].get("agent_context"), '{"q":1}', self.m(q))
        self.assertNotIn("agent_context", rows[b], self.m(q))


# ---- 保留の書き込み (bd・br の両方) -------------------------------------------------------------------------

class SnoozeWrites:
    """保留と保留の解除 (humanq.Store.snooze / unsnooze) を、偽物と本物の CLI に通す。途中の失敗は humanq の差し替え口
    (humanq.bd / humanq.br。CLI を 1 回打つ関数) から注入する: ラベルの段階を 2 回続けて失敗させ (1 回は再試行で通る)、
    送り直しでコメントが 1 件のまま・ラベルが付く (外れる) こと。status と担当が変わらないこと (保留は status を使わない)。"""

    def snooze_project(self) -> str:
        type(self).n += 1
        proj = self.new_project(f"snz-{type(self).n}")
        gi = Path(proj, ".beads", ".gitignore")
        if self.kind == "br" and not gi.exists():        # 偽の br の init は .gitignore を書かない。本物が書く印を足す (humanq が br と読む)
            gi.write_text(".br_history/\n*-fsqlite-ns-gate\n", encoding="utf-8")
        return proj

    def via_swap_point(self, rules: dict):
        """humanq の差し替え口に入れる、CLI を 1 回打つ関数。rules は {語の列: 残りの失敗の回数}。語がこの順で argv に
        現れる呼び出しを、回数が残っている間だけ (CLI を打たずに) BdError で失敗させる。"""
        def run(repo, *args):
            for words, left in list(rules.items()):
                it = iter(args)
                if left > 0 and all(any(a == w for a in it) for w in words.split()):
                    rules[words] = left - 1
                    raise backend.BdError(f"injected ({words})")
            if self.kind == "bd":                       # backend.run_bd / run_br と同じ打ち方・同じ例外
                p = subprocess.run([*self.cmd, "-C", repo, *args], capture_output=True, text=True, timeout=120, env=clean_env())
                if p.returncode != 0:
                    raise backend.BdError((p.stderr or p.stdout).strip()[:800] or f"bd exit {p.returncode}")
                return p.stdout
            p = subprocess.run([*self.cmd, *args], cwd=repo, capture_output=True, text=True, timeout=120, env=clean_env())
            if p.returncode != 0:
                raise backend.CliExit((p.stderr or p.stdout).strip()[:800] or f"br exit {p.returncode}", p.returncode)
            return p.stdout
        return run

    def humanq_marks(self, quirk: str, proj: str, iid: str) -> list[str]:
        rows = self.jok(quirk, "comments", iid, cwd=proj)
        self.assertIsInstance(rows, list, self.m(quirk))
        return sorted(str(c.get("text")).rstrip() for c in rows if isinstance(c, dict) and str(c.get("text")).startswith("[humanq]"))

    def shown(self, quirk: str, proj: str, iid: str) -> dict:
        out = self.jok(quirk, "show", iid, cwd=proj)
        self.assertIsInstance(out, list, self.m(quirk))
        self.assertEqual(len(out), 1, self.m(quirk))
        self.assertIsInstance(out[0], dict, self.m(quirk))
        return out[0]

    def test_snooze_resend_after_an_injected_label_failure(self):
        q = "保留の送り直し (差し替え口からラベルの失敗を注入)"
        proj = self.snooze_project()
        sd = os.path.join(self.tmp, f"state-snz-{type(self).n}")
        os.makedirs(sd)
        registry.register(sd, proj)
        self.assertEqual(registry.project_kind(proj)[0], self.kind, self.m(q))
        iid = self.create("snooze me", labels="human", cwd=proj)
        self.ok(q, "update", iid, "--status", "in_progress", "--assignee", "agent-x", cwd=proj)
        before = {k: self.shown(q, proj, iid).get(k) for k in ("status", "assignee")}
        self.assertEqual(before, {"status": "in_progress", "assignee": "agent-x"}, self.m(q))
        be = humanq.BACKENDS[self.kind]
        until = (humanq.today() + timedelta(days=1)).isoformat()       # 戻る日は明日以降
        rules = {"label add snoozed": 2}

        def call(q, fn, *args, label_fails=False):
            """fn を呼ぶ。label_fails なら label の段階の WriteError を確かめる。想定外の例外は FAIL (変異で漏れた例外を ERROR にしない)。"""
            try:
                got = fn(*args)
            except humanq.WriteError as e:
                self.assertTrue(label_fails, f"{self.m(q)}: 想定外の WriteError ({e.stage}): {e}")
                self.assertEqual((e.stage, e.written), ("label", True), self.m(q))
                return None
            except AssertionError:
                raise
            except Exception as e:  # noqa: BLE001
                self.fail(f"{self.m(q)}: 想定外の例外 {type(e).__name__}: {e}")
            self.assertFalse(label_fails, f"{self.m(q)}: ラベルの段階の失敗が上がらなかった")
            return got

        with mock.patch.object(humanq, self.kind, self.via_swap_point(rules)):
            store = humanq.Store(sd)
            call(q, store.snooze, proj, {"id": iid, "until": until, "note": "情報待ち"}, label_fails=True)
            self.assertEqual(rules, {"label add snoozed": 0}, f"{self.m(q)}: ラベルの段階を 1 回だけ再試行していない")
            want = [f"[humanq] 保留: {until} に戻る (qsig: {humanq.qsig(None)})\n\n情報待ち"]      # 問いの無いチケット
            self.assertEqual(self.humanq_marks(q, proj, iid), want, self.m(q))
            call(q, store.snooze, proj, {"id": iid, "until": until, "note": "情報待ち"})       # 送り直し
            self.assertEqual(self.humanq_marks(q, proj, iid), want, f"{self.m(q)}: 送り直しでコメントが二重になった")
            self.assertIn("snoozed", self.shown(q, proj, iid).get("labels") or [], self.m(q))
            data = call(q, humanq.load_project, proj, be)
            self.assertEqual([(i["id"], i["snooze"]["until"]) for i in data["snoozed"]], [(iid, until)], self.m(q))
            self.assertEqual(data["human"], [], self.m(q))
            rules["label remove snoozed"] = 2
            call(q, store.unsnooze, proj, {"id": iid}, label_fails=True)
            call(q, store.unsnooze, proj, {"id": iid})
            self.assertEqual(self.humanq_marks(q, proj, iid), sorted(want + ["[humanq] 保留の解除"]),
                             f"{self.m(q)}: 解除の送り直しでコメントが二重になった")
            self.assertNotIn("snoozed", self.shown(q, proj, iid).get("labels") or [], self.m(q))
            data = call(q, humanq.load_project, proj, be)
            self.assertEqual(([i["id"] for i in data["human"]], data["snoozed"]), ([iid], []), self.m(q))
        after = {k: self.shown(q, proj, iid).get(k) for k in ("status", "assignee")}
        self.assertEqual(after, before, f"{self.m(q)}: 保留で status か担当が変わった")


# ---- 偽の br の断る分岐と REFUSED の突き合わせ ------------------------------------------------------------

class OverviewReads:
    """見通しの読み (状態別の件数と ready。backend の status_counts / ready) を、偽物と本物に同じ assertion で当てる。
    数える範囲 (閉じたものを数えない) と ready に出るもの (open だけ・human / answered の付いたものも出る・labels を持つ) は、
    偽物の癖として固定してある。本物の bd の ready / count の範囲と行の形は測っていないので、本物の bd で回すとここが最初の実測になる。"""

    def overview_backend(self):
        """この CLI (偽物か本物) を 1 回ずつ打つ backend (bd は -C でプロジェクトを指し、br はプロジェクトの dir で打つ)。"""
        def run(repo: str, *args: str) -> str:
            argv = ("-C", repo, *args) if self.kind == "bd" else args
            p = self.run_cli(*argv, cwd=repo)
            if p.returncode != 0:
                raise backend.CliExit((p.stderr or p.stdout).strip()[:800] or f"exit {p.returncode}", p.returncode)
            return p.stdout
        return backend.BdBackend(run=run) if self.kind == "bd" else backend.BrBackend(run=run)

    def overview_read(self, proj: str, what: str, quirk: str):
        try:
            return getattr(self.overview_backend(), what)(proj)
        except backend.BdError as e:
            self.fail(f"{self.m(quirk)}: {what} が失敗: {e}")

    def test_overview_reads_have_the_shapes_the_backend_reads(self):
        q = "見通しの読み (count の groups と ready の行)"
        type(self).n += 1
        proj = self.new_project(f"overview-{type(self).n}")
        plain = self.create("plain", labels="plugin:x,human", prio=2, cwd=proj)
        answered = self.create("answered", labels="answered", prio=1, cwd=proj)
        busy = self.create("busy", prio=0, cwd=proj)
        self.ok(q, "update", busy, "--status", "in_progress", cwd=proj)
        done = self.create("done", prio=0, cwd=proj)
        self.ok(q, "close", done, cwd=proj)
        count_args = ("count", "--by-status") if self.kind == "bd" else ("count", "--by", "status")
        raw = self.jok(q, *count_args, cwd=proj)
        self.assertIsInstance(raw, dict, self.m(q))
        self.assertIsInstance(raw.get("total"), int, self.m(q))
        counts = dict(self.overview_read(proj, "status_counts", q))
        self.assertEqual(counts, {"open": 2, "in_progress": 1}, f"{self.m(q)}: 既定の範囲 (閉じたものを数えない)")
        self.assertEqual(raw["total"], sum(counts.values()), self.m(q))
        rows = {r["id"]: r for r in self.overview_read(proj, "ready", q)}
        self.assertEqual(sorted(rows), sorted([plain, answered]), f"{self.m(q)}: ready は open だけ (answered の付いたものも出る)")
        self.assertIsInstance(rows[plain]["labels"], list, f"{self.m(q)}: ready の行が labels を持たない")
        self.assertEqual(sorted(rows[plain]["labels"]), ["human", "plugin:x"], self.m(q))
        self.assertEqual((rows[plain]["title"], rows[plain]["priority"], rows[plain]["status"]), ("plain", 2, "open"), self.m(q))
        self.assertIsInstance(rows[plain]["created_at"], str, self.m(q))

    def test_overview_reads_on_an_empty_project(self):
        q = "見通しの読みが空のプロジェクトで空"
        self.assertEqual(self.overview_read(self.empty, "status_counts", q), [], self.m(q))
        self.assertEqual(self.overview_read(self.empty, "ready", q), [], self.m(q))


class FakeBrUsageBranches(unittest.TestCase):
    """偽の br (tests/fake_beads.py) の usage エラーの分岐 (`raise Usage(` と `raise br_usage_unexpected(`) の全部を、
    BrQuirks.REFUSED の形のどれかが踏むこと。分岐を足したのに REFUSED へ形を足し忘れると、その分岐は本物の br と
    突き合わされないまま、偽物だけが断る形になる (本物は通す形が 4 つ見つかったのはこの型の漏れ)。
    数えるのは、ソースの `raise Usage(` と `raise br_usage_unexpected(` の字面だけ。別名を介した raise (`_U = Usage` の
    後の `raise _U(...)` など) と、usage でない断り (`raise Fail(`・`raise br_validation(`・`raise br_json_error(`) は
    対象外 (そちらの追従漏れはこのテストでは気づけない)。
    偽物を進程内で動かし、断りが上がった raise の行を集めて、ソースの分岐の行 (ast) と比べる。REFUSED は後ろに --json を付けて、
    REFUSED_AS_LAST_WORD は何も付けずに流す (test_every_form_the_fake_refuses_is_refused_by_the_real_one と同じ)。"""

    RAISERS = ("Usage", "br_usage_unexpected")

    def load_fake(self):
        spec = importlib.util.spec_from_file_location("fake_beads_usage_branches", FAKE)
        fb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fb)
        return fb

    def source_branches(self) -> dict[int, str]:
        """{行: その行の raise の文}。"""
        text = FAKE.read_text(encoding="utf-8")
        lines = text.splitlines()
        out = {}
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name)
                    and node.exc.func.id in self.RAISERS):
                out[node.lineno] = lines[node.lineno - 1].strip()
        return out

    def raised_lines(self, fb, argv: list[str]) -> set[int]:
        """argv で br を進程内に動かして、usage エラーを上げた raise の行 (偽物のファイルの中で一番内側の行)。usage エラーで
        なければ空。偽物が usage エラー以外の例外 (クラッシュ) で落ちたら、テストの失敗にする。"""
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                fb.run_br(argv)
        except fb.Usage as e:
            last = 0
            tb = e.__traceback__
            while tb is not None:
                if tb.tb_frame.f_code.co_filename == str(FAKE):
                    last = tb.tb_lineno
                tb = tb.tb_next
            return {last}
        except fb.Fail:
            return set()
        except Exception as e:  # noqa: BLE001  偽物がクラッシュしたら、走っていないのではなく失敗として数える
            self.fail(f"偽の br が {argv} で usage エラー以外の例外 {type(e).__name__}: {e} で落ちた")
        return set()

    def test_every_usage_branch_of_the_fake_is_reached_by_a_refused_form(self):
        branches = self.source_branches()
        self.assertGreaterEqual(len(branches), 10, f"ast で分岐を拾えていない: {branches}")
        fb = self.load_fake()
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-usage-branches-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(tmp)
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith("FAKE_BEADS_")}, clear=True):
            fb.br_init(tmp, [])
            sink = io.StringIO()
            with contextlib.redirect_stdout(sink):
                fb.run_br(["create", "x"])
            ids = list(fb.Project(tmp).state["issues"])
            self.assertEqual(len(ids), 1)
            reached: set[int] = set()
            nofile = os.path.join(tmp, "no-such-file.txt")
            body = os.path.join(tmp, "body.txt")
            Path(body).write_text("x\n", encoding="utf-8")
            for table, suffix in ((BrQuirks.REFUSED, ["--json"]), (BrQuirks.REFUSED_AS_LAST_WORD, [])):
                for name, form in table.items():
                    argv = [a.format(id=ids[0], nofile=nofile, file=body) for a in form]
                    reached |= self.raised_lines(fb, [*argv, *suffix])
        missing = {line: src for line, src in branches.items() if line not in reached}
        self.assertEqual(missing, {}, "偽の br の usage エラーの分岐のうち、REFUSED のどの形も踏まないもの (行: 文)。"
                                      "REFUSED に、その分岐を踏む形を足す (本物の br も断ることを契約テストが確かめる)")


for _subject in SUBJECTS:
    _mixin = BdQuirks if _subject.endswith("bd") else BrQuirks
    _name = _mixin.__name__ + "_" + _subject.replace("-", "_")
    globals()[_name] = type(_name, (_mixin, SnoozeWrites, OverviewReads, ContractBase), {"SUBJECT": _subject})
del _subject, _mixin, _name


if __name__ == "__main__":
    unittest.main()
