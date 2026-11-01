"""コマンドが走るディレクトリの追い方の残り (v0.29.3)。

- firebase の相対パスの `--config` を、コマンドが走るディレクトリ (Bash の作業ディレクトリ +
  コマンド中の `cd`) で解決する。0.29.2 までは起動ディレクトリから解決し、
  `cd sub && firebase --config firebase.json deploy` を起動側の firebase.json で照合した
- 作業ディレクトリを変える sudo の option (`-D` / `--chdir` / `-R` / `--chroot` / `-i` /
  `--login`) の中で走るセグメントは、相対パスの context をどのファイルに解決するか・
  ディレクトリ単位の固定が効いているかを決められないとして扱う。0.29.2 までは剥がした後の
  コマンドを起動側のディレクトリで走るものとして照合した
- ディレクトリ単位の固定 (`firebase use` の記録など) を、target が走りうる**全部の**
  ディレクトリで確かめる。0.29.2 までは任意の 1 つだけを見ていた。括弧・条件・パイプの中の
  `cd` があるコマンドでは、移動前と移動後の両方で確かめる
"""
from __future__ import annotations

import json
import os
import re
import shlex
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import (  # noqa: F401
    BaseWithTmpProject,
    _FirebaseConfigFixture,
    setUpModule,
    tearDownModule,
)
from test_kubectl_workdir import _FakeKubectl

from core import command_parser, workdir  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402
from services import firebase, kubectl  # noqa: E402


def _decision(result) -> tuple[str, str]:
    if result is None:
        return "allow", ""
    out = result["hookSpecificOutput"]
    return out.get("permissionDecision", "allow"), (
        out.get("permissionDecisionReason") or out.get("additionalContext") or ""
    )


def _call(case: unittest.TestCase, name: str, *args):
    """firebase の関数を呼ぶ。例外 (宣言が無い・型の誤り) は FAIL として数える。"""
    try:
        return getattr(firebase, name)(*args)
    except Exception as e:  # noqa: BLE001
        case.fail(f"firebase.{name}{args!r}: 例外 {e!r}")


class _Asserts:
    def _assert_allow(self, result, command):
        decision, reason = _decision(result)
        self.assertEqual(decision, "allow", f"{command}\n{reason}")
        self.assertNotIn("不一致", reason)

    def _assert_deny(self, result, command) -> str:
        decision, reason = _decision(result)
        self.assertEqual(decision, "deny", f"{command}\n{reason}")
        return reason


# -- firebase の相対パスの --config ------------------------------------------------
class FirebaseRelativeConfigTests(_Asserts, _FirebaseConfigFixture):
    """起動側と sub/ の両方に firebase.json があり、`firebase use` の切替先が違う。"""

    def setUp(self):
        super().setUp()
        self._write_accounts({"firebase": "right-project"})

    def _run(self, command, *, with_cli=True, cwd=None):
        run = self._fake_cli if with_cli else FileNotFoundError("firebase")
        try:
            with self.isolated_cache(), mock.patch(
                "subprocess.run", side_effect=run
            ), mock.patch("services.firebase.shutil.which", return_value="/usr/bin/firebase"):
                return dispatch(command, str(cwd or self.project_dir))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    _MOVED = (
        "cd sub && firebase --config firebase.json deploy",
        "cd sub && firebase deploy --config firebase.json",
        "cd sub && firebase deploy --config=firebase.json",
        "cd sub && firebase deploy -c firebase.json",
        "cd sub && firebase deploy -cfirebase.json",
        "cd sub && firebase deploy --config ./firebase.json",
        "cd sub; firebase deploy --config firebase.json",
        "pushd sub && firebase deploy --config firebase.json",
        "cd sub\nfirebase deploy --config firebase.json",
        "cd sub && npx firebase-tools --config firebase.json deploy",
    )

    def test_dod_config_is_read_in_the_moved_dir(self):
        """DoD: `cd sub && firebase --config firebase.json deploy` は sub/firebase.json で照合する。"""
        for with_cli in (True, False):
            # sub の切替先が期待値 → 通る (0.29.2 は起動側の wrong-project で deny)
            self._record({self.project_dir: "wrong-project", self.sub: "right-project"})
            for command in self._MOVED:
                with self.subTest(command=command, with_cli=with_cli, sub="right"):
                    self._assert_allow(self._run(command, with_cli=with_cli), command)
            # sub の切替先が期待値と違う → deny (0.29.2 は起動側の right-project で allow)
            self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
            for command in self._MOVED:
                with self.subTest(command=command, with_cli=with_cli, sub="wrong"):
                    reason = self._assert_deny(self._run(command, with_cli=with_cli), command)
                    self.assertIn("wrong-project", reason)

    def test_bash_cwd_without_cd_is_the_base(self):
        """コマンドに cd が無くても、Bash の作業ディレクトリから解決する (firebase-tools と同じ)。"""
        self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
        command = "firebase deploy --config firebase.json"
        self._assert_allow(self._run(command), command)
        reason = self._assert_deny(self._run(command, cwd=self.sub), command)
        self.assertIn("wrong-project", reason)

    def test_missing_file_in_the_moved_dir_is_denied(self):
        self._record({self.project_dir: "right-project"})
        (self.project_dir / "only-root.json").write_text("{}", encoding="utf-8")
        command = "cd sub && firebase deploy --config only-root.json"
        reason = self._assert_deny(self._run(command), command)
        self.assertIn("--config のファイルが見つからない", reason)

    def test_guide_is_absolute_and_round_trips_from_the_launch_dir(self):
        """deny が案内する切替行を、起動ディレクトリで加工せずに通し、書込を再現すると元のコマンドが通る。"""
        self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
        command = "cd sub && firebase deploy --config firebase.json"
        reason = self._assert_deny(self._run(command), command)
        lines = re.findall(r"firebase --config \S+ use \S+", reason)
        self.assertEqual(len(lines), 1, reason)
        line = lines[0]
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(dispatch(line, str(self.project_dir)), line)
        run.assert_not_called()
        tokens = shlex.split(line, comments=True)
        self.assertEqual(tokens[:2], ["firebase", "--config"])
        config = Path(tokens[2])
        self.assertTrue(config.is_absolute(), line)
        self.assertTrue(os.path.samefile(config, self.sub / "firebase.json"), line)
        self._record({self.project_dir: "right-project", config.parent: tokens[4]})
        self._assert_allow(self._run(command), command)

    def test_same_relative_config_in_two_dirs_is_unresolved(self):
        self._record({self.project_dir: "right-project", self.sub: "right-project"})
        for command in (
            "cd sub && firebase deploy -c firebase.json; cd .. && firebase deploy -c firebase.json",
            "(cd sub); firebase deploy --config firebase.json",
            "false && cd sub; firebase deploy --config firebase.json",
            "cd sub | cat; firebase deploy --config firebase.json",
        ):
            with self.subTest(command=command):
                reason = self._assert_deny(self._run(command), command)
                self.assertIn(firebase.UNRESOLVED_CONTEXT, reason)

    def test_absolute_config_is_unaffected_by_cd(self):
        self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
        root_json = self.project_dir / "firebase.json"
        for command in (
            f"(cd sub); firebase deploy --config {root_json}",
            f"cd sub && firebase deploy --config {root_json}",
        ):
            with self.subTest(command=command):
                self._assert_allow(self._run(command), command)

    def test_no_config_keeps_the_project_root(self):
        """`--config` の無いコマンドは 0.29.2 と同じ (`cd` を追わない)。"""
        self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
        command = "(cd sub); firebase deploy"
        self._assert_allow(self._run(command), command)

    # -- 配置の形 ----------------------------------------------------------------
    def test_moved_dir_is_a_symlink(self):
        """symlink の移動先: firebase-tools の作業ディレクトリは実体のパス。`..` も実体の親から字句的に畳む。"""
        deep = self.project_dir / "deep" / "inner"
        deep.mkdir(parents=True)
        (deep / "firebase.json").write_text("{}", encoding="utf-8")
        (deep.parent / "firebase.json").write_text("{}", encoding="utf-8")
        (self.project_dir / "lnk").symlink_to(deep)
        self._record({
            self.project_dir: "wrong-project", deep: "right-project", deep.parent: "right-project",
        })
        for command in (
            "cd lnk && firebase deploy --config firebase.json",
            # `..` は実体の親 (deep)。論理パスの親 (起動側) ではない
            "cd lnk && firebase deploy --config ../firebase.json",
        ):
            with self.subTest(command=command):
                self._assert_allow(self._run(command), command)
        self._record({
            self.project_dir: "right-project", deep: "wrong-project", deep.parent: "wrong-project",
        })
        for command in (
            "cd lnk && firebase deploy --config firebase.json",
            "cd lnk && firebase deploy --config ../firebase.json",
        ):
            with self.subTest(command=command, record="wrong"):
                self._assert_deny(self._run(command), command)

    def test_launch_dir_reached_through_a_symlinked_parent(self):
        """Bash の作業ディレクトリが symlink を通る (起動側の論理パス) 形でも、移動先の実体で照合する。"""
        alias = Path(self.tmp) / "alias"
        alias.symlink_to(self.project_dir)
        self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
        command = "cd sub && firebase deploy --config firebase.json"
        reason = self._assert_deny(self._run(command, cwd=alias), command)
        self.assertIn("wrong-project", reason)

    def test_config_is_a_hard_link_of_the_root_file(self):
        """hard link でも root はファイルのパスのディレクトリ (inode ではない)。"""
        (self.sub / "firebase.json").unlink()
        os.link(self.project_dir / "firebase.json", self.sub / "firebase.json")
        self.assertTrue(os.path.samefile(self.project_dir / "firebase.json", self.sub / "firebase.json"))
        self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
        command = "cd sub && firebase deploy --config firebase.json"
        self._assert_deny(self._run(command), command)

    def test_case_only_name_difference(self):
        probe = self.project_dir / "SUB"
        if not probe.exists():
            self.skipTest("大文字小文字を区別する FS")
        self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
        command = "cd SUB && firebase deploy --config firebase.json"
        self._assert_deny(self._run(command), command)


class FirebaseResolveContextUnitTests(unittest.TestCase):
    def test_table(self):
        real = os.path.realpath("/")
        cases = [
            ({}, {None}, {}),
            ({"project": "p"}, {None}, {"project": "p"}),
            ({"config": "/abs/f.json"}, {None}, {"config": "/abs/f.json"}),
            ({"config": "f.json"}, {"/"}, {"config": os.path.join(real, "f.json")}),
            ({"config": "./a/../f.json"}, {"/"}, {"config": os.path.join(real, "f.json")}),
            ({"config": "f.json"}, {None}, None),
            ({"config": "f.json"}, {"/", "/tmp"}, None),
            ({"config": "f.json"}, set(), None),
        ]
        for ctx, dirs, want in cases:
            with self.subTest(ctx=ctx, dirs=dirs):
                self.assertEqual(_call(self, "resolve_context", ctx, dirs, {}), want)

    def test_does_not_mutate_input(self):
        ctx = {"config": "f.json"}
        _call(self, "resolve_context", ctx, {"/"}, {})
        self.assertEqual(ctx, {"config": "f.json"})


class PhysicalDirUnitTests(unittest.TestCase):
    """`firebase._physical_dir`: getcwd と同じ綴りの実体のパス。得られなければ realpath。"""

    def setUp(self):
        import tempfile
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(self._cleanup)
        self.sub = self.tmp / "sub"
        self.sub.mkdir()

    def _cleanup(self):
        import shutil
        for d in self.tmp.rglob("*"):
            try:
                d.chmod(0o700)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_existing_dir(self):
        self.assertEqual(_call(self, "_physical_dir", str(self.sub)), str(self.sub))

    def test_symlink_is_resolved(self):
        (self.tmp / "lnk").symlink_to(self.sub)
        self.assertEqual(_call(self, "_physical_dir", str(self.tmp / "lnk")), str(self.sub))

    def test_missing_dir_falls_back_to_realpath(self):
        missing = self.tmp / "missing"
        self.assertEqual(_call(self, "_physical_dir", str(missing)), str(missing))

    def test_unopenable_dir_falls_back_to_realpath(self):
        if os.geteuid() == 0:
            self.skipTest("root は権限を無視する")
        locked = self.sub / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.assertEqual(_call(self, "_physical_dir", str(locked)), str(locked))

    def test_fcntl_failure_falls_back_to_realpath(self):
        if firebase.fcntl is None or not hasattr(firebase.fcntl, "F_GETPATH"):
            self.skipTest("F_GETPATH が無い")
        with mock.patch.object(firebase.fcntl, "fcntl", side_effect=OSError("x")):
            self.assertEqual(_call(self, "_physical_dir", str(self.tmp / "SUB")), str(self.tmp / "SUB"))
        with mock.patch.object(firebase, "fcntl", None):
            self.assertEqual(_call(self, "_physical_dir", str(self.sub)), str(self.sub))

    def test_case_only_name_difference_uses_the_on_disk_spelling(self):
        if not (self.tmp / "SUB").exists():
            self.skipTest("大文字小文字を区別する FS")
        if firebase.fcntl is None or not hasattr(firebase.fcntl, "F_GETPATH"):
            self.skipTest("F_GETPATH が無い")
        self.assertEqual(_call(self, "_physical_dir", str(self.tmp / "SUB")), str(self.sub))
        got = _call(self, "resolve_context", {"config": "firebase.json"}, {str(self.tmp / "SUB")}, {})
        self.assertEqual(got, {"config": str(self.sub / "firebase.json")})


# -- sudo の作業ディレクトリを変える option --------------------------------------------
_SUDO_MOVES = (
    "sudo -D sub",
    "sudo -Dsub",
    "sudo --chdir sub",
    "sudo --chdir=sub",
    "sudo -u root -D sub",
    "sudo -nD sub",
    "sudo -E -D sub",
    "sudo -R /",
    "sudo --chroot=/",
    "sudo -i",
    "sudo --login",
    "time sudo -D sub",
    "sudo -D sub time",
)
_SUDO_STAYS = (
    "sudo",
    "sudo -u root",
    "sudo -E",
    "sudo -s",
    "sudo -- ",
)


class SudoChdirKubectlTests(_Asserts, BaseWithTmpProject):
    """DoD: `sudo -D sub kubectl ... --kubeconfig k.yaml` は相対の kubeconfig を決められず deny。"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("KUBECONFIG", None)
        self.sub = self.project_dir / "sub"
        self.sub.mkdir()
        self._write_accounts({"kubectl": "ctx"})
        # 起動側の k.yaml は期待値、sub/k.yaml は違う (0.29.2 は起動側で照合して allow)
        (self.project_dir / "k.yaml").write_text("ctx\n", encoding="utf-8")
        (self.sub / "k.yaml").write_text("wrong\n", encoding="utf-8")
        self.fake = _FakeKubectl()

    def _run(self, command):
        try:
            with self.isolated_cache(), mock.patch("subprocess.run", side_effect=self.fake):
                return dispatch(command, str(self.project_dir))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def test_relative_kubeconfig_under_a_moving_sudo_is_denied(self):
        for prefix in _SUDO_MOVES:
            for tail in (
                "kubectl apply -f x.yaml --kubeconfig k.yaml",
                "kubectl --kubeconfig=k.yaml apply -f x.yaml",
            ):
                command = f"{prefix} {tail}"
                with self.subTest(command=command):
                    reason = self._assert_deny(self._run(command), command)
                    self.assertIn(kubectl.UNRESOLVED_CONTEXT, reason)

    def test_relative_kubeconfig_env_under_a_moving_sudo_is_denied(self):
        os.environ["KUBECONFIG"] = "k.yaml"
        command = "sudo -E -D sub kubectl apply -f x.yaml"
        reason = self._assert_deny(self._run(command), command)
        self.assertIn(kubectl.UNRESOLVED_CONTEXT, reason)

    def test_query_tier_only_warns(self):
        command = "sudo -D sub kubectl get pods --kubeconfig k.yaml"
        decision, text = _decision(self._run(command))
        self.assertEqual(decision, "allow")
        self.assertIn(kubectl.UNRESOLVED_CONTEXT, text)

    def test_sudo_without_a_moving_option_resolves_at_the_base(self):
        for prefix in _SUDO_STAYS:
            command = f"{prefix} kubectl apply -f x.yaml --kubeconfig k.yaml"
            with self.subTest(command=command):
                self._assert_allow(self._run(command), command)

    def test_absolute_kubeconfig_under_a_moving_sudo_is_still_verified(self):
        kc = self.project_dir / "k.yaml"
        command = f"sudo -D sub kubectl apply -f x.yaml --kubeconfig {kc}"
        self._assert_allow(self._run(command), command)
        kc_sub = self.sub / "k.yaml"
        command = f"sudo -D sub kubectl apply -f x.yaml --kubeconfig {kc_sub}"
        reason = self._assert_deny(self._run(command), command)
        self.assertIn("現在=wrong", reason)

    def test_only_the_segment_under_sudo_is_affected(self):
        command = "sudo -D sub true; kubectl apply -f x.yaml --kubeconfig k.yaml"
        self._assert_allow(self._run(command), command)

    def test_same_candidate_with_and_without_sudo_is_unresolved(self):
        command = (
            "kubectl apply -f x.yaml --kubeconfig k.yaml; "
            "sudo -D sub kubectl apply -f x.yaml --kubeconfig k.yaml"
        )
        reason = self._assert_deny(self._run(command), command)
        self.assertIn(kubectl.UNRESOLVED_CONTEXT, reason)


class SudoChdirFirebaseTests(_Asserts, _FirebaseConfigFixture):
    def _run(self, command):
        try:
            with self.isolated_cache(), mock.patch(
                "subprocess.run", side_effect=self._fake_cli
            ), mock.patch("services.firebase.shutil.which", return_value="/usr/bin/firebase"):
                return dispatch(command, str(self.project_dir))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def test_relative_config_under_a_moving_sudo_is_unresolved(self):
        self._write_accounts({"firebase": "right-project"})
        self._record({self.project_dir: "right-project", self.sub: "wrong-project"})
        command = "sudo -D sub firebase deploy --config firebase.json"
        reason = self._assert_deny(self._run(command), command)
        self.assertIn(firebase.UNRESOLVED_CONTEXT, reason)

    def test_pin_under_a_moving_sudo_is_not_trusted(self):
        """未登録で `firebase use` の記録 (起動側) があっても、sudo -D の中は固定とみなさない。"""
        self._write_accounts({"github": "me"})
        self._record({self.project_dir: "right-project"})
        self._assert_allow(self._run("firebase deploy"), "firebase deploy")
        for prefix in ("sudo -D sub", "sudo --chdir=sub", "sudo -i"):
            command = f"{prefix} firebase deploy"
            with self.subTest(command=command):
                reason = self._assert_deny(self._run(command), command)
                self.assertIn('"firebase" キーがありません', reason)


class WrapperChdirCandidatesUnitTests(unittest.TestCase):
    def test_table(self):
        for prefix in _SUDO_MOVES:
            command = f"{prefix} kubectl get pods"
            with self.subTest(command=command):
                self.assertEqual(
                    command_parser.wrapper_chdir_candidates(command), {"kubectl get pods"}
                )
        for prefix in _SUDO_STAYS:
            command = f"{prefix} kubectl get pods"
            with self.subTest(command=command):
                self.assertEqual(command_parser.wrapper_chdir_candidates(command), set())

    def test_shell_wrapped_parts_inherit_the_move(self):
        got = command_parser.wrapper_chdir_candidates(
            "sudo -D sub watch 'kubectl get pods && aws s3 ls'; gh pr list"
        )
        self.assertEqual(got, {"kubectl get pods", "aws s3 ls"})
        got = command_parser.wrapper_chdir_candidates("watch 'sudo -D sub kubectl get pods'")
        self.assertEqual(got, {"kubectl get pods"})
        # 分割し直した内側の段にだけ sudo -D がある
        got = command_parser.wrapper_chdir_candidates(
            "watch 'true && sudo -D sub kubectl get pods'"
        )
        self.assertEqual(got, {"kubectl get pods"})

    def test_flag_region_ends_at_the_command(self):
        """コマンド本体の引数の `-D` / `-i` は sudo の option ではない。"""
        for command in (
            "sudo kubectl get pods -D sub",
            "sudo kubectl exec -i pod -- true",
            "sudo -u -D kubectl get pods",
        ):
            with self.subTest(command=command):
                self.assertEqual(command_parser.wrapper_chdir_candidates(command), set())

    def test_recorder_is_reset(self):
        command_parser.wrapper_chdir_candidates("sudo -D sub kubectl get pods")
        self.assertIsNone(command_parser._WRAPPER_CHDIR_SEEN)
        with mock.patch.object(
            command_parser, "extract_candidate_readings", side_effect=RuntimeError("x")
        ):
            with self.assertRaises(RuntimeError):
                command_parser.wrapper_chdir_candidates("sudo -D sub kubectl get pods")
        self.assertIsNone(command_parser._WRAPPER_CHDIR_SEEN)

    def test_extraction_is_unchanged(self):
        """記録は分解の結果を変えない。"""
        for command in ("sudo -D sub kubectl get pods", "sudo -i 'gh pr list'"):
            with self.subTest(command=command):
                before = command_parser.extract_candidate_readings(command)
                command_parser.wrapper_chdir_candidates(command)
                self.assertEqual(command_parser.extract_candidate_readings(command), before)


# -- ディレクトリ単位の固定を全部のディレクトリで確かめる -----------------------------------
class PinnedInEveryDirTests(_Asserts, BaseWithTmpProject):
    """firebase は未登録 (固定だけ)。a / b / sub はそれぞれ firebase.json を持つ project root。"""

    def setUp(self):
        super().setUp()
        (self.project_dir / "firebase.json").write_text("{}", encoding="utf-8")
        for name in ("a", "b", "sub"):
            d = self.project_dir / name
            d.mkdir()
            (d / "firebase.json").write_text("{}", encoding="utf-8")
        xdg = Path(self.tmp) / "xdg"
        self._store = xdg / "configstore" / "firebase-tools.json"
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, *names):
        self._store.parent.mkdir(parents=True, exist_ok=True)
        active = {
            os.path.realpath(self.project_dir / n if n else self.project_dir): "my-proj"
            for n in names
        }
        self._store.write_text(json.dumps({"activeProjects": active}), encoding="utf-8")

    def _run(self, command):
        try:
            with self.isolated_cache(), mock.patch(
                "subprocess.run", side_effect=AssertionError("CLI を呼ばない")
            ):
                return dispatch(command, str(self.project_dir))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    _TWO_DIRS = "cd a && firebase deploy; cd ../b && firebase deploy"
    _SUBSHELL = "(cd sub && true); firebase deploy"

    def _check(self, with_accounts_file: bool):
        if with_accounts_file:
            self._write_accounts({"github": "me"})
        else:
            (self.new_dir).rmdir()
        cases = [
            # DoD 1: 2 つのディレクトリで走る。片方の固定だけでは通さない
            (self._TWO_DIRS, ("a",), "deny"),
            (self._TWO_DIRS, ("b",), "deny"),
            (self._TWO_DIRS, ("a", "b"), "allow"),
            # DoD 2: 括弧の中の cd。移動前 (起動側) と移動後 (sub) の両方で確かめる
            (self._SUBSHELL, ("sub",), "deny"),
            (self._SUBSHELL, ("", "sub"), "allow"),
            # 起動側の記録は sub にも効く (firebase-tools は親方向に探す)
            (self._SUBSHELL, ("",), "allow"),
            ("false && cd sub; firebase deploy", ("sub",), "deny"),
            ("false && cd sub; firebase deploy", ("", "sub"), "allow"),
            # 確実に順に走る cd は移動先だけ
            ("cd sub && firebase deploy", ("sub",), "allow"),
            ("cd sub && firebase deploy", ("a",), "deny"),
            ("firebase deploy", ("",), "allow"),
            ("firebase deploy", ("sub",), "deny"),
        ]
        for command, recorded, want in cases:
            with self.subTest(command=command, recorded=recorded, accounts=with_accounts_file):
                self._record(*recorded)
                result = self._run(command)
                if want == "allow":
                    self._assert_allow(result, command)
                else:
                    self._assert_deny(result, command)

    def test_with_accounts_file(self):
        self._check(True)

    def test_without_accounts_file(self):
        self._check(False)

    def test_env_pin_is_unaffected_by_dirs(self):
        self._write_accounts({"github": "me"})
        with mock.patch.dict(os.environ, {"CLOUDSDK_CORE_ACCOUNT": "a@example.com"}):
            for command in (
                "cd a && gcloud run deploy; cd ../b && gcloud run deploy",
                "(cd sub && true); gcloud run deploy",
            ):
                with self.subTest(command=command):
                    self._assert_allow(self._run(command), command)

    def test_env_pin_under_a_moving_sudo_is_not_trusted(self):
        """sudo -D の中は、env で固定する service も「決められない」側 (deny) に倒す。"""
        self._write_accounts({"github": "me"})
        with mock.patch.dict(os.environ, {"AWS_PROFILE": "dev"}):
            self._assert_allow(self._run("sudo -E aws s3 rm s3://b/x"), "sudo -E")
            command = "sudo -E -D sub aws s3 rm s3://b/x"
            reason = self._assert_deny(self._run(command), command)
            self.assertIn('"aws" キーがありません', reason)

    def test_any_failing_dir_is_not_pinned(self):
        """is_pinned が 1 つのディレクトリで例外を出したら固定されていない側。"""
        self._write_accounts({"github": "me"})
        self._record("a", "b")

        real = firebase.is_pinned

        def flaky(env, directory, **kwargs):
            if os.path.basename(directory) == "b":
                raise RuntimeError("x")
            return real(env, directory, **kwargs)

        with mock.patch.object(firebase, "is_pinned", side_effect=flaky):
            self._assert_deny(self._run(self._TWO_DIRS), self._TWO_DIRS)


class SegmentDirsMaybeSkippedTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("cd sub && gh pr create", {"/w/sub"}, {"/w", "/w/sub"}),
            ("(cd sub && true); gh pr create", {"/w/sub"}, {"/w", "/w/sub"}),
            ("(cd a); (cd b); gh pr create", {"/w/a/b"}, {"/w", "/w/a", "/w/b", "/w/a/b"}),
            ('(cd "$D"); gh pr create', {None}, {"/w", None}),
            ("cd sub & gh pr create", {"/w"}, {"/w"}),
        ]
        for command, exact, maybe in cases:
            with self.subTest(command=command):
                self.assertEqual(workdir.segment_dirs(command, "/w")["gh pr create"], exact)
                self.assertEqual(
                    workdir.segment_dirs(command, "/w", maybe_skipped=True)["gh pr create"], maybe
                )

    def test_cap_boundary(self):
        n = workdir.MAX_POSSIBLE_DIRS
        for k, want_len in ((n - 2, n - 1), (n - 1, n), (n, None)):
            command = "".join(f"(cd /d{i}); " for i in range(k)) + "gh pr create"
            with self.subTest(k=k):
                got = workdir.segment_dirs(command, "/w", maybe_skipped=True)["gh pr create"]
                if want_len is None:
                    self.assertEqual(got, {None})
                else:
                    self.assertEqual(len(got), want_len)
                    self.assertNotIn(None, got)

    def test_relative_cds_stay_bounded(self):
        """相対パスの cd は可能性を倍々に増やす。上限を超えたら None に畳む (k=12 で 4096 通り)。"""
        command = "".join(f"(cd d{i}); " for i in range(12)) + "gh pr create"
        got = workdir.segment_dirs(command, "/w", maybe_skipped=True)["gh pr create"]
        self.assertEqual(got, {None})
        # 上限の手前: 4 個の相対 cd は 16 通り
        command = "".join(f"(cd d{i}); " for i in range(4)) + "gh pr create"
        got = workdir.segment_dirs(command, "/w", maybe_skipped=True)["gh pr create"]
        self.assertEqual(len(got), 16)

if __name__ == "__main__":
    unittest.main()

