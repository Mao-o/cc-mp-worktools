#!/usr/bin/env python3
"""verify-plugin-release: `gh pr create` / `gh pr ready` の前に完了条件を検査する hook。

PreToolUse:Bash に登録する。対象コマンドでなければ何も出力せずに終わる。

判定:
- FAIL がある → PR 作成を止める (deny)。理由には FAIL / WARN の行だけを載せる
- ゲートを完了できない (時間切れ・内部エラー・設定の破損) → 同じく止める。
  hook 自体が時間切れになると Claude Code はコマンドをそのまま実行するため、
  hook 全体で 1 つの予算 (HOOK_BUDGET) を持ち、先に打ち切って止める判断を返す
- draft PR の作成 / VERIFY_PLUGIN_RELEASE_MODE=warn → 止めずに結果を伝える
- VERIFY_PLUGIN_RELEASE_MODE=off → 何もしない

手動実行: `python3 <plugin>/hooks/verify-plugin-release check [--base BRANCH] [--strict-validate] [PATH]`
(終了コード 0 = PASS / 1 = FAIL / 2 = ゲートを完了できなかった)
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import threading
from pathlib import Path

import config
import runner
import gate
import layout
from command import Invocation
from reading import read_command
from runner import HOOK_BUDGET, Deadline, GateTimeout, git, run

MODE_ENV = "VERIFY_PLUGIN_RELEASE_MODE"
_REASON_LIMIT = 1800
_TAG = "[verify-plugin-release]"
_SPLIT_ADVICE = "PR 操作が複数あれば別のコマンドに分け、それでも収まらなければ test_command で実行範囲を絞る"


def _mode() -> str:
    v = os.environ.get(MODE_ENV, "enforce").strip().lower()
    return v if v in ("enforce", "warn", "off") else "enforce"


def format_report(rep: gate.Report, verbose: bool) -> str:
    lines = [f"base: {rep.base or '?'} / branch: {rep.branch or '?'}"]
    lines += [f"NOTE: {n}" for n in rep.notes]
    for r in rep.results:
        if verbose or r.status in (gate.FAIL, gate.WARN):
            lines.append(f"{r.status:<4}  {r.check}: {r.detail}")
    return "\n".join(lines)


def _clip(text: str) -> str:
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 20] + "\n... (省略)"


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _clip(reason),
        }
    }


def _context(text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": _clip(text)}}


class RepoLookupError(Exception):
    pass


def _repo_root(cwd: Path, dl: Deadline) -> Path | None:
    """git repo の root。repo の外なら None、判定できなければ RepoLookupError。"""
    try:
        r = git(["rev-parse", "--show-toplevel"], cwd, dl, cap=5)
    except (OSError, GateTimeout) as e:
        raise RepoLookupError(f"git repo の判定に失敗した: {e}") from e
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip())
    if "not a git repository" in r.stderr.lower():
        return None
    raise RepoLookupError(f"git repo の判定に失敗した (exit {r.returncode}): {r.stderr.strip()[:200]}")


def _pr_refs(root: Path, target: str | None, dl: Deadline) -> tuple[str, str, str] | None:
    """`gh pr view` で (head branch, base branch, head commit) を得る。取れなければ None。"""
    args = ["gh", "pr", "view", "--json", "headRefName,baseRefName,headRefOid"]
    if target:
        args.insert(3, target)
    try:
        r = run(args, root, dl, cap=10)
        data = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and all(data.get(k) for k in ("headRefName", "baseRefName", "headRefOid")):
        return data["headRefName"], data["baseRefName"], data["headRefOid"]
    return None


def evaluate(command: str, cwd: str) -> dict | None:
    mode = _mode()
    if mode == "off":
        return None
    budget = Deadline(HOOK_BUDGET)
    # 1 つのコマンドに複数の PR 操作があれば全部を検査し、止めるものがあれば止める
    invocations, reason = read_command(command, budget)
    if reason:
        soft = mode == "warn"
        return _error(RuntimeError(f"{reason}。gh pr create / ready は単独のコマンドとして実行する"), soft)
    context = None
    for inv in invocations:
        result = _evaluate_one(inv, cwd, mode, budget)
        if result is None:
            continue
        if result["hookSpecificOutput"].get("permissionDecision") == "deny":
            return result
        context = context or result
    return context


# `gh pr ready <url>` の URL から HOST/OWNER/REPO を取り出す
_PR_URL = re.compile(r"^https?://([^/]+/[^/]+/[^/]+)/pull/\d+/?(?:[?#].*)?$", re.I)

# origin URL から (host, owner, repo) を取り出す。scp 形式 (git@host:owner/repo) と URL 形式の両方
_REMOTE_URL = re.compile(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$", re.I)
_REMOTE_SCP = re.compile(r"^(?:[^@/]+@)?([^/:]+):([^/]+)/([^/]+?)(?:\.git)?/?$")


def _origin(root: Path, dl: Deadline) -> tuple[str, str, str] | None:
    r = git(["remote", "get-url", "origin"], root, dl)
    return _parse_remote(r.stdout.strip()) if r.returncode == 0 else None


def _parse_remote(url: str) -> tuple[str, str, str] | None:
    m = _REMOTE_URL.match(url) or _REMOTE_SCP.match(url)
    return tuple(x.lower() for x in m.groups()) if m else None  # type: ignore[return-value]


def _other_default_remote(root: Path, dl: Deadline) -> str | None:
    """`gh repo set-default` が origin と別の repo を既定にしていれば、その remote 名を返す。

    gh はこの設定を `remote.<name>.gh-resolved` に保存する。
    """
    r = git(["config", "--get-regexp", r"^remote\..*\.gh-resolved$"], root, dl)
    origin = _origin(root, dl)
    for line in r.stdout.splitlines():
        key = line.split(None, 1)[0]
        name = key[len("remote.") : -len(".gh-resolved")]
        if name == "origin":
            continue
        u = git(["remote", "get-url", name], root, dl)
        if origin is None or _parse_remote(u.stdout.strip()) != origin:
            return name
    return None


def _same_repo(root: Path, repo: str, dl: Deadline) -> bool:
    """`--repo [HOST/]OWNER/REPO` が origin と同じ repo を指しているか。判定できなければ False。

    host を省いた形は gh と同じく GH_HOST (無ければ github.com) を host とみなす。
    """
    origin = _origin(root, dl)
    parts = [x.lower() for x in repo.strip().rstrip("/").split("/") if x]
    if origin is None or len(parts) not in (2, 3):
        return False
    host = parts[0] if len(parts) == 3 else os.environ.get("GH_HOST", "github.com").lower()
    return (host, parts[-2], parts[-1]) == origin


def _load_config(root: Path, dl: Deadline) -> tuple[config.Config, str | None]:
    """commit 済みの設定を読む。作業ツリーの設定は検査を弱められてしまうので使わない。"""
    rel = config.CONFIG_RELPATH.as_posix()
    r = git(["show", f"HEAD:{rel}"], root, dl)
    committed = r.stdout if r.returncode == 0 else None
    cfg = config.parse(committed) if committed is not None else config.Config()
    note = None
    local = root / config.CONFIG_RELPATH
    if local.is_file():
        try:
            differs = local.read_text(encoding="utf-8") != committed
        except OSError:
            differs = True
        if differs:
            note = f"{rel} の未 commit の内容は使っていない (commit 済みの設定で検査した)"
    return cfg, note


# Windows の Git Bash のドライブの綴り (`/c/Users/...` = `C:\Users\...`)
_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(?=/|$)")


def _native(path: str) -> str:
    """Git Bash の `/c/...` を Python が読む `C:/...` に読み替える (Windows だけ。POSIX の `/c` は `/c` のまま)。

    読み替えないと `C:\\c\\Users\\...` (hook の cwd のドライブの下の `c`) と読み、存在しないとして止めていた。
    """
    m = _MSYS_DRIVE.match(path) if os.path.sep == "\\" else None
    return f"{m.group(1).upper()}:/{path[m.end():].lstrip('/')}" if m else path


def _step(base: Path, step: str) -> Path:
    """cd を 1 つ進める。shell の cd と同じく論理パス (`..` は symlink を辿る前の親) で進める。"""
    return Path(os.path.normpath(base / os.path.expanduser(_native(step))))


# 走ったかもしれない cd の後ろで数え上げる cwd の数の上限 (超えたら数え上げずに止める)
_MAX_POSSIBLE_DIRS = 16


def _searches_cdpath(step: str) -> bool:
    """shell が CDPATH の下で探す引数か (`/` か `~` で始まらず、最初の成分が `.` / `..` でない相対の名前。`a` / `a/b` / `.x`)。

    bash も zsh も、CDPATH があると相対の名前を cwd より先に CDPATH の下で探すので、行き先の有無にかかわらず読み替わりうる。
    """
    native = _native(step)
    if native.startswith(("/", "~")) or native.split("/", 1)[0] in (".", ".."):
        return False
    return not (os.path.sep == "\\" and (native.startswith("\\") or re.match(r"[A-Za-z]:", native)))


def _fails_if_missing(step: str) -> bool:
    """行き先が無いとき、shell の cd が確かに失敗する (cwd が変わらない) か。

    `..` を含む引数は、論理パスの行き先が無いと bash が書かれたままの物理パスで入り直すので、失敗するとは限らない。
    Windows の `/tmp` のようなドライブの無い絶対パスは Git Bash が別の場所に読み替えるので、失敗するとは限らない。
    """
    native = _native(step)  # `/c/...` は `C:/...` になり、ここでのドライブの無い絶対パスに当たらない
    if ".." in re.split(r"[\\/]", native):
        return False
    return not (os.path.sep == "\\" and native.startswith(("/", "\\")))


def _possible_dirs(start: Path, history: tuple[str, ...]) -> list[Path] | None:
    """走ったかもしれない cd の後ろで、shell が居うる cwd を全部挙げる。挙げきれなければ None (止める)。

    ループと関数の外では、cd は出てきた順に高々 1 回走り、走らないか失敗すれば cwd は変わらない。なので hook の cwd に
    cd の引数の部分列を順に当てた先がすべての候補になる (括弧の中やパイプの左の cd も足すので、候補は多めになる)。
    hook の環境に CDPATH があると、相対の名前は CDPATH の下の dir にも入りうるので挙げない。
    """
    if os.environ.get("CDPATH") and any(_searches_cdpath(s) for s in history):
        return None
    dirs = [start]
    for step in history:
        for base in list(dirs):
            target = _step(base, step)
            if target.is_dir():
                if target not in dirs:
                    dirs.append(target)
            elif not _fails_if_missing(step):
                return None
        if len(dirs) > _MAX_POSSIBLE_DIRS:
            return None
    return dirs


def _may_be_plugin_repo(inv: Invocation, start: Path, budget: Deadline) -> bool:
    """確かでない cd の後ろの PR 操作が、plugin repo で走りうるか。確かめられなければ True (止める)。"""
    if inv.cd_history is None:
        return True
    try:
        dirs = _possible_dirs(start, inv.cd_history)
    except (OSError, RuntimeError, ValueError):
        return True
    if dirs is None:
        return True
    for d in dirs:
        try:
            root = _repo_root(d, budget.sub(10))
        except RepoLookupError:
            return True
        if root is not None and layout.is_plugin_repo(root):
            return True
    return False


def _evaluate_one(inv: Invocation, cwd: str, mode: str, budget: Deadline) -> dict | None:
    soft = mode == "warn" or inv.draft
    if budget.remaining() <= 0:
        return _error(GateTimeout(f"hook 全体の制限時間 ({HOOK_BUDGET} 秒) を使い切った。{_SPLIT_ADVICE}"), soft)
    workdir = Path(_native(cwd or os.getcwd()))
    if inv.cd_unresolved is not None or inv.cd:
        # `cd "$WT" && gh pr create` のように移動先が静的に決まらない / 存在しない場合、
        # 検査する repo が分からない。素通しにせず「完了できなかった」として扱う。
        unresolved = inv.cd_unresolved or (inv.cd if any(c in inv.cd for c in "$`") else None)
        if unresolved is not None:
            # 居うる cwd を数え上げられ、どれも plugin repo でなければ何もしない (plugin repo でない repo と repo の外で止めない)
            if not _may_be_plugin_repo(inv, workdir, budget):
                return None
            return _error(
                RuntimeError(
                    f"cd の移動先 ({unresolved}) を静的に解決できない (cd が走らないことがある形を含む)。"
                    "`cd <絶対パス> && gh ...` の形で実行する"
                ),
                soft,
            )
        # cd を 1 つずつ辿る。途中の移動先が無ければ止める (`cd 無い && cd ..` を `.` と読まないため)
        # 権限の無い dir・長すぎる名前・symlink の循環は is_dir / resolve が例外を投げる (Python 3.11〜3.13)。
        # hook が例外で終わるとコマンドがそのまま走るので、止める側に倒す
        try:
            for step in inv.cd_steps or (inv.cd,):
                workdir = _step(workdir, step)
                if not workdir.is_dir():
                    return _error(RuntimeError(f"cd の移動先 ({workdir}) が存在しない"), soft)
            workdir = workdir.resolve()
        except (OSError, RuntimeError) as e:
            return _error(RuntimeError(f"cd の移動先 ({workdir}) を確かめられない: {e}"), soft)

    probe = budget.sub(10)
    try:
        root = _repo_root(workdir, probe)
    except RepoLookupError as e:
        return _error(e, soft)
    if root is None:
        return None  # git repo の外。gh 自身のエラーに任せる
    if not layout.is_plugin_repo(root):
        return None

    shrunk = ""
    try:
        cfg, cfg_note = _load_config(root, budget.sub(10))
        left = budget.remaining()
        if cfg.timeout_seconds > left:
            shrunk = (
                f"。制限時間 ({cfg.timeout_seconds} 秒) を hook 全体の制限時間 ({HOOK_BUDGET} 秒) の残り {left:.0f} 秒に"
                f"縮めて検査した。{_SPLIT_ADVICE}"
            )
        dl = budget.sub(cfg.timeout_seconds)
        base_hint = inv.base
        # gh が PR を作る repo は --repo → GH_REPO → `gh repo set-default` → origin の順で決まる。
        # origin 以外を指していると、手元で検査した base と PR の base が一致しない
        selected = inv.repo or os.environ.get("GH_REPO") or None
        if selected and not _same_repo(root, selected, dl):
            raise RuntimeError(
                f"PR の作成先 ({selected}) が origin と同じ repo か確認できない。"
                "対象 repo の checkout で実行する"
            )
        if not selected:
            other = _other_default_remote(root, dl)
            if other:
                raise RuntimeError(
                    f"`gh repo set-default` の既定 ({other}) が origin と別の repo を指している。"
                    "--repo で origin を明示するか、既定を origin に戻す"
                )
        if inv.kind == "ready":
            url = _PR_URL.match(inv.target or "")
            if url and not _same_repo(root, url.group(1), dl):
                raise RuntimeError(f"対象 PR ({inv.target}) が origin と別の repo にある")
            refs = _pr_refs(root, inv.target, dl)
            if refs is None:
                # 引数なしの `gh pr ready` も「現在の branch の PR」を対象にするため、
                # base を知るには PR の参照が要る。取れなければ既定の base で代用せず止める
                what = f"gh pr view {inv.target}" if inv.target else "gh pr view"
                raise RuntimeError(f"`{what}` で PR の branch を取得できない")
            # 手元で検査した内容が PR の中身と同じであることを branch と commit の両方で確かめる。
            # 別 branch の PR や、push していない commit がある状態では検査結果が PR に当てはまらない
            head_now = git(["rev-parse", "--abbrev-ref", "HEAD"], root, dl).stdout.strip()
            if refs[0] != head_now:
                raise RuntimeError(
                    f"対象 PR の branch ({refs[0]}) が現在の checkout ({head_now}) と異なる。"
                    "その branch を checkout してから実行する"
                )
            sha_now = git(["rev-parse", "HEAD"], root, dl).stdout.strip()
            if refs[2] != sha_now:
                raise RuntimeError(
                    f"PR の head ({refs[2][:10]}) と手元の HEAD ({sha_now[:10]}) が異なる。"
                    "push していない commit があれば push してから実行する"
                )
            base_hint = refs[1]
        elif inv.head:
            # --head で別 branch を PR にする場合、手元の checkout は PR の中身と一致しない。
            # 検査対象を取り違えて通すより、その branch を checkout して実行させる
            if ":" in inv.head:
                # owner:branch は fork 側の branch。手元の同名 branch と中身が同じとは限らない
                raise RuntimeError(
                    f"--head ({inv.head}) は別 owner の branch を指しており、手元で検査できない"
                )
            head_now = git(["rev-parse", "--abbrev-ref", "HEAD"], root, dl).stdout.strip()
            want = inv.head
            if want != head_now:
                raise RuntimeError(
                    f"--head の branch ({want}) が現在の checkout ({head_now}) と異なる。"
                    "その branch を checkout してから実行する"
                )
            # --head を明示すると gh は push を省くので、PR はリモートの branch から作られる。
            # 手元の HEAD と一致しなければ、検査した内容と PR の中身が食い違う
            if cfg.fetch:
                git(["fetch", "--quiet", "origin", want], root, dl, cap=15)
            remote = git(["rev-parse", "--verify", "--quiet", f"origin/{want}^{{commit}}"], root, dl)
            sha_now = git(["rev-parse", "HEAD"], root, dl).stdout.strip()
            if remote.returncode != 0 or remote.stdout.strip() != sha_now:
                raise RuntimeError(
                    f"--head 指定時は push されない。origin/{want} が手元の HEAD と一致しないので、"
                    "push してから実行する"
                )
        rep = gate.run_gate(root, cfg, dl, base_hint=base_hint)
        if cfg_note:
            rep.notes.append(cfg_note)
    except GateTimeout as e:
        return _error(GateTimeout(f"{e}{shrunk}"), soft)
    except (config.ConfigError, OSError, RuntimeError) as e:
        return _error(e, soft)
    except Exception as e:  # noqa: BLE001 - 想定外の例外も「止める」側に倒す
        return _error(e, soft)

    body = format_report(rep, verbose=False)
    if rep.failed:
        if soft:
            why = "draft PR" if inv.draft else f"{MODE_ENV}=warn"
            return _context(f"{_TAG} 完了条件を満たしていない ({why} のため止めていない)\n{body}")
        return _deny(
            f"{_TAG} PR 前の完了条件を満たしていない。FAIL を解消してから再実行する。\n"
            f"{body}\n"
            f"(一時的に止めずに通すには {MODE_ENV}=warn)"
        )
    if any(r.status == gate.WARN for r in rep.results) or rep.notes:
        return _context(f"{_TAG} PASS (WARN あり)\n{body}")
    return None


def _error(e: BaseException, soft: bool) -> dict:
    msg = f"{_TAG} ゲートを完了できなかった: {type(e).__name__}: {e}"
    if soft:
        return _context(msg)
    return _deny(
        f"{msg}\n異常時は PR 作成を止める設定になっている。原因を解消するか、"
        f"一時的に {MODE_ENV}=warn で実行する。"
    )


def _manual(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="verify-plugin-release check")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--base", default=None)
    ap.add_argument(
        "--strict-validate",
        action="store_true",
        help="claude plugin validate の warning を FAIL にする (plugin directory checks の節は除く。"
        "設定ファイルの strict_validate より優先)",
    )
    args = ap.parse_args(argv)
    try:
        root = _repo_root(Path(args.path).resolve(), Deadline(10))
    except RepoLookupError as e:
        print(str(e), file=sys.stderr)
        return 2
    if root is None:
        print("git repo ではない", file=sys.stderr)
        return 2
    try:
        cfg, note = _load_config(root, Deadline(10))
        if args.strict_validate:
            cfg = dataclasses.replace(cfg, strict_validate=True)
        rep = gate.run_gate(root, cfg, Deadline(cfg.timeout_seconds), base_hint=args.base)
        if note:
            rep.notes.append(note)
    except Exception as e:  # noqa: BLE001
        print(f"ゲートを完了できなかった: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(format_report(rep, verbose=True))
    print("結果:", "FAIL" if rep.failed else "PASS")
    return 1 if rep.failed else 0


def _arm_last_resort() -> threading.Timer:
    """どの子プロセスの待ちで固まっても、hook の時間切れの前に止める判断を返す。

    Windows の subprocess.run は時間切れの後に孫が pipe を閉じるまで待つ。このスレッドはその待ちと無関係に動く。
    """

    def fire() -> None:
        msg = f"hook 全体の制限時間 ({HOOK_BUDGET} 秒) を超えても子プロセスの終わりを待っている。{_SPLIT_ADVICE}"
        json.dump(_error(GateTimeout(msg), _mode() == "warn"), sys.stdout)
        sys.stdout.flush()
        os._exit(0)

    timer = threading.Timer(runner.LAST_RESORT, fire)
    timer.daemon = True
    timer.start()
    return timer


def main() -> None:
    # Windows の既定 (cp1252 等) では日本語の入出力で例外になり、hook が JSON を
    # 返せないまま終わる = Claude Code はコマンドをそのまま実行する。入出力は
    # UTF-8 に固定する (stdin は bytes で読んで decode、stdout は reconfigure)。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        sys.exit(_manual(sys.argv[2:]))
    try:
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, OSError):
        return
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return
    command = (data.get("tool_input") or {}).get("command") or ""
    if not isinstance(command, str) or not command:
        return
    timer = _arm_last_resort()
    try:
        result = evaluate(command, data.get("cwd") or "")
    except Exception as e:  # noqa: BLE001 - ゲートを完了できない → 止める (例外で終わるとコマンドが走る)
        result = _error(e, _mode() == "warn")
    timer.cancel()
    if result is not None:
        json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
