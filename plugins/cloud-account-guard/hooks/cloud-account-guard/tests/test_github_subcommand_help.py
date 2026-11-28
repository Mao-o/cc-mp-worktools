"""gh のサブコマンドの後ろの `--help` を READONLY にする (v0.30.0)。

0.29.x までは READONLY の情報系が CLI 名直後の形 (`gh --help`) だけだったので、
`gh auth switch --help` は切替 (STATE_CHANGING。成功 cache の破棄・連結規則の切替側)、
`gh auth token --help` は開示 (DISCLOSING) として扱われ、不一致・未設定で deny されていた。

読み取りにするのは「gh がヘルプを出して終わる」と文字列から言える形だけ
(`services/github.py` の `_SUBCOMMAND_HELP` の説明)。右の注記の「実測」は gh 2.105 を
認証の無い一時 HOME で走らせた結果 (CHANGELOG の 0.30.0 の節)。

- option の値の位置の `--help` (`--title --help`) は値になり、コマンドが走る (実測)
- `-h` は数えない (`gh auth switch -h <host>` は切替)
- alias / extension は `--help` を中身に渡す (実測) ので、組み込みのコマンドのパスだけ。alias は
  グループ (子を持つコマンド。`gh pr`) の下にも置ける (実測) ので、グループは直後が `--help` の形だけ
"""
from __future__ import annotations

import os
import re
import time
import unittest
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import BaseWithTmpProject, setUpModule, tearDownModule  # noqa: F401

from core import tiers  # noqa: E402
from core.command_parser import extract_candidates  # noqa: E402
from core.dispatcher import (  # noqa: E402
    _analyze_command,
    _candidate_forms,
    _match_service,
    _segment_facts,
    _service_name,
    _unexpected_switch_before_write,
    dispatch,
)
from services import github  # noqa: E402

READONLY = tiers.READONLY
QUERY = tiers.QUERY
WRITE = tiers.WRITE

_MISMATCH = (
    "GitHub [github.com] アカウント不一致: 現在=work, 期待=Mao-o"
    " — 切り替え: gh auth switch --hostname github.com --user Mao-o"
)
_CHAIN_MARKER = "切替と書込を同一コマンドに連結しています"


class _Case(unittest.TestCase):
    """分解・分類の例外を assertion の失敗にする (変異で例外が漏れても FAIL として数える)。"""

    def call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def single(self, command: str) -> str:
        cands = self.call(extract_candidates, command)
        self.assertEqual(len(cands), 1, f"複数セグメントに分解された: {command!r} -> {cands!r}")
        return cands[0][0]

    def tier(self, command: str):
        cand = self.single(command)
        svc = self.call(_match_service, cand)
        if svc is None:
            return None, None
        forms = self.call(_candidate_forms, cand, svc)
        return _service_name(svc), self.call(tiers.classify, forms, svc)

    def changes_github(self, command: str) -> bool:
        cand = self.single(command)
        facts = self.call(_segment_facts, cand)
        self.assertEqual(len(facts), 4)
        return github in facts[3]


# 読み取りになる形 (どれも実測でヘルプを出して終わる)。右は 0.29.11 の tier。
HELP_FORMS = [
    ("gh auth switch --help", WRITE),
    ("gh auth token --help", WRITE),  # DISCLOSING の形そのもの
    ("gh auth login --help", WRITE),
    ("gh auth refresh --help", WRITE),
    ("gh auth logout --help", READONLY),
    ("gh auth status --help", QUERY),  # 宣言外の option で降格していた
    ("gh auth git-credential --help", WRITE),
    ("gh pr list --help", QUERY),
    ("gh pr create --help", WRITE),
    ("gh pr --help", WRITE),
    ("gh api --help", WRITE),
    ("gh api repos/o/r --help", WRITE),  # 位置引数の後ろもヘルプ (実測)
    ("gh pr view 123 --help", QUERY),
    ("gh repo deploy-key add --help", WRITE),
    ("gh repo\tdeploy-key  add --help", WRITE),  # パスの語の間のタブ・複数の空白
    ("gh repo deploy-key --help", WRITE),  # 2 段目のグループの直後
    ("gh auth git-credential x --help", WRITE),  # hidden (実測でヘルプ)
    ("gh pr create --title x --help", WRITE),  # 値の後ろ
    ("gh pr create --title=x --help", WRITE),  # 値を `=` で付けた option の後ろ
    ("gh pr create -t=x --help", WRITE),
    ("gh pr create - --help", WRITE),  # `-` 単独は位置引数
    ("gh auth switch -h ghe.example.com --help", WRITE),
    ("gh auth switch --user other --help", WRITE),
    ("gh pr create --help=false --help", WRITE),  # 後勝ちでヘルプ
    ("gh pr create --help 2>&1", WRITE),
    ("gh pr create --help >/dev/null 2>&1", WRITE),
    ("gh pr create --help > out.txt", WRITE),
    ("gh pr create --help < /dev/null", WRITE),
    ("gh pr create --help &>/dev/null", WRITE),
    ("gh pr create --help>/dev/null", WRITE),
    ("gh pr create --help\t2>&1", WRITE),
    ("GH_HOST=ghe.example.com gh pr create --help", WRITE),  # 行頭の代入は剥がれる
    ("sudo gh pr create --help", WRITE),  # wrapper は剥がれる
]

# 読み取りにしない形 (従来どおり)。右は期待する tier。
NOT_HELP_FORMS = [
    # -h はヘルプとして数えない (DoD 3)
    ("gh auth switch -h ghe.example.com", WRITE),
    ("gh pr create -h", WRITE),
    # option の値の位置の --help (DoD 4。実測で実行される)
    ("gh pr create --title --help", WRITE),
    ("gh pr create -t --help", WRITE),
    ("gh pr create -dt --help", WRITE),
    ("gh pr create --body-file --help", WRITE),
    ("gh auth switch -h --help", WRITE),
    ("gh auth switch --user --help", WRITE),
    ("gh auth switch --hostname --help", WRITE),
    ("gh auth switch -u --help", WRITE),
    ("gh pr list -L --help", QUERY),
    # bool の flag の直後も値の位置と区別できないので数えない (実際はヘルプ)
    ("gh pr create --draft --help", WRITE),
    ("gh auth status --show-token --help", WRITE),  # DISCLOSING のまま
    # ヘルプの形に当たるが、DISCLOSING が READONLY より先に効く (実際はヘルプ。止める側)
    ("gh auth status --show-token=true --help", WRITE),
    ("gh auth status -t=1 --help", WRITE),
    # --help が最後の語ではない / 後ろで取り消しうる
    ("gh pr create --help --fill", WRITE),
    ("gh pr create --help --help=false", WRITE),
    ("gh pr create --help=true", WRITE),
    ("gh auth token --help --show-token", WRITE),
    ("gh pr create --help -- x", WRITE),
    # `--` の後ろは位置引数 (`--` が直前でなくても)
    ("gh pr create -- --help", WRITE),
    ("gh pr create x -- --help", WRITE),
    ("gh pr create -- x --help", WRITE),
    ("gh api -- repos/o/r --help", QUERY),  # `gh api` の判定は `--` の後ろを見ない (0.29.11 と同じ)
    ("gh pr create --\tx --help", WRITE),
    # 語の位置を文字列から決められない形 (引用符・展開・glob・コメント・チルダ・`=` 始まり)。
    # 直前の語と、途中の語 (`... y --help`) の両方
    ("gh pr create $X y --help", WRITE),
    ("gh pr create 'a b' y --help", WRITE),
    ("gh pr create *.txt y --help", WRITE),
    ("gh pr create =x y --help", WRITE),
    ("gh pr create ~/x y --help", WRITE),
    ("gh pr create x#y z --help", WRITE),
    ("gh pr create '--help'", WRITE),
    ('gh pr create "--help"', WRITE),
    ("gh pr create '--title' --help", WRITE),
    ("gh pr create $X --help", WRITE),
    ('gh pr create --title "$X" --help', WRITE),
    ("gh pr create --help $X", WRITE),
    ("gh pr create *.txt --help", WRITE),
    ("gh pr create {a,b} --help", WRITE),
    ("gh pr create ~/x --help", WRITE),
    ("gh pr create =x --help", WRITE),
    ("gh pr create x#y --help", WRITE),
    # `--help` の続きの語 (`--help2` は別の option)
    ("gh pr create --help2>&1", WRITE),
    ("gh pr create --help-me", WRITE),
    ("gh pr create --HELP", WRITE),
    # 組み込みでないコマンド (alias / extension / 別名) と外した組み込み
    ("gh co --help", WRITE),
    ("gh myalias --help", WRITE),
    ("gh extension exec x --help", WRITE),
    ("gh ext exec x --help", WRITE),
    ("gh extension install o/gh-x --help", WRITE),
    ("gh copilot --help", WRITE),
    ("gh skill install o/r --help", WRITE),
    ("gh agent-task list --help", WRITE),
    ("gh discussion list --help", WRITE),
    ("gh event listen --help", WRITE),
    ("gh cs ssh --help", WRITE),
    ("gh prx create --help", WRITE),
    ("gh issue ls --help", WRITE),  # 組み込みの別名は表に入れていない
    # グループの直後の語が組み込みの子でない形。gh はグループの下にも alias を置け、alias は
    # `--help` を中身に渡す (実測: `gh alias set --shell 'pr zz' 'echo RAN "$@"'` の後の
    # `gh pr zz --help` は `RAN --help`、`gh pr -R o/r zz --help` は `RAN -R o/r --help`)
    ("gh issue mine --help", WRITE),  # gh alias set --help の例の名前
    ("gh pr ship --help", WRITE),
    ("gh auth sw --help", WRITE),
    ("gh alias zz --help", WRITE),
    ("gh repo deploy-key zz --help", WRITE),  # 2 段目のグループの下
    ("gh pr -R o/r zz --help", WRITE),  # グループの option の後ろ
    ("gh pr zz x --help", WRITE),
    ("GH_CONFIG_DIR=cfg gh pr ship --help", WRITE),
    ("gh pr -R o/r --help", WRITE),  # グループは直後が --help の形だけ
    ("gh codespace ports zz --help", WRITE),
    # 書込・開示そのもの
    ("gh pr create", WRITE),
    ("gh auth token", WRITE),
    ("gh pr list", QUERY),
]


class TestTier(_Case):
    def test_help_forms_are_readonly(self):
        for command, before in HELP_FORMS:
            with self.subTest(command=command):
                self.assertEqual(self.tier(command), ("github", READONLY), f"0.29.11 は {before}")

    def test_other_forms_keep_their_tier(self):
        for command, expected in NOT_HELP_FORMS:
            with self.subTest(command=command):
                self.assertEqual(self.tier(command), ("github", expected))

    def test_other_services_subcommand_help_is_still_verified(self):
        """範囲は gh だけ (aws / gcloud / kubectl / firebase のサブコマンドの --help は従来どおり)。"""
        for command, service in (
            ("aws s3 cp --help", "aws"),
            ("aws s3 cp help", "aws"),
            ("gcloud run deploy --help", "gcloud"),
            ("kubectl apply --help", "kubectl"),
            ("firebase deploy --help", "firebase"),
            ("firebase use --help", "firebase"),
        ):
            with self.subTest(command=command):
                name, tier = self.tier(command)
                self.assertEqual(name, service)
                self.assertNotEqual(tier, READONLY)


class TestHelpCommands(_Case):
    def test_every_leaf_reads_help(self):
        """末端 (子を持たないパス) は、後ろに語を置いた形もヘルプ (末端の下には alias を置けない)。"""
        for path in github._HELP_LEAVES:
            for command in (f"gh {path} --help", f"gh {path} x --help", f"gh {path} x y --help"):
                with self.subTest(command=command):
                    self.assertEqual(self.tier(command), ("github", READONLY))

    def test_groups_read_help_only_directly(self):
        """グループは直後が `--help` の形だけ。直後の語が組み込みの子でなければ alias でありうる。"""
        for path in github._HELP_GROUPS:
            with self.subTest(command=f"gh {path} --help"):
                self.assertEqual(self.tier(f"gh {path} --help"), ("github", READONLY))
            for command in (f"gh {path} zz --help", f"gh {path} zz x --help",
                            f"gh {path} -R o/r zz --help"):
                with self.subTest(command=command):
                    name, tier = self.tier(command)
                    self.assertEqual(name, "github")
                    self.assertNotEqual(tier, READONLY)

    def test_tables_are_split_by_children(self):
        """子を持つパスがグループ、持たないパスが末端 (グループを末端に混ぜると、その下の alias が
        読み取りになる)。グループはどれも表の中に子を持つ。"""
        paths = github._HELP_LEAVES + github._HELP_GROUPS
        self.assertEqual(len(set(paths)), len(paths), "表に重複か、両方に入ったパスがある")
        self.assertGreater(len(github._HELP_GROUPS), 20)
        self.assertGreater(len(github._HELP_LEAVES), 150)
        for path in paths:
            with self.subTest(path=path):
                has_child = any(other.startswith(path + " ") for other in paths)
                self.assertEqual(path in github._HELP_GROUPS, has_child)
                if " " in path:
                    self.assertIn(path.rsplit(" ", 1)[0], github._HELP_GROUPS)

    def test_passthrough_and_preview_commands_are_not_listed(self):
        """引数を別のプログラムに渡す / (preview) のコマンドと alias は表のパスの先頭に無い。"""
        tops = {path.split()[0] for path in github._HELP_LEAVES + github._HELP_GROUPS}
        for name in ("copilot", "extension", "extensions", "ext", "agent-task", "discussion",
                     "event", "skill", "co", "cs"):
            with self.subTest(name=name):
                self.assertNotIn(name, tops)
                self.assertNotEqual(self.tier(f"gh {name} --help")[1], READONLY)

    def test_command_name_must_be_a_whole_word(self):
        """`pr` が `prx` / `pr-x` の先頭に当たらない (表の名前は語として照合する)。"""
        for command in ("gh prx --help", "gh pr-x --help", "gh authx switch --help", "gh 'pr' --help"):
            with self.subTest(command=command):
                self.assertNotEqual(self.tier(command)[1], READONLY)


class TestStateChanging(_Case):
    """ヘルプの形は成功 cache を破棄せず、連結規則の切替側にもならない (DoD 2)。"""

    def test_help_forms_do_not_change_state(self):
        for command in (
            "gh auth switch --help",
            "gh auth login --help",
            "gh auth logout --help",
            "gh auth refresh --help",
            "gh auth switch --user other --help",
            "gh auth switch -h ghe.example.com --help",
            "gh auth switch --help 2>&1",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.changes_github(command))

    def test_real_switches_still_change_state(self):
        for command in (
            "gh auth switch",
            "gh auth switch -h ghe.example.com",
            "gh auth switch --user --help",
            "gh auth switch -h --help",
            "gh auth switch --help --user other",
            "gh auth switch --draft --help",
            "gh auth login --hostname --help",
            "gh auth login --skip-ssh-key",
            "gh auth refresh --scopes --help",
            "gh auth logout --user --help",
        ):
            with self.subTest(command=command):
                self.assertTrue(self.changes_github(command))


class TestUnlessHelpIsExact(_Case):
    """`_unless_help(p)` が当たる ⟺ 0.29.11 の p が当たり、かつヘルプの形ではない。

    STATE_CHANGING / DISCLOSING から外れるのはヘルプの形だけで、それ以外は 0.29.11 と
    同じ形に当たり続けることを、入力の集合全体で確かめる。
    """

    OLD_STATE = r"^gh\s+auth\s+(switch|login|logout|refresh)\b"
    OLD_TOKEN = r"^gh\s+auth\s+token(?=\s|$)"

    def _inputs(self):
        heads = ["gh auth switch", "gh auth login", "gh auth logout", "gh auth refresh",
                 "gh auth token", "gh auth status", "gh pr create", "gh auth switch-x"]
        tails = ["", " --help", " x --help", " --user --help", " --user x --help", " -h --help",
                 " -h h --help", " --help 2>&1", " --help --fill", " -- --help", " $X --help",
                 " --help=false --help", " --show-token --help", "\t--help", " --help >f"]
        inputs = [h + t for h in heads for t in tails]
        inputs += [c for c, _t in HELP_FORMS + NOT_HELP_FORMS]
        return inputs

    def test_state_changing(self):
        self.assertEqual(len(github.STATE_CHANGING), 1)
        new = re.compile(github.STATE_CHANGING[0])
        self._check(new, re.compile(self.OLD_STATE))

    def test_disclosing_token(self):
        entries = [p for p, opts in github.DISCLOSING if "auth\\s+token" in p]
        self.assertEqual(len(entries), 1, github.DISCLOSING)
        self._check(re.compile(entries[0]), re.compile(self.OLD_TOKEN))

    def _check(self, new, old):
        help_re = re.compile(github._RO_SUBCOMMAND_HELP)
        hits = 0
        for cand in self._inputs():
            with self.subTest(cand=cand):
                is_help = bool(help_re.search(cand))
                expected = bool(old.search(cand)) and not is_help
                self.assertEqual(bool(new.search(cand)), expected)
                hits += expected
        self.assertGreater(hits, 5, "空振り: 0.29.11 の pattern に当たる入力が無い")

    def test_requires_anchor(self):
        with self.assertRaises(AssertionError):
            github._unless_help(r"gh\s+auth")


class TestDispatch(BaseWithTmpProject):
    """hook 全体の判定 (verify は mock)。"""

    def _run(self, command: str, verify_value=_MISMATCH):
        with mock.patch("services.github.verify", return_value=verify_value) as v:
            try:
                result = dispatch(command, str(self.project_dir))
            except Exception as e:  # noqa: BLE001
                self.fail(f"例外: {e!r}")
        return result, v

    def _reason(self, result) -> str:
        self.assertIsNotNone(result)
        out = result["hookSpecificOutput"]
        self.assertEqual(out.get("permissionDecision"), "deny", out)
        return out["permissionDecisionReason"]

    def test_ticket_line_passes_on_mismatch(self):
        """DoD 1: チケットの 1 行は不一致でも通り、照合もしない。"""
        self._write_accounts({"github": "Mao-o"})
        command = "gh auth switch --help 2>&1; gh auth token --help 2>&1"
        with self.isolated_cache():
            result, v = self._run(command)
        self.assertIsNone(result)
        self.assertFalse(v.called)

    def test_help_passes_without_accounts_file(self):
        """DoD 1: 未設定 (accounts.local.json が無い) でも deny しない。"""
        for command in ("gh auth switch --help", "gh auth token --help", "gh pr create --help"):
            with self.subTest(command=command), self.isolated_cache():
                result, v = self._run(command)
                self.assertIsNone(result)
                self.assertFalse(v.called)

    def test_real_write_on_the_same_line_is_still_verified(self):
        """DoD 5: 読み取りにした --help と同じ行の書込は従来どおり照合する。"""
        self._write_accounts({"github": "Mao-o"})
        for command, detected in (
            ("gh pr create --help && gh pr create --fill", "gh pr create --fill"),
            ("gh auth switch --help 2>&1; gh pr create --fill", "gh pr create --fill"),
            ("gh pr create --fill; gh auth token --help", "gh pr create --fill"),
        ):
            with self.subTest(command=command), self.isolated_cache():
                result, v = self._run(command)
                reason = self._reason(result)
                self.assertEqual(v.call_count, 1)
                self.assertIn(f"(検出コマンド: {detected})", reason)
                self.assertNotIn("--help", reason.split("(検出コマンド:", 1)[1])

    def test_value_position_help_is_still_verified(self):
        """DoD 4: option の値の位置の --help は WRITE のまま照合する。"""
        self._write_accounts({"github": "Mao-o"})
        for command in ("gh pr create --title --help", "gh pr create --draft --help"):
            with self.subTest(command=command), self.isolated_cache():
                result, v = self._run(command)
                self._reason(result)
                self.assertEqual(v.call_count, 1)

    def test_help_switch_is_not_chained_with_write(self):
        """DoD 2: ヘルプの切替は連結規則の切替側にならない (切替前の状態での照合が一致なら通す)。"""
        self._write_accounts({"github": "Mao-o"})
        for command in (
            "gh auth switch --help && gh pr create",
            "gh auth switch --user other --help && gh pr create",
            "gh auth login --help; gh pr create --fill",
        ):
            with self.subTest(command=command), self.isolated_cache():
                result, v = self._run(command, verify_value=None)
                self.assertIsNone(result)
                self.assertEqual(v.call_count, 1)

    def test_help_write_is_not_chained_after_a_switch(self):
        """DoD 2: ヘルプの書込は連結規則の書込側にならない。"""
        self._write_accounts({"github": "Mao-o"})
        with self.isolated_cache():
            result, _v = self._run("gh auth switch --user other && gh pr create --help", None)
        self.assertIsNone(result)

    def test_real_switch_is_still_chained(self):
        """DoD 3 / 4: `-h <host>` と値の位置の --help の切替は、従来どおり連結規則で止める。"""
        self._write_accounts({"github": "Mao-o"})
        for command in (
            "gh auth switch -h ghe.example.com && gh pr create",
            "gh auth switch --user --help && gh pr create",
            "gh auth switch -h --help && gh pr create",
            "gh auth switch --help --user other && gh pr create",
        ):
            with self.subTest(command=command), self.isolated_cache():
                result, v = self._run(command, verify_value=None)
                self.assertIn(_CHAIN_MARKER, self._reason(result))
                self.assertFalse(v.called)

    def test_chain_pairs(self):
        """連結規則の組そのもの (dispatcher の判定の手前)。"""
        for command, expected in (
            ("gh auth switch --help && gh pr create", None),
            ("gh auth switch --user other && gh pr create --help", None),
            ("gh auth switch --user --help && gh pr create",
             ("gh auth switch --user --help", "gh pr create")),
        ):
            with self.subTest(command=command):
                analyzed = self._call(_analyze_command, command)
                self.assertEqual(len(analyzed), 3)
                got = self._call(_unexpected_switch_before_write, github, "Mao-o", analyzed[2])
                self.assertEqual(got, expected)

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")

    def test_help_does_not_invalidate_success_cache(self):
        """DoD 2: ヘルプの切替は成功 cache を破棄しない (値の位置の --help は破棄する)。"""
        self._write_accounts({"github": "Mao-o"})
        for command, invalidates in (
            ("gh auth switch --help", False),
            ("gh auth login --help 2>&1", False),
            ("gh auth switch --user --help", True),
        ):
            with self.subTest(command=command), self.isolated_cache():
                with mock.patch("services.github.verify", return_value=None) as v:
                    self.assertIsNone(dispatch("gh pr list", str(self.project_dir)))
                    self.assert_cache_published("gh pr list", v)
                    dispatch(command, str(self.project_dir))
                    before = v.call_count
                    self.assertIsNone(dispatch("gh pr create", str(self.project_dir)))
                    self.assertEqual(v.call_count - before, 1 if invalidates else 0)


@unittest.skipUnless(os.environ.get("RUN_TIMING_TESTS") == "1", "時間を測るテスト (RUN_TIMING_TESTS=1 で走る)")
class TestHelpRegexIsLinear(unittest.TestCase):
    """繰り返しを含む regex が語数に線形であること (2 乗なら 4 倍の入力で 16 倍)。

    小さい入力と大きい入力は同じ単位の繰り返しなので、同じ経路を通る。
    """

    UNITS = (" -x", " a", " a=b", " 2>&1", " >>f", " <<-x", "  ", " >")
    TAILS = ("", " --help $", " -x --help", " --help 2>&1")
    # 4 倍の入力で、線形なら比は約 4、2 乗なら約 16。
    LIMIT = 10.0

    @staticmethod
    def _best(pattern, text):
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            pattern.search(text)
            best = min(best, time.perf_counter() - started)
        return best

    def _times(self, pattern, small_text, large_text):
        """small と large の時間 (それぞれの最小値)。

        比が LIMIT 以上になった点だけ、両方を最大 3 回まで測り足して最小値を更新する。負荷の
        揺れは時間を伸ばす向きにしか働かないので、測り足すほど最小値は揺れの無い時間に近づき、
        2 乗なら何度測っても比は 16 前後のまま残る。並走の負荷 (load 4〜5) の下で、1 回ずつの
        測定の比の最悪値が 10.13 になって落ちたことがある。
        """
        small = self._best(pattern, small_text)
        large = self._best(pattern, large_text)
        for _ in range(3):
            if large >= 1.0 or small <= 2e-4 or large / small < self.LIMIT:
                break
            small = min(small, self._best(pattern, small_text))
            large = min(large, self._best(pattern, large_text))
        return small, large

    def _assert_linear(self, pattern, small_text, large_text, where):
        """large が 1 秒未満で、small が 0.2 ms を超えるなら比が LIMIT 未満 (最初に外れた点で落ちる)。"""
        small, large = self._times(pattern, small_text, large_text)
        self.assertLess(large, 1.0, where)
        if small > 2e-4:
            self.assertLess(large / small, self.LIMIT, where)

    def _patterns(self):
        token = [p for p, _o in github.DISCLOSING if "auth\\s+token" in p]
        self.assertEqual(len(token), 1, github.DISCLOSING)
        self.assertEqual(len(github.STATE_CHANGING), 1)
        return (
            re.compile(github._RO_SUBCOMMAND_HELP),
            re.compile(github.STATE_CHANGING[0]),
            re.compile(token[0]),
        )

    def test_linear(self):
        for pattern in self._patterns():
            for head in ("gh pr create", "gh auth switch", "gh auth token", "gh repo deploy-key add",
                         "gh pr"):
                for unit in self.UNITS:
                    for tail in self.TAILS:
                        self._assert_linear(pattern, head + unit * 5000 + tail, head + unit * 20000 + tail,
                                            (pattern.pattern[:40], head, unit, tail))

    def test_blanks_inside_the_path_are_linear(self):
        """パスの語の間に空白・タブを並べ、最後で外れる入力 (末端とグループの選択がそれぞれ空白を読む)。"""
        for pattern in self._patterns():
            for sep in (" ", "\t", " \t"):
                for words in (("repo", "deploy-key", "add"), ("pr", "create"), ("pr",),
                              ("auth", "token"), ("auth", "switch")):
                    for tail in ("", " x", " --help x", " zz --help"):
                        def text(n, words=words, sep=sep, tail=tail):
                            return "gh" + "".join(sep * n + w for w in words) + tail
                        self._assert_linear(pattern, text(5000), text(20000),
                                            (pattern.pattern[:40], sep, words, tail))

    def test_redirects_after_help_do_not_backtrack(self):
        """`--help` の後ろのリダイレクトの並びが最後で外れる入力 (`... --help >f >f ... x`)。

        リダイレクトの前の空白を 2 通りに読める形にすると、外れたときに並びの数の指数の時間が
        かかる (22 個で 1 本約 0.9 秒)。test_linear の単位は `--help` の前に置くので、この経路を
        通らない。"""
        token = [p for p, _o in github.DISCLOSING if "auth\\s+token" in p]
        self.assertEqual(len(token), 1, github.DISCLOSING)
        for pattern in (github._RO_SUBCOMMAND_HELP, github.STATE_CHANGING[0], token[0]):
            compiled = re.compile(pattern)
            for unit in (" >f", " >>f", " <f", " >&2"):
                with self.subTest(pattern=pattern[:30], unit=unit):
                    text = "gh auth token --help" + unit * 22 + " x"
                    started = time.perf_counter()
                    compiled.search(text)
                    self.assertLess(time.perf_counter() - started, 0.05)


if __name__ == "__main__":
    unittest.main()
