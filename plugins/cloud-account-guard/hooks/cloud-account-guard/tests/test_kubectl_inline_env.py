"""インラインの KUBECONFIG を付けた kubectl の deny の案内が、同じ KUBECONFIG を引き継ぐ (v0.29.4)。

0.29.3 までは `KUBECONFIG=a.yaml kubectl apply` の deny が素の `kubectl config use-context X` を
案内していた。素の切替は Bash の env の kubeconfig (既定なら `~/.kube/config`) に書くので、
案内どおりにしても同じ deny に戻った。

案内の行は加工せずに、(1) hook に通して止まらないこと (期待値への切替として通る) と、(2) 偽の
kubectl を PATH の先頭に置いた bash と zsh で実際に走らせ、名前どおりのファイルに書いて exit 0
になることを確かめ、(3) 元のコマンドが通ることまで往復で確かめる。
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess as _sp
import unittest
from pathlib import Path
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import BaseWithTmpProject, setUpModule, tearDownModule  # noqa: F401
from test_kubectl_workdir import _FakeKubectl

from core.dispatcher import dispatch  # noqa: E402
from services import kubectl  # noqa: E402

_ENV_GUIDE = re.compile(r"KUBECONFIG=\S+ kubectl config use-context ctx")
_PLAIN_GUIDE = re.compile(r"(?<!\S)kubectl config use-context ctx(?!\S)")
_SHELLS = [s for s in ("/bin/bash", shutil.which("zsh")) if s and os.path.exists(s)]

# 偽の kubectl: `config use-context <name>` だけを受け、KUBECONFIG の最初の要素 (作業ディレクトリ
# からの相対はそのまま) に名前を書く。それ以外の呼び出しは失敗させる。
_FAKE = """#!/bin/sh
[ "$1 $2" = "config use-context" ] || exit 2
f="${KUBECONFIG%%:*}"
[ -n "$f" ] || exit 3
printf '%s\\n' "$3" > "$f"
"""


class InlineKubeconfigGuideTests(BaseWithTmpProject):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("KUBECONFIG", None)
        self.home = Path(self.tmp) / "home"
        (self.home / ".kube").mkdir(parents=True)
        self.default_kc = self.home / ".kube" / "config"
        self.default_kc.write_text("keep\n", encoding="utf-8")
        os.environ["HOME"] = str(self.home)
        (self.new_dir / "accounts.local.json").write_text('{"kubectl": "ctx"}', encoding="utf-8")
        self.sub = self.project_dir / "sub"
        self.sub.mkdir()
        self.other = Path(self.tmp) / "other"
        self.other.mkdir()
        self.bin = Path(self.tmp) / "fakebin"
        self.bin.mkdir()
        fake = self.bin / "kubectl"
        fake.write_text(_FAKE, encoding="utf-8")
        fake.chmod(0o755)
        self.shell_env = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.home),
        }
        self.fake = _FakeKubectl()

    # -- helpers -----------------------------------------------------------
    def _dispatch(self, command, cwd):
        try:
            with self.isolated_cache(), mock.patch("subprocess.run", side_effect=self.fake):
                return dispatch(command, str(cwd))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def _reason(self, result) -> tuple[str, str]:
        if result is None:
            return "allow", ""
        out = result["hookSpecificOutput"]
        return out.get("permissionDecision", "allow"), (
            out.get("permissionDecisionReason") or out.get("additionalContext") or ""
        )

    def _deny(self, command, cwd) -> str:
        decision, reason = self._reason(self._dispatch(command, cwd))
        self.assertEqual(decision, "deny", command)
        return reason

    def _allow(self, command, cwd):
        decision, reason = self._reason(self._dispatch(command, cwd))
        self.assertEqual(decision, "allow", f"{command}\n{reason}")

    def _guide(self, reason) -> str:
        lines = _ENV_GUIDE.findall(reason)
        self.assertEqual(len(lines), 1, reason)
        return lines[0]

    def _run_in_shells(self, line, cwd, written: Path, keep: list[Path]):
        """案内の行を加工せずに hook に通し、bash / zsh で実際に走らせる。"""
        self.assertTrue(_SHELLS)
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(dispatch(line, str(cwd)), line)
        run.assert_not_called()
        tokens = shlex.split(line, comments=True)
        self.assertTrue(tokens[0].startswith("KUBECONFIG="), tokens)
        self.assertEqual(tokens[1:], ["kubectl", "config", "use-context", "ctx"])
        before = {p: p.read_text(encoding="utf-8") for p in keep}
        for shell in _SHELLS:
            with self.subTest(shell=shell):
                if written.exists():
                    written.write_text("wrong\n", encoding="utf-8")
                args = [shell, "-f", "-c", line] if shell.endswith("zsh") else [shell, "-c", line]
                done = _sp.run(args, cwd=str(cwd), env=self.shell_env, capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(written.read_text(encoding="utf-8").strip(), "ctx")
                for p, text in before.items():
                    self.assertEqual(p.read_text(encoding="utf-8"), text, p)

    def test_fake_kubectl_shadows_the_real_one(self):
        for shell in _SHELLS:
            with self.subTest(shell=shell):
                args = [shell, "-f", "-c", "command -v kubectl"] if shell.endswith("zsh") else [
                    shell, "-c", "command -v kubectl"
                ]
                done = _sp.run(args, env=self.shell_env, capture_output=True, text=True)
                self.assertEqual(done.stdout.strip(), str(self.bin / "kubectl"))

    # -- DoD: 案内どおりに打つと元のコマンドが通る -------------------------------
    def test_relative_inline_kubeconfig_round_trips(self):
        kc = self.project_dir / "k.yaml"
        for situation in ("mismatch", "unset"):
            with self.subTest(situation=situation):
                if situation == "mismatch":
                    kc.write_text("wrong\n", encoding="utf-8")
                elif kc.exists():
                    kc.unlink()
                command = "KUBECONFIG=k.yaml kubectl apply -f x.yaml"
                line = self._guide(self._deny(command, self.project_dir))
                value = shlex.split(line)[0].split("=", 1)[1]
                self.assertTrue(os.path.isabs(value), line)
                self.assertEqual(os.path.realpath(value), os.path.realpath(kc))
                if not kc.exists():
                    kc.write_text("", encoding="utf-8")
                # 案内は絶対パスなので、別のディレクトリで打っても同じファイルを指す
                self._run_in_shells(line, self.sub, kc, [self.default_kc])
                self._allow(command, self.project_dir)

    def test_absolute_inline_kubeconfig_round_trips(self):
        kc = self.other / "k.yaml"
        kc.write_text("wrong\n", encoding="utf-8")
        for command, cwd in (
            (f"KUBECONFIG={kc} kubectl apply -f x.yaml", self.project_dir),
            (f"cd sub && KUBECONFIG={kc} kubectl apply -f x.yaml", self.project_dir),
            (f"KUBECONFIG={kc} kubectl --namespace n apply -f x.yaml", self.sub),
        ):
            with self.subTest(command=command):
                kc.write_text("wrong\n", encoding="utf-8")
                line = self._guide(self._deny(command, cwd))
                self.assertEqual(shlex.split(line)[0], f"KUBECONFIG={kc}")
                self._run_in_shells(line, self.project_dir, kc, [self.default_kc])
                self._allow(command, cwd)

    def test_list_value_keeps_every_element(self):
        first = self.project_dir / "k.yaml"
        second = self.other / "b.yaml"
        first.write_text("wrong\n", encoding="utf-8")
        second.write_text("other\n", encoding="utf-8")
        command = f"KUBECONFIG=k.yaml{os.pathsep}{second} kubectl apply -f x.yaml"
        line = self._guide(self._deny(command, self.project_dir))
        value = shlex.split(line)[0].split("=", 1)[1]
        parts = value.split(os.pathsep)
        self.assertEqual(len(parts), 2, line)
        self.assertEqual(os.path.realpath(parts[0]), os.path.realpath(first))
        self.assertEqual(parts[1], str(second))
        self._run_in_shells(line, self.sub, first, [self.default_kc, second])
        self._allow(command, self.project_dir)

    def test_dot_slash_relative_value(self):
        kc = self.project_dir / "k.yaml"
        kc.write_text("wrong\n", encoding="utf-8")
        command = "KUBECONFIG=./k.yaml kubectl apply -f x.yaml"
        line = self._guide(self._deny(command, self.project_dir))
        value = shlex.split(line)[0].split("=", 1)[1]
        self.assertTrue(os.path.isabs(value), line)
        self.assertNotIn("/./", value)
        self._run_in_shells(line, self.sub, kc, [self.default_kc])

    def test_dash_and_escape_named_files_round_trip(self):
        """敵対入力の最小セット: `-` 始まり・`\\_` を含む名前の相対パス (絶対パスにして案内する)。"""
        for name, given in (("-x.yaml", "-x.yaml"), ("a_b.yaml", "a\\_b.yaml")):
            with self.subTest(name=name):
                kc = self.project_dir / name
                kc.write_text("wrong\n", encoding="utf-8")
                command = f"KUBECONFIG={given} kubectl apply -f x.yaml"
                line = self._guide(self._deny(command, self.project_dir))
                value = shlex.split(line)[0].split("=", 1)[1]
                self.assertEqual(os.path.realpath(value), os.path.realpath(kc))
                self._run_in_shells(line, self.sub, kc, [self.default_kc])
                self._allow(command, self.project_dir)

    def test_relative_value_stays_relative_when_the_absolute_path_is_not_guidable(self):
        for name in ("my dir", "日本語"):
            with self.subTest(name=name):
                d = self.project_dir / name
                d.mkdir()
                kc = d / "k.yaml"
                kc.write_text("wrong\n", encoding="utf-8")
                command = "KUBECONFIG=k.yaml kubectl apply -f x.yaml"
                line = self._guide(self._deny(command, d))
                self.assertEqual(shlex.split(line)[0], "KUBECONFIG=k.yaml")
                # Bash の作業ディレクトリ (d) で打てば同じファイル
                self._run_in_shells(line, d, kc, [self.default_kc])
                self._allow(command, d)

    # -- 案内しない形 / 従来どおりの形 ------------------------------------------
    def test_unguidable_values_are_said_in_words(self):
        bad = self.project_dir / "a b.yaml"
        bad.write_text("wrong\n", encoding="utf-8")
        os.environ["KUBECONFIG"] = str(self.default_kc)
        self.default_kc.write_text("wrong\n", encoding="utf-8")
        for command in (
            "KUBECONFIG='a b.yaml' kubectl apply -f x.yaml",
            "KUBECONFIG='~/k.yaml' kubectl apply -f x.yaml",
            # hook の env にある KUBECONFIG をインラインで空にする (kubectl は既定のファイルを読む)
            "KUBECONFIG= kubectl apply -f x.yaml",
        ):
            with self.subTest(command=command):
                reason = self._deny(command, self.project_dir)
                self.assertNotIn("kubectl config use-context", reason)
                self.assertIn(kubectl._KUBECONFIG_ENV_BY_HAND, reason)

    def test_inline_value_equal_to_the_hook_env_keeps_the_plain_guide(self):
        kc = self.other / "k.yaml"
        kc.write_text("wrong\n", encoding="utf-8")
        os.environ["KUBECONFIG"] = str(kc)
        for command in (f"KUBECONFIG={kc} kubectl apply -f x.yaml", "kubectl apply -f x.yaml"):
            with self.subTest(command=command):
                reason = self._deny(command, self.project_dir)
                self.assertEqual(_ENV_GUIDE.findall(reason), [])
                self.assertIn("切り替え: kubectl config use-context ctx", reason)

    def test_no_inline_env_keeps_the_plain_guide(self):
        self.default_kc.write_text("wrong\n", encoding="utf-8")
        # hook の env に KUBECONFIG が無いときのインラインの空は未設定と同じ (kubectl は既定の
        # ファイルを読む)。素の切替がそのファイルに書くので、素の切替を案内する
        for command in ("kubectl apply -f x.yaml", "KUBECONFIG= kubectl apply -f x.yaml"):
            with self.subTest(command=command):
                reason = self._deny(command, self.project_dir)
                self.assertEqual(_ENV_GUIDE.findall(reason), [])
                self.assertNotIn(kubectl._KUBECONFIG_ENV_BY_HAND, reason)
                self.assertEqual(len(_PLAIN_GUIDE.findall(reason)), 1, reason)

    def test_kubeconfig_option_wins_over_inline_env(self):
        kc = self.other / "k.yaml"
        kc.write_text("wrong\n", encoding="utf-8")
        reason = self._deny(
            f"KUBECONFIG=k.yaml kubectl apply -f x.yaml --kubeconfig {kc}", self.project_dir
        )
        self.assertEqual(_ENV_GUIDE.findall(reason), [])
        self.assertIn(f"kubectl config use-context ctx --kubeconfig {kc}", reason)

    def test_relative_inline_after_cd_is_still_said_in_words(self):
        (self.sub / "k.yaml").write_text("wrong\n", encoding="utf-8")
        reason = self._deny("cd sub && KUBECONFIG=k.yaml kubectl apply -f x.yaml", self.project_dir)
        self.assertNotIn("kubectl config use-context", reason)
        self.assertIn("相対パスの KUBECONFIG はコマンドが走るディレクトリ", reason)


class GuideValueUnitTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("k.yaml", "/w", "/w/k.yaml"),
            ("./k.yaml", "/w", "/w/k.yaml"),
            ("../k.yaml", "/w/s", "/w/s/../k.yaml"),
            ("/abs/k.yaml", None, "/abs/k.yaml"),
            ("k.yaml", None, "k.yaml"),
            (f"a.yaml{os.pathsep}/b.yaml", "/w", f"/w/a.yaml{os.pathsep}/b.yaml"),
            # 空の要素はそのまま (kubectl は読み飛ばす。cwd に連結するとディレクトリを指す)
            (f"a.yaml{os.pathsep}{os.pathsep}/b.yaml", "/w", f"/w/a.yaml{os.pathsep}{os.pathsep}/b.yaml"),
            ("k.yaml", "/my dir", "k.yaml"),
            ("a b.yaml", "/w", None),
            ("", "/w", None),
            # `-` 始まりの相対パスも、絶対パスにすれば 1 語の値として案内できる
            ("-x.yaml", "/w", "/w/-x.yaml"),
            ("-x.yaml", None, None),
            ("~/k.yaml", "/w", None),
            ("$(id).yaml", "/w", None),
        ]
        for value, cwd, want in cases:
            with self.subTest(value=value, cwd=cwd):
                try:
                    got = kubectl._guide_kubeconfig_env(value, cwd)
                except Exception as e:  # noqa: BLE001
                    self.fail(f"例外: {e!r}")
                self.assertEqual(got, want)

    def test_inline_detection(self):
        with mock.patch.dict(os.environ, {"KUBECONFIG": "/h.yaml"}):
            self.assertIsNone(kubectl._inline_kubeconfig(None))
            self.assertIsNone(kubectl._inline_kubeconfig({"KUBECONFIG": "/h.yaml"}))
            self.assertEqual(kubectl._inline_kubeconfig({"KUBECONFIG": "/i.yaml"}), "/i.yaml")
            self.assertEqual(kubectl._inline_kubeconfig({"KUBECONFIG": ""}), "")
            self.assertEqual(kubectl._inline_kubeconfig({}), "")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KUBECONFIG", None)
            self.assertIsNone(kubectl._inline_kubeconfig({}))
            self.assertEqual(kubectl._inline_kubeconfig({"KUBECONFIG": "k"}), "k")
            # kubectl は空の KUBECONFIG を未設定と同じに扱う: 素の切替が同じファイルに書く
            self.assertIsNone(kubectl._inline_kubeconfig({"KUBECONFIG": ""}))
        with mock.patch.dict(os.environ, {"KUBECONFIG": ""}):
            self.assertIsNone(kubectl._inline_kubeconfig({}))
            self.assertIsNone(kubectl._inline_kubeconfig({"KUBECONFIG": ""}))
            self.assertEqual(kubectl._inline_kubeconfig({"KUBECONFIG": "/i.yaml"}), "/i.yaml")


if __name__ == "__main__":
    unittest.main()
