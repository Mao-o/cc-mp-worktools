"""比較器: plugin の 2 つの版に同じコーパスを流し、シェルの CLI が受け取った組と verify に渡った組を突き合わせる。

usage (sandbox の中で流す。偽の CLI 以外の CLI は起動しない):
  python3 compare.py --old <版> --new <版> [--corpus 名前 ...] [--work DIR] [--jobs 3] [--show N] [--json OUT]

版: hook パッケージの dir (`.../hooks/cloud-account-guard`)、plugin の dir、または git の ref
(`git archive <ref> plugins/cloud-account-guard` で work の下に取り出す)。
コーパス: lattice / boundary / shellvars / ctxopt (corpus.py) か、JSONL のパス。既定は 4 つ全部。

数えるもの (行ごと。run = bash か zsh で実際に起動した偽の CLI 1 回):
- DOD: 旧が止める (deny / ask) 行で、新が止めず、かつ run の組が、新の verify に渡った同じ CLI の組の
  どれとも違うもの。違う名前 (`env:NAME` / `ctx:key`) と、違いが出たシェルの内訳つき。「うち期待外」は、
  組の違う run が world の規則で期待外の照合先になる行 (組は違っても同じアカウントに当たる行を除いた数)
- NEW_ABS / OLD_ABS: その版が止めず、run の組がその版の verify の組と違う行 (旧新を問わない、その版だけの素通し)
- UNVERIFIED: 旧が止め新が止めない行で、新がその CLI を照合せずに run が走ったもの (読み取り専用の tier など)
- FIXED: 旧が止め新が止めない行で、どの run の組も新の verify の組と一致するもの (誤 deny の解消)
- TIGHTEN: 旧が止めず新が止める行 (理由の 1 行目つき)
- CLI (tag の先頭) ごとの旧 → 新の遷移
組 = decl.py。名前の一覧は --decl の版 (既定はこの道具の置かれた版) の宣言から引く。
exit 1: DOD が 1 件でもある。
"""
import argparse
import collections
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
sys.path.insert(0, TESTS)
from oracle import corpus as corpus_mod  # noqa: E402
from oracle import decl  # noqa: E402

PLUGIN_REL = os.path.join("plugins", "cloud-account-guard")
PKG_REL = os.path.join("hooks", "cloud-account-guard")
STOP = ("deny", "ask")
CORPORA = ("boundary", "ctxopt", "shellvars", "lattice")


# --- 版の解決 ---------------------------------------------------------------------------------

def _repo_root():
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("git の作業ツリーの中で使うか、版を dir で渡してください")
    return out.stdout.strip()


def resolve_version(spec, work):
    """版の指定を hook パッケージの dir にする。"""
    for cand in (spec, os.path.join(spec, PKG_REL)):
        if os.path.isfile(os.path.join(cand, "core", "dispatcher.py")):
            return os.path.realpath(cand)
    root = _repo_root()
    rev = subprocess.run(["git", "rev-parse", "--verify", spec + "^{commit}"], cwd=root, capture_output=True,
                         text=True)
    if rev.returncode != 0:
        raise SystemExit(f"版が見つからない: {spec}")
    sha = rev.stdout.strip()
    dest = os.path.join(work, "versions", sha[:12])
    pkg = os.path.join(dest, PLUGIN_REL, PKG_REL)
    if not os.path.isfile(os.path.join(pkg, "core", "dispatcher.py")):
        data = subprocess.run(["git", "archive", "--format=tar", sha, "--", PLUGIN_REL], cwd=root,
                              capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            if hasattr(tarfile, "data_filter"):
                tf.extractall(dest, filter="data")
            else:  # Python 3.11 の古い patch 版
                tf.extractall(dest)
    return os.path.realpath(pkg)


def _files_hash(names):
    h = hashlib.sha256()
    for name in names:
        with open(os.path.join(HERE, name), "rb") as f:
            h.update(name.encode() + b"\0" + f.read())
    return h.hexdigest()


def _tree_hash(root, rels):
    h = hashlib.sha256()
    for rel in rels:
        base = os.path.join(root, rel)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(filenames):
                if name.endswith((".py", ".jsonl")):
                    p = os.path.join(dirpath, name)
                    h.update(os.path.relpath(p, root).encode())
                    with open(p, "rb") as f:
                        h.update(f.read())
    return h.hexdigest()


# --- 実行 ---------------------------------------------------------------------------------------

def _dump_decl(pkg, path):
    out = subprocess.run([sys.executable, "-B", "-I", os.path.join(HERE, "decl.py"), pkg],
                         capture_output=True, text=True, check=True).stdout
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    return json.loads(out)


def _run_jobs(jobs, limit):
    """jobs: [(label, argv, out_path)]。limit 個まで並べて流す。"""
    pending = list(jobs)
    running = []
    failed = []
    while pending or running:
        while pending and len(running) < limit:
            label, argv, out = pending.pop(0)
            err = open(out + ".err", "w", encoding="utf-8")
            p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=err, env=_child_env())
            running.append((label, p, out, err, time.monotonic()))
        time.sleep(0.2)
        for item in list(running):
            label, p, out, err, t0 = item
            if p.poll() is None:
                continue
            err.close()
            running.remove(item)
            if p.returncode != 0:
                failed.append(label)
                with open(out + ".err", encoding="utf-8") as ef:
                    sys.stderr.write(f"[{label}] exit {p.returncode}\n{ef.read()[-2000:]}\n")
            else:
                print(f"  {label}: {time.monotonic() - t0:.1f}s", flush=True)
    if failed:
        raise SystemExit(f"失敗したジョブ: {failed}")


def _child_env():
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("TMPDIR", "LANG"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _load(path):
    with open(path, encoding="utf-8") as f:
        return {r["id"]: r for r in map(json.loads, f) if r}


# --- 数える -------------------------------------------------------------------------------------

def _runs(shell_rec):
    out = []
    for sname in ("bash", "zsh"):
        runs = shell_rec.get(sname)
        if isinstance(runs, list):
            out.extend((sname, r) for r in runs)
    return out


def mismatches(runs, calls):
    """(組が違う run, 照合されなかった run)。run は (shell, run)。"""
    by_cli = collections.defaultdict(list)
    for c in calls:
        by_cli[c["cli"]].append(c["tuple"])
    bad, unverified = [], []
    for sname, r in runs:
        tuples = by_cli.get(r["cli"])
        if not tuples:
            unverified.append((sname, r))
            continue
        if decl.key(r["tuple"]) in {decl.key(t) for t in tuples}:
            continue
        nearest = min(tuples, key=lambda t: len(decl.diff_names(r["tuple"], t)))
        bad.append((sname, r, decl.diff_names(r["tuple"], nearest)))
    return bad, unverified


def tally(rows, old, new, shell):
    """コーパスの行と、旧新の hook 側・シェル側の記録から数を作る。"""
    s = {"inputs": 0, "timeout": 0, "split": 0, "exc_old": 0, "exc_new": 0,
         "trans": collections.Counter(), "dod": [], "new_abs": [], "old_abs": [], "unverified": [],
         "tighten": [], "fixed": []}
    for row in rows:
        i = row["id"]
        o, n, sh = old[i], new[i], shell[i]
        s["inputs"] += 1
        if "timeout" in (sh.get("bash"), sh.get("zsh")):
            s["timeout"] += 1
        bash, zsh = sh.get("bash"), sh.get("zsh")
        if isinstance(bash, list) and isinstance(zsh, list):
            if sorted(decl.key(r["tuple"]) for r in bash) != sorted(decl.key(r["tuple"]) for r in zsh):
                s["split"] += 1
        s["exc_old"] += o["d"] == "EXC"
        s["exc_new"] += n["d"] == "EXC"
        cli = (row.get("tag") or "?").split(":")[0]
        od = "stop" if o["d"] in STOP else ("EXC" if o["d"] == "EXC" else "allow")
        nd = "stop" if n["d"] in STOP else ("EXC" if n["d"] == "EXC" else "allow")
        s["trans"][(cli, f"{od}->{nd}")] += 1
        runs = _runs(sh)
        nbad, nunv = mismatches(runs, n["calls"])
        obad, _ = mismatches(runs, o["calls"])
        item = {"id": i, "tag": row.get("tag"), "cmd": row["cmd"], "launch": row.get("launch") or {}}
        if nd == "allow" and nbad:
            s["new_abs"].append({**item, "bad": _bad_summary(nbad)})
        if od == "allow" and obad:
            s["old_abs"].append({**item, "bad": _bad_summary(obad)})
        if od == "stop" and nd == "allow":
            if not nbad and not nunv:
                s["fixed"].append(item)
            if nbad:
                s["dod"].append({**item, "bad": _bad_summary(nbad), "old_why": o["why"],
                                 "new_calls": [c["tuple"] for c in n["calls"]]})
            if nunv:
                s["unverified"].append({**item, "runs": [[sn, r["cli"], r["args"][:4]] for sn, r in nunv]})
        if od == "allow" and nd == "stop":
            s["tighten"].append({**item, "why": n["why"]})
    return s


def _bad_summary(bad):
    out = []
    for sname, r, names in bad:
        out.append({"shell": sname, "cli": r["cli"], "names": names, "tuple": r["tuple"], "ok": r.get("ok")})
    return out


def wrong_target(item):
    """組が違う run のどれかが、world の規則で期待外の照合先 (ok が False) か。"""
    return any(b.get("ok") is False for b in item["bad"])


def _kind(item):
    names = {n for b in item["bad"] for n in b["names"]}
    parts = sorted({n.split(":")[0] for n in names})
    return "+".join(parts) or "same-names"


def _shells(item):
    return "+".join(sorted({b["shell"] for b in item["bad"]}))


def report(name, s, show):
    lines = [f"== {name}: 入力 {s['inputs']} / timeout {s['timeout']} / bash と zsh で組が割れた行 {s['split']}"
             f" / EXC 旧 {s['exc_old']} 新 {s['exc_new']}"]
    lines.append("  CLI ごとの遷移 (旧 -> 新): " + ", ".join(
        f"{cli} {t} {c}" for (cli, t), c in sorted(s["trans"].items())))
    for key, title in (("dod", "DOD (旧が止め、新が通し、組が違う)"), ("new_abs", "NEW_ABS (新が通し、組が違う)"),
                       ("old_abs", "OLD_ABS (旧が通し、組が違う)")):
        items = s[key]
        lines.append(f"  {title}: {len(items)} (うち CLI が world の規則で期待外の照合先で走る行 "
                     f"{sum(map(wrong_target, items))})")
        if not items:
            continue
        by_kind = collections.Counter(_kind(x) for x in items)
        by_shell = collections.Counter(_shells(x) for x in items)
        by_name = collections.Counter(n for x in items for n in sorted({n for b in x["bad"] for n in b["names"]}))
        by_tag = collections.Counter(":".join((x["tag"] or "?").split(":")[:2]) for x in items)
        lines.append("    違う部分: " + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items())))
        lines.append("    シェル: " + ", ".join(f"{k} {v}" for k, v in sorted(by_shell.items())))
        lines.append("    名前: " + ", ".join(f"{k} {v}" for k, v in by_name.most_common(12)))
        lines.append("    形 (tag): " + ", ".join(f"{k} {v}" for k, v in by_tag.most_common(12)))
        by_origin = collections.Counter((x["tag"] or "?").split(":")[-1] for x in items
                                        if (x["tag"] or "").count(":") >= 2)
        if by_origin and key == "dod":
            lines.append("    tag の末尾: " + ", ".join(f"{k} {v}" for k, v in by_origin.most_common(12)))
        for x in items[:show]:
            b = x["bad"][0]
            lines.append(f"    - {x['id']} [{x['tag']}] {x['cmd'][:150]!r} {b['shell']} {b['cli']} {b['names']}")
    lines.append(f"  UNVERIFIED (旧が止め、新が照合せずに通した CLI がある): {len(s['unverified'])}")
    for x in s["unverified"][:show]:
        lines.append(f"    - {x['id']} [{x['tag']}] {x['cmd'][:150]!r} {x['runs'][:2]}")
    fixed_by_tag = collections.Counter(":".join((x["tag"] or "?").split(":")[:2]) for x in s["fixed"])
    lines.append(f"  FIXED (旧が止め、新が通し、どの run の組も新の verify の組と一致): {len(s['fixed'])}"
                 + ("" if not s["fixed"] else " / " + ", ".join(f"{k} {v}" for k, v in fixed_by_tag.most_common(8))))
    lines.append(f"  TIGHTEN (旧が通し、新が止める): {len(s['tighten'])}")
    by_why = collections.Counter(x["why"][:80] for x in s["tighten"])
    for why, c in by_why.most_common(show):
        lines.append(f"    {c} × {why}")
    return "\n".join(lines)


def _jsonable(s):
    out = dict(s)
    out["trans"] = {f"{cli} {t}": c for (cli, t), c in sorted(s["trans"].items())}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--corpus", nargs="*", default=list(CORPORA))
    ap.add_argument("--work", default=None)
    ap.add_argument("--decl", default=None, help="名前の一覧を引く版 (既定はこの道具の置かれた版)")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--show", type=int, default=5)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    work = os.path.realpath(args.work or tempfile.mkdtemp(prefix="cag-oracle-"))
    os.makedirs(work, exist_ok=True)
    t0 = time.monotonic()
    old_pkg = resolve_version(args.old, work)
    new_pkg = resolve_version(args.new, work)
    decl_pkg = resolve_version(args.decl, work) if args.decl else os.path.dirname(TESTS)
    decl_path = os.path.join(work, "decl.json")
    decl_map = _dump_decl(decl_pkg, decl_path)
    # 記録の再利用の鍵: コーパスの中身・記録を作るファイル・宣言・版の中身 (比較器を直しても記録は使い回す)
    shell_tool = _files_hash(["shellside.py", "world.py", "decl.py"])
    hook_tool = _files_hash(["hookside.py", "world.py", "decl.py"])
    decl_hash = hashlib.sha256(json.dumps(decl_map, sort_keys=True).encode()).hexdigest()
    print(f"work: {work}\nold: {old_pkg}\nnew: {new_pkg}", flush=True)
    jobs, plan = [], []
    for name in args.corpus:
        label = os.path.splitext(os.path.basename(name))[0]
        d = os.path.join(work, label)
        os.makedirs(d, exist_ok=True)
        corpus_path = os.path.join(d, "corpus.jsonl")
        if os.path.isfile(name):
            with open(name, encoding="utf-8") as f:
                rows = [json.loads(x) for x in f if x.strip()]
        else:
            rows = corpus_mod.generate(name, decl_map)
        corpus_mod.write(rows, corpus_path)
        with open(corpus_path, "rb") as f:
            chash = hashlib.sha256(f.read()).hexdigest()
        sh_out = os.path.join(d, "shell-" + hashlib.sha256((chash + shell_tool + decl_hash).encode())
                              .hexdigest()[:16] + ".jsonl")
        outs = {}
        for side, pkg in (("old", old_pkg), ("new", new_pkg)):
            key = hashlib.sha256((chash + hook_tool + decl_hash + _tree_hash(pkg, ["core", "services"]))
                                 .encode()).hexdigest()[:16]
            outs[side] = os.path.join(d, f"hook-{key}.jsonl")
        if not os.path.isfile(sh_out):
            jobs.append((f"{label}/shell", [sys.executable, "-B", os.path.join(HERE, "shellside.py"),
                                            os.path.join(d, "w-shell"), decl_path, corpus_path, sh_out], sh_out))
        for side, pkg in (("old", old_pkg), ("new", new_pkg)):
            if not os.path.isfile(outs[side]) and all(j[2] != outs[side] for j in jobs):
                jobs.append((f"{label}/{side}", [sys.executable, "-B", "-I", os.path.join(HERE, "hookside.py"),
                                                  pkg, os.path.join(d, "w-" + side), decl_path, corpus_path,
                                                  outs[side]], outs[side]))
        plan.append((label, rows, outs["old"], outs["new"], sh_out))
    _run_jobs(jobs, max(1, args.jobs))
    summaries = {}
    total_dod = 0
    for label, rows, o_path, n_path, s_path in plan:
        s = tally(rows, _load(o_path), _load(n_path), _load(s_path))
        summaries[label] = s
        total_dod += len(s["dod"])
        print(report(label, s, args.show), flush=True)
    print(f"所要 {time.monotonic() - t0:.1f}s / DOD 合計 {total_dod}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({k: _jsonable(v) for k, v in summaries.items()}, f, ensure_ascii=False, indent=1)
    return 1 if total_dod else 0


if __name__ == "__main__":
    sys.exit(main())
