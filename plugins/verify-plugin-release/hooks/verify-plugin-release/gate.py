"""PR 前の完了条件チェック本体。

検査は「PR に載る内容 = base...HEAD の commit」を対象にする。テストと
`claude plugin validate` だけは作業ツリーで走らせる (commit 済みの状態を
取り出して実行するより速く、未 commit の変更があれば別途 WARN で知らせる)。

各検査は Result を返すだけで、PR を止めるかどうかは __main__ が決める。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import layout
from config import Config
from runner import Deadline, GateTimeout, git, run

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

_FETCH_CAP = 15
_VALIDATE_CAP = 60
_SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv"}
# ゲート自身がテストを走らせるので、bytecode を作らせない (作ると次の検査で
# 「未 commit の変更」に見える。.gitignore に __pycache__ が無い repo で起きる)
_TEST_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
_MAX_TEST_WORKERS = 8


@dataclass(frozen=True)
class Result:
    check: str
    status: str
    detail: str


@dataclass
class Report:
    base: str = ""
    branch: str = ""
    notes: list[str] = field(default_factory=list)
    results: list[Result] = field(default_factory=list)

    def add(self, check: str, status: str, detail: str) -> None:
        self.results.append(Result(check, status, detail))

    @property
    def failed(self) -> bool:
        return any(r.status == FAIL for r in self.results)


def _is_bytecode(path: str) -> bool:
    return "__pycache__/" in path or path.rstrip("/").endswith(("__pycache__", ".pyc"))


def _lines(text: str) -> list[str]:
    return [x for x in text.split("\0") if x] if "\0" in text else [x for x in text.splitlines() if x]


def _short(items: list[str], n: int = 5) -> str:
    head = ", ".join(items[:n])
    return head + (f" ほか {len(items) - n} 件" if len(items) > n else "")


# --- base の解決 -------------------------------------------------------------


def _ref_exists(root: Path, ref: str, dl: Deadline) -> bool:
    return git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], root, dl).returncode == 0


def _default_branch(root: Path, dl: Deadline) -> str | None:
    r = git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root, dl)
    if r.returncode == 0 and r.stdout.strip().startswith("origin/"):
        return r.stdout.strip()[len("origin/") :]
    for name in ("main", "master"):
        if _ref_exists(root, "origin/" + name, dl) or _ref_exists(root, name, dl):
            return name
    return None


def _configured_base(root: Path, dl: Deadline) -> str | None:
    """`gh pr create` が --base 省略時に最初に使う `branch.<current>.gh-merge-base`。"""
    cur = git(["rev-parse", "--abbrev-ref", "HEAD"], root, dl).stdout.strip()
    if not cur or cur == "HEAD":
        return None
    r = git(["config", "--get", f"branch.{cur}.gh-merge-base"], root, dl)
    return r.stdout.strip() or None if r.returncode == 0 else None


def resolve_base(root: Path, hint: str | None, cfg: Config, dl: Deadline, rep: Report) -> str | None:
    branch = hint or _configured_base(root, dl) or _default_branch(root, dl)
    if not branch:
        return None
    has_origin = git(["remote", "get-url", "origin"], root, dl).returncode == 0
    if cfg.fetch and has_origin:
        r = git(["fetch", "--quiet", "origin", branch], root, dl, cap=_FETCH_CAP)
        if r.returncode != 0:
            rep.notes.append(f"origin/{branch} の fetch に失敗したため手元の ref で検査した")
    for ref in (f"origin/{branch}", branch):
        if _ref_exists(root, ref, dl):
            return ref
    return None


# --- 個別の検査 -------------------------------------------------------------


def _read_version(root: Path, rev: str, rel_manifest: str, dl: Deadline) -> tuple[bool, str | None]:
    """(manifest が存在するか, version) を返す。"""
    r = git(["show", f"{rev}:{rel_manifest}"], root, dl)
    if r.returncode != 0:
        return False, None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return True, None
    v = data.get("version") if isinstance(data, dict) else None
    return True, v if isinstance(v, str) and v else None


_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def _semver_key(v: str) -> tuple | None:
    m = _SEMVER.match(v.strip())
    if not m:
        return None
    core = tuple(int(x) for x in m.group(1, 2, 3))
    pre = m.group(4)
    if pre is None:
        return (core, 1, ())  # 正式版は同じ番号の pre-release より新しい
    ids = tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre.split("."))
    return (core, 0, ids)


def _compare_versions(old: str, new: str) -> int | None:
    """old < new なら負、等しければ 0、下がっていれば正。semver でなければ None。"""
    a, b = _semver_key(old), _semver_key(new)
    if a is None or b is None:
        return None
    return (a > b) - (a < b)


def _check_version(root: Path, base: str, plugin: layout.Plugin, dl: Deadline, rep: Report) -> None:
    rel = f"{plugin.dir}/{layout.PLUGIN_MANIFEST}" if plugin.dir else layout.PLUGIN_MANIFEST
    old_exists, old = _read_version(root, base, rel, dl)
    new_exists, new = _read_version(root, "HEAD", rel, dl)
    name = f"version[{plugin.name}]"
    if not new_exists:
        rep.add(name, FAIL, f"{rel} が HEAD に無い")
    elif new is None and old is None:
        rep.add(name, SKIP, "version 未設定 (commit SHA で配布される)")
    elif new is None:
        rep.add(name, FAIL, f"version が削除された (base: {old})")
    elif not old_exists:
        rep.add(name, PASS, f"新規 plugin ({new})")
    elif new == old:
        rep.add(name, FAIL, f"version が据え置き ({old})。bump しないと既存ユーザーに更新が届かない")
    elif old is None:
        rep.add(name, PASS, f"(未設定) -> {new}")
    else:
        order = _compare_versions(old, new)
        if order is None:
            rep.add(name, WARN, f"{old} -> {new} (semver として比較できないため上がったか確認していない)")
        elif order < 0:
            rep.add(name, PASS, f"{old} -> {new}")
        else:
            rep.add(name, FAIL, f"version が下がっている ({old} -> {new})")


def _check_changelog(root: Path, plugin: layout.Plugin, changed: list[str], dl: Deadline, rep: Report) -> None:
    rel = f"{plugin.dir}/CHANGELOG.md" if plugin.dir else "CHANGELOG.md"
    name = f"changelog[{plugin.name}]"
    if git(["cat-file", "-e", f"HEAD:{rel}"], root, dl).returncode != 0:
        rep.add(name, SKIP, "CHANGELOG.md が無い")
    elif rel in changed:
        rep.add(name, PASS, "更新あり")
    else:
        rep.add(name, FAIL, f"{rel} が未更新")


def _test_dirs(plugin_path: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(plugin_path):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        if Path(dirpath).name == "tests" and any(
            f.startswith("test") and f.endswith(".py") for f in filenames
        ):
            out.append(Path(dirpath))
            dirnames[:] = []
    return out


@dataclass
class _TestPlan:
    """1 plugin 分のテスト実行計画。skip があれば実行しない。"""

    plugin: layout.Plugin
    skip: str | None = None
    jobs: list[tuple[list[str], Path]] = field(default_factory=list)  # (コマンド, 実行ディレクトリ)
    custom: bool = False  # test_command で指定されたコマンドか


def _plan_tests(root: Path, plugin: layout.Plugin, cfg: Config) -> _TestPlan:
    ppath = root / plugin.dir if plugin.dir else root
    if cfg.test_command is False:
        return _TestPlan(plugin, skip="設定で無効化 (test_command: false)")
    if isinstance(cfg.test_command, list):
        return _TestPlan(plugin, jobs=[(cfg.test_command, ppath)], custom=True)
    dirs = _test_dirs(ppath)
    if not dirs:
        return _TestPlan(plugin, skip="Python の tests/ を検出できない (test_command で指定できる)")
    cmd = [sys.executable, "-m", "unittest", "discover", "tests"]
    return _TestPlan(plugin, jobs=[(cmd, d.parent) for d in dirs])


_POLL = 0.5


def _run_job(cmd: list[str], cwd: Path, dl: Deadline, stop: threading.Event) -> int:
    """1 suite を実行して exit code を返す。stop が立つか制限時間を過ぎたら kill する。"""
    if stop.is_set():
        return -1
    if dl.remaining() <= 0:
        raise GateTimeout(f"制限時間切れ ({cmd[0]} の実行前)")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, **_TEST_ENV},
    )
    while True:
        try:
            return proc.wait(timeout=_POLL)
        except subprocess.TimeoutExpired:
            if stop.is_set() or dl.remaining() <= 0:
                proc.kill()
                proc.wait()
                if stop.is_set():
                    return -1
                raise GateTimeout(f"制限時間切れ ({' '.join(cmd[:3])})") from None


def _run_tests(root: Path, plans: list[_TestPlan], dl: Deadline, rep: Report) -> None:
    """全 plugin の test suite を並列に走らせ、plugin ごとの結果を順に記録する。

    複数の plugin にまたがる PR では、suite を直列に走らせると合計時間が制限時間を
    超える (実測: 5 plugin の PR で 90 秒超)。suite 同士は独立しているので並列にし、
    所要時間を「最も遅い suite」程度に抑える。
    """
    jobs = [(i, cmd, cwd) for i, plan in enumerate(plans) if not plan.skip for cmd, cwd in plan.jobs]
    exit_codes: dict[tuple[int, Path], int] = {}
    if jobs:
        workers = max(1, min(len(jobs), _MAX_TEST_WORKERS, os.cpu_count() or 1))
        stop = threading.Event()
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {pool.submit(_run_job, cmd, cwd, dl, stop): (i, cwd) for i, cmd, cwd in jobs}
            for fut in as_completed(futures):
                exit_codes[futures[fut]] = fut.result()  # GateTimeout / OSError はここで上がる
        except BaseException:
            # 1 本が失敗したら残りを待たずに止める。待つと hook 自体の timeout を超え、
            # Claude Code が PR 作成をそのまま通してしまう (止める判断を返せない)
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
    for i, plan in enumerate(plans):
        name = f"tests[{plan.plugin.name}]"
        if plan.skip:
            rep.add(name, SKIP, plan.skip)
        elif plan.custom:
            cmd, cwd = plan.jobs[0]
            code = exit_codes[(i, cwd)]
            label = " ".join(cmd)
            rep.add(name, PASS if code == 0 else FAIL, f"`{label}` が成功" if code == 0 else f"`{label}` が exit {code}")
        else:
            failed = [cwd.relative_to(root).as_posix() for _, cwd in plan.jobs if exit_codes[(i, cwd)] != 0]
            if failed:
                rep.add(name, FAIL, f"落ちた suite: {_short(failed)}")
            else:
                rep.add(name, PASS, f"{len(plan.jobs)} suite green")


def _check_validate(
    root: Path, target: str, label: str, cfg: Config, dl: Deadline, rep: Report, claude: str | None
) -> None:
    name = f"validate[{label}]"
    if not claude:
        rep.add(name, SKIP, "claude コマンドが PATH に無い")
        return
    r = run([claude, "plugin", "validate", target or "."], root, dl, cap=_VALIDATE_CAP)
    out = (r.stdout + "\n" + r.stderr).strip()
    last = out.splitlines()[-1] if out else ""
    if r.returncode != 0:
        rep.add(name, FAIL, last or f"exit {r.returncode}")
        return
    warn_lines = [ln.strip() for ln in out.splitlines() if "warning" in ln.lower()]
    if warn_lines:
        rep.add(name, FAIL if cfg.strict_validate else WARN, warn_lines[0])
    else:
        rep.add(name, PASS, "warning なしで passed")


def _check_workflows(root: Path, changed: list[str], rep: Report) -> None:
    wf = [p for p in changed if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))]
    wf = [p for p in wf if (root / p).is_file()]
    if not wf:
        return
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        rep.add("workflow-yaml", SKIP, "PyYAML が無いため検査しない")
        return
    broken = []
    for p in wf:
        try:
            with open(root / p, encoding="utf-8") as f:
                yaml.safe_load(f)
        except Exception:  # noqa: BLE001 - YAML の構文エラーは型が多い
            broken.append(p)
    if broken:
        rep.add("workflow-yaml", FAIL, f"YAML として読めない: {_short(broken)}")
    else:
        rep.add("workflow-yaml", PASS, f"{len(wf)} file(s) が YAML として妥当")


def _check_merge(root: Path, base: str, dl: Deadline, rep: Report) -> None:
    r = git(["merge-tree", "--write-tree", base, "HEAD"], root, dl)
    if r.returncode == 0:
        rep.add("merge", PASS, f"{base} と競合なし")
    elif r.returncode == 1:
        rep.add("merge", FAIL, f"{base} と競合する。rebase してから PR にする")
    else:
        rep.add("merge", SKIP, "git merge-tree --write-tree が使えない (git 2.38 以上が必要)")


# --- 本体 -------------------------------------------------------------------


def run_gate(
    root: Path,
    cfg: Config,
    dl: Deadline,
    base_hint: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Report:
    rep = Report()
    base = resolve_base(root, base_hint, cfg, dl, rep)
    if base is None:
        rep.add("base", FAIL, f"base branch を解決できない ({base_hint or 'origin/HEAD, main, master'})")
        return rep
    rep.base = base

    head = git(["rev-parse", "--abbrev-ref", "HEAD"], root, dl).stdout.strip()
    rep.branch = head
    base_name = base.split("/", 1)[1] if base.startswith("origin/") else base
    if head == "HEAD":
        rep.add("branch", FAIL, "detached HEAD になっている")
    elif head == base_name:
        rep.add("branch", FAIL, f"base と同じ {head} にいる (作業ブランチを切っていない)")
    else:
        rep.add("branch", PASS, head)

    changed = _lines(git(["diff", "--name-only", "-z", f"{base}...HEAD"], root, dl).stdout)
    if not changed:
        rep.add("diff", FAIL, f"{base} との差分が無い (commit していない可能性)")
        return rep
    rep.add("diff", PASS, f"{len(changed)} files")

    plugins = layout.discover(root)
    touched: dict[str, layout.Plugin] = {}
    per_plugin: dict[str, list[str]] = {}
    root_files: list[str] = []
    for path in changed:
        p = layout.owner(path, plugins)
        if p is None:
            root_files.append(path)
        else:
            touched[p.dir] = p
            per_plugin.setdefault(p.dir, []).append(path)

    # テストと validate は作業ツリーで走らせるため、変更対象の plugin に未 commit の変更が
    # あると「PR に載る commit」ではなく手元の状態を検査したことになる。その場合は止める
    dirty = [
        path
        for ln in _lines(git(["status", "--porcelain"], root, dl).stdout)
        if not _is_bytecode(path := ln[3:])
    ]
    dirty_in = [f for f in dirty if (p := layout.owner(f, plugins)) is not None and p.dir in touched]
    dirty_out = [f for f in dirty if f not in dirty_in]
    if dirty_in:
        rep.add(
            "uncommitted",
            FAIL,
            f"検査対象の plugin に未 commit の変更がある (commit か stash してから): {_short(dirty_in)}",
        )
    if dirty_out:
        rep.add("uncommitted-other", WARN, f"PR に載らない未 commit の変更がある: {_short(dirty_out)}")

    if cfg.single_plugin_per_pr:
        units = [f"plugin:{p.name}" for p in touched.values()] + (["repo root"] if root_files else [])
        if len(units) > 1:
            rep.add("single-plugin", FAIL, f"1 PR に複数の範囲が混ざっている: {_short(units)}")
        else:
            rep.add("single-plugin", PASS, units[0])

    claude = which("claude")
    new_plugin = False
    test_plans: list[_TestPlan] = []
    removed_plugin = False
    for pdir in sorted(touched):
        plugin = touched[pdir]
        files = per_plugin[pdir]
        rel_manifest = f"{pdir}/{layout.PLUGIN_MANIFEST}" if pdir else layout.PLUGIN_MANIFEST
        if git(["cat-file", "-e", f"HEAD:{rel_manifest}"], root, dl).returncode != 0:
            # plugin を削除した PR。個別の検査は無意味なので、marketplace 側の整合だけを見る
            removed_plugin = True
            if plugin.in_marketplace:
                rep.add(f"removed[{plugin.name}]", FAIL, f"{pdir} を削除したが marketplace.json に entry が残っている")
            else:
                rep.add(f"removed[{plugin.name}]", PASS, "plugin の削除 (marketplace 未登録)")
            continue
        if all(layout.is_doc_only(f, plugin) for f in files):
            rep.add(f"release[{plugin.name}]", SKIP, "ドキュメントのみの変更のため version / CHANGELOG は不問")
        else:
            _check_version(root, base, plugin, dl, rep)
            _check_changelog(root, plugin, files, dl, rep)
        if git(["cat-file", "-e", f"{base}:{rel_manifest}"], root, dl).returncode != 0:
            new_plugin = True
            if (root / layout.MARKETPLACE).is_file() and not plugin.in_marketplace:
                rep.add(f"listed[{plugin.name}]", WARN, "marketplace.json に entry が無い")
        test_plans.append(_plan_tests(root, plugin, cfg))
        _check_validate(root, pdir, plugin.name, cfg, dl, rep, claude)

    _run_tests(root, test_plans, dl, rep)
    if (root / layout.MARKETPLACE).is_file() and (layout.MARKETPLACE in root_files or new_plugin or removed_plugin):
        _check_validate(root, "", "marketplace", cfg, dl, rep, claude)
    _check_workflows(root, root_files, rep)
    _check_merge(root, base, dl, rep)
    return rep
