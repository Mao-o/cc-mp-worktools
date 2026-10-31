"""kubectl の相対パスの kubeconfig を、コマンドが走るディレクトリで解決する (v0.28.0)。

kubectl は相対パスの `--kubeconfig` / `KUBECONFIG` を自分の作業ディレクトリから開く。0.27.0 までの
hook は検証の kubectl を hook プロセスの作業ディレクトリで起動していたので、
`cd sub && kubectl --kubeconfig k.yaml apply` を `sub/k.yaml` ではなく `./k.yaml` で照合することがあり、
deny の案内も相対パスのまま (起動ディレクトリで打つと別のファイルを指す) だった。

偽の kubectl は**実際のファイル**を読み書きする: 渡された作業ディレクトリ (無ければ hook
プロセスの作業ディレクトリ) を開き、そこからの相対で kubeconfig を開く (`dir_fd`。kernel が
`..` と symlink を解くのは本物の kubectl と同じ)。ファイルの中身は context 名だけ。
配置の 4 形 (移動先が symlink / 起動ディレクトリの親が symlink / hard link / 大文字小文字だけ
違う名前) を回し、同一性はパスの文字列ではなく inode (`os.path.samestat`) で比べ、案内を
実行した後に残すべきファイルの中身を読み直す。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess as _sp
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _testutil  # noqa: F401
from test_dispatcher import BaseWithTmpProject, setUpModule, tearDownModule  # noqa: F401

from core import workdir  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402
from services import kubectl  # noqa: E402

_REAL_RUN = _sp.run
_GUIDE = re.compile(r"kubectl config use-context ctx --kubeconfig \S+")


class _FakeKubectl:
    def __init__(self):
        self.calls: list[tuple[list, dict]] = []

    def _path(self, argv, kwargs):
        if "--kubeconfig" in argv:
            return argv[argv.index("--kubeconfig") + 1]
        env = kwargs.get("env") or os.environ
        value = env.get("KUBECONFIG") or ""
        parts = [p for p in value.split(os.pathsep) if p]
        if parts:
            return parts[0]
        return os.path.join(env.get("HOME", "/nonexistent"), ".kube", "config")

    def _read(self, path, cwd):
        dfd = os.open(cwd or os.getcwd(), os.O_RDONLY)
        try:
            fd = os.open(path, os.O_RDONLY, dir_fd=dfd)
        except FileNotFoundError:
            return None
        finally:
            os.close(dfd)
        with os.fdopen(fd, encoding="utf-8") as f:
            return f.read().strip() or None

    def __call__(self, argv, **kwargs):
        if argv[:3] != ["kubectl", "config", "current-context"]:
            return _REAL_RUN(argv, **kwargs)
        self.calls.append((list(argv), dict(kwargs)))
        cwd = kwargs.get("cwd")
        if cwd is not None and not os.path.isdir(cwd):
            raise FileNotFoundError(cwd)
        current = self._read(self._path(argv, kwargs), cwd)
        if current is None:
            return SimpleNamespace(stdout="", stderr="error", returncode=1)
        return SimpleNamespace(stdout=current + "\n", stderr="", returncode=0)


class KubectlWorkdirTests(BaseWithTmpProject):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("KUBECONFIG", None)
        self.sub = self.project_dir / "sub"
        self.sub.mkdir()
        self._write_accounts({"kubectl": "ctx"})
        self.fake = _FakeKubectl()
        self.launch = str(self.project_dir)

    # -- helpers -----------------------------------------------------------
    def _dispatch(self, command, cwd=None):
        with self.isolated_cache(), mock.patch("subprocess.run", side_effect=self.fake):
            return dispatch(command, cwd or self.launch)

    def _decision(self, result):
        if result is None:
            return "allow", ""
        out = result["hookSpecificOutput"]
        return out.get("permissionDecision", "allow"), (
            out.get("permissionDecisionReason") or out.get("additionalContext") or ""
        )

    def _assert_allow(self, command, cwd=None):
        try:
            result = self._dispatch(command, cwd)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        decision, reason = self._decision(result)
        self.assertEqual(decision, "allow", f"{command}\n{reason}")
        self.assertNotIn("不一致", reason)

    def _assert_deny(self, command, cwd=None) -> str:
        try:
            result = self._dispatch(command, cwd)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外: {e!r}")
        decision, reason = self._decision(result)
        self.assertEqual(decision, "deny", command)
        return reason

    def _guide_path(self, reason) -> tuple[str, str]:
        lines = _GUIDE.findall(reason)
        self.assertEqual(len(lines), 1, reason)
        tokens = shlex.split(lines[0], comments=True)
        path = tokens[tokens.index("--kubeconfig") + 1]
        self.assertTrue(os.path.isabs(path), lines[0])
        return lines[0], path

    def _run_guide(self, line, path, cwd):
        """案内行を加工せずに hook に通し (CLI を呼ばずに通る)、CLI の書込を再現する。"""
        with mock.patch("subprocess.run") as run:
            self.assertIsNone(dispatch(line, cwd), line)
        run.assert_not_called()
        with open(path, "w", encoding="utf-8") as f:  # symlink は辿る (kubectl と同じ)
            f.write("ctx\n")

    @staticmethod
    def _write(path: Path, ctx: str):
        path.write_text(ctx + "\n", encoding="utf-8")

    @staticmethod
    def _same(a, b) -> bool:
        return os.path.samestat(os.stat(a), os.stat(b))

    # -- DoD ---------------------------------------------------------------
    def test_cd_then_relative_kubeconfig_uses_the_file_in_the_moved_dir(self):
        self._write(self.project_dir / "k.yaml", "wrong")
        self._write(self.sub / "k.yaml", "ctx")
        for command in (
            "cd sub && kubectl --kubeconfig k.yaml apply -f x.yaml",
            "cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml",
            "cd sub && kubectl apply -f x.yaml --kubeconfig=k.yaml",
            "cd sub && kubectl apply -f x.yaml --kubeconfig ./k.yaml",
            "pushd sub && kubectl apply -f x.yaml --kubeconfig k.yaml",
            "cd sub; kubectl apply -f x.yaml --kubeconfig k.yaml",
        ):
            with self.subTest(command):
                self._assert_allow(command)

    def test_cd_then_relative_kubeconfig_mismatch_is_denied_even_if_root_matches(self):
        self._write(self.project_dir / "k.yaml", "ctx")
        self._write(self.sub / "k.yaml", "wrong")
        reason = self._assert_deny("cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml")
        self.assertIn("現在=wrong", reason)

    def test_bash_cwd_without_cd_is_the_base(self):
        """コマンドに cd が無くても、Bash の作業ディレクトリ (hook input の cwd) から解決する。"""
        self._write(self.project_dir / "k.yaml", "wrong")
        self._write(self.sub / "k.yaml", "ctx")
        self._assert_allow("kubectl apply -f x.yaml --kubeconfig k.yaml", cwd=str(self.sub))
        self._write(self.sub / "k.yaml", "wrong")
        self._write(self.project_dir / "k.yaml", "ctx")
        self._assert_deny("kubectl apply -f x.yaml --kubeconfig k.yaml", cwd=str(self.sub))

    def test_guide_is_absolute_and_round_trips_from_the_launch_dir(self):
        root_kc = self.project_dir / "k.yaml"
        self._write(root_kc, "keep")
        sub_kc = self.sub / "k.yaml"
        command = "cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml"
        for situation in ("mismatch", "unset"):
            with self.subTest(situation=situation):
                if situation == "mismatch":
                    self._write(sub_kc, "wrong")
                elif sub_kc.exists():
                    sub_kc.unlink()
                reason = self._assert_deny(command)
                line, path = self._guide_path(reason)
                if situation == "mismatch":
                    self.assertTrue(self._same(path, sub_kc), line)
                else:
                    self.assertEqual(os.path.realpath(path), os.path.realpath(sub_kc))
                # 起動ディレクトリで打つ (案内は絶対パスなので同じファイルを指す)
                self._run_guide(line, path, self.launch)
                self.assertEqual(root_kc.read_text(encoding="utf-8").strip(), "keep")
                self.assertEqual(sub_kc.read_text(encoding="utf-8").strip(), "ctx")
                self._assert_allow(command)

    def test_cache_does_not_carry_a_success_to_another_dir(self):
        a, b = self.project_dir / "a", self.project_dir / "b"
        a.mkdir()
        b.mkdir()
        self._write(a / "k.yaml", "ctx")
        self._write(b / "k.yaml", "wrong")
        with self.isolated_cache(), mock.patch("subprocess.run", side_effect=self.fake):
            self.assertIsNone(
                dispatch("cd a && kubectl apply -f x.yaml --kubeconfig k.yaml", self.launch)
            )
            # 同じ cache で成功が公開されていることを確かめてから (2 回目は CLI を呼ばない)
            n = len(self.fake.calls)
            self.assertIsNone(
                dispatch("cd a && kubectl apply -f x.yaml --kubeconfig k.yaml", self.launch)
            )
            self.assertEqual(len(self.fake.calls), n, "成功 cache が公開されていない")
            result = dispatch("cd b && kubectl apply -f x.yaml --kubeconfig k.yaml", self.launch)
        self.assertEqual(self._decision(result)[0], "deny")

    # -- 決められない形 ----------------------------------------------------
    def test_same_relative_command_in_two_dirs_is_unresolved(self):
        self._write(self.project_dir / "k.yaml", "ctx")
        self._write(self.sub / "k.yaml", "ctx")
        seq = "cd sub && kubectl {v} -f x.yaml --kubeconfig k.yaml; cd .. && kubectl {v} -f x.yaml --kubeconfig k.yaml"
        reason = self._assert_deny(seq.format(v="apply"))
        self.assertIn(kubectl.UNRESOLVED_CONTEXT, reason)
        self.assertEqual(self.fake.calls, [])
        # QUERY tier は警告だけ
        result = self._dispatch(seq.format(v="get"))
        decision, text = self._decision(result)
        self.assertEqual(decision, "allow")
        self.assertIn(
            kubectl.UNRESOLVED_CONTEXT + "\n(リモート read のみのコマンドなので実行は止めません)", text
        )
        # WRITE の deny には付けない
        self.assertNotIn("実行は止めません", reason)

    # 実際には走らない (後続に効かない) cd。hook の移動の追跡は括弧・条件・パイプを落とすので、
    # 移動先のファイルで照合すると誤 allow になる (root/k.yaml が kubectl の読むファイル)。
    _NOT_SEQUENTIAL = (
        "(cd sub; true) && kubectl apply -f x.yaml --kubeconfig k.yaml",
        "(cd sub && true); kubectl apply -f x.yaml --kubeconfig k.yaml",
        "(cd sub); kubectl apply -f x.yaml --kubeconfig k.yaml",
        "(cd sub && kubectl get pods); kubectl apply -f x.yaml --kubeconfig k.yaml",
        "false && cd sub; kubectl apply -f x.yaml --kubeconfig k.yaml",
        "true || cd sub; kubectl apply -f x.yaml --kubeconfig k.yaml",
        "if false; then cd sub; fi; kubectl apply -f x.yaml --kubeconfig k.yaml",
        "cd sub | cat; kubectl apply -f x.yaml --kubeconfig k.yaml",
        "(cd sub && KUBECONFIG=k.yaml true); KUBECONFIG=k.yaml kubectl apply -f x.yaml",
        "{ cd sub; } ; kubectl apply -f x.yaml --kubeconfig k.yaml",
        "for d in sub; do cd sub; done; kubectl apply -f x.yaml --kubeconfig k.yaml",
        "mkdir -p sub && cd sub; kubectl apply -f x.yaml --kubeconfig k.yaml",
        "false && cd sub\nkubectl apply -f x.yaml --kubeconfig k.yaml",
    )
    # 移動の追跡がそもそも cd を拾わない形 (起動側のファイルで正しく照合する)
    _NOT_TRACKED = (
        "x=$(cd sub && pwd); kubectl apply -f x.yaml --kubeconfig k.yaml",
        "x=`cd sub`; kubectl apply -f x.yaml --kubeconfig k.yaml",
        "f() { cd sub; }; kubectl apply -f x.yaml --kubeconfig k.yaml",
        "bash -c 'cd sub'; kubectl apply -f x.yaml --kubeconfig k.yaml",
    )

    def test_cd_that_may_not_run_is_unresolved(self):
        self._write(self.project_dir / "k.yaml", "wrong")
        self._write(self.sub / "k.yaml", "ctx")
        for command in self._NOT_SEQUENTIAL:
            with self.subTest(command):
                reason = self._assert_deny(command)
                self.assertIn(kubectl.UNRESOLVED_CONTEXT, reason)
        for command in self._NOT_TRACKED:
            with self.subTest(command):
                reason = self._assert_deny(command)
                self.assertTrue(
                    kubectl.UNRESOLVED_CONTEXT in reason or "現在=wrong" in reason, reason
                )
        # QUERY tier は警告だけ
        result = self._dispatch("(cd sub); kubectl get pods --kubeconfig k.yaml")
        decision, text = self._decision(result)
        self.assertEqual(decision, "allow")
        self.assertIn(kubectl.UNRESOLVED_CONTEXT, text)

    def test_cd_that_may_not_run_keeps_absolute_and_no_path_commands(self):
        """相対パスが無ければ、括弧や条件の中の cd があっても今までどおり照合する。"""
        kc = self.project_dir / "k.yaml"
        self._write(kc, "ctx")
        self._assert_allow(f"(cd sub); kubectl apply -f x.yaml --kubeconfig {kc}")
        with mock.patch.dict(os.environ, {"KUBECONFIG": str(kc)}):
            self._assert_allow("false && cd sub; kubectl apply -f x.yaml")
        self._write(kc, "wrong")
        reason = self._assert_deny(f"cd sub | cat; kubectl apply -f x.yaml --kubeconfig {kc}")
        self.assertIn("現在=wrong", reason)

    def test_sequential_cd_is_still_resolved(self):
        """`&&` / `;` だけで繋いだ cd、引用符の中の記号、リダイレクトの `&` は解決する。"""
        self._write(self.project_dir / "k.yaml", "wrong")
        self._write(self.sub / "k.yaml", "ctx")
        (self.project_dir / "a").mkdir()
        for command in (
            "cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml",
            "cd a; cd ../sub && kubectl apply -f x.yaml --kubeconfig k.yaml",
            "cd sub\nkubectl apply -f x.yaml --kubeconfig k.yaml",
            "cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml 2>&1",
            "cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml &>/dev/null",
            "cd sub && kubectl apply -f 'x(1)|{y}&`.yaml' --kubeconfig k.yaml",
            'cd sub && kubectl apply -f "if (x).yaml" --kubeconfig k.yaml',
            "cd sub && kubectl apply -f x\\(1\\).yaml --kubeconfig k.yaml",
            "mkdir -p sub && cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml",
            "true; cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml;",
            "cd sub && echo 'a; b' && kubectl apply -f x.yaml --kubeconfig k.yaml",
        ):
            with self.subTest(command):
                self._assert_allow(command)
        # 括弧があっても cd が無ければ、Bash の作業ディレクトリで解決する
        self._write(self.project_dir / "k.yaml", "ctx")
        self._assert_allow("(kubectl apply -f x.yaml --kubeconfig k.yaml)")

    def test_cd_physical_dotdot(self):
        """`cd -P lnk/..` は symlink を解いてから `..` を辿る (bash と同じ)。"""
        real = self.project_dir / "a" / "b" / "real"
        real.mkdir(parents=True)
        (self.project_dir / "lnk").symlink_to(real, target_is_directory=True)
        command = "cd -P lnk/.. && kubectl apply -f x.yaml --kubeconfig k.yaml"
        self._write(real.parent / "k.yaml", "wrong")  # kubectl が実際に読む
        self._write(self.project_dir / "k.yaml", "ctx")  # 論理パスの `..`
        self.assertIn("現在=wrong", self._assert_deny(command))
        self._write(real.parent / "k.yaml", "ctx")
        self._write(self.project_dir / "k.yaml", "wrong")
        self._assert_allow(command)
        # `-L` が後なら論理パス (後のものが勝つ)
        self._assert_deny("cd -P -L lnk/.. && kubectl apply -f x.yaml --kubeconfig k.yaml")

    def test_absolute_kubeconfig_in_two_dirs_is_not_unresolved(self):
        kc = self.project_dir / "k.yaml"
        self._write(kc, "ctx")
        cmd = f"kubectl apply -f x.yaml --kubeconfig {kc}"
        self._assert_allow(f"cd sub && {cmd}; cd .. && {cmd}")

    def test_resolver_exception_denies(self):
        self._write(self.project_dir / "k.yaml", "ctx")
        with mock.patch.object(kubectl, "resolve_context", side_effect=RuntimeError("x")):
            reason = self._assert_deny("kubectl apply -f x.yaml --kubeconfig k.yaml")
        self.assertIn(kubectl.UNRESOLVED_CONTEXT, reason)

    # -- KUBECONFIG ----------------------------------------------------------
    def test_relative_kubeconfig_env_runs_kubectl_in_the_moved_dir(self):
        self._write(self.project_dir / "k.yaml", "wrong")
        self._write(self.sub / "k.yaml", "ctx")
        abs_other = self.project_dir / "missing.yaml"
        cases = {
            "inline": ("cd sub && KUBECONFIG=k.yaml kubectl apply -f x.yaml", {}),
            "hook env": ("cd sub && kubectl apply -f x.yaml", {"KUBECONFIG": "k.yaml"}),
            "list with a relative element": (
                "cd sub && kubectl apply -f x.yaml",
                {"KUBECONFIG": f"k.yaml{os.pathsep}{abs_other}"},
            ),
        }
        for name, (command, env) in cases.items():
            with self.subTest(name), mock.patch.dict(os.environ, env):
                self.fake.calls.clear()
                self._assert_allow(command)
                self.assertEqual(
                    os.path.realpath(self.fake.calls[-1][1]["cwd"]),
                    os.path.realpath(self.sub),
                )

    def test_relative_kubeconfig_env_in_missing_dir_says_so(self):
        reason = self._assert_deny("cd nowhere && KUBECONFIG=k.yaml kubectl apply -f x.yaml")
        self.assertIn("コマンドが走るディレクトリが見つからない", reason)
        self.assertNotIn("kubectl コマンドが見つかりません", reason)

    def test_relative_kubeconfig_env_after_cd_is_not_guided_as_a_command(self):
        """cd の後の相対の KUBECONFIG: 切替を Bash の作業ディレクトリで打つと別のファイルに書く。"""
        keep = self.project_dir / "k.yaml"
        self._write(keep, "keep")
        self._write(self.sub / "k.yaml", "wrong")
        with mock.patch.dict(os.environ, {"KUBECONFIG": "k.yaml"}):
            reason = self._assert_deny("cd sub && kubectl apply -f x.yaml")
            self.assertNotIn("kubectl config use-context", reason)
            self.assertIn("相対パスの KUBECONFIG はコマンドが走るディレクトリ", reason)
            self.assertIn(os.path.realpath(self.sub), reason)
            (self.sub / "k.yaml").unlink()
            reason = self._assert_deny("cd sub && kubectl apply -f x.yaml")
            self.assertNotIn("kubectl config use-context", reason)
            self.assertIn("相対パスの KUBECONFIG はコマンドが走るディレクトリ", reason)
            # cd が無ければ (Bash の作業ディレクトリで走る) 従来どおりコマンドの形で案内する
            self._write(keep, "wrong")
            reason = self._assert_deny("kubectl apply -f x.yaml")
            self.assertIn("切り替え: kubectl config use-context ctx", reason)

    def test_cache_key_includes_the_dir_of_a_relative_kubeconfig_env(self):
        a, b = self.project_dir / "a", self.project_dir / "b"
        a.mkdir()
        b.mkdir()
        self._write(a / "k.yaml", "ctx")
        self._write(b / "k.yaml", "wrong")
        with mock.patch.dict(os.environ, {"KUBECONFIG": "k.yaml"}), self.isolated_cache(), \
                mock.patch("subprocess.run", side_effect=self.fake):
            self.assertIsNone(dispatch("cd a && kubectl apply -f x.yaml", self.launch))
            n = len(self.fake.calls)
            self.assertIsNone(dispatch("cd a && kubectl apply -f x.yaml", self.launch))
            self.assertEqual(len(self.fake.calls), n, "成功 cache が公開されていない")
            result = dispatch("cd b && kubectl apply -f x.yaml", self.launch)
        self.assertEqual(self._decision(result)[0], "deny")

    def test_guide_stays_relative_when_the_absolute_path_is_not_guidable(self):
        """Bash の作業ディレクトリの実パスに空白や非 ASCII があると絶対パスは案内に使えない。

        そのディレクトリで走るコマンドなら、元の相対パスのまま案内する (そこで打てば同じファイル)。
        案内の行は加工せずに shell の規則で分割し、hook に通してから CLI の書込を再現する。
        """
        keep = self.project_dir / "k.yaml"
        self._write(keep, "keep")
        for name in ("my dir", "日本語", "Mobile Documents"):
            for given in ("k.yaml", "./k.yaml"):
                with self.subTest(name=name, given=given):
                    d = self.project_dir / name
                    d.mkdir(exist_ok=True)
                    kc = d / "k.yaml"
                    self._write(kc, "wrong")
                    command = f"kubectl apply -f x.yaml --kubeconfig {given}"
                    reason = self._assert_deny(command, cwd=str(d))
                    lines = _GUIDE.findall(reason)
                    self.assertEqual(len(lines), 1, reason)
                    tokens = shlex.split(lines[0], comments=True)
                    path = tokens[tokens.index("--kubeconfig") + 1]
                    self.assertEqual(path, given)
                    with mock.patch("subprocess.run") as run:
                        self.assertIsNone(dispatch(lines[0], str(d)), lines[0])
                    run.assert_not_called()
                    with open(os.path.join(d, path), "w", encoding="utf-8") as f:
                        f.write("ctx\n")
                    self.assertEqual(keep.read_text(encoding="utf-8").strip(), "keep")
                    self._assert_allow(command, cwd=str(d))
        # 相対パス自体が許容形でなければ、相対でも案内しない
        d = self.project_dir / "my dir"
        self._write(d / "a b.yaml", "wrong")
        reason = self._assert_deny("kubectl apply -f x.yaml --kubeconfig 'a b.yaml'", cwd=str(d))
        self.assertEqual(_GUIDE.findall(reason), [])
        self.assertIn(kubectl._KUBECONFIG_BY_HAND, reason)
        # 絶対パスが許容形なら、Bash の作業ディレクトリで走るコマンドでも絶対パスで案内する
        plain = self.project_dir / "plain"
        plain.mkdir()
        self._write(plain / "k.yaml", "wrong")
        _line, path = self._guide_path(
            self._assert_deny("kubectl apply -f x.yaml --kubeconfig k.yaml", cwd=str(plain))
        )
        self.assertTrue(self._same(path, plain / "k.yaml"))
        # cd の後 (Bash の作業ディレクトリと違う) は相対では案内しない (起動側で打つと別のファイル)
        self._write(d / "k.yaml", "wrong")
        reason = self._assert_deny("cd 'my dir' && kubectl apply -f x.yaml --kubeconfig k.yaml")
        self.assertEqual(_GUIDE.findall(reason), [])
        self.assertIn(kubectl._KUBECONFIG_BY_HAND, reason)

    def test_no_relative_path_keeps_the_hook_cwd(self):
        """相対パスが無ければ cwd は渡さない (0.27.0 と同じ。cd の有無で照合を変えない)。"""
        kc = self.project_dir / "k.yaml"
        self._write(kc, "ctx")
        for command, env in (
            (f"cd sub && kubectl apply -f x.yaml --kubeconfig {kc}", {}),
            ("cd sub && kubectl apply -f x.yaml", {"KUBECONFIG": str(kc)}),
            # --kubeconfig があれば KUBECONFIG は読まれない (相対でも cwd は要らない)
            (f"cd sub && kubectl apply -f x.yaml --kubeconfig {kc}", {"KUBECONFIG": "k.yaml"}),
        ):
            with self.subTest(command, env=env), mock.patch.dict(os.environ, env):
                self.fake.calls.clear()
                self._assert_allow(command)
                argv, kwargs = self.fake.calls[-1]
                self.assertIsNone(kwargs.get("cwd"))
                if "--kubeconfig" in argv:
                    self.assertEqual(argv[argv.index("--kubeconfig") + 1], str(kc))

    # -- 配置の 4 形 ---------------------------------------------------------
    def test_moved_dir_is_a_symlink_dotdot_is_physical(self):
        """(1) 移動先が symlink: `..` は論理パスの親ではなく実体の親 (kubectl と同じ)。"""
        real = self.project_dir / "a" / "b" / "real"
        real.mkdir(parents=True)
        link = self.project_dir / "lnk"
        link.symlink_to(real, target_is_directory=True)
        physical_parent = real.parent / "k.yaml"
        logical_parent = self.project_dir / "k.yaml"
        command = "cd lnk && kubectl apply -f x.yaml --kubeconfig ../k.yaml"
        self._write(physical_parent, "ctx")
        self._write(logical_parent, "wrong")
        self._assert_allow(command)
        self._write(physical_parent, "wrong")
        self._write(logical_parent, "ctx")
        line, path = self._guide_path(self._assert_deny(command))
        self.assertTrue(self._same(path, physical_parent), line)
        self._run_guide(line, path, self.launch)
        self.assertEqual(logical_parent.read_text(encoding="utf-8").strip(), "ctx")
        self._assert_allow(command)

    def test_kubeconfig_itself_is_a_symlink(self):
        target = self.project_dir / "real.yaml"
        self._write(target, "wrong")
        (self.sub / "k.yaml").symlink_to(target)
        self._write(self.project_dir / "k.yaml", "ctx")
        command = "cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml"
        line, path = self._guide_path(self._assert_deny(command))
        self.assertTrue(self._same(path, target), line)
        self._run_guide(line, path, self.launch)
        self.assertEqual((self.project_dir / "k.yaml").read_text(encoding="utf-8").strip(), "ctx")
        self._assert_allow(command)

    def test_launch_dir_reached_through_a_symlinked_parent(self):
        """(2) 起動ディレクトリ (残すべき k.yaml がある側) を、親の symlink 越しに指す。"""
        alias_parent = Path(self.tmp) / "alias"
        alias_parent.symlink_to(Path(self.tmp), target_is_directory=True)
        alias = alias_parent / "project"
        keep = self.project_dir / "k.yaml"
        self._write(keep, "keep")
        sub_kc = self.sub / "k.yaml"
        self._write(sub_kc, "wrong")
        command = "cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml"
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(alias)}):
            line, path = self._guide_path(self._assert_deny(command, cwd=str(alias)))
            self.assertTrue(self._same(path, sub_kc), line)
            self.assertFalse(self._same(path, keep), line)
            self._run_guide(line, path, str(alias))
            self.assertEqual(keep.read_text(encoding="utf-8").strip(), "keep")
            self._assert_allow(command, cwd=str(alias))

    def test_hard_link(self):
        """(3) hard link: 移動先の k.yaml が別名のファイルと同じ inode。"""
        other = self.project_dir / "other.yaml"
        self._write(other, "wrong")
        os.link(other, self.sub / "k.yaml")
        keep = self.project_dir / "k.yaml"
        self._write(keep, "ctx")
        command = "cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml"
        line, path = self._guide_path(self._assert_deny(command))
        self.assertTrue(self._same(path, other), line)
        self._run_guide(line, path, self.launch)
        self.assertEqual(other.read_text(encoding="utf-8").strip(), "ctx")
        self.assertEqual(keep.read_text(encoding="utf-8").strip(), "ctx")
        self._assert_allow(command)

    def test_case_only_name_difference(self):
        """(4) 大文字小文字だけ違う名前 (区別しない FS でだけ意味を持つ)。"""
        (self.sub / "K.yaml").write_text("ctx\n", encoding="utf-8")
        if not (self.sub / "k.yaml").exists():
            self.skipTest("大文字小文字を区別する FS")
        self._write(self.project_dir / "k.yaml", "wrong")
        command = "cd sub && kubectl apply -f x.yaml --kubeconfig k.yaml"
        self._assert_allow(command)
        (self.sub / "K.yaml").write_text("wrong\n", encoding="utf-8")
        self._write(self.project_dir / "k.yaml", "ctx")
        line, path = self._guide_path(self._assert_deny(command))
        self.assertTrue(self._same(path, self.sub / "K.yaml"), line)


class ChdirIsSequentialTests(unittest.TestCase):
    def test_table(self):
        cases = [
            # cd が無ければ常に True
            ("no cd", "(kubectl get pods) | cat & if x; then y; fi", True),
            ("plain", "cd a && kubectl apply", True),
            ("semicolons", "cd a; cd b && kubectl apply", True),
            ("newline", "cd a\nkubectl apply", True),
            ("and chain", "mkdir a && cd a && kubectl apply", True),
            ("trailing semicolon", "cd a && kubectl apply;", True),
            ("conditional cd, trailing semicolon", "mkdir a && cd a && kubectl apply;", True),
            ("conditional cd, trailing newlines", "mkdir a && cd a && kubectl apply\n\n", True),
            ("redirect 2>&1", "cd a && kubectl apply 2>&1", True),
            ("redirect &>", "cd a && kubectl apply &>/dev/null", True),
            ("redirect >&", "cd a && kubectl apply >&/dev/null", True),
            ("quoted symbols", "cd a && kubectl apply -f '(|{}`&;'", True),
            ("double quoted", 'cd a && kubectl apply -f "x) if ;"', True),
            ("escaped", "cd a && kubectl apply -f x\\(1\\)\\|\\&", True),
            ("keyword as part of word", "cd a && kubectl apply -f iffy", True),
            ("pushd", "pushd a && kubectl apply", True),
            ("open paren", "(cd a); kubectl apply", False),
            ("subshell after", "cd a && (kubectl apply)", False),
            ("brace", "{ cd a; }; kubectl apply", False),
            ("close brace only", "cd a; kubectl apply }", False),
            ("pipe", "cd a | cat; kubectl apply", False),
            ("or", "true || cd a; kubectl apply", False),
            ("background", "cd a & kubectl apply", False),
            ("backtick", "cd a; x=`true`", False),
            ("if", "if true; then cd a; fi; kubectl apply", False),
            ("while", "while false; do cd a; done; kubectl apply", False),
            # 分解が `case x in x) cd a` を 1 セグメントにする = 移動の追跡も cd を拾わない
            ("case (cd not tracked)", "case x in x) cd a;; esac; kubectl apply", True),
            ("case with tracked cd", "case x in x) true;; esac; cd a; kubectl apply", False),
            ("conditional cd with inline env", "false && X=1 cd a; kubectl apply", False),
            ("until", "until true; do cd a; done", False),
            ("conditional cd then list", "false && cd a; kubectl apply", False),
            ("conditional cd then newline", "false && cd a\nkubectl apply", False),
            ("conditional popd then list", "false && popd; kubectl apply", False),
            ("popd plain", "popd; kubectl apply", True),
        ]
        for name, command, want in cases:
            with self.subTest(name):
                try:
                    got = workdir.chdir_is_sequential(command)
                except Exception as e:  # noqa: BLE001
                    self.fail(f"例外: {e!r}")
                self.assertEqual(got, want, command)


class ResolveContextUnitTests(unittest.TestCase):
    def test_table(self):
        real = os.path.realpath("/")
        cases = [
            ("absolute flag", {"kubeconfig": "/k"}, {"/x"}, {}, {"kubeconfig": "/k"}),
            ("empty flag", {"kubeconfig": ""}, {"/x", "/y"}, {}, {"kubeconfig": ""}),
            ("relative flag", {"kubeconfig": "k"}, {"/"}, {}, {"kubeconfig": os.path.join(real, "k")}),
            ("relative flag two dirs", {"kubeconfig": "k"}, {"/", "/tmp"}, {}, None),
            ("relative flag unknown dir", {"kubeconfig": "k"}, {None}, {}, None),
            ("no flag no env", {}, {"/", "/tmp"}, {}, {}),
            ("absolute env", {}, {"/", "/tmp"}, {"KUBECONFIG": "/a:/b"}, {}),
            # base が無い / 違う = cd の後とみなし、切替をコマンドの形で案内しない印を立てる
            ("relative env", {}, {"/"}, {"KUBECONFIG": "k"}, {"cwd": real, "cwd_moved": True}),
            ("relative env two dirs", {}, {"/", "/tmp"}, {"KUBECONFIG": "/a:k"}, None),
            ("flag wins over env", {"kubeconfig": "/k"}, {"/", "/tmp"}, {"KUBECONFIG": "k"}, {"kubeconfig": "/k"}),
            ("context kept", {"context": "c", "kubeconfig": "k"}, {"/"}, {}, {"context": "c", "kubeconfig": os.path.join(real, "k")}),
            # 先頭の `./` だけを剥がす (`..` は剥がさない。剥がすと空なら元のまま)
            ("dot slash", {"kubeconfig": "./k"}, {"/"}, {}, {"kubeconfig": os.path.join(real, "k")}),
            ("dot slash twice", {"kubeconfig": "././k"}, {"/"}, {}, {"kubeconfig": os.path.join(real, "k")}),
            ("dot double slash", {"kubeconfig": ".//k"}, {"/tmp"}, {}, {"kubeconfig": os.path.join(os.path.realpath("/tmp"), "k")}),
            ("dotdot kept", {"kubeconfig": "../k"}, {"/"}, {}, {"kubeconfig": os.path.join(real, "../k")}),
            ("dot slash only", {"kubeconfig": "./"}, {"/"}, {}, {"kubeconfig": os.path.join(real, "./")}),
            # base と同じディレクトリなら元の相対パスを案内用に残す / cwd_moved を立てない
            ("relative flag at base", {"kubeconfig": "./k"}, {"/"}, {}, {"kubeconfig": os.path.join(real, "k"), "kubeconfig_given": "./k"}, "/"),
            ("relative flag off base", {"kubeconfig": "k"}, {"/"}, {}, {"kubeconfig": os.path.join(real, "k")}, "/tmp"),
            ("relative env at base", {}, {"/"}, {"KUBECONFIG": "k"}, {"cwd": real}, "/"),
            ("relative env off base", {}, {"/"}, {"KUBECONFIG": "k"}, {"cwd": real, "cwd_moved": True}, "/tmp"),
            ("absolute flag at base", {"kubeconfig": "/k"}, {"/"}, {}, {"kubeconfig": "/k"}, "/"),
        ]
        for name, ctx, dirs, env, want, *base in cases:
            with self.subTest(name):
                try:
                    got = kubectl.resolve_context(ctx, dirs, env, base=(base or [None])[0])
                except Exception as e:  # noqa: BLE001
                    self.fail(f"例外: {e!r}")
                self.assertEqual(got, want)

    def test_does_not_mutate_input(self):
        ctx = {"kubeconfig": "k"}
        kubectl.resolve_context(ctx, {"/"}, {})
        self.assertEqual(ctx, {"kubeconfig": "k"})


if __name__ == "__main__":
    unittest.main()
