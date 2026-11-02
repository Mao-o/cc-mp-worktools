"""`builtin` を前に付けたコマンドの読み。

どの形を移動として読むかは、偽の git を置いて bash 3.2 と `zsh -f` で実測した結果に合わせている
(git がどのディレクトリで走ったかを見た)。
"""
from __future__ import annotations

import os
import unittest

import test_guard
from test_guard import bash, decision, evaluate


class BuiltinPrefixTest(test_guard.GuardBase):
    def assertDenied(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(evaluate(bash(cmd, self.a))), "deny")

    def assertAllowed(self, cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNone(evaluate(bash(cmd, self.a)))

    def test_builtin_cd_moves_the_shell(self):
        m = self.main
        self.assertDenied((
            f'builtin cd "{m}"; git reset --hard',
            f'builtin cd "{m}"; git stash',
            f'builtin cd "{m}"; git commit -m x',
            f'builtin cd "{m}" && git reset --hard',
            f'builtin cd "{m}"\ngit reset --hard',
            f'builtin cd -P "{m}"; git reset --hard',
            f'builtin cd -- "{m}"; git reset --hard',
            f'builtin cd -L -- "{m}"; git reset --hard',
            f'builtin cd "{m}" >/dev/null; git reset --hard',
            f'builtin cd "{m}" &>/dev/null; git reset --hard',
            f'builtin cd "{m}" 2>/dev/null; git reset --hard',
            f'builtin cd "{m}" >/dev/null 2>&1; git reset --hard',
            # 引用符や `\` で綴っても同じコマンド
            f'\\builtin cd "{m}"; git reset --hard',
            f'\'builtin\' cd "{m}"; git reset --hard',
            f'builtin \'cd\' "{m}"; git reset --hard',
            f'builtin builtin cd "{m}"; git reset --hard',
            # 前置き
            f'FOO=1 builtin cd "{m}"; git reset --hard',
            f'! builtin cd "{m}"; git reset --hard',
            f'if builtin cd "{m}"; then git reset --hard; fi',
            f'{{ builtin cd "{m}"; git reset --hard; }}',
            f'(builtin cd "{m}"; git reset --hard)',
            # `time` は今の shell で動かす
            f'time builtin cd "{m}"; git reset --hard',
            f'time FOO=1 builtin cd "{m}"; git reset --hard',
            f'! time builtin cd "{m}"; git reset --hard',
            # pipeline の最後の要素の cd は zsh では残る
            f'printf a | builtin cd "{m}" && git commit -m x',
            f'cat <<EOF && builtin cd "{m}" && git add . && git commit -m x\nbody\nEOF',
            f'git add . && builtin cd "{m}" && git commit -m x',
        ))

    def test_builtin_cd_back_to_own_worktree_is_allowed(self):
        m, a = self.main, self.a
        self.assertAllowed((
            f'builtin cd "{a}"; git reset --hard',
            f'cd "{m}"; builtin cd "{a}"; git reset --hard',
            f'cd "{m}"; time builtin cd "{a}"; git reset --hard',
            f'cd "{m}"; builtin cd "{a}" >/dev/null 2>&1; git reset --hard',
            f'builtin cd "{m}"; builtin cd "{a}"; git reset --hard',
            # 読み取りは止めない
            f'builtin cd "{m}"; git log -1',
            f'builtin cd "{m}"; git status',
            # サブシェルの中の cd は外に残らない
            f'(builtin cd "{m}"); git reset --hard',
            f'builtin cd "{m}" | cat; git reset --hard',
            f'builtin cd "{m}" & wait; git reset --hard',
        ))

    def test_builtin_in_a_child_process_does_not_move_the_shell(self):
        # 外部の builtin コマンド (macOS の /usr/bin/builtin) が子プロセスで走る。
        # ただし `time -p` は bash だけ今の shell で動く (zsh は `-p` をコマンドとして起動する)。読みが割れるので読まない
        m, a = self.main, self.a
        prefixes = ("env ", "env -- ", "env FOO=1 ", "nohup ", "sudo ", "sudo -u me ", "command ", "command -- ",
                    "exec ", "time -p ", "time time ", "time env ", "FOO=1 time ")
        self.assertDenied(tuple(f'cd "{m}"; {p}builtin cd "{a}"; git reset --hard' for p in prefixes))
        # bash は `command builtin cd` を今の shell で動かすが、Bash tool の zsh は動かさない。
        # `FOO=1 time` は逆で、zsh だけ今の shell で動かす (読まないので見逃す側。ここでは固定しない)
        self.assertAllowed(tuple(f'{p}builtin cd "{m}"; git reset --hard' for p in prefixes if p != "FOO=1 time "))

    def test_forms_a_shell_does_not_read_as_cd_keep_the_cwd(self):
        # bash と zsh で読みが割れる形や、どちらでも走らない形は移動として読まない
        m, a = self.main, self.a
        forms = ('builtin -- cd', 'builtin - cd', 'builtin -e cd', 'builtin --help cd', 'builtin command cd',
                 'builtin exec cd', 'builtin time cd', 'builtin env cd', 'builtin FOO=1 cd', 'builtin ! cd',
                 'builtin chdir', 'builtin')
        self.assertDenied(tuple(f'cd "{m}"; {f} "{a}"; git reset --hard' for f in forms))
        # `builtin - cd` / `builtin chdir` は zsh だけ移動する (読まないので見逃す側。ここでは固定しない)
        zsh_only = ("builtin - cd", "builtin chdir")
        self.assertAllowed(tuple(f'{f} "{m}"; git reset --hard' for f in forms if f not in zsh_only))

    def test_builtin_popd_is_not_read(self):
        # スタックが空なら popd は失敗し、main に残ったまま git が走る
        m = self.main
        self.assertDenied((
            f'cd "{m}"; builtin popd; git reset --hard',
            # スタックがあっても読まないので、ここは誤 deny を固定している。
            # `pushd m; pushd w; builtin popd` は見逃す (旧版も同じ)
            f'pushd "{m}"; builtin popd; git reset --hard',
        ))

    def test_builtin_pushd_is_not_read(self):
        # 対の `builtin popd` を読まないので、読むと戻り先を取り違える。どれも実測で main に残る
        m, a = self.main, self.a
        self.assertDenied((
            f'cd "{m}"; builtin pushd "{a}"; builtin popd; git reset --hard',
            f'cd "{m}" && builtin pushd "{a}" && builtin popd && git reset --hard',
            f'cd "{m}"; builtin pushd +1; git reset --hard',
            f'cd "{m}"; builtin pushd -0; git reset --hard',
            f'cd "{m}"; builtin pushd -n "{a}"; git reset --hard',
            # 次の 2 つは bash だけ main に残る (zsh は移動する)
            f'cd "{m}"; builtin pushd; git reset --hard',
            f'cd "{m}"; builtin pushd -q "{a}"; git reset --hard',
        ))
        self.assertAllowed((
            f'builtin pushd "{m}"; builtin popd; git reset --hard',
            f'builtin pushd -n "{m}"; git reset --hard',
            # 実際には main で走るが読まない (旧版も同じ)。見逃す側
            f'builtin pushd "{m}"; git reset --hard',
        ))

    def test_builtin_cd_that_does_not_reach_the_operand_is_not_read(self):
        # 移動しない形や行き先の分からない形を読むと、手前の `cd <main>` が消える。どれも実測で両シェル main に残る
        m, a = self.main, self.a
        parent = os.path.dirname(m)
        name = os.path.basename(m)
        self.assertDenied((
            f'cd "{m}"; builtin cd +1; git reset --hard',
            f'cd "{m}"; builtin cd -1; git reset --hard',
            f'cd "{m}"; builtin cd -n; git reset --hard',
            f'cd "{m}"; builtin cd -x "{a}"; git reset --hard',
            f'cd "{m}"; builtin cd -e "{a}"; git reset --hard',
            f'cd "{m}"; builtin cd -@ "{a}"; git reset --hard',
            # `-q` は bash だけ、語が 2 つの形は zsh だけ main に残る
            f'cd "{m}"; builtin cd -q "{a}"; git reset --hard',
            f'cd "{m}"; builtin cd "{a}" extra; git reset --hard',
            # shell が展開する語
            f'cd "{m}"; builtin cd "$PWD"; git reset --hard',
            f'cd "{m}"; builtin cd $(pwd); git reset --hard',
            f'cd "{m}"; builtin cd `pwd`; git reset --hard',
            f'cd "{m}"; builtin cd {parent}/{name[:-1]}*; git reset --hard',
            f'cd "{m}"; builtin cd {parent}/{name[:-1]}?; git reset --hard',
            f'cd "{m}"; builtin cd {parent}/[{name[0]}]{name[1:]}; git reset --hard',
            f'cd "{m}"; builtin cd {parent}/{{{name},x}}; git reset --hard',
            f'cd "{m}"; builtin cd ~+; git reset --hard',
            # 数字の語は redirect の fd と区別しないが、読んだ行き先は実際と同じ
            f'cd "{m}"; builtin cd 2 >/dev/null; git reset --hard',
            # これだけは実際には HOME へ移動する。redirect を行き先と読むので誤 deny (旧版も同じ deny)
            f'cd "{m}"; builtin cd >/dev/null; git reset --hard',
        ))
        self.assertAllowed((
            # `-e` はどちらのシェルでも失敗して移動しない
            f'builtin cd -e "{m}"; git reset --hard',
            # 行き先を読まないので見逃す側 (旧版も同じ)
            f'builtin cd {parent}/{name[:-1]}*; git reset --hard',
            f'builtin cd "{m}" extra; git reset --hard',
        ))

    def test_builtin_cd_home_is_read(self):
        # 引数なしと `~` は HOME へ移動する (`HOME=` の書き換えは追わない。`cd` と同じ読み)
        m = self.main
        self.assertAllowed((
            f'cd "{m}"; builtin cd; git reset --hard',
            f'cd "{m}"; builtin cd ~; git reset --hard',
            f'cd "{m}"; builtin cd --; git reset --hard',
        ))

    def test_builtin_changes_git_environment(self):
        m, a = self.main, self.a
        self.assertDenied((
            f'builtin export GIT_DIR="{m}/.git"; git reset --hard',
            f'GIT_DIR="{m}/.git"; builtin export GIT_DIR; git reset --hard',
            # option 付きと値の決まらない形は読まない。`unset -f` は関数を消すので GIT_DIR は残り、main で走る
            f'export GIT_DIR="{m}/.git"; builtin unset -f GIT_DIR; git reset --hard',
            f'export GIT_DIR="{m}/.git"; builtin export -x GIT_DIR="{a}/.git"; git reset --hard',
            f'export GIT_DIR="{m}/.git"; builtin export +x GIT_DIR="{a}/.git"; git reset --hard',
            f'KEEP="{m}/.git"; export GIT_DIR="{m}/.git"; builtin export GIT_DIR=$KEEP; git reset --hard',
        ))
        self.assertAllowed((
            f'export GIT_DIR="{m}/.git"; builtin unset GIT_DIR; git reset --hard',
            f'export GIT_DIR="{m}/.git"; builtin export GIT_DIR="{a}/.git"; git reset --hard',
            # `export -n` は export を外すので、読むと誤って止める
            f'GIT_DIR="{m}/.git"; builtin export -n GIT_DIR; git reset --hard',
        ))

    def test_builtin_true_and_false_decide_the_next_command(self):
        m = self.main
        self.assertAllowed((
            f'builtin true || git -C "{m}" reset --hard',
            f'builtin : || git -C "{m}" reset --hard',
            f'builtin false && git -C "{m}" reset --hard',
        ))
        self.assertDenied((
            f'builtin true && git -C "{m}" reset --hard',
            f'builtin : && git -C "{m}" reset --hard',
            f'builtin false || git -C "{m}" reset --hard',
        ))

    def test_builtin_git_does_not_run_git(self):
        # `builtin git` は組み込みが無いので走らない。後ろの wrapper も同じ
        m = self.main
        self.assertAllowed(tuple(
            f'builtin {w}git -C "{m}" reset --hard' for w in ("", "env ", "nohup ", "sudo ", "time ")
        ))


if __name__ == "__main__":
    unittest.main()
