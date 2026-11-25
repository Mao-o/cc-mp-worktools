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


def project_cli(sd: str, root: str) -> str | None:
    """登録した root の明示の設定 (bd / br / None=auto)。entry が無い・読めない値は None。"""
    e = _read(os.path.join(sd, "projects.json")).get("projects", {})
    e = e.get(root) if isinstance(e, dict) else None
    return cli_setting(e.get("cli") if isinstance(e, dict) else None)[0]


def live_projects(sd: str) -> list[str]:
    """登録済みで、まだ .beads/ があるもの (名前順)。消えたものは表示しないだけで、登録は残す。"""
    projects = _read(os.path.join(sd, "projects.json")).get("projects", {})
    live = [p for p in projects if isinstance(p, str) and os.path.isdir(os.path.join(p, ".beads"))]
    return sorted(live, key=lambda p: (Path(p).name.lower(), p))


def write_server_info(sd: str, info: dict) -> None:
    with _locked(sd):
        _write(os.path.join(sd, "server.json"), info)


def read_server_info(sd: str) -> dict:
    return _read(os.path.join(sd, "server.json"))
