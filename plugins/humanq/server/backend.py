"""humanq が beads の CLI と話す層。CLI の形 (argv・出力の形・metadata の置き場・コメントの並び) を知っているのはここだけ。

humanq.py は、ここが返す CLI に依らない形の値だけを読む:

- Row: {id, title, priority, labels, created_at, description, notes, question_raw}
  値は CLI が返したまま (空の既定値を補うのは読む側)。question_raw は humanq の問いの生の値 (dict か文字列か None)
- Comment: {text, created_at}。値は CLI が返したまま。dict でない要素は {text: None, created_at: None} にする
  (本文の無いコメントとして数えない)
- comments() は**必ず古い順で返す**。送り直しの判定・両ラベルの向き・表示する回答の本文は、最後の要素を最新として読む

この段の backend は bd (BdBackend) だけ。
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

# コメントとラベルの書き込みの作者 (README の「回答の形」)
ACTOR = "user/humanq"
# 回答済み (answered) として拾う status。`bd human list` の既定 (closed などの終わったものを除く) に揃える。
# 担当中 (in_progress) のチケットに human を付けて問うのは普通で、open だけにすると回答した途端にどの一覧からも消える
ANSWERED_STATUSES = "open,in_progress,blocked,deferred"


class BdError(RuntimeError):
    """CLI を起動できない・失敗した・出力を読めない。"""


def run_bd(repo: str, *args: str) -> str:
    """bd を repo で 1 回打ち、stdout を返す。cwd は渡さず -C で repo を指す。"""
    try:
        p = subprocess.run(["bd", "-C", repo, *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BdError(f"bd を起動できない: {e}") from e
    if p.returncode != 0:
        raise BdError((p.stderr or p.stdout).strip()[:800] or f"bd exit {p.returncode}")
    return p.stdout


def parse_bd_list(out: str) -> list:
    """bd の --json の出力を list に読む。list 以外 (envelope の {"issues": [...]} を含む) は BdError。"""
    out = out.strip()
    if not out:
        return []
    try:
        v = json.loads(out)
    except json.JSONDecodeError:
        if re.fullmatch(r"No [^\n]*", out):  # 該当 0 件のとき案内文 1 行だけを返す版がある
            return []
        raise BdError(f"bd の出力が JSON ではない: {out[:200]}") from None
    if v is None:
        return []  # 0 件の list は null を返す
    if not isinstance(v, list):
        raise BdError(f"bd の出力が list ではない ({type(v).__name__}): {out[:200]}")
    return v


def comment_of(c) -> dict:
    """CLI のコメント 1 件を Comment にする。"""
    if not isinstance(c, dict):
        return {"text": None, "created_at": None}
    return {"text": c.get("text"), "created_at": c.get("created_at")}


class BdBackend:
    """bd (beads) の CLI。run は bd を 1 回打つ関数 (repo, *args) -> stdout で、呼ぶたびに引く
    (humanq.py は自分の bd を引く関数を渡し、テストの humanq.bd の差し替えをここまで届かせる)。"""

    def __init__(self, run: Callable[..., str] = run_bd):
        self._run = run

    @staticmethod
    def question_raw(metadata):
        """bd のチケットの metadata から、humanq の問いの生の値を取り出す (README の「問いの書き方」: metadata.humanq)。"""
        return metadata.get("humanq") if isinstance(metadata, dict) else None

    @classmethod
    def row(cls, i: dict) -> dict:
        """bd のチケット 1 件 (list / human list の要素) を Row にする。dict でなければ AttributeError (呼ぶ側が読めないことにする)。"""
        return {
            "id": i.get("id"),
            "title": i.get("title"),
            "priority": i.get("priority"),
            "labels": i.get("labels"),
            "created_at": i.get("created_at"),
            "description": i.get("description"),
            "notes": i.get("notes"),
            "question_raw": cls.question_raw(i.get("metadata")),
        }

    def json(self, repo: str, *args: str) -> list:
        return parse_bd_list(self._run(repo, *args, "--json"))

    def list_human(self, repo: str) -> list[dict]:
        return [self.row(i) for i in self.json(repo, "human", "list")]

    def list_answered(self, repo: str) -> list[dict]:
        return [self.row(i) for i in self.json(repo, "list", "--status", ANSWERED_STATUSES, "--label", "answered",
                                               "--flat", "-n", "0")]

    def comments(self, repo: str, iid: str) -> list[dict]:
        # bd はコメントを古い順で返す (bd 1.3.4 で実測。偽の bd も同じ)。並べ替えずにそのまま渡す
        return [comment_of(c) for c in self.json(repo, "comments", iid)]

    def add_comment(self, repo: str, iid: str, path: str) -> None:
        self._run(repo, "--actor", ACTOR, "comments", "add", iid, "-f", path)

    def label(self, repo: str, op: str, iid: str, name: str) -> None:
        """op は add | remove。"""
        self._run(repo, "--actor", ACTOR, "label", op, iid, name)
