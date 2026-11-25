#!/usr/bin/env python3
"""テスト用の偽の beads CLI。本物の bd が無い環境 (CI) でも、humanq のサーバーの読み書きを端から端まで回すため。

テストは一時の bin dir に、このファイルを `bd` の名前で置き (実行権限付き)、その dir を PATH の先頭に入れる
(tests/test_server.py の install_fake)。振る舞いは argv[0] の basename で分ける。今は bd だけで、
ほかの名前は exit 2 にする (別の CLI の偽物を足すときは PROGS に足す)。

- 状態: <project>/.beads/fake-state.json (issues・labels・comments・status・metadata)。
  呼び出しは .beads/fake-state.lock の flock で 1 本ずつにする (サーバーは読みと書きを別のスレッドで並べて打つ)
- 記録: env の FAKE_BEADS_LOG があれば、呼び出しのたびに {prog, argv, cwd} を 1 行の JSON で追記する
  (argv は argv[0] を除いたもの)。失敗させた呼び出しも記録する
- 失敗の注入: env の FAKE_BEADS_FAIL。規則は `;` で区切る。規則の語の列が、argv にこの順で現れる
  (間に別の語を挟んでよい) 呼び出しを exit 1 にする。`:n` を付けると、そのプロジェクトで最初の n 回だけ
  (数は .beads/fake-fail.json に残る)。例: `label remove human:1` は `--actor x label remove <id> human` の 1 回目だけ
- bd 1.3.4 で実測した癖を持つ:
  - `human list --json` は 0 件で null を返す。`list --json` は 0 件で [] を返す
  - 知らない flag は `Error: unknown flag: <flag>` で exit 1 (`human list --label x` もこれ)
  - metadata は object で返し、値が無ければ key を出さない (labels・description・notes も同じ)
  - labels は名前の順で返す (付けた順ではない)
  - `comments --json` は古い順。要素は {id, issue_id, author, text, created_at}。id は UUID 文字列。
    `--actor X` が author に入る
  - `.beads/metadata.json` の database が beads.db で embeddeddolt/ が無いと、
    `Error: historical SQLite workspace detected; ...` で exit 1 (embeddeddolt/ があれば本物も database の値を見ない)
  - `-C <dir>` に .beads/ が無ければ `Error: cannot use -C directory "<dir>": no beads project found` で exit 1
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
OPEN_STATUSES = ("open", "in_progress", "blocked", "deferred")
STATUSES = OPEN_STATUSES + ("closed",)


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
    for k in ("description", "notes"):
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
    opts, pos = take(args, {"--metadata": True, "--status": True, "-s": True, "--title": True})
    want(pos, 1, "update <id> [flags]")
    i = p.issue(pos[0], "resolving")
    if "--metadata" in opts:
        i["metadata"] = read_metadata(str(opts["--metadata"]))
    status = opts.get("--status") or opts.get("-s")
    if status is not None:
        if status not in STATUSES:
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
    items = [view(i) for i in p.state["issues"].values() if i["status"] in OPEN_STATUSES and "human" in i["labels"]]
    if as_json:
        emit(items or None)                                         # 0 件は null
    else:
        print("\n".join(f"{x['id']} {x['title']}" for x in items) or "No human-needed beads found.")


def bd_list(p: Project, args: list[str], as_json: bool) -> None:
    opts, pos = take(args, {"--status": True, "-s": True, "--label": True, "-l": True, "--flat": False,
                            "-n": True, "--limit": True, "--all": False})
    want(pos, 0, "list [flags]")
    status = opts.get("--status") or opts.get("-s")
    allowed = set(str(status).split(",")) if status else (set(STATUSES) if opts.get("--all") else set(OPEN_STATUSES))
    bad = allowed - set(STATUSES)
    if bad:
        raise Fail(f"Error: invalid status: {sorted(bad)[0]}")
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
        elif cmd == "human":
            bd_human(p, args, as_json)
        elif cmd == "list":
            bd_list(p, args, as_json)
        else:
            raise Fail(f'Error: unknown command "{cmd}" for "bd"')


PROGS = {"bd": run_bd}


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
    return 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
