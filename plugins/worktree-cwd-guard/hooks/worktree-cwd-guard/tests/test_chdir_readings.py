"""移動の組み込み (`cd` / `chdir` / `pushd` / `popd`) を、今の shell で動くかと shell ごとの違いまで含めて読む。

期待値は、偽の git を置いて bash 3.2 / 5.2 と `zsh -f` で実測した結果に合わせている (git がどの checkout を対象に走ったかを見た)。
どれかの shell で main に書く形は止め、どの shell でも main に書かない形は止めない (plain の cd を成功と読む前提は除く)。
"""
from __future__ import annotations

import unittest

import test_guard
from test_guard import bash, decision, evaluate

GIT = "git reset --hard"


class ChdirReadingBase(test_guard.GuardBase):
    def _evaluate(self, cmd):
        try:
            return evaluate(bash(cmd, self.a))
        except Exception as e:  # noqa: BLE001 - 判定が例外で落ちるのも読みの失敗として数える
            self.fail(f"{cmd}: {e!r}")

    def assertDenied(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(self._evaluate(cmd)), "deny")

    def assertAllowed(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self._evaluate(cmd))


class WrappedCdTest(ChdirReadingBase):
    # 子プロセスで走るか、何も起動しない wrapper。移動はどちらの shell でも残らない。
    # `exec` は shell を置き換えるので実測では後ろの git が走らないが、それは読まない (`cd <main>; exec cd <worktree>; git ...` は止まる側)
    NO_MOVE = (
        "env", "env --", "env FOO=1", "env -i", "env -", "env -u X", "nohup", "nohup --", "sudo", "sudo -u me", "sudo -n",
        "sudo --", "exec", "exec -a x", "exec -c", "exec --", "time env", "time nohup", "time exec",
        "FOO=1 time", "FOO=1 time -p", "FOO=1 env", "FOO=1 nohup", "command -v", "command -V", "command -x", "command FOO=1",
        "command time", "command env", "command exec", "command nohup", "env command", "nohup command", "sudo command",
        "env time", "nohup time", "exec command", "exec time", "time -f x", "time -x", "time -f x time", "FOO=1 time --", "\\env",
    )
    # bash だけ今の shell で動く。`time --` と 2 つ目の `time` は bash 5.2 だけ (bash 3.2 は外部の time)
    BASH_ONLY = (
        "command", "command --", "command -p", "\\command", "'command'", "FOO=1 command", "command command", "time command",
        "time -p", "time --", "time -p --", "time time", "time -p time", "time -- time",
    )

    def test_wrapper_that_leaves_the_shell_does_not_move(self):
        m, a = self.main, self.a
        for verb in ("cd", "pushd", "chdir"):
            self.assertDenied(tuple(f'cd "{m}"; {p} {verb} "{a}"; {GIT}' for p in self.NO_MOVE))
            self.assertAllowed(tuple(f'{p} {verb} "{m}"; {GIT}' for p in self.NO_MOVE))

    def test_wrapper_with_the_forms_agents_write(self):
        m = self.main
        self.assertAllowed((
            f'{{ env cd "{m}"; {GIT}; }}',
            f'(env cd "{m}"; {GIT})',
            f'cat <<EOF | env cd "{m}" && git add .\nbody\nEOF',
            f'printf a | env cd "{m}"; {GIT}',
            f'env cd "{m}" & wait; {GIT}',
        ))

    def test_wrapper_that_runs_in_bash_only_reads_both(self):
        m, a = self.main, self.a
        self.assertDenied(tuple(f'cd "{m}"; {p} cd "{a}"; {GIT}' for p in self.BASH_ONLY))
        self.assertDenied(tuple(f'{p} cd "{m}"; {GIT}' for p in self.BASH_ONLY))
        self.assertDenied((
            f'time -- pushd "{m}"; {GIT}',
            f'time time pushd "{m}"; {GIT}',
            f'time -- export GIT_DIR="{m}/.git"; {GIT}',
            f'time time export GIT_DIR="{m}/.git"; {GIT}',
            f'if command cd "{m}"; then {GIT}; fi',
            f'(command cd "{m}"; {GIT})',
            f'git add . && command cd "{m}" && git commit -m x',
            f'cat <<EOF && command cd "{m}" && git add .\nbody\nEOF',
        ))

    def test_readings_of_the_two_shells_do_not_mix(self):
        # 2 つの移動のどちらもどちらの shell でも同じ結果になる。片方の shell の移動と、もう片方の失敗を組み合わせて読むと止まる
        m, a = self.main, self.a
        self.assertAllowed((
            f'command cd "{m}"; command cd "{a}"; {GIT}',
            f'chdir "{m}"; chdir "{a}"; {GIT}',
            f'cd -q "{m}"; cd -q "{a}"; {GIT}',
            f'pushd -q "{m}"; popd -q; {GIT}',
            f'pushd -q "{a}" && {GIT}; popd -q; {GIT}',
            f'time -p cd "{m}"; time -p cd "{a}"; {GIT}',
        ))

    def test_time_keyword_runs_in_the_shell(self):
        m, a = self.main, self.a
        self.assertDenied((f'time cd "{m}"; {GIT}', f'time FOO=1 cd "{m}"; {GIT}', f'time ! cd "{m}"; {GIT}', f'{{ time cd "{m}"; {GIT}; }}'))
        self.assertAllowed((f'cd "{m}"; time cd "{a}"; {GIT}', f'cd "{m}"; time FOO=1 cd "{a}"; {GIT}'))

    def test_exported_variables_do_not_change_in_a_child(self):
        m, a = self.main, self.a
        self.assertAllowed((
            f'env export GIT_DIR="{m}/.git"; {GIT}',
            f'nohup export GIT_DIR="{m}/.git"; {GIT}',
            f'sudo export GIT_DIR="{m}/.git"; {GIT}',
            f'env GIT_DIR="{m}/.git"; {GIT}',
            # zsh の `command export` は外部コマンドになり、bash は今の shell の変数を変える
            f'command unset GIT_DIR; {GIT}',
        ))
        self.assertDenied((
            f'export GIT_DIR="{m}/.git"; env export GIT_DIR="{a}/.git"; {GIT}',
            f'export GIT_DIR="{m}/.git"; env unset GIT_DIR; {GIT}',
            f'export GIT_DIR="{m}/.git"; nohup unset GIT_DIR; {GIT}',
            f'export GIT_DIR="{m}/.git"; sudo unset GIT_DIR; {GIT}',
            f'export GIT_DIR="{m}/.git"; env GIT_DIR="{a}/.git"; {GIT}',
            f'command export GIT_DIR="{m}/.git"; {GIT}',
            f'export GIT_DIR="{m}/.git"; command export GIT_DIR="{a}/.git"; {GIT}',
            f'export GIT_DIR="{m}/.git"; command unset GIT_DIR; {GIT}',
        ))
        self.assertAllowed((f'export GIT_DIR="{m}/.git"; unset GIT_DIR; {GIT}', f'export GIT_DIR="{a}/.git"; {GIT}'))

    def test_git_after_a_wrapper_still_runs(self):
        m = self.main
        self.assertDenied(tuple(f'{p} git -C "{m}" reset --hard' for p in ("env", "nohup", "sudo", "exec", "command", "time")))
        self.assertDenied((f'cd "{m}"; command {GIT}', f'cd "{m}"; nohup {GIT}', f'env -C "{m}" {GIT}'))


class ChdirTest(ChdirReadingBase):
    def test_chdir_moves_in_zsh(self):
        m, a = self.main, self.a
        self.assertDenied((
            f'chdir "{m}"; {GIT}',
            f'chdir "{m}" && {GIT}',
            f'chdir -q "{m}"; {GIT}',
            f'chdir -L "{m}"; {GIT}',
            f'chdir -- "{m}"; {GIT}',
            f'chdir "{m}" >/dev/null 2>&1; {GIT}',
            f'time chdir "{m}"; {GIT}',
            f'if chdir "{m}"; then {GIT}; fi',
            f'(chdir "{m}"; {GIT})',
            f'printf a | chdir "{m}"; {GIT}',
            f'printf a | chdir "{m}" && {GIT}',
            f'cat <<EOF | chdir "{m}" && git add .\nbody\nEOF',
            f'cat <<EOF && chdir "{m}" && git add .\nbody\nEOF',
            f'git add . && chdir "{m}" && git commit -m x',
            f'builtin chdir "{m}"; {GIT}',
            f'builtin chdir -q "{m}"; {GIT}',
            # bash は失敗して main に残る
            f'cd "{m}"; chdir "{a}"; {GIT}',
            f'cd "{m}"; builtin chdir "{a}"; {GIT}',
            f'cd "{m}"; chdir "{a}" || {GIT}',
            f'chdir "{m}" ; chdir -x "{a}"; {GIT}',
        ))

    def test_chdir_that_does_not_reach_main(self):
        m, a = self.main, self.a
        self.assertAllowed((
            f'chdir "{a}"; {GIT}',
            # 成功なら worktree へ移動し、失敗なら git を飛ばす
            f'cd "{m}"; chdir "{a}" && {GIT}',
            f'chdir "{m}" || {GIT}',
            # 語が 2 つ / 読めない option は zsh も失敗する
            f'chdir "{m}" extra; {GIT}',
            f'chdir -n "{m}"; {GIT}',
            f'chdir -e "{m}"; {GIT}',
            f'chdir -x "{m}"; {GIT}',
            # サブシェルの中の移動は外に残らない
            f'(chdir "{m}"); {GIT}',
            f'chdir "{m}" & wait; {GIT}',
            f'chdir "{m}"; git log -1',
            # 行き先の分からない `builtin chdir` は読まない (`builtin cd` と同じ)
            f'builtin chdir "$PWD"; {GIT}',
        ))


class CdOptionTest(ChdirReadingBase):
    def test_options_only_zsh_accepts_move_in_zsh_and_fail_in_bash(self):
        m, a = self.main, self.a
        for opt in ("-q", "-s", "-qP", "-q --", "-s -L"):
            for verb in ("cd", "pushd"):
                self.assertDenied((f'{verb} {opt} "{m}"; {GIT}', f'cd "{m}"; {verb} {opt} "{a}"; {GIT}'))
                self.assertAllowed((f'{verb} {opt} "{a}"; {GIT}', f'cd "{m}"; {verb} {opt} "{a}" && {GIT}'))
        self.assertDenied((f'cd -q "{m}" >/dev/null; {GIT}', f'git add . && cd -q "{m}" && git commit -m x'))

    def test_builtin_cd_with_zsh_options(self):
        m, a = self.main, self.a
        self.assertDenied((f'builtin cd -q "{m}"; {GIT}', f'builtin cd -s "{m}"; {GIT}', f'builtin cd -qP -- "{m}"; {GIT}'))
        self.assertDenied((f'cd "{m}"; builtin cd -q "{a}"; {GIT}',))

    def test_bash_does_not_accept_pushd_link_options(self):
        # bash の pushd は `-L` / `-P` を受け付けない。zsh は移動する
        m, a = self.main, self.a
        for opt in ("-L", "-P", "-LP", "-L --"):
            self.assertDenied((f'pushd {opt} "{m}"; {GIT}', f'cd "{m}"; pushd {opt} "{a}"; {GIT}'))
        self.assertAllowed((f'cd "{m}"; pushd -- "{a}"; {GIT}', f'pushd -- "{a}"; {GIT}'))
        self.assertDenied((f'pushd -- "{m}"; {GIT}',))

    def test_options_of_both_shells_still_move(self):
        m, a = self.main, self.a
        for opt in ("-L", "-P", "-LP", "--", "-L --"):
            self.assertDenied((f'cd {opt} "{m}"; {GIT}',))
            self.assertAllowed((f'cd "{m}"; cd {opt} "{a}"; {GIT}',))

    def test_options_neither_shell_accepts_do_not_move(self):
        m, a = self.main, self.a
        for opt in ("-n", "-x", "-@"):
            self.assertDenied((f'cd "{m}"; cd {opt} "{a}"; {GIT}', f'cd "{m}"; cd {opt}; {GIT}', f'cd "{m}"; cd {opt} "{a}" || {GIT}'))
            self.assertAllowed((f'cd {opt} "{m}"; {GIT}', f'cd "{m}"; cd {opt} "{a}" && {GIT}'))
        for opt in ("-e", "-@", "-x", "-n"):
            self.assertDenied((f'cd "{m}"; pushd -P {opt} "{a}"; {GIT}',))
        for opt in ("-e", "-@", "-x"):
            self.assertAllowed((f'pushd {opt} "{m}"; {GIT}',))

    def test_options_of_newer_bash_are_read_as_moving_there(self):
        # `cd -e` は bash 3.2 と zsh が失敗し、bash 5.2 は移動する。新しい bash を見逃さない側に倒す
        m, a = self.main, self.a
        for opt in ("-e", "-P -e"):
            self.assertDenied((f'cd {opt} "{m}"; {GIT}', f'cd "{m}"; cd {opt} "{a}"; {GIT}'))
        self.assertAllowed((f'cd -e "{a}"; {GIT}',))

    def test_stack_and_previous_directory_stay_unknown(self):
        # 行き先が分からないので注意だけ出す
        m = self.main
        for cmd in (f'cd "{m}"; cd -; {GIT}', f'cd "{m}"; cd +1; {GIT}', f'cd "{m}"; cd -1; {GIT}', f'cd "{m}"; pushd +1; {GIT}',
                    f'cd "{m}"; cd -- -x; {GIT}'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "context")

    def test_two_words_move_in_bash_and_fail_in_zsh(self):
        m, a = self.main, self.a
        self.assertDenied((
            f'cd "{m}" extra; {GIT}',
            f'cd "{m}" extra >/dev/null; {GIT}',
            f'cd "{m}"; cd "{a}" extra; {GIT}',
            f'cd "{m}"; cd "{a}" extra 2>/dev/null; {GIT}',
            f'cd "{m}"; pushd "{a}" extra 2>/dev/null; {GIT}',
        ))
        self.assertAllowed((
            # redirect の前の語が 1 つなら 2 語ではない
            f'cd "{m}"; cd "{a}" >/dev/null; {GIT}',
            f'cd "{m}"; cd "{a}" 2>/dev/null; {GIT}',
            f'cd "{m}"; cd "{a}" >/dev/null 2>&1; {GIT}',
            f'cd "{m}"; pushd "{a}" 2>/dev/null; {GIT}',
        ))


class PushdPopdTest(ChdirReadingBase):
    def test_pushd_n_does_not_move(self):
        m, a = self.main, self.a
        self.assertDenied((
            f'cd "{m}"; pushd -n "{a}"; {GIT}',
            f'cd "{m}"; pushd -n -- "{a}"; {GIT}',
            f'cd "{m}"; pushd -n; {GIT}',
            f'cd "{m}"; pushd -n +1; {GIT}',
            f'cd "{m}" && pushd -n "{a}" && {GIT}',
            # bash は積んだ行き先へ popd が移動し、zsh は pushd が失敗して main に残る
            f'cd "{m}"; pushd -n "{a}"; popd; {GIT}',
            f'pushd -n "{m}"; popd; {GIT}',
            f'pushd -n -- "{m}"; popd; {GIT}',
            f'pushd -n "{m}" && popd && {GIT}',
        ))
        self.assertAllowed((
            f'pushd -n "{m}"; {GIT}',
            f'pushd -n "{a}"; {GIT}',
            f'pushd -n +1; {GIT}',
            f'pushd -n "{m}" >/dev/null; {GIT}',
        ))

    def test_popd_after_redirect_pops(self):
        # 語の無い popd に redirect を付けただけ。行き先は決まる
        m, a = self.main, self.a
        self.assertAllowed((
            f'pushd "{m}" >/dev/null; popd >/dev/null; {GIT}',
            f'pushd "{m}" >/dev/null 2>&1; popd >/dev/null 2>&1; {GIT}',
        ))
        self.assertDenied((
            f'cd "{m}"; pushd "{a}" >/dev/null; popd >/dev/null; {GIT}',
            f'cd "{m}"; pushd "{a}" >/dev/null 2>&1; popd >/dev/null 2>&1; {GIT}',
            f'pushd "{m}" >/dev/null && {GIT} && popd >/dev/null',
        ))

    def test_popd_q_pops_in_zsh(self):
        m, a = self.main, self.a
        self.assertDenied((f'cd "{m}"; pushd -q "{a}"; popd -q; {GIT}', f'pushd -q "{m}" && {GIT} && popd -q'))
        self.assertAllowed((f'cd "{m}"; pushd -q "{a}" && {GIT}; popd -q',))

    def test_popd_on_an_empty_stack_fails_and_stays(self):
        m = self.main
        self.assertDenied((
            f'cd "{m}"; popd; {GIT}',
            f'cd "{m}"; popd >/dev/null; {GIT}',
            f'cd "{m}"; popd 2>/dev/null; {GIT}',
            f'cd "{m}"; popd -q; {GIT}',
            f'cd "{m}"; popd || {GIT}',
        ))
        # `popd +1` と、`pushd -n +1` でスタックを回した後の popd は、行き先を読まない (`pushd -n +1` の例は bash が main に残る)
        for cmd in (f'popd; {GIT}', f'cd "{m}"; popd && {GIT}', f'pushd "{m}"; popd +1; {GIT}', f'pushd "{m}"; pushd -n +1; popd; {GIT}'):
            with self.subTest(cmd=cmd):
                # 呼び出し前のスタックは分からないので注意は出す
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "context")

    def test_pushd_without_words_stays_unknown(self):
        m = self.main
        for cmd in (f'cd "{m}"; pushd >/dev/null; {GIT}', f'pushd >/dev/null; {GIT}', f'pushd; {GIT}', f'pushd -q; {GIT}'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "context")
        self.assertDenied((f'cd "{m}"; pushd 2 >/dev/null; {GIT}',))

    def test_stack_is_tracked_through_each_step(self):
        m, a = self.main, self.a
        # 語の無い pushd は今の場所をスタックに積む。ここは main と worktree が入れ替わり、次の popd で worktree に戻る
        self.assertAllowed((f'cd "{m}"; pushd "{a}"; pushd; popd; {GIT}',))
        # `popd +1` はスタックの要素を消す。行き先は読まないので注意だけ出す (消さないと次の popd を worktree に戻ると読む)
        self.assertEqual(decision(evaluate(bash(f'pushd "{m}"; popd +1; popd; {GIT}', self.a))), "context")
        # 語の無い popd も消す。2 つ目の popd はスタックが空に見える
        self.assertEqual(decision(evaluate(bash(f'pushd "{m}"; popd; popd; {GIT}', self.a))), "context")


class NegationTest(ChdirReadingBase):
    """`!` の成否は読まない。移動の読みはそのままで、`&&` / `||` の後ろはどちらも実行しうるとする。"""

    def test_either_outcome_runs_what_follows(self):
        m, a = self.main, self.a
        self.assertDenied((
            f'! cd "{m}" || {GIT}',
            f'! false && git -C "{m}" reset --hard',
            f'! true || git -C "{m}" reset --hard',
            f'cd "{m}"; ! false && {GIT}',
            f'cd "{m}"; ! chdir "{a}" && {GIT}',
            f'cd "{m}"; ! cd -q "{a}" && {GIT}',
            f'cd "{m}"; if ! chdir "{a}"; then {GIT}; fi',
            f'cd "{m}"; time ! chdir "{a}" && {GIT}',
            # 実際には git は走らない (成否を読まないので止まる)
            f'! cd "{m}" && {GIT}',
            f'! true && git -C "{m}" reset --hard',
            f'! : && git -C "{m}" reset --hard',
            f'! false || git -C "{m}" reset --hard',
            f'! chdir "{m}" && {GIT}',
        ))
        self.assertAllowed((
            f'! cd "{a}" && {GIT}',
            f'if ! chdir "{a}"; then {GIT}; fi',
        ))

    def test_two_bangs_cancel(self):
        m, a = self.main, self.a
        self.assertAllowed((
            f'! ! false && git -C "{m}" reset --hard',
            f'! ! true || git -C "{m}" reset --hard',
            f'cd "{m}"; ! ! cd "{a}" && {GIT}',
            f'cd "{m}"; ! ! cd "{a}" || {GIT}',
        ))

    def test_move_after_negation_reads_like_plain_move(self):
        # 移動は成功すると読む (plain の `cd x; git ...` と同じ)。行き先が別 checkout でなければ止めない
        m, a = self.main, self.a
        self.assertAllowed((
            f'cd "{m}"; ! cd "{a}"; {GIT}',
            f'cd "{m}"; ! cd "{a}"\n{GIT}',
            f'cd "{m}"; if ! cd "{a}"; then exit 1; fi; {GIT}',
            f'cd "{m}"; ! pushd "{a}"; {GIT}',
            f'cd "{m}"; ! cd "{a}" || true; {GIT}',
            f'cd "{m}"; ! cd "{a}" && exit 1; {GIT}',
            f'cd "{m}"; ! cd "{a}" && {GIT}',
            f'cd "{m}"; ! cd "{a}" && true && {GIT}',
            f'cd "{m}"; ! cd "{a}" && true; {GIT}',
            f'if ! cd "{a}"; then exit 1; fi; git add .',
            f'cd "{m}"; if ! cd "{a}"; then exit 1; fi; git add .',
            f'! cd nonexistent && {GIT}',
            # サブシェルの中の移動は外に残らない
            f'! (cd "{m}") && {GIT}',
        ))
        # 行き先が dynamic なら注意だけ出す (plain の cd と同じ)
        for cmd in (f'cd "{m}"; ! cd $(echo "{a}"); {GIT}', f'cd "{m}"; ! cd +1 && {GIT}', f'cd "{m}"; ! popd +1 && {GIT}'):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(self._evaluate(cmd)), "context")
        # 移動先が別 checkout の下なら止める。後ろの -C は移動の成否に関係なく止める
        self.assertDenied((
            f'cd "{m}"; ! cd nonexistent && {GIT}',
            f'cd "{m}"; ! pushd nonexistent && {GIT}',
            f'cd "{m}"; ! cd nonexistent && ({GIT})',
            f'cd "{m}"; ! popd && {GIT}',
            f'cd "{m}"; ! cd -x && {GIT}',
            f'cd -q "{m}"; ! chdir nowhere && {GIT}',
            f'! cd /nonexistent && git -C "{m}" reset --hard',
            f'! cd +1 && git -C "{m}" reset --hard',
            f'! pushd nonexistent && git -C "{m}" reset --hard',
            f'! popd && git -C "{m}" reset --hard',
            f'! chdir nonexistent && git -C "{m}" reset --hard',
            f'! time cd nonexistent && git -C "{m}" reset --hard',
            f'! builtin cd nonexistent && git -C "{m}" reset --hard',
        ))

    def test_failed_move_after_negation_is_not_read(self):
        # 受け入れている見逃し: `!` の後ろの移動の失敗は読まない。実際は main で走るが、plain の `cd <無いパス> || ...` と同じく成功と読む
        m = self.main
        self.assertAllowed((
            f'cd "{m}"; ! cd /nonexistent && {GIT}',
            f'cd "{m}"; ! cd /nonexistent; {GIT}',
            f'cd "{m}"; ! cd /nonexistent &&\n{GIT}',
            f'cd "{m}" &&\n! cd /nonexistent &&\n{GIT}',
            f'pushd "{m}"; ! cd /nonexistent &&\n{GIT}',
            f'cd "{m}"; ! cd /nonexistent && ({GIT})',
            f'cd "{m}"; ! cd /nonexistent && (\n{GIT}\n)',
            f'cd "{m}"; ! cd /nonexistent && true && {GIT}',
        ))

    def test_bang_after_command_is_a_command_name(self):
        m = self.main
        # `command !` の `!` はコマンド名で、予約語にならない (どちらの shell でも見つからず失敗する)
        self.assertAllowed((f'command ! cd "{m}"; {GIT}', f'command ! cd "{m}" && {GIT}'))
        self.assertDenied((f'command ! cd "{m}" || git -C "{m}" reset --hard',))
        # `env !` も同じ
        self.assertAllowed((f'env ! false && git -C "{m}" reset --hard',))


class ClosingBraceTest(ChdirReadingBase):
    # zsh は `{ cd <dir> }` の語の末尾の `}` を閉じとして読む (bash は構文エラーで何も走らせない)
    def test_closing_brace_is_not_a_word(self):
        m, a = self.main, self.a
        self.assertDenied((
            f'{{ chdir "{m}" }}; {GIT}',
            f'{{ cd -q "{m}" }}; {GIT}',
            f'{{ pushd -q "{m}" }}; {GIT}',
            f'{{ cd "{m}" }}; {GIT}',
            f'{{ builtin cd "{m}" }}; {GIT}',
            f'{{ cd "{m}" }} >/dev/null; {GIT}',
            f'cd "{m}"; {{ pushd "{a}" }}; {{ popd }}; {GIT}',
            f'cd "{m}"; {{ cd "{a}" extra }}; {GIT}',
        ))
        self.assertAllowed((
            f'cd "{m}"; {{ cd "{a}" }}; {GIT}',
            f'cd "{m}"; {{ builtin cd "{a}" }}; {GIT}',
            f'{{ pushd "{m}" }}; {{ popd }}; {GIT}',
            # 語の無い `cd` は HOME へ移動する
            f'cd "{m}"; {{ cd }}; {GIT}',
        ))
        self.assertEqual(decision(evaluate(bash(f'cd "{m}"; {{ pushd }}; {GIT}', self.a))), "context")


class LayoutTest(ChdirReadingBase):
    """`!` と括弧・コマンド置換・改行の組み合わせで、実行する状態が空になって後ろの git を読まなくなることがない。"""

    def test_negated_assignment_with_command_substitution(self):
        # `! x=$(...)` の `(` の手前の語はコマンドとして読む。成否を片方に決めると、`&&` の後ろの括弧の中を読まなくなる
        m = self.main
        gm = f'git -C "{m}" reset --hard'
        self.assertDenied((
            f'cd "{m}" && if ! out=$(git pull --rebase 2>&1); then echo "$out"; exit 1; fi',
            f'cd "{m}" &&\nif ! out=$({GIT} 2>&1); then\nexit 1\nfi',
            f'cd "{m}" && ! x=$(cat missing) && {GIT}',
            f'true && ! x=$({gm})',
            f'git fetch && if ! x=$({gm}); then :; fi',
            f'cd "{m}" && ! export x=$({GIT})',
            f'cd "{m}" && ! : $({GIT})',
        ))
        # 実行されない形は読まない
        self.assertAllowed((f'false && ! x=$({gm})', f'true || ! x=$({gm})'))

    def test_newline_right_after_opening_paren(self):
        m = self.main
        gm = f'git -C "{m}" reset --hard'
        for head in ("! cd /nonexistent", "! pushd /nonexistent", "! popd +9", "! cd +1"):
            self.assertDenied((
                f'{head} && (\n{gm}\n)',
                f'{head} && ( # c\n{gm}\n)',
                f'{head} && ( (\n{gm}\n) )',
                f'if {head} && (\n{gm}\n); then true; fi',
                f'{head} && ({gm})',
            ))

    def test_negated_paren(self):
        m = self.main
        gm = f'git -C "{m}" reset --hard'
        self.assertDenied((
            f'true && ! ( {gm} )',
            f'cd "{m}" && ! ( {GIT} )',
            f'cd "{m}" && ! ({GIT})',
            f'git status && ! ( {gm} )',
            f'false || ! ( {gm} )',
            f'true && ! ! ( {gm} )',
            f'true && if ( {gm} ); then true; fi',
            f'true && ! ( {gm} ); true',
        ))
        # 実際に飛ばす形は飛ばす
        self.assertAllowed((f'false && ! ( {gm} )', f'true || ! ( {gm} )', f'false && ! ! ( {gm} )'))

    def test_keyword_before_paren_reads_as_a_succeeding_command(self):
        # 受け入れている見逃し (0.5.0 と同じ): `(` の手前の予約語は成功したコマンドと読むので、`false ||` の後ろの括弧の中を読まない
        gm = f'git -C "{self.main}" reset --hard'
        self.assertAllowed((
            f'false || if ( {gm} ); then :; fi',
            f'false || while ( {gm} ); do break; done',
            f'false || {{ ( {gm} ); }}',
            f'false || ! ! ( {gm} )',
        ))

    def test_newline_after_and_or_ends_the_list(self):
        # 演算子の後ろの改行は list を切る。次の行は手前の成否に関係なく実行すると読む (0.5.0 と同じ。実際より止める側)
        m, a = self.main, self.a
        self.assertDenied((
            f'false ||\ngit -C "{m}" reset --hard',
            f'! false &&\ngit -C "{m}" reset --hard',
            f'false &&\ngit -C "{m}" reset --hard',
            f'true ||\ngit -C "{m}" reset --hard',
            f'cd /nonexistent ||\ngit -C "{m}" reset --hard',
            f'true && !\ngit -C "{m}" reset --hard',
        ))
        self.assertAllowed((
            f'cd "{m}"; ! cd "{a}" ||\n{GIT}',
            f'cd "{m}"; ! ! cd "{a}" &&\n{GIT}',
            f'cd "{a}" || exit 1\ngit add .',
            f'(cd sub && make) && git add .',
            f'cd "{a}" && git add .',
        ))
        # `|` の後ろの改行は pipeline の続き (zsh の `command cd` は外部コマンドで、bash は要素をサブシェルで動かす)
        self.assertAllowed((f'printf a |\ncommand cd "{m}"; {GIT}',))


class ReservedBangTest(ChdirReadingBase):
    def test_bang_after_time(self):
        m, a = self.main, self.a
        gm = f'git -C "{m}" reset --hard'
        self.assertDenied((
            f'time -- ! false || {gm}',
            f'time time ! false || {gm}',
            f'time -p -- ! false || {gm}',
            f'time -p ! false || {gm}',
            f'time ! false || {gm}',
            f'! time false || {gm}',
            # bash は `time -p` の後ろを今の shell で動かし、zsh は外部の time が失敗する。どちらも git が走る
            f'time -p ! true || {gm}',
            f'time -- ! true || {gm}',
            # zsh は main にいて外部の time が失敗する。`!` が予約語になるのは bash の読みだけだが、どちらの読みでも成否を読まない
            f'chdir "{m}"; time -- ! true || {GIT}',
        ))
        # 後ろが今の shell で動かないと `!` は予約語にならない
        self.assertAllowed((f'time -f x ! false && {gm}', f'cd "{m}"; time ! cd "{a}" || {GIT}'))

    def test_bang_after_assignment_is_a_command_name(self):
        m, a = self.main, self.a
        gm = f'git -C "{m}" reset --hard'
        self.assertDenied((
            f'FOO=1 ! false || {gm}',
            f'cd "{m}"; FOO=1 ! cd "{a}"; {GIT}',
            f'cd "{m}"; FOO=1 ! cd "{a}" || {GIT}',
            # 代入の前の `!` は予約語
            f'! FOO=1 true || {gm}',
        ))
        # 代入の前の `!` の後ろは移動として読む
        self.assertAllowed((f'cd "{m}"; ! FOO=1 cd "{a}"; {GIT}',))


class LoneClosingBraceTest(ChdirReadingBase):
    def test_brace_without_opening_is_a_word(self):
        # 開き括弧が無い `}` は閉じではなく、`}` という名前のディレクトリへの移動 (無ければ失敗して動かない)
        m, a = self.main, self.a
        self.assertDenied((
            f'cd "{m}"; cd }}; {GIT}',
            f'cd "{m}"; pushd }}; {GIT}',
            f'cd "{m}"; builtin cd }}; {GIT}',
            f"cd \"{m}\"; cd '}}'; {GIT}",
            f'cd "{m}"; cd \\}}; {GIT}',
            # 閉じた後の `}` も語
            f'{{ true; }}; cd "{m}"; cd }}; {GIT}',
            f'{{ true }} >/dev/null; cd "{m}"; cd }}; {GIT}',
            # 語が 2 つになる。bash 3.2 だけ移動する
            f'cd "{m}"; cd "{a}" }}; {GIT}',
        ))
        # `popd }` は `popd +N` と同じく語のある popd で、行き先は読まない (語の無い popd の、スタックが空の読みにしない)
        self.assertEqual(decision(evaluate(bash(f'cd "{m}"; popd }}; {GIT}', a))), "context")

    def test_opening_brace_is_counted_where_the_command_is_not_run(self):
        m, a = self.main, self.a
        # `(` の手前の `{` も数える
        self.assertAllowed((f'cd "{m}"; {{ (true); cd "{a}" }}; {GIT}',))
        # 実行されないコマンドの括弧も数える (開きと閉じの対が崩れない)
        self.assertDenied((f'{{ false && true }}; cd "{m}"; cd }}; {GIT}',))
        self.assertAllowed((f'cd "{m}"; false && {{ true }}; {{ cd "{a}" }}; {GIT}', f'cd "{m}"; {{ false && {{ true }}; cd "{a}" }}; {GIT}'))


class PipelineTest(ChdirReadingBase):
    def test_last_element_moves_in_zsh_only(self):
        m, a = self.main, self.a
        self.assertDenied((f'printf a | cd "{m}"; {GIT}', f'printf a | cd "{m}"\n{GIT}', f'cd "{m}"; printf a | cd "{a}"; {GIT}'))
        self.assertDenied((f'printf a | pushd -q "{m}"; {GIT}', f'cd "{m}"; printf a | command cd "{a}"; {GIT}'))
        # zsh の `command cd` は外部コマンドで、bash は pipeline の要素をサブシェルで動かす
        self.assertAllowed((f'printf a | command cd "{m}"; {GIT}',))


class BackgroundListTest(ChdirReadingBase):
    """zsh の `&` は最後の pipeline だけを background にする。bash は AND-OR list 全体を background にする。"""

    def test_move_before_the_last_pipeline_stays_in_zsh(self):
        m = self.main
        self.assertDenied((
            f'cd "{m}" && sleep 0 & {GIT}',
            f'cd "{m}" && true & wait; {GIT}',
            f'pushd "{m}" && true & {GIT}',
            f'chdir "{m}" && true & {GIT}',
            f'cd "{m}" || true & {GIT}',
            # 最後の pipeline が複数の要素や括弧でも同じ
            f'cd "{m}" && true | cat & {GIT}',
            f'cd "{m}" && (true) & {GIT}',
            f'(cd "{m}" && true & {GIT})',
            # 環境変数も残る
            f'export GIT_DIR="{m}/.git" && true & {GIT}',
        ))

    def test_the_last_pipeline_itself_does_not_stay(self):
        m, a = self.main, self.a
        self.assertAllowed((
            f'cd "{m}" & {GIT}',
            f'cd "{m}" &\n{GIT}',
            f'true && cd "{m}" & {GIT}',
            f'false || cd "{m}" & {GIT}',
            f'cd "{a}" && cd "{m}" & {GIT}',
            # サブシェルの中で残っても、外には残らない
            f'(cd "{m}" && true &); {GIT}',
            # 普段の形
            f'cd "{a}" && make & wait; git add .',
            f'cd "{a}" && git add . && git commit -m x &',
        ))

    def test_each_shell_reads_its_own_start(self):
        m, a = self.main, self.a
        # bash は list の開始時点へ戻るので main (実際に main で走る)
        self.assertDenied((f'cd "{m}"; cd "{a}" && true & {GIT}', f'export GIT_DIR="{m}/.git"; unset GIT_DIR && true & {GIT}'))
        # zsh の手前の移動が失敗すれば、手前の場所に残る (0.6.0 と同じ deny。bash は chdir / cd -q が失敗して worktree)
        self.assertDenied((
            f'cd -q "{m}"; cd /nonexistent && true & {GIT}',
            f'chdir "{m}"; ! cd /nonexistent && true & {GIT}',
            f'cd -q "{m}"; cd /nonexistent || true & {GIT}',
            # 移動できていても読みは変わらない (実際には zsh も worktree で走る)
            f'chdir "{m}"; chdir "{a}" && true & {GIT}',
            f'cd -q "{m}"; cd -q "{a}" && true & {GIT}',
        ))
        # 自分の worktree の中の移動は止めない
        self.assertAllowed(('cd sub && true & git add .',))

    def test_list_folded_after_the_operator_is_one_list(self):
        m, a = self.main, self.a
        self.assertDenied((
            f'cd "{m}"; cd "{a}" &&\ntrue & {GIT}',
            f'cd "{m}"; cd "{a}" &&\n\ntrue & {GIT}',
            f'cd "{m}"; cd "{a}" && # c\ntrue & {GIT}',
            f'cd "{m}"; cd /nonexistent ||\ntrue & {GIT}',
            f'cd "{m}"; cd "{a}" &&\ntrue &&\ntrue & {GIT}',
            f'cd "{m}"; cd "{a}" &&\nmake &\nwait\ngit add .',
            f'export GIT_DIR="{m}/.git"; unset GIT_DIR &&\ntrue & {GIT}',
            f'GIT_DIR="{m}/.git"; unset GIT_DIR &&\ntrue & export GIT_DIR; git add .',
            # 値が割れたら list の開始時点の値を読む (bash は list ごと background で動くので、今の shell の値はそちら)
            f'export GIT_DIR="{m}/.git"; export GIT_DIR="{a}/.git" &&\ntrue & git add .',
            f'GIT_DIR="{m}/.git"; GIT_DIR="{a}/.git" &&\ntrue & export GIT_DIR; git add .',
            # 改行の時点の候補も残す (0.6.0 と同じ deny。`command` は bash だけ今の shell で動く)
            f'command cd "{m}" &&\ntrue & {GIT}',
            f'command export GIT_DIR="{m}/.git" &&\ntrue & {GIT}',
            # 切らない読みは `&` で手前の値に戻す。bash は改行の時点の値で git を動かすので、切る読みも通す
            f'export GIT_DIR="{a}/.git"; command export GIT_DIR="{m}/.git" &&\ntrue & git -C "{m}" reset --hard',
            f'export GIT_DIR="{a}/.git"; command export GIT_DIR="{m}/.git" &&\ntrue & cd "{m}"; {GIT}',
            f'export GIT_DIR="{a}/.git"; command export GIT_DIR="{m}/.git" &&\ntrue &\nwait\ngit -C "{m}" reset --hard',
            f'export GIT_DIR="{a}/.git"; command export GIT_DIR="{m}/.git" && # c\ntrue & git -C "{m}" reset --hard',
            f'export GIT_DIR="{a}/.git"; command export GIT_DIR="{m}/.git" ||\ntrue & git -C "{m}" reset --hard',
            # zsh は `||` の直後の `;` を読み飛ばして右辺が続く (bash は構文エラー)
            f'cd "{m}"; cd /nonexistent || ; true & {GIT}',
            f'cd -q "{m}"; cd /nonexistent || ; true & {GIT}',
            # zsh だけの移動の折り返しは zsh の読みでしか止まらない (`cd -q` は bash では失敗する)
            f'cd -q "{m}"; cd /nonexistent ||\ntrue & {GIT}',
        ))
        self.assertAllowed((
            f'cd "{a}" &&\nmake &\nwait\ngit add .',
            'cd sub &&\nmake &\ngit add .',
            'git add . &&\ngit commit -m x &\nwait',
            # 改行の手前にコマンドがある (`&&` の後ろの list はそこで終わる) なら切れる
            f'cd "{m}"; cd "{a}"\nmake &\ngit add .',
            f'cd "{m}"; cd "{a}" &&\nmake\nmake &\ngit add .',
        ))


class FailedMoveTest(ChdirReadingBase):
    """移動が失敗して `||` の右に進む経路は読む。右辺の後ろには残さない。"""

    def test_right_of_or_runs_where_the_move_failed(self):
        m = self.main
        gm = f'git -C "{m}" reset --hard'
        self.assertDenied((
            f'cd "{m}"; cd /nonexistent || {GIT}',
            f'cd /nonexistent || {gm}',
            f'cd "{m}"; cd }} || {GIT}',
            f'cd "{m}"; cd +1 || {GIT}',
            f'cd "{m}"; popd +9 || {GIT}',
            f'cd }} || {gm}',
            f'cd +1 || {gm}',
            f'popd +9 || {gm}',
            f'pushd /nonexistent || {gm}',
            # 語彙
            f'cd "{m}"; builtin cd /nonexistent || {GIT}',
            f'cd "{m}"; time cd /nonexistent || {GIT}',
            f'cd "{m}"; FOO=1 cd /nonexistent || {GIT}',
            f'cd "{m}"; cd -- /nonexistent || {GIT}',
            f'cd "{m}"; cd -P /nonexistent || {GIT}',
            f'cd "{m}"; cd /nonexistent >/dev/null 2>&1 || {GIT}',
            # 続けて失敗する
            f'cd "{m}"; cd /nonexistent || cd /n2 || {GIT}',
            f'cd "{m}" && cd /nonexistent || {GIT}',
            # pipeline の要素
            f'cd "{m}"; cd /nonexistent || git status | {GIT}',
        ))

    def test_layout_of_the_right_side(self):
        m = self.main
        gm = f'git -C "{m}" reset --hard'
        for head, git in ((f'cd "{m}"; cd /nonexistent', GIT), ('cd /nonexistent', gm)):
            self.assertDenied((
                f'{head} ||\n{git}',
                f'{head} ||\n\n{git}',
                f'{head} || # c\n{git}',
                f'{head} || ({git})',
                f'{head} || (\n{git}\n)',
                f'{head} || ( # c\n{git}\n)',
                f'{head} ||\n({git})',
                f'{head} || ! ({git})',
                f'{head} || ! (\n{git}\n)',
                f'{head} ||\n! ({git})',
                f'{head} || {{ {git}; }}',
                f'{head} || {{\n{git}\n}}',
                f'{head} ||\n{{\n{git}\n}}',
                f'{head} || {{ ({git}); }}',
                f'{head} || if {git}; then :; fi',
                f'{head} || if ({git}); then :; fi',
                f'{head} || x=$({git})',
                f'{head} || echo $({git})',
            ))

    def test_right_side_does_not_outlive_itself(self):
        # 普段の形。失敗の経路は右辺の中でしか読まないので、移動できる前提の後ろの git を止めない
        m, a = self.main, self.a
        self.assertAllowed((
            f'cd "{a}" || exit 1; git add .',
            f'cd "{a}" || exit 1\ngit add .',
            f'cd "{a}" ||\nexit 1\ngit add .',
            f'cd "{m}"; cd "{a}" || exit 1; git add .',
            f'cd "{m}"; cd "{a}" || exit 1\ngit add .',
            f'cd "{m}"; cd "{a}" ||\nexit 1\ngit add .',
            f'cd "{m}"; cd "{a}" ||\n\nexit 1\ngit add .',
            f'cd "{m}"; cd "{a}" || exit 1 && git add .',
            f'cd "{m}"; cd "{a}" || {{ echo no; exit 1; }}; git add .',
            f'cd "{m}"; cd "{a}" || {{\necho no\nexit 1\n}}\ngit add .',
            f'cd "{m}"; cd "{a}" || true; git add .',
            f'cd "{m}"; cd "{a}" || (exit 1); git add .',
            f'cd "{m}"; cd "{a}" || x=$(echo no); git add .',
            f'cd "{m}"; if ! cd "{a}"; then exit 1; fi; git add .',
            f'cd "{m}"; cd "{a}" && git add .',
            f'cd "{a}" && git add .',
            'cd sub 2>/dev/null || exit 1; git add .',
            f'pushd "{a}" >/dev/null || exit 1; git add .; popd >/dev/null',
        ))

    def test_and_forgets_the_failed_move(self):
        m, a = self.main, self.a
        self.assertAllowed((
            f'cd "{m}"; cd "{a}" && false || {GIT}',
            f'cd "{m}"; cd "{a}" && true || {GIT}',
        ))

    def test_substitution_in_the_move_runs_before_it(self):
        # `cd $(...)` の中は移動の手前の場所で走る。行き先の語の `$(` も同じ経路で読む
        m, a = self.main, self.a
        self.assertDenied((f'cd "{m}"; cd $({GIT})',))
        # 読み取りと、自分の worktree に戻る形は止めない
        self.assertAllowed((f'cd "{m}"; cd $(git status); cd "{a}"; git add .',))

    def test_success_of_the_right_side_is_read(self):
        m = self.main
        # 右辺が移動で、成功すれば次の `||` には進まない。失敗すれば元の場所に残る
        self.assertAllowed((f'cd /nonexistent || cd "{m}" || git add .',))
        # 右辺の括弧の成否は両方ありうる
        self.assertDenied((f'cd "{m}"; cd /nonexistent || (true) || {GIT}',))

    def test_background_ends_the_right_side(self):
        # `&` の後ろは右辺ではない。bash は list ごと background で動くので、`command cd` の移動は今の shell に残らない
        # (zsh の `command cd` は外部コマンドで移動しない)
        m, a = self.main, self.a
        self.assertAllowed((f'command cd "{m}" && cd "{a}" || true & git add .',))

    def test_or_after_a_pipeline_or_a_negation_reads_as_before(self):
        m, a = self.main, self.a
        # `!` の後ろの移動の失敗は読まない (`! !` も同じ)
        self.assertAllowed((f'cd "{m}"; ! ! cd "{a}" || {GIT}', f'cd "{m}"; ! cd /nonexistent || {GIT}'))
        # 外部コマンドとして動く移動は成否の両方を読む (従来どおり)
        self.assertDenied((f'cd "{m}"; env cd /nonexistent || {GIT}', f'cd "{m}"; cd /nonexistent | cat || {GIT}'))
        # pipeline の途中の移動は `||` の相手ではない
        self.assertAllowed((f'cd "{a}" | cat || git add .',))
        # zsh だけ移動する pipeline の最後の要素も、失敗すれば手前の場所に残る
        self.assertDenied((f'chdir "{m}"; printf a | chdir /nonexistent || {GIT}',))

    def test_right_side_does_not_change_what_follows(self):
        m, a = self.main, self.a
        # 右辺の変数の変更は読みの本筋に持ち込まない。export 済みの GIT_DIR は後ろの git に届く (cd が成功すれば右辺は動かない)
        self.assertDenied((
            f'export GIT_DIR="{m}/.git"; cd "{a}" || unset GIT_DIR; git add .',
            f'export GIT_DIR="{m}/.git"; cd /nonexistent || unset GIT_DIR\ngit add .',
        ))
        self.assertAllowed((
            f'cd "{a}" || export GIT_DIR="{m}/.git"; git add .',
            f'cd "{a}" || GIT_DIR="{m}/.git"; export GIT_DIR; git add .',
        ))
        # 右辺の中の読みは、手前の変数のまま
        self.assertDenied((f'export GIT_DIR="{m}/.git"; cd /nonexistent || git add .',))

    def test_only_shell_that_moves_reads_it(self):
        m, a = self.main, self.a
        # `command cd` は zsh では外部コマンドで成否の両方を読む (従来どおり)。bash は移動の失敗を読む
        self.assertDenied((f'cd "{m}"; command cd "{a}" || {GIT}',))
        # `&&` で失敗の経路を捨てるので、後ろの git は成功した移動先で読む
        self.assertAllowed((f'cd "{m}"; cd /nonexistent || cd "{a}" && git add .',))

    def test_accepted_misses(self):
        # 受け入れている見逃し: 右辺の外の経路は読まない (従来どおり)
        m = self.main
        self.assertAllowed((
            f'cd "{m}"; cd /nonexistent || true; {GIT}',
            f'cd "{m}"; cd /nonexistent || true && {GIT}',
            f'cd "{m}"; cd /nonexistent || git status && {GIT}',
            f'cd "{m}"; cd /nonexistent && false || {GIT}',
            f'cd "{m}"; cd /nonexistent || {{ echo x; {GIT}; }}',
            f'cd "{m}"; {{ cd /nonexistent; }} || {GIT}',
            f'cd "{m}"; cd /nonexistent || echo `{GIT}`',
            f'cd "{m}"; cd /nonexistent || echo "$({GIT})"',
            f'cd /nonexistent || cd "{m}"; {GIT}',
            f'cd /nonexistent || cd "{m}" && {GIT}',
            f'cd "{m}"; cd /nonexistent || if true; then {GIT}; fi',
            f'cd "{m}"; cd /nonexistent || while true; do {GIT}; break; done',
            f'cd "{m}"; if ! cd /nonexistent; then {GIT}; fi',
            f'cd "{m}"; if cd /nonexistent; then :; else {GIT}; fi',
            f'cd "{m}"; until cd /nonexistent; do {GIT}; break; done',
        ))


if __name__ == "__main__":
    unittest.main()
