"""偽の CLI (tests/fake_beads.py) を、テストの bin dir に bd・br などの名前で置く道具 (discover の対象ではない)。

偽物は呼び出しのたびに python を 1 つ起動する (suite 全体で数千回)。振る舞いは変えずに、起動を軽くする:
- 実体はプロセスごとに 1 つだけ作る (一時 dir に fake_beads.py の写し・その場で compile した pyc・それを読む起動の
  script)。bin dir には起動の script の hard link を置く。macOS は新しく書いた実行ファイルの最初の exec に 0.1 秒ほど
  かけるので、テストごとに写しを書くと、テストごとにその待ちが乗る。hard link は同じファイルなので、待つのは最初の 1 回
  だけ。hard link を張れない (別のファイルシステムなど) ときは写しを置く
- 起動の script は pyc を読む (呼び出しのたびに 1,000 行を compile しない)。pyc はこのプロセスで今の fake_beads.py から
  作るので、古い pyc を読むことは無い (読めない pyc なら import が source から読み直す)
- shebang は今の python に -S を付ける (site を読まない。偽物は標準ライブラリだけを使う)。python のパスに空白があるときは、
  今までどおり `/usr/bin/env python3` にする
偽物は argv[0] の basename で振る舞いを決める。hard link の名前が bd・br なので、写しのときと同じに振る舞う。
"""
from __future__ import annotations

import atexit
import os
import py_compile
import re
import shutil
import sys
import tempfile
from pathlib import Path

FAKE = Path(__file__).resolve().parent / "fake_beads.py"
_launcher: list[str] = []                           # このプロセスの起動の script (作ったら 1 件)


def launcher() -> str:
    """このプロセスの起動の script のパス (初回に作る)。"""
    if _launcher:
        return _launcher[0]
    d = os.path.realpath(tempfile.mkdtemp(prefix="humanq-fakecli-"))
    atexit.register(shutil.rmtree, d, True)
    src = os.path.join(d, "fake_beads.py")
    shutil.copyfile(FAKE, src)
    py_compile.compile(src, doraise=True)
    shebang = "#!/usr/bin/env python3" if re.search(r"\s", sys.executable) else "#!" + sys.executable + " -S"
    path = os.path.join(d, "fake")
    Path(path).write_text(f"{shebang}\nimport sys\nsys.path.insert(0, {d!r})\nimport fake_beads\nfake_beads.entry()\n",
                          encoding="utf-8")
    os.chmod(path, 0o755)
    _launcher.append(path)
    return path


def install(bin_dir: str, prog: str) -> str:
    """偽物を bin_dir に prog の名前で置き、そのパスを返す (同じ名前のファイルがあれば置き換える)。"""
    src = launcher()
    dst = os.path.join(bin_dir, prog)
    if os.path.lexists(dst):
        os.unlink(dst)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o755)
    return dst
