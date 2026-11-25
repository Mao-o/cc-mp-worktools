"""hook 側の組: コーパスの行を 1 つの版の dispatch に流し、判定と verify に渡った組を記録する。

usage: python3 -B -I hookside.py <hook_pkg_dir> <world_dir> <decl.json> <corpus.jsonl> <out.jsonl>

verify は world の規則で一致 / 不一致を返す偽物。CLI は起動しない (subprocess.run は AssertionError)。
hook プロセスの env は行ごとに作り直す (外側の env を持ち込まない): PATH・HOME (W/home)・
CLAUDE_PROJECT_DIR (W/project)・TMPDIR (行ごとに空の成功 cache) と、行の launch (起動時の env)。
hook プロセスの作業ディレクトリは W/hookcwd (Bash の作業ディレクトリ W/project とは別)。

verify に渡った組のパスは、実物の verify が CLI を起動するディレクトリで解決する:
kubectl は context の "cwd" (相対パスの KUBECONFIG を解決した走るディレクトリ) があればそこ、firebase は
project root (`--config` があればそのファイルのディレクトリ)、ほかは hook プロセスの作業ディレクトリ。

出力の 1 行: {"id", "d" (allow / deny / ask / context / EXC), "why", "calls": [{"cli", "tuple", "ok"}]}。
"""
import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PKG, W, DECL, CORPUS, OUT = sys.argv[1:6]
os.makedirs(W, exist_ok=True)
W = os.path.realpath(W)
PKG = os.path.realpath(PKG)
sys.path.insert(0, os.path.dirname(HERE))
from oracle import decl, world  # noqa: E402

D = decl.load(DECL)
PROJECT, HOME, HOOKCWD = world.paths(W)
world.build(W)
os.chdir(HOOKCWD)
CACHE = os.path.join(W, "cache")
BASE_ENV = {"PATH": "/usr/bin:/bin", "HOME": HOME, "CLAUDE_PROJECT_DIR": PROJECT, "TMPDIR": CACHE}
os.environ.clear()
os.environ.update(BASE_ENV)
mock.patch.object(Path, "home", staticmethod(lambda: Path(HOME))).start()
sys.path.insert(0, PKG)
import services  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402

BY_SERVICE = {entry["service"]: cli for cli, entry in D.items()}


def _base(cli, ctx, project_dir):
    if cli == "kubectl" and ctx.get("cwd"):
        return ctx["cwd"]
    if cli == "firebase":
        return os.path.dirname(ctx["config"]) if ctx.get("config") else project_dir
    return os.getcwd()


def _fake(cli, calls):
    def verify(_expected, project_dir, env=None, context=None):
        full = dict(env) if env is not None else dict(os.environ)
        ctx = dict(context or {})
        base = _base(cli, ctx, project_dir)
        ok = world.OK[cli](full, base, ctx)
        calls.append({"cli": cli, "tuple": decl.tuple_from_env(D[cli], full, ctx, base, W), "ok": ok})
        return None if ok else f"{cli}: 不一致 (偽の verify)"
    return verify


def run_one(case):
    calls = []
    env = dict(BASE_ENV)
    env.update({k: v.replace("{W}", W) for k, v in (case.get("launch") or {}).items()})
    if os.path.exists(CACHE):
        shutil.rmtree(CACHE)
    os.makedirs(CACHE)
    patches = [mock.patch.object(svc, "verify", side_effect=_fake(BY_SERVICE[svc.__name__.rsplit(".", 1)[-1]], calls))
               for svc in services.ALL]
    patches.append(mock.patch("subprocess.run", side_effect=AssertionError("CLI を起動しない")))
    with mock.patch.dict(os.environ, env, clear=True):
        for p in patches:
            p.start()
        try:
            r = dispatch(case["cmd"].replace("{W}", W), PROJECT)
        except Exception as e:  # noqa: BLE001
            return "EXC", repr(e)[:200], calls
        finally:
            for p in patches:
                p.stop()
    if r is None:
        return "allow", "", calls
    h = r["hookSpecificOutput"]
    reason = decl.short(h.get("permissionDecisionReason") or h.get("additionalContext") or "", W)
    lines = [x for x in reason.splitlines() if x.strip()]
    head = lines[1] if len(lines) > 1 and h.get("permissionDecision") else (lines[0] if lines else "")
    return h.get("permissionDecision") or "context", head[:200], calls


def main():
    n = 0
    with open(CORPUS, encoding="utf-8") as f, open(OUT, "w", encoding="utf-8") as out:
        for line in f:
            if not line.strip():
                continue
            case = json.loads(line)
            d, why, calls = run_one(case)
            out.write(json.dumps({"id": case["id"], "d": d, "why": why, "calls": calls}, ensure_ascii=False) + "\n")
            n += 1
    shutil.rmtree(CACHE, ignore_errors=True)
    print(n)


main()
