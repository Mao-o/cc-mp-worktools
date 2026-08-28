"""``_find_path_candidates`` (operand_lexer.py) のコマンド別 option 知識の単体テスト
(0.22.0)。

0.21.x までは「option で始まらない token = path 候補」の一律規則だったため、
grep 系 / jq / awk / sed の **第 1 positional (pattern / filter / script)** と、
値が path ではない option の値 (``git log -S<string>`` / ``--exclude=GLOB`` 等) が
path 候補に混ざり、``.env`` 様の文字列を含むだけで hard deny になっていた。

判定 (deny / ask / allow) は ``test_bash_handler.py`` 側で default / auto の両 mode
を assert する。ここでは候補抽出だけを固定する。
"""
from __future__ import annotations

import shlex
import unittest

from _testutil import FIXTURES  # noqa: F401

from handlers.bash.operand_lexer import _find_path_candidates


def _cands(command: str) -> list[str]:
    return _find_path_candidates(shlex.split(command, comments=False, posix=True))


class TestPatternFirstPositional(unittest.TestCase):
    """grep 系 / jq の第 1 positional は pattern であって path ではない。"""

    def test_grep_family_first_positional_is_not_a_path(self):
        cases = {
            "grep .env README.md": ["README.md"],
            "grep -rn .env src/": ["src/"],
            "grep -v .env out.txt": ["out.txt"],
            "grep -E '.env|.envrc' notes.md": ["notes.md"],
            "grep -r .env": [],
            "egrep .env README.md": ["README.md"],
            "fgrep .env README.md": ["README.md"],
            "rg .env src/": ["src/"],
            "rg -n id_rsa .": ["."],
            "ag .env .": ["."],
            "ack .env lib/": ["lib/"],
            "git grep .env": [],
            "git grep -n .env -- src/": ["src/"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_jq_filter_is_not_a_path(self):
        cases = {
            "jq .env package.json": ["package.json"],
            "jq -r .env.NODE_ENV cfg.json": ["cfg.json"],
            "jq .env": [],
            "jq . .env": [".env"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_real_path_operands_stay_candidates(self):
        # 対照: 本当に path のものは残る (deny 維持の根拠)
        cases = {
            "grep TODO .env": [".env"],
            "grep -rn TODO -- .env": [".env"],
            "grep -- .env README.md": ["README.md"],
            "rg TODO .env": [".env"],
            "jq . .env": [".env"],
            "grep -c PASSWORD .env.local": [".env.local"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_pattern_supplied_by_option_makes_positionals_paths(self):
        # -e / -f / --regexp / --file が 1 つでもあれば positional は全て path
        cases = {
            "grep -e .env README.md": ["README.md"],
            "grep -f .env x.txt": [".env", "x.txt"],
            "grep --file=.env foo README.md": [".env", "foo", "README.md"],
            "grep -f.env foo README.md": [".env", "foo", "README.md"],
            "grep -rne TODO .env": [".env"],
            "grep foo -e bar .env": ["foo", ".env"],
            "grep --regexp TODO .env": [".env"],
            "grep --regexp=TODO .env": [".env"],
            # GNU getopt_long の一意な prefix 省略
            "grep --reg=TODO .env": [".env"],
            "grep --fi .env x.txt": [".env", "x.txt"],
            "rg -e TODO .env": [".env"],
            "rg -f pats.txt .env": ["pats.txt", ".env"],
            "git grep -e TODO -- .env": [".env"],
            "jq -f filter.jq .env": ["filter.jq", ".env"],
            "jq --from-file filter.jq .env": ["filter.jq", ".env"],
            "ack --match TODO .env": [".env"],
            "ack -f .env": [".env"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_write_redirect_target_is_a_path_not_the_pattern(self):
        # safe-read の grep 系は residual metachar 判定を skip するため ``>`` token
        # がここまで届く。redirect 先は pattern ではなく (書込み先の) path。
        cases = {
            "grep foo > .env": [".env"],
            "grep > .env foo": [".env"],
            "grep foo >> .env": [".env"],
            "grep foo 2> .env": [".env"],
            "grep foo >.env": [".env"],
            "grep foo README.md > out.txt": ["README.md", "out.txt"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)


class TestAwkSedScriptIsNotAPath(unittest.TestCase):
    """awk / sed の第 1 positional は script。``-e`` / ``-f`` があれば positional は
    全て file operand。"""

    def test_script_positional_skipped(self):
        cases = {
            "sed -n 's/.env/X/p' README.md": ["README.md"],
            "sed -n '/.env/p' README.md": ["README.md"],
            "awk '/.env/ {print}' notes.txt": ["notes.txt"],
            "awk -F: '{print $1}' /etc/passwd": ["/etc/passwd"],
            "awk -v x=1 '/.env/' notes.txt": ["notes.txt"],
            "gawk '{print}' notes.txt": ["notes.txt"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_file_operands_after_script_stay_candidates(self):
        cases = {
            "sed -n 1,5p .env": [".env"],
            "sed -i 's/a/b/' .env": [".env"],
            "sed -e 's/a/b/' .env": [".env"],
            "sed --expression='s/a/b/' .env": [".env"],
            "sed --expr 's/a/b/' .env": [".env"],
            "sed -f script.sed .env": ["script.sed", ".env"],
            "awk '{print}' .env": [".env"],
            "awk -f prog.awk .env": ["prog.awk", ".env"],
            "awk -F , '{print $2}' .env": [".env"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)


class TestOptionValueIsNotAPath(unittest.TestCase):
    """値が path ではない option (検索文字列 / 正規表現 / 数値 / 書式 / glob) の値を
    候補から外す。値が path の option (patterns file / files-from / 出力先) の値と、
    値省略可の option の分離形の次 token は候補に残す。"""

    def test_git_log_like(self):
        cases = {
            "git log -S.env --oneline": [],
            "git log -S .env": [],
            "git log -G.env": [],
            "git log --grep=.env": [],
            "git log --grep .env": [],
            # 分離形の値が ``-`` で始まる (0.21.x は ``-x.env`` の ``tok[2:]`` =
            # 元の文字列に無い ``.env`` で一致していた)
            "git log --grep -x.env": [],
            "git log --author=.env": [],
            "git log --committer .env": [],
            "git log --format=.env": [],
            "git log --pretty=.env": [],
            "git log -n 5 --since=.env --until .env": [],
            "git show -s --format=%H HEAD": ["HEAD"],
            "git -C repo log -S.env": ["repo"],
            "git shortlog -s --author .env": [],
            "git diff -I .env HEAD": ["HEAD"],
            "git diff --diff-filter A -- src/": ["src/"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_git_controls_keep_path_operands(self):
        cases = {
            "git log -p -- .env": [".env"],
            "git log -p .env": [".env"],
            # ``-L <range>:<file>`` は値に file を含む (行範囲の履歴 = 内容出力)
            "git log -L1,10:.env": ["1,10:.env"],
            "git log -L 1,10:.env": ["1,10:.env"],
            # ``--pretty`` は値省略可 (``git log --pretty oneline`` は oneline を
            # path 扱いする、git 2.50 実測)。分離形の次 token は path のまま
            "git log --pretty .env -p": [".env"],
            "git log --output=.env": [".env"],
            "git log -O.env": [".env"],
            "git diff --cached .env": [".env"],
            "git show HEAD:.env": ["HEAD:.env"],
            "git commit -m x .env": [".env"],
            "git commit -F .env": [".env"],
            # commit の ``-S[<keyid>]`` は値省略可 → 分離形は消費しない
            "git commit -S .env": [".env"],
            # log の ``-m`` は値を取らない (commit の ``-m`` と別物)
            "git log -m .env -p": [".env"],
            # spec の無いサブコマンドは 0.21.x の規則
            "git format-patch -n .env": [".env"],
            "git add .env": [".env"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_grep_family_values(self):
        cases = {
            "grep -rn TODO --exclude=.env": [],
            "grep -rn TODO --exclude .env": [],
            "grep -rn TODO --exclude-dir=.env": [],
            "grep -rn TODO --include='*.env' src/": ["src/"],
            # ``-A`` の値 (数値) を pattern 枠と誤認しない
            "grep -A 3 .env README.md": ["README.md"],
            "grep -m 1 -B 2 TODO README.md": ["README.md"],
            "rg TODO -g '*.env'": [],
            "rg -g '!.env' TODO": [],
            "rg --max-depth 2 TODO src/": ["src/"],
            "rg -A 2 .env src/": ["src/"],
            "rg -t py .env src/": ["src/"],
            "ag -G .env TODO src/": ["src/"],
            "ag --ignore .env TODO": [],
            "ack --type=perl TODO lib/": ["lib/"],
            "ack -A 2 TODO lib/": ["lib/"],
            # 対照
            "grep -A3 TODO .env": [".env"],
            "rg -t py TODO .env": [".env"],
            "grep -rn TODO --exclude-from=.env src/": [".env", "src/"],
            "rg --ignore-file .env TODO": [".env"],
            # ag の ``-C [LINES]`` は値省略可 → 分離形の次 token は pattern 枠
            "ag -C 3 TODO": ["TODO"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_archive_and_sync_values(self):
        cases = {
            "tar --exclude='.env' -czf out.tgz src": ["out.tgz", "src"],
            "tar -czf out.tgz --exclude .env src": ["out.tgz", "src"],
            "tar --strip-components=1 -xf a.tgz": ["a.tgz"],
            "rsync -a --exclude='.env' src/ dst/": ["src/", "dst/"],
            "rsync -av --exclude .env --exclude '*.log' src/ host:dst/": ["src/", "host:dst/"],
            "zip -r out.zip src -x '.env'": ["out.zip", "src"],
            "unzip -l out.zip -x .env": ["out.zip"],
            # 対照
            "tar -czf out.tgz .env": ["out.tgz", ".env"],
            "tar -T .env -cf out.tgz": [".env", "out.tgz"],
            "tar -X .env -cf out.tgz src": [".env", "out.tgz", "src"],
            # GNU tar の ``--exclude-ignore[-recursive]=FILE`` は各ディレクトリ内の
            # FILE を読んで pattern にする (``-X`` と同じ「中身を読む」option)
            "tar --exclude-ignore=.env -cf out.tar src": [".env", "out.tar", "src"],
            "tar --exclude-ignore .env -cf out.tar src": [".env", "out.tar", "src"],
            "tar --exclude-ignore-recursive=.env -cf out.tar src": [".env", "out.tar", "src"],
            "tar czf out.tgz .env": ["czf", "out.tgz", ".env"],
            "rsync -a .env host:dst/": [".env", "host:dst/"],
            "rsync --files-from=.env src dst": [".env", "src", "dst"],
            "zip -r out.zip .env": ["out.zip", ".env"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_jq_and_diff_values(self):
        cases = {
            "jq --arg k .env '.[$k]' cfg.json": ["cfg.json"],
            "jq --argjson v '{\"a\":1}' . cfg.json": ["cfg.json"],
            "jq --indent 2 . cfg.json": ["cfg.json"],
            "diff --ignore-matching-lines=.env a b": ["a", "b"],
            "diff -I .env a b": ["a", "b"],
            "diff -x .env -r a b": ["a", "b"],
            # 対照 (値が path の option / 値省略可の ``-U``)
            "jq --slurpfile x .env . cfg.json": [".env", "cfg.json"],
            "jq --rawfile x .env . cfg.json": [".env", "cfg.json"],
            "diff -U 3 a b": ["3", "a", "b"],
            "diff .env .env.example": [".env", ".env.example"],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)


class TestUnknownCommandsUnchanged(unittest.TestCase):
    """option 知識の無いコマンドは 0.21.x までの規則そのまま (後方互換)。"""

    def test_legacy_rules(self):
        cases = {
            "cat .env README.md": [".env", "README.md"],
            "cmd -o=.env": [".env"],
            "gpg --keyring=.env --export": [".env"],
            "cat -- .env": [".env"],
            "openssl rsa -in key.pem": ["rsa", "n", "key.pem"],
            "cat -": [],
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(_cands(cmd), expected)

    def test_metadata_gate_inputs_remain_candidates(self):
        # ``_reads_file_content`` / ``_git_ls_files_exposes_object`` (bash_handler)
        # は生の token 列で gate を判定し、gate を抜けた後の operand scan が
        # ここで値を拾って deny する。この 4 形は候補に残り続けること。
        for cmd in ("file -f .env", "wc --files0-from=.env",
                    "tree --fromfile .env", "git ls-files -s .env"):
            with self.subTest(cmd=cmd):
                self.assertIn(".env", _cands(cmd))


if __name__ == "__main__":
    unittest.main()
