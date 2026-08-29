from __future__ import annotations

import json
import os
from fnmatch import fnmatch
from itertools import islice
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


def read_text(path: Path, limit: int = 120_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return ""


def load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def safe_iterdir(path: Path) -> List[Path]:
    try:
        return sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []


# 1 ディレクトリあたり列挙する項目数の上限 (stat 回数の上限でもある)。
MAX_NESTED_SCAN_ENTRIES = 512


def has_nested_project_markers(
    root: Path,
    markers: Iterable[str],
    skip_dirs: Iterable[str] = (),
    max_depth: int = 2,
    max_dirs: int = 64,
) -> bool:
    """True when any of ``markers`` exists in a bounded subtree under ``root``.

    ルート直下にマニフェストを置かないワークスペース (例: ``web/`` と ``api/``
    にそれぞれのマニフェストがあり、ルートには何も無い構成) を「非プロジェクト」
    と誤判定すると、構造・依存・スタック・テストの収集が丸ごと走らず facts が
    消える。gate の目的は「無関係なディレクトリの高コストな全走査を避けること」
    なので、探索は深さと訪問ディレクトリ数の両方で必ず打ち切る。

    ``max_dirs`` に達したら False を返す (「見つからなかった」ではなく
    「これ以上は見ない」の意味だが、gate としては同じ扱いでよい -- 走査を
    避けたい相手はまさに巨大なディレクトリなので)。
    """
    markers = list(markers)
    skip = set(skip_dirs)
    queue = [(root, 0)]
    visited = 0
    while queue:
        current, depth = queue.pop(0)
        if depth > 0:
            visited += 1
            if has_project_markers(current, markers):
                return True
        if depth >= max_depth or visited >= max_dirs:
            continue
        # ディレクトリ全体を列挙・ソートしてから打ち切ると、子が数十万ある
        # ディレクトリでメモリと時間を食い、この関数が主張する上限が意味を
        # 失う (hook の実行時間上限を超えうる)。残り予算ぶんだけ取り出して
        # 打ち切る。そのため cap に達したときの結果は列挙順に依存するが、
        # 走査を避けたい相手はまさに巨大なディレクトリなので許容する。
        remaining = max_dirs - visited - len(queue)
        if remaining <= 0:
            continue
        try:
            # `os.scandir()` を直接使う。`Path.iterdir()` は CPython では
            # 内部で全エントリをスナップショットしてから yield するため、
            # 何件目で打ち切っても「巨大ディレクトリを丸ごと読む」コストは
            # 避けられない (hook の実行時間上限を超えうる)。scandir は
            # ストリーミングで、しかも DirEntry が種別をキャッシュするので
            # 判定側の stat 呼び出しも減る。
            #
            # 打ち切りは「候補ディレクトリの件数」ではなく「列挙した項目数」に
            # かける。候補で数えると、候補が 1 つも無い巨大ディレクトリで
            # 全件を見てしまうため。代償として、大量のファイルの後ろに埋もれた
            # サブプロジェクトは見つからないことがある (走査を強制する
            # オプションが escape hatch)。
            with os.scandir(current) as it:
                for entry in islice(it, MAX_NESTED_SCAN_ENTRIES):
                    if _is_candidate_dir(entry, skip):
                        queue.append((Path(entry.path), depth + 1))
                        if len(queue) + visited >= max_dirs:
                            break
        except OSError:
            continue
    return False


def _is_candidate_dir(entry: "os.DirEntry[str]", skip: set) -> bool:
    """入れ子マーカー探索で降りてよいディレクトリか。

    ``.git`` を持つディレクトリは独立した repo であり、その存在は親を
    「プロジェクト」にしない。``walk_files(..., respect_subgit=True)`` も
    同じ境界で刈るので、判定をそちらに揃える (揃えないと、clone を 1 つ
    置いただけのディレクトリが gate を通り、しかも walk 側は clone を刈る
    ため無関係な兄弟だけを拾った誤解を招く facts が出る)。
    """
    name = entry.name
    if name.startswith(".") or name in skip:
        return False
    try:
        # DirEntry のキャッシュを使う (follow_symlinks=False で追加の stat を
        # 避ける)。`.git` の有無だけは実際に見る必要がある。
        if not entry.is_dir(follow_symlinks=False):
            return False
        return not (Path(entry.path) / ".git").exists()
    except OSError:
        return False


def scan_project_markers(root: Path, markers: Iterable[str]) -> Tuple[bool, bool]:
    """``(マーカーが見つかったか, 走査を完了できたか)`` を返す。

    literal なマーカーは ``exists()`` で直接見るので列挙は起きない。glob
    マーカーだけは列挙が要るが、``root.glob()`` を 1 パターンずつ回すと
    マッチしないときに毎回ルートを全列挙する (パターン数ぶんの全列挙)。
    マーカー無しの巨大なフラットディレクトリ -- まさにこの gate が抑止したい
    対象 -- で hook の実行時間上限を食い潰しうるので、列挙は 1 回・件数上限
    つきにし、その 1 パスで全パターンを突き合わせる。

    上限で打ち切った場合は 2 つ目の戻り値が ``False`` になる。**打ち切りは
    「マーカーが無い」の根拠にならない** ので、呼び出し側はそれを区別する
    こと (区別せずに「無い」と扱うと、ルートのファイル数が多い実在の
    プロジェクトで facts が丸ごと消える)。
    """
    globs = []
    for marker in markers:
        if "*" in marker or "?" in marker:
            globs.append(marker)
        elif (root / marker).exists():
            return True, True
    if not globs:
        return False, True
    try:
        with os.scandir(root) as it:
            seen = 0
            for entry in islice(it, MAX_NESTED_SCAN_ENTRIES):
                seen += 1
                if any(fnmatch(entry.name, pattern) for pattern in globs):
                    return True, True
    except OSError:
        return False, True
    # ちょうど上限件数だったときは「まだ続きがあるかもしれない」と扱う
    # (安全側: 打ち切り扱いにすると gate は skip せず後続の走査に委ねる)。
    return False, seen < MAX_NESTED_SCAN_ENTRIES


def has_project_markers(root: Path, markers: Iterable[str]) -> bool:
    """``scan_project_markers()`` の真偽値だけを見る簡易版。

    打ち切りと「本当に無い」を区別しないので、**その区別が要る gate では
    使わないこと** (``scan_project_markers()`` を直接使う)。
    """
    return scan_project_markers(root, markers)[0]


def walk_files(
    root: Path,
    skip_dirs: Iterable[str],
    limit: int = 5000,
    respect_subgit: bool = True,
) -> List[str]:
    skip = set(skip_dirs)
    results: List[str] = []
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        if respect_subgit and dirpath != root_str and ".git" in dirnames:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root_str)
            results.append(rel)
            if len(results) >= limit:
                return results
    return results
