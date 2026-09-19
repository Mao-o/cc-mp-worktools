"""cursor agent CLI の検出 (0.11.0) と、読み取り専用の起動 argv 契約。

argv 側: `-p` / `--print` 単独は cursor-agent の help で「Has access to all tools,
including write and shell」とされる書込可能モード。`readonly_argv` がそこへ戻らない
ことを固定する。

検出側: `cursor` という名前は環境によって Agent CLI 本体・そのシム・**IDE ランチャー**の
どれでもありうる。`cursor-agent` → `agent` → `cursor` の順に見て、`--version` に応答した
最初のものを使い、結果を TTL 付きでキャッシュすること (と、キャッシュを捨てる条件) を
ここで固定する。

**本物の cursor は決して起動しない**: PATH を偽 CLI だけのディレクトリに差し替え、
候補の実体もすべてこのテストが書いた bash script にする。
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import _testutil  # noqa: F401  (hooks/ を sys.path に載せる)

from _common import cursorcli

READ_ONLY_FLAGS = ["--trust", "--print", "--mode", "plan"]
LEGACY_PREFIX = ["cursor", "agent", *READ_ONLY_FLAGS]


class CursorCliTestCase(unittest.TestCase):
    """PATH を偽 CLI だけに差し替え、TMPDIR (キャッシュの置き場) も隔離する。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self._tmp.name, "bin")
        self.tmpdir = os.path.join(self._tmp.name, "tmp")
        os.makedirs(self.bin)
        os.makedirs(self.tmpdir)
        self.probe_log = os.path.join(self._tmp.name, "probes")
        # PATH を偽 bin だけに差し替える (本物の cursor-agent / agent を掴まないため)。
        # その結果、偽 CLI の中では `sleep` すら PATH から消えるので**絶対パスで解決して
        # おく** — bare `sleep 30` にすると「応答しない CLI」のはずが exit 127 で即死し、
        # timeout の経路を一切通らないまま緑になる (実際に踏んだ)。
        self.sleep = shutil.which("sleep") or "/bin/sleep"
        self._env = mock.patch.dict(
            os.environ, {"PATH": self.bin, "TMPDIR": self.tmpdir}
        )
        self._env.start()
        # `EXTERNAL_AI_CURSOR_COMMAND` が開発者 shell に居ると検出テストが嘘になる
        os.environ.pop(cursorcli.ENV_COMMAND, None)
        cursorcli.reset()

    def tearDown(self) -> None:
        cursorcli.reset()
        self._env.stop()
        self._tmp.cleanup()

    # -- 偽 CLI -----------------------------------------------------------

    def fake_cli(self, name: str, *, version: str = "2026.09.01", exit_code: int = 0):
        """`--version` に応答する偽 CLI を PATH 先頭のディレクトリに置く。

        呼ばれるたびに probe ログへ 1 行足すので、probe の回数を数えられる。
        """
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                f"echo {name} >> {self.probe_log}\n"
                f"printf '%s\\n' {version!r}\n"
                f"exit {exit_code}\n"
            )
        os.chmod(path, 0o755)
        return path

    def silent_cli(self, name: str):
        """0 で終了するが何も出力しない CLI (応答とみなさない)。"""
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\n" f"echo {name} >> {self.probe_log}\n" "exit 0\n")
        os.chmod(path, 0o755)
        return path

    def hanging_cli(self, name: str, *, ignore_term: bool = False):
        """応答しない CLI (`--version` を投げても返ってこない)。

        `ignore_term=True` は SIGTERM を無視する (SIG_IGN は子にも継承されるので
        `sleep` も無視する) ので、停止には SIGKILL 段が必要になる。
        """
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                + ("trap '' TERM\n" if ignore_term else "")
                + f"echo {name} >> {self.probe_log}\n"
                + f"{self.sleep} 30 &\nwait\n"
            )
        os.chmod(path, 0o755)
        return path

    def probes(self) -> list[str]:
        try:
            with open(self.probe_log, encoding="utf-8") as f:
                return f.read().split()
        except OSError:
            return []

    def cache(self) -> dict | None:
        try:
            with open(cursorcli.cache_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def write_cache(self, entry: dict) -> None:
        path = cursorcli.cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entry, f)


class TestReadonlyArgv(CursorCliTestCase):
    def test_runs_print_mode_as_read_only_plan_mode(self):
        self.fake_cli("cursor")
        self.assertEqual(
            cursorcli.readonly_argv("PROMPT"), [*LEGACY_PREFIX, "PROMPT"]
        )

    def test_prompt_is_passed_verbatim_as_the_last_single_argument(self):
        # フラグに見える本文 (改行 / `--mode agent` / `-p`) も分割されず 1 引数のまま末尾に付く
        self.fake_cli("cursor")
        prompt = "line 1\n--mode agent\n-p --trust"
        argv = cursorcli.readonly_argv(prompt)
        self.assertEqual(argv[:-1], LEGACY_PREFIX)
        self.assertEqual(argv[-1], prompt)

    def test_agent_binary_is_launched_without_the_agent_subcommand(self):
        """`cursor-agent` は本体そのものなので `agent` サブコマンドを足さない。"""
        self.fake_cli("cursor-agent")
        self.assertEqual(
            cursorcli.readonly_argv("PROMPT"),
            ["cursor-agent", *READ_ONLY_FLAGS, "PROMPT"],
        )

    def test_unresolved_falls_back_to_the_legacy_launch_shape(self):
        """検出できなくても例外にせず 0.10.0 までの形を返す (呼び出し側は
        `is_available()` で先に確認する契約なので通常は到達しない)。"""
        self.assertIsNone(cursorcli.resolve())
        self.assertEqual(cursorcli.readonly_argv("PROMPT"), [*LEGACY_PREFIX, "PROMPT"])

    def test_readonly_flags_are_the_signature_source(self):
        """explore-parallel の PID 再利用ガードはこのフラグ列を署名に使う
        (`cursor.py::_SIGNATURE_TOKENS`)。環境で変わる argv[0] / サブコマンドは含めない。"""
        self.assertEqual(list(cursorcli.READONLY_FLAGS), READ_ONLY_FLAGS)


class TestDetectionOrder(CursorCliTestCase):
    def test_prefers_cursor_agent_over_cursor(self):
        self.fake_cli("cursor-agent")
        self.fake_cli("cursor")
        self.assertEqual(cursorcli.resolve(), ("cursor-agent", ()))
        self.assertEqual(self.probes(), ["cursor-agent"], "後続の候補まで起動している")

    def test_prefers_agent_over_cursor(self):
        self.fake_cli("agent")
        self.fake_cli("cursor")
        self.assertEqual(cursorcli.resolve(), ("agent", ()))

    def test_falls_back_to_cursor_with_the_agent_subcommand(self):
        self.fake_cli("cursor")
        self.assertEqual(cursorcli.resolve(), ("cursor", ("agent",)))

    def test_unresponsive_candidate_is_skipped(self):
        """0 で終了しても何も出力しない候補は使わない (次の候補へ)。"""
        self.silent_cli("cursor-agent")
        self.fake_cli("cursor")
        self.assertEqual(cursorcli.resolve(), ("cursor", ("agent",)))
        self.assertEqual(self.probes(), ["cursor-agent", "cursor"])

    def test_failing_candidate_is_skipped(self):
        self.fake_cli("cursor-agent", exit_code=1)
        self.fake_cli("cursor")
        self.assertEqual(cursorcli.resolve(), ("cursor", ("agent",)))

    def test_hanging_candidate_loses_to_a_responsive_one(self):
        """応答を確認できた候補が居れば、応答しない候補より優先する
        (本番の失敗は最大 600 秒待ちだったのを probe の timeout に置き換えるのが主眼)。

        timeout は 1 秒に緩めてある: 作ったばかりの script の初回起動はこの端末で
        数百 ms かかることがあり、きつくすると「応答するはずの候補」まで保留に
        倒れてテストが揺れる。
        """
        self.hanging_cli("cursor-agent")
        self.fake_cli("cursor")
        with mock.patch.object(cursorcli, "PROBE_TIMEOUT_SEC", 1.0), mock.patch.object(
            cursorcli, "PROBE_KILL_GRACE_SEC", 0.1
        ):
            started = time.monotonic()
            resolved = cursorcli.resolve()
            elapsed = time.monotonic() - started
        self.assertEqual(resolved, ("cursor", ("agent",)))
        self.assertLess(elapsed, 5.0, "probe の timeout が効いていない")

    def test_hanging_candidate_is_still_used_when_nothing_answers(self):
        """**応答を確認できないことは失格にしない**。起動の遅い本物 (node CLI の
        cold start) を切ってレビュー機能が黙って止まる方向には倒さない。"""
        self.hanging_cli("cursor-agent")
        with mock.patch.object(cursorcli, "PROBE_TIMEOUT_SEC", 0.3), mock.patch.object(
            cursorcli, "PROBE_KILL_GRACE_SEC", 0.1
        ):
            self.assertEqual(cursorcli.resolve(), ("cursor-agent", ()))

    def test_budget_stops_further_probes_but_keeps_the_first_candidate(self):
        """合計予算を使い切ったら残りの候補は確認せず、保留中の最初の候補を使う。"""
        self.hanging_cli("cursor-agent")
        self.hanging_cli("agent")
        self.fake_cli("cursor")
        with mock.patch.object(cursorcli, "PROBE_TIMEOUT_SEC", 0.3), mock.patch.object(
            cursorcli, "PROBE_KILL_GRACE_SEC", 0.1
        ), mock.patch.object(cursorcli, "PROBE_BUDGET_SEC", 0.35):
            self.assertEqual(cursorcli.resolve(), ("cursor-agent", ()))
        # 予算切れの候補は起動しない (応答する `cursor` まで到達していないこと)。
        # 保留中の候補は probe が timeout で kill されるため、ログに現れないことも
        # ある (script の cold start が timeout より遅い場合)。
        self.assertNotIn("cursor", self.probes(), "予算を使い切った後も probe している")

    def test_unresponsive_probe_returns_within_the_timeout(self):
        """応答しない候補の probe は timeout で切り上がり、後始末まで含めて短く済む。

        **`PROBE_KILL_GRACE_SEC` を短くしていること自体はここでは測れていない**
        (下の注記)。猶予が効くのは SIGTERM を無視するプロセスが残っている場合だけで、
        このフィクスチャ (`trap '' TERM` + 背景の sleep) は TERM で group ごと死ぬため、
        猶予を既定 (5 秒) に戻しても経過時間が変わらない = mutation で落ちない。
        `kill_process_group` の TERM→KILL の段自体は `test_subproc.py` が押さえている。
        """
        self.hanging_cli("cursor-agent", ignore_term=True)
        with mock.patch.object(cursorcli, "PROBE_TIMEOUT_SEC", 0.3), mock.patch.object(
            cursorcli, "PROBE_KILL_GRACE_SEC", 0.1
        ):
            started = time.monotonic()
            cursorcli.resolve()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "probe の timeout が効いていない")

    def test_failing_candidate_is_disqualified_even_if_it_is_the_only_one(self):
        """明示的に失敗する候補 (非 0 終了) は使わない — 応答は確認できているので
        「遅いだけ」の可能性が無い。"""
        self.fake_cli("cursor", exit_code=1)
        self.assertIsNone(cursorcli.resolve())

    def test_nothing_installed_is_unavailable(self):
        self.assertIsNone(cursorcli.resolve())
        self.assertFalse(cursorcli.is_available())


class TestDetectionCache(CursorCliTestCase):
    def test_result_is_cached_and_not_reprobed(self):
        self.fake_cli("cursor-agent")
        self.assertEqual(cursorcli.resolve(), ("cursor-agent", ()))
        cursorcli.reset()  # プロセスを跨いだ再呼び出し相当
        self.assertEqual(cursorcli.resolve(), ("cursor-agent", ()))
        self.assertEqual(self.probes(), ["cursor-agent"], "キャッシュが効いていない")

    def test_cache_file_is_private(self):
        self.fake_cli("cursor-agent")
        cursorcli.resolve()
        mode = os.stat(cursorcli.cache_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600, "共有 $TMPDIR に誰でも読めるキャッシュを置かない")

    def test_cache_is_dropped_when_the_binary_moves(self):
        path = self.fake_cli("cursor-agent")
        self.assertEqual(cursorcli.resolve(), ("cursor-agent", ()))
        os.remove(path)
        cursorcli.reset()
        self.assertIsNone(cursorcli.resolve(), "実体が消えてもキャッシュを使い続けている")

    def test_expired_cache_is_refreshed(self):
        self.fake_cli("cursor")
        self.write_cache(
            {
                "command": "cursor-agent",
                "path": os.path.join(self.bin, "cursor-agent"),
                "at": time.time() - cursorcli.CACHE_TTL_SEC - 1,
            }
        )
        self.assertEqual(cursorcli.resolve(), ("cursor", ("agent",)))

    def test_negative_cache_is_honored_then_expires(self):
        self.fake_cli("cursor")
        self.write_cache({"command": None, "at": time.time()})
        self.assertIsNone(cursorcli.resolve(), "否定キャッシュが効いていない")
        self.assertEqual(self.probes(), [], "否定キャッシュ中に probe している")

        cursorcli.reset()
        self.write_cache(
            {"command": None, "at": time.time() - cursorcli.NEGATIVE_CACHE_TTL_SEC - 1}
        )
        self.assertEqual(
            cursorcli.resolve(),
            ("cursor", ("agent",)),
            "否定キャッシュの TTL 超過後に測り直していない",
        )

    def test_negative_ttl_is_shorter_than_positive(self):
        """入れた直後に使い始められるように、否定側の TTL は短くしておく。"""
        self.assertLess(cursorcli.NEGATIVE_CACHE_TTL_SEC, cursorcli.CACHE_TTL_SEC)

    def test_cache_naming_a_command_outside_the_candidates_is_ignored(self):
        """共有 `$TMPDIR` で他人が置いたキャッシュに任意の実体を書かれても使わない。

        `shutil.which` はセパレータを含む値をそのパスとして解決するので、候補名の
        検査が無いと**外部 AI CLI として起動する実体を他人に選ばせる**ことになる。
        """
        evil = self.fake_cli("evil-cli")
        self.fake_cli("cursor")
        self.write_cache({"command": evil, "path": evil, "at": time.time()})
        self.assertEqual(cursorcli.resolve(), ("cursor", ("agent",)))
        self.assertNotIn("evil-cli", self.probes(), "候補外の実体を起動している")

    def test_unsafe_cache_dir_is_not_trusted(self):
        """置き場が group/other 書込可なら (他人が中身を差し替えられるので) 使わない。"""
        self.fake_cli("cursor")
        cursorcli.resolve()  # まず正常にキャッシュを作る
        self.assertIsNotNone(self.cache())
        os.chmod(os.path.dirname(cursorcli.cache_path()), 0o777)
        cursorcli.reset()

        self.assertEqual(cursorcli.resolve(), ("cursor", ("agent",)))
        self.assertEqual(
            self.probes(),
            ["cursor", "cursor"],
            "信頼できない置き場のキャッシュをそのまま使っている (再 probe されていない)",
        )

    def test_broken_cache_file_is_ignored(self):
        self.fake_cli("cursor")
        path = cursorcli.cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(cursorcli.resolve(), ("cursor", ("agent",)))


class TestCommandOverride(CursorCliTestCase):
    def test_override_wins_and_skips_the_probe(self):
        self.fake_cli("cursor-agent")
        self.fake_cli("cursor")
        with mock.patch.dict(os.environ, {cursorcli.ENV_COMMAND: "cursor"}):
            self.assertEqual(cursorcli.resolve(), ("cursor", ("agent",)))
        self.assertEqual(self.probes(), [], "固定指定しているのに probe している")
        self.assertIsNone(self.cache(), "固定指定の結果をキャッシュに書いている")

    def test_override_with_absolute_path_keeps_the_subcommand_rule(self):
        agent_path = self.fake_cli("cursor-agent")
        cursor_path = self.fake_cli("cursor")
        with mock.patch.dict(os.environ, {cursorcli.ENV_COMMAND: agent_path}):
            self.assertEqual(cursorcli.resolve(), (agent_path, ()))
        cursorcli.reset()
        with mock.patch.dict(os.environ, {cursorcli.ENV_COMMAND: cursor_path}):
            self.assertEqual(cursorcli.resolve(), (cursor_path, ("agent",)))

    def test_missing_override_disables_cursor(self):
        """固定指定した実体が無いなら、検出へ落ちずに「使えない」とする
        (利用者が指した実体と違うものを黙って使わない)。"""
        self.fake_cli("cursor")
        with mock.patch.dict(os.environ, {cursorcli.ENV_COMMAND: "nope-cli"}):
            self.assertIsNone(cursorcli.resolve())
            self.assertFalse(cursorcli.is_available())


if __name__ == "__main__":
    unittest.main()
