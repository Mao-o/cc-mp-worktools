"""本物の bd を打つ脚 (test_server の WithBd と契約テストの real-*) が、env を立てたときだけ走ること。

既定の suite が、PATH に bd があっても本物の bd を 1 回も起動しないことを、記録用の偽の bd を PATH に置いて数える。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import real_cli  # noqa: E402

# test_server を別のプロセスで読み込み、モジュールの判定 (HAVE_BD) と WithBd の skip の旗を JSON で返す
DRIVER = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import test_server as t\n"
    "print(json.dumps({'have': t.HAVE_BD, 'skip': bool(getattr(t.WithBd, '__unittest_skip__', False)),\n"
    "                  'why': getattr(t.WithBd, '__unittest_skip_why__', '')}))\n"
)


class RealCommand(unittest.TestCase):
    def test_without_env_nothing_is_started(self):
        with mock.patch.dict(os.environ, {}, clear=False) as env, mock.patch.object(subprocess, "run") as run:
            env.pop("HUMANQ_TEST_BD", None)
            self.assertIsNone(real_cli.real_command("bd"))
            env["HUMANQ_TEST_BD"] = "  "
            self.assertIsNone(real_cli.real_command("bd"))
        self.assertEqual(run.call_args_list, [], "env が空なのに bd を起動した")

    def test_one_means_the_bd_on_path_and_version_is_checked(self):
        ok = subprocess.CompletedProcess([], 0, "bd 1\n", "")
        with mock.patch.dict(os.environ, {"HUMANQ_TEST_BD": "1"}), mock.patch.object(shutil, "which", return_value="/x/bd"), \
                mock.patch.object(subprocess, "run", return_value=ok) as run:
            self.assertEqual(real_cli.real_command("bd"), ["/x/bd"])
        self.assertEqual(run.call_args.args[0], ["/x/bd", "--version"])

    def test_one_without_bd_on_path_is_none(self):
        with mock.patch.dict(os.environ, {"HUMANQ_TEST_BD": "1"}), mock.patch.object(shutil, "which", return_value=None), \
                mock.patch.object(subprocess, "run") as run:
            self.assertIsNone(real_cli.real_command("bd"))
        self.assertEqual(run.call_args_list, [])

    def test_launcher_string_is_split(self):
        ok = subprocess.CompletedProcess([], 0, "br 1\n", "")
        with mock.patch.dict(os.environ, {"HUMANQ_TEST_BR": "run-it --flag -- br"}), mock.patch.object(subprocess, "run", return_value=ok):
            self.assertEqual(real_cli.real_command("br"), ["run-it", "--flag", "--", "br"])

    def test_version_failure_or_missing_binary_is_none(self):
        bad = subprocess.CompletedProcess([], 1, "", "no")
        def got(**kw):
            with mock.patch.dict(os.environ, {"HUMANQ_TEST_BD": "bd"}), mock.patch.object(subprocess, "run", **kw):
                try:
                    return real_cli.real_command("bd")
                except Exception as e:      # 例外が漏れる変異は ERROR ではなく FAIL に数える
                    self.fail(f"例外が漏れた: {e!r}")
        self.assertIsNone(got(return_value=bad))
        self.assertIsNone(got(side_effect=FileNotFoundError("bd")))
        self.assertIsNone(got(side_effect=subprocess.TimeoutExpired(["bd"], 60)))

    def test_skip_reason_names_the_variable(self):
        self.assertIn("HUMANQ_TEST_BD", real_cli.skip_reason("bd"))
        self.assertIn("HUMANQ_TEST_BR", real_cli.skip_reason("br"))

    def test_clean_env_drops_beads_variables(self):
        with mock.patch.dict(os.environ, {"BEADS_DIR": "x", "BD_X": "1", "BR_X": "1", "FAKE_BEADS_FAIL": "1", "KEEP_ME": "1"}):
            e = real_cli.clean_env()
        self.assertEqual({k for k in e if k.startswith(("BEADS_", "BD_", "BR_", "FAKE_BEADS_"))}, set())
        self.assertEqual(e.get("KEEP_ME"), "1")


class DefaultSuiteDoesNotStartRealBd(unittest.TestCase):
    """PATH に bd があっても、env が無ければ test_server は bd を 1 回も起動せず、WithBd を skip する。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="humanq-realcli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls.log")
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        script = Path(bin_dir, "bd")
        script.write_text(f'#!/bin/sh\necho "$@" >> "{self.log}"\necho "bd version 9.9.9"\n', encoding="utf-8")
        os.chmod(script, 0o755)
        self.bin = bin_dir

    def load(self, **env) -> dict:
        e = {k: v for k, v in os.environ.items() if not k.startswith(("HUMANQ_TEST_", "FAKE_BEADS_"))}
        e["PATH"] = self.bin + os.pathsep + e.get("PATH", "")
        e["PYTHONDONTWRITEBYTECODE"] = "1"
        e.update(env)
        p = subprocess.run([sys.executable, "-c", DRIVER, str(HERE)], capture_output=True, text=True, timeout=120, env=e, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def calls(self) -> list[str]:
        return Path(self.log).read_text(encoding="utf-8").splitlines() if os.path.exists(self.log) else []

    def test_default_starts_no_bd_and_skips_withbd(self):
        got = self.load()
        self.assertEqual(self.calls(), [], "env が無いのに PATH の bd を起動した")
        self.assertEqual((got["have"], got["skip"]), (False, True), got)
        self.assertIn("HUMANQ_TEST_BD", got["why"])

    def test_default_skips_the_real_contract_legs_without_starting_bd(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("HUMANQ_TEST_", "FAKE_BEADS_"))}
        e["PATH"] = self.bin + os.pathsep + e.get("PATH", "")
        e["PYTHONDONTWRITEBYTECODE"] = "1"
        p = subprocess.run([sys.executable, "-m", "unittest", "-v", "tests.test_cli_contract.BdQuirks_real_bd"], cwd=str(HERE.parent),
                           capture_output=True, text=True, timeout=120, env=e)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("skipped", p.stderr)
        self.assertIn("HUMANQ_TEST_BD", p.stderr)
        self.assertEqual(self.calls(), [], "env が無いのに契約テストが PATH の bd を起動した")

    def test_env_one_runs_withbd(self):
        got = self.load(HUMANQ_TEST_BD="1")
        self.assertEqual(self.calls(), ["--version"])
        self.assertEqual((got["have"], got["skip"]), (True, False), got)


if __name__ == "__main__":
    unittest.main()
