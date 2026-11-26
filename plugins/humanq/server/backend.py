"""humanq が beads の CLI と話す層。CLI の形 (argv・出力の形・問いの置き場・コメントの並び) を知っているのはここだけ。

humanq.py は、ここが返す CLI に依らない形の値だけを読む:

- Row: {id, title, priority, labels, created_at, description, notes, question_raw}
  値は CLI が返したまま (空の既定値を補うのは読む側)。question_raw は humanq の問いの生の値 (dict か文字列か None)
- Comment: {text, created_at}。値は CLI が返したまま。dict でない要素は {text: None, created_at: None} にする
  (本文の無いコメントとして数えない)
- comments() は**必ず古い順で返す**。送り直しの判定・両ラベルの向き・表示する回答の本文は、最後の要素を最新として読む
- load(repo) は画面の読み 1 回分: (判断待ちの Row の list, 回答済みの Row の list)。回答済みの Row には thread
  (そのチケットの Comment の list。古い順。読めなければ None) が付く

backend は 2 つ: bd (BdBackend。読み書き) と br (BrBackend。この段では読みだけ)。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable

# コメントとラベルの書き込みの作者 (README の「回答の形」)
ACTOR = "user/humanq"
# 回答済み (answered) として拾う status。`bd human list` の既定 (closed などの終わったものを除く) に揃える。
# 担当中 (in_progress) のチケットに human を付けて問うのは普通で、open だけにすると回答した途端にどの一覧からも消える
ANSWERED_STATUSES = "open,in_progress,blocked,deferred"


class BdError(RuntimeError):
    """CLI を起動できない・失敗した・出力を読めない。"""


class CliExit(BdError):
    """CLI が非 0 で終わった。code は exit code。"""

    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


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

    def load(self, repo: str) -> tuple[list[dict], list[dict]]:
        """判断待ちの一覧 → 回答済みの一覧 → 回答済みごとのコメント、の順に打つ (サーバーが打つ bd の argv の列の固定)。
        コメントを読めないチケットの thread は None (そのチケットだけ、回答の本文と署名が空になる)。"""
        human = self.list_human(repo)
        answered = []
        for r in self.list_answered(repo):
            try:
                thread = self.comments(repo, r["id"])
            except BdError:
                thread = None
            answered.append({**r, "thread": thread})
        return human, answered

    def add_comment(self, repo: str, iid: str, path: str) -> None:
        self._run(repo, "--actor", ACTOR, "comments", "add", iid, "-f", path)

    def label(self, repo: str, op: str, iid: str, name: str) -> None:
        """op は add | remove。"""
        self._run(repo, "--actor", ACTOR, "label", op, iid, name)


# ---- br (beads_rust。0.7.6 で実測) ------------------------------------------------------------------------------
# bd との違い: -C が無い (cwd から上へ .beads を探す)・human サブコマンドが無い・list は envelope で返す・
# list -s はカンマ区切りを受け付けず繰り返す・問いは metadata ではなく agent_context (JSON の文字列) の key humanq・
# コメントの並びは決まっていない (id は単調に増える int)・show は id が 1 つでも無いと全体が exit 3。

# br が cwd より優先して読む DB・.beads・JSONL の場所の env (br 0.7.6 の `br capabilities` の env_vars と実測。
# BEADS_DIR は文書に無いが効く)。起動するときに外す: 残っていると、登録したプロジェクトではない DB を黙って読む
BR_TARGET_ENV = ("BEADS_DB", "BD_DB", "BD_DATABASE", "BEADS_DIR", "BEADS_JSONL")
BR_NOT_FOUND = 3            # show に渡した id が 1 つでも無い (ISSUE_NOT_FOUND) ときの exit code
BR_TIMEOUT = 60
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def run_br(repo: str, *args: str) -> str:
    """br を 1 回打ち、stdout を返す。br に -C は無いので repo を cwd にする (プロジェクトごとに版を切り替える
    仕組みも、そのプロジェクトの dir で起動するのでそのまま効く)。DB の場所を指す env (BR_TARGET_ENV) は渡さない。"""
    env = {k: v for k, v in os.environ.items() if k not in BR_TARGET_ENV}
    try:
        p = subprocess.run(["br", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=BR_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise BdError(f"br が {BR_TIMEOUT} 秒で終わらない: {e}") from e
    except OSError as e:
        raise BdError(f"br を起動できない (PATH を確認): {e}") from e
    if p.returncode != 0:
        raise CliExit((p.stderr or p.stdout).strip()[:800] or f"br exit {p.returncode}", p.returncode)
    return p.stdout


def _loads(out: str, what: str):
    try:
        return json.loads(out)
    except (ValueError, RecursionError):    # RecursionError は深い入れ子 (json の parser の限界)
        raise BdError(f"{what}の出力が JSON ではない: {out.strip()[:200]}") from None


def parse_br_list(out: str) -> list:
    """br の list --json の envelope ({"issues": [...], "has_more": ...}) から issues を取り出す。0 件も同じ形。
    envelope でないもの・JSON でないもの・途中で切れた一覧 (has_more。--limit 0 で読むので普通は起きない) は BdError。"""
    v = _loads(out, "br list ")
    if not isinstance(v, dict) or not isinstance(v.get("issues"), list):
        raise BdError(f"br list の出力が envelope ({{\"issues\": [...]}}) ではない: {out.strip()[:200]}")
    if v.get("has_more"):
        raise BdError(f"br list の一覧が途中で切れている (has_more。{len(v['issues'])} 件まで)")
    return v["issues"]


def parse_br_show(out: str) -> list:
    v = _loads(out, "br show ")
    if not isinstance(v, list):
        raise BdError(f"br show の出力が list ではない ({type(v).__name__}): {out.strip()[:200]}")
    return v


def _when(v) -> datetime | None:
    """コメントの created_at を時刻にする。読めなければ None (並べ替えで一番古い側に置く)。"""
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _comment_order(c) -> tuple:
    """br のコメントの並べ替えの key: created_at → id (同じ秒のコメントは、単調に増える id で決める)。
    読めない時刻・id のものは、それぞれの段で古い側に置く (最新の回答と読まない)。"""
    t = _when(c.get("created_at")) if isinstance(c, dict) else None
    cid = c.get("id") if isinstance(c, dict) else None
    has_id = isinstance(cid, int)
    return (t is not None, t or _EPOCH, has_id, cid if has_id else 0)


def br_thread(raw) -> list[dict] | None:
    """br の show のチケットの comments を、古い順の Comment の list にする。key が無い・null は 0 件 (br はコメントの
    無いチケットで key を出さない)。list でなければ None (読めない)。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    return [comment_of(c) for c in sorted(raw, key=_comment_order)]


def _br_id(i) -> str:
    """一覧の要素の id。show の引数にするので、文字列でない・空・- で始まる (flag に読まれる) ものは BdError。"""
    iid = i.get("id") if isinstance(i, dict) else None
    if not isinstance(iid, str) or not iid or iid.startswith("-"):
        raise BdError(f"br list の要素の id を読めない: {str(i)[:200]}")
    return iid


class BrBackend:
    """br (beads_rust) の CLI。この段では読みだけ (load)。run は br を 1 回打つ関数 (repo, *args) -> stdout で、呼ぶたびに引く。

    1 プロジェクトの読みは 3 プロセス: 判断待ちの list → 回答済みの list → 両方の id をまとめた show。問い
    (agent_context) とコメントは show から取る (br の schema の文書は agent_context を「list には出ない」としている)。
    読みの flag は br の既定のまま (JSONL の方が新しければ DB へ取り込む auto-import も、利用者が打ったときと同じに効かせる)。"""

    def __init__(self, run: Callable[..., str] = run_br):
        self._run = run

    @staticmethod
    def question_raw(agent_context):
        """br のチケットの agent_context (JSON の文字列) から、humanq の問いの生の値 (top-level の key humanq) を取り出す。
        JSON の object でない・読めない・空・key が無いときは None。object で返す版にも備えて dict はそのまま読む。"""
        v = agent_context
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (ValueError, RecursionError):
                return None
        return v.get("humanq") if isinstance(v, dict) else None

    @classmethod
    def row(cls, listed: dict, shown: dict, thread: bool = False) -> dict:
        """一覧の要素と show の要素から Row を作る (同じ key は show の値)。問いは show の agent_context だけから読む。
        thread を付けると、show の comments を古い順に並べた thread を足す。"""
        i = {**listed, **shown}
        r = {
            "id": i.get("id"),
            "title": i.get("title"),
            "priority": i.get("priority"),
            "labels": i.get("labels"),
            "created_at": i.get("created_at"),
            "description": i.get("description"),
            "notes": i.get("notes"),
            "question_raw": cls.question_raw(shown.get("agent_context")),
        }
        if thread:
            r["thread"] = br_thread(shown.get("comments"))
        return r

    def _list(self, repo: str, label: str) -> list:
        """label の付いたチケットの一覧。status では絞らない (-s を付けない): br の list の既定は closed と tombstone
        だけを除き、組み込みの deferred・draft・pinned も、任意の文字列の status (br update --status は宣言なしで受け
        付ける) も出す。利用者が `br list -l human` で見る判断待ちと同じものを、画面と hook の件数に出す (br 0.7.6 で実測)。"""
        return parse_br_list(self._run(repo, "list", "-l", label, "--limit", "0", "--json"))

    def load(self, repo: str) -> tuple[list[dict], list[dict]]:
        """一覧から show までの間にチケットが消えると、show は exit 3 で全体が失敗する。そのときは 1 回だけ一覧から
        読み直し、それでも失敗したら BdError。どちらの一覧も空なら show は打たない。"""
        for attempt in (1, 2):
            human, answered = self._list(repo, "human"), self._list(repo, "answered")
            ids = list(dict.fromkeys(_br_id(i) for i in human + answered))
            if not ids:
                return [], []
            try:
                shown = parse_br_show(self._run(repo, "show", *ids, "--json"))
                break
            except CliExit as e:
                if e.code != BR_NOT_FOUND:
                    raise
                if attempt == 2:
                    raise BdError(f"br show が一覧を読み直しても失敗した: {e}") from e
        by = {}
        for d in shown:
            if not isinstance(d, dict) or not isinstance(d.get("id"), str):
                raise BdError(f"br show の要素を読めない: {str(d)[:200]}")
            by[d["id"]] = d
        lost = [i for i in ids if i not in by]
        if lost:
            raise BdError(f"br show の出力に {lost[0]} が無い")
        return ([self.row(i, by[i["id"]]) for i in human],
                [self.row(i, by[i["id"]], thread=True) for i in answered])
