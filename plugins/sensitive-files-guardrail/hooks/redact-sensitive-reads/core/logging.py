"""秘密非混入ログ。

呼出側は第二引数に path / 値 / basename / command 文字列を絶対に渡してはならない。
渡してよいのはエラー種別・関数名・処理時間・classify 結果などの
「公開しても安全な情報」のみ。

0.4.3 で **detail に文字種ホワイトリスト** を導入 (L1)。設計コメントだけで
依存していた呼出側責任の最終防御層として、コード変更時の意図せぬ秘密混入
(path / 値 / basename) を実行時に止める。違反は ``_BAD`` placeholder に
置換してログする。category 側は固定文字列 (caller がハードコード) なので
sanitize 対象外。

0.27.0 (内部バックログ): unittest 実行が実ログを汚染し計測値を誤らせていた
問題への対処として、``SFG_LOG_PATH`` 環境変数で書込み先を差し替えられるように
した。テスト側 (``tests/_testutil.py`` / ``tests/conftest.py``) がこの環境変数を
プロセス起動時に一度だけ tmpdir へ設定する。``LOG_PATH`` はモジュール import
時に 1 回だけ解決するので、環境変数はそれより前 (import 前) に設定されている
必要がある — テスト側の sys.path bootstrap (``_testutil`` を最初に import する
慣例) がこの前提を自然に満たす。``LOG_PATH`` 自体は従来通りモジュール属性の
ままなので、``mock.patch.object(L, "LOG_PATH", ...)`` による個別テストの差し替え
(``tests/test_logging.py``) は影響を受けない。

0.32.0 (内部バックログ): ログ量そのものへの対策として、``begin_deferred`` /
``flush_deferred`` による **判定確定後の遅延 emit** と ``SFG_LOG_LEVEL``
(``LOG_LEVEL``) を追加した。``SFG_LOG_LEVEL=WARNING`` にすると「最終判定が
allow だった呼出の INFO」だけが落ち、deny / ask 経路の診断と ERROR、および
``log_info(..., always=True)`` でマークした記録 (leveling 対象外) は残る。
**既定 (未設定) は INFO なので挙動は従来と完全に同一** (量対策は opt-in)。
詳細は ``_resolve_log_level`` / ``flush_deferred`` の docstring。

同じく 0.27.0 (内部バックログ): ログの無制限増加を防ぐ 1 世代ローテーションを
追加した (``MAX_LOG_BYTES`` / ``_rotate_if_needed``)。ローテーションは
サイドカー lock ファイル (``<log>.lock``) の ``flock`` で**プロセス間 直列化**
してある — 直列化しないと、並行する 2 プロセスが揃って閾値超過を観測し、
先行プロセスが rename して新ログに 1 行書いた直後に後続プロセスが
``os.replace`` してしまい、``.1`` が「1 行だけの新ログ」で上書きされて前世代
が丸ごと消える (外部レビュー R1 P2-B)。
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

try:  # pragma: no cover - 非対応 OS 側の分岐は CI で踏まない
    import fcntl
except ImportError:  # Windows には fcntl が無い
    fcntl = None  # type: ignore[assignment]


def _resolve_log_path() -> Path:
    """``LOG_PATH`` の実体を解決する (内部バックログ)。

    ``SFG_LOG_PATH`` 環境変数が設定されていればそれを使う (テスト実行が実ログ
    ``~/.claude/logs/redact-hook.log`` を汚染しないための差し替え口)。未設定なら
    既定のパス。モジュール import 時に 1 回だけ呼ばれる。
    """
    override = os.environ.get("SFG_LOG_PATH")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "logs" / "redact-hook.log"


LOG_PATH = _resolve_log_path()


# ---- ログレベルと遅延 emit (0.32.0、内部バックログ) ----------------------
#
# 動機: ``redact-hook.log`` は Bash 呼出のたびに **allow 経路でも** INFO を
# 書く (実測 7.3MB / 12 万行)。0.27.0 で 5MB / 1 世代のローテーションを入れたが、
# 量そのものは減っていない。
#
# 「allow 経路の INFO を落とす」を log 呼出の時点では判定できない: ``ask_or_allow``
# の結果は runtime の ``permission_mode`` に依存し、同一コマンド内で後続 segment の
# deny が先行の ask/allow を上書きするため、「この log 呼出は allow 経路か」が
# 静的に決まらない。そこで **判定確定までバッファし、最終判定に応じて出すか
# 決める**。
#
# 有効レベルの決め方 (バッファされた INFO record):
# - 最終判定が allow  → INFO 相当
# - 最終判定が deny / ask → WARNING 相当 (deny / ask の診断は必ず残す)
#
# ``LOG_LEVEL`` (= ``SFG_LOG_LEVEL``) はその閾値。**既定は INFO なので挙動は
# 従来と完全に同一** (量対策は opt-in)。既定を変えなかったのは、分類分布の
# 計測 (``bash_classify`` の集計) が既にこのログを前提にしているため。
#
# **行の label は ``INFO `` のまま**にしてある (既存の grep / 集計を壊さない)。
# 「有効レベル」は出すか出さないかの判断にだけ使う内部概念。
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}
_LEVEL_INFO = _LEVELS["INFO"]
_LEVEL_WARNING = _LEVELS["WARNING"]
_DEFAULT_LEVEL = _LEVEL_INFO


def _resolve_log_level() -> int:
    """``SFG_LOG_LEVEL`` を数値レベルに解決する (未設定 / 不正値は INFO)。

    不正値で INFO (= 従来挙動) に倒すのは、typo で**ログが黙って消える**より
    「多すぎる」方が安全なため。``LOG_PATH`` と同じく import 時に 1 回解決し、
    モジュール属性として残す (``mock.patch.object(L, "LOG_LEVEL", ...)`` で
    テストから差し替えられる)。
    """
    raw = os.environ.get("SFG_LOG_LEVEL", "")
    return _LEVELS.get(raw.strip().upper(), _DEFAULT_LEVEL)


LOG_LEVEL = _resolve_log_level()

# 遅延バッファ。``None`` = 遅延無効 (即時書込 = 従来動作)。``list`` = 遅延中。
# 要素は ``(行, leveling 対象外か)`` の対 — ``log_info(..., always=True)`` で
# 積んだ行は ``flush_deferred`` の閾値判断を素通りして必ず書く (下記)。
# hook はシングルスレッドの短命プロセスなのでモジュール変数で足りる。
# 関数を差し替える方式にしないのは、``handlers.bash_handler._muted_logging``
# が ``L.log_info`` / ``L.log_error`` を属性ごと保存・復元するため
# (両方が関数差し替えだと入れ子で取り違える)。
_pending: list[tuple[str, bool]] | None = None


def begin_deferred() -> None:
    """以降の INFO を判定確定までバッファする (``__main__`` が呼ぶ)。"""
    global _pending
    _pending = []


def flush_deferred(decision: str | None) -> None:
    """バッファを最終判定に応じて書き出し、遅延を解除する。

    Args:
        decision: ``core.output.decision_of`` の結果 (``"deny"`` / ``"ask"`` /
            allow なら ``None``)。

    ``begin_deferred`` を呼んでいない / バッファが空なら何もしない。
    **必ず呼ぶこと** (呼ばないとバッファが捨てられる) — ``__main__`` は
    ``finally`` で呼ぶ。

    ``log_info(..., always=True)`` で積んだ行は **leveling 対象外** なので、
    閾値で落とす場合もその行だけは書く (0.32.0、マージ前レビューの指摘)。
    走査は 1 回だけで、書く行の**相対順序は元のまま**保つ。
    """
    global _pending
    pending, _pending = _pending, None
    if not pending:
        return
    effective = _LEVEL_INFO if decision is None else _LEVEL_WARNING
    drop_unmarked = effective < LOG_LEVEL
    for line, always in pending:
        if drop_unmarked and not always:
            continue
        _append(line)

# ログファイルの 1 世代ローテーション閾値 (内部バックログ)。この byte 数を
# 超えて書き込む**前**に ``<LOG_PATH>.1`` へ rename する (直近世代のみ保持)。
# テストが差し替えやすいようモジュール定数にしてある。
MAX_LOG_BYTES = 5 * 1024 * 1024

# detail に許可する文字種 (0.4.3, L1)。
# 既存使用例の最大は 33 文字 (``segment_residual_metachar_lenient``)。
# `:` は ``f"shell_keyword_lenient:{first}"`` のような identifier 連結用。
# `[` `]` は ``_SHELL_KEYWORDS`` の ``[[`` / ``]]`` / ``[`` / ``]`` 用。
# `!` は ``_OPAQUE_WRAPPERS`` の ``!`` (否定) 用 (現状ログに来ないが将来拡張)。
# 長さ 64 で打ち切り (path 文字列等が誤って入ったときの被害を抑える)。
_DETAIL_RE = re.compile(r"^[A-Za-z0-9_:.\-\[\]!]{0,64}$")
_DETAIL_PLACEHOLDER = "_BAD"


def _sanitize_detail(detail: str) -> str:
    """detail を文字種ホワイトリストで通す。違反は ``_BAD`` に置換 (L1)。

    str 以外、長さ超過、許可外文字混入のいずれでも placeholder を返す。
    呼出側の契約 (公開可情報のみ) を破った場合の最終防御。
    """
    if not isinstance(detail, str):
        return _DETAIL_PLACEHOLDER
    if _DETAIL_RE.match(detail):
        return detail
    return _DETAIL_PLACEHOLDER


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _rotate_if_needed(path: Path) -> None:
    """``path`` が ``MAX_LOG_BYTES`` を超えていたら 1 世代ローテーションする
    (内部バックログ)。

    ``<path>.1`` への rename のみ (2 世代目以降は保持せず上書き)。stat / rename
    のいずれの失敗も握りつぶし、そのまま追記を続ける (ログ機構の不具合で hook
    本体の判定を止めない — ``log_error`` / ``log_info`` 全体の契約と同じ)。
    rename 後も同じ file descriptor を開いたまま追記し続ける並行プロセスが
    あれば、その分の行は rename 済みの旧 world (= ``.1`` 側) に残ることがある
    (数行が世代をまたいで混ざる程度で内容は壊れない)。

    **プロセス間の直列化** (外部レビュー R1 P2-B): 並行する複数プロセスが揃って
    閾値超過を観測しうる。先行プロセスが rename して新ログに 1 行書いた後に
    後続プロセスが ``os.replace`` すると、``.1`` が「1 行だけの新ログ」で
    上書きされ前世代が丸ごと消える。parallel tool call が並行呼出を作るため
    現実に起こりうる経路で、8 プロセスを barrier で同時解放する再現実験では
    40 ラウンド中 36〜40 ラウンドで ``.1`` が 1 行 (41 byte) に潰れた
    (フィールドでの観測ではなく合成負荷での再現)。これを防ぐため:

    1. サイドカー lock ファイル ``<path>.lock`` に ``flock(LOCK_EX | LOCK_NB)``
       を取る。取れなければ**ローテーションを skip** して通常の追記に進む
       (他プロセスが担当しているため。待たずに譲るので hook を遅延させない)。
    2. lock 内で **再 stat** し、``(st_dev, st_ino)`` が最初に観測したものと
       同一で、かつ依然として閾値超過であることを確認してから ``os.replace``
       する。lock 待ちの間に他プロセスが rename 済みなら inode が変わる
       (またはファイルが消える) ので、その場合は何もしない。

    lock ファイルは削除しない (削除と再作成の競合で「別 inode を lock する
    2 プロセス」が生じ、直列化そのものが壊れるため)。閾値を超えるまで作られ
    ないので通常運用では出現しない。

    ``fcntl`` が無い環境 (Windows) では**ローテーションを一切行わない**。
    直列化できないまま rename するとログを失う方向に倒れるため、ファイルが
    伸び続ける方 (データを失わない方) を選ぶ。なお本 plugin は現状 Windows
    非対応 (README「互換性」) なので実運用上の分岐ではない。
    """
    if fcntl is None:
        return
    try:
        before = path.stat()
        if before.st_size < MAX_LOG_BYTES:
            return
    except OSError:
        return
    try:
        lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError:
        return
    try:
        try:
            # LOCK_NB なので取れなければ即 BlockingIOError (OSError のサブ
            # クラス)。担当を他プロセスに譲り、そのまま追記に進む。
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        try:
            now = path.stat()
        except OSError:
            # lock 待ちの間に他プロセスが rename 済み
            return
        if (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino):
            # 同じ path だが別ファイル = 既に他プロセスがローテーション済み
            return
        if now.st_size < MAX_LOG_BYTES:
            return
        try:
            os.replace(path, Path(str(path) + ".1"))
        except OSError:
            pass
    finally:
        # close で flock も解放される
        os.close(lock_fd)


def _append(line: str) -> None:
    """1 行をログファイルに追記する (ローテーション込み、内部バックログ)。

    ディレクトリ作成 / ローテーション / 書込みのいずれの失敗も握りつぶす
    (hook の責務ではない)。
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(LOG_PATH)
        with LOG_PATH.open("a") as f:
            f.write(line)
    except OSError:
        pass


def log_error(category: str, detail: str = "") -> None:
    """エラーログを記録する。detail は公開可情報のみを想定 (L1 で sanitize)。

    stderr にも category を出力 (Claude Code UI で可視化される)。
    ファイル書込失敗は握りつぶす (hook の責務ではない)。

    **``LOG_LEVEL`` に関わらず必ず書く**。遅延中 (``begin_deferred`` 済み) なら
    バッファを**先に吐き出してから**自分を書く (0.32.0): この時点では最終判定が
    未確定でバッファを leveling できないため、順序を保って全部出す方に倒す。
    error は稀なので量への影響は無視でき、しかも「エラーが出た呼出」は診断情報を
    一番欲しい場面なので、ここで INFO を落とすのは筋が悪い。
    """
    global _pending
    safe_detail = _sanitize_detail(detail)
    line = f"{_now()} ERROR {category} {safe_detail}\n".rstrip() + "\n"
    try:
        sys.stderr.write(f"[redact-hook] {category}\n")
    except OSError:
        pass
    if _pending:
        for pending_line, _always in _pending:
            _append(pending_line)
        _pending = []
    _append(line)


def log_info(category: str, detail: str = "", *, always: bool = False) -> None:
    """INFO ログ (stderr には出さない)。detail は公開可情報のみ (L1 で sanitize)。

    遅延中 (``begin_deferred`` 済み) はバッファに積み、``flush_deferred`` が
    最終判定に応じて出すか決める (0.32.0)。遅延していない呼出は ``LOG_LEVEL``
    の閾値だけで判断する。

    ``always=True`` は「**レベル固定で積む** (leveling 対象外)」マーク
    (0.32.0、マージ前レビューの指摘)。``LOG_LEVEL`` / 最終判定に関わらず必ず
    書くが、**書く位置は他の INFO と同じ** (遅延中なら flush 時) なので
    ``log_error`` のような即時書込・順序入替は起こさない。

    用途は「記録が消えると開示している緩和策が成立しなくなる」種類の記録だけ
    に限る — 例: repo 同梱 patterns を読み込んだ事実
    (``core.patterns._note_project_patterns``)。この記録は「repo の ``!`` 行が
    保護を弱めても後から辿れる」ことを公表した緩和策なので、`allow` に倒れた
    呼出 (= まさに除外が効いた呼出) で落ちると緩和策そのものが無くなる。
    通常の診断 (``bash_classify`` 等) はマークしない — 量対策の目的が消える。
    """
    safe_detail = _sanitize_detail(detail)
    line = f"{_now()} INFO  {category} {safe_detail}\n".rstrip() + "\n"
    if _pending is not None:
        _pending.append((line, always))
        return
    if not always and _LEVEL_INFO < LOG_LEVEL:
        return
    _append(line)
