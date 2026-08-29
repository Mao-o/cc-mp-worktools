"""PEM / armored 鍵の minimal-info 化 (0.23.0)。

回帰対象: ``_KEY_RE`` が PEM 最終行の base64 パディング ``=`` を ``KEY=`` と
誤認し、base64 本体を「鍵名」として reason に出していた不具合。

fixture は**実鍵ではなく合成データ**を固定で使う。発火は最終行に ``+`` ``/`` が
無いときだけなので、実鍵を生成すると約 90% の確率で pass する flaky テストになる
(詳細は ``tests/fixtures/keys/README.md``)。
"""
from __future__ import annotations

import unittest
from io import BytesIO
from pathlib import Path

from redaction.engine import redact, redact_large_file
from redaction.keyonly_scan import scan_keys
from redaction.pem import _MAX_BLOCKS as MAX_BLOCKS
from redaction.pem import (
    format_pem,
    looks_pem,
    redact_pem,
    scan_pem_markers,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "keys"
SYNTHETIC_PEM = FIXTURE_DIR / "synthetic_rsa.pem.txt"

# fixture 本文に現れる base64 様の行 (これが reason に出たら漏洩)
_BODY_FRAGMENTS = [
    "NOTAREALKEYNOTAREALKEY",
    "syntheticTestDataOnly",
    "NotARealKeyJustTestDataAAAABBBBCCCCDDDD1234567890abcdEFghIJkl",
]


def _redact_text(basename: str, text: str) -> str:
    data = text.encode("utf-8")
    return redact(BytesIO(data), basename, len(data))


class TestPemLeakRegression(unittest.TestCase):
    """base64 本体が reason に出ないこと (本丸)。"""

    def setUp(self) -> None:
        self.text = SYNTHETIC_PEM.read_text()

    def test_fixture_has_the_triggering_shape(self):
        """fixture が「修正前に漏洩した形」であることを固定する。

        最終 body 行が ``=`` で終わり ``+`` ``/`` を含まないこと。この性質が
        崩れると本テストは不具合を検出しなくなる (テスト自身の前提の pin)。
        """
        body_lines = [
            ln for ln in self.text.splitlines()
            if ln and not ln.startswith("-----")
        ]
        last = body_lines[-1]
        self.assertTrue(last.endswith("="), f"最終行が = で終わらない: {last!r}")
        self.assertNotIn("+", last)
        self.assertNotIn("/", last)

    def test_inline_path_does_not_leak_body(self):
        reason = _redact_text("synthetic_rsa.pem", self.text)
        for frag in _BODY_FRAGMENTS:
            self.assertNotIn(frag, reason, f"base64 本体が漏洩: {frag}")

    def test_inline_path_reports_pem_format(self):
        reason = _redact_text("synthetic_rsa.pem", self.text)
        self.assertIn("format: pem", reason)
        self.assertIn("RSA PRIVATE KEY", reason)
        self.assertIn("blocks: 1", reason)

    def test_large_file_path_does_not_leak_body(self):
        """32KB 超は ``redact_large_file`` (scan_stream) 経路に入る。"""
        # block を繰り返して 32KB 超の bundle を作る
        bundle = self.text * 200
        data = bundle.encode("utf-8")
        self.assertGreater(len(data), 32 * 1024)
        reason = redact_large_file(BytesIO(data), "bundle.key")
        for frag in _BODY_FRAGMENTS:
            self.assertNotIn(frag, reason, f"base64 本体が漏洩: {frag}")
        self.assertIn("format: pem", reason)

    def test_extensionless_key_is_detected_by_content(self):
        """``id_rsa`` のような拡張子なしでも content sniff で PEM 経路に入る。"""
        reason = _redact_text("id_rsa", self.text)
        self.assertIn("format: pem", reason)
        for frag in _BODY_FRAGMENTS:
            self.assertNotIn(frag, reason)

    def test_dotenv_with_embedded_pem_does_not_leak_continuation_lines(self):
        """``.env`` の値に複数行 PEM を埋めた形 (dotenv 経路の多層防御)。"""
        env_text = (
            "APP_NAME=demo\n"
            "PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n"
            + "\n".join(
                ln for ln in self.text.splitlines() if not ln.startswith("-----")
            )
            + "\n-----END RSA PRIVATE KEY-----\n"
            "AFTER_KEY=tail\n"
        )
        reason = _redact_text(".env", env_text)
        # dotenv 経路のままであること (format は dotenv)
        self.assertIn("format: dotenv", reason)
        # 正当なキーは見えること
        self.assertIn("APP_NAME", reason)
        self.assertIn("AFTER_KEY", reason)
        # base64 継続行が「鍵名」として出ないこと
        for frag in _BODY_FRAGMENTS:
            self.assertNotIn(frag, reason, f"継続行が鍵名として漏洩: {frag}")


class TestDotenvPemBlockTracking(unittest.TestCase):
    """`.env` 埋め込み PEM は**パーサ状態**で継続行を捨てること (Codex P1)。

    初版は候補文字列のヒューリスティック (24 文字以上 / 大小混在 / 数字あり) だけで
    弾いていたが、RSA / EC PKCS#8 の末尾行は短いことがあり閾値を素通りして
    鍵の断片が「鍵名」として出ていた。
    """

    def _env_with_pem(self, tail: str) -> str:
        return (
            "APP_NAME=demo\n"
            "PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----\n"
            "MHcCAQEEIBvXQ0mKZ8lNqWrTyUiOpAsDfGhJkLzXcVbNm1234567890abcdEFgh\n"
            f"{tail}\n"
            "-----END EC PRIVATE KEY-----\n"
            "AFTER=tail\n"
        )

    def test_short_padded_tail_is_not_reported_as_key(self):
        """末尾行が閾値未満でも鍵名として出ないこと (P1 の本丸)。"""
        reason = _redact_text(".env", self._env_with_pem("AbCd12="))
        self.assertNotIn("AbCd12", reason)
        self.assertIn("entries: 3", reason)

    def test_long_tail_still_rejected(self):
        long_tail = "NotARealKeyJustTestDataAAAABBBBCCCC1234567890abcdEFghIJkl="
        reason = _redact_text(".env", self._env_with_pem(long_tail))
        self.assertNotIn("NotARealKey", reason)
        self.assertIn("entries: 3", reason)

    def test_surrounding_keys_are_preserved(self):
        """block の前後の正当なキーは残ること。"""
        reason = _redact_text(".env", self._env_with_pem("AbCd12="))
        for key in ("APP_NAME", "PRIVATE_KEY", "AFTER"):
            self.assertIn(key, reason)

    def test_body_lines_without_padding_are_also_skipped(self):
        """`=` を持たない本文行も block 内なら捨てられること。"""
        reason = _redact_text(".env", self._env_with_pem("PlainBodyLineNoPadding"))
        self.assertNotIn("PlainBodyLineNoPadding", reason)

    def test_keys_after_block_end_resume(self):
        """END 以降は通常のパースに戻ること (block が閉じない bug の pin)。"""
        text = self._env_with_pem("AbCd12=") + "TRAILING=value\n"
        reason = _redact_text(".env", text)
        self.assertIn("TRAILING", reason)

    def test_inline_comment_marker_does_not_open_block(self):
        """インラインコメント内の marker で block を開かないこと (Codex R6 P2)。

        生の値で判定すると ``A=one # -----BEGIN PRIVATE KEY-----`` のような
        例示コメントで block が開き、END が現れるまで以降のキーが丸ごと
        報告から消える。判定はコメント除去後の値で行う。
        """
        text = (
            "APP_NAME=demo\n"
            "A=one # -----BEGIN PRIVATE KEY-----\n"
            "AFTER1=x\n"
            "AFTER2=y\n"
        )
        reason = _redact_text(".env", text)
        self.assertIn("entries: 4", reason)
        for key in ("APP_NAME", "A", "AFTER1", "AFTER2"):
            self.assertIn(key, reason)

    def test_streaming_path_also_ignores_comment_markers(self):
        """32KB 超の .env (streaming 経路) でも同じ扱いになること (Codex R7 P2)。

        inline 側だけコメント除去を実装すると、``redact_large_file`` 経由の
        大きい .env で以降のキーが消える。marker 判定は
        ``pem.opens_pem_block`` / ``closes_pem_block`` に集約してある。
        """
        # 32KB 超にしつつ、キー数は keyonly_scan.PREVIEW_CAP (60) 未満に
        # 収める (長い値で嵩を稼ぐ)。cap を超えると後半のキーが表示から
        # 落ちるだけで、抽出自体の成否と区別できなくなるため。
        pad = "".join(f"PAD{i}=" + "x" * 2000 + "\n" for i in range(20))
        text = (
            pad
            + "A=one # -----BEGIN PRIVATE KEY-----\n"
            + "AFTER1=x\nAFTER2=y\n"
        )
        data = text.encode()
        self.assertGreater(len(data), 32 * 1024)
        reason = redact_large_file(BytesIO(data), ".env")
        self.assertIn("AFTER1", reason)
        self.assertIn("AFTER2", reason)

    def test_streaming_path_still_skips_real_pem_body(self):
        """コメント除去を入れても実 PEM 本文は従来どおり捨てること。"""
        pad = "".join(f"PAD{i}=" + "x" * 2000 + "\n" for i in range(20))
        text = (
            pad
            + "PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n"
            + "AbCd12=\n"
            + "-----END RSA PRIVATE KEY-----\n"
            + "AFTER=tail\n"
        )
        data = text.encode()
        self.assertGreater(len(data), 32 * 1024)
        reason = redact_large_file(BytesIO(data), ".env")
        self.assertNotIn("AbCd12", reason)
        self.assertIn("AFTER", reason)

    def test_quoted_marker_value_still_opens_block(self):
        """クォート付きの実 PEM 値では従来どおり block を開くこと。"""
        text = (
            "APP_NAME=demo\n"
            'PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----"\n'
            "AbCd12=\n"
            "-----END RSA PRIVATE KEY-----\n"
            "AFTER=tail\n"
        )
        reason = _redact_text(".env", text)
        self.assertNotIn("AbCd12", reason)
        self.assertIn("AFTER", reason)

    def test_unterminated_block_does_not_swallow_rest_silently(self):
        """END が無い壊れた入力でも例外にならないこと。"""
        text = (
            "APP_NAME=demo\n"
            "PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----\n"
            "AbCd12=\n"
        )
        reason = _redact_text(".env", text)
        self.assertIn("APP_NAME", reason)
        self.assertNotIn("AbCd12", reason)


class TestLargeBundleBlockCount(unittest.TestCase):
    """32KB 超 bundle の block 数がストリーム全体から数えられること (Codex P2)。"""

    def _bundle(self, n_blocks: int) -> bytes:
        body = "\n".join("A" * 64 for _ in range(30))
        block = (
            f"-----BEGIN CERTIFICATE-----\n{body}\nX1=\n"
            "-----END CERTIFICATE-----\n"
        )
        return (block * n_blocks).encode()

    def test_counts_all_blocks_not_just_head(self):
        data = self._bundle(20)
        self.assertGreater(len(data), 32 * 1024)
        reason = redact_large_file(BytesIO(data), "bundle.key")
        self.assertIn("blocks: 20", reason)

    def test_begin_and_end_counts_match(self):
        """head 限定だった頃は end_markers を blocks で上書きして差異を隠していた。"""
        reason = redact_large_file(BytesIO(self._bundle(20)), "bundle.key")
        self.assertNotIn("do not match", reason)

    def test_armored_bytes_is_whole_file(self):
        data = self._bundle(20)
        reason = redact_large_file(BytesIO(data), "bundle.key")
        self.assertIn(f"armored bytes: {len(data)}", reason)

    def test_scan_limit_is_disclosed(self):
        """走査上限を超えたら件数が部分的である旨を出すこと。"""
        f = BytesIO(self._bundle(20))
        info = scan_pem_markers(f, max_bytes=4096)
        self.assertTrue(info["truncated_scan"])
        self.assertIn("scan limit", format_pem(info))

    def test_marker_split_across_chunk_boundary_is_counted(self):
        """chunk 境界に marker が跨っても取りこぼさないこと。"""
        from redaction import pem as pem_mod
        data = self._bundle(3)
        f = BytesIO(data)
        original = pem_mod._STREAM_CHUNK
        try:
            pem_mod._STREAM_CHUNK = 37  # marker 長より短い chunk で境界を跨がせる
            info = scan_pem_markers(f)
        finally:
            pem_mod._STREAM_CHUNK = original
        self.assertEqual(info["blocks"], 3)
        self.assertEqual(info["end_markers"], 3)


class TestLooksPem(unittest.TestCase):
    def test_detects_begin_marker(self):
        self.assertTrue(looks_pem("-----BEGIN RSA PRIVATE KEY-----\nabc\n"))
        self.assertTrue(looks_pem("# comment\n-----BEGIN CERTIFICATE-----\n"))

    def test_rejects_non_pem(self):
        self.assertFalse(looks_pem("KEY=value\nOTHER=x\n"))
        self.assertFalse(looks_pem(""))
        self.assertFalse(looks_pem("just text"))

    def test_begin_marker_far_down_is_ignored(self):
        """先頭 40 行を超える位置の BEGIN は sniff 対象外 (dotenv 誤判定回避)。"""
        text = "\n".join(f"K{i}=v" for i in range(60))
        text += "\n-----BEGIN RSA PRIVATE KEY-----\n"
        self.assertFalse(looks_pem(text))


class TestRedactPem(unittest.TestCase):
    def test_counts_multiple_blocks(self):
        text = (
            "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
            "-----BEGIN RSA PRIVATE KEY-----\nBBBB\n-----END RSA PRIVATE KEY-----\n"
        )
        info = redact_pem(text)
        self.assertEqual(info["blocks"], 2)
        self.assertEqual(info["block_types"], ["CERTIFICATE", "RSA PRIVATE KEY"])
        self.assertEqual(info["end_markers"], 2)

    def test_dedupes_block_types_but_keeps_count(self):
        text = ("-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----\n") * 3
        info = redact_pem(text)
        self.assertEqual(info["blocks"], 3)
        self.assertEqual(info["block_types"], ["CERTIFICATE"])

    def test_mismatched_markers_are_reported(self):
        text = "-----BEGIN CERTIFICATE-----\nA\n"  # END なし
        out = format_pem(redact_pem(text))
        self.assertIn("do not match", out)

    def test_format_never_includes_body(self):
        text = "-----BEGIN X-----\nSECRETBODYLINE\n-----END X-----\n"
        out = format_pem(redact_pem(text))
        self.assertNotIn("SECRETBODYLINE", out)


class TestLegitimateKeysArePreserved(unittest.TestCase):
    """正当な鍵名が「base64 断片っぽい」だけで落とされないこと (Codex R2 P2)。

    初版は「24 文字以上 / ``_`` を含まない / 大小混在 / 数字あり」で弾いていたが、
    ``oauth2ClientSecretProduction`` のような versioned camelCase を巻き込んで
    黙って報告から消していた。判定は綴りではなく PEM ブロック状態で行う。
    """

    LEGIT_KEYS = [
        "oauth2ClientSecretProduction",
        "stripeApiKeyV2Production",
        "auth0TenantDomainStaging",
        "gcpServiceAccount2024Key",
        "DATABASE_URL",
        "apiKey",
    ]

    def test_dotenv_keeps_versioned_camelcase_keys(self):
        for name in self.LEGIT_KEYS:
            with self.subTest(name=name):
                text = f"APP=demo\n{name}=value\nAFTER=x\n"
                reason = _redact_text(".env", text)
                self.assertIn(name, reason, f"正当な鍵名が落ちた: {name}")

    def test_keyonly_scan_keeps_versioned_camelcase_keys(self):
        text = "\n".join(f"{n}: value" for n in self.LEGIT_KEYS)
        found = scan_keys(text)
        for name in self.LEGIT_KEYS:
            self.assertIn(name, found, f"正当な鍵名が落ちた: {name}")

    def test_keyonly_scan_skips_pem_body_by_block_state(self):
        """綴りではなくブロック状態で本文行を捨てること。"""
        text = (
            "REAL_KEY: value\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "AbCd12=\n"
            "NotARealKeyJustTestDataAAAABBBBCCCC1234567890abcdEFghIJkl=\n"
            "-----END RSA PRIVATE KEY-----\n"
            "AFTER_KEY: value\n"
        )
        found = scan_keys(text)
        self.assertIn("REAL_KEY", found)
        self.assertIn("AFTER_KEY", found)
        self.assertNotIn("AbCd12", found)
        self.assertEqual(len(found), 2)


class TestInlineBundleBlockCount(unittest.TestCase):
    """32KB 未満の inline bundle でも 50 block 超を数え落とさないこと (Codex R2 P2)。"""

    def _inline_bundle(self, n: int) -> str:
        return "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n" * n

    def test_counts_all_blocks_past_label_cap(self):
        info = redact_pem(self._inline_bundle(60))
        self.assertEqual(info["blocks"], 60)
        self.assertEqual(info["end_markers"], 60)

    def test_no_false_mismatch_note(self):
        out = format_pem(redact_pem(self._inline_bundle(60)))
        self.assertNotIn("do not match", out)

    def test_label_preview_is_capped_and_disclosed(self):
        out = format_pem(redact_pem(self._inline_bundle(60)))
        self.assertIn("block labels are listed", out)


class TestLabelCapBoundary(unittest.TestCase):
    """preview 打ち切りの note は label が**実際に省かれた**ときだけ出すこと。

    ちょうど ``_MAX_BLOCKS`` 件のときは全件列挙できているので
    「最初の N 件のみ」と言ってはいけない (off-by-one)。
    inline / streaming の両経路で同じ境界にする。
    """

    def _bundle_text(self, n: int) -> str:
        return "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n" * n

    def test_inline_boundary(self):
        for n, expect in ((MAX_BLOCKS - 1, False), (MAX_BLOCKS, False),
                          (MAX_BLOCKS + 1, True)):
            with self.subTest(blocks=n):
                info = redact_pem(self._bundle_text(n))
                self.assertEqual(info["blocks"], n)
                self.assertEqual(info["truncated_blocks"], expect)
                note_shown = "block labels are listed" in format_pem(info)
                self.assertEqual(note_shown, expect)

    def test_streaming_boundary(self):
        for n, expect in ((MAX_BLOCKS - 1, False), (MAX_BLOCKS, False),
                          (MAX_BLOCKS + 1, True)):
            with self.subTest(blocks=n):
                info = scan_pem_markers(BytesIO(self._bundle_text(n).encode()))
                self.assertEqual(info["blocks"], n)
                self.assertEqual(info["truncated_blocks"], expect)

    def test_both_paths_agree_on_the_boundary(self):
        """同じ入力で inline と streaming の判定が一致すること。"""
        for n in (MAX_BLOCKS - 1, MAX_BLOCKS, MAX_BLOCKS + 1):
            with self.subTest(blocks=n):
                text = self._bundle_text(n)
                inline = redact_pem(text)
                stream = scan_pem_markers(BytesIO(text.encode()))
                self.assertEqual(inline["blocks"], stream["blocks"])
                self.assertEqual(
                    inline["truncated_blocks"], stream["truncated_blocks"]
                )


if __name__ == "__main__":
    unittest.main()
