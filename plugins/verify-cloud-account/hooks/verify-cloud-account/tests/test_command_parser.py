"""command_parser: split / env strip / wrapper strip / extract_candidates のテスト。"""
from __future__ import annotations

import unittest

import _testutil  # noqa: F401

from core import command_parser  # noqa: E402
from core.command_parser import (  # noqa: E402
    extract_candidates,
    split_on_operators,
    strip_leading_env,
    strip_transparent_wrappers,
)


class TestSplitOnOperators(unittest.TestCase):
    def test_single_command(self):
        self.assertEqual(split_on_operators("gh pr list"), ["gh pr list"])

    def test_and_operator(self):
        self.assertEqual(split_on_operators("a && b"), ["a", "b"])

    def test_or_operator(self):
        self.assertEqual(split_on_operators("a || b"), ["a", "b"])

    def test_semicolon(self):
        self.assertEqual(split_on_operators("a ; b"), ["a", "b"])

    def test_pipe(self):
        self.assertEqual(split_on_operators("a | b"), ["a", "b"])

    def test_newline(self):
        self.assertEqual(split_on_operators("a\nb"), ["a", "b"])

    def test_mixed(self):
        self.assertEqual(
            split_on_operators("a && b ; c | d || e"),
            ["a", "b", "c", "d", "e"],
        )

    def test_double_quotes_protect(self):
        self.assertEqual(
            split_on_operators('echo "a && b"'),
            ['echo "a && b"'],
        )

    def test_single_quotes_protect(self):
        self.assertEqual(
            split_on_operators("echo 'a ; b'"),
            ["echo 'a ; b'"],
        )

    def test_subshell_protect(self):
        self.assertEqual(
            split_on_operators("cmd $(date && echo x)"),
            ["cmd $(date && echo x)"],
        )

    def test_backtick_protect(self):
        self.assertEqual(
            split_on_operators("cmd `date; echo x`"),
            ["cmd `date; echo x`"],
        )

    def test_trim_and_filter_empty(self):
        self.assertEqual(split_on_operators("  ;;  "), [])
        self.assertEqual(split_on_operators(""), [])

    def test_escape_ampersand(self):
        self.assertEqual(
            split_on_operators(r"echo a\&\&b && echo c"),
            [r"echo a\&\&b", "echo c"],
        )

    def test_subshell_with_quoted_paren(self):
        """Codex R1 回帰: `$()` 内の quote で `)` を保護する。

        `$(printf ")")` の内側の `")"` が閉じ括弧と誤認されると、paren_depth
        が早閉じして後続の `&&` が subshell 内と誤解される。
        """
        self.assertEqual(
            split_on_operators('echo $(printf ")") && gh pr list'),
            ['echo $(printf ")")', "gh pr list"],
        )

    def test_subshell_with_single_quoted_semicolon(self):
        self.assertEqual(
            split_on_operators("echo $(printf ';') && gh pr list"),
            ["echo $(printf ';')", "gh pr list"],
        )

    def test_nested_subshell(self):
        self.assertEqual(
            split_on_operators("echo $(echo $(date)) && gh pr list"),
            ["echo $(echo $(date))", "gh pr list"],
        )

    def test_comment_strips_trailing(self):
        """Codex R2 回帰: unquoted `#` 以降はコメント扱いで分割対象外。"""
        self.assertEqual(
            split_on_operators("gh pr list # note && aws s3 ls"),
            ["gh pr list"],
        )

    def test_comment_with_newline(self):
        self.assertEqual(
            split_on_operators("gh pr list # note\naws s3 ls"),
            ["gh pr list", "aws s3 ls"],
        )

    def test_comment_after_operator(self):
        self.assertEqual(
            split_on_operators("gh pr list &&# note\naws s3 ls"),
            ["gh pr list", "aws s3 ls"],
        )

    def test_hash_inside_token_not_comment(self):
        """`foo#bar` のトークン内 `#` はコメント開始ではない。"""
        self.assertEqual(
            split_on_operators("echo foo#bar && gh pr list"),
            ["echo foo#bar", "gh pr list"],
        )

    def test_hash_inside_double_quotes_not_comment(self):
        self.assertEqual(
            split_on_operators('echo "a # b" && gh pr list'),
            ['echo "a # b"', "gh pr list"],
        )

    def test_hash_inside_single_quotes_not_comment(self):
        self.assertEqual(
            split_on_operators("echo 'a # b' && gh pr list"),
            ["echo 'a # b'", "gh pr list"],
        )


class TestStripLeadingEnv(unittest.TestCase):
    def test_single_assignment(self):
        self.assertEqual(strip_leading_env("FOO=bar gh pr list"), "gh pr list")

    def test_multiple_assignments(self):
        self.assertEqual(
            strip_leading_env("FOO=bar BAZ=qux gh pr list"),
            "gh pr list",
        )

    def test_quoted_value(self):
        self.assertEqual(
            strip_leading_env('FOO="a b c" gh pr list'),
            "gh pr list",
        )

    def test_no_assignment(self):
        self.assertEqual(strip_leading_env("gh pr list"), "gh pr list")

    def test_assignment_only_kept(self):
        """`FOO=bar` だけのケースは剥がすと空コマンドになるので保持。"""
        self.assertEqual(strip_leading_env("FOO=bar"), "FOO=bar")

    def test_subshell_value_kept(self):
        """値に $() を含む場合は保守的に剥がさない。"""
        self.assertEqual(
            strip_leading_env("FOO=$(date) gh pr list"),
            "FOO=$(date) gh pr list",
        )

    def test_backtick_value_kept(self):
        self.assertEqual(
            strip_leading_env("FOO=`date` gh pr list"),
            "FOO=`date` gh pr list",
        )

    def test_empty_value(self):
        self.assertEqual(strip_leading_env("FOO= gh pr list"), "gh pr list")


class TestStripTransparentWrappers(unittest.TestCase):
    def test_sudo(self):
        self.assertEqual(strip_transparent_wrappers("sudo gh pr list"), "gh pr list")

    def test_time(self):
        self.assertEqual(strip_transparent_wrappers("time gh pr list"), "gh pr list")

    def test_nohup(self):
        self.assertEqual(strip_transparent_wrappers("nohup gh pr list"), "gh pr list")

    def test_command_builtin(self):
        self.assertEqual(
            strip_transparent_wrappers("command gh pr list"), "gh pr list"
        )
        self.assertEqual(
            strip_transparent_wrappers("builtin gh pr list"), "gh pr list"
        )

    def test_exec(self):
        self.assertEqual(strip_transparent_wrappers("exec gh pr list"), "gh pr list")

    def test_env_simple(self):
        self.assertEqual(
            strip_transparent_wrappers("env FOO=bar gh pr list"),
            "gh pr list",
        )

    def test_env_with_option_not_stripped(self):
        """env -i / env --  など option 付きは挙動が変わるため剥がさない。"""
        self.assertEqual(
            strip_transparent_wrappers("env -i gh pr list"),
            "env -i gh pr list",
        )
        self.assertEqual(
            strip_transparent_wrappers("env -- gh pr list"),
            "env -- gh pr list",
        )

    def test_npx(self):
        self.assertEqual(
            strip_transparent_wrappers("npx firebase deploy"),
            "firebase deploy",
        )

    def test_pnpm_exec(self):
        self.assertEqual(
            strip_transparent_wrappers("pnpm exec firebase deploy"),
            "firebase deploy",
        )

    def test_pnpm_dlx(self):
        self.assertEqual(
            strip_transparent_wrappers("pnpm dlx firebase deploy"),
            "firebase deploy",
        )

    def test_mise_exec(self):
        self.assertEqual(
            strip_transparent_wrappers("mise exec -- firebase deploy"),
            "firebase deploy",
        )

    def test_bun_x(self):
        self.assertEqual(
            strip_transparent_wrappers("bun x firebase deploy"),
            "firebase deploy",
        )

    def test_stacked_wrappers(self):
        self.assertEqual(
            strip_transparent_wrappers("sudo time gh pr list"),
            "gh pr list",
        )

    def test_env_assign_mixed_with_wrapper(self):
        self.assertEqual(
            strip_transparent_wrappers("FOO=bar sudo gh pr list"),
            "gh pr list",
        )

    def test_no_wrapper(self):
        self.assertEqual(strip_transparent_wrappers("gh pr list"), "gh pr list")

    def test_bash_c_not_stripped(self):
        """`bash -c` は script 実行なので透過剥がし対象外 (= 検証対象外)。"""
        self.assertEqual(
            strip_transparent_wrappers("bash -c 'gh pr list'"),
            "bash -c 'gh pr list'",
        )

    def test_sudo_flag_with_value(self):
        """Codex R3 回帰: `sudo -u USER` の値ありフラグをペアで剥がす。"""
        self.assertEqual(
            strip_transparent_wrappers("sudo -u deploy gh pr list"),
            "gh pr list",
        )

    def test_sudo_bool_flag(self):
        """bool flag (値なし) は 1 トークンで消費。`sudo -n gh` の `gh` を食わない。"""
        self.assertEqual(
            strip_transparent_wrappers("sudo -n gh pr list"),
            "gh pr list",
        )

    def test_sudo_long_flag_equals(self):
        """`--user=deploy` は 1 トークンで消費。"""
        self.assertEqual(
            strip_transparent_wrappers("sudo --user=deploy gh pr list"),
            "gh pr list",
        )

    def test_sudo_long_flag_space(self):
        """`--user deploy` は 2 トークン消費。"""
        self.assertEqual(
            strip_transparent_wrappers("sudo --user deploy gh pr list"),
            "gh pr list",
        )

    def test_sudo_multiple_flags_mixed(self):
        """値あり / 値なしが混在するケース。"""
        self.assertEqual(
            strip_transparent_wrappers("sudo -u deploy -E gh pr list"),
            "gh pr list",
        )
        self.assertEqual(
            strip_transparent_wrappers("sudo -g app -u deploy gh pr list"),
            "gh pr list",
        )

    def test_sudo_double_dash_separator(self):
        """`sudo -- gh` の `--` は flag 終端として消費、以降はコマンド。"""
        self.assertEqual(
            strip_transparent_wrappers("sudo -- gh pr list"),
            "gh pr list",
        )

    def test_sudo_unknown_flag_treated_as_bool(self):
        """未知の `-X` は bool と仮定し単独トークンとして skip。"""
        self.assertEqual(
            strip_transparent_wrappers("sudo -X gh pr list"),
            "gh pr list",
        )

    def test_time_flag_with_value(self):
        self.assertEqual(
            strip_transparent_wrappers("time -o out.txt gh pr list"),
            "gh pr list",
        )

    def test_npx_flag_with_value(self):
        self.assertEqual(
            strip_transparent_wrappers("npx -p some-pkg firebase deploy"),
            "firebase deploy",
        )

    def test_exec_flag_with_value(self):
        """`exec -a myname gh ...` の `-a` は値あり flag。"""
        self.assertEqual(
            strip_transparent_wrappers("exec -a myname gh pr list"),
            "gh pr list",
        )

    def test_stacked_wrappers_with_flags(self):
        """多段 wrapper + 各 wrapper の flag の組合せ。"""
        self.assertEqual(
            strip_transparent_wrappers(
                "sudo -u deploy time -o out.txt mise exec -- firebase deploy"
            ),
            "firebase deploy",
        )


class TestExtractCandidates(unittest.TestCase):
    """extract_candidates は (候補断片, inline_env dict) のリストを返す。"""

    def test_chain_with_cd(self):
        self.assertEqual(
            extract_candidates("cd /tmp && gh pr create"),
            [("cd /tmp", {}), ("gh pr create", {})],
        )

    def test_env_prefix_collected(self):
        # 先頭 KEY=VALUE は候補から剥がしつつ inline env として収集する
        self.assertEqual(
            extract_candidates("FOO=bar gh pr create"),
            [("gh pr create", {"FOO": "bar"})],
        )

    def test_sudo_stripped(self):
        self.assertEqual(
            extract_candidates("sudo gh pr create"),
            [("gh pr create", {})],
        )

    def test_readonly_and_mutating_both_surfaced(self):
        self.assertEqual(
            extract_candidates("gh auth status && gh pr list"),
            [("gh auth status", {}), ("gh pr list", {})],
        )

    def test_nested_wrappers(self):
        self.assertEqual(
            extract_candidates("sudo time mise exec -- firebase deploy"),
            [("firebase deploy", {})],
        )

    def test_quoted_command_not_decomposed(self):
        self.assertEqual(
            extract_candidates('echo "gh auth status"'),
            [('echo "gh auth status"', {})],
        )

    def test_empty_command(self):
        self.assertEqual(extract_candidates(""), [])
        self.assertEqual(extract_candidates("   "), [])

    # --- inline env 収集 (検証 subprocess への伝播用) ---

    def test_aws_profile_collected(self):
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod aws s3 ls"),
            [("aws s3 ls", {"AWS_PROFILE": "prod"})],
        )

    def test_quoted_env_value_unquoted(self):
        self.assertEqual(
            extract_candidates('AWS_PROFILE="my prof" aws s3 ls'),
            [("aws s3 ls", {"AWS_PROFILE": "my prof"})],
        )

    def test_multiple_env_collected(self):
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod AWS_REGION=us-east-1 aws s3 ls"),
            [("aws s3 ls", {"AWS_PROFILE": "prod", "AWS_REGION": "us-east-1"})],
        )

    def test_env_with_variable_ref_not_collected(self):
        # 未展開の $VAR は静的に解決できないため env に入れない (剥がしはする)
        self.assertEqual(
            extract_candidates("AWS_PROFILE=$HOME aws s3 ls"),
            [("aws s3 ls", {})],
        )

    def test_env_before_nonscrub_wrapper_collected(self):
        # D11: env scrub しない wrapper (time/nohup/...) の前に置かれた pre-wrapper
        # env は実行時にも有効なので収集する。sudo は別扱い (TestSudoEnvScrub 参照)。
        self.assertEqual(
            extract_candidates("FOO=bar time gh pr list"),
            [("gh pr list", {"FOO": "bar"})],
        )

    def test_env_collected_per_segment(self):
        # チェーンの各セグメントで env は独立して収集される
        self.assertEqual(
            extract_candidates("cd /tmp && AWS_PROFILE=prod aws s3 ls"),
            [("cd /tmp", {}), ("aws s3 ls", {"AWS_PROFILE": "prod"})],
        )

    def test_duplicate_env_key_last_wins(self):
        # 同一キーの重複代入は shell semantics に合わせ最右 (最後) が勝つ
        self.assertEqual(
            extract_candidates("AWS_PROFILE=dev AWS_PROFILE=prod aws s3 ls"),
            [("aws s3 ls", {"AWS_PROFILE": "prod"})],
        )

    def test_inner_env_after_wrapper_overrides_outer(self):
        # `env NAME=VALUE` は内側の値を実行環境へ適用する → 検証も内側 (other) で
        # 行う必要があるため、wrapper を跨いでも内側が外側を上書きする
        self.assertEqual(
            extract_candidates("AWS_PROFILE=expected env AWS_PROFILE=other aws s3 ls"),
            [("aws s3 ls", {"AWS_PROFILE": "other"})],
        )


class TestSudoEnvScrub(unittest.TestCase):
    """D16 回帰: sudo の env scrub を考慮し pre-sudo env を伝播しない。

    `AWS_PROFILE=prod sudo aws ...` のように sudo の**前**にインライン env を
    置くと、sudo は `-E`/`--preserve-env` 無しに継承環境を scrub するため、
    実行時の `sudo aws ...` に AWS_PROFILE は届かない。これを検証 subprocess に
    渡すと「検証 prod / 実行 別アカウント」の false-allow バイパスになる。
    preserve-env 指定の無い sudo を跨いだ pre-sudo env は drop する。
    """

    def test_pre_sudo_env_scrubbed_without_preserve(self):
        # 中心ケース: pre-sudo env は scrub される → inline_env に含まれない
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod sudo aws s3 rm s3://x"),
            [("aws s3 rm s3://x", {})],
        )

    def test_pre_sudo_env_preserved_with_short_E(self):
        # `sudo -E` は継承環境を保持する → pre-sudo env を伝播してよい
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod sudo -E aws s3 rm s3://x"),
            [("aws s3 rm s3://x", {"AWS_PROFILE": "prod"})],
        )

    def test_pre_sudo_env_preserved_with_long_preserve_env(self):
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod sudo --preserve-env aws s3 rm s3://x"),
            [("aws s3 rm s3://x", {"AWS_PROFILE": "prod"})],
        )

    def test_pre_sudo_env_preserved_with_preserve_env_list(self):
        # `--preserve-env=LIST` 形式。リスト内容や sudoers までは静的に追えないが、
        # preserve 指定があれば保守的に伝播を許す (保持しすぎは安全側 = 誤 deny)。
        self.assertEqual(
            extract_candidates(
                "AWS_PROFILE=prod sudo --preserve-env=AWS_PROFILE aws s3 rm s3://x"
            ),
            [("aws s3 rm s3://x", {"AWS_PROFILE": "prod"})],
        )

    def test_pre_sudo_env_preserved_with_preserve_env_among_other_flags(self):
        # 値ありフラグ (`-u deploy`) と preserve-env が混在しても検出する
        self.assertEqual(
            extract_candidates(
                "AWS_PROFILE=prod sudo -u deploy -E aws s3 rm s3://x"
            ),
            [("aws s3 rm s3://x", {"AWS_PROFILE": "prod"})],
        )

    def test_pre_sudo_env_scrubbed_with_value_flag_only(self):
        # preserve-env 無し (値ありフラグだけ) なら scrub される
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod sudo -u deploy aws s3 rm s3://x"),
            [("aws s3 rm s3://x", {})],
        )

    def test_pre_sudo_env_preserved_with_time_wrapper(self):
        # 非 sudo wrapper (time) は env を scrub しない → D11 を回帰させない
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod time aws s3 rm s3://x"),
            [("aws s3 rm s3://x", {"AWS_PROFILE": "prod"})],
        )

    def test_pre_sudo_env_preserved_with_nohup_wrapper(self):
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod nohup aws s3 rm s3://x"),
            [("aws s3 rm s3://x", {"AWS_PROFILE": "prod"})],
        )

    def test_pre_sudo_env_scrubbed_in_multistage_via_sudo(self):
        # 多段 (`... sudo time aws ...`): sudo を経由するので pre-sudo env は drop
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod sudo time aws s3 rm s3://x"),
            [("aws s3 rm s3://x", {})],
        )

    def test_env_between_sudo_and_command_survives(self):
        # `sudo FOO=bar cmd` の post-sudo command-line env は sudo 自身が target へ
        # 渡すため伝播を維持する (pre-sudo env とは別物)
        self.assertEqual(
            extract_candidates("sudo AWS_PROFILE=prod aws s3 rm s3://x"),
            [("aws s3 rm s3://x", {"AWS_PROFILE": "prod"})],
        )

    def test_inner_env_after_sudo_preserve_overrides_outer(self):
        # inner-wins が sudo 跨ぎでも壊れないこと (preserve-env 指定時)。
        # outer (expected) は -E で保持され、post-sudo の other が上書きする。
        self.assertEqual(
            extract_candidates(
                "AWS_PROFILE=expected sudo -E AWS_PROFILE=other aws s3 ls"
            ),
            [("aws s3 ls", {"AWS_PROFILE": "other"})],
        )

    def test_double_dash_before_preserve_env_is_command(self):
        # `sudo -- -E aws` の `--` 以降はコマンド本体。`-E` は flag ではないので
        # preserve とは見なさない → pre-sudo env は scrub される。
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod sudo -- aws s3 rm s3://x"),
            [("aws s3 rm s3://x", {})],
        )


class TestWrapperEnvClassificationGuard(unittest.TestCase):
    """D16 guard: 全透過 wrapper が env 伝播クラスに分類されていることを強制する。

    `_WRAPPERS_SINGLE` / `_WRAPPERS_TWO` / `_WRAPPERS_THREE` に wrapper を追加
    したのに `_WRAPPER_ENV_CLASS` に分類を足し忘れると、その wrapper の env 挙動
    (継承 env を素通すか / scrub・reset するか) が未検証のまま伝播経路に入り、
    過去 (D11 round1-3 / 8zr) と同じ「検証 env ≠ 実行 env」の whack-a-mole を
    再発させる。このテストが両者の同期を機械的に保証する。

    新しい wrapper を追加するときは:
      1. `_WRAPPERS_*` に追加
      2. `_WRAPPER_ENV_CLASS` に "passthrough" / "conditional_scrub" を追加
      3. conditional_scrub なら scrub 補正ロジック + 回帰テストを追加
      4. CLAUDE.local.md の D16 表とチェックリストを更新
    """

    def _all_wrapper_keys(self):
        keys = set(command_parser._WRAPPERS_SINGLE)
        keys |= set(command_parser._WRAPPERS_TWO)
        keys |= set(command_parser._WRAPPERS_THREE)
        return keys

    def test_every_wrapper_is_classified(self):
        # `env` は意図的に _WRAPPERS_* に入れず _strip_one_wrapper で特別扱い
        # するため、分類対象は _WRAPPERS_* のみ。
        unclassified = self._all_wrapper_keys() - set(
            command_parser._WRAPPER_ENV_CLASS
        )
        self.assertEqual(
            unclassified,
            set(),
            "未分類の透過 wrapper があります。_WRAPPER_ENV_CLASS に "
            "'passthrough' / 'conditional_scrub' を追加し、CLAUDE.local.md の "
            f"D16 表を更新してください: {sorted(map(str, unclassified))}",
        )

    def test_no_stale_classification_entries(self):
        # 逆方向: _WRAPPER_ENV_CLASS に _WRAPPERS_* から消えた wrapper が
        # 残っていないか (分類だけ残り実装が消えた死にエントリの検出)。
        stale = set(command_parser._WRAPPER_ENV_CLASS) - self._all_wrapper_keys()
        self.assertEqual(
            stale,
            set(),
            f"_WRAPPER_ENV_CLASS に実装の無い分類が残っています: "
            f"{sorted(map(str, stale))}",
        )

    def test_classification_values_are_known(self):
        known = {"passthrough", "conditional_scrub"}
        unknown = set(command_parser._WRAPPER_ENV_CLASS.values()) - known
        self.assertEqual(
            unknown, set(), f"未知の分類値: {sorted(unknown)}"
        )

    def test_required_and_optional_value_flags_are_disjoint(self):
        """同じ flag を「必須引数」と「optional 引数」の両方に載せない。

        optional 引数の flag (GNU の `--replace[=R]` 等) を必須側に登録すると、
        bare 形が次の token = コマンド名を食い、`xargs --replace gh pr close {}` の
        `gh` が検証をすり抜ける (PR #48 Codex P1-A)。この 2 集合が交わっていないこと
        自体が退行防止の番人になる。
        """
        for wrapper, optional in command_parser._WRAPPER_FLAGS_OPTIONAL_VALUE.items():
            required = command_parser._WRAPPER_FLAGS_WITH_VALUE.get(wrapper, set())
            overlap = optional & required
            self.assertEqual(
                overlap,
                set(),
                f"{wrapper}: optional 引数の flag を必須側にも登録している "
                f"(bare 形がコマンド名を食う): {sorted(overlap)}",
            )

    def test_optional_value_flags_do_not_consume_bare(self):
        for cmd, want in (
            ("xargs --replace gh pr close {}", "gh pr close {}"),
            ("xargs --eof gh pr create", "gh pr create"),
            ("xargs --max-lines gh pr create", "gh pr create"),
            # `=` 形は値を取る (1 トークンで消費される)
            ("xargs --replace={} gh pr close {}", "gh pr close {}"),
            ("xargs --max-lines=5 gh pr create", "gh pr create"),
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(strip_transparent_wrappers(cmd), want)

    def test_numeric_argument_safety_net(self):
        """値付き flag の登録漏れがあっても、値が数値ならコマンドに到達する。

        `watch` のように一次情報 (man page) を開発機で取れない wrapper は flag 表を
        空にしてこのネットに委ねている。コマンド名が純粋な数値になることは無いので、
        読み飛ばしがコマンドを食う心配がない。
        """
        for cmd, want in (
            ("watch --equexit 5 gh pr create", "gh pr create"),
            ("watch -q 5 gh pr create", "gh pr create"),
            ("watch -n 5 kubectl get pods", "kubectl get pods"),
            ("xargs -Z 5 gh pr create", "gh pr create"),
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(strip_transparent_wrappers(cmd), want)

    def test_timeout_duration_is_not_eaten_by_the_safety_net(self):
        # timeout は位置引数 DURATION を自前で消費するのでネットを無効にしている。
        # 有効だと DURATION が読み飛ばされ、コマンド名が DURATION と誤判定されて
        # wrapper 剥がし自体が諦められる。
        for cmd in ("timeout 30 gh pr create", "timeout -k 10 30 gh pr create"):
            with self.subTest(cmd=cmd):
                self.assertEqual(strip_transparent_wrappers(cmd), "gh pr create")

    def test_only_sudo_is_conditional_scrub(self):
        # 現状 scrub するのは sudo のみ。新しい conditional_scrub wrapper を
        # 足すときは scrub 補正ロジックと回帰テストの追加を忘れないよう、
        # ここを更新する時点で気づけるようにする (= 番兵)。
        scrub = {
            w
            for w, cls in command_parser._WRAPPER_ENV_CLASS.items()
            if cls == "conditional_scrub"
        }
        self.assertEqual(scrub, {"sudo"})


class TestWrapperEnvPropagationContract(unittest.TestCase):
    """D16 contract: wrapper ごとの env 伝播/非伝播を 1 つの表で固定化する。

    各 wrapper について「pre-wrapper のインライン env が inline_env に乗るか」を
    実 `extract_candidates` 出力で表明する。passthrough wrapper は乗る、
    sudo (preserve 無し) は乗らない、env のリセット系 (-i / -u / --) は wrapper
    として剥がされず opaque のまま (= 検証スキップ) であることを 1 箇所で保証し、
    将来のリファクタや wrapper 追加が分類を崩したら即座に落ちるようにする。
    """

    # (コマンド, 期待 normalized, 期待 inline_env)。
    # AWS_PROFILE を pre-wrapper env として置き、伝播されるかを見る。
    PASSTHROUGH_CASES = [
        ("AWS_PROFILE=prod time aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        ("AWS_PROFILE=prod nohup aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        ("AWS_PROFILE=prod command aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        # `builtin` は外部 CLI を起動しないので実運用では service に match しないが、
        # parser の扱い (剥がして env を収集) は他の passthrough と同じ。
        ("AWS_PROFILE=prod builtin aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        ("AWS_PROFILE=prod exec aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        ("AWS_PROFILE=prod npx aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        (
            "AWS_PROFILE=prod pnpm exec aws s3 ls",
            "aws s3 ls",
            {"AWS_PROFILE": "prod"},
        ),
        (
            "AWS_PROFILE=prod pnpm dlx aws s3 ls",
            "aws s3 ls",
            {"AWS_PROFILE": "prod"},
        ),
        (
            "AWS_PROFILE=prod mise exec -- aws s3 ls",
            "aws s3 ls",
            {"AWS_PROFILE": "prod"},
        ),
        ("AWS_PROFILE=prod bun x aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        # v0.9.0 追加の実行ラッパ。いずれも継承 env をそのまま子へ渡す。
        ("AWS_PROFILE=prod timeout 30 aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        ("AWS_PROFILE=prod nice -n 5 aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        ("AWS_PROFILE=prod stdbuf -oL aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        ("AWS_PROFILE=prod setsid aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        (
            "AWS_PROFILE=prod caffeinate -i aws s3 ls",
            "aws s3 ls",
            {"AWS_PROFILE": "prod"},
        ),
        ("AWS_PROFILE=prod watch -n 5 aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
        ("AWS_PROFILE=prod xargs -n 1 aws s3 ls", "aws s3 ls", {"AWS_PROFILE": "prod"}),
    ]

    def test_every_passthrough_wrapper_has_a_propagation_case(self):
        """passthrough 分類の全 wrapper が上の表に載っていることを強制する。

        分類 (`_WRAPPER_ENV_CLASS`) を足しただけで伝播ケースを書かないと、
        「剥がすが env を運ばない」「flag の消費規則が違う」といった実挙動の
        ずれが誰にも観測されないまま入る。D16 チェックリスト手順 6 の機械化。
        """
        covered = set()
        for command, _normalized, _env in self.PASSTHROUGH_CASES:
            tokens = command.split()[1:]  # 先頭の inline env を除く
            if not tokens:
                continue
            for size in (3, 2, 1):
                key = tuple(tokens[:size]) if size > 1 else tokens[0]
                if key in command_parser._WRAPPER_ENV_CLASS:
                    covered.add(key)
                    break
        passthrough = {
            w
            for w, cls in command_parser._WRAPPER_ENV_CLASS.items()
            if cls == "passthrough"
        }
        missing = passthrough - covered
        self.assertEqual(
            missing,
            set(),
            "PASSTHROUGH_CASES に env 伝播ケースの無い passthrough wrapper が "
            f"あります: {sorted(map(str, missing))}",
        )

    def test_passthrough_wrappers_propagate_pre_wrapper_env(self):
        for command, normalized, env in self.PASSTHROUGH_CASES:
            with self.subTest(command=command):
                self.assertEqual(
                    extract_candidates(command),
                    [(normalized, env)],
                )

    def test_sudo_without_preserve_does_not_propagate(self):
        # conditional_scrub: preserve 指定が無ければ pre-sudo env は乗らない。
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod sudo aws s3 ls"),
            [("aws s3 ls", {})],
        )

    def test_sudo_with_preserve_propagates(self):
        self.assertEqual(
            extract_candidates("AWS_PROFILE=prod sudo -E aws s3 ls"),
            [("aws s3 ls", {"AWS_PROFILE": "prod"})],
        )

    def test_env_reset_forms_stay_opaque(self):
        # `env -i` / `env -u` / `env --` は wrapper として剥がさない (opaque)。
        # 剥がさない = セグメントがそのまま残り service に match せず検証スキップ。
        # これにより「実行は縮小環境 / 検証は親環境」の非対称を作らない (安全側)。
        for command in (
            "env -i AWS_PROFILE=prod aws s3 ls",
            "env -u AWS_PROFILE aws s3 ls",
            "env -- aws s3 ls",
        ):
            with self.subTest(command=command):
                cands = extract_candidates(command)
                # normalized セグメントは元コマンドのまま (剥がされていない)、
                # かつ env は収集されない。
                self.assertEqual(len(cands), 1)
                normalized, inline_env = cands[0]
                self.assertTrue(normalized.startswith("env -"))
                self.assertEqual(inline_env, {})

    def test_env_plain_form_propagates(self):
        # オプション無し `env` は剥がして、その command-line env を収集する。
        self.assertEqual(
            extract_candidates("env AWS_PROFILE=prod aws s3 ls"),
            [("aws s3 ls", {"AWS_PROFILE": "prod"})],
        )


class TestHeredocProtection(unittest.TestCase):
    """heredoc 本文を候補セグメントにしない (誤 deny 防止)。

    `cat > deploy.sh <<'EOF' ... EOF` の本文に書かれた CLI 例は**実行されない**
    ただのテキストなので、候補として検証してはいけない。改行を無条件の区切りに
    していた頃は本文 1 行 1 行が候補になり、スクリプトや PR 本文を heredoc で
    書くだけでファイル生成そのものが deny されていた。
    """

    def test_body_lines_are_not_candidates(self):
        cmd = "cat > deploy.sh <<'EOF'\n#!/bin/bash\ngh release create v1\nEOF"
        cands = [c for c, _env in extract_candidates(cmd)]
        self.assertEqual(len(cands), 1)
        self.assertTrue(cands[0].startswith("cat > deploy.sh"))

    def test_command_after_heredoc_is_still_a_separate_candidate(self):
        # delimiter 行の改行はトップレベルの区切り。閉じないと heredoc の次の
        # コマンドが本文に吸収され、検証されないまま素通りする (バイパス)。
        cmd = "cat > d.sh <<'EOF'\ngh release create v1\nEOF\ngh pr create --fill"
        cands = [c for c, _env in extract_candidates(cmd)]
        self.assertEqual(len(cands), 2)
        self.assertEqual(cands[1], "gh pr create --fill")

    def test_unquoted_and_dash_forms(self):
        # `<<-` は本文と delimiter 行の先頭タブを除去して比較する (man 1 bash)。
        cmd = "cat <<-EOF > x\n\tgh pr create\n\tEOF\naws s3 ls"
        cands = [c for c, _env in extract_candidates(cmd)]
        self.assertEqual(len(cands), 2)
        self.assertEqual(cands[1], "aws s3 ls")

    def test_double_quoted_and_backslash_delimiters(self):
        for opener, closer in (('<<"EOF"', "EOF"), ("<<\\EOF", "EOF")):
            with self.subTest(opener=opener):
                cmd = f"cat {opener}\ngh pr create\n{closer}\naws s3 ls"
                cands = [c for c, _env in extract_candidates(cmd)]
                self.assertEqual(len(cands), 2)
                self.assertEqual(cands[1], "aws s3 ls")

    def test_multiple_heredocs_on_one_line(self):
        cmd = "cat <<A <<B\na1\nA\nb1\nB\naws s3 ls"
        cands = [c for c, _env in extract_candidates(cmd)]
        self.assertEqual(len(cands), 2)
        self.assertEqual(cands[1], "aws s3 ls")

    def test_here_string_is_not_a_heredoc(self):
        # `<<<` は本文行を持たないので改行での分割を止めてはいけない。
        cands = [c for c, _env in extract_candidates('grep foo <<< "$bar"\ngh pr list')]
        self.assertEqual(cands, ['grep foo <<< "$bar"', "gh pr list"])

    def test_arithmetic_left_shift_is_not_treated_as_heredoc(self):
        # `(( x = 1 << 2 ))` の `2` を delimiter と誤認すると、その数字の行まで
        # 飲み込んで後続の検証対象セグメントを消してしまう (バイパス)。
        # bare 形の delimiter を識別子に限定することで防ぐ。
        cands = [c for c, _env in extract_candidates("(( x = 1 << 2 ))\ngh pr create")]
        self.assertIn("gh pr create", cands)

    def test_unterminated_heredoc_does_not_swallow_the_rest(self):
        """terminator が無ければ heredoc として扱わず、通常の改行分割に戻す。

        「閉じていなければ末尾まで飲み込む」方式は、delimiter を 1 文字でも
        読み違えた瞬間に「以降すべて検証しない」へ化ける。実際に
        `cat <<END-OF-FILE` を `END` と読む / `(( x = 1 << y ))` を heredoc と読む
        の 2 経路で、後続の `gh pr create` が丸ごと未検証になっていた。
        未終端 heredoc は bash 側でもエラー (or 入力待ち) になる壊れたコマンドなので、
        そちらを検証してしまう方の代償を取る。
        """
        cands = [c for c, _env in extract_candidates("cat <<EOF\ngh pr create\n")]
        self.assertIn("gh pr create", cands)

    def test_delimiter_is_matched_as_a_whole_token(self):
        # 途中で切ると (例: `END-OF-FILE` を `END` と読む) terminator が現れず、
        # 後続コマンドまで本文として飲み込む = 検証が消える。
        for opener, closer in (
            ("<<END-OF-FILE", "END-OF-FILE"),
            ("<<EOF.txt", "EOF.txt"),
            ("<<1EOF", "1EOF"),
        ):
            with self.subTest(opener=opener):
                cmd = f"cat {opener} > x\nbody\n{closer}\ngh pr create --fill"
                cands = [c for c, _env in extract_candidates(cmd)]
                self.assertEqual(len(cands), 2, cands)
                self.assertEqual(cands[1], "gh pr create --fill")
                self.assertNotIn("body", cands[0])

    def test_arithmetic_shift_with_identifier_operand(self):
        # `1 << 2` (数字) だけでなく `1 << y` (識別子) も heredoc ではない。
        # 識別子だけを弾く方式では防げないため、terminator の実在確認で担保する。
        for cmd in ("(( x = 1 << y ))\ngh pr create", "(( x = 1 << 2 ))\ngh pr create"):
            with self.subTest(cmd=cmd):
                self.assertIn(
                    "gh pr create", [c for c, _e in extract_candidates(cmd)]
                )

    def test_body_is_dropped_from_the_normalized_candidate(self):
        """本文はデータなので候補文字列に残さない (option 走査に食わせない)。"""
        cmd = "cat > deploy.sh <<'EOF'\n--profile prod\nEOF"
        cands = [c for c, _env in extract_candidates(cmd)]
        self.assertEqual(cands, ["cat > deploy.sh <<'EOF'"])


class TestCommandBuiltinQuery(unittest.TestCase):
    """`command -v gh` は存在確認なので wrapper として剥がさない (誤 deny 防止)。

    `man 1 bash`: `command [-pVv] command [arg ...]` /「If either the -V or -v
    option is supplied, a description of command is printed」。つまり `-v` 付きは
    後続 CLI を実行しない。剥がすと `gh` 単体の候補になり、インストール確認の
    定型句だけでアカウント検証が走って deny されていた。
    """

    def test_dash_v_stays_opaque(self):
        for cmd in ("command -v gh", "command -V aws", "command -pv aws", "command -vp gh"):
            with self.subTest(cmd=cmd):
                normalized = strip_transparent_wrappers(cmd)
                self.assertTrue(normalized.startswith("command "), normalized)

    def test_dash_v_with_redirect_chain(self):
        cands = [c for c, _e in extract_candidates("command -v gh >/dev/null 2>&1 && echo ok")]
        self.assertTrue(cands[0].startswith("command -v gh"))

    def test_executing_forms_are_still_stripped(self):
        for cmd in ("command gh pr create", "command -p gh pr create", "command -- gh pr create"):
            with self.subTest(cmd=cmd):
                self.assertEqual(strip_transparent_wrappers(cmd), "gh pr create")


class TestExecutionWrappers(unittest.TestCase):
    """v0.9.0 で追加した実行ラッパ。剥がさないと mutating コマンドが素通りする。"""

    def test_timeout_consumes_duration_positional(self):
        for cmd, want in (
            ("timeout 30 gh pr create --fill", "gh pr create --fill"),
            ("timeout 5m aws s3 sync . s3://b", "aws s3 sync . s3://b"),
            ("timeout 1.5h gh pr create", "gh pr create"),
            ("timeout -s KILL 30 gh pr create", "gh pr create"),
            ("timeout --signal=KILL 30 gh pr create", "gh pr create"),
            ("timeout -k 10 30 gh pr create", "gh pr create"),
            ("timeout --preserve-status 30 gh pr create", "gh pr create"),
            ("timeout -k10 30 gh pr create", "gh pr create"),
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(strip_transparent_wrappers(cmd), want)

    def test_timeout_without_duration_stays_opaque(self):
        # DURATION の形をしていない = 文書化された呼び出し形ではない。本体トークンを
        # 誤って食うより、不透明なまま検証をスキップする方が安全側 (lenient)。
        self.assertEqual(
            strip_transparent_wrappers("timeout notanumber gh pr create"),
            "timeout notanumber gh pr create",
        )

    def test_other_wrappers(self):
        for cmd, want in (
            ("xargs -I{} gh pr close {}", "gh pr close {}"),
            ("xargs -I {} gh pr close {}", "gh pr close {}"),
            ("xargs -n 1 -P 4 aws s3 rm", "aws s3 rm"),
            ("watch -n 5 kubectl get pods", "kubectl get pods"),
            ("nice -n 10 aws s3 sync . s3://b", "aws s3 sync . s3://b"),
            ("nice -5 aws s3 ls", "aws s3 ls"),
            ("stdbuf -oL aws s3 ls", "aws s3 ls"),
            ("stdbuf -o L aws s3 ls", "aws s3 ls"),
            ("setsid gh pr create", "gh pr create"),
            ("caffeinate -i aws s3 sync . s3://b", "aws s3 sync . s3://b"),
            ("caffeinate -t 60 aws s3 ls", "aws s3 ls"),
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(strip_transparent_wrappers(cmd), want)

    def test_pipeline_second_stage_is_extracted(self):
        cands = [c for c, _e in extract_candidates("gh pr list | xargs -I{} gh pr close {}")]
        self.assertEqual(cands, ["gh pr list", "gh pr close {}"])


class TestShellSyntaxNormalization(unittest.TestCase):
    """構文プレフィックス / 末尾記号 / パスの正規化 (検証バイパスの解消)。

    いずれも行頭 anchored な PATTERNS (`^gh(?=\\s|$)`) に一致せず service=None に
    なっていた形。Claude が日常的に生成する形なので、検証を走らせる側に倒す。
    """

    def test_grouping_and_negation_prefixes(self):
        for cmd, want in (
            ("(gh pr create --fill)", "gh pr create --fill"),
            ("{ gh pr create", "gh pr create"),
            ("! gh pr create", "gh pr create"),
            ("(cd dir", "cd dir"),
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(strip_transparent_wrappers(cmd), want)

    def test_reserved_word_prefixes(self):
        for word in ("if", "then", "elif", "else", "do", "while", "until"):
            with self.subTest(word=word):
                self.assertEqual(
                    strip_transparent_wrappers(f"{word} gh pr create"), "gh pr create"
                )

    def test_leading_redirections(self):
        for cmd in (
            "</dev/null gh pr create",
            "2>/dev/null gh pr create",
            ">out.txt gh pr create",
            "> out.txt gh pr create",
            "2>&1 gh pr create",
            "&> out.txt gh pr create",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(strip_transparent_wrappers(cmd), "gh pr create")

    def test_command_path_and_backslash_are_normalized(self):
        for cmd, want in (
            ("/opt/homebrew/bin/gh pr create", "gh pr create"),
            ("./node_modules/.bin/firebase deploy", "firebase deploy"),
            ("\\gh pr create", "gh pr create"),
            ("AWS_PROFILE=prod /usr/bin/aws s3 ls", "aws s3 ls"),
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(strip_transparent_wrappers(cmd), want)

    def test_balanced_parens_in_arguments_are_preserved(self):
        # 末尾 `)` を無条件に剥がすと引数を壊す。開き括弧と対応していれば残す。
        self.assertEqual(
            strip_transparent_wrappers("gh pr view $(echo 1)"), "gh pr view $(echo 1)"
        )

    def test_for_loop_body_becomes_a_candidate(self):
        cands = [
            c for c, _e in extract_candidates(
                'for f in *.tgz; do gh release upload v1 "$f"; done'
            )
        ]
        self.assertIn('gh release upload v1 "$f"', cands)

    def test_brace_group_with_trailing_close(self):
        cands = [c for c, _e in extract_candidates("{ gh pr create; }")]
        self.assertEqual(cands, ["gh pr create"])


if __name__ == "__main__":
    unittest.main()
