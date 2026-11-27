#!/usr/bin/env python3
"""テスト用の偽の beads CLI。本物の bd が無い環境 (CI) でも、humanq のサーバーの読み書きを端から端まで回すため。

テストは一時の bin dir に、このファイルを `bd` の名前で置き (実行権限付き)、その dir を PATH の先頭に入れる
(tests/test_server.py の install_fake)。振る舞いは argv[0] の basename で分ける。bd と br があり、
ほかの名前は exit 2 にする (別の CLI の偽物を足すときは PROGS に足す)。br の癖は下の一覧の後ろにある。humanq の製品コードは br の list・show・comments add・label を使う。

- 状態: <project>/.beads/fake-state.json (issues・labels・comments・status・metadata)。
  呼び出しは .beads/fake-state.lock の flock で 1 本ずつにする (サーバーは読みと書きを別のスレッドで並べて打つ)
- 記録: env の FAKE_BEADS_LOG があれば、呼び出しのたびに {prog, argv, cwd} を 1 行の JSON で追記する
  (argv は argv[0] を除いたもの)。失敗させた呼び出しも記録する
- 失敗の注入: env の FAKE_BEADS_FAIL。規則は `;` で区切る。規則の語の列が、argv にこの順で現れる
  (間に別の語を挟んでよい) 呼び出しを exit 1 にする。`:n` を付けると、そのプロジェクトで最初の n 回だけ
  (数は .beads/fake-fail.json に残る)。例: `label remove human:1` は `--actor x label remove <id> human` の 1 回目だけ
- 警告の注入: env の FAKE_BEADS_WARN があれば、成功 (exit 0) した呼び出しの stderr にその文を 1 行足す
- bd 1.3.4 で実測した癖を持つ:
  - status は組み込みの 7 つ (open・in_progress・blocked・deferred・closed・pinned・hooked) と、`config set status.custom
    a,b:done` で宣言した独自のもの。`list` の既定と `human list` は同じ status を出す: closed・pinned・category が
    done か frozen の独自の status は出ず、hooked・deferred・category が wip か未指定の独自の status は出る。
    `list --status` には知らない status を書けない (`Error: invalid status "x" (valid: ...)`、exit 1)
  - `human list --json` は 0 件で null を返す。`list --json` は 0 件で [] を返す
  - 知らない flag は `Error: unknown flag: <flag>` で exit 1 (`human list --label x` もこれ)
  - metadata は object で返し、値が無ければ key を出さない (labels・description・notes・assignee も同じ)。
    担当は `update --assignee` (`-a`) で書ける
  - labels は名前の順で返す (付けた順ではない)
  - `comments --json` は古い順。要素は {id, issue_id, author, text, created_at}。id は UUID 文字列。
    `--actor X` が author に入る
  - `.beads/metadata.json` の database が beads.db で embeddeddolt/ が無いと、
    `Error: historical SQLite workspace detected; ...` で exit 1 (embeddeddolt/ があれば本物も database の値を見ない)
  - `-C <dir>` に .beads/ が無ければ `Error: cannot use -C directory "<dir>": no beads project found` で exit 1
- br 0.7.6 で実測した癖を持つ (名前が br のとき。詳しくは tests/test_fake_beads.py が 1 件ずつ押さえる):
  - -C・human サブコマンド・list --flat・list -n・update --metadata/--set-metadata は usage エラーで exit 2。
    起動した cwd から上へ .beads を探す
  - 見つけた .beads が git の linked worktree の最上位にあり、そこに DB (metadata.json の database か beads.db) も
    redirect も無ければ、本体の worktree の .beads (中身は問わない。実体が .beads か _beads という名前の dir のもの) を
    読み書きする (br_worktree_home)。init は空の beads.db を作る (本物の init が作る DB のファイルの代わり。中身は
    fake-state.json)
  - list -s のカンマ区切りは stdout の JSON error (VALIDATION_FAILED、exit 4) で非 0、繰り返しは通る。
    list --json は {"issues","total","limit","offset","has_more"} の envelope (0 件も同じ形)。既定は closed と tombstone だけを除く
    (draft・pinned・独自の status も出る)。-s に書けるのは組み込みの 8 つ・どれかのチケットに付いている status・all
    (all は tombstone を含まない)。ほかは VALIDATION_FAILED
  - update --status は組み込み以外の任意の文字列 (宣言は要らない) を受け付ける。closed と tombstone は断る (VALIDATION_FAILED、
    exit 4。close / delete を使わせる)。delete はチケットを tombstone にする (show では読める)
  - show --json は配列 (comments と agent_context を含み metadata は無い)。id が 1 つでも無ければ ISSUE_NOT_FOUND で exit 3。
    偽物は comments の key を 0 件でも出す (本物の 0.7.6 は 0 件のとき key を出さない。読む側は両方を受ける)
  - update --notes で notes を、--assignee で担当を書ける。description・notes・assignee は値のある issue にだけ list と show に出る
  - comments の並びは決まっていない (偽物は新しいものを先頭で返す)。id は int
  - label add / remove の戻りは [{"status": added|removed|exists|not_found, ...}] で、どれも exit 0
  - update --agent-context は丸ごと置き換え。半分未満・空にする書き換えは --force が無いと非 0
  - database が dolt の dir で打つと、エラーにせず .beads/dolt・dolt-wal・dolt-shm を作って空の結果を返す
依存は標準ライブラリだけ (fcntl を使うので macOS / Linux)。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone

HISTORICAL = ("Error: historical SQLite workspace detected; explicit migration is required before this bd version can open "
              "or modify the workspace. Preserve .beads unchanged and follow docs/getting-started/upgrading.md#cross-era-upgrades")
# bd 1.3.4 の組み込みの status (`bd statuses` の順)。独自の status は `bd config set status.custom a,b:done` で足す
# (`名前` か `名前:category`。category は wip・done・frozen。省略は unspecified)
STATUSES = ("open", "in_progress", "blocked", "deferred", "closed", "pinned", "hooked")
HIDDEN_BY_DEFAULT = ("closed", "pinned")           # list・human list の既定で出ない組み込み (hooked・deferred は出る)
HIDDEN_CATEGORIES = ("done", "frozen")             # 独自の status のうち、既定で出ないもの


def custom_statuses(state: dict) -> dict[str, str]:
    """status.custom の宣言を {名前: category} にする。"""
    out: dict[str, str] = {}
    for part in str((state.get("config") or {}).get("status.custom") or "").split(","):
        name, _, cat = part.strip().partition(":")
        if name.strip():
            out[name.strip()] = cat.strip() or "unspecified"
    return out


def valid_statuses(state: dict) -> tuple[str, ...]:
    return STATUSES + tuple(custom_statuses(state))


def shown_by_default(state: dict, status: str) -> bool:
    """bd list の既定と bd human list が出す status。実測 (bd 1.3.4): closed・pinned・完了か凍結の独自の status は出ない。"""
    if status in HIDDEN_BY_DEFAULT:
        return False
    return custom_statuses(state).get(status) not in HIDDEN_CATEGORIES
# br 0.7.6 の組み込みの status (`br list -s <知らない status>` の文が挙げる順)。br の status はこれ以外の任意の文字列にもできる
BR_STATUSES = ("open", "in_progress", "blocked", "deferred", "draft", "closed", "tombstone", "pinned")
# update --status で断る終わりの status と、本物の理由の文 (close / delete の専用のコマンドを使わせる)
BR_TERMINAL = {
    "closed": "refusing to close via `br update --status closed`: terminal-state transitions must go through `br close` "
              "so close-policy (close-reason / AC / attribution) is enforced.",
    "tombstone": "refusing to tombstone via `br update --status tombstone`: use `br delete <id>` instead so dependency "
                 "rewiring and tombstone metadata are applied correctly.",
}
BR_VALIDATION_EXIT = 4       # VALIDATION_FAILED の exit code (br 0.7.6 の実測)


class Fail(Exception):
    """exit code と、stderr (または stdout) に出す文。"""

    def __init__(self, msg: str, code: int = 1, stdout: bool = False):
        super().__init__(msg)
        self.code = code
        self.stdout = stdout


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_call(prog: str, argv: list[str]) -> None:
    path = os.environ.get("FAKE_BEADS_LOG")
    if not path:
        return
    line = json.dumps({"prog": prog, "argv": argv, "cwd": os.getcwd()}, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def matches(words: list[str], argv: list[str]) -> bool:
    """words が argv にこの順で現れるか (間に別の語を挟んでよい)。"""
    it = iter(argv)
    return all(any(a == w for a in it) for w in words)


def parse_rules(spec: str) -> list[tuple[str, list[str], int | None]]:
    rules = []
    for raw in spec.split(";"):
        raw = raw.strip()
        if not raw:
            continue
        body, sep, n = raw.rpartition(":")
        if sep and n.isdigit():
            rules.append((raw, body.split(), int(n)))
        else:
            rules.append((raw, raw.split(), None))
    return rules


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-fake-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def inject_failure(beads: str, argv: list[str]) -> None:
    rules = parse_rules(os.environ.get("FAKE_BEADS_FAIL") or "")
    if not rules:
        return
    path = os.path.join(beads, "fake-fail.json")
    counts = read_json(path, {})
    for raw, words, limit in rules:
        if not matches(words, argv):
            continue
        used = counts.get(raw, 0)
        if limit is not None and used >= limit:
            continue
        counts[raw] = used + 1
        write_json(path, counts)
        raise Fail(f"Error: fake-beads: injected failure ({raw})")


# ---- 引数 ------------------------------------------------------------------------------

def split_globals(argv: list[str]) -> tuple[str | None, str | None, bool, list[str]]:
    """-C <dir>・--actor <name>・--json はどこに書いてもよい (本物の bd も同じ)。残りを返す。"""
    cdir = actor = None
    as_json = False
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-C" or a == "--actor":
            if i + 1 >= len(argv):
                raise Fail(f"Error: flag needs an argument: {a}")
            if a == "-C":
                cdir = argv[i + 1]
            else:
                actor = argv[i + 1]
            i += 2
            continue
        if a.startswith("--actor="):
            actor = a.split("=", 1)[1]
        elif a == "--json":
            as_json = True
        else:
            rest.append(a)
        i += 1
    return cdir, actor, as_json, rest


def take(args: list[str], spec: dict[str, bool]) -> tuple[dict[str, str | bool], list[str]]:
    """args から spec の flag (値を取るなら True) を抜き出す。知らない flag は本物と同じ文で落とす。"""
    opts: dict[str, str | bool] = {}
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and a != "-":
            name, eq, val = a.partition("=")
            if name not in spec:
                raise Fail(f"Error: unknown flag: {name}" if name.startswith("--") else f"Error: unknown shorthand flag: {name!r} in {a}")
            if spec[name]:
                if eq:
                    opts[name] = val
                else:
                    if i + 1 >= len(args):
                        raise Fail(f"Error: flag needs an argument: {name}")
                    opts[name] = args[i + 1]
                    i += 1
            else:
                opts[name] = True
        else:
            pos.append(a)
        i += 1
    return opts, pos


def want(pos: list[str], n: int, usage: str) -> None:
    if len(pos) != n:
        raise Fail(f"Error: accepts {n} arg(s), received {len(pos)}\nUsage: bd {usage}")


# ---- 状態 ------------------------------------------------------------------------------

class Project:
    def __init__(self, root: str):
        self.root = root
        self.beads = os.path.join(root, ".beads")
        self.path = os.path.join(self.beads, "fake-state.json")
        self.state = read_json(self.path, None) or {"prefix": "bd", "next": 1, "issues": {}}

    def save(self) -> None:
        write_json(self.path, self.state)

    def issue(self, iid: str, how: str = "resolving issue ID") -> dict:
        i = self.state["issues"].get(iid)
        if i is None:
            raise Fail(f'Error: {how} "{iid}": no issue found matching "{iid}"' if how == "resolving issue ID"
                       else f'Error: {how} {iid}: no issue found matching "{iid}"')
        return i


def view(i: dict, counts: bool = False) -> dict:
    out = {k: i[k] for k in ("id", "title", "status", "priority", "issue_type", "created_at", "created_by", "updated_at")}
    for k in ("description", "notes", "assignee"):
        if i.get(k):
            out[k] = i[k]
    if i.get("metadata"):
        out["metadata"] = i["metadata"]
    if i.get("labels"):
        out["labels"] = sorted(i["labels"])                       # 本物は名前の順で返す (付けた順ではない)
    if counts:
        out.update({"dependency_count": 0, "dependent_count": 0, "comment_count": len(i.get("comments") or [])})
    return out


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def touch(i: dict) -> None:
    i["updated_at"] = now()


# ---- bd のコマンド -------------------------------------------------------------------------

def bd_init(root: str, args: list[str]) -> None:
    opts, pos = take(args, {"--non-interactive": False, "--quiet": False, "--skip-agents": False, "--skip-hooks": False,
                            "-p": True, "--prefix": True})
    want(pos, 0, "init [flags]")
    beads = os.path.join(root, ".beads")
    os.makedirs(beads, exist_ok=True)
    write_json(os.path.join(beads, "metadata.json"), {"database": "dolt", "backend": "dolt", "dolt_mode": "embedded"})
    p = Project(root)
    p.state = {"prefix": str(opts.get("-p") or opts.get("--prefix") or "bd"), "next": 1, "issues": {}}
    p.save()


def read_metadata(raw: str) -> dict | None:
    """--metadata の値 (JSON か @file) を読む。create と update で同じ。"""
    if raw.startswith("@"):
        try:
            with open(raw[1:], encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            raise Fail(f"Error: reading metadata file: {e}") from None
    try:
        meta = json.loads(raw)
    except ValueError:
        raise Fail("Error: invalid metadata JSON") from None
    if not isinstance(meta, dict):
        raise Fail("Error: metadata must be a JSON object")
    return meta or None


def bd_create(p: Project, args: list[str], as_json: bool, actor: str) -> None:
    opts, pos = take(args, {"-p": True, "--priority": True, "-t": True, "--type": True, "-d": True, "--description": True,
                            "-l": True, "--labels": True, "--metadata": True})
    want(pos, 1, "create <title> [flags]")
    labels: list[str] = []
    for name in str(opts.get("-l") or opts.get("--labels") or "").split(","):   # 本物と同じく , で区切る
        if name.strip() and name.strip() not in labels:
            labels.append(name.strip())
    meta = read_metadata(str(opts["--metadata"])) if "--metadata" in opts else None
    iid = f"{p.state['prefix']}-{p.state['next']}"
    p.state["next"] += 1
    t = now()
    i = {"id": iid, "title": pos[0], "status": "open", "priority": int(opts.get("-p") or opts.get("--priority") or 2),
         "issue_type": str(opts.get("-t") or opts.get("--type") or "task"), "created_at": t, "created_by": actor,
         "updated_at": t, "description": str(opts.get("-d") or opts.get("--description") or ""), "notes": "",
         "metadata": meta, "labels": labels, "comments": []}
    p.state["issues"][iid] = i
    p.save()
    if as_json:
        emit({**view(i), "schema_version": 1})
    else:
        print(f"✓ Created issue: {iid} — {i['title']}")


def bd_update(p: Project, args: list[str]) -> None:
    opts, pos = take(args, {"--metadata": True, "--status": True, "-s": True, "--title": True, "--assignee": True, "-a": True})
    want(pos, 1, "update <id> [flags]")
    i = p.issue(pos[0], "resolving")
    if "--metadata" in opts:
        i["metadata"] = read_metadata(str(opts["--metadata"]))
    assignee = opts.get("--assignee") or opts.get("-a")
    if assignee is not None:                        # 本物は show・list・human list に assignee を出す (bd 1.3.4 で実測)
        i["assignee"] = str(assignee)
    status = opts.get("--status") or opts.get("-s")
    if status is not None:
        if status not in valid_statuses(p.state):
            raise Fail(f"Error: invalid status: {status}")
        i["status"] = status
    if "--title" in opts:
        i["title"] = str(opts["--title"])
    touch(i)
    p.save()
    print(f"✓ Updated issue: {i['id']} — {i['title']}")


def bd_close(p: Project, args: list[str]) -> None:
    opts, pos = take(args, {"-r": True, "--reason": True})
    want(pos, 1, "close <id> [flags]")
    i = p.issue(pos[0], "resolving")
    i["status"] = "closed"
    touch(i)
    p.save()
    print(f"✓ Closed {i['id']} — {i['title']}: {opts.get('-r') or opts.get('--reason') or 'Closed'}")


def bd_show(p: Project, args: list[str], as_json: bool) -> None:
    _, pos = take(args, {})
    want(pos, 1, "show <id>")
    i = p.issue(pos[0], "resolving")
    if as_json:
        emit([{**view(i, counts=True), "comments_omitted": True}])
    else:
        print(f"{i['id']} · {i['title']}   [{i['status']}]")


def bd_label(p: Project, args: list[str]) -> None:
    if not args or args[0] not in ("add", "remove"):
        raise Fail(f'Error: unknown command "{args[0] if args else ""}" for "bd label"')
    op, rest = args[0], args[1:]
    _, pos = take(rest, {})
    want(pos, 2, f"label {op} <id> <label>")
    i = p.issue(pos[0])
    name = pos[1]
    if op == "add":
        if name not in i["labels"]:
            i["labels"].append(name)
        msg = f"✓ Added label '{name}' to {i['id']}"
    else:
        i["labels"] = [x for x in i["labels"] if x != name]      # 付いていなくても成功する (本物も同じ)
        msg = f"✓ Removed label '{name}' from {i['id']}"
    touch(i)
    p.save()
    print(msg)


def bd_comments(p: Project, args: list[str], as_json: bool, actor: str) -> None:
    if args and args[0] == "add":
        opts, pos = take(args[1:], {"-f": True, "--file": True})
        f = opts.get("-f") or opts.get("--file")
        if f:
            want(pos, 1, "comments add <id> [-f file]")
            try:
                with open(str(f), encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                raise Fail(f"Error: reading comment file: {e}") from None
        else:
            want(pos, 2, "comments add <id> <text>")
            text = pos[1]
        i = p.issue(pos[0], "resolving")
        i["comments"].append({"id": str(uuid.uuid4()), "issue_id": i["id"], "author": actor, "text": text,
                              "created_at": now()})
        touch(i)
        p.save()
        print(f"Comment added to {i['id']}")
        return
    _, pos = take(args, {})
    want(pos, 1, "comments <id>")
    i = p.state["issues"].get(pos[0])
    if i is None:
        msg = f'resolving {pos[0]}: no issue found matching "{pos[0]}"'
        if as_json:                                                 # 本物は JSON の error を stdout に出して exit 1
            raise Fail(json.dumps({"error": msg, "schema_version": 1}, indent=2), stdout=True)
        raise Fail("Error: " + msg)
    cs = list(i["comments"])                                        # 古い順
    if as_json:
        emit(cs)
    else:
        for c in cs:
            print(f"[{c['author']}] {c['text']}")


def bd_human(p: Project, args: list[str], as_json: bool) -> None:
    if not args or args[0] != "list":
        raise Fail(f'Error: unknown command "{args[0] if args else ""}" for "bd human"')
    _, pos = take(args[1:], {})
    want(pos, 0, "human list")
    items = [view(i) for i in p.state["issues"].values()
             if shown_by_default(p.state, i["status"]) and "human" in i["labels"]]
    if as_json:
        emit(items or None)                                         # 0 件は null
    else:
        print("\n".join(f"{x['id']} {x['title']}" for x in items) or "No human-needed beads found.")


def bd_config(p: Project, args: list[str]) -> None:
    """config set / get。fake が読むのは status.custom だけ (ほかの key も値は保存して返す)。"""
    if not args or args[0] not in ("set", "get"):
        raise Fail(f'Error: unknown command "{args[0] if args else ""}" for "bd config"')
    cfg = p.state.setdefault("config", {})
    if args[0] == "set":
        _, pos = take(args[1:], {})
        want(pos, 2, "config set <key> <value>")
        cfg[pos[0]] = pos[1]
        p.save()
        print(f"Set {pos[0]} = {pos[1]}")
    else:
        _, pos = take(args[1:], {})
        want(pos, 1, "config get <key>")
        print(cfg.get(pos[0], f"{pos[0]} (not set)"))


def bd_list(p: Project, args: list[str], as_json: bool) -> None:
    opts, pos = take(args, {"--status": True, "-s": True, "--label": True, "-l": True, "--flat": False,
                            "-n": True, "--limit": True, "--all": False})
    want(pos, 0, "list [flags]")
    status = opts.get("--status") or opts.get("-s")
    valid = valid_statuses(p.state)
    if status:
        allowed = set(str(status).split(","))
    elif opts.get("--all"):
        allowed = set(valid)
    else:
        allowed = {s for s in valid if shown_by_default(p.state, s)}
    bad = allowed - set(valid)
    if bad:
        raise Fail(f'Error: invalid status "{sorted(bad)[0]}" (valid: {", ".join(valid)})')
    label = opts.get("--label") or opts.get("-l")
    items = [i for i in p.state["issues"].values() if i["status"] in allowed and (not label or label in i["labels"])]
    limit = int(opts.get("-n") or opts.get("--limit") or 50)
    if limit > 0:
        items = items[:limit]
    if as_json:
        emit([view(i, counts=True) for i in items])                  # 0 件は []
    else:
        print("\n".join(f"{i['id']} {i['title']}" for i in items))


def run_bd(argv: list[str]) -> None:
    cdir, actor, as_json, rest = split_globals(argv)
    root = os.path.abspath(cdir) if cdir else os.getcwd()
    if not rest:
        raise Fail("Error: missing command")
    cmd, args = rest[0], rest[1:]
    if cmd == "init":
        if not os.path.isdir(root):
            raise Fail(f'Error: cannot use -C directory "{cdir}": no such directory')
        bd_init(root, args)
        return
    beads = os.path.join(root, ".beads")
    if not os.path.isdir(beads):
        raise Fail(f'Error: cannot use -C directory "{root}": no beads project found')
    meta = read_json(os.path.join(beads, "metadata.json"), {})
    if isinstance(meta, dict) and meta.get("database") == "beads.db" and not os.path.isdir(os.path.join(beads, "embeddeddolt")):
        raise Fail(HISTORICAL)
    who = actor or os.environ.get("USER") or "fake"
    with open(os.path.join(beads, "fake-state.lock"), "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        inject_failure(beads, argv)
        p = Project(root)
        if cmd == "create":
            bd_create(p, args, as_json, who)
        elif cmd == "update":
            bd_update(p, args)
        elif cmd == "close":
            bd_close(p, args)
        elif cmd == "show":
            bd_show(p, args, as_json)
        elif cmd == "label":
            bd_label(p, args)
        elif cmd == "comments":
            bd_comments(p, args, as_json, who)
        elif cmd == "config":
            bd_config(p, args)
        elif cmd == "human":
            bd_human(p, args, as_json)
        elif cmd == "list":
            bd_list(p, args, as_json)
        else:
            raise Fail(f'Error: unknown command "{cmd}" for "bd"')


# ---- br (beads_rust 0.7.6) -----------------------------------------------------------------
# 実測した癖は先頭の docstring の一覧にある。bd と状態の形 (fake-state.json) は共有するが、
# コメントの id は int、返す並びは古い順ではない (本物は決まっていないので、偽物はわざと新しいものを先頭にする)。

class Usage(Fail):
    """clap の usage エラー。exit 2 で stderr に出す。"""

    def __init__(self, msg: str):
        super().__init__(msg, code=2)


def br_usage_unexpected(name: str) -> Usage:
    return Usage(f"error: unexpected argument '{name}' found\n\nUsage: br [OPTIONS] <COMMAND>\n\nFor more information, try '--help'.")


def br_json_error(code: str, message: str, exit_code: int) -> Fail:
    """本物は JSON の error を stdout に出して非 0 で終わる。"""
    return Fail(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False), code=exit_code, stdout=True)


def br_validation(reason: str, as_json: bool) -> Fail:
    """VALIDATION_FAILED。--json なら stdout の JSON の error、無ければ stderr の 1 行 (どちらも exit 4)。"""
    msg = f"Validation failed: status: {reason}"
    if as_json:
        return br_json_error("VALIDATION_FAILED", msg, BR_VALIDATION_EXIT)
    return Fail(f"Error: {msg}", code=BR_VALIDATION_EXIT)


def br_split_globals(argv: list[str]) -> tuple[str | None, bool, list[str]]:
    """--actor <name>・--json はどこに書いてもよい。-C は無い (usage エラー)。残りを返す。"""
    actor = None
    as_json = False
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-C" or a.startswith("-C="):
            raise br_usage_unexpected("-C")
        if a == "--actor":
            if i + 1 >= len(argv):
                raise Usage("error: a value is required for '--actor <ACTOR>' but none was supplied")
            actor = argv[i + 1]
            i += 2
            continue
        if a.startswith("--actor="):
            actor = a.split("=", 1)[1]
        elif a == "--json":
            as_json = True
        else:
            rest.append(a)
        i += 1
    return actor, as_json, rest


def br_take(args: list[str], spec: dict[str, bool]) -> tuple[dict, list[str]]:
    """spec の flag (値を取るなら True) を抜き出す。値を取る flag は繰り返せる (値は順に溜まる)。
    知らない flag は clap と同じ usage エラー。"""
    opts: dict = {}
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and a != "-":
            name, eq, val = a.partition("=")
            if name not in spec:
                raise br_usage_unexpected(name)
            if spec[name]:
                if not eq:
                    if i + 1 >= len(args):
                        raise Usage(f"error: a value is required for '{name} <VALUE>' but none was supplied")
                    val = args[i + 1]
                    i += 1
                opts.setdefault(name, []).append(val)
            else:
                opts[name] = True
        else:
            pos.append(a)
        i += 1
    return opts, pos


def br_one(opts: dict, *names: str) -> str | None:
    for n in names:
        if opts.get(n):
            return opts[n][-1]
    return None


def br_find_root(start: str) -> str | None:
    """cwd から上へ .beads を探す (br に -C は無い)。"""
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, ".beads")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def br_worktree_home(root: str) -> str:
    """br 0.7.6 の実測 (使い捨ての repo の `br where`): root が linked worktree の最上位 (root/.git が gitdir のファイル) で、
    root/.beads に DB も redirect も無ければ、本体の worktree の .beads (dir なら中身を問わない) を使う。本体に .beads が
    無い・実体の名前が .beads か _beads でないときは root のまま (本物は後者で設定エラーになる。偽物は root を読む)。
    worktree の管理 dir (gitdir) が消えていても、gitdir の形から本体を決める。"""
    try:
        with open(os.path.join(root, ".git"), encoding="utf-8") as f:
            first = f.readline()
    except OSError:                                 # 無い・dir (本体そのもの)
        return root
    if not first.startswith("gitdir:"):
        return root
    gitdir = os.path.normpath(os.path.join(root, first[len("gitdir:"):].strip()))
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as f:
            common = os.path.normpath(os.path.join(gitdir, f.readline().strip()))
    except OSError:
        if os.path.basename(os.path.dirname(gitdir)) != "worktrees":
            return root
        common = os.path.dirname(os.path.dirname(gitdir))
    if os.path.basename(common) != ".git":
        return root
    main = os.path.dirname(common)

    def db_names(beads: str) -> set[str]:
        meta = read_json(os.path.join(beads, "metadata.json"), {})
        db = meta.get("database") if isinstance(meta, dict) else None
        return {"beads.db"} | ({db} if isinstance(db, str) and db else set())

    beads, mbeads = os.path.join(root, ".beads"), os.path.join(main, ".beads")
    if os.path.exists(os.path.join(beads, "redirect")) or any(os.path.exists(os.path.join(beads, n)) for n in db_names(beads)):
        return root
    if not os.path.isdir(mbeads) or os.path.basename(os.path.realpath(mbeads)) not in (".beads", "_beads"):
        return root
    return main


def br_time(micro: bool = False) -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ" if micro else "%Y-%m-%dT%H:%M:%SZ")


def br_issue(p: Project, iid: str) -> dict:
    i = p.state["issues"].get(iid)
    if i is None:
        raise br_json_error("ISSUE_NOT_FOUND", f"Issue not found: {iid}", 3)
    return i


def br_comments_of(i: dict) -> list[dict]:
    """並びは決まっていない。偽物は新しいものを先頭にする (古い順を前提にした読み方を落とすため)。"""
    return [{"id": c["id"], "issue_id": i["id"], "author": c["author"], "text": c["text"], "created_at": c["created_at"]}
            for c in reversed(i["comments"])]


def br_row(i: dict) -> dict:
    """metadata と owner の key は無い。description・notes・agent_context は値のある issue にだけ出る (agent_context は文字列)。"""
    out = {"id": i["id"], "title": i["title"], "status": i["status"], "priority": i["priority"],
           "issue_type": i["issue_type"], "labels": sorted(i["labels"]), "created_at": i["created_at"],
           "updated_at": i["updated_at"]}
    for k in ("description", "notes", "agent_context", "assignee"):
        if i.get(k):
            out[k] = i[k]
    return out


def br_init(root: str, args: list[str]) -> None:
    opts, pos = br_take(args, {"--prefix": True, "--force": False})
    if pos:
        raise br_usage_unexpected(pos[0])
    beads = os.path.join(root, ".beads")
    os.makedirs(beads, exist_ok=True)
    write_json(os.path.join(beads, "metadata.json"), {"database": "beads.db", "jsonl_export": "issues.jsonl"})
    open(os.path.join(beads, "beads.db"), "a").close()      # 本物の DB のファイルの代わり (worktree の解決が見る)
    p = Project(root)
    p.state = {"prefix": br_one(opts, "--prefix") or "bd", "next": 1, "next_comment": 1, "issues": {}}
    p.save()


def br_create(p: Project, args: list[str], as_json: bool, actor: str) -> None:
    opts, pos = br_take(args, {"-p": True, "--priority": True, "-t": True, "--type": True, "-d": True,
                               "--description": True, "-l": True, "--labels": True})
    if len(pos) != 1:
        raise Usage("error: expected exactly one <TITLE>")
    labels: list[str] = []
    for raw in opts.get("-l", []) + opts.get("--labels", []):
        for name in raw.split(","):
            if name.strip() and name.strip() not in labels:
                labels.append(name.strip())
    iid = f"{p.state['prefix']}-{p.state['next']}"
    p.state["next"] += 1
    t = br_time(True)
    i = {"id": iid, "title": pos[0], "status": "open", "priority": int(br_one(opts, "-p", "--priority") or 2),
         "issue_type": br_one(opts, "-t", "--type") or "task", "created_at": t, "created_by": actor, "updated_at": t,
         "description": br_one(opts, "-d", "--description") or "", "labels": labels, "comments": [], "agent_context": ""}
    p.state["issues"][iid] = i
    p.save()
    if as_json:
        emit(br_row(i))
    else:
        print(f"Created {iid}: {i['title']}")


def br_update(p: Project, args: list[str], as_json: bool) -> None:
    opts, pos = br_take(args, {"--agent-context": True, "--force": False, "--status": True, "--title": True, "--notes": True,
                               "--assignee": True})
    if len(pos) != 1:
        raise Usage("error: expected exactly one <ID>")
    i = br_issue(p, pos[0])
    status = br_one(opts, "--status")
    if status in BR_TERMINAL:                                           # closed / tombstone は close / delete でだけ
        raise br_validation(BR_TERMINAL[status], as_json)
    if "--agent-context" in opts:
        raw = br_one(opts, "--agent-context") or ""
        if raw.startswith("@"):
            try:
                with open(raw[1:], encoding="utf-8") as f:
                    raw = f.read()
            except OSError as e:
                raise Fail(f"Error: reading agent-context file: {e}") from None
        old = i.get("agent_context") or ""
        if old and len(raw) * 2 < len(old) and not opts.get("--force"):      # 今の値の半分未満・空にする書き換えは断る
            raise Fail(f"Error: refusing to overwrite non-empty 'agent_context' ({len(old)} chars) with a shorter value "
                       f"({len(raw)} chars) without --force")
        i["agent_context"] = raw
    if status is not None:
        i["status"] = status        # 組み込み以外の任意の文字列 (review・打ち間違いの in-progress など) も、宣言なしで受け付ける
    if "--title" in opts:
        i["title"] = br_one(opts, "--title") or ""
    if "--notes" in opts:
        i["notes"] = br_one(opts, "--notes") or ""
    if "--assignee" in opts:                        # 本物は show・list に assignee を出す (br 0.7.6 で実測)
        i["assignee"] = br_one(opts, "--assignee") or ""
    i["updated_at"] = br_time(True)
    p.save()
    if as_json:
        emit(br_row(i))
    else:
        print(f"Updated {i['id']}")


def br_close(p: Project, args: list[str]) -> None:
    _, pos = br_take(args, {"--reason": True, "-r": True})
    if len(pos) != 1:
        raise Usage("error: expected exactly one <ID>")
    i = br_issue(p, pos[0])
    i["status"] = "closed"
    i["updated_at"] = br_time(True)
    p.save()
    print(f"Closed {i['id']}")


def br_delete(p: Project, args: list[str], as_json: bool) -> None:
    """チケットを tombstone にする (消さない。show では読める)。無い id が 1 つでもあれば ISSUE_NOT_FOUND (exit 3)。"""
    _, pos = br_take(args, {"--force": False, "--reason": True})
    if not pos:
        raise Usage("error: expected at least one <ID>")
    found = [br_issue(p, iid) for iid in pos]
    for i in found:
        i["status"] = "tombstone"
        i["updated_at"] = br_time(True)
    p.save()
    if as_json:
        emit({"deleted": [i["id"] for i in found], "deleted_count": len(found), "dependencies_removed": 0,
              "labels_removed": 0, "events_removed": 0, "references_updated": 0, "orphaned_issues": []})
    else:
        print(f"Deleted {len(found)} issue(s):\n" + "\n".join(f"  - {i['id']}" for i in found))


def br_list(p: Project, args: list[str], as_json: bool) -> None:
    opts, pos = br_take(args, {"-s": True, "--status": True, "-l": True, "--label": True, "--limit": True, "--all": False})
    if pos:
        raise br_usage_unexpected(pos[0])
    given = opts.get("-s", []) + opts.get("--status", [])
    # -s に書けるのは組み込み・どれかのチケットに付いている status・all。カンマ区切りは 1 つの知らない status になる
    known = set(BR_STATUSES) | {i["status"] for i in p.state["issues"].values()} | {"all"}
    for s in given:
        if s not in known:
            raise br_validation(f"unknown status '{s}'. Built-in statuses: {', '.join(BR_STATUSES)}. Custom statuses must "
                                "be declared in .beads/policy.yaml (workflow.statuses) or exist on at least one issue. "
                                "Use --status all to include every status", True)
    labels = opts.get("-l", []) + opts.get("--label", [])

    def shown(st: str) -> bool:
        if given:                                                       # all は tombstone を含まない (tombstone は名指しのときだけ)
            return st in given or ("all" in given and st != "tombstone")
        if opts.get("--all"):
            return st != "tombstone"
        return st not in ("closed", "tombstone")                        # 既定は closed と tombstone だけを除く (draft・pinned・独自の status も出る)

    items = [i for i in p.state["issues"].values() if shown(i["status"]) and all(x in i["labels"] for x in labels)]
    total = len(items)
    limit = int(br_one(opts, "--limit") or 0)                           # 0 は無制限 (既定も無制限)
    if limit > 0:
        items = items[:limit]
    if as_json:
        emit({"issues": [br_row(i) for i in items], "total": total, "limit": limit, "offset": 0,
              "has_more": limit > 0 and total > limit})
    else:
        print("\n".join(f"{i['id']} {i['title']}" for i in items))


def br_show(p: Project, args: list[str], as_json: bool) -> None:
    _, pos = br_take(args, {})
    if not pos:
        raise Usage("error: the following required arguments were not provided:\n  <IDS>...")
    found = [br_issue(p, iid) for iid in pos]                           # 1 つでも無ければ全体が exit 3
    if as_json:
        emit([{**br_row(i), "comments": br_comments_of(i)} for i in found])
    else:
        print("\n".join(f"{i['id']} · {i['title']}   [{i['status']}]" for i in found))


def br_label(p: Project, args: list[str], as_json: bool) -> None:
    if not args or args[0] not in ("add", "remove"):
        raise Usage(f"error: unrecognized subcommand '{args[0] if args else ''}'")
    op = args[0]
    _, pos = br_take(args[1:], {})
    if len(pos) != 2:
        raise Usage(f"error: expected <ID> <LABEL> for 'label {op}'")
    i = br_issue(p, pos[0])
    name = pos[1]
    if op == "add":
        status = "exists" if name in i["labels"] else "added"
        if status == "added":
            i["labels"].append(name)
    else:
        status = "removed" if name in i["labels"] else "not_found"
        i["labels"] = [x for x in i["labels"] if x != name]
    i["updated_at"] = br_time(True)
    p.save()
    if as_json:
        emit([{"status": status, "issue_id": i["id"], "label": name}])
    else:
        print(f"{status}: {name} {i['id']}")


def br_comments(p: Project, args: list[str], as_json: bool, actor: str) -> None:
    if args and args[0] == "add":
        opts, pos = br_take(args[1:], {"-f": True, "--file": True})
        f = br_one(opts, "-f", "--file")
        if f:
            if len(pos) != 1:
                raise Usage("error: expected exactly one <ID> with --file")
            try:
                with open(f, encoding="utf-8", newline="") as fh:       # 本文末尾の改行は保つ
                    text = fh.read()
            except OSError as e:
                raise Fail(f"Error: reading comment file: {e}") from None
        else:
            if len(pos) != 2:
                raise Usage("error: expected <ID> <TEXT>")
            text = pos[1]
        i = br_issue(p, pos[0])
        cid = p.state.get("next_comment", 1)
        p.state["next_comment"] = cid + 1                               # int で単調に増える
        i["comments"].append({"id": cid, "author": actor, "text": text, "created_at": br_time()})
        i["updated_at"] = br_time(True)
        p.save()
        if as_json:
            emit({"id": cid, "issue_id": i["id"], "author": actor, "text": text, "created_at": i["comments"][-1]["created_at"]})
        else:
            print(f"Comment added to {i['id']}")
        return
    _, pos = br_take(args, {})
    if len(pos) != 1:
        raise Usage("error: expected exactly one <ID>")
    cs = br_comments_of(br_issue(p, pos[0]))
    if as_json:
        emit(cs)
    else:
        for c in cs:
            print(f"[{c['author']}] {c['text']}")


def br_dolt_accident(beads: str, cmd: str, as_json: bool) -> None:
    """database が dolt の dir で打たれたとき。本物はエラーにせず sqlite 用のファイルを作って空の結果を返す
    (実機で起きた事故の再現)。後の案は、この 3 つが作られないことを確かめる。"""
    for name in ("dolt", "dolt-wal", "dolt-shm"):
        with open(os.path.join(beads, name), "a"):
            pass
    if cmd == "list" and as_json:
        emit({"issues": [], "total": 0, "limit": 0, "offset": 0, "has_more": False})


BR_COMMANDS = ("init", "create", "update", "close", "delete", "list", "show", "label", "comments")


def run_br(argv: list[str]) -> None:
    actor, as_json, rest = br_split_globals(argv)
    if not rest:
        raise Usage("error: a subcommand is required")
    cmd, args = rest[0], rest[1:]
    if cmd not in BR_COMMANDS:
        raise Usage(f"error: unrecognized subcommand '{cmd}'")            # human を含む
    if cmd == "init":
        br_init(os.getcwd(), args)
        return
    root = br_find_root(os.getcwd())
    if root is None:
        raise Fail("Error: no .beads directory found (run `br init`)")
    root = br_worktree_home(root)
    beads = os.path.join(root, ".beads")
    meta = read_json(os.path.join(beads, "metadata.json"), {})
    if isinstance(meta, dict) and meta.get("database") == "dolt":
        br_dolt_accident(beads, cmd, as_json)
        return
    who = actor or os.environ.get("USER") or "fake"
    with open(os.path.join(beads, "fake-state.lock"), "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        inject_failure(beads, argv)
        p = Project(root)
        if cmd == "create":
            br_create(p, args, as_json, who)
        elif cmd == "update":
            br_update(p, args, as_json)
        elif cmd == "close":
            br_close(p, args)
        elif cmd == "delete":
            br_delete(p, args, as_json)
        elif cmd == "list":
            br_list(p, args, as_json)
        elif cmd == "show":
            br_show(p, args, as_json)
        elif cmd == "label":
            br_label(p, args, as_json)
        else:
            br_comments(p, args, as_json, who)


PROGS = {"bd": run_bd, "br": run_br}


def main() -> int:
    prog = os.path.basename(sys.argv[0])
    argv = sys.argv[1:]
    log_call(prog, argv)
    fn = PROGS.get(prog)
    if fn is None:
        sys.stderr.write(f"fake-beads: unknown program name {prog!r} (expected one of {sorted(PROGS)})\n")
        return 2
    try:
        fn(argv)
    except Fail as e:
        (sys.stdout if e.stdout else sys.stderr).write(str(e) + "\n")
        return e.code
    warn = os.environ.get("FAKE_BEADS_WARN")
    if warn:                                        # 成功 (exit 0) の呼び出しの stderr に警告を足す (本物の br の JSONL の export の失敗)
        sys.stderr.write(warn + "\n")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
