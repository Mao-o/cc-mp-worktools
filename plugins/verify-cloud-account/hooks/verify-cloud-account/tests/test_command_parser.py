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
    ]

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


if __name__ == "__main__":
    unittest.main()
