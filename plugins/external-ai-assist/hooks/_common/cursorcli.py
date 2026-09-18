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

そこで **候補を順に見て、最初に「起動できる」ものを使う**:

| 順 | コマンド | 起動時のサブコマンド | 理由 |
|---|---|---|---|
| 1 | `cursor-agent` | なし | Agent CLI 本体の名前。曖昧さが無い |
| 2 | `agent` | なし | 同じ実体が置かれることがある名前 |
| 3 | `cursor` | `agent` | 0.10.0 までの起動形。シム / IDE ランチャーのどちらでもありうる |

- 候補は `--version` で応答するかを確認してから採用する (`_probe`)。壊れたコマンドを
  掴んで長時間待つ経路を、短い probe に置き換えるのが狙い。
  **「応答を確認できない」(timeout) は失格にしない** — 起動の遅い本物 (node ベースの
  CLI は cold start に数秒かかりうる) を切ってレビュー機能が黙って止まるほうが、
  0.10.0 までの「掴んでから失敗を待つ」より悪いため。応答が確認できた候補を優先し、
  どれも確認できなければ最初に見つかった候補を使う (= 0.10.0 と同じ扱い)
- 結果は `$TMPDIR/external-ai-assist/cursorcli.json` に TTL 付きでキャッシュする
  (`is_available()` は編集ツールのたびに呼ばれるので、毎回 probe しない)。
  記録した実体パスが `which` の結果と食い違ったらキャッシュを捨てて測り直す
- `EXTERNAL_AI_CURSOR_COMMAND` を設定するとその値を無条件で使う (probe もしない)。
  検出が環境に合わない場合の逃げ道で、テストもこれで実体を固定する。**basename が
  `cursor` のときだけ `agent` サブコマンドを足す** (上の表と同じ規則)

**この probe では IDE ランチャーと Agent CLI を見分けられない** (どちらも
`--version` に 0 で応答する)。見分けは上の**検出順**が担っていて、「IDE ランチャー
しか無い環境」は順序では解けない。`--help` の内容で判定する案は、実機の出力を確認
できていないため採らない (誤判定すると機能が黙って止まる)。**未ログイン状態の検出も
同様に未実装** — `--version` はログイン状態に関係なく応答し、実際の失敗文言を実機で
確認できていないため、推測でパターンを書くと正常なレビュー本文を誤検出して機能を
止めうる。どちらも実機確認が済んでから入れる (内部バックログ)。
"""
from __future__ import annotations

import json
import os
import shutil
import time

from . import subproc
from .flock import write_private
from .hooklog import make_logger

NAME = "cursor"

#: 検出できなかったときに使う従来の起動形 (0.10.0 までと同じ)。
BINARY = "cursor"

#: 検出順 (コマンド名)。起動時のサブコマンドは `subcommand_for()` が決める。
CANDIDATES = ("cursor-agent", "agent", "cursor")

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
#: probe が要るのはキャッシュが無いときだけ (TTL 1 時間)。
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


def resolve() -> tuple[str, tuple[str, ...]] | None:
    """使える Agent CLI の `(コマンド, サブコマンド)`。見つからなければ None。

    プロセス内で 1 回だけ計算する (hook は 1 回の呼び出しごとに使い捨てプロセス)。
    """
    if _RESOLVED_KEY not in _RESOLVED:
        _RESOLVED[_RESOLVED_KEY] = _resolve_uncached()
    return _RESOLVED[_RESOLVED_KEY]


def is_available() -> bool:
    return resolve() is not None


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


def _resolve_uncached() -> tuple[str, tuple[str, ...]] | None:
    override = os.environ.get(ENV_COMMAND, "").strip()
    if override:
        if shutil.which(override) is None:
            log(f"{ENV_COMMAND}={override} が見つからないため cursor を使わない")
            return None
        return override, subcommand_for(override)

    cached = _load_cache()
    if cached is not _MISS:
        return cached  # type: ignore[return-value]

    found = _detect()
    _save_cache(found)
    return found


def _detect() -> tuple[str, tuple[str, ...]] | None:
    """候補を順に probe して使うものを決める。

    **probe の「応答なし (timeout / 予算切れ)」は候補を失格にしない。** 判定は 3 値で、

    | probe の結果 | 扱い |
    |---|---|
    | `PROBE_OK` (0 終了 + 出力あり) | 即採用 |
    | `PROBE_FAILED` (非 0 終了 / 出力なし) | 失格。次の候補へ |
    | `PROBE_UNKNOWN` (timeout / 予算切れ) | **保留**。`PROBE_OK` の候補が 1 つも無ければ使う |

    timeout を失格にすると、起動の遅い本物 (node ベースの CLI は cold start に数秒
    かかりうる) を「壊れている」と誤判定して、レビュー機能が黙って止まる方向に倒れる。
    **機能を黙って落とすほうが、0.10.0 までと同じ「掴んでから失敗を待つ」より悪い**ので、
    応答が確認できた候補を優先しつつ、どれも確認できなければ従来どおり `which` の結果を
    使う (0.10.0 の挙動)。
    """
    deadline = time.monotonic() + PROBE_BUDGET_SEC
    fallback: str | None = None
    for command in CANDIDATES:
        path = shutil.which(command)
        if path is None:
            continue
        verdict = _probe(path, deadline)
        if verdict == PROBE_OK:
            return command, subcommand_for(command)
        if verdict == PROBE_UNKNOWN:
            if fallback is None:
                fallback = command
            log(f"{command} ({path}) の応答を確認できなかった (保留)")
            continue
        log(f"{command} ({path}) が --version に応答しないため候補から外す")
    if fallback is not None:
        log(f"応答を確認できた候補が無いため {fallback} を使う (0.10.0 と同じ扱い)")
        return fallback, subcommand_for(fallback)
    return None


def _probe(path: str, deadline: float) -> str:
    """`<path> --version` の応答を `PROBE_*` で返す。

    **`agent` サブコマンドは付けない**。`cursor` が IDE ランチャーだった場合に
    サブコマンドを渡すと引数の解釈が実装依存になるため、最も無害な形だけを試す
    (どちらの実体でも `--version` は即座に返る)。
    """
    remaining = min(PROBE_TIMEOUT_SEC, deadline - time.monotonic())
    if remaining <= 0:
        log("検出の予算を使い切ったため、残りの候補は確認しない")
        return PROBE_UNKNOWN
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
        return PROBE_UNKNOWN  # timeout / 起動できない
    if result.returncode == 0 and (result.stdout or "").strip():
        return PROBE_OK
    return PROBE_FAILED


# --------------------------------------------------------------------------
# キャッシュ
# --------------------------------------------------------------------------


def cache_path() -> str:
    root = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(root, "external-ai-assist", "cursorcli.json")


def _load_cache():
    """有効なキャッシュがあれば `(コマンド, サブコマンド)` か None、無ければ `_MISS`。

    無効とみなす条件: 読めない / 壊れている / TTL 超過 / 記録した実体パスが現在の
    `which` の結果と違う (cursor を入れ直した・PATH が変わった)。
    """
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
    ttl = CACHE_TTL_SEC if isinstance(command, str) and command else NEGATIVE_CACHE_TTL_SEC
    if time.time() - float(stamp) > ttl:
        return _MISS

    if not isinstance(command, str) or not command:
        return None
    if shutil.which(command) != entry.get("path"):
        return _MISS
    return command, subcommand_for(command)


def _save_cache(found: tuple[str, tuple[str, ...]] | None) -> None:
    entry: dict[str, object] = {"at": time.time()}
    if found is not None:
        entry["command"] = found[0]
        entry["path"] = shutil.which(found[0])
    try:
        write_private(cache_path(), json.dumps(entry))
    except OSError:
        pass  # キャッシュできなくても検出そのものは成立する (次回また probe する)
