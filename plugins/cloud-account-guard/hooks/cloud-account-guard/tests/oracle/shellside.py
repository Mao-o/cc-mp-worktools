"""oracle (シェル側): コマンド 1 行を bash 3.2 と zsh -f で走らせ、偽の CLI が受け取った「照合を決める組」を出す。

usage: python3 -B shellside.py <world_dir> <decl.json> <corpus.jsonl> <out.jsonl>

偽の CLI (aws / gcloud / kubectl / firebase / gh) は、名前・`pwd -P`・argv・env 全体を 1 回の起動ごとに
1 ファイルへ書いて exit 0 する。偽の sudo は `-E` を外して exec する。作業ディレクトリは W/project、
HOME は W/home。env は行ごとの launch (起動時の env) と shellvars (シェルの変数。hook には見えない)。
コマンド語に絶対パスで書いた CLI (`/usr/bin/aws` など) は、偽物を迂回しないよう偽物のパスに置き換える。

出力の 1 行: {"id", "bash": [run...] | "timeout", "zsh": ...}。run = {"cli", "pwd", "args", "tuple", "ok"}。
tuple は decl.tuple_from_env の形 (パスは CLI の作業ディレクトリで解決した実体のパス)。ok は、その組で
world の規則が期待値に当たるか (組の違いのうち「CLI が期待外の照合先で走る」ものを分けるため。組の比較には
使わない)。
"""
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from oracle import decl, world  # noqa: E402

LOG_VAR = "CAG_ORACLE_LOG"
FS, RS = "\x1f", "\x1e"
FAKE = f"""#!/bin/sh
f=$(/usr/bin/mktemp "${LOG_VAR}.XXXXXX") || exit 0
{{ printf '%s\\037%s\\037' "$(/usr/bin/basename "$0")" "$(/bin/pwd -P)"
  for a in "$@"; do printf '%s\\036' "$a"; done
  printf '\\037'
  /usr/bin/env -0; }} > "$f"
exit 0
"""
SUDO = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -E) shift ;;
    -*) echo "fake sudo: unsupported $1" >&2; exit 98 ;;
    *) break ;;
  esac
done
exec "$@"
"""
ZSH = "/bin/zsh" if os.access("/bin/zsh", os.X_OK) else (shutil.which("zsh") or "/bin/zsh")
SHELLS = {"bash": ["/bin/bash", "-c"], "zsh": [ZSH, "-f", "-c"]}
TIMEOUT = 10


def available_shells():
    return [name for name, argv in SHELLS.items() if os.access(argv[0], os.X_OK)]


def setup(w):
    """世界と偽の CLI を作り、(project, fakebin, log) を返す。"""
    project = world.build(w)
    fakebin = os.path.join(w, "bin")
    os.makedirs(fakebin, exist_ok=True)
    for name, text in [(c, FAKE) for c in decl.CLIS] + [("sudo", SUDO)]:
        p = os.path.join(fakebin, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(p, 0o755)
    return project, fakebin, os.path.join(w, "shell.log")


def _parse(text):
    name, pwd, args_s, env_s = text.split(FS, 3)
    args = args_s.split(RS)[:-1]
    env = {}
    for item in env_s.split("\0"):
        k, eq, v = item.partition("=")
        if eq:
            env[k] = v
    env.pop(LOG_VAR, None)
    return name, pwd, args, env


def run_line(w, d, case, fakebin, project, log):
    """1 行をシェルごとに走らせ、{shell: [run...] | "timeout"} を返す。"""
    abs_cli = re.compile(r"(?<![\w/.-])/(?:usr/)?(?:local/)?(?:s?bin)/(" + "|".join(decl.CLIS) + r")\b")
    cmd = case["cmd"].replace("{W}", w)
    cmd = abs_cli.sub(lambda m: os.path.join(fakebin, m.group(1)), cmd)
    env = {"PATH": fakebin + os.pathsep + "/usr/bin:/bin", "HOME": os.path.join(w, "home"), LOG_VAR: log}
    env.update({k: v.replace("{W}", w) for k, v in (case.get("launch") or {}).items()})
    env.update({k: v.replace("{W}", w) for k, v in (case.get("shellvars") or {}).items()})
    logdir = os.path.dirname(log)
    prefix = os.path.basename(log) + "."
    out = {}
    for sname in available_shells():
        for x in os.listdir(logdir):
            if x.startswith(prefix):
                os.unlink(os.path.join(logdir, x))
        try:
            subprocess.run(SHELLS[sname] + [cmd + "\nwait"], cwd=project, env=env, capture_output=True,
                           timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            out[sname] = "timeout"
            continue
        runs = []
        for x in sorted(os.listdir(logdir)):
            if not x.startswith(prefix):
                continue
            with open(os.path.join(logdir, x), encoding="utf-8", errors="surrogateescape", newline="") as lf:
                text = lf.read()
            if text.count(FS) < 3:
                continue  # 書き終わる前に止まった起動 (背景の CLI など)
            name, pwd, args, cenv = _parse(text)
            entry = d.get(name)
            if entry is None:
                continue
            ctx = decl.context_from_argv(entry, args)
            runs.append({"cli": name, "pwd": decl.short(pwd, w), "args": [decl.short(a, w) for a in args],
                         "tuple": decl.tuple_from_env(entry, cenv, ctx, pwd, w),
                         "ok": world.OK[name](cenv, pwd, ctx)})
        runs.sort(key=lambda r: (r["cli"], decl.key(r["tuple"]), r["args"]))
        out[sname] = runs
    return out


def main(argv):
    w, decl_path, corpus, out_path = argv
    os.makedirs(w, exist_ok=True)
    w = os.path.realpath(w)
    d = decl.load(decl_path)
    project, fakebin, log = setup(w)
    n = 0
    with open(corpus, encoding="utf-8") as f, open(out_path, "w", encoding="utf-8") as out:
        for line in f:
            if not line.strip():
                continue
            case = json.loads(line)
            rec = {"id": case["id"], **run_line(w, d, case, fakebin, project, log)}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(n)


if __name__ == "__main__":
    main(sys.argv[1:])
