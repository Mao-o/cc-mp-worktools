"""cursor agent CLI の検出と、読み取り専用の起動 argv。

3 hook (explore-parallel の並走調査 / exitplan-review / post-implementation-review) は
いずれも cursor に「読むだけ」を求めるので、起動 argv は `readonly_argv` に一本化する。
待機方式は hook ごとに違う (explore-parallel はバックグラウンド Popen + 結果ファイル、
review 系 2 hook は `subproc.run_for_output`) ため、ここで共有するのは検出と argv だけ。

## 検出 (0.11.0)

0.10.0 までは `shutil.which("cursor")` だけを見ていた。`cursor` という名前は環境に
よって指す実体が違う:

- Agent CLI 本体 (`cursor-agent`)
- Agent CLI へ `exec` するシム
- **Cursor.app が入れる IDE ランチャー** (VS Code 系の `code` 相当)

IDE ランチャーしか無い環境では `which` が当たってしまい、`cursor agent ...` を
起動しては失敗するまで待つ (review 系 hook では最大 600 秒) ことになる。

そこで **候補を順に見て、最初に失格でないものを使う**:

| 順 | コマンド | 起動時のサブコマンド | 理由 |
|---|---|---|---|
| 1 | `cursor-agent` | なし | Agent CLI 本体の名前。曖昧さが無い |
| 2 | `cursor` | `agent` | 0.10.0 までの起動形。シム / IDE ランチャーのどちらでもありうる |

**候補に `agent` は入れない** (マージ前レビューの指摘)。`agent` はプロダクト名を持たない
汎用名で、Cursor と無関係な実体 (社内スクリプト・別ツールの別名) が PATH に居るだけで
採用されうる。3 hook はいずれもここで決めた argv で起動するので、そこには**リポジトリの
git diff や実装プランがそのまま渡る** (argv は `ps` からも見える)。候補を 1 つ増やすことは
**送信先を 1 つ増やすこと**で、下振れに上限が無い。`agent` に本物が置かれている環境は
`EXTERNAL_AI_CURSOR_COMMAND=agent` で明示指定する (probe も飛ばすので従来どおり動く)。

- 候補は `--version` で応答するかを確認する (`_probe`)。壊れたコマンドを掴んで長時間
  待つ経路を、短い probe に置き換えるのが狙い。判定は 3 値 (`PROBE_*`) で、
  **採用は「候補順」が決める** — 失格 (`PROBE_FAILED`) でない最初の候補を使い、
  `PROBE_OK` は「以降の候補を probe せずに確定できる」最適化としてのみ使う
  (`_detect`)。応答確認を採用条件にすると、cold start の遅い本物 (node ベースの CLI) が
  保留になった隙に後ろの IDE ランチャーが勝ってしまう
- **名前で同定できない候補 (`AMBIGUOUS_CANDIDATES`) は、`--version` の出力に
  `cursor` の語がある場合だけ `PROBE_OK`** にする。無ければ `PROBE_UNKNOWN` (保留) に
  落とす — 採用そのものは候補順が決めるので挙動は 0.10.0 と同じだが、「確認できていない」
  ことはキャッシュ TTL (下記) に反映する。`cursor-agent` は名前が固有なので無条件
- 結果は `$TMPDIR/external-ai-assist/cursorcli.json` に TTL 付きでキャッシュする
  (`is_available()` は編集ツールのたびに呼ばれるので、毎回 probe しない)。
  **確認できた (`PROBE_OK`) 結果だけが 1 時間**で、応答未確認のまま採用した結果は
  否定側と同じ短い TTL (`NEGATIVE_CACHE_TTL_SEC`) にする。記録した実体パスが `which` の
  結果と食い違ったとき、および**同じパスの中身が入れ替わったとき** (inode / mtime の
  不一致) はキャッシュを捨てて測り直す
- `EXTERNAL_AI_CURSOR_COMMAND` を設定するとその値を無条件で使う (probe もしない)。
  検出が環境に合わない場合の逃げ道で、テストもこれで実体を固定する。**basename が
  `cursor` のときだけ `agent` サブコマンドを足す** (上の表と同じ規則)
- 検出には**予算 (`PROBE_BUDGET_SEC`) があり、呼び出し側から `deadline` でさらに縮め
  られる**。explore-parallel の pre hook は残骸 GC と同じ 5 秒の枠で回るため、GC が
  使った時間を引いた残りを渡す (`__main__._main`)。予算切れは既存の `PROBE_UNKNOWN`
  経路に落ちるだけなので、機能が止まる方向には倒れない

**この probe で IDE ランチャーと Agent CLI を確実に見分けられるわけではない** (どちらも
`--version` に 0 で応答しうる)。見分けは上の**検出順**と曖昧な名前の同定が担う。
`--help` の内容で判定する案は、実機の出力を確認できていないため採らない (誤判定すると
機能が黙って止まる)。**未ログイン状態の検出も同様に未実装** — `--version` はログイン状態に
関係なく応答し、実際の失敗文言を実機で確認できていないため、推測でパターンを書くと
正常なレビュー本文を誤検出して機能を止めうる。どちらも実機確認が済んでから入れる
(内部バックログ)。
"""
from __future__ import annotations

import json
import os
import shutil
import time

from . import subproc
from .flock import UnsafeStateDirError, ensure_private_root, write_private
from .hooklog import make_logger

NAME = "cursor"

#: 検出できなかったときに使う従来の起動形 (0.10.0 までと同じ)。
BINARY = "cursor"

#: 検出順 (コマンド名)。起動時のサブコマンドは `subcommand_for()` が決める。
#:
#: **汎用名 `agent` は入れない** (module docstring)。Cursor と無関係な実体に git diff /
#: プランを渡す経路になるため、そこに本物が居る環境は `ENV_COMMAND` で明示指定する。
CANDIDATES = ("cursor-agent", "cursor")

#: 名前だけでは Cursor Agent CLI と同定できない候補。`--version` の出力に
#: `IDENTITY_TOKEN` があるときだけ `PROBE_OK` (= 確認できた) 扱いにする。
AMBIGUOUS_CANDIDATES = ("cursor",)

#: 曖昧な名前の候補を「確認できた」とみなすために `--version` の出力へ要求する語
#: (小文字で比較する)。
IDENTITY_TOKEN = "cursor"

#: 読み取り専用 (`--mode plan`) の print モードで 1 回実行するためのフラグ列。
#:
#: `--print` (`-p`) 単独は cursor-agent の help で「Has access to all tools, including
#: write and shell」とされる書込可能モードで、`--mode plan` が「read-only/planning
#: (no edits)」。0.4.0 までの explore-parallel は `-p` 単独だったため、調査の裏で作業
#: ツリーを書き換えうる agent が走っていた (0.4.1 で修正)。`--trust` は workspace 信頼の
#: 確認ダイアログを省くためで、書込許可ではない (0.2.0 からの既定)。
READONLY_FLAGS = ("--trust", "--print", "--mode", "plan")

#: 検出結果を固定する環境変数 (値は PATH 上のコマンド名か絶対パス)。
ENV_COMMAND = "EXTERNAL_AI_CURSOR_COMMAND"

#: probe (`<cmd> --version`) 1 回あたりの上限と、検出 1 回で使える合計。
#:
#: 検出は `is_available()` 経由で hook の早い段階に走る。post-implementation-review の
#: post-tool hook は timeout 10 秒で `git status` (最大 5 秒) と同居し、explore-parallel の
#: pre hook は timeout 5 秒で残骸 GC (2 秒) と同居するため、合計でも数秒に収める。
#: probe が要るのはキャッシュが無いときだけ (確認できた結果は TTL 1 時間)。
#:
#: `PROBE_BUDGET_SEC` は検出 1 回の**自前の**上限で、呼び出し側が `deadline` を渡すと
#: そこまでで打ち切る (短いほうが勝つ)。explore-parallel の pre は GC が使った時間を
#: 引いた残りを渡すため、GC + 検出 + 起動枠の待ちの合計が hook timeout を超えない。
PROBE_TIMEOUT_SEC = 1.5
PROBE_BUDGET_SEC = 3.0

#: `_probe` の 3 値。`PROBE_UNKNOWN` (応答を確認できない) は候補を失格にしない
#: (`_detect` の docstring。起動の遅い本物を誤って切らないため)。
PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_UNKNOWN = "unknown"

#: 応答しない probe を止めるときの SIGTERM → SIGKILL の猶予 (秒)。
#: 既定 (`subproc.KILL_GRACE_SEC` = 5 秒) は長時間レビュー CLI 向けの値で、`--version` に
#: 応答しない時点でその CLI は使わないのだから行儀よく終わるのを待つ価値がない。
PROBE_KILL_GRACE_SEC = 0.5

#: 検出結果のキャッシュ TTL (秒)。見つかった場合と見つからなかった場合で分ける —
#: 「入れた直後に使い始める」ほうが「入っているのに毎回 probe する」より体験が良い。
CACHE_TTL_SEC = 3600
NEGATIVE_CACHE_TTL_SEC = 300

log = make_logger("cursorcli")

_MISS = object()  # 「有効なキャッシュが無い」を None (= 有効な否定結果) と区別する
_RESOLVED: dict[str, tuple[str, tuple[str, ...]] | None] = {}
_RESOLVED_KEY = "value"


def reset() -> None:
    """プロセス内に覚えた検出結果を捨てる (テストと、環境を変えて測り直したいとき用)。

    本番の hook は 1 回の呼び出しごとに使い捨てプロセスなので呼ぶ必要は無い。
    """
    _RESOLVED.clear()


def subcommand_for(command: str) -> tuple[str, ...]:
    """そのコマンドを Agent CLI として起動するときに足すサブコマンド。

    `cursor` (IDE 側と共用の名前) だけが `cursor agent ...` の形を要求する。
    `cursor-agent` / `agent` は本体そのものなのでサブコマンドを足さない。
    """
    return ("agent",) if os.path.basename(command) == "cursor" else ()


def resolve(deadline: float | None = None) -> tuple[str, tuple[str, ...]] | None:
    """使える Agent CLI の `(コマンド, サブコマンド)`。見つからなければ None。

    プロセス内で 1 回だけ計算する (hook は 1 回の呼び出しごとに使い捨てプロセス)。

    `deadline` (time.monotonic 基準) を渡すと **probe をそこまでで打ち切る**。短い
    hook timeout の中で回す呼び出し側 (explore-parallel の pre は残骸 GC と同じ 5 秒の枠)
    が、既に使った時間を引いた残りを渡すため。予算切れは `PROBE_UNKNOWN` = 保留に
    落ちるだけで、候補そのものは従来どおり使える。
    """
    if _RESOLVED_KEY not in _RESOLVED:
        _RESOLVED[_RESOLVED_KEY] = _resolve_uncached(deadline)
    return _RESOLVED[_RESOLVED_KEY]


def is_available(deadline: float | None = None) -> bool:
    return resolve(deadline) is not None


def readonly_argv(prompt: str) -> list[str]:
    """読み取り専用 (`--mode plan`) の print モードでプロンプトを 1 回実行する argv。

    検出できなかった場合は 0.10.0 までと同じ `cursor agent ...` に倒す (呼び出し側は
    `is_available()` で先に確認する契約なので通常は到達しない。ここで例外を投げると
    hook 側の fail-open 経路を通らずに落ちる形が増えるため、従来形を返す)。
    """
    resolved = resolve()
    command, prefix = resolved if resolved else (BINARY, subcommand_for(BINARY))
    return [command, *prefix, *READONLY_FLAGS, prompt]


# --------------------------------------------------------------------------
# 検出の実処理
# --------------------------------------------------------------------------


def _resolve_uncached(deadline: float | None = None) -> tuple[str, tuple[str, ...]] | None:
    override = os.environ.get(ENV_COMMAND, "").strip()
    if override:
        if shutil.which(override) is None:
            log(f"{ENV_COMMAND}={override} が見つからないため cursor を使わない")
            return None
        return override, subcommand_for(override)

    cached = _load_cache()
    if cached is not _MISS:
        return cached  # type: ignore[return-value]

    found, confirmed = _detect(deadline)
    _save_cache(found, confirmed)
    return found


def _detect(
    deadline: float | None = None,
) -> tuple[tuple[str, tuple[str, ...]] | None, bool]:
    """候補を順に probe して使うものを決める。`(結果, 応答を確認できたか)` を返す。

    **採用は候補順が決める** (マージ前レビューの指摘)。失格 (`PROBE_FAILED`) でない
    最初の候補を使い、`PROBE_OK` は「以降の候補を probe せずに確定できる」最適化として
    のみ使う。判定は 3 値:

    | probe の結果 | 扱い |
    |---|---|
    | `PROBE_OK` (0 終了 + 出力あり。曖昧な名前は同定も必要) | 採用 (確認済み) |
    | `PROBE_FAILED` (非 0 終了 / 出力なし) | 失格。次の候補へ |
    | `PROBE_UNKNOWN` (timeout / 予算切れ / 同定できない) | **採用するが未確認** |

    以前は「最初の `PROBE_OK`」で即 return し、`PROBE_UNKNOWN` は後続に `PROBE_OK` が
    無いときの fallback にしていた。その順序だと、cold start の遅い本物
    (`cursor-agent`) が `PROBE_TIMEOUT_SEC` 内に応答を確認できなかった隙に、後ろの
    候補 (IDE ランチャーでありうる `cursor`) が即応答して勝ってしまう —
    **`PROBE_UNKNOWN` を失格にしない理由 (遅い本物を切らない) と矛盾する**うえ、
    この検出が解こうとしていた構成そのもの (本物が `cursor-agent`、IDE ランチャーが
    `cursor`) で失敗する。

    未確認のまま採用した結果は短い TTL (`NEGATIVE_CACHE_TTL_SEC`) でしか保存しない
    (`_save_cache`)。1 時間固定すると「未確認の推測」が長く居座る。

    timeout を失格にしないのは従来どおり: 起動の遅い本物を「壊れている」と誤判定して
    レビュー機能が黙って止まるほうが、0.10.0 までの「掴んでから失敗を待つ」より悪い。
    """
    budget = time.monotonic() + PROBE_BUDGET_SEC
    if deadline is not None:
        budget = min(budget, deadline)
    for command in CANDIDATES:
        path = shutil.which(command)
        if path is None:
            continue
        verdict = _verdict_for(command, path, budget)
        if verdict == PROBE_FAILED:
            log(f"{command} ({path}) が --version に応答しないため候補から外す")
            continue
        if verdict == PROBE_UNKNOWN:
            log(f"{command} ({path}) は応答を確認できないまま使う (候補順を優先)")
        return (command, subcommand_for(command)), verdict == PROBE_OK
    return None, False


def _verdict_for(command: str, path: str, deadline: float) -> str:
    """`_probe` の結果に「名前で同定できるか」を重ねた最終判定。

    `AMBIGUOUS_CANDIDATES` (= `cursor`) は、`--version` が 0 で応答しても**それだけでは
    Cursor Agent CLI だと言えない** (IDE ランチャーも応答する)。出力に `IDENTITY_TOKEN`
    があるときだけ `PROBE_OK` とし、無ければ `PROBE_UNKNOWN` (保留) に落とす。
    採用そのものは候補順が決めるので挙動は 0.10.0 と同じで、違いはキャッシュ TTL と
    ログだけ。`cursor-agent` は名前が固有なので出力を見ない。
    """
    verdict, stdout = _probe(path, deadline)
    if verdict != PROBE_OK or command not in AMBIGUOUS_CANDIDATES:
        return verdict
    if IDENTITY_TOKEN not in stdout.lower():
        log(f"{command} ({path}) の --version 出力から Cursor Agent CLI と同定できない")
        return PROBE_UNKNOWN
    return PROBE_OK


def _probe(path: str, deadline: float) -> tuple[str, str]:
    """`<path> --version` の `(PROBE_*, stdout)`。応答が無ければ stdout は空文字列。

    **`agent` サブコマンドは付けない**。`cursor` が IDE ランチャーだった場合に
    サブコマンドを渡すと引数の解釈が実装依存になるため、最も無害な形だけを試す
    (どちらの実体でも `--version` は即座に返る)。

    stdout を返すのは、曖昧な名前の候補を出力の内容で同定するため (`_verdict_for`)。
    """
    remaining = min(PROBE_TIMEOUT_SEC, deadline - time.monotonic())
    if remaining <= 0:
        log("検出の予算を使い切ったため、残りの候補は確認しない")
        return PROBE_UNKNOWN, ""
    # `subprocess.run` ではなく `run_captured` を使う: timeout 時に process group ごと
    # 止めて残出力を読み捨てるため、CLI が残した孫プロセスが pipe を握ったまま
    # `communicate()` を何十秒もブロックする経路が無い (これを踏むと「長時間待ちを
    # 短い probe に置き換える」という目的そのものが崩れる)。
    result = subproc.run_captured(
        [path, "--version"],
        timeout_sec=remaining,
        kill_grace_sec=PROBE_KILL_GRACE_SEC,
    )
    if result is None:
        return PROBE_UNKNOWN, ""  # timeout / 起動できない
    stdout = (result.stdout or "").strip()
    if result.returncode == 0 and stdout:
        return PROBE_OK, stdout
    return PROBE_FAILED, stdout


# --------------------------------------------------------------------------
# キャッシュ
# --------------------------------------------------------------------------


def cache_path() -> str:
    root = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(root, "external-ai-assist", "cursorcli.json")


def _cache_dir_ok() -> bool:
    """キャッシュ置き場 (hook が所有する 1 階層) を安全に使えるか。

    共有 `$TMPDIR` (Linux の `/tmp`) では、hook が一度も動いていない環境で**他ユーザーが
    先回りして**このディレクトリを作れる。攻撃者が置いた `cursorcli.json` を信用すると
    **外部 AI CLI として起動する実体を他人に選ばせる**ことになるので、`state` 系と同じ
    `ensure_private_root` (所有者・group/other 書込権・symlink の検査) を通す。
    安全でなければキャッシュを使わない = 毎回検出する (probe には予算がある)。

    `_load_cache` 側の「候補名 (`CANDIDATES`) 以外は信用しない」検査と二重にしてある —
    片方だけだと、ディレクトリを奪えた攻撃者が任意のパスを `command` に書けてしまう
    (`shutil.which` はセパレータを含む値をそのパスとして解決するため)。
    """
    try:
        ensure_private_root(os.path.dirname(cache_path()))
        return True
    except UnsafeStateDirError:
        log("キャッシュ置き場の所有者/権限が信頼できないため検出結果をキャッシュしない")
        return False
    except OSError:
        return False


def _fingerprint(path: str | None) -> tuple[int, float] | None:
    """実体の `(inode, mtime)`。読めなければ None。

    パスが同じまま**中身が入れ替わった**ケース (同じ `~/.local/bin/cursor` に別のツールを
    入れ直した・インストーラが置き換えた) を検知するために記録する。`which` の結果だけを
    見ていると、この形は肯定キャッシュの TTL (1 時間) のあいだ気付けず、そこへ git diff を
    送り続けることになる (マージ前レビューの指摘)。
    """
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_ino, st.st_mtime


def _load_cache():
    """有効なキャッシュがあれば `(コマンド, サブコマンド)` か None、無ければ `_MISS`。

    無効とみなす条件: 置き場が信用できない / 読めない / 壊れている / TTL 超過 /
    **記録されたコマンドが `CANDIDATES` に無い** / 記録した実体パスが現在の `which` の
    結果と違う (cursor を入れ直した・PATH が変わった) / **実体の inode・mtime が
    記録と違う** (同じパスの中身が入れ替わった)。

    TTL は「応答を確認できた肯定結果」だけが `CACHE_TTL_SEC` で、否定結果と
    **未確認のまま採用した結果**は `NEGATIVE_CACHE_TTL_SEC`。
    """
    if not _cache_dir_ok():
        return _MISS
    try:
        with open(cache_path()) as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return _MISS
    if not isinstance(entry, dict):
        return _MISS

    stamp = entry.get("at")
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return _MISS
    command = entry.get("command")
    confirmed = entry.get("confirmed") is True
    ttl = (
        CACHE_TTL_SEC
        if isinstance(command, str) and command and confirmed
        else NEGATIVE_CACHE_TTL_SEC
    )
    if time.time() - float(stamp) > ttl:
        return _MISS

    if not isinstance(command, str) or not command:
        return None
    if command not in CANDIDATES:
        # 自分が書く値は必ず候補名なので、候補外は「他人が置いた」か「古い形式」。
        # 任意のパスを起動させられる経路を作らないため信用しない (`_cache_dir_ok` 参照)。
        log(f"キャッシュの command ({command}) が候補外のため無視する")
        return _MISS
    path = entry.get("path")
    if shutil.which(command) != path:
        return _MISS
    fingerprint = _fingerprint(path if isinstance(path, str) else None)
    if fingerprint is None or list(fingerprint) != [entry.get("ino"), entry.get("mtime")]:
        # 記録が無い (古い形式) / 読めない / 入れ替わっている — いずれも測り直す
        log("キャッシュした実体の inode / mtime が記録と一致しないため測り直す")
        return _MISS
    return command, subcommand_for(command)


def _save_cache(
    found: tuple[str, tuple[str, ...]] | None, confirmed: bool = False
) -> None:
    """検出結果を記録する。`confirmed` は `--version` の応答を確認できたか (TTL を分ける)。"""
    if not _cache_dir_ok():
        return
    entry: dict[str, object] = {"at": time.time()}
    if found is not None:
        path = shutil.which(found[0])
        entry["command"] = found[0]
        entry["confirmed"] = bool(confirmed)
        entry["path"] = path
        fingerprint = _fingerprint(path)
        if fingerprint is not None:
            entry["ino"], entry["mtime"] = fingerprint
    try:
        write_private(cache_path(), json.dumps(entry))
    except OSError:
        pass  # キャッシュできなくても検出そのものは成立する (次回また probe する)
