"""firebase の固定の判定が `--config` のファイルのあるディレクトリを見る (v0.29.4)。

0.29.3 までの `is_pinned` は `--config` を見ず、`firebase --config other/firebase.json deploy` を
other ではなく走るディレクトリの `firebase use` の記録で固定と判定した。firebase-tools は
`--config` のファイルのあるディレクトリを project root にし、configstore の切替先をその
ディレクトリ (の文字列) から親方向に探す。照合 (verify) 側は 0.18.0 からそうしている。

`--config` のファイルが見つからないなど決められないときは、0.29.3 までと同じ判定 (走る
ディレクトリの記録) のまま。決められないことを理由に未登録の deny には倒さない。

配置の 4 形 (config のディレクトリが symlink / 親ディレクトリが symlink / hard link /
大文字小文字だけ違う名前) を回す。configstore の照合は firebase-tools と同じく文字列なので、
どの形でも「firebase-tools が探すパスの文字列」で記録したときだけ固定とみなす。
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
from test_dispatcher import BaseWithTmpProject, setUpModule, tearDownModule  # noqa: F401

from core.dispatcher import dispatch  # noqa: E402
from services import firebase  # noqa: E402


def _case_insensitive(path: Path) -> bool:
    upper = Path(str(path).upper()) if str(path) != str(path).upper() else None
    return upper is not None and upper.exists()


class PinByConfigDirTests(BaseWithTmpProject):
    """firebase は未登録 (固定だけ)。a / b はそれぞれ firebase.json を持つ project root。"""

    def setUp(self):
        super().setUp()
        self._write_accounts({"github": "me"})
        self.real = Path(os.path.realpath(self.project_dir))
        for name in ("a", "b"):
            d = self.project_dir / name
            d.mkdir()
            (d / "firebase.json").write_text("{}", encoding="utf-8")
        xdg = Path(self.tmp) / "xdg"
        self._store = xdg / "configstore" / "firebase-tools.json"
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_accounts(self, data):
        (self.new_dir / "accounts.local.json").write_text(json.dumps(data), encoding="utf-8")

    def _record(self, *paths):
        """configstore に、渡したパスの文字列そのままで切替先を記録する。"""
        self._store.parent.mkdir(parents=True, exist_ok=True)
        self._store.write_text(
            json.dumps({"activeProjects": {str(p): "my-proj" for p in paths}}), encoding="utf-8"
        )

    def _decision(self, command, cwd) -> str:
        try:
            with self.isolated_cache(), mock.patch(
                "subprocess.run", side_effect=AssertionError("CLI を呼ばない")
            ):
                result = dispatch(command, str(cwd))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        if result is None:
            return "allow"
        return result["hookSpecificOutput"].get("permissionDecision", "allow")

    def _table(self, cases):
        for command, cwd, recorded, want in cases:
            with self.subTest(command=command, cwd=str(cwd), recorded=[str(r) for r in recorded]):
                self._record(*recorded)
                self.assertEqual(self._decision(command, cwd), want)

    def test_config_dir_record_decides(self):
        a, b = self.real / "a", self.real / "b"
        abs_b = self.project_dir / "b" / "firebase.json"
        cases = [
            # DoD: 走るディレクトリ (a) にだけ記録があり、--config の先 (b) には無い → 未登録の deny
            ("firebase --config ../b/firebase.json deploy", a, [a], "deny"),
            ("firebase -c ../b/firebase.json deploy", a, [a], "deny"),
            ("firebase deploy --config=../b/firebase.json", a, [a], "deny"),
            (f"firebase --config {abs_b} deploy", a, [a], "deny"),
            ("cd a && firebase --config ../b/firebase.json deploy", self.project_dir, [a], "deny"),
            # --config の先 (b) の記録で固定とみなす (firebase-tools はそこの切替先で動く)
            ("firebase --config ../b/firebase.json deploy", a, [b], "allow"),
            ("firebase --config ../b/firebase.json deploy", a, [a, b], "allow"),
            ("cd a && firebase --config ../b/firebase.json deploy", self.project_dir, [b], "allow"),
            # 親方向に探す (b の上の記録も効く)
            ("firebase --config b/firebase.json deploy", self.project_dir, [self.real], "allow"),
            # --config の無いコマンドは今までどおり
            ("firebase deploy", a, [a], "allow"),
            ("firebase deploy", a, [b], "deny"),
        ]
        self._table(cases)

    def _reason(self, command, cwd) -> str:
        try:
            with self.isolated_cache(), mock.patch(
                "subprocess.run", side_effect=AssertionError("CLI を呼ばない")
            ):
                result = dispatch(command, str(cwd))
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        self.assertIsNotNone(result, command)
        out = result["hookSpecificOutput"]
        self.assertEqual(out.get("permissionDecision"), "deny", command)
        return out.get("permissionDecisionReason") or ""

    def test_deny_guide_round_trips_to_the_config_dir(self):
        """--config 付きの未固定の deny の案内どおりに打つと、--config の先 (b) に記録されて通る。

        案内の形 (`firebase --config <file> use <alias>`) を理由から切り出し、プレースホルダを
        差し替えた行を加工せずに hook に通す (固定の操作として止めない)。firebase-tools はその
        `--config` のファイルのあるディレクトリ (b) に記録するので、それを模して元のコマンドを流す。
        """
        a, b = self.real / "a", self.real / "b"
        for original, config in (
            ("firebase --config ../b/firebase.json deploy", "../b/firebase.json"),
            ("firebase -c ../b/firebase.json deploy", "../b/firebase.json"),
            ("firebase deploy --config=../b/firebase.json", "../b/firebase.json"),
            (f"firebase --config {self.project_dir / 'b' / 'firebase.json'} deploy",
             str(self.project_dir / "b" / "firebase.json")),
        ):
            with self.subTest(original=original):
                self._record(a)
                reason = self._reason(original, a)
                forms = re.findall(r"firebase --config <file> use <alias>", reason)
                self.assertEqual(len(forms), 1, reason)
                self.assertIn("そのファイルのあるディレクトリが project root", reason)
                line = forms[0].replace("<file>", config).replace("<alias>", "my-proj")
                self.assertEqual(
                    shlex.split(line, comments=True),
                    ["firebase", "--config", config, "use", "my-proj"],
                )
                self.assertEqual(self._decision(line, a), "allow", line)
                # firebase-tools は --config のファイルのあるディレクトリ (b) に、path.resolve
                # (getcwd から字句的に解決し、symlink は解かない) の綴りで記録する
                where = Path(os.path.normpath(os.path.join(str(a), config))).parent
                self.assertTrue(os.path.samefile(where, b))
                self._record(a, where)
                self.assertEqual(self._decision(original, a), "allow", original)

    def test_without_any_accounts_file(self):
        """accounts.local.json がどこにも無い経路でも、--config の先の記録で判定する。"""
        (self.new_dir / "accounts.local.json").unlink()
        a, b = self.real / "a", self.real / "b"
        self._table([
            ("firebase --config ../b/firebase.json deploy", a, [a], "deny"),
            ("firebase --config ../b/firebase.json deploy", a, [b], "allow"),
            ("firebase deploy", a, [a], "allow"),
        ])

    def test_config_file_name_is_not_searched_upward(self):
        """--config のファイル名が firebase.json でなくても、そのファイルのあるディレクトリが root。"""
        c = self.project_dir / "c"
        c.mkdir()
        (c / "fb-prod.json").write_text("{}", encoding="utf-8")
        a, rc = self.real / "a", self.real / "c"
        self._table([
            ("firebase --config ../c/fb-prod.json deploy", a, [a], "deny"),
            ("firebase --config ../c/fb-prod.json deploy", a, [rc], "allow"),
        ])

    def test_undecidable_config_keeps_the_old_judgement(self):
        """ファイルが見つからない --config は、走るディレクトリの記録で判定する (0.29.3 と同じ)。"""
        a = self.real / "a"
        self._table([
            ("firebase --config ../nope/firebase.json deploy", a, [a], "allow"),
            ("firebase --config ../nope/firebase.json deploy", a, [], "deny"),
            # ディレクトリを指す --config もファイルではない
            ("firebase --config ../b deploy", a, [a], "allow"),
        ])
        # 今までどおりの起点は firebase.json のある root (a)。その下の記録は効かない
        deep = self.project_dir / "a" / "deep"
        deep.mkdir()
        self._table([
            ("firebase --config nope.json deploy", deep, [self.real / "a" / "deep"], "deny"),
            ("firebase deploy", deep, [self.real / "a" / "deep"], "deny"),
            ("firebase --config nope.json deploy", deep, [a], "allow"),
        ])

    # -- 配置の 4 形 ---------------------------------------------------------
    def test_config_dir_is_a_symlink(self):
        """(1) --config の先のディレクトリが symlink: firebase-tools は symlink を解かないパスで探す。"""
        link = self.project_dir / "blink"
        link.symlink_to(self.project_dir / "b", target_is_directory=True)
        a = self.real / "a"
        self._table([
            ("firebase --config ../blink/firebase.json deploy", a, [self.real / "b"], "deny"),
            ("firebase --config ../blink/firebase.json deploy", a, [self.real / "blink"], "allow"),
            ("firebase --config ../blink/firebase.json deploy", a, [a], "deny"),
        ])

    def test_parent_dir_is_a_symlink(self):
        """(2) 絶対パスの --config が symlink の親ディレクトリを通る: 書かれたパスの文字列で探す。"""
        plink = Path(self.tmp) / "plink"
        plink.symlink_to(self.project_dir, target_is_directory=True)
        a = self.real / "a"
        given = plink / "b" / "firebase.json"
        self._table([
            (f"firebase --config {given} deploy", a, [self.real / "b"], "deny"),
            (f"firebase --config {given} deploy", a, [plink / "b"], "allow"),
            (f"firebase --config {given} deploy", a, [a], "deny"),
        ])

    def test_hard_link(self):
        """(3) hard link: 同じ inode でも、root はパス (b) で決まる。a の記録では固定にならない。"""
        h = self.project_dir / "h"
        h.mkdir()
        os.link(self.project_dir / "a" / "firebase.json", h / "firebase.json")
        self.assertTrue(
            os.path.samestat(
                os.stat(self.project_dir / "a" / "firebase.json"), os.stat(h / "firebase.json")
            )
        )
        a = self.real / "a"
        self._table([
            ("firebase --config ../h/firebase.json deploy", a, [a], "deny"),
            ("firebase --config ../h/firebase.json deploy", a, [self.real / "h"], "allow"),
        ])

    def test_case_only_name_difference(self):
        """(4) 大文字小文字だけ違う名前: configstore の照合は文字列なので、書かれた綴りで探す。"""
        if not _case_insensitive(self.project_dir / "b"):
            self.skipTest("大文字小文字を区別する FS")
        a = self.real / "a"
        self._table([
            ("firebase --config ../B/firebase.json deploy", a, [self.real / "b"], "deny"),
            ("firebase --config ../B/firebase.json deploy", a, [self.real / "B"], "allow"),
            # 走るディレクトリ側の綴りはディスク上の綴り (getcwd と同じ)
            ("cd A && firebase --config firebase.json deploy", self.project_dir, [a], "allow"),
        ])


class IsPinnedUnitTests(BaseWithTmpProject):
    def setUp(self):
        super().setUp()
        self.real = Path(os.path.realpath(self.project_dir))
        for name in ("a", "b"):
            (self.project_dir / name).mkdir()
            (self.project_dir / name / "firebase.json").write_text("{}", encoding="utf-8")
        xdg = Path(self.tmp) / "xdg"
        store = xdg / "configstore" / "firebase-tools.json"
        store.parent.mkdir(parents=True)
        store.write_text(
            json.dumps({"activeProjects": {str(self.real / "a"): "p"}}), encoding="utf-8"
        )
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(xdg)}

    def _call(self, *args, **kwargs):
        try:
            return firebase.is_pinned(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def test_table(self):
        a = str(self.real / "a")
        cases = [
            (None, True),
            ({}, True),
            # `--project` は project root を動かさない。記録 (p) と違う project を指すと固定ではない
            # (v0.36.6。0.36.5 までは `{"project": "x"}` も True だった。test_pin_context_options)
            ({"project": "p"}, True),
            ({"project": "x"}, False),
            ({"config": "../b/firebase.json"}, False),
            ({"config": str(self.real / "b" / "firebase.json")}, False),
            ({"config": "firebase.json"}, True),
            ({"config": "../nope.json"}, True),
            ({"config": ""}, True),
        ]
        for context, want in cases:
            with self.subTest(context=context):
                self.assertIs(self._call(self.env, a, context=context), want)
        # 2 引数の呼び出し (accounts_builder など) は今までどおり
        self.assertTrue(self._call(self.env, a))

    def test_declares_context_use(self):
        self.assertTrue(getattr(firebase, "PIN_USES_CONTEXT", False))


if __name__ == "__main__":
    unittest.main()
