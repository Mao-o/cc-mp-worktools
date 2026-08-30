"""flock 付き read-modify-write。

exitplan-review のマーカー (hash / count) と post-implementation-review の状態ファイルは
どちらも「開く → LOCK_EX → 全読み → 書き戻し → LOCK_UN」の同型なので、ここに寄せる。

非ブロッキングで review 実行中ずっと保持する `cursor_lock` は、ロックファイルを開けない
環境で直列化を諦める fail-open 分岐が固有なので共通化していない
(post-implementation-review/state.py)。

## 権限 (0.9.0, 内部バックログ)

state / マーカーファイルは絶対パス一覧やレビュー本文 (コード抜粋) を含む。macOS の
`$TMPDIR` はユーザー専用ディレクトリだが、Linux の `/tmp` は共有で、既定 umask
(大半の環境で 022) のまま作成すると他ユーザーから読める (`-rw-r--r--` を実機で確認)。
このモジュールで作る新規ディレクトリは 0o700、新規ファイルは 0o600 に締める。

- `os.makedirs(path, mode=0o700)` は **最後の 1 階層にしか mode を適用しない**
  (中間ディレクトリは umask 既定のまま作られるのが Python 公式ドキュメントに明記された
  仕様)。`$TMPDIR/post-implementation-review/state/` のような多階層パスをこれで作ると、
  肝心の `post-implementation-review/` 自体が既定モードのまま残る。`_makedirs_private`
  で 1 階層ずつ `os.mkdir(component, 0o700)` して回避する
- 組み込み `open()` は permission bits を指定できないため、新規ファイルの作成は
  `os.open(..., 0o600)` + `os.fdopen` で行う (`write_private` / `locked_file` 内)
- **既存ファイル/ディレクトリの権限は変更しない** (旧版が既定 umask で作ったものは
  この関数を呼んだだけでは締まらない)。ディレクトリの retrofit は `harden_dir` を
  各 hook の periodic GC (Stop 契機) から呼ぶこと。個々のファイルまでは retrofit
  しない — 親ディレクトリを 0o700 にすれば他ユーザーは traversal 自体ができず、
  ファイル単体の mode に関わらず読めなくなるため (POSIX のディレクトリ権限の性質)

## 事前に作られた状態ディレクトリを信用しない (マージ前レビューの指摘)

共有 `$TMPDIR` では、hook がまだ一度も動いていない環境で**他ユーザーが先回りして**
state ディレクトリ (post-implementation-review の `state_root()`、exitplan-review の
`marker_dir` 相当) を作れる。所有者が攻撃者で誰でも書ける状態のまま受け入れると、
ファイル単体を 0o600 にした意味が無い — ディレクトリの書込権限を持つ側は配下の
ファイルを列挙・削除・差し替えできる。

`_makedirs_private` (上記) は中間ディレクトリの生成を担当する薄いヘルパーで、既存の
ディレクトリを無条件に受け入れる設計のままにしている。これは**意図的**: 再帰の
末端は `$TMPDIR` 自身のような「環境が用意した祖先ディレクトリ」に必ず到達し、
`/tmp` は典型的に root 所有・group/other 書込可 (sticky bit 付き) の共有ディレクトリ
なので、既存ディレクトリの所有者/権限を検査する処理をここに混ぜると `/tmp` 自体を
弾いてしまい hook が初回起動時に必ず壊れる。

代わりに、hook が所有する**ちょうど 1 階層** (`state_root()` / `marker_dir`) だけを
対象にした `ensure_private_root` を用意した。呼び出し側 (各 hook の phase handler) は
state を読み書きする前に毎回これを呼び、`UnsafeStateDirError` を fail-closed
(レビューしない) の合図として扱うこと。
"""
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO


class UnsafeStateDirError(Exception):
    """hook が所有する状態ディレクトリ (`ensure_private_root` の対象) が信用できない。

    実効ユーザー以外が所有している、group/other に書込権がある、または symlink に
    差し替えられている場合に送出する。

    **意図的に `OSError` を継承しない**: `state.py` 等の呼び出し側は個々の
    read-modify-write 失敗を `except OSError: pass` で fail-open にしている
    (壊れた state ファイル 1 個を無視しても実害が小さいため)。この例外は逆に
    fail-closed (レビューしない) にしたいので、既存の fail-open 経路に紛れ込ませては
    いけない。同じ理由で `ensure_private_root` は `os.mkdir` 等から受け取る他の
    `OSError` (ENOSPC・読み取り専用 FS など) をこの例外に包み直さずそのまま伝播させる
    — それらは「状態を保持できない」であって「状態が信用できない」ではないため、
    既存の fail-open 経路に委ねてよい。
    """


def _is_private_root_safe(path: str) -> bool:
    """path が「symlink ではない実効ユーザー所有ディレクトリで、group/other に
    書込権が無い」ことを確認する。

    **`os.lstat` を使う (symlink を辿らない)**。`os.stat` / `os.path.isdir` は
    symlink を辿ってしまうため、symlink 先がディレクトリなら `S_ISDIR` が True に
    なり、symlink をこの用途の状態ディレクトリとして掴まされる経路をすり抜けて
    しまう。`os.lstat` ならシンボリックリンク自体の mode (`S_IFLNK`) を見るので、
    symlink は常に `S_ISDIR` が False になりここで弾かれる。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(st.st_mode)
        and st.st_uid == os.geteuid()
        and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def ensure_private_root(path: str) -> None:
    """hook が所有する最上位の状態ディレクトリ (`state_root()` / exitplan-review の
    `marker_dir` 相当) を安全に用意する。呼び出し側は state を読み書きする**前**に
    毎回呼ぶこと (モジュール docstring「事前に作られた状態ディレクトリを信用しない」
    参照。`_makedirs_private` はこの検査を代わりに行わない)。

    - 新規作成 (`os.mkdir` が競合なく成功) なら、作成者が自分になるので無条件に安全
    - 既に存在し、かつ最初から `_is_private_root_safe` を満たすなら、そのまま使う
    - **既に存在し、最初から安全でない場合はこの回では使わない**。symlink でなければ
      次回以降のために `os.chmod` で締め直しを試みるが、**成功したかどうかに関わらず
      `UnsafeStateDirError` を送出する**。「見つかった時点で緩かった」という事実
      自体が、緩かった間に他ユーザーが中身を書き換えられた可能性を否定できない
      ため、その場で締め直せても今回は信用しない (次にこの関数を呼んだ時点で
      既に 0o700 になっていれば、その回からは通常どおり使える — 自己所有で
      chmod が効く環境なら 1 回のスキップだけで自己修復する)。**symlink には
      chmod を呼ばない** — `os.chmod` は既定で symlink を辿るため、呼ぶと symlink
      先 (攻撃者が選んだ任意のパスでありうる) の権限を変えてしまう
    - 所有者が違って締め直しが失敗する場合は当然この先も安全にならないため、
      管理者が手動でディレクトリを消す/権限を直すまで永続的に拒否し続ける

    以前は「既に存在する」だけで受け入れて早期 return し、締め直し失敗も
    `except OSError: pass` で握り潰していたため、攻撃者所有のディレクトリや
    緩いまま放置されたディレクトリがそのまま使われ続けていた (マージ前レビューの指摘)。

    **残る競合窓**: この検査と実際の読み書き (`locked_file` / `write_private` の
    `os.open`) の間にファイルシステムを差し替える古典的な TOCTOU には対応していない
    (`O_NOFOLLOW` + `dir_fd` 相対の全面書き換えが要り、影響範囲がこの修正の数倍に
    なる)。ここで塞ぐのは「hook が一度も動く前に先回りしてディレクトリを作られる」
    という、実演された経路。
    """
    try:
        os.mkdir(path, 0o700)
        return
    except FileExistsError:
        pass

    if _is_private_root_safe(path):
        return

    if not os.path.islink(path):
        try:
            os.chmod(path, 0o700)  # 次回のために締め直す (今回は信用しない)
        except OSError:
            pass
    raise UnsafeStateDirError(path)


def _makedirs_private(path: str) -> None:
    """path までの各階層を 0o700 で作成する (既存の祖先には触らない)。

    `os.makedirs(path, mode=0o700)` は最後の階層にしか mode を適用しないため、
    共有 `$TMPDIR` 上で中間ディレクトリが既定 umask (0o755 相当) のまま残りうる
    (モジュール docstring 参照)。1 階層ずつ確認しながら作ることで、新規作成分は
    すべて 0o700 にする。

    **既存ディレクトリの所有者/権限をここで検査しない (意図的)**: この再帰は
    `path` の祖先を辿って必ず `$TMPDIR` 自身に到達する。`/tmp` は典型的に root
    所有・group/other 書込可 (sticky bit 付き) の共有ディレクトリであり、これは
    正当な状態なので弾いてはいけない。「既存ディレクトリを無条件に信用しない」
    検査は、hook が所有する 1 階層だけを対象にする `ensure_private_root` の責務
    にしてある (モジュール docstring 参照)。ここに検査を足すと `$TMPDIR` 自体を
    弾いてテストは緑のまま (`tempfile.TemporaryDirectory()` は常に 0o700 所有な
    ので再帰が `$TMPDIR` に達する前に既存ディレクトリ扱いで return してしまい、
    このケースを一切踏まない) 本番の初回起動が壊れる — 変更する前に
    `ensure_private_root` 側で対応できないか検討すること。
    """
    if not path or os.path.isdir(path):
        return
    parent = os.path.dirname(path)
    if parent and parent != path:
        _makedirs_private(parent)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass


def harden_dir(path: str) -> None:
    """既存ディレクトリの権限が 0o700 より緩ければ締め直す (旧版からの retrofit)。

    共有 `$TMPDIR` では他ユーザー所有のディレクトリを掴む可能性があるため、
    chmod 失敗 (`PermissionError` 等) は fail-open で無視する。
    """
    try:
        if os.path.isdir(path) and os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        pass


def write_private(path: str, content: str) -> None:
    """新規ファイルを 0o600 で作成して content を書き込む (既存ファイルは truncate)。

    親ディレクトリも `_makedirs_private` で 0o700 に作る。state ファイル以外
    (Bash スナップショット・レビュー結果の参照コピー) が使う、flock を要らない
    単発書込み用のヘルパー。
    """
    parent = os.path.dirname(path)
    if parent:
        _makedirs_private(parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


@contextmanager
def locked_file(path: str) -> Iterator[IO[str]]:
    """path を read+write で開き (無ければ 0o600 で新規作成)、排他ロックを取って
    file object を yield する。

    親ディレクトリは `_makedirs_private` で 0o700 に作る。OSError は呼び出し側に
    伝播させる (fail-open の扱いは hook ごとに決める)。読み書きは `read_all` /
    `rewrite` で行う。

    以前は組み込み `open(path, "a+")` を使っていた (permission bits を指定できず
    既定 umask で作られていた)。`O_CREAT` のみ (`a+` の「書込は常に末尾」という
    性質は使わない) にしても機能は変わらない — 呼び出し側 (`read_all` / `rewrite`)
    は必ず明示的に `seek()` してから読み書きするため。
    """
    parent = os.path.dirname(path)
    if parent:
        _makedirs_private(parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield f
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_all(f: IO[str]) -> str:
    f.seek(0)
    return f.read()


def rewrite(f: IO[str], content: str) -> None:
    """ファイル全体を content で置き換える (seek → truncate → write → flush)。"""
    f.seek(0)
    f.truncate()
    f.write(content)
    f.flush()
