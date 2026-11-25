"""検証の対象外のクラウド CLI を使ったことの知らせ (v0.31.0)。

この plugin が照合するのは services/ の CLI (gh / firebase / aws / gcloud / kubectl) だけで、ほかの
クラウド CLI は照合せずに通す。対象外の CLI を使い始めたことに気づけるよう、その CLI が Bash の
コマンドの位置に現れたら、allow のまま `additionalContext` で知らせる (deny はしない。判定表は
変えない)。

- 対象外の CLI の一覧 (`UNVERIFIED_CLIS`) はここ 1 か所に置く。検証に対応したら (services/ に
  足したら) ここから外す。外し忘れは、services の PATTERNS に一致する名前として契約テストが検出する
- 候補は dispatcher の分解 (`_analyze_command` が返すセグメントの列) をそのまま使う。読みは足さない
  (wrapper を剥がす規則・`$(...)` の中を見ない、などは検証の候補と同じ)
- 同じセッション (hook input の `session_id`) の同じエージェントでは、CLI ごとに 1 回だけ知らせる。
  エージェントは hook input の `agent_id` で分ける (サブエージェントの中で発火したときだけ付く。メインは
  無い)。サブエージェントは同じ `session_id` を持ち、`additionalContext` はツールを呼んだエージェントにだけ
  届くので、メインとサブエージェントは互いの記録で黙らない。記録は成功 cache と同じ dir
  (`cache.state_dir`) に CLI ごとのファイルで置く。`session_id` が無い・記録の dir を使えない・
  記録を作れないときは 1 回に絞れないので毎回知らせる (deprecation の案内と同じ倒し方)
- 知らせるかどうか・記録するかどうかは dispatcher が決める (deny のときは知らせず、記録もしない)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from core import cache

# 検証の対象外のクラウド CLI (コマンド語)。Bash のコマンドの位置に現れたら知らせる。
# 検証に対応したら外す (services/ の PATTERNS と重ならないことを契約テストが確かめる)。
UNVERIFIED_CLIS: tuple[str, ...] = ("wrangler", "cf")

# 候補の先頭の語が一覧の名前か。npx で版を固定した形 (`npx wrangler@3 deploy` の wrapper を剥がした
# `wrangler@3 deploy`) も同じ CLI に数える (services/firebase.py の `_CLI` と同じ許し方)。
# 名前の後ろは空白か終端に限る (`wrangler-foo` / `cfg` は別のコマンド)。
_CLI_RE = re.compile(
    r"^(" + "|".join(re.escape(name) for name in UNVERIFIED_CLIS) + r")(?:@\S+)?(?=\s|$)"
)

# 記録のファイル名の頭。成功 cache の破棄 (`cache.invalidate` の `<service>-*.json`) と重ならない。
_FLAG_PREFIX = "unverified-cli-"


def detect(candidates) -> list[str]:
    """候補 (dispatcher の分解のセグメントの形) のうち、先頭の語が対象外の CLI のものの名前。

    名前は最初に現れた順で、重複を除く。
    """
    found: list[str] = []
    for candidate in candidates:
        m = _CLI_RE.match(candidate)
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found


def _flag_path(base: Path, session_id: str, agent: str, name: str) -> Path:
    # session_id / agent_id はファイル名に直接入れない (hook input の値で、パスの区切りを含みうる)。
    material = json.dumps([session_id, agent, name], ensure_ascii=True)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return base / f"{_FLAG_PREFIX}{digest}.flag"


def claim(names, session_id, agent_id=None) -> list[str]:
    """このセッションのこのエージェントでまだ知らせていない名前を返し、知らせたことを記録する。

    記録の単位は「セッション × エージェント × CLI」。`agent_id` は hook input の値で、サブエージェントの
    中で発火したときだけ付く。文字列でない・無いときはメイン (`""`) として数える。
    記録は `O_CREAT | O_EXCL` で作る (並行する hook のどちらか 1 つだけが知らせる)。既にあれば知らせない。
    session_id が文字列でない・空、記録の dir を使えない (`cache.state_dir` が None)、記録を作れない
    (既にある以外の OSError) ときは、1 回に絞れないので知らせる側に倒す。
    """
    names = list(names)
    if not isinstance(session_id, str) or not session_id:
        return names
    base = cache.state_dir()
    if base is None:
        return names
    agent = agent_id if isinstance(agent_id, str) else ""
    fresh: list[str] = []
    for name in names:
        try:
            fd = os.open(
                _flag_path(base, session_id, agent, name),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            continue
        except OSError:
            fresh.append(name)
            continue
        os.close(fd)
        fresh.append(name)
    return fresh


def notice(names) -> str:
    """知らせの本文 (名前は `UNVERIFIED_CLIS` の要素だけ。コマンドの文字列は載せない)。"""
    shown = " / ".join(names)
    return (
        f"[cloud-account-guard] {shown} は検証の対象外の CLI です。cloud-account-guard はこの CLI の"
        "アカウントを照合しておらず、期待外のアカウントで実行されても止めません。"
        "使い続ける場合は、検証の追加を検討する価値があります "
        "(この知らせは同じセッションの同じエージェントでは CLI ごとに 1 回だけ出ます)。"
    )
