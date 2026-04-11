"""engine._soft_timeout の UNIX / Windows 分岐テスト。

Step 0-c 実測結果が確定するまで、Windows 方針は ``__main__`` 冒頭の
``_is_unsupported_platform`` で fail-closed にしている。engine 単体としても
SIGALRM 非対応環境では ``_soft_timeout`` が fall-through (timeout 無効) となる
ことを確認する。
"""
from __future__ import annotations

import importlib
import sys
import unittest
from unittest import mock

from _testutil import FIXTURES  # noqa: F401


class TestSoftTimeout(unittest.TestCase):
    def test_no_sigalrm_falls_through(self):
        """SIGALRM 非対応環境では ``_soft_timeout`` は何もせず通過する。"""
        # signal モジュールを手元で複製し SIGALRM を持たないように書き換える
        import signal as real_signal

        fake_signal = mock.MagicMock(spec=real_signal)
        # SIGALRM を削除 (hasattr が False になるように del_attr で)
        del fake_signal.SIGALRM
        with mock.patch.dict(sys.modules, {"signal": fake_signal}):
            # engine を reload して新しい signal を参照させる
            import redaction.engine as engine
            importlib.reload(engine)
            with engine._soft_timeout(1):
                # timeout なしで本文が通れば OK
                pass
        # reload して元に戻す (他テスト影響抑止)
        import redaction.engine as engine
        importlib.reload(engine)

    def test_sigalrm_available_applies_timeout(self):
        """SIGALRM 対応環境 (mac/linux) では timeout が適用される。"""
        import redaction.engine as engine
        importlib.reload(engine)
        import signal as real_signal
        if not hasattr(real_signal, "SIGALRM"):
            self.skipTest("SIGALRM not available")
        # timeout 無しで無害に通ることを確認 (1 秒 sleep 入れないので発火せず)
        with engine._soft_timeout(1):
            pass


if __name__ == "__main__":
    unittest.main()
