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

    def test_failed_move_after_negation_stays(self):
        # `! cd x && git ...` の git は移動が失敗したときに走る。行き先に関係なく、手前の場所で git を読む
        m, a = self.main, self.a
        for dest in ("nonexistent", "+1", "-", "-1", '"$NOPE"', '-- nonexistent', '-x'):
            with self.subTest(dest=dest):
                self.assertDenied((f'! cd {dest} && git -C "{m}" reset --hard', f'cd "{m}"; ! cd {dest} && {GIT}', f'cd "{m}" && ! cd {dest} && {GIT}'))
        self.assertDenied((
            f'! pushd nonexistent && git -C "{m}" reset --hard',
            f'! popd && git -C "{m}" reset --hard',
            f'! chdir nonexistent && git -C "{m}" reset --hard',
            f'! time cd nonexistent && git -C "{m}" reset --hard',
            f'! builtin cd nonexistent && git -C "{m}" reset --hard',
            f'cd "{m}"; ! pushd nonexistent && {GIT}',
            f'cd "{m}"; ! popd && {GIT}',
            f'cd "{m}"; ! popd +1 && {GIT}',
            # zsh だけ main にいる。zsh の chdir の失敗も読む (bash の読みは手前の `cd -q` が失敗して worktree に残る)
            f'cd -q "{m}"; ! chdir nowhere && {GIT}',
            # worktree が消えていれば main で走る。cd の失敗を読むので止まる (普段は走らない)
            f'cd "{m}"; ! cd "{a}" && {GIT}',
        ))
        self.assertAllowed((
            # 手前の場所が自分の worktree なら、移動が失敗してもそこで走る
            f'! cd nonexistent && {GIT}',
            f'! cd "{a}" && {GIT}',
            # 2 回反転すると plain の cd と同じ読み (失敗を読まない)
            f'cd "{m}"; ! ! cd "{a}" && {GIT}',
        ))
        # 移動でないコマンドは足さない (`! true` は失敗にしかならない)
        self.assertAllowed((f'! true && git -C "{m}" reset --hard', f'! : && git -C "{m}" reset --hard'))

    def test_failed_move_is_read_only_for_the_next_and(self):
        # `;` の後ろは plain の `cd x; git ...` と同じ読み (移動は成功する)。実測で worktree に戻っている
        m, a = self.main, self.a
        self.assertAllowed((
            f'cd "{m}"; ! cd "{a}"; {GIT}',
            f'cd "{m}"; ! cd "{a}"\n{GIT}',
            f'cd "{m}"; if ! cd "{a}"; then exit 1; fi; {GIT}',
            f'cd "{m}"; ! pushd "{a}"; {GIT}',
            f'cd "{m}"; ! cd "{a}" || true; {GIT}',
            # サブシェルを伴う `!` は移動の読みを足さない
            f'! (cd "{m}") && {GIT}',
        ))
        # `&&` の先のサブシェルや続きの `&&` にも届く
        self.assertDenied((
            f'cd "{m}"; ! cd nowhere && ({GIT})',
            f'cd "{m}"; ! cd "{a}" && true && {GIT}',
            f'cd "{m}"; ! cd "{a}" && true; {GIT}',
        ))

    def test_bang_after_command_is_a_command_name(self):
        m = self.main
        # `command !` の `!` はコマンド名で、反転しない (どちらの shell でも見つからず失敗する)
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
    """`!` の反転と失敗の読みは、同じ行か改行か、括弧の有無で割れない。実行する状態が空になって後ろの git を黙って読まなくなることがない。"""

    FAILING = ("! cd /nonexistent", "! pushd /nonexistent", "! popd +9", "! cd +1")

    def test_newline_right_after_opening_paren(self):
        # 失敗の読みは括弧を実行した時点で普通の状態になる。括弧の直後の改行で状態が消えない
        m = self.main
        gm = f'git -C "{m}" reset --hard'
        for head in self.FAILING:
            self.assertDenied((
                f'{head} && (\n{gm}\n)',
                f'{head} && ( # c\n{gm}\n)',
                f'{head} && ( (\n{gm}\n) )',
                f'if {head} && (\n{gm}\n); then true; fi',
                f'{head} && ({gm})',
            ))
        self.assertDenied((f'cd "{m}"; ! cd /nonexistent && (\n{GIT}\n)', f'cd "{m}"; ! cd /nonexistent && ({GIT})'))

    def test_negated_paren_keeps_the_operator(self):
        # `!` だけの語をコマンドとして実行して失敗にすると、`&&` の括弧を実行する状態が無くなる
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
            f'false || if ( {gm} ); then true; fi',
            f'true && ! ( {gm} ); true',
        ))
        # 演算子は持ち越すので、実際に飛ばす形は飛ばしたまま
        self.assertAllowed((f'false && ! ( {gm} )', f'true || ! ( {gm} )', f'false && ! ! ( {gm} )'))

    def test_newline_after_and_or_continues_the_list(self):
        m, a = self.main, self.a
        self.assertDenied((
            f'cd "{m}"; ! cd /nonexistent &&\n{GIT}',
            f'cd "{m}"; ! cd /nonexistent && \n\n{GIT}',
            f'cd "{m}"; ! cd /nonexistent && # c\n{GIT}',
            f'cd "{m}"; ! cd /nonexistent && {GIT}',
            f'cd "{m}"; ! cd /nonexistent && \\\n{GIT}',
            f'cd "{m}" &&\n! cd /nonexistent &&\n{GIT}',
            f'pushd "{m}"; ! cd /nonexistent &&\n{GIT}',
            f'! false &&\ngit -C "{m}" reset --hard',
            f'false ||\ngit -C "{m}" reset --hard',
        ))
        # 演算子が改行で消えないので、同じ行と同じ形は同じ読み
        self.assertAllowed((
            f'false &&\ngit -C "{m}" reset --hard',
            f'true ||\ngit -C "{m}" reset --hard',
            f'cd "{m}"; ! cd "{a}" ||\n{GIT}',
            f'cd "{m}"; ! ! cd "{a}" &&\n{GIT}',
        ))
        # 普通の改行は従来どおり list を区切る (失敗の読みは残らない)
        self.assertAllowed((f'cd "{m}"; ! cd "{a}"\n{GIT}',))
        # `!` だけの行はコマンドが無いまま反転する。次の行は別の list
        self.assertDenied((f'true && !\ngit -C "{m}" reset --hard',))
        # `|` の後ろの改行は pipeline の続き (zsh の `command cd` は外部コマンドで、bash は要素をサブシェルで動かす)
        self.assertAllowed((f'printf a |\ncommand cd "{m}"; {GIT}',))


class ReservedBangTest(ChdirReadingBase):
    def test_bang_after_external_time_does_not_flip(self):
        # 外部の time の引数になる shell があるので、その shell の読みでは反転しない
        m = self.main
        gm = f'git -C "{m}" reset --hard'
        self.assertDenied((
            f'time -- ! false || {gm}',
            f'time time ! false || {gm}',
            f'time -p -- ! false || {gm}',
            f'time -p ! false || {gm}',
            f'cd "{m}"; time -- ! cd /nonexistent && {GIT}',
            # bash は `time -p` の後ろを今の shell で動かして反転し、zsh は外部の time が失敗する。どちらも git が走る
            f'time -p ! true || {gm}',
        ))
        self.assertAllowed((f'time ! false || {gm}', f'! time false || {gm}', f'! ! false && {gm}'))

    def test_bang_after_assignment_is_a_command_name(self):
        m, a = self.main, self.a
        gm = f'git -C "{m}" reset --hard'
        self.assertDenied((f'FOO=1 ! false || {gm}', f'cd "{m}"; FOO=1 ! cd "{a}"; {GIT}', f'cd "{m}"; FOO=1 ! cd "{a}" || {GIT}'))
        # 代入の前の `!` は予約語
        self.assertAllowed((f'! FOO=1 false || {gm}',))


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
        self.assertDenied((f'printf a | cd "{m}"; {GIT}', f'cd "{m}"; printf a | cd "{a}"; {GIT}'))
        self.assertDenied((f'printf a | pushd -q "{m}"; {GIT}', f'cd "{m}"; printf a | command cd "{a}"; {GIT}'))
        # zsh の `command cd` は外部コマンドで、bash は pipeline の要素をサブシェルで動かす
        self.assertAllowed((f'printf a | command cd "{m}"; {GIT}',))


if __name__ == "__main__":
    unittest.main()
