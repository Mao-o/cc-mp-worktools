"""``redaction/decoding.py`` (0.31.0、内部バックログ) の単体 + マトリクステスト。

0.30.0 までは全経路が ``raw.decode("utf-8", errors="replace")`` の一択で、

- **BOM 付き UTF-8**: 残った U+FEFF に行頭 regex が一致せず先頭 1 鍵が黙って消える
- **UTF-16**: 全バイトが U+FFFD になり ``entries: 0`` / ``(no entries)``
  = 生きた鍵を含むファイルを「空」とモデルに伝える

という誤情報を返していた。verdict (deny) は変わらないので、これは思想 2
(block 時に意図を汲んだ**正しい**情報を返す) の修正。

入力形状は ``tests/fixtures/encodings/README.md`` の方針どおり、1 本の UTF-8
ソースからテスト実行時に再エンコードする (バイト列は commit しない)。
"""
from __future__ import annotations

import codecs
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from _testutil import (  # noqa: F401
    ENCODINGS_WITH_DETECTION,
    ENCODING_SAMPLE_DOTENV,
    ENCODING_SAMPLE_DOTENV_KEYS,
    ENCODING_SAMPLE_OPAQUE,
    ENCODING_SAMPLE_YAML,
    FIXTURES,
    encode_sample,
)

from redaction.decoding import Decoded, decode_note, decode_text
from redaction.engine import MAX_INLINE_BYTES, redact, redact_large_file
from redaction.file_render import render_for_bash


def _redact_bytes(basename: str, raw: bytes) -> str:
    return redact(BytesIO(raw), basename, len(raw))


class TestBomDetection(unittest.TestCase):
    """BOM から採用コーデックが決まる。"""

    def test_plain_utf8_is_unchanged(self):
        raw = encode_sample(ENCODING_SAMPLE_DOTENV, "utf-8")
        d = decode_text(raw)
        self.assertEqual(d.encoding, "utf-8")
        self.assertFalse(d.replaced)
        self.assertEqual(d.text, ENCODING_SAMPLE_DOTENV)

    def test_utf8_bom_is_stripped(self):
        raw = encode_sample(ENCODING_SAMPLE_DOTENV, "utf-8-sig")
        d = decode_text(raw)
        self.assertEqual(d.encoding, "utf-8-sig")
        # BOM が残ると行頭 regex が効かない = 先頭 1 鍵が消える原因
        self.assertFalse(d.text.startswith("﻿"))
        self.assertEqual(d.text, ENCODING_SAMPLE_DOTENV)

    def test_utf16_with_bom(self):
        d = decode_text(encode_sample(ENCODING_SAMPLE_DOTENV, "utf-16"))
        self.assertEqual(d.encoding, "utf-16")
        self.assertEqual(d.text, ENCODING_SAMPLE_DOTENV)

    def test_utf16_explicit_bom_bytes_both_orders(self):
        for bom in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
            with self.subTest(bom=bom):
                body = ENCODING_SAMPLE_DOTENV.encode(
                    "utf-16-le" if bom == codecs.BOM_UTF16_LE else "utf-16-be"
                )
                d = decode_text(bom + body)
                self.assertEqual(d.encoding, "utf-16")
                self.assertEqual(d.text, ENCODING_SAMPLE_DOTENV)

    def test_utf32_bom_wins_over_utf16_prefix(self):
        """UTF-32 の BOM は UTF-16 の BOM を接頭辞として含む (``FF FE 00 00``)。

        判定順を間違えると UTF-32 ファイルが UTF-16 として誤デコードされる
        (鍵名の間に NUL が挟まり 1 件も拾えない)。
        """
        for enc in ("utf-32", "utf-32-le", "utf-32-be"):
            with self.subTest(enc=enc):
                raw = ENCODING_SAMPLE_DOTENV.encode(enc)
                if enc != "utf-32":
                    # LE / BE 単体は BOM を付けないので手で足す
                    raw = (
                        codecs.BOM_UTF32_LE if enc.endswith("le")
                        else codecs.BOM_UTF32_BE
                    ) + raw
                d = decode_text(raw)
                self.assertEqual(d.encoding, "utf-32")
                self.assertEqual(d.text, ENCODING_SAMPLE_DOTENV)


class TestBomLessUtf16Heuristic(unittest.TestCase):
    """BOM 無し UTF-16 は NUL の**パリティ偏り**で推定する。"""

    def test_ascii_utf16_le_and_be(self):
        for enc, expected in (("utf-16-le", "utf-16-le"), ("utf-16-be", "utf-16-be")):
            with self.subTest(enc=enc):
                d = decode_text(encode_sample(ENCODING_SAMPLE_DOTENV, enc))
                self.assertEqual(d.encoding, expected)
                self.assertEqual(d.text, ENCODING_SAMPLE_DOTENV)

    def test_non_ascii_values_still_detected(self):
        """値が非 ASCII でも鍵名・``=``・改行が ASCII なので偏りは残る。

        実測: 値を全部日本語にしても支配パリティの NUL 比率は 0.5 超
        (閾値 0.30)。
        """
        text = "KEY_A=日本語の値です\nKEY_B=もうひとつの値\n"
        for enc in ("utf-16-le", "utf-16-be"):
            with self.subTest(enc=enc):
                d = decode_text(text.encode(enc))
                self.assertEqual(d.encoding, enc)
                self.assertEqual(d.text, text)

    def test_utf8_text_is_never_taken_for_utf16(self):
        for text in (ENCODING_SAMPLE_DOTENV, "A=1\n", "# comment only\n"):
            with self.subTest(text=text[:12]):
                self.assertEqual(decode_text(text.encode("utf-8")).encoding, "utf-8")

    def test_nul_on_both_parities_is_not_utf16(self):
        """NUL を多く含むだけのバイナリ (両パリティに散る) を巻き込まない。"""
        raw = bytes(200) + bytes(range(1, 201))
        self.assertEqual(decode_text(raw).encoding, "utf-8")

    def test_short_input_is_not_guessed(self):
        for raw in (b"", b"A", b"\x00"):
            with self.subTest(raw=raw):
                self.assertEqual(decode_text(raw).encoding, "utf-8")


class TestParityAlignedBinaryGuard(unittest.TestCase):
    """0.31.0 隔離内レビュー P2-4: **NUL がパリティ整列したバイナリ**を
    UTF-16 と誤判定しない。

    パリティ条件 (``_bom_less_utf16_codec``) だけでは、NUL が片側パリティに
    偏っているバイナリが通る。通ると ``replaced`` が False になるので取りこぼし
    note も出ず、代わりに ``note: decoded as utf-16-be ...`` という**断定の
    偽情報**が出る (本リリースが消そうとしている欠陥の同型)。``decode_text`` は
    推定コーデックで先頭を decode して可読文字比率 (``_MIN_TEXT_RATIO`` = 0.90)
    を確認してから受け入れる。

    閾値は**両側から**固定する: 下の 4 形状 (実測 0.645〜0.871) が utf-8 に
    落ちること、および ``TestBomLessUtf16Heuristic`` の 7 形状 (実測
    0.970〜1.000) が検出を維持すること。片側だけだと閾値が中間値に
    ドリフトしても気付けない。
    """

    @staticmethod
    def _pcm16(little: bool) -> bytes:
        """16bit PCM の静かな区間 (上位バイトが 0 のまま低位だけ動く)。"""
        out = bytearray()
        for i in range(256):
            lo = (i * 7) % 251 + 1
            out += bytes((lo, 0)) if little else bytes((0, lo))
        return bytes(out)

    @staticmethod
    def _asn1_der() -> bytes:
        """ASN.1/DER の ``02 02 00 xx`` 形 TLV (秘密鍵 / keystore の形)。"""
        out = bytearray()
        for i in range(128):
            out += bytes((0x02, 0x02, 0x00, (i * 13) % 251 + 1))
        return bytes(out)

    @staticmethod
    def _high_byte_pairs() -> bytes:
        """``(0x?? 0x00)*`` 形 (値域が可読範囲の外)。"""
        return bytes(b for i in range(256) for b in (0x80 + (i % 0x7F), 0x00))

    def _shapes(self) -> dict[str, bytes]:
        return {
            "pcm16-le": self._pcm16(True),
            "pcm16-be": self._pcm16(False),
            "asn1-der": self._asn1_der(),
            "high-byte-pairs": self._high_byte_pairs(),
        }

    def test_parity_aligned_binaries_fall_back_to_utf8(self):
        for name, raw in self._shapes().items():
            with self.subTest(shape=name):
                d = decode_text(raw)
                self.assertEqual(
                    d.encoding, "utf-8",
                    f"{name}: パリティ整列バイナリが UTF-16 と誤判定された",
                )

    def test_binary_gets_hedged_note_not_an_encoding_claim(self):
        """偽情報 (``decoded as utf-16-*``) ではなく hedge した note に落ちる。"""
        for name, raw in self._shapes().items():
            with self.subTest(shape=name):
                note = decode_note(decode_text(raw))
                self.assertIsNotNone(note, f"{name}: 取りこぼし開示が無い")
                self.assertIn("not valid UTF-8", note)
                self.assertNotIn("decoded as utf-16", note)

    def test_binary_named_as_a_key_reports_no_keys_without_claiming_encoding(self):
        """``server.key`` に DER を置いた実測ケース (誤情報の出口を塞ぐ)。"""
        reason = redact(
            BytesIO(self._asn1_der()), "server.key", len(self._asn1_der())
        )
        self.assertNotIn("decoded as utf-16", reason)

    def test_utf16_text_with_control_chars_is_still_detected(self):
        """可読比率がぎりぎり閾値超えのテキスト (実測 0.97 帯) は維持する。"""
        text = ENCODING_SAMPLE_DOTENV.rstrip("\n") + "\nNOTE=a\x01b\x02c\x03\n"
        for enc in ("utf-16-le", "utf-16-be"):
            with self.subTest(enc=enc):
                d = decode_text(text.encode(enc))
                self.assertEqual(d.encoding, enc)
                self.assertEqual(d.text, text)

    def test_utf16_text_with_tab_and_crlf_is_still_detected(self):
        """``\\t`` / ``\\r`` / ``\\n`` は ``isprintable()`` が False なので、
        可読側に数え忘れると CRLF の ``.env`` が丸ごと巻き込まれる。"""
        text = "KEY_A=1\tvalue\r\nKEY_B=2\r\n" * 4
        for enc in ("utf-16-le", "utf-16-be"):
            with self.subTest(enc=enc):
                d = decode_text(text.encode(enc))
                self.assertEqual(d.encoding, enc)
                self.assertEqual(d.text, text)

    def test_bom_wins_over_the_text_guard(self):
        """明示的な BOM は内容で覆さない (guard は BOM 無し推定にだけ掛ける)。"""
        raw = codecs.BOM_UTF16_LE + self._high_byte_pairs()
        self.assertEqual(decode_text(raw).encoding, "utf-16")


class TestDecodeRobustness(unittest.TestCase):
    """例外を投げない / 途中で切れた入力を吸収する。"""

    def test_truncated_utf16_does_not_raise(self):
        raw = encode_sample(ENCODING_SAMPLE_DOTENV, "utf-16")[:-1]  # 奇数長
        d = decode_text(raw)
        self.assertEqual(d.encoding, "utf-16")
        self.assertIn("DATABASE_URL", d.text)

    def test_latin1_falls_back_and_discloses(self):
        """BOM も NUL も無い latin-1 は推定できない → 取りこぼしを開示する。"""
        raw = "NAME=café\n".encode("latin-1")
        d = decode_text(raw)
        self.assertEqual(d.encoding, "utf-8")
        self.assertTrue(d.replaced)
        note = decode_note(d)
        self.assertIsNotNone(note)
        self.assertIn("not valid UTF-8", note)

    def test_empty_input(self):
        self.assertEqual(decode_text(b""), Decoded("", "utf-8", False))

    def test_note_is_none_for_clean_utf8(self):
        self.assertIsNone(decode_note(decode_text(b"A=1\n")))

    def test_note_names_the_encoding(self):
        d = decode_text(encode_sample(ENCODING_SAMPLE_DOTENV, "utf-16"))
        self.assertEqual(decode_note(d), "note: decoded as utf-16 (non-UTF-8 text encoding).")

    def test_utf8_bom_note_does_not_claim_non_utf8(self):
        """``utf-8-sig`` は UTF-8 + BOM なので「非 UTF-8」と書かない (P3-1)。"""
        d = decode_text(encode_sample(ENCODING_SAMPLE_DOTENV, "utf-8-sig"))
        note = decode_note(d)
        self.assertEqual(note, "note: decoded as utf-8 with BOM.")
        self.assertNotIn("non-UTF-8", note)


class TestParserMatrix(unittest.TestCase):
    """3 パーサ (dotenv / yaml / keys-only) × エンコーディングのマトリクス。

    「entries が正しい件数になる」ことと「先頭の鍵が消えない」ことの両方を見る
    (BOM の事故は**先頭 1 件だけ**が消えるので、件数を見ないと気付けない)。
    """

    def test_dotenv_all_keys_in_every_encoding(self):
        for enc in ENCODINGS_WITH_DETECTION:
            with self.subTest(enc=enc):
                reason = _redact_bytes(
                    ".env", encode_sample(ENCODING_SAMPLE_DOTENV, enc)
                )
                self.assertIn("entries: 3", reason)
                for key in ENCODING_SAMPLE_DOTENV_KEYS:
                    self.assertIn(key, reason)
                self.assertNotIn("(no entries)", reason)

    def test_yaml_all_keys_in_every_encoding(self):
        for enc in ENCODINGS_WITH_DETECTION:
            with self.subTest(enc=enc):
                reason = _redact_bytes(
                    "secrets.yaml", encode_sample(ENCODING_SAMPLE_YAML, enc)
                )
                self.assertIn("entries: 3 (top-level)", reason)
                for key in ENCODING_SAMPLE_DOTENV_KEYS:
                    self.assertIn(key, reason)

    def test_keyonly_scan_all_keys_in_every_encoding(self):
        for enc in ENCODINGS_WITH_DETECTION:
            with self.subTest(enc=enc):
                reason = _redact_bytes(
                    "unknown.conf", encode_sample(ENCODING_SAMPLE_OPAQUE, enc)
                )
                self.assertIn("entries: 3", reason)
                for key in ENCODING_SAMPLE_DOTENV_KEYS:
                    self.assertIn(key, reason)
                self.assertNotIn("(no keys matched)", reason)

    def test_values_never_leak_in_any_encoding(self):
        for enc in ENCODINGS_WITH_DETECTION:
            with self.subTest(enc=enc):
                reason = _redact_bytes(
                    ".env", encode_sample(ENCODING_SAMPLE_DOTENV, enc)
                )
                self.assertNotIn("dummy-pass", reason)
                self.assertNotIn("dummydummy", reason)


class TestFileRenderEncodingMatrix(unittest.TestCase):
    """0.31.0 隔離内レビュー P2-2: Bash deny 側 (``render_for_bash``) も
    同じデコード層を通ることを固定する。

    初版は ``engine.redact`` だけをマトリクスで固定していたため、
    ``file_render`` のデコードを 0.30.0 の無条件 ``utf-8`` に戻す mutation が
    **両 suite すべて green** で通り抜けた。「片方だけ直すと Bash の deny
    reason だけが鍵を取りこぼす」という修正理由そのものが無保護だった。
    """

    def _render(self, enc: str, basename: str = ".env"):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / basename).write_bytes(
                encode_sample(ENCODING_SAMPLE_DOTENV, enc)
            )
            return render_for_bash(basename, tmp)

    def test_all_keys_in_every_encoding(self):
        for enc in ENCODINGS_WITH_DETECTION:
            with self.subTest(enc=enc):
                reason, info, status, _base = self._render(enc)
                self.assertIsNotNone(reason, f"{enc}: reason が None")
                self.assertEqual(status, "")
                self.assertIsNotNone(info, f"{enc}: dotenv info dict が無い")
                # 件数と鍵名を **両方** 見る: BOM の事故は先頭 1 件だけが消える
                # ので「いくつか出ている」だけの assert では検出できない。
                self.assertEqual(
                    info["entries"], 3,
                    f"{enc}: entries が 3 でない (先頭鍵 / 全鍵の欠落)",
                )
                self.assertEqual(
                    [k["name"] for k in info["keys"]],
                    list(ENCODING_SAMPLE_DOTENV_KEYS),
                    f"{enc}: 鍵名の並びが一致しない",
                )
                for key in ENCODING_SAMPLE_DOTENV_KEYS:
                    self.assertIn(key, reason, f"{enc}: {key} が reason に無い")
                self.assertNotIn("(no entries)", reason)

    def test_values_never_leak_in_any_encoding(self):
        for enc in ENCODINGS_WITH_DETECTION:
            with self.subTest(enc=enc):
                reason, _info, _status, _base = self._render(enc)
                self.assertNotIn("dummy-pass", reason)
                self.assertNotIn("dummydummy", reason)

    def test_decode_note_rides_on_the_bash_reason(self):
        """非 UTF-8 で読めたことが Bash 側の reason にも出る (engine と同じ)。"""
        reason, _info, _status, _base = self._render("utf-16")
        self.assertIn("decoded as utf-16", reason)
        plain, _i, _s, _b = self._render("utf-8")
        self.assertNotIn("decoded as", plain)


class TestStreamingEncoding(unittest.TestCase):
    """0.31.0 隔離内レビュー P2-3: 32KB 超の streaming 経路。

    UTF-8 BOM の剥離は **chunk 間の状態を必要としない** (先頭 3 byte を捨てる
    だけ) ので対応した。BOM 無し UTF-16 / UTF-32 は符号単位の状態が chunk を
    跨ぐため据え置きで、``_UNPARSED_NOTE`` の保険に委ねる。
    """

    _FIRST = "FIRST_KEY_MUST_SURVIVE"
    _KEYS = 300

    def _big_dotenv(self) -> str:
        # 鍵数は ``keyonly_scan.MAX_KEYS`` (500) 未満に抑え、長いコメント行で
        # 32KB を超えさせる (entries の期待値を固定したいため)。
        parts = [f"{self._FIRST}=dummy0\n"]
        for i in range(1, self._KEYS):
            parts.append("# " + "p" * 100 + "\n")
            parts.append(f"KEY_{i:04d}=dummy{i}\n")
        return "".join(parts)

    @staticmethod
    def _big_single_block_pem() -> str:
        # marker はリテラルを組み立てて作る (この plugin 自身が稼働している
        # 環境でファイルを書くため、鍵形状のリテラルを置かない)。
        dash = "-" * 5
        begin = dash + "BEGIN PRIVATE KEY" + dash
        end = dash + "END PRIVATE KEY" + dash
        body = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5YWJjZGVm\n"
        return begin + "\n" + body * 700 + end + "\n"

    def test_utf8_bom_first_key_survives_in_stream(self):
        """BOM が残ると先頭 1 鍵だけが消える。0 件ではないので保険も出ない。"""
        text = self._big_dotenv()
        for enc in ("utf-8", "utf-8-sig"):
            with self.subTest(enc=enc):
                raw = encode_sample(text, enc)
                self.assertGreater(len(raw), MAX_INLINE_BYTES)
                reason = redact_large_file(BytesIO(raw), ".env")
                self.assertIn(f"entries: {self._KEYS}", reason)
                self.assertIn(self._FIRST, reason)

    def test_utf16_stream_stays_keyonly_but_discloses(self):
        """据え置き側。誤認を断定ではなく「不明」に落とす保険が効くこと。"""
        for enc in ("utf-16", "utf-16-le"):
            with self.subTest(enc=enc):
                raw = encode_sample(self._big_dotenv(), enc)
                reason = redact_large_file(BytesIO(raw), ".env")
                self.assertIn("entries: 0", reason)
                # >32KB は format 名を問わず keys-only scan の保険が出る
                # (``format_keyonly`` 側。dotenv パーサは通らない)。
                self.assertIn("matched no keys (encoding", reason)

    def test_utf8_bom_single_block_pem_routes_to_pem(self):
        """BEGIN が先頭行にしか無い形は BOM で pem 経路から外れていた。

        ``pem._BEGIN_RE`` は ``^`` 固定なので、残った U+FEFF で 1 行目が
        一致しない。複数 block のバンドルは 2 本目以降の BEGIN が行頭に来る
        ため偶然通っており、単一 block のときだけ ``opaque`` に落ちていた。
        """
        text = self._big_single_block_pem()
        for enc in ("utf-8", "utf-8-sig"):
            with self.subTest(enc=enc):
                raw = encode_sample(text, enc)
                self.assertGreater(len(raw), MAX_INLINE_BYTES)
                reason = redact_large_file(BytesIO(raw), "server.key")
                self.assertIn("format: pem", reason)
                self.assertIn("blocks: 1", reason)
                self.assertIn("PRIVATE KEY", reason)

    def test_utf16_pem_is_deliberately_not_routed_to_pem(self):
        """UTF-16 は pem 経路に**乗せない** (``_PEM_SNIFF_CODECS``)。

        ``scan_pem_markers`` は chunk を無条件 utf-8 としてデコードするので、
        UTF-16 では marker を 1 本も数えられず ``blocks: 0`` を**断定**する。
        「0 block の armored file」という偽情報を出すより、keys-only scan に
        残して「鍵が 0 件 / エンコーディングか format が違うかも」と言うほうが
        正確。この選択が黙って反転しないように固定する。
        """
        text = self._big_single_block_pem()
        for enc in ("utf-16", "utf-16-le"):
            with self.subTest(enc=enc):
                raw = encode_sample(text, enc)
                reason = redact_large_file(BytesIO(raw), "server.key")
                self.assertNotIn("format: pem", reason)
                self.assertIn("matched no keys", reason)


class TestUnparsedDisclosure(unittest.TestCase):
    """``entries: 0`` を「空ファイル」と言い切らない (保険、0.31.0)。"""

    def test_dotenv_zero_entries_with_content_discloses(self):
        # 推定できない latin-1 の非 ASCII だけのファイル (鍵行が 1 つも無い形)
        raw = "résumé sans clé\n".encode("latin-1")
        reason = _redact_bytes(".env", raw)
        self.assertIn("entries: 0", reason)
        self.assertIn("(no entries)", reason)
        self.assertIn("no KEY=value lines were parsed", reason)

    def test_dotenv_truly_empty_file_says_nothing_extra(self):
        reason = _redact_bytes(".env", b"")
        self.assertIn("(no entries)", reason)
        self.assertNotIn("no KEY=value lines were parsed", reason)

    def test_keyonly_zero_keys_with_content_discloses(self):
        reason = _redact_bytes("unknown.conf", b"\x81\x82\x83 no keys here\n")
        self.assertIn("(no keys matched)", reason)
        self.assertIn("matched no keys", reason)

    def test_blank_or_comment_only_dotenv_says_nothing_extra(self):
        """0.31.0 隔離内レビュー P3-2: 「読めなかった」のではなく「鍵が無い」。

        命題 (「空ではないが KEY=value 行を parse しなかった」) 自体は真だが、
        ``(encoding or format may differ)`` の部分が誤誘導になる。理解できて
        いる行 (空行 / コメント) しか無い場合は出さない。
        """
        for raw in (b"\n\n", b"# all commented out\n", b"   \n\t\n"):
            with self.subTest(raw=raw):
                reason = _redact_bytes(".env", raw)
                self.assertIn("(no entries)", reason)
                self.assertNotIn("no KEY=value lines were parsed", reason)

    def test_dotenv_with_only_a_pem_value_is_not_called_an_encoding_problem(self):
        """PEM block は「理解できている行」なので unparsed に数えない。

        数えると、PEM を値に持つ ``.env`` で「エンコーディングが違うかも」と
        言い出す (直そうとしている誤情報の移設になる)。
        """
        dash = "-" * 5
        raw = (
            dash + "BEGIN PRIVATE KEY" + dash + "\n"
            "QUJDREVGR0hJSktMTU5PUFFS\n"
            + dash + "END PRIVATE KEY" + dash + "\n"
        ).encode("utf-8")
        reason = _redact_bytes(".env", raw)
        self.assertIn("(no entries)", reason)
        self.assertNotIn("no KEY=value lines were parsed", reason)

    def test_parse_failure_does_not_double_report_the_cause(self):
        """0.31.0 隔離内レビュー P3-3: 原因が特定できているときは重ねない。

        ``note: JSON parse failed — ...`` が原因を断定している横で
        「エンコーディングか format が違うかもしれない」を並べると、
        確定している話を不確かに見せる。保険は ``reason == "large"`` 限定。
        """
        for basename, raw, marker in (
            ("credentials.json", b"{not json at all", "JSON parse failed"),
            ("secrets.toml", b"[broken", "TOML parse failed"),
        ):
            with self.subTest(basename=basename):
                reason = _redact_bytes(basename, raw)
                self.assertIn(marker, reason)
                # gate が効いている経路であること (鍵 0 件の分岐に入っていないと
                # assertNotIn が空振りする)。
                self.assertIn("(no keys matched)", reason)
                self.assertNotIn("matched no keys (encoding", reason)

    def test_large_keyonly_still_discloses(self):
        """negative control: ``reason == "large"`` の経路では保険が出続ける。"""
        raw = ("\x81" * 40 + "\n") * 900
        data = raw.encode("latin-1")
        self.assertGreater(len(data), MAX_INLINE_BYTES)
        reason = redact_large_file(BytesIO(data), "unknown.conf")
        self.assertIn("matched no keys (encoding", reason)


if __name__ == "__main__":
    unittest.main()
