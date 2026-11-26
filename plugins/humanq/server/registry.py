"""humanq の状態ファイル (登録したプロジェクト・動いているサーバー)。hook とサーバーの両方が使う。

複数のセッションが同時に登録しても壊れないように、書き込みは flock の下で一時ファイル → rename。
"""
from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import json
import os
import tempfile
import time
from pathlib import Path

DEFAULT_PORT = 8765
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def state_dir() -> str:
    d = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(Path.home(), ".claude", "humanq")
    os.makedirs(d, exist_ok=True)
    return d


def plugin_version() -> str:
    try:
        return json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "0"


def find_bd_root(cwd: str) -> str | None:
    """cwd から上へ .beads/ のある dir を探す。HOME と / では止める (HOME 直下の .beads は対象外)。"""
    home = os.path.realpath(Path.home())
    p = Path(os.path.realpath(cwd))
    while True:
        if str(p) == home or p.parent == p:
            return None
        if (p / ".beads").is_dir():
            return str(p)
        p = p.parent


CLI_ENV = "HUMANQ_BEADS_CLI"
BD, BR = "bd", "br"          # humanq.py は CLI の名前を直接書かず、これを使う (tests/test_backend.py の層の検査)
KINDS = (BD, BR, "unknown", "conflict")


def cli_setting(raw) -> tuple[str | None, bool]:
    """HUMANQ_BEADS_CLI (または projects.json の cli) の値を (指定, 正しいか) にする。
    指定は "bd" / "br" / None (auto)。未設定・空・auto は (None, True)。前後の空白と大文字小文字は無視する。
    それ以外の値は auto とみなし (None, False) (警告を出すかどうかは呼ぶ側が決める)。"""
    if raw is None:
        return None, True
    v = str(raw).strip().lower()
    if v in ("", "auto"):
        return None, True
    if v in ("bd", "br"):
        return v, True
    return None, False


def _br_marks(beads: str) -> list[str]:
    """br の印 (どれか 1 つで br)。.beads/.gitignore は tracked なので fresh clone にも残る。
    .gitignore はコメント行を読まない (印の語を説明に書いただけの bd のプロジェクトを br と読まないため)。"""
    marks = []
    try:
        with open(os.path.join(beads, ".gitignore"), encoding="utf-8", errors="replace") as f:
            for line in f.read().splitlines():
                t = line.strip()
                if t and not t.startswith("#") and (".br_history" in t or "fsqlite" in t):
                    marks.append(f".gitignore の行 {t}")
                    break
    except OSError:
        pass
    if os.path.isdir(os.path.join(beads, ".br_history")):
        marks.append(".br_history/ がある")
    try:
        names = sorted(os.listdir(beads))
    except OSError:
        names = []
    for pat in ("*-fsqlite-ns-*", ".br-db-*.lock"):
        hit = [n for n in names if fnmatch.fnmatchcase(n, pat)]
        if hit:
            marks.append(f"{hit[0]} がある")
    return marks


def _metadata(beads: str) -> dict:
    try:
        with open(os.path.join(beads, "metadata.json"), encoding="utf-8") as f:
            v = json.load(f)
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _bd_marks(beads: str, meta: dict) -> list[str]:
    marks = []
    if meta.get("database") == "dolt" or meta.get("backend") == "dolt":
        marks.append("metadata.json が dolt")
    if os.path.isdir(os.path.join(beads, "embeddeddolt")):
        marks.append("embeddeddolt/ がある")
    return marks


def project_kind(root: str, cli=None) -> tuple[str, str]:
    """root の .beads/ が bd か br かを、ファイルの印だけで決める (CLI は打たない)。(kind, reason) を返す。
    kind は bd / br / unknown / conflict。cli は明示の設定 (cli_setting が読める値。読めなければ auto)。

    手順: 明示が bd / br なら、それに反する印があれば conflict、無ければ指定どおり。明示が無ければ、
    br の印と bd の印の両方で conflict、br の印だけで br、bd の印だけで bd。どちらの印も無く metadata.json の
    database が .db で終わるなら unknown (旧 bd の SQLite の形とも br の形とも決められない)。それ以外は bd。
    issues.jsonl の有無は使わない (bd の形でも持つものが多い)。
    br を bd の .beads に向けて打つと、エラーを出さずに空の一覧を返し、.beads/ に dolt のファイルを作る。
    br は、br と確定した dir にしか打たないこと。"""
    explicit, _ = cli_setting(cli)
    beads = os.path.join(root, ".beads")
    meta = _metadata(beads)
    br, bd = _br_marks(beads), _bd_marks(beads, meta)
    if explicit == "br":
        if bd:
            return "conflict", f"{CLI_ENV}=br だが bd の印がある ({'、'.join(bd)})"
        return "br", f"{CLI_ENV}=br の指定"
    if explicit == "bd":
        if br:
            return "conflict", f"{CLI_ENV}=bd だが br の印がある ({'、'.join(br)})"
        return "bd", f"{CLI_ENV}=bd の指定"
    if br and bd:
        return "conflict", f"br の印 ({'、'.join(br)}) と bd の印 ({'、'.join(bd)}) の両方がある"
    if br:
        return "br", f"br の印: {'、'.join(br)}"
    if bd:
        return "bd", f"bd の印: {'、'.join(bd)}"
    db = meta.get("database")
    if isinstance(db, str) and db.endswith(".db"):
        return "unknown", f"metadata.json の database が {db} で、bd の SQLite の形とも br の形とも決められない"
    return "bd", "印が無い (bd として読む)"


@contextlib.contextmanager
def _locked(sd: str):
    with open(os.path.join(sd, ".lock"), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def register(sd: str, root: str, cli: str | None = None) -> None:
    """root を登録する。cli は明示の設定 (bd / br / None=auto。cli_setting が読めない値は None)。entry は毎回丸ごと書き換える。"""
    path = os.path.join(sd, "projects.json")
    with _locked(sd):
        data = _read(path)
        projects = data.setdefault("projects", {})
        projects[root] = {"last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "cli": cli_setting(cli)[0]}
        _write(path, data)


def _entries(sd: str) -> dict:
    projects = _read(os.path.join(sd, "projects.json")).get("projects", {})
    return projects if isinstance(projects, dict) else {}


def _entry_cli(e) -> str | None:
    return cli_setting(e.get("cli") if isinstance(e, dict) else None)[0]


def project_cli(sd: str, root: str, groups: list[dict] | None = None) -> str | None:
    """root の明示の設定 (bd / br / None=auto)。root が登録の組 (project_groups) の代表か一員なら、その組の設定。
    組に無ければ root の entry の設定。entry が無い・読めない値は None。groups は読み済みの project_groups (省略は読む)。"""
    g = _group_of(root, project_groups(sd) if groups is None else groups)
    return g["cli"] if g else _entry_cli(_entries(sd).get(root))


# ---- 同じ beads の DB に届く登録をまとめる -----------------------------------------------------------------
# git worktree で開いたセッションは worktree の dir を登録するが、br はその worktree の .beads/ に DB が無ければ、本体の
# worktree の .beads/ を読み書きする (下の _br_home)。symlink の .beads/ や大文字小文字だけ違う登録のパスも、同じ DB に届く。
# 同じ DB に届く登録を 1 つのプロジェクト (組) として出さないと、画面に同じ問いが 2 枚出て、2 枚から同時に答えると
# 書き込みのロックがパスごとに別々になり、回答のコメントが 2 件になる。
# 登録のファイル (projects.json) の形は変えない: まとめるのは読むときだけ (前の版が書いた登録もそのまま読む)。

def _git_file_dir(root: str) -> str | None:
    """root/.git が `gitdir: <path>` のファイル (linked worktree・submodule) なら、その path (相対は root から)。
    それ以外 (dir・無い・読めない・形が違う) は None。git は打たない (外側の GIT_DIR などの env に左右されない)。"""
    dot = os.path.join(root, ".git")
    if not os.path.isfile(dot):
        return None
    try:
        with open(dot, encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError:
        return None
    g = first[len("gitdir:"):].strip() if first.startswith("gitdir:") else ""
    if not g or "\0" in g:                 # NUL の入った壊れたファイルは読まない (os の呼び出しが ValueError を上げる)
        return None
    return os.path.normpath(os.path.join(root, g))


def _linked_common_dir(gitdir: str) -> str | None:
    """linked worktree の gitdir (<common>/worktrees/<name>) の common dir。gitdir の commondir のファイルが読めればそれ、
    読めなければ dir の形 (親が worktrees) から決める (worktree の管理 dir を消した後も br は本体を読む: br 0.7.6 で実測)。
    linked worktree の形でなければ (submodule の modules/<name> など) None。"""
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8", errors="replace") as f:
            c = f.readline().strip()
        if c and "\0" not in c:            # NUL の入った commondir は読まず、下の dir の形から決める
            return os.path.normpath(os.path.join(gitdir, c))
    except OSError:
        pass
    parent = os.path.dirname(gitdir)
    return os.path.dirname(parent) if os.path.basename(parent) == "worktrees" else None


def git_main_worktree(root: str) -> str | None:
    """root が git の linked worktree の最上位 (root/.git が gitdir のファイル) なら、本体の worktree の dir (realpath)。
    本体そのもの・git の外・submodule・名前が .git でない bare の repo の worktree は None (`<dir>/.git` に置いた bare の repo は
    `<dir>` を返す。br 0.7.6 も `<dir>/.beads` を使う)。"""
    g = _git_file_dir(root)
    common = _linked_common_dir(g) if g else None
    if not common or os.path.basename(common) != ".git":
        return None
    return os.path.realpath(os.path.dirname(common))


def git_common_dir(root: str) -> str | None:
    """root/.git の common dir (realpath)。本体 (root/.git が dir) はその dir、linked worktree は本体の .git。
    root/.git が無い・読めない・linked worktree の形でなければ None。書き込みのロックの key に使う。"""
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return os.path.realpath(dot)
    g = _git_file_dir(root)
    common = _linked_common_dir(g) if g else None
    return os.path.realpath(common) if common else None


def _br_db_names(beads: str) -> set[str]:
    """br の DB のファイルの候補 (.beads/ からの相対): metadata.json の database と、br の既定の beads.db。
    どちらかがあれば「worktree が自分の DB を持つ」とみなす (多めに見るほど、まとめない側に倒れる)。"""
    db = _metadata(beads).get("database")
    return {"beads.db", db} if isinstance(db, str) and db else {"beads.db"}


def _br_home(root: str, cli, entries: dict | None = None) -> str:
    """br を root で打ったときに読み書きする .beads/ の持ち主の dir。root か、root が linked worktree なら本体の worktree。
    cli は root の entry の明示の設定。entries (登録の全件) に本体があれば、本体は本体の entry の設定でも判定し、
    どちらかで br と読めなければまとめない (組の設定は最後に登録した一員のものなので、どちらの設定でも本体を br と読めること)。
    br 0.7.6 の実測 (使い捨ての repo で `br where` と一覧の中身を比べた):
    - worktree の最上位の .beads/ に DB (metadata.json の database か beads.db) が無いと、本体の worktree の .beads/ を使う。
      本体の .beads/ が dir なら中身を問わない (空でも、そこに DB を作る)。本体に .beads/ が無ければ worktree の .beads/ に作る
    - worktree の .beads/ に DB があれば、それを使う (本体の DB とは別の DB)。DB 以外のファイル (.br_history/ など) では決まらない
    - worktree の下の dir の .beads/ (monorepo の sub/.beads) は、本体の同じ dir ではなく、その .beads/ に DB を作る
    - 本体の .beads/ が .beads (か _beads) という名前の dir に解決しない symlink だと、worktree では設定エラーで止まる
    どれかが外れたら root のまま (別のプロジェクトとして出す。同じ DB を別に出すのは、別の DB を 1 つにまとめて
    片方の判断待ちを画面から消すより害が小さい)。bd の worktree の解決は測っていない (bd にも worktree ごとに .beads/ を
    選ぶ仕組みがあり、br と同じとは限らない) ので、bd と読むプロジェクトはまとめない (書き込みのロックだけを git の repo
    ごとに取る: git_common_dir)。"""
    main = git_main_worktree(root)
    if main is None:
        return root
    beads, mbeads = os.path.join(root, ".beads"), os.path.join(main, ".beads")
    clis = [cli]
    if entries is not None and main in entries:
        clis.append(_entry_cli(entries[main]))
    if project_kind(root, cli)[0] != BR or any(project_kind(main, c)[0] != BR for c in clis):
        return root
    if os.path.exists(os.path.join(beads, "redirect")):         # br は redirect に従う (行き先はここでは追わない)
        return root
    if any(os.path.exists(os.path.join(beads, n)) for n in _br_db_names(beads)):
        return root
    if not os.path.isdir(mbeads) or os.path.basename(os.path.realpath(mbeads)) not in (".beads", "_beads"):
        return root
    return main


def _identity(home: str):
    """home/.beads の実体 (device と inode)。symlink の先・大文字小文字だけ違うパスも同じ値になる。stat できなければパスの文字列。"""
    try:
        st = os.stat(os.path.join(home, ".beads"))
    except OSError:
        return ("path", home)
    return ("inode", st.st_dev, st.st_ino)


def project_groups(sd: str) -> list[dict]:
    """登録済みで .beads/ があるものを、同じ beads の DB に届くもの (同じ実体の .beads/) ごとにまとめた組 (名前順)。
    組は {root, members, cli}:
    - root は組の代表 (画面のパス・CLI を打つ dir)。br の worktree は本体の worktree (登録が無くても)。.beads/ が symlink で
      ないものを先に、パスの名前順で 1 つ
    - members は組に入った登録のパス (名前順)。hook・画面は一員のパスで問い合わせても、代表のパスで読み書きする
    - cli は組の明示の設定。最後に登録した (last_seen が最新の) 一員の設定 (1 つの登録で、最後の登録の設定が効くのと同じ)
    消えたもの (.beads/ が無い) は出さないだけで、登録は残す。1 件の判定で os の呼び出しが例外 (OSError・ValueError) を
    上げたら、その登録はまとめずに自分のパスのまま出す (ほかの登録の読みは続ける)。"""
    entries = _entries(sd)
    groups: dict = {}
    for p, e in entries.items():
        if not isinstance(p, str) or not os.path.isdir(os.path.join(p, ".beads")):
            continue
        try:
            home = _br_home(p, _entry_cli(e), entries)
        except (OSError, ValueError):       # 1 件の読みの失敗 (壊れた .git など) で全体を止めない。その登録は自分のパスのまま出す
            home = p
        groups.setdefault(_identity(home), []).append((p, home))
    def seen(p: str) -> tuple[str, str]:            # 同じ秒の登録はパスの名前順の後ろ
        e = entries[p]
        return (str((e if isinstance(e, dict) else {}).get("last_seen") or ""), p)

    out = []
    for members in groups.values():
        homes = sorted({h for _, h in members}, key=lambda h: (os.path.islink(os.path.join(h, ".beads")), h))
        last = max((p for p, _ in members), key=seen)
        out.append({"root": homes[0], "members": sorted(p for p, _ in members), "cli": _entry_cli(entries[last])})
    return sorted(out, key=lambda g: (Path(g["root"]).name.lower(), g["root"]))


def live_projects(sd: str) -> list[str]:
    """画面に出すプロジェクト (project_groups の代表のパス。名前順)。消えたものは表示しないだけで、登録は残す。"""
    return [g["root"] for g in project_groups(sd)]


def _group_of(path, groups: list[dict]) -> dict | None:
    for g in groups:
        if path == g["root"] or path in g["members"]:
            return g
    return None


def resolve_project(sd: str, path, groups: list[dict] | None = None) -> str | None:
    """path (登録のパスか組の代表のパス) を、読み書きに使う代表のパスにする。どの組にも無ければ None (未登録。文字列でない値も)。
    groups は読み済みの project_groups (省略は読む)。"""
    g = _group_of(path, project_groups(sd) if groups is None else groups)
    return g["root"] if g else None


def write_server_info(sd: str, info: dict) -> None:
    with _locked(sd):
        _write(os.path.join(sd, "server.json"), info)


def read_server_info(sd: str) -> dict:
    return _read(os.path.join(sd, "server.json"))
