"""移動の組み込み (`cd` / `chdir` / `pushd` / `popd`) を、今の shell で動くかと shell ごとの違いまで含めて読む。

期待値は、偽の git を置いて bash 3.2 と `zsh -f` で実測した結果に合わせている (git がどの checkout を対象に走ったかを見た)。
どちらかの shell で main に書く形は止め、どちらの shell でも main に書かない形は止めない。
"""
from __future__ import annotations

import unittest

import test_guard
from test_guard import bash, decision, evaluate

GIT = "git reset --hard"


class ChdirReadingBase(test_guard.GuardBase):
    def assertDenied(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def assertAllowed(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))


class WrappedCdTest(ChdirReadingBase):
    # 子プロセスで走るか、何も起動しない wrapper。移動はどちらの shell でも残らない。
    # `exec` は shell を置き換えるので実測では後ろの git が走らないが、それは読まない (`cd <main>; exec cd <worktree>; git ...` は止まる側)
    NO_MOVE = (
        "env", "env --", "env FOO=1", "env -i", "env -", "env -u X", "nohup", "nohup --", "sudo", "sudo -u me", "sudo -n",
        "sudo --", "exec", "exec -a x", "exec -c", "exec --", "time time", "time env", "time nohup", "time exec",
        "FOO=1 time", "FOO=1 time -p", "FOO=1 env", "FOO=1 nohup", "command -v", "command -V", "command -x", "command FOO=1",
        "command time", "command env", "command exec", "command nohup", "env command", "nohup command", "sudo command",
        "env time", "nohup time", "exec command", "exec time", "time --", "time -p --", "time -f x", "time -x", "\\env",
    )
    # bash だけ今の shell で動く
    BASH_ONLY = (
        "command", "command --", "command -p", "\\command", "'command'", "FOO=1 command", "command command", "time command",
        "time -p",
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
        for opt in ("-n", "-x"):
            self.assertDenied((f'cd "{m}"; cd {opt} "{a}"; {GIT}', f'cd "{m}"; cd {opt}; {GIT}', f'cd "{m}"; cd {opt} "{a}" || {GIT}'))
            self.assertAllowed((f'cd {opt} "{m}"; {GIT}', f'cd "{m}"; cd {opt} "{a}" && {GIT}'))
        for opt in ("-e", "-@", "-x", "-n"):
            self.assertDenied((f'cd "{m}"; pushd -P {opt} "{a}"; {GIT}',))
        for opt in ("-e", "-@", "-x"):
            self.assertAllowed((f'pushd {opt} "{m}"; {GIT}',))

    def test_options_of_newer_bash_are_read_as_moving_there(self):
        # `cd -e` / `cd -@` は bash 3.2 と zsh が失敗し、bash 4.2 / 4.3 以降は移動する。新しい bash を見逃さない側に倒す
        m, a = self.main, self.a
        for opt in ("-e", "-@", "-P -e"):
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
                self.assertIn(decision(evaluate(bash(cmd, self.a))), ("context", "deny"))
        self.assertEqual(decision(evaluate(bash(f'pushd >/dev/null; {GIT}', self.a))), "context")
        self.assertDenied((f'cd "{m}"; pushd 2 >/dev/null; {GIT}',))


class NegationTest(ChdirReadingBase):
    def test_negation_flips_success(self):
        m, a = self.main, self.a
        self.assertDenied((
            f'! cd "{m}" || {GIT}',
            f'! false && git -C "{m}" reset --hard',
            f'! true || git -C "{m}" reset --hard',
            f'cd "{m}"; ! chdir "{a}" && {GIT}',
            f'cd "{m}"; ! cd -q "{a}" && {GIT}',
            f'cd "{m}"; if ! chdir "{a}"; then {GIT}; fi',
            f'cd "{m}"; time ! chdir "{a}" && {GIT}',
        ))
        self.assertAllowed((
            f'! cd "{m}" && {GIT}',
            f'! cd "{a}" && {GIT}',
            f'! true && git -C "{m}" reset --hard',
            f'! false || git -C "{m}" reset --hard',
            f'if ! chdir "{a}"; then {GIT}; fi',
            f'! chdir "{m}" && {GIT}',
        ))

    def test_bang_after_command_is_a_command_name(self):
        m = self.main
        # `command !` の `!` はコマンド名で、反転しない (どちらの shell でも見つからず失敗する)
        self.assertAllowed((f'command ! cd "{m}"; {GIT}', f'command ! cd "{m}" && {GIT}'))
        self.assertDenied((f'command ! cd "{m}" || git -C "{m}" reset --hard',))
        # `env !` も同じ
        self.assertAllowed((f'env ! false && git -C "{m}" reset --hard',))


class PipelineTest(ChdirReadingBase):
    def test_last_element_moves_in_zsh_only(self):
        m, a = self.main, self.a
        self.assertDenied((f'printf a | cd "{m}"; {GIT}', f'cd "{m}"; printf a | cd "{a}"; {GIT}'))
        self.assertDenied((f'printf a | pushd -q "{m}"; {GIT}', f'cd "{m}"; printf a | command cd "{a}"; {GIT}'))
        # zsh の `command cd` は外部コマンドで、bash は pipeline の要素をサブシェルで動かす
        self.assertAllowed((f'printf a | command cd "{m}"; {GIT}',))


if __name__ == "__main__":
    unittest.main()
