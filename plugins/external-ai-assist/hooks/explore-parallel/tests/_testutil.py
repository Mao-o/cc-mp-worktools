"""explore-parallel テスト共通のパス設定とフィクスチャ。

exitplan-review / post-implementation-review の tests と同じ方式: TMPDIR を隔離し、
`__main__.py` を `__main__` 以外の名前で読み込んで `_main()` を直接呼ぶ。cursor は起動せず、
PATH 先頭の偽 `cursor` (argv を記録して固定文字列を出力する bash script) に差し替える。

`state.BASE_DIR` は import 時に TMPDIR から決まるので、`load_entry` はその前に `cursor` /
`state` を sys.modules から外して読み直す (本番は hook 起動ごとに新プロセスなので同じ条件)。
"""
import importlib.util
import io
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_PKG_DIR = Path(__file__).resolve().parent.parent
_HOOKS_DIR = _PKG_DIR.parent  # hooks/_common の解決用 (本番は __main__.py が載せる)
for _p in (_HOOKS_DIR, _PKG_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _common import settings  # noqa: E402  (sys.path 挿入後に import する)

_ENTRY_PATH = _PKG_DIR / "__main__.py"


def clear_plugin_env(keep: dict | None = None) -> None:
    """開発者 shell の `EXTERNAL_AI_*` を外す (`keep` に挙げたものだけ残す)。

    「未設定時は従来どおり」の回帰テストは、開発者が shell で
    `EXTERNAL_AI_EXPLORE_PARALLEL` 等を export していると嘘になる。個別に列挙する
    方式だと変数が増えるたびに漏れるので接頭辞で一掃する。`mock.patch.dict` は stop 時に
    dict の中身を丸ごと元に戻すので、start した後に消したキーも自動で復元される。
    """
    keep = keep or {}
    for key in [k for k in os.environ if k.startswith(settings.ENV_PREFIX)]:
        if key not in keep:
            del os.environ[key]

# argv は NUL 区切りで記録する (プロンプト本文に改行や `--` が含まれるため)
_RECORD_ARGV = "for a in \"$@\"; do printf '%s\\0' \"$a\"; done > {argv_file}\n"


def load_entry():
    """`__main__.py` を `__main__` 以外の名前で読み込む (_main() の自動実行を避ける)。"""
    for name in ("cursor", "state"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("explore_parallel_entry", _ENTRY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def explore_payload(
    tool_use_id: str, prompt: str = "find the auth flow", subagent_type: str = "Explore"
) -> dict:
    """PreToolUse / PostToolUse(Agent) の hook input 相当。"""
    return {
        "session_id": "s",
        "tool_name": "Agent",
        "tool_use_id": tool_use_id,
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt},
    }


class HookTestCase(unittest.TestCase):
    """TMPDIR を隔離し、PATH 先頭に偽 cursor 用の bin を置き、entry module を用意する基底クラス。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = self._tmp.name
        self.tmpdir = os.path.join(base, "tmp")
        self.bin = os.path.join(base, "bin")
        os.makedirs(self.tmpdir)
        os.makedirs(self.bin)
        self._env = mock.patch.dict(
            os.environ,
            {
                "TMPDIR": self.tmpdir,
                "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            },
        )
        self._env.start()
        clear_plugin_env()

        self.entry = load_entry()
        self.cursor = sys.modules["cursor"]
        self.state = sys.modules["state"]
        # 偽 cursor は即終了する。万一取り残してもテストを 60 秒待たせない
        self._patches = [
            mock.patch.object(self.cursor, "TIMEOUT_SEC", 5),
            mock.patch.object(self.cursor, "POLL_INTERVAL_SEC", 0.05),
        ]
        for p in self._patches:
            p.start()
        self._children: list[int] = []

    def tearDown(self) -> None:
        for pid in self._children:
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        for p in self._patches:
            p.stop()
        self._env.stop()
        self._tmp.cleanup()

    # -- 偽 cursor ---------------------------------------------------------

    def fake_cursor(self, output: str = "SEMANTIC-RESULT\n") -> str:
        """PATH 先頭に偽 `cursor` を置き、argv の記録先パスを返す。"""
        argv_file = os.path.join(self.tmpdir, "cursor.argv")
        path = os.path.join(self.bin, "cursor")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "#!/bin/bash\n"
                + _RECORD_ARGV.format(argv_file=shlex.quote(argv_file))
                + f"printf '%s' {shlex.quote(output)}\n"
            )
        os.chmod(path, 0o755)
        return argv_file

    def read_argv(self, argv_file: str) -> list[str]:
        with open(argv_file, encoding="utf-8") as f:
            return f.read().split("\0")[:-1]

    # -- hook 起動 ---------------------------------------------------------

    def run_hook(self, phase: str, payload: dict) -> str:
        stdin = io.StringIO(json.dumps(payload))
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(
            sys, "argv", ["explore-parallel", "--phase", phase]
        ), mock.patch.object(sys, "stdin", stdin), mock.patch.object(
            sys, "stdout", out
        ), mock.patch.object(sys, "stderr", err):
            try:
                self.entry._main()
            except SystemExit:
                pass
        self.last_stderr = err.getvalue()
        return out.getvalue()

    def reap_cursor(self, tool_use_id: str, timeout: float = 5.0) -> int:
        """pre が書いた pid を読み、偽 cursor の終了を待って回収する。

        本番では pre の hook プロセスが終わった時点で子は PID 1 に引き取られ、終了次第
        reap される。テストでは子のまま残るので自分で wait し、post の `_is_running` が
        zombie を「走行中」と見ない状態にする (Linux では zombie に `kill(pid, 0)` が成功する)。
        偽 cursor がハングしても suite を止めないよう deadline 付きで poll し、超過なら fail
        (tearDown が SIGKILL する)。
        """
        _, pid_file = self.state.paths(self.cursor.NAME, tool_use_id)
        self.assertTrue(pid_file.is_file(), "pre が pid ファイルを書いていない")
        pid = int(pid_file.read_text().strip())
        self._children.append(pid)
        deadline = time.monotonic() + timeout
        while True:
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break  # pre() 戻り時に終了済みで、Popen.__del__ (_internal_poll) が reap した
            if reaped == pid:
                break
            if time.monotonic() >= deadline:
                self.fail(f"偽 cursor (pid {pid}) が {timeout}s 以内に終了しなかった")
            time.sleep(0.02)
        self._children.remove(pid)  # reap 済み pid に tearDown の SIGKILL を送らない
        return pid
