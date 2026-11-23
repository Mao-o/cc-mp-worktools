"""コマンドが走るディレクトリの追い方の残り、その 2 (v0.29.3)。

- 外部プログラムとして走る `cd` (`nohup cd sub` / `env cd sub` / `sudo cd sub` / `timeout 5 cd sub` /
  `xargs cd sub` / `nice cd sub` / `/usr/bin/cd sub`) は、シェルを移動させない。分解すると
  `cd sub` に見えるので、移動先のファイルで相対パスの `--config` / `--kubeconfig` を照合したり、
  移動先の固定で通したりしない
- `CDPATH` (環境か前置きの代入) があると、相対パスの `cd sub` の行き先はシェルで割れる
  (bash は `CDPATH` の候補を先に、zsh は作業ディレクトリを先に探す)。決められない扱い
- zsh の `chdir` (cd の同義語。bash には無い) は行き先を決められない扱い
- ディレクトリ単位の固定は、`cd` より後ろにだけパイプ・`||`・背景実行・波括弧・制御構文が
  ある形では、移動後のディレクトリだけで確かめる (締めすぎない)
- ディレクトリに依存しない固定 (aws / gcloud の環境変数) は、ディレクトリの可能性の数の上限で
  消さない
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

import _testutil  # noqa: F401
import test_chdir_followups as base
from test_dispatcher import setUpModule, tearDownModule  # noqa: F401

from core import command_parser, workdir  # noqa: E402
from services import firebase, kubectl  # noqa: E402

# 外部プログラムとして走る wrapper の付いた cd (bash と zsh で移動しないことを実測)
_EXTERNAL_CDS = (
    "nohup cd sub;",
    "env cd sub;",
    "env FOO=1 cd sub;",
    "sudo cd sub;",
    "timeout 5 cd sub;",
    "xargs cd sub;",
    "nice cd sub;",
    "/usr/bin/cd sub;",
    "nohup cd sub &&",
    # zsh の command は外部コマンドとして走らせる (bash は組み込みの cd で移動する)
    "command cd sub;",
)
# シェル自身で走る形 (移動する)
_IN_SHELL_CDS = ("builtin cd sub;", "time cd sub;", "\\cd sub;")


# -- 外部プログラムとして走る cd / CDPATH / chdir: firebase の相対パスの --config -----------------
class FirebaseExternalCdTests(base.FirebaseRelativeConfigTests):
    def setUp(self):
        super().setUp()
        # 起動側の切替先は wrong、sub は right (hook が sub と読むと誤 allow になる配置)
        self._record({self.project_dir: "wrong-project", self.sub: "right-project"})

    def test_cd_run_by_an_external_wrapper_does_not_move(self):
        for pre in _EXTERNAL_CDS:
            command = f"{pre} firebase deploy --config firebase.json"
            with self.subTest(command=command):
                self._assert_deny(self._run(command), command)

    def test_cd_run_by_the_shell_still_moves(self):
        for pre in _IN_SHELL_CDS:
            command = f"{pre} firebase deploy --config firebase.json"
            with self.subTest(command=command):
                self._assert_allow(self._run(command), command)

    def test_cdpath_makes_a_relative_cd_unresolved(self):
        (self.project_dir / "other" / "sub").mkdir(parents=True)
        other = self.project_dir / "other"
        # 行き先を決められない cd の後は、起動リポジトリの外かもしれないとして止まる
        command = f"CDPATH={other} cd sub && firebase deploy --config firebase.json"
        self._assert_deny(self._run(command), command)
        with mock.patch.dict(os.environ, {"CDPATH": str(other)}):
            command = "cd sub && firebase deploy --config firebase.json"
            self._assert_deny(self._run(command), command)
            # `./` / `../` / 絶対パスは CDPATH を見ない
            for command in (
                "cd ./sub && firebase deploy --config firebase.json",
                f"cd {self.sub} && firebase deploy --config firebase.json",
                f"cd ../{self.project_dir.name}/sub && firebase deploy --config firebase.json",
            ):
                with self.subTest(command=command):
                    self._assert_allow(self._run(command), command)

    def test_zsh_chdir_is_unresolved(self):
        self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
        command = "chdir sub && firebase deploy --config firebase.json"
        self._assert_deny(self._run(command), command)

    def test_cd_flags_unknown_to_zsh_and_bash32_do_not_move(self):
        # bash 3.2 は `-e` / `-@` を invalid option、zsh 5.9 は `cd old new` の置換形として失敗する
        for pre in ("cd -e sub;", "cd -@ sub;", "cd -P -e sub;", "cd -e sub\n", "builtin cd -e sub;"):
            command = f"{pre} firebase deploy --config firebase.json"
            with self.subTest(command=command):
                self._assert_deny(self._run(command), command)

    def test_time_and_builtin_with_options_do_not_move(self):
        # zsh: `time -p` / `time --` / `builtin --` はどれも cd を走らせない (bash も `time --` は走らせない)
        for pre in ("time -p cd sub;", "time -- cd sub;", "builtin -- cd sub;"):
            command = f"{pre} firebase deploy --config firebase.json"
            with self.subTest(command=command):
                self._assert_deny(self._run(command), command)

    def test_cdpath_assigned_earlier_in_the_command_is_unresolved(self):
        # bash は other/sub へ、zsh は ./sub へ移る (シェルで割れる)
        (self.project_dir / "other" / "sub").mkdir(parents=True)
        for pre in ("CDPATH=other; cd sub &&", "export CDPATH=other; cd sub &&",
                    "CDPATH=other\ncd sub\n", "declare -x CDPATH=other; cd sub;"):
            command = f"{pre} firebase deploy --config firebase.json"
            with self.subTest(command=command):
                self._assert_deny(self._run(command), command)


# 継承した FirebaseRelativeConfigTests の test_* は継承元のモジュールで走るので、ここでは消す
for _name in [n for n in dir(base.FirebaseRelativeConfigTests) if n.startswith("test_")]:
    setattr(FirebaseExternalCdTests, _name, None)


class KubectlExternalCdTests(base.SudoChdirKubectlTests):
    def setUp(self):
        super().setUp()
        # 起動側の k.yaml は違う、sub/k.yaml が期待値 (hook が sub と読むと誤 allow になる配置)
        (self.project_dir / "k.yaml").write_text("wrong\n", encoding="utf-8")
        (self.sub / "k.yaml").write_text("ctx\n", encoding="utf-8")

    def test_cd_run_by_an_external_wrapper_does_not_move(self):
        for pre in _EXTERNAL_CDS:
            command = f"{pre} kubectl apply -f x.yaml --kubeconfig k.yaml"
            with self.subTest(command=command):
                reason = self._assert_deny(self._run(command), command)
                self.assertIn(kubectl.UNRESOLVED_CONTEXT, reason)

    def test_cd_run_by_the_shell_still_moves(self):
        for pre in _IN_SHELL_CDS:
            command = f"{pre} kubectl apply -f x.yaml --kubeconfig k.yaml"
            with self.subTest(command=command):
                self._assert_allow(self._run(command), command)

    def test_cdpath_and_chdir_are_unresolved(self):
        for command in (
            "CDPATH=/elsewhere cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml",
            "chdir sub && kubectl apply -f x.yaml --kubeconfig k.yaml",
            "cd -e sub; kubectl apply -f x.yaml --kubeconfig k.yaml",
            "export CDPATH=/elsewhere; cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml",
        ):
            with self.subTest(command=command):
                self._assert_deny(self._run(command), command)


for _name in [n for n in dir(base.SudoChdirKubectlTests) if n.startswith("test_")]:
    setattr(KubectlExternalCdTests, _name, None)


# -- ディレクトリ単位の固定: 締めすぎない / 外部の cd / 上限と環境変数の固定 ---------------------
class PinnedSureChdirTests(base.PinnedInEveryDirTests):
    """firebase は未登録で、`firebase use` の記録は sub だけ (起動側には無い)。"""

    # cd が成功した後でしか走らない firebase は、移動後 (sub) だけで確かめる
    _AFTER_CD = (
        "cd sub && firebase deploy",
        "cd sub && firebase deploy 2>&1 | tail -20",
        "cd sub && firebase deploy &",
        "cd sub && { firebase deploy; }",
        "cd sub && if true; then firebase deploy; fi",
        "cd sub && firebase deploy || echo fail",
        "git pull; cd sub && firebase deploy | tee log",
        "cd sub || exit 1; firebase deploy",
        "cd sub; firebase deploy | tee log",
        "mkdir -p sub && cd sub && firebase deploy | tee log",
        "mkdir -p sub && cd sub && firebase deploy; echo done",
        "cd sub && firebase deploy; echo done",
        # 前にある完結したパイプライン・`||`・背景実行は、後ろの cd を不確かにしない
        "git status | head; cd sub && firebase deploy",
        "true || echo x; cd sub && firebase deploy",
        "sleep 1 & cd sub && firebase deploy",
        # 0.29.9 までの代入の値の読みを足す形 (置換の中のパス付きコマンド・空白を含む `${...}`。v0.29.10)
        "cd sub && X=$(/bin/date +%s) firebase deploy",
        "cd sub && X=${Y:-a b} firebase deploy",
    )
    # cd が走らなかった (後続に効かなかった) まま firebase が起動側で走りうる
    _MAYBE_AT_ORIGIN = (
        # 括弧は分解の時点で落ち、`(cd sub && true); firebase deploy` と見分けられない
        "(cd sub && firebase deploy)",
        "(cd sub && true); firebase deploy",
        "cd sub || true; firebase deploy",
        "cd sub || return; firebase deploy",
        "cd sub || exit | cat; firebase deploy",
        "false && cd sub; firebase deploy",
        "mkdir -p sub && cd sub && true; firebase deploy",
        "false && cd sub && true\nfirebase deploy",
        "echo x | cd sub; firebase deploy",
        "cd sub | cat; firebase deploy",
        "cd sub & firebase deploy",
        "{ cd sub; } | cat; firebase deploy",
        "if false; then cd sub; fi; firebase deploy",
        "x=$(pwd); cd sub && firebase deploy",
        "nohup cd sub; firebase deploy",
        "env cd sub && firebase deploy",
        "sudo cd sub; firebase deploy",
        "command cd sub; firebase deploy",
        "watch 'cd sub'; firebase deploy",
    )

    def test_after_a_sure_cd_only_the_moved_dir(self):
        self._write_accounts({"github": "me"})
        for command in self._AFTER_CD:
            with self.subTest(command=command):
                self._record("sub")
                self._assert_allow(self._run(command), command)
                self._record("a")
                self._assert_deny(self._run(command), command)

    def test_maybe_at_the_origin_checks_both(self):
        self._write_accounts({"github": "me"})
        for command in self._MAYBE_AT_ORIGIN:
            with self.subTest(command=command):
                self._record("sub")
                self._assert_deny(self._run(command), command)
                self._record("", "sub")
                self._assert_allow(self._run(command), command)

    def test_env_pin_survives_the_cap(self):
        """可能性の数の上限で畳んだ None で、aws / gcloud の環境変数の固定を消さない。"""
        self._write_accounts({"github": "me"})
        many = "".join(f"(cd d{i}); " for i in range(8))
        cases = (
            ({"AWS_PROFILE": "dev"}, many + "aws s3 rm s3://b/x"),
            ({"CLOUDSDK_CORE_ACCOUNT": "a@example.com"}, many + "gcloud run deploy"),
        )
        for env, command in cases:
            with self.subTest(command=command), mock.patch.dict(os.environ, env):
                self._assert_allow(self._run(command), command)
        # firebase (ディレクトリに依存する固定) は上限で畳んだら固定されていない側
        self._record("", "sub", "a", "b")
        command = many + "firebase deploy"
        self._assert_deny(self._run(command), command)
        self.assertTrue(firebase.PIN_DEPENDS_ON_DIR)

    def test_undecidable_dir_is_not_pinned(self):
        """上限で畳んだ None は、is_pinned が何を返しても firebase の固定とみなさない。"""
        self._write_accounts({"github": "me"})
        command = "".join(f"(cd d{i}); " for i in range(8)) + "firebase deploy"
        with mock.patch.object(firebase, "is_pinned", return_value=True):
            self._assert_allow(self._run("firebase deploy"), "firebase deploy")
            self._assert_deny(self._run(command), command)


for _name in [n for n in dir(base.PinnedInEveryDirTests) if n.startswith("test_")]:
    setattr(PinnedSureChdirTests, _name, None)


# -- 単体 -------------------------------------------------------------------------------------
class ExternalChdirCandidatesUnitTests(unittest.TestCase):
    def test_table(self):
        cases = {
            "nohup cd sub; gh pr list": {"cd sub"},
            "env cd sub": {"cd sub"},
            "env FOO=1 pushd sub": {"pushd sub"},
            "sudo -u root cd sub": {"cd sub"},
            "timeout 5 cd sub": {"cd sub"},
            "xargs cd sub": {"cd sub"},
            "nice popd": {"popd"},
            "nohup chdir sub": {"chdir sub"},
            "pnpm exec cd sub": {"cd sub"},
            "mise exec -- cd sub": {"cd sub"},
            "/usr/bin/cd sub": {"cd sub"},
            "/usr/bin/time cd sub": {"cd sub"},
            "watch 'cd sub && gh pr list'": {"cd sub"},
            "time nohup cd sub": {"cd sub"},
            # シェル自身で走る形・cd 以外
            "cd sub": set(),
            "command cd sub": {"cd sub"},
            "builtin cd sub": set(),
            "time cd sub": set(),
            "exec cd sub": set(),
            "\\cd sub": set(),
            "FOO=1 cd sub": set(),
            "nohup gh pr list": set(),
            "/usr/bin/gh pr list": set(),
            "(cd sub && gh pr list)": set(),
        }
        for command, want in cases.items():
            with self.subTest(command=command):
                self.assertEqual(command_parser.external_chdir_candidates(command), want)

    def test_recorder_is_reset(self):
        command_parser.external_chdir_candidates("nohup cd sub")
        self.assertIsNone(command_parser._EXTERNAL_CHDIR_SEEN)
        with mock.patch.object(
            command_parser, "extract_candidate_readings", side_effect=RuntimeError("x")
        ):
            with self.assertRaises(RuntimeError):
                command_parser.external_chdir_candidates("nohup cd sub")
        self.assertIsNone(command_parser._EXTERNAL_CHDIR_SEEN)

    def test_sequential_is_false(self):
        self.assertTrue(workdir.chdir_is_sequential("builtin cd sub; gh pr create"))
        for pre in _EXTERNAL_CDS:
            command = f"{pre} gh pr create"
            with self.subTest(command=command):
                self.assertFalse(workdir.chdir_is_sequential(command))


class SureChdirsUnitTests(unittest.TestCase):
    def test_table(self):
        sure = {"cd sub"}
        cases = {
            **{c: sure for c in PinnedSureChdirTests._AFTER_CD},
            **{c: set() for c in PinnedSureChdirTests._MAYBE_AT_ORIGIN},
            # `&&` の右の cd は鎖の中では確実 (鎖が終わった後に移動前も残すのは segment_dirs)
            "false && cd sub; firebase deploy": sure,
            "mkdir -p sub && cd sub && true; firebase deploy": sure,
            "false && cd sub && true\nfirebase deploy": sure,
            "cd a; cd b && gh pr create": {"cd a", "cd b"},
            "cd sub\ngh pr create": sure,
            "false && cd sub && gh pr create": sure,
            "false && cd sub\n": sure,
            # 同じ cd が確実な位置とそうでない位置の両方にある
            "cd sub; gh pr list | (cd sub; true)": set(),
            "gh pr list": set(),
            # 読みが 2 つある (ANSI-C の引用符) コマンドは、どの cd も確実とみなさない
            "echo $'\\'' ; cd sub && firebase deploy | tee log": set(),
            "echo $'x' ; cd sub && firebase deploy | tee log": sure,
            # zsh の読みが足される (`${...}` の中の素の `{`)。既定の読みの分割だけでは確実と言えない
            "cd sub && echo ${x:-{}} | gh pr create": set(),
        }
        for command, want in cases.items():
            with self.subTest(command=command):
                self.assertEqual(workdir.sure_chdirs(command), want)
        self.assertEqual(
            len(command_parser.extract_candidate_readings("cd sub && echo ${x:-{}} | gh pr create")), 2
        )

    def test_chain_after_a_conditional_cd(self):
        """`&&` の右の確実な cd は鎖の中だけ。鎖が終わったら鎖の始まりも残す。"""
        cases = [
            ("false && cd sub && gh pr create; gh pr list", {"/w/sub"}, {"/w", "/w/sub"}),
            ("false && cd sub && gh pr create\ngh pr list", {"/w/sub"}, {"/w", "/w/sub"}),
            ("false && cd sub && gh pr create || gh pr list", {"/w/sub"}, {"/w", "/w/sub"}),
            ("cd sub && gh pr create; gh pr list", {"/w/sub"}, {"/w/sub"}),
            ("cd a; false && cd sub && gh pr create; gh pr list", {"/w/a/sub"}, {"/w/a", "/w/a/sub"}),
        ]
        for command, create, listed in cases:
            with self.subTest(command=command):
                got = workdir.segment_dirs(
                    command, "/w", maybe_skipped=True, sure=workdir.sure_chdirs(command)
                )
                self.assertEqual(got["gh pr create"], create)
                self.assertEqual(got["gh pr list"], listed)
                # maybe_skipped でなければ鎖の後も移動先だけ (0.29.2 までと同じ)
                exact = workdir.segment_dirs(command, "/w")
                self.assertEqual(exact["gh pr list"], create)

    def test_segment_dirs_uses_sure(self):
        command = "(cd a); cd sub && gh pr create | tail"
        got = workdir.segment_dirs(command, "/w", maybe_skipped=True, sure=frozenset({"cd sub"}))
        self.assertEqual(got["gh pr create"], {"/w/sub", "/w/a/sub"})
        got = workdir.segment_dirs(command, "/w", maybe_skipped=True)
        self.assertEqual(got["gh pr create"], {"/w", "/w/a", "/w/sub", "/w/a/sub"})
        # 括弧より後ろの cd は確実とみなさない (前方に括弧がある)
        self.assertEqual(workdir.sure_chdirs(command), set())
        command = "cd a; cd sub && gh pr create | tail"
        got = workdir.segment_dirs(
            command, "/w", maybe_skipped=True, sure=workdir.sure_chdirs(command)
        )
        self.assertEqual(got["gh pr create"], {"/w/a/sub"})


class CdpathAndChdirUnitTests(unittest.TestCase):
    def test_segment_dirs(self):
        cases = [
            ("CDPATH=/x cd sub && gh pr create", {None}),
            ("CDPATH= cd sub && gh pr create", {"/w/sub"}),
            ("CDPATH=/x cd ./sub && gh pr create", {"/w/sub"}),
            ("CDPATH=/x cd ../sub && gh pr create", {"/sub"}),
            ("CDPATH=/x cd . && gh pr create", {"/w"}),
            ("CDPATH=/x cd .. && gh pr create", {"/"}),
            ("CDPATH=/x cd /y && gh pr create", {"/y"}),
            ("CDPATH=/x pushd sub && gh pr create", {None}),
            ("chdir sub && gh pr create", {None}),
            ("chdir /y && gh pr create", {None}),
        ]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CDPATH", None)
            for command, want in cases:
                with self.subTest(command=command):
                    self.assertEqual(workdir.segment_dirs(command, "/w")["gh pr create"], want)
            self.assertEqual(
                workdir.segment_dirs("cd sub && gh pr create", "/w")["gh pr create"], {"/w/sub"}
            )
        with mock.patch.dict(os.environ, {"CDPATH": "/x"}):
            self.assertEqual(
                workdir.segment_dirs("cd sub && gh pr create", "/w")["gh pr create"], {None}
            )
            self.assertEqual(
                workdir.segment_dirs("cd ./sub && gh pr create", "/w")["gh pr create"], {"/w/sub"}
            )



if __name__ == "__main__":
    unittest.main()
