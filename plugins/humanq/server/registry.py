"""humanq の状態ファイル (登録したプロジェクト・動いているサーバー)。hook とサーバーの両方が使う。

複数のセッションが同時に登録しても壊れないように、書き込みは flock の下で一時ファイル → rename。
"""
from __future__ import annotations

import contextlib
import fcntl
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


def register(sd: str, root: str) -> None:
    path = os.path.join(sd, "projects.json")
    with _locked(sd):
        data = _read(path)
        projects = data.setdefault("projects", {})
        projects[root] = {"last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _write(path, data)


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
