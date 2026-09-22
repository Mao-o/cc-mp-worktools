"""``.npmrc`` 内容ゲート (0.34.0) の床テスト。

既定 patterns には ``.npmrc`` を残したまま、**開いた中身に認証らしい行が
あるかどうか**で Read の deny / allow を決める。判定の定義と根拠は
``hooks/_shared/npmrc.py`` のモジュール docstring、判定表は
``docs/MATRIX.md`` の Read handler 表。

fixture のトークン値は実鍵形状の literal を書かないよう連結で組み立てる
(``tests/fixtures/keys/README.md`` と同じ方針)。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from _testutil import FIXTURES  # noqa: F401

from _shared.npmrc import (
    MAX_NPMRC_BYTES,
    decode_npmrc,
    has_auth_line,
    is_npmrc_basename,
    path_requires_block,
    scan_auth_lines,
)
from core import output
from handlers import read_handler

# 実鍵形状の literal を書かないための組み立て (push protection 対策)。
_FAKE_TOKEN = "npm_" + "x" * 36
_FAKE_PASSWORD = "p" + "w" * 15
_PEM_HEAD = "-----BEGIN " + "PRIVATE KEY" + "-----"

# 0.34.0 のマージ前レビュー P1-1 で拾えていなかった 8 形。
# **いずれも 0.33.2 では deny だった** (0.33.2 は `.npmrc` を内容に依らず
# deny していたため)。内容ゲートの初版はキー部の部分一致だけを見ていたので、
# この 8 形が deny → allow に落ちていた = 本 PR が開いた新規露出。
# 境界は「認証の設定行が存在するか」のままで、定義を実装に追いつかせた。
_P1_AUTH_FORMS: tuple[tuple[str, str], ...] = (
    # (ラベル, `.npmrc` の 1 行)  — npm 公式の設定キーのみ
    ("registry の URL に userinfo", f"registry=https://alice:{_FAKE_TOKEN}@npm.example.invalid/"),
    (
        "scoped registry の URL に userinfo",
        f"@myco:registry=https://alice:{_FAKE_TOKEN}@npm.example.invalid/",
    ),
    ("proxy 認証", f"proxy=http://alice:{_FAKE_TOKEN}@proxy.example.invalid:8080/"),
    (
        "https-proxy 認証",
        f"https-proxy=http://alice:{_FAKE_TOKEN}@proxy.example.invalid:8080/",
    ),
    ("client TLS 秘密鍵の本体", f'key="{_PEM_HEAD}"'),
    ("秘密鍵ファイルの場所", "keyfile=/home/someone/npm-client.key"),
    ("証明書ファイルの場所", "certfile=/home/someone/npm-client.crt"),
    ("2FA ワンタイムパスワード", "otp=123456"),
)

_CONFIG_ONLY = (
    "# pnpm settings\n"
    "engine-strict=true\n"
    "auto-install-peers=true\n"
    "; ini style comment\n"
    "@myscope:registry=https://registry.example.invalid/\n"
    "save-exact = true\n"
)


def _make_envelope(name: str, cwd: str, mode: str = "default") -> dict:
    return {
        "tool_name": "Read",
        "tool_input": {"file_path": name},
        "cwd": cwd,
        "permission_mode": mode,
    }


def _decision(resp: dict) -> str | None:
    return (resp.get("hookSpecificOutput") or {}).get("permissionDecision")


class TestAuthLineDetection(unittest.TestCase):
    """``has_auth_line`` の定義そのもの (docs と 1:1 で読める形にしておく)。"""

    def test_scoped_registry_auth_token(self):
        self.assertTrue(
            has_auth_line("//registry.npmjs.org/:_authToken=" + _FAKE_TOKEN + "\n")
        )

    def test_scoped_registry_with_env_reference_is_still_auth(self):
        # 値が環境変数参照でも「認証の設定行がある」= deny 側に倒す (境界を
        # 「行の存在」に固定して単純に保つ、設計書 §1)。
        self.assertTrue(
            has_auth_line("//registry.example.invalid/:_authToken=${NPM_TOKEN}\n")
        )

    def test_empty_value_is_still_auth(self):
        self.assertTrue(has_auth_line("//registry.example.invalid/:_authToken=\n"))

    def test_each_auth_key_substring(self):
        for line in (
            "_auth=" + _FAKE_TOKEN,
            "_authToken=" + _FAKE_TOKEN,
            "_password=" + _FAKE_PASSWORD,
            "username=someone",
            "email=someone@example.invalid",
            "always-auth=true",
        ):
            with self.subTest(line=line):
                self.assertTrue(has_auth_line(line + "\n"))

    def test_case_is_ignored(self):
        for line in ("_AUTHTOKEN=" + _FAKE_TOKEN, "Always-Auth=true", "EMAIL=a@b.c"):
            with self.subTest(line=line):
                self.assertTrue(has_auth_line(line + "\n"))

    def test_leading_whitespace_does_not_hide_auth_line(self):
        self.assertTrue(has_auth_line("    _authToken=" + _FAKE_TOKEN + "\n"))

    def test_comment_lines_are_ignored(self):
        for line in (
            "# _authToken=" + _FAKE_TOKEN,
            "; //registry.example.invalid/:_authToken=" + _FAKE_TOKEN,
            "   # always-auth=true",
        ):
            with self.subTest(line=line):
                self.assertFalse(has_auth_line(line + "\n"))

    def test_config_only_has_no_auth_line(self):
        self.assertFalse(has_auth_line(_CONFIG_ONLY))

    def test_empty_file_has_no_auth_line(self):
        self.assertFalse(has_auth_line(""))

    def test_section_header_is_not_auth(self):
        self.assertFalse(has_auth_line("[install]\nengine-strict=true\n"))

    def test_auth_substring_only_in_value_is_not_auth(self):
        # キー部だけを見る。値に ``_authToken`` という語があっても認証行では
        # ない (``//`` 判定も**キー部の先頭**だけを見る)。
        self.assertFalse(has_auth_line("note=see _authToken docs\n"))
        self.assertFalse(has_auth_line("note=//registry.example.invalid/\n"))

    def test_line_without_equals_falls_to_key_side(self):
        # ``=`` が無い行は行全体をキー部として読む (deny 側に倒れる)。
        self.assertTrue(has_auth_line("//registry.example.invalid/:_authToken\n"))

    def test_p1_forms_are_auth_lines(self):
        """マージ前レビュー P1-1 の 8 形 (いずれも 0.33.2 では deny だった)。"""
        for label, line in _P1_AUTH_FORMS:
            with self.subTest(form=label):
                self.assertTrue(has_auth_line(line + "\n"), msg=label)

    def test_p1_forms_are_auth_lines_among_config(self):
        """設定行に混ざっていても拾う (1 行でも当たれば認証あり)。"""
        for label, line in _P1_AUTH_FORMS:
            with self.subTest(form=label):
                self.assertTrue(has_auth_line(_CONFIG_ONLY + line + "\n"), msg=label)

    def test_exact_only_keys(self):
        """``key`` / ``cert`` / ``otp`` は**完全一致**でだけ認証行。

        部分一致にすると ``keyword`` / ``certainty`` のような無関係なキーに
        誤爆する。完全一致側を消すと P1-1 の ``key=`` / ``cert=`` / ``otp=``
        が落ちる。
        """
        for line in ("key=x", "cert=x", "otp=1", "cafile=/etc/ssl/ca.pem", "KEY=x", "Otp=1"):
            with self.subTest(line=line):
                self.assertTrue(has_auth_line(line + "\n"))
        for line in ("keyword=hello", "certainty=high", "otpauth-note=see docs"):
            with self.subTest(line=line):
                self.assertFalse(has_auth_line(line + "\n"))

    def test_userinfo_is_detected_on_the_value_side(self):
        """値が ``scheme://user:pass@host`` 形なら**キー名を問わず**認証行。"""
        self.assertTrue(
            has_auth_line(f"some-mirror=https://u:{_FAKE_TOKEN}@host.example.invalid/\n")
        )
        # userinfo が無い素の URL は設定行のまま
        for line in (
            "registry=https://npm.example.invalid/",
            "proxy=http://proxy.example.invalid:8080/",
            "@myco:registry=https://npm.example.invalid/",
            "homepage=https://example.invalid/a@b",
        ):
            with self.subTest(line=line):
                self.assertFalse(has_auth_line(line + "\n"))

    def test_quoted_userinfo_value(self):
        self.assertTrue(
            has_auth_line(f'registry="https://u:{_FAKE_TOKEN}@host.example.invalid/"\n')
        )

    def test_hyphen_and_underscore_are_equivalent(self):
        """キー名の ``-`` / ``_`` 差を吸収する (片側だけ正規化すると静かに漏れる)。"""
        for line in ("always-auth=true", "always_auth=true", "ALWAYS-AUTH=true"):
            with self.subTest(line=line):
                self.assertTrue(has_auth_line(line + "\n"))

    def test_slash_slash_key_is_detected_on_its_own(self):
        """``//`` 始まりのキーは**部分一致リストに当たらなくても**認証行。

        0.34.0 のマージ前レビュー P3-6: 既存テストの ``//`` 形はどれも
        ``_authToken`` を含んでいて部分一致側でも拾えるため、``//`` 分岐を
        壊しても 1 件も落ちなかった。
        """
        self.assertTrue(has_auth_line("//registry.example.invalid/:always-auth=true\n"))
        self.assertTrue(has_auth_line("//registry.example.invalid/:myfield=x\n"))
        # キー部の**先頭**が ``//`` であること。値側の ``//`` では発火しない
        self.assertFalse(has_auth_line("note=//registry.example.invalid/\n"))

    def test_substring_only_key_is_still_auth(self):
        """npm 標準でないキーでも識別力のある語を含めば認証行 (退行防止)。

        完全一致リストだけに整理すると ``foo_auth=`` のような形が
        deny → allow に落ち、P1-1 と同じ「新規露出」を作り直す。
        """
        for line in ("foo_auth=x", "svc_password=x", "ci-username=bot", "my_keyfile=/k"):
            with self.subTest(line=line):
                self.assertTrue(has_auth_line(line + "\n"))


class TestScanAuthLines(unittest.TestCase):
    """``scan_auth_lines``: deny reason の根拠 (件数 + キー名)。値は返さない。"""

    def test_counts_lines_and_lists_keys_in_order(self):
        text = (
            "engine-strict=true\n"
            f"//registry.example.invalid/:_authToken={_FAKE_TOKEN}\n"
            f"_password={_FAKE_PASSWORD}\n"
        )
        count, keys = scan_auth_lines(text)
        self.assertEqual(count, 2)
        self.assertEqual(keys, ["//registry.example.invalid/:_authToken", "_password"])

    def test_values_are_never_returned(self):
        text = f"//registry.example.invalid/:_authToken={_FAKE_TOKEN}\n"
        _, keys = scan_auth_lines(text)
        self.assertNotIn(_FAKE_TOKEN, "".join(keys))

    def test_duplicate_keys_are_collapsed_but_counted(self):
        text = "always-auth=true\nalways_auth=false\n"
        count, keys = scan_auth_lines(text)
        self.assertEqual(count, 2)
        self.assertEqual(keys, ["always-auth"])

    def test_config_only_is_empty(self):
        self.assertEqual(scan_auth_lines(_CONFIG_ONLY), (0, []))

    def test_separator_less_line_never_leaks_the_rest_of_the_line(self):
        """``=`` を忘れた行 (`_authToken TOKEN`) は deny のまま、ラベルは先頭語だけ (マージ前レビューの指摘)。

        以前は行全体をキー名として返しており、値が deny reason に写っていた。
        """
        token = "npm_" + "x" * 36
        count, keys = scan_auth_lines(f"_authToken {token}\n")
        self.assertEqual(count, 1)
        self.assertEqual(keys, ["_authToken"])
        self.assertNotIn(token, " ".join(keys))
        # 先頭語が認証キーでない (行全体でだけ一致する) 形は固定文言
        count, keys = scan_auth_lines(f"registry {token} _password\n")
        self.assertEqual(count, 1)
        self.assertEqual(keys, ["(no '=' separator)"])
        self.assertNotIn(token, " ".join(keys))


class TestDecodeNpmrc(unittest.TestCase):
    def test_utf8(self):
        self.assertEqual(decode_npmrc(b"engine-strict=true\n"), "engine-strict=true\n")

    def test_bom_is_stripped(self):
        decoded = decode_npmrc(b"\xef\xbb\xbfengine-strict=true\n")
        self.assertEqual(decoded, "engine-strict=true\n")

    def test_undecodable_returns_none(self):
        # UTF-16 (BOM 付き) や任意のバイナリは ``None`` = 判定不能 → deny 側。
        self.assertIsNone(decode_npmrc("_authToken=x\n".encode("utf-16")))
        self.assertIsNone(decode_npmrc(b"\xff\xfe\x00\x00binary"))


class TestIsNpmrcBasename(unittest.TestCase):
    def test_exact_and_case_insensitive(self):
        self.assertTrue(is_npmrc_basename(".npmrc"))
        self.assertTrue(is_npmrc_basename(".NPMRC"))

    def test_other_names(self):
        for name in (".npmrc.bak", "npmrc", ".pypirc", ".env"):
            with self.subTest(name=name):
                self.assertFalse(is_npmrc_basename(name))


class BaseNpmrcRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _write(self, content: str, name: str = ".npmrc") -> Path:
        p = Path(self.tmp) / name
        p.write_text(content)
        return p

    def _read(self, name: str = ".npmrc", mode: str = "default") -> dict:
        return read_handler.handle(_make_envelope(name, self.tmp, mode=mode))


class TestReadHandlerNpmrcGate(BaseNpmrcRead):
    """Read handler の判定 (docs/MATRIX.md の Read handler 表と対)。"""

    def test_config_only_is_allowed(self):
        self._write(_CONFIG_ONLY)
        for mode in ("default", "auto", "bypassPermissions"):
            with self.subTest(mode=mode):
                self.assertTrue(output.is_allow(self._read(mode=mode)))

    def test_empty_file_is_allowed(self):
        self._write("")
        self.assertTrue(output.is_allow(self._read()))

    def test_comment_only_is_allowed(self):
        self._write("# nothing to see\n; also nothing\n")
        self.assertTrue(output.is_allow(self._read()))

    def test_auth_line_is_denied(self):
        for content in (
            "//registry.example.invalid/:_authToken=" + _FAKE_TOKEN + "\n",
            "engine-strict=true\n_auth=" + _FAKE_TOKEN + "\n",
            "_password=" + _FAKE_PASSWORD + "\n",
            "always-auth=true\n",
            "# comment\n_AUTHTOKEN=" + _FAKE_TOKEN + "\n",
        ):
            with self.subTest(content=content):
                self._write(content)
                for mode in ("default", "auto", "bypassPermissions"):
                    self.assertEqual(_decision(self._read(mode=mode)), "deny")

    def test_p1_forms_are_denied_by_read(self):
        """P1-1 の 8 形は Read でも deny (0.33.2 と同じ結論に戻す)。"""
        for label, line in _P1_AUTH_FORMS:
            with self.subTest(form=label):
                self._write(_CONFIG_ONLY + line + "\n")
                resp = self._read()
                self.assertEqual(_decision(resp), "deny", msg=label)
                reason = (resp.get("hookSpecificOutput") or {}).get(
                    "permissionDecisionReason"
                ) or ""
                # 値 (トークン / PEM) は reason に出さない
                self.assertNotIn(_FAKE_TOKEN, reason, msg=label)
                self.assertNotIn(_PEM_HEAD, reason, msg=label)

    def test_denied_reason_does_not_leak_the_value(self):
        self._write("//registry.example.invalid/:_authToken=" + _FAKE_TOKEN + "\n")
        resp = self._read()
        # 先に verdict を assert する: allow に退行したとき ``KeyError`` で
        # error になると、mutation で「テストが走っていない」と区別できない。
        self.assertEqual(_decision(resp), "deny")
        reason = (resp.get("hookSpecificOutput") or {}).get(
            "permissionDecisionReason"
        ) or ""
        self.assertNotIn(_FAKE_TOKEN, reason)
        self.assertIn(".npmrc", reason)

    def test_undecodable_is_denied(self):
        p = Path(self.tmp) / ".npmrc"
        p.write_bytes("engine-strict=true\n".encode("utf-16"))
        self.assertEqual(_decision(self._read()), "deny")

    def test_oversized_is_denied(self):
        # 上限を超えるものは中身を見ずに deny (fail-closed)。認証行は 1 行も
        # 書かない — 「内容ではなくサイズで deny した」ことを固定するため。
        p = Path(self.tmp) / ".npmrc"
        p.write_text("engine-strict=true\n" * (MAX_NPMRC_BYTES // 10))
        self.assertGreater(p.stat().st_size, MAX_NPMRC_BYTES)
        self.assertEqual(_decision(self._read()), "deny")

    def test_symlink_is_unchanged(self):
        # 内容ゲートは classify の後。symlink は従来どおり ask_or_deny。
        target = Path(self.tmp) / "real-npmrc"
        target.write_text(_CONFIG_ONLY)
        os.symlink(target, Path(self.tmp) / ".npmrc")
        self.assertEqual(_decision(self._read()), "ask")
        self.assertEqual(_decision(self._read(mode="bypassPermissions")), "deny")

    def test_other_credential_files_are_unaffected(self):
        # ゲートは ``.npmrc`` 限定。``.pypirc`` / ``.netrc`` は内容に依らず deny。
        for name in (".pypirc", ".netrc"):
            with self.subTest(name=name):
                self._write("just = config\n", name=name)
                self.assertEqual(_decision(self._read(name)), "deny")


class TestNpmrcDenyReasonShowsTheEvidence(BaseNpmrcRead):
    """0.34.0 のマージ前レビュー P2-5: deny の根拠を reason の先頭に出す。

    既存の minimal info は ini の keys-only scan なので
    ``//registry…/:_authToken`` を 1 件も拾わず、reason には無害な設定キー
    だけが並んでいた (``entries: 0`` / ``(no keys matched)``)。0.34.0 で
    deny が「認証行が実際にある」という精密な信号になった以上、その信号を
    reason に出さないと「壊れたファイル」「除外レシピを足せば直る」と
    誤読される (= 保護を切る方向のナッジ)。
    """

    def _read_reason(self, content: str) -> str:
        self._write(content)
        resp = self._read()
        # 先に verdict を assert する (allow 退行時に KeyError で error に
        # ならないよう、mutation で failure として見えるようにするため)
        self.assertEqual(_decision(resp), "deny")
        return (resp.get("hookSpecificOutput") or {}).get(
            "permissionDecisionReason"
        ) or ""

    def test_reason_states_the_count_and_the_key_names(self):
        reason = self._read_reason(
            "engine-strict=true\n"
            f"//registry.example.invalid/:_authToken={_FAKE_TOKEN}\n"
            f"_password={_FAKE_PASSWORD}\n"
        )
        self.assertIn("認証設定行 2 件", reason)
        self.assertIn("//registry.example.invalid/:_authToken", reason)
        self.assertIn("_password", reason)
        self.assertNotIn(_FAKE_TOKEN, reason)
        self.assertNotIn(_FAKE_PASSWORD, reason)

    def test_evidence_comes_first(self):
        # 末尾からの盲目 cut (``output._truncate``) に食われない位置に置く。
        reason = self._read_reason(
            f"//registry.example.invalid/:_authToken={_FAKE_TOKEN}\n"
        )
        self.assertTrue(reason.startswith("note: .npmrc に認証設定行"))

    def test_userinfo_form_names_the_plain_key(self):
        reason = self._read_reason(
            f"registry=https://alice:{_FAKE_TOKEN}@npm.example.invalid/\n"
        )
        self.assertIn("認証設定行 1 件", reason)
        self.assertIn("registry", reason)
        self.assertNotIn(_FAKE_TOKEN, reason)

    def test_fail_closed_paths_do_not_claim_auth_lines(self):
        """decode 不能 / サイズ上限超は「認証行を見つけた」と書かない。"""
        p = Path(self.tmp) / ".npmrc"
        p.write_bytes("engine-strict=true\n".encode("utf-16"))
        resp = self._read()
        self.assertEqual(_decision(resp), "deny")
        reason = (resp.get("hookSpecificOutput") or {}).get(
            "permissionDecisionReason"
        ) or ""
        self.assertNotIn("認証設定行", reason)

    def test_reason_stays_in_the_byte_budget(self):
        """キー名が大量 / 長大でも予算内 (prefix は固定長)。"""
        long_key = "//registry.example.invalid/" + "z" * 400
        lines = [f"{long_key}{i}:_authToken={_FAKE_TOKEN}" for i in range(50)]
        lines += [f"KEY_{i}=value{i}" for i in range(200)]
        reason = self._read_reason("\n".join(lines) + "\n")
        self.assertLessEqual(
            len(reason.encode("utf-8")), output.MAX_REASON_BYTES
        )
        self.assertIn("認証設定行 50 件", reason)
        # 件数を絞った旨が出る (5 件 + 「ほか N 件」)
        self.assertIn("ほか 45 件", reason)
        # prefix 分を引いてから折り畳むので、盲目 cut に落ちない
        self.assertNotIn("...[truncated]", reason)
        self.assertIn("</DATA>", reason)

    def test_minimal_info_is_folded_not_blind_cut_when_the_prefix_is_added(self):
        """prefix 分を**引いてから**折り畳む (0.34.0 P2-5 の実装契約)。

        引かずに連結すると合計が 3KB 予算を超え、``output._truncate`` の
        盲目 cut が ``</DATA>`` 閉じタグと末尾 note を鍵行の途中で落とす。
        60 鍵 × 40 文字の ``.npmrc`` は折り畳みが実際に起きる大きさ。
        """
        lines = [f"//registry.example.invalid/:_authToken={_FAKE_TOKEN}"]
        lines += [f"CONFIG_KEY_{i:03d}{'z' * 40}=value{i}" for i in range(60)]
        reason = self._read_reason("\n".join(lines) + "\n")
        self.assertLessEqual(
            len(reason.encode("utf-8")), output.MAX_REASON_BYTES
        )
        self.assertNotIn("...[truncated]", reason)
        self.assertIn("</DATA>", reason)
        self.assertIn("認証設定行 1 件", reason)

    def test_pathological_key_names_fall_back_to_the_count(self):
        """キー名が上限を超えるほど長い場合は件数だけに落とす (予算保護)。"""
        lines = [
            f"//registry{i}.example.invalid/{'ぜ' * 300}:_authToken={_FAKE_TOKEN}"
            for i in range(5)
        ]
        reason = self._read_reason("\n".join(lines) + "\n")
        self.assertIn("認証設定行 5 件。", reason)
        self.assertNotIn("キー名:", reason)
        self.assertLessEqual(
            len(reason.encode("utf-8")), output.MAX_REASON_BYTES
        )


class TestPathRequiresBlock(BaseNpmrcRead):
    """Stop hook が使う path 経由版 (``checker.find_sensitive_files``)。"""

    def test_config_only_is_not_blocked(self):
        p = self._write(_CONFIG_ONLY)
        self.assertFalse(path_requires_block(str(p)))

    def test_auth_line_is_blocked(self):
        p = self._write("//registry.example.invalid/:_authToken=" + _FAKE_TOKEN + "\n")
        self.assertTrue(path_requires_block(str(p)))

    def test_missing_file_is_blocked(self):
        self.assertTrue(path_requires_block(os.path.join(self.tmp, "nope")))

    def test_symlink_is_blocked(self):
        target = Path(self.tmp) / "real-npmrc"
        target.write_text(_CONFIG_ONLY)
        link = Path(self.tmp) / ".npmrc"
        os.symlink(target, link)
        self.assertTrue(path_requires_block(str(link)))

    def test_directory_is_blocked(self):
        d = Path(self.tmp) / "dir-npmrc"
        d.mkdir()
        self.assertTrue(path_requires_block(str(d)))

    def test_oversized_is_blocked(self):
        p = self._write("engine-strict=true\n" * (MAX_NPMRC_BYTES // 10))
        self.assertGreater(p.stat().st_size, MAX_NPMRC_BYTES)
        self.assertTrue(path_requires_block(str(p)))

    def test_undecodable_is_blocked(self):
        p = Path(self.tmp) / ".npmrc"
        p.write_bytes("engine-strict=true\n".encode("utf-16"))
        self.assertTrue(path_requires_block(str(p)))

    def test_p1_forms_are_blocked(self):
        """P1-1 の 8 形は Stop 側でも報告する (Read と同じ結論)。"""
        for label, line in _P1_AUTH_FORMS:
            with self.subTest(form=label):
                p = self._write(_CONFIG_ONLY + line + "\n")
                self.assertTrue(path_requires_block(str(p)), msg=label)


if __name__ == "__main__":
    unittest.main()
