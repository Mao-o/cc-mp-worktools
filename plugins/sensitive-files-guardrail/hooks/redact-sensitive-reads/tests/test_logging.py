"""logging.py の detail sanitize テスト (L1, 0.4.3)。

呼出側責任で「公開可情報のみ渡す」設計だが、コード変更時の意図せぬ秘密混入
(path / 値 / basename / コマンド文字列) を実行時に止める最終防御層。違反は
``_BAD`` placeholder に置換し、ログファイルへ漏れない。
"""
from __future__ import annotations

import collections
import multiprocessing as mp
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _testutil import FIXTURES  # noqa: F401

from core import logging as L


def _rotation_stress_worker(
    log_path: str, max_bytes: int, tag: str, start, lines: int
) -> None:
    """ローテーション並行テストの子プロセス本体 (module level に置く必要がある)。

    macOS の ``multiprocessing`` 既定は **spawn** で、子は ``core.logging`` を
    **再 import** する。したがって親側の ``mock.patch.object(L, "LOG_PATH", ...)``
    / ``MAX_LOG_BYTES`` は子に伝播しない — 子自身が import 後に設定し直す。
    あわせて ``SFG_LOG_PATH`` も渡す: 属性設定が何らかの理由で効かなくても
    実ログ (``~/.claude/logs/redact-hook.log``) には絶対に書かないための二重化
    (実ログは 5MB 閾値を超えているため、1 行でも書くと本番ログがローテーション
    してしまう)。
    """
    os.environ["SFG_LOG_PATH"] = log_path
    from core import logging as CL

    CL.LOG_PATH = Path(log_path)
    CL.MAX_LOG_BYTES = max_bytes
    # timeout を付けないと、import 段階で落ちた兄弟プロセスがいたときに残りが
    # 待ち続け、失敗が「join の 180 秒ハング」として現れて原因が見えなくなる。
    start.wait(timeout=60)
    for i in range(lines):
        CL.log_info("classify", f"{tag}_{i}")


class TestSanitizeDetail(unittest.TestCase):
    """``_sanitize_detail`` のホワイトリスト挙動。"""

    def test_typical_identifiers_pass(self):
        # 既存使用例で頻出する文字列はすべて通る
        for ok in (
            "FileNotFoundError",
            "OSError",
            "regular",
            "symlink",
            "missing",
            "input_redirect_glob_match",
            "segment_residual_metachar_lenient",
            "shell_keyword_lenient:if",
            "shell_keyword_lenient:[[",
            "glob_match:cat",
            "shlex_fail:ValueError",
            "bash:ValueError",
            "dotenv_parse_failed",
            "",
        ):
            self.assertEqual(L._sanitize_detail(ok), ok)

    def test_path_like_detail_is_blocked(self):
        # path / 値が混入したケースは _BAD に倒される
        for bad in (
            "/Users/testuser/.env",
            "./relative/.env",
            ".env",  # `.` 単体は OK だが basename レベルは長さ的にも置換不要…
        ):
            # `.env` 単独は許可文字だけなので通る (誤検知ではなく仕様)
            # → path 形式のテストとしては / を含むケースで判定
            pass

        for bad in (
            "/Users/testuser/.env",
            "./relative/.env",
            "DATABASE_URL=postgresql://x",
            "some value with space",
            'with "quote"',
        ):
            self.assertEqual(L._sanitize_detail(bad), L._DETAIL_PLACEHOLDER)

    def test_overlong_detail_is_blocked(self):
        # 64 文字超は丸ごと _BAD に
        long_val = "A" * 65
        self.assertEqual(L._sanitize_detail(long_val), L._DETAIL_PLACEHOLDER)

    def test_max_length_passes(self):
        # ちょうど 64 文字は通る (境界条件)
        boundary = "A" * 64
        self.assertEqual(L._sanitize_detail(boundary), boundary)

    def test_non_string_returns_placeholder(self):
        for bad in (None, 123, ["list"], {"k": "v"}):
            self.assertEqual(
                L._sanitize_detail(bad),  # type: ignore[arg-type]
                L._DETAIL_PLACEHOLDER,
            )

    def test_control_chars_are_blocked(self):
        # 改行・タブ・null 等は秘密側に入っている可能性が高いので drop
        for bad in (
            "ok\nleak",
            "ok\tleak",
            "ok\x00leak",
        ):
            self.assertEqual(L._sanitize_detail(bad), L._DETAIL_PLACEHOLDER)


class TestLogFileSanitize(unittest.TestCase):
    """ログファイル書き込み時に detail が sanitize されること。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        # LOG_PATH を tmpdir に差し替え (実ホームを汚染しない)
        self._log_path_patch = mock.patch.object(
            L, "LOG_PATH", Path(self.tmp) / "redact-hook.log",
        )
        self._log_path_patch.start()
        self.addCleanup(self._log_path_patch.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_log(self) -> str:
        log_path = L.LOG_PATH
        if not log_path.exists():
            return ""
        return log_path.read_text()

    def test_clean_detail_passes_through(self):
        L.log_info("classify", "regular")
        self.assertIn("regular", self._read_log())
        self.assertNotIn("_BAD", self._read_log())

    def test_dirty_detail_replaced_with_placeholder(self):
        # path 風の detail を渡すと _BAD に置換されてログに書かれる
        L.log_info("classify", "/Users/testuser/.env/secret")
        log = self._read_log()
        self.assertIn("_BAD", log)
        # 元の path 文字列はログに漏れない
        self.assertNotIn("/Users/testuser/.env/secret", log)
        self.assertNotIn("secret", log)

    def test_log_error_also_sanitizes(self):
        # log_error も同じ防御層を通す
        # ただし stderr 出力は category だけで detail は出さない設計
        try:
            saved_stderr = os.dup(2)
            r, w = os.pipe()
            os.dup2(w, 2)
            os.close(w)
            try:
                L.log_error("normalize_failed", "/Users/testuser/secret/path")
            finally:
                os.dup2(saved_stderr, 2)
                os.close(saved_stderr)
                os.close(r)
        except Exception:
            # piping が壊れても本体テストは続行
            L.log_error("normalize_failed", "/Users/testuser/secret/path")

        log = self._read_log()
        self.assertIn("_BAD", log)
        self.assertNotIn("/Users/testuser/secret/path", log)


class TestLogPathOverride(unittest.TestCase):
    """``SFG_LOG_PATH`` による ``LOG_PATH`` の差し替え (内部バックログ)。

    unittest 実行が実ログ (``~/.claude/logs/redact-hook.log``) を汚染して
    計測値を誤らせる問題への対処。
    """

    def test_env_override_used_when_set(self):
        with mock.patch.dict(os.environ, {"SFG_LOG_PATH": "/tmp/sfg-test-xyz/x.log"}):
            self.assertEqual(L._resolve_log_path(), Path("/tmp/sfg-test-xyz/x.log"))

    def test_default_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SFG_LOG_PATH", None)
            self.assertEqual(
                L._resolve_log_path(),
                Path.home() / ".claude" / "logs" / "redact-hook.log",
            )

    def test_testutil_bootstrap_sets_env_var_before_any_test_runs(self):
        # このテストファイル自身が (他の全テストファイルと同じ慣例で)
        # 冒頭で ``from _testutil import FIXTURES`` している。_testutil は
        # プロセス起動時 (unittest discover の collection phase) に一度だけ
        # SFG_LOG_PATH を設定するため、どのテストが実行される時点でも既に
        # 設定済みのはず (core/logging.py の docstring 参照)。
        self.assertIn("SFG_LOG_PATH", os.environ)


class TestLogRotation(unittest.TestCase):
    """ログファイルの 1 世代ローテーション (内部バックログ)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.log_path = Path(self.tmp) / "redact-hook.log"
        self.rotated_path = Path(str(self.log_path) + ".1")
        self._log_path_patch = mock.patch.object(L, "LOG_PATH", self.log_path)
        self._log_path_patch.start()
        self.addCleanup(self._log_path_patch.stop)
        self._max_bytes_patch = mock.patch.object(L, "MAX_LOG_BYTES", 200)
        self._max_bytes_patch.start()
        self.addCleanup(self._max_bytes_patch.stop)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_rotation_when_under_threshold(self):
        L.log_info("classify", "small")
        self.assertFalse(self.rotated_path.exists())

    def test_rotates_when_over_threshold_before_next_write(self):
        # 閾値 (200 byte) を直接超えさせておき、次の書込みが rename を起こす
        # ことを確認する (open **前** に st_size を見る、という提案どおり)。
        self.log_path.write_text("PRE_ROTATION_MARKER\n" * 20)
        self.assertGreater(self.log_path.stat().st_size, 200)
        L.log_info("classify", "after_rotation")
        self.assertTrue(self.rotated_path.exists())
        self.assertIn("PRE_ROTATION_MARKER", self.rotated_path.read_text())
        current = self.log_path.read_text()
        self.assertIn("after_rotation", current)
        self.assertNotIn("PRE_ROTATION_MARKER", current)

    def test_rotation_failure_does_not_raise(self):
        # rename 失敗 (権限 / 別プロセスが保持中 等) でもログ機構が hook 本体の
        # 判定を止めてはいけない (fail-open のログ契約)。
        self.log_path.write_text("X" * 1000)
        with mock.patch("os.replace", side_effect=OSError("boom")) as m_replace:
            try:
                L.log_info("classify", "still_attempted")
            except OSError:
                self.fail("log_info must swallow rotation failures")
        # lock 導入 (外部レビュー R1 P2-B) 後にこのテストが空振りしないことの
        # 担保: lock 取得や再 stat の段階で早期 return していると os.replace に
        # 到達せず「失敗を握りつぶした」ことを何も検証しなくなる。
        self.assertEqual(m_replace.call_count, 1)
        self.assertIn("still_attempted", self.log_path.read_text())

    def test_no_rotation_at_all_when_fcntl_is_unavailable(self):
        # Windows など fcntl の無い環境では直列化できないので、ローテーション
        # 自体を行わない (ログを失う方向ではなく、伸び続ける方に倒す)。
        self.log_path.write_text("KEEP_ME\n" * 40)
        with mock.patch.object(L, "fcntl", None):
            with mock.patch("os.replace") as m_replace:
                L.log_info("classify", "no_rotation")
        self.assertEqual(m_replace.call_count, 0)
        self.assertFalse(self.rotated_path.exists())
        body = self.log_path.read_text()
        self.assertIn("KEEP_ME", body)
        self.assertIn("no_rotation", body)


@unittest.skipIf(L.fcntl is None, "fcntl の無い環境ではローテーションを行わない")
class TestLogRotationSerialization(unittest.TestCase):
    """外部レビュー R1 P2-B: ローテーションのプロセス間直列化。

    修正前は stat と ``os.replace`` の間に他プロセスが割り込めた。先行プロセス
    が rename して新ログに 1 行書いた後で後続プロセスが replace すると、``.1``
    が「1 行だけの新ログ」で上書きされ、前世代が丸ごと失われる。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sfg-rotlock-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log_path = Path(self.tmp) / "redact-hook.log"
        self.rotated_path = Path(str(self.log_path) + ".1")
        self.lock_path = Path(str(self.log_path) + ".lock")

    def _patch_module(self, max_bytes: int) -> None:
        for patcher in (
            mock.patch.object(L, "LOG_PATH", self.log_path),
            mock.patch.object(L, "MAX_LOG_BYTES", max_bytes),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_rotation_skipped_while_lock_is_held_elsewhere(self):
        """lock を別ハンドルが保持していたらローテーションを skip し、追記は続く。

        ``flock`` は open file description 単位なので、同一プロセスでも別途
        ``os.open`` したハンドルの排他ロックは競合する (= 他プロセス相当)。
        """
        self._patch_module(200)
        self.log_path.write_text("OLD_GENERATION\n" * 40)
        holder = os.open(str(self.lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
        self.addCleanup(os.close, holder)
        L.fcntl.flock(holder, L.fcntl.LOCK_EX)

        L.log_info("classify", "while_locked")

        # 担当を譲ったのでローテーションは起きない
        self.assertFalse(self.rotated_path.exists())
        # 追記そのものは壊れない (ログを落とさない)
        body = self.log_path.read_text()
        self.assertIn("OLD_GENERATION", body)
        self.assertIn("while_locked", body)

    def test_no_replace_when_file_changed_between_stat_and_lock(self):
        """lock 取得までの間に他プロセスが rename 済みなら replace しない。

        ``flock`` の side effect で「先行プロセスがローテーションを完了し新ログ
        に 1 行書いた」状態を作る。修正前はここで無条件に ``os.replace`` して
        ``.1`` を 1 行の新ログで潰していた。
        """
        self._patch_module(200)
        real_replace = os.replace
        real_flock = L.fcntl.flock
        self.log_path.write_text("OLD_GENERATION\n" * 40)
        original_body = self.log_path.read_text()

        def _flock_then_someone_else_rotates(fd, operation):
            real_flock(fd, operation)
            real_replace(str(self.log_path), str(self.rotated_path))
            self.log_path.write_text("NEW_GENERATION\n")

        with mock.patch.object(
            L.fcntl, "flock", side_effect=_flock_then_someone_else_rotates
        ):
            with mock.patch("os.replace", wraps=real_replace) as m_replace:
                L.log_info("classify", "after_swap")

        # inode が変わっているので replace には進まない
        self.assertEqual(m_replace.call_count, 0)
        # 前世代 (.1) は無傷
        self.assertEqual(self.rotated_path.read_text(), original_body)
        body = self.log_path.read_text()
        self.assertIn("NEW_GENERATION", body)
        self.assertIn("after_swap", body)

    def test_concurrent_writers_keep_the_previous_generation_intact(self):
        """N プロセス同時書込みで ``.1`` が前世代を保持し、1 行も失われないこと。

        閾値 (100,000 byte) と行数を「ローテーションがちょうど 1 回だけ起きる」
        よう調整してある — 1 世代しか保持しない設計上、2 回目のローテーションが
        起きると「全行が 1 回ずつ存在する」は**仕様として**成り立たなくなり、
        バグでないものを追いかけることになるため。

        修正前コード (commit e6ac2ba) では同型の負荷で ``.1`` が 41 byte
        (1 行) に潰れた: 40 ラウンド中 36〜40 ラウンドで前世代が消失し、
        24〜40 ラウンドで marker 行そのものが失われた (scratch 実測)。
        """
        n_proc = 4
        lines = 200
        max_bytes = 100_000
        prefill_lines = 8_000
        self.log_path.write_text(
            "".join(f"PREFILL_{i:05d}\n" for i in range(prefill_lines))
        )
        self.assertGreater(self.log_path.stat().st_size, max_bytes)

        ctx = mp.get_context("spawn")
        start = ctx.Barrier(n_proc)
        procs = [
            ctx.Process(
                target=_rotation_stress_worker,
                args=(str(self.log_path), max_bytes, f"w{i}", start, lines),
            )
            for i in range(n_proc)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=180)
            self.assertEqual(p.exitcode, 0, msg=f"worker exitcode={p.exitcode}")

        self.assertTrue(self.rotated_path.exists(), "1 回はローテーションが起きる")
        rotated = self.rotated_path.read_text(errors="replace")
        current = self.log_path.read_text(errors="replace")

        # (a) 前世代が 1 行に潰れず、prefill 世代を丸ごと保持している
        self.assertEqual(rotated.count("PREFILL_"), prefill_lines)
        self.assertGreaterEqual(self.rotated_path.stat().st_size, max_bytes)

        # (b) 全行が log と .1 のどこかにちょうど 1 回ずつ存在する
        seen = collections.Counter(re.findall(r"w\d+_\d+", current + rotated))
        expected = {
            f"w{i}_{j}" for i in range(n_proc) for j in range(lines)
        }
        self.assertEqual(set(seen), expected)
        self.assertEqual([m for m, c in seen.items() if c != 1], [])


if __name__ == "__main__":
    unittest.main()
