"""humanq が beads の CLI と話す層。CLI の形 (argv・出力の形・問いの置き場・コメントの並び) を知っているのはここだけ。

humanq.py は、ここが返す CLI に依らない形の値だけを読む:

- Row: {id, title, priority, labels, created_at, description, notes, question_raw}
  値は CLI が返したまま (空の既定値を補うのは読む側)。question_raw は humanq の問いの生の値 (dict か文字列か None)
- Comment: {text, created_at}。値は CLI が返したまま。dict でない要素は {text: None, created_at: None} にする
  (本文の無いコメントとして数えない)
- comments() は**必ず古い順で返す**。送り直しの判定・両ラベルの向き・表示する回答の本文は、最後の要素を最新として読む
- load(repo, memo=None) は画面の読み 1 回分: (判断待ちの Row の list, 回答済みの Row の list)。回答済みの Row と、判断待ちの
  Row のうち保留か情報待ちのラベル (THREAD_LABELS) の付いたものには thread (そのチケットの Comment の list。古い順。読めなければ
  None) が付く (保留の戻る日と情報待ちの足りない情報はコメントが正本で、ラベルはコメントを読むチケットを絞るための印。ラベルの無い
  判断待ちのコメントは読まない)。memo (ThreadMemo) を渡すと、回答済みと保留・情報待ちの thread を覚えから使い、CLI で読むのは
  覚えの無い・古いもの (保留・情報待ちは時間の上限まで) だけ (bd だけ)
- 書き込み (回答・取り消し・保留・保留の解除・情報待ち・情報待ちの取り下げ) の読み直しは pending(repo, iid) (判断待ちの Row の list。iid の問いを照合に使う) と
  list_answered(repo) と comments(repo, iid)。pending の Row に thread が付いていれば、送り直しの判定と署名の照合は
  その thread で行う (comments を打たない)。書き込みは add_comment と label (op は add | remove)
- 見通し (読むだけ。判断待ちの読みとは別に、画面が見通しのタブを開いたときだけ読む) は status_counts(repo) と ready(repo)。
  status_counts は (status, 件数) の list (CLI が返した並び。数える範囲は CLI の既定のまま)。ready は ReadyRow の list
  ({id, title, priority, status, labels, created_at}。値は CLI が返したまま。並びは CLI のもので、読む側が並べ直す)。
  ready は件数の上限を外して読む (上位を決めるのは読む側)

backend は 2 つ: bd (BdBackend) と br (BrBackend)。どちらも読み書きする。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable

# コメントとラベルの書き込みの作者 (README の「回答の形」)
ACTOR = "user/humanq"
# 保留のラベル (README の「回答の形」)。付いた判断待ちだけ、読みのときにコメントを読む
SNOOZE_LABEL = "snoozed"
# 情報待ちのラベル (README の「回答の形」)。保留のラベルと同じく、付いた判断待ちだけ読みのときにコメントを読む
NEEDINFO_LABEL = "needs-info"
# 読みのときにコメントを読む判断待ちの印 (どちらも、正本はコメントで、ラベルは読むチケットを絞る印)
THREAD_LABELS = (SNOOZE_LABEL, NEEDINFO_LABEL)


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
    except RecursionError:              # 深い入れ子 (json の parser の限界)。読めない出力として扱う (_loads と同じ)
        raise BdError(f"bd の出力が JSON ではない (入れ子が深すぎる): {out[:200]}") from None
    except json.JSONDecodeError:
        if re.fullmatch(r"No [^\n]*", out):  # 該当 0 件のとき案内文 1 行だけを返す版がある
            return []
        raise BdError(f"bd の出力が JSON ではない: {out[:200]}") from None
    except ValueError:                  # JSONDecodeError でない ValueError (桁の多すぎる整数など。_loads と同じ扱い)
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


def labeled(row, name: str) -> bool:
    """Row (か CLI の要素) に name のラベルが付いているか。labels が list でなければ付いていない (文字列の部分一致にしない)。"""
    labels = row.get("labels") if isinstance(row, dict) else None
    return isinstance(labels, list) and name in labels


def watched(row) -> bool:
    """判断待ちの Row に、読みのときにコメントを読む印 (THREAD_LABELS: 保留か情報待ちのラベル) が付いているか。"""
    return any(labeled(row, name) for name in THREAD_LABELS)


class ThreadMemo:
    """回答済みと、保留か情報待ちのラベルの付いた判断待ちの thread (bd では 1 件ごとに `bd comments` を 1 プロセス) の覚え。key は
    (repo, id)。サーバー (humanq.Store) が 1 つ持ち、BdBackend.load に渡す。読むたびに打つと、回答済みと保留の件数に比例して
    読み (画面の 15 秒ごとの読み・回答の後の読み) が遅くなり、SessionStart の hook の件数の読み (8 秒) が時間切れになる
    (本物の bd 1.3.4 で 1 件あたり約 0.2 秒)。

    - 覚えた thread は ttl 秒の間、CLI を打たずに使う。読みが thread から読むもの (回答済みのカードの回答の本文と署名・
      両方のラベルの向き・保留と情報待ちの判定) はどれも [humanq] のコメントだけで決まり、[humanq] のコメントはこのサーバーだけが
      書くので、サーバー自身の書き込み (回答・取り消し・保留・保留の解除・情報待ち・その取り下げ) がそのチケットの覚えを捨てれば (drop)、変化は拾える。
      ttl は、ほかで書かれたコメント (別の端末からの同期など) を拾い直すまでの上限。ラベルと問いは毎回の一覧で読む
      (エージェントがラベルを外す・問いを書き換えるのは、thread を読まなくても分かる)。書き込みの照合 (送り直しの判定・
      取り消しの署名) は覚えを使わず、書き込みのロックの下で読み直す
    - 保留・情報待ちは、1 回の読みで CLI を打つのは budget 秒まで (覚えの無いものから先に読む)。間に合わなかったものは、古い覚えが
      あればそれを使い、無ければ thread を None (戻る日・足りない情報を読めない = 判断待ちに出す) にして、left に残す (サーバーが裏で読む)。
      回答済みには上限を当てない (覚えの無い・古いものは全部読む)
    - 読みの途中で書き込みが覚えを捨てたら、その読みが読んだ古い thread で覚え直さない (repo ごとの世代で照合する)"""

    def __init__(self, ttl: float, budget: float, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.budget, self.clock = ttl, budget, clock
        self._lock = threading.Lock()
        self._memo: dict[tuple[str, str], tuple[float, list]] = {}
        self._gen: dict[str, int] = {}
        self._left: dict[str, list[str]] = {}

    def get(self, repo: str, iid: str) -> tuple[list | None, bool]:
        """(覚えた thread (無ければ None), ttl の内か)。"""
        with self._lock:
            hit = self._memo.get((repo, iid))
        if hit is None:
            return None, False
        return hit[1], self.clock() - hit[0] < self.ttl

    def gen(self, repo: str) -> int:
        """読み始める前に取る世代。put に渡す。"""
        with self._lock:
            return self._gen.get(repo, 0)

    def put(self, repo: str, iid: str, thread: list, gen: int) -> None:
        """読んだ thread を覚える。読み始めてから repo の覚えが捨てられていたら (世代が違えば) 覚えない。"""
        with self._lock:
            if self._gen.get(repo, 0) == gen:
                self._memo[(repo, iid)] = (self.clock(), thread)

    def drop(self, repo: str, iid: str) -> None:
        """iid の覚えを捨て、repo の世代を進める (書き込みの後に呼ぶ)。同じ DB に届く別の登録 (bd の linked worktree は本体と
        まとめない) の同じチケットの覚えも捨て、その登録の世代も進める (別の DB の同じ id なら、読み直すだけ)。"""
        with self._lock:
            for r in {k[0] for k in self._memo if k[1] == iid} | {repo}:
                self._memo.pop((r, iid), None)
                self._gen[r] = self._gen.get(r, 0) + 1

    def keep(self, repo: str, ids) -> None:
        """repo の覚えを ids (今の回答済みと、保留か情報待ちのラベルの付いた判断待ち) だけにする。どちらの一覧からも外れたチケットの
        覚えは捨てる (もう一度回答済みか保留になったら読み直す。ほかで取り消して答え直されたものも、間の読みで外れていれば拾う)。"""
        ids = set(ids)
        with self._lock:
            for k in [k for k in self._memo if k[0] == repo and k[1] not in ids]:
                del self._memo[k]

    def set_left(self, repo: str, ids: list[str]) -> None:
        with self._lock:
            if ids:
                self._left[repo] = list(ids)
            else:
                self._left.pop(repo, None)

    def take_left(self, repo: str) -> list[str]:
        """最後の読みが時間の上限で読まなかったチケット (取ると空になる)。"""
        with self._lock:
            return self._left.pop(repo, [])


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

    def pending(self, repo: str, iid: str) -> list[dict]:
        """回答の書き込みが照合に使う判断待ちの Row。bd は判断待ちの一覧の全件 (問いは一覧の metadata にある)。
        thread は付けない (コメントは書き込みの側が comments で読む)。"""
        return self.list_human(repo)

    def list_answered(self, repo: str) -> list[dict]:
        # status では絞らない (--status を付けない): bd list の既定は bd human list と同じ status を出す (closed・pinned・
        # 完了や凍結の独自の status を除く。hooked や独自の status は出る。bd 1.3.4 で実測)。回答した判断待ちは、
        # 判断待ちに出ていた status のままここに出る。status を列挙すると、列挙に無い status のチケットはどの一覧からも消える
        return [self.row(i) for i in self.json(repo, "list", "--label", "answered", "--flat", "-n", "0")]

    def comments(self, repo: str, iid: str) -> list[dict]:
        # bd はコメントを古い順で返す (bd 1.3.4 で実測。偽の bd も同じ)。並べ替えずにそのまま渡す
        return [comment_of(c) for c in self.json(repo, "comments", iid)]

    def load(self, repo: str, memo: ThreadMemo | None = None) -> tuple[list[dict], list[dict]]:
        """判断待ちの一覧 → 回答済みの一覧 → 回答済みごとのコメント → 保留か情報待ちのラベルの付いた判断待ちごとのコメント、の順に
        打つ (サーバーが打つ bd の argv の列の固定。どちらのラベルも無ければ、判断待ちのコメントは打たない)。
        コメントを読めないチケットの thread は None (回答済みはそのチケットだけ回答の本文と署名が空になり、保留・情報待ちは戻る日・
        足りない情報を読めないので判断待ちに出す)。bd の引数に渡せない id (_passable) の判断待ちは、打たずに None。
        memo を渡すと、回答済みと保留・情報待ちの thread は覚え (ThreadMemo) から使い、覚えの内のものは打たない (打つ列からそのチケットの
        コメントの読みが抜ける)。回答済みは覚えの無い・古いものを全部読み、保留・情報待ちは覚えの無い・古いものだけを memo.budget 秒まで
        読む。両方のラベルのチケットは、回答済みの読みで覚えたものを判断待ちの側でも使う (同じ読みで 2 回は打たない)。"""
        human = self.list_human(repo)
        listed = self.list_answered(repo)
        marked_rows = [r for r in human if watched(r)]
        if memo is not None:
            memo.keep(repo, [r["id"] for r in listed] + [r["id"] for r in marked_rows if _passable(r["id"])])
        answered = [{**r, "thread": self._answered_thread(repo, r["id"], memo)} for r in listed]
        if memo is None:
            for r in marked_rows:
                r["thread"] = self._thread(repo, r["id"])
            return human, answered
        todo = []
        for r in marked_rows:
            if not _passable(r["id"]):
                r["thread"] = None
                continue
            r["thread"], fresh = memo.get(repo, r["id"])
            if not fresh:
                todo.append(r)
        todo.sort(key=lambda r: r["thread"] is not None)        # 覚えの無いもの (戻る日を読めないもの) から先に読む
        deadline = memo.clock() + memo.budget
        left = []
        for r in todo:
            if memo.clock() >= deadline:
                left.append(r["id"])
                continue
            gen = memo.gen(repo)
            try:
                thread = self.comments(repo, r["id"])
            except BdError:
                continue            # 読めない: 古い覚えがあればそれ、無ければ None のまま (次の読みで読み直す)
            r["thread"] = thread
            memo.put(repo, r["id"], thread, gen)
        memo.set_left(repo, left)
        return human, answered

    def _answered_thread(self, repo: str, iid, memo: ThreadMemo | None) -> list[dict] | None:
        """回答済みの thread を 1 件。読めなければ None。memo を渡すと、覚えの内ならそれを使い、読めたら覚える。
        読めなかったときは古い覚えを使わない (覚えずに読んだときと同じく、回答の本文と署名を空にする)。"""
        if memo is not None:
            hit, fresh = memo.get(repo, iid)
            if fresh:
                return hit
            gen = memo.gen(repo)
        try:
            thread = self.comments(repo, iid)
        except BdError:
            return None
        if memo is not None:
            memo.put(repo, iid, thread, gen)
        return thread

    def _thread(self, repo: str, iid) -> list[dict] | None:
        """保留・情報待ちの thread を 1 件読む。渡せない id は打たずに None、読めなければ None。"""
        if not _passable(iid):
            return None
        try:
            return self.comments(repo, iid)
        except BdError:
            return None

    def add_comment(self, repo: str, iid: str, path: str) -> None:
        self._run(repo, "--actor", ACTOR, "comments", "add", iid, "-f", path)

    def label(self, repo: str, op: str, iid: str, name: str) -> None:
        """op は add | remove。"""
        self._run(repo, "--actor", ACTOR, "label", op, iid, name)

    # ---- 見通し (読むだけ) ----

    def status_counts(self, repo: str) -> list[tuple[str, int]]:
        """状態別の件数 (`bd count --by-status --json`。{total, groups, schema_version} を返す)。数える範囲は bd の既定のまま。"""
        return parse_count_groups(self._run(repo, "count", "--by-status", "--json"), "bd count ")

    def ready(self, repo: str) -> list[dict]:
        """次に着手できるチケット (`bd ready --limit 0 --json`。--limit 0 は件数の上限を外す。0 件の null も読む)。"""
        return ready_rows(self.json(repo, "ready", "--limit", "0"))


# ---- 見通しの読み (bd と br で共通の形) -----------------------------------------------------------------------------

def ready_rows(items: list) -> list[dict]:
    """ready の要素を ReadyRow の list にする。dict でない・id が空でない文字列でない要素は落とす (1 件の崩れで、そのプロジェクトの
    見通し全体を読めなくしない。落とした件数は数えない)。"""
    rows = []
    for i in items:
        if not isinstance(i, dict) or not isinstance(i.get("id"), str) or not i["id"]:
            continue
        rows.append({"id": i["id"], "title": i.get("title"), "priority": i.get("priority"), "status": i.get("status"),
                     "labels": i.get("labels"), "created_at": i.get("created_at")})
    return rows


def parse_count_groups(out: str, what: str) -> list[tuple[str, int]]:
    """count の --json ({total, groups: [{group, count}, ...]}) を (status, 件数) の list にする (groups の並びのまま)。
    groups が null なら 0 件 (bd は空の list を null で返すことがある)。JSON でない・object でない・groups が list でない・
    要素の group が文字列でない・count が 0 以上の int でない (真偽値を含む) ときは BdError。total は読まない (読む側が足す)。"""
    v = _loads(out, what)
    if not isinstance(v, dict) or "groups" not in v:
        raise BdError(f"{what}の出力が {{total, groups}} の形ではない: {out.strip()[:200]}")
    groups = v["groups"]
    if groups is None:
        return []
    if not isinstance(groups, list):
        raise BdError(f"{what}の groups が list ではない ({type(groups).__name__}): {out.strip()[:200]}")
    pairs = []
    for g in groups:
        name = g.get("group") if isinstance(g, dict) else None
        n = g.get("count") if isinstance(g, dict) else None
        if not isinstance(name, str) or type(n) is not int or n < 0:
            raise BdError(f"{what}の groups の要素を読めない: {str(g)[:200]}")
        pairs.append((name, n))
    return pairs


def _loads(out: str, what: str):
    try:
        return json.loads(out)
    except (ValueError, RecursionError):    # RecursionError は深い入れ子 (json の parser の限界)
        raise BdError(f"{what}の出力が JSON ではない: {out.strip()[:200]}") from None


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
    _log_warnings(p.stderr)
    return p.stdout


def _log_warnings(stderr: str | None) -> None:
    """br が exit 0 のまま stderr に出す警告 (例: 書き込みは DB に入ったが、JSONL の自動 export に失敗した) を、サーバーの
    stderr (server.log) に 1 行ずつ残す。回答の成否も画面も変えない: 回答は DB に入っていて、JSONL には次に誰かが br で書くと入る。
    残さないと、git で共有する JSONL に入っていないことが誰にも見えない。"""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if "Warning:" in ln]
    for ln in lines[:5]:
        print(f"humanq: br: {ln[:300]}", file=sys.stderr, flush=True)


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


def _passable(iid) -> bool:
    """br の引数 (show・comments add・label) に渡せる id か。文字列でない・空・- で始まる (flag に読まれる) ものは渡さない。"""
    return isinstance(iid, str) and bool(iid) and not iid.startswith("-")


def _br_id(i) -> str:
    """一覧の要素の id。show の引数にするので、渡せない id (_passable) は BdError。"""
    iid = i.get("id") if isinstance(i, dict) else None
    if not _passable(iid):
        raise BdError(f"br list の要素の id を読めない: {str(i)[:200]}")
    return iid


class BrBackend:
    """br (beads_rust) の CLI。run は br を 1 回打つ関数 (repo, *args) -> stdout で、呼ぶたびに引く。

    1 プロジェクトの読みは 3 プロセス: 判断待ちの list → 回答済みの list → 両方の id をまとめた show。問い
    (agent_context) とコメントは show から取る (br の schema の文書は agent_context を「list には出ない」としている)。
    保留か情報待ちのラベルの付いた判断待ちの thread も、同じ show のコメントから作る (プロセスは増えない)。
    読みの flag は br の既定のまま (JSONL の方が新しければ DB へ取り込む auto-import も、利用者が打ったときと同じに効かせる)。

    回答は 5 プロセス: 判断待ちの list → そのチケットの show (問いと署名の照合・送り直しの判定のコメント) →
    comments add → label add answered → label remove human。取り消しは回答済みの list → show → comments add →
    label add human → label remove answered。保留は判断待ちの list → show → comments add → label add snoozed、保留の
    解除は判断待ちの list → show → comments add (最後の [humanq] コメントが保留のときだけ) → label remove snoozed
    (付いているときだけ)、情報待ちは判断待ちの list → show → comments add → label add needs-info。
    書き込みの形 (--actor の位置・comments add -f・label の位置引数) は bd と
    同じで、label は付いている / 付いていないときも exit 0 (冪等。br 0.7.6 で実測)。"""

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

    def load(self, repo: str, memo: ThreadMemo | None = None) -> tuple[list[dict], list[dict]]:
        """一覧から show までの間にチケットが消えると、show は exit 3 で全体が失敗する。そのときは 1 回だけ一覧から
        読み直し、それでも失敗したら BdError。どちらの一覧も空なら show は打たない。
        memo は使わない (回答済みと保留の thread も同じ show から読むので、覚えても CLI の呼び出しは減らない)。"""
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
        rows = []
        for i in human:
            r = self.row(i, by[i["id"]])
            if watched(r):                      # 保留の戻る日・情報待ちの足りない情報はコメントにある (show はもう読んであるので、打ち足さない)
                r["thread"] = br_thread(by[i["id"]].get("comments"))
            rows.append(r)
        return rows, [self.row(i, by[i["id"]], thread=True) for i in answered]

    # ---- 書き込みの読み直しと書き込み (humanq.Store が書き込みのロックの下で呼ぶ) ----

    def _show(self, repo: str, iid: str) -> dict:
        """1 件の show。渡せない id (_passable) は打たずに BdError。出力が iid の 1 件でなければ BdError。"""
        if not _passable(iid):
            raise BdError(f"br show に渡せない id: {str(iid)[:200]}")
        shown = parse_br_show(self._run(repo, "show", iid, "--json"))
        if len(shown) != 1 or not isinstance(shown[0], dict) or shown[0].get("id") != iid:
            raise BdError(f"br show {iid} の出力が {iid} の 1 件ではない: {str(shown)[:200]}")
        return shown[0]

    def pending(self, repo: str, iid: str) -> list[dict]:
        """回答の書き込みが照合に使う判断待ちの Row。iid が判断待ちの一覧にあれば、その 1 件だけを show で読み、問いと
        thread (古い順のコメント) を付けて返す。送り直しの判定と署名の照合はこの thread で行う (show を 1 回で済ます)。
        一覧に無ければ [] (show は打たない)。"""
        hit = [i for i in self._list(repo, "human") if isinstance(i, dict) and i.get("id") == iid]
        if not hit:
            return []
        return [self.row(hit[0], self._show(repo, iid), thread=True)]

    def list_answered(self, repo: str) -> list[dict]:
        """回答済みの一覧の Row (取り消しが、チケットが回答済みにあるかを見るため)。show は打たないので問いは入らない。"""
        return [self.row(i, {}) for i in self._list(repo, "answered") if isinstance(i, dict)]

    def comments(self, repo: str, iid: str) -> list[dict]:
        """iid のコメント (古い順)。show の comments を並べ替える。list でない comments は読めない (BdError)。"""
        thread = br_thread(self._show(repo, iid).get("comments"))
        if thread is None:
            raise BdError(f"br show {iid} の comments を読めない (list ではない)")
        return thread

    def add_comment(self, repo: str, iid: str, path: str) -> None:
        self._run(repo, "--actor", ACTOR, "comments", "add", iid, "-f", path)

    def label(self, repo: str, op: str, iid: str, name: str) -> None:
        """op は add | remove。既に付いている (exists)・付いていない (not_found) も exit 0 なので成功とみなす。"""
        self._run(repo, "--actor", ACTOR, "label", op, iid, name)

    # ---- 見通し (読むだけ) ----

    def status_counts(self, repo: str) -> list[tuple[str, int]]:
        """状態別の件数 (`br count --by status --json`。{total, groups} を返し、groups は status の名前の順)。
        数える範囲は br の既定のまま (closed・tombstone・deferred は数えない。br 0.7.6 で実測)。"""
        return parse_count_groups(self._run(repo, "count", "--by", "status", "--json"), "br count ")

    def ready(self, repo: str) -> list[dict]:
        """次に着手できるチケット (`br ready --limit 0 --json`。素の配列で、要素は labels を持つ。human の付いたチケットも出る。
        --limit 0 は件数の上限を外す (br 0.7.6 の既定も 0)。list でなければ BdError。"""
        v = _loads(self._run(repo, "ready", "--limit", "0", "--json"), "br ready ")
        if not isinstance(v, list):
            raise BdError(f"br ready の出力が list ではない ({type(v).__name__}): {str(v)[:200]}")
        return ready_rows(v)
