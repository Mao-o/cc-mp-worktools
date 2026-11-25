"""コーパスの生成器。どれも機械的に作る (手で選んだ形は boundary.jsonl の記録だけ)。

usage: python3 corpus.py <name> <out.jsonl>      name: lattice / boundary / shellvars / ctxopt

1 行 = {"id", "tag", "cmd", "launch" (起動時の env。hook とシェルの両方), "shellvars" (シェルにだけある変数)}。
`{W}` は世界のディレクトリに置き換わる (world.py)。tag の先頭は CLI 名 (比較器が CLI ごとに数える)。

- lattice: 名前 2 つ × 値を決める書き方 7 種 × 7 種 × 区切り 3 × 書いた順序 2 × 起動時の env 2
- boundary: 前段の export を照合に重ねる実装案 (採用していない) のゲートの両側と、その案へのマージ前レビューの
  指摘の形 (boundary.jsonl。1 行 = origin / tag / cmd / launch / shellvars。tag の末尾に origin を付ける)
- shellvars: bash 3.2 と zsh -f が持つ変数を実際に列挙し (`declare -p` の属性、
  `${(k)parameters}` の型)、全変数 × 代入の書き方。照合用の名前の側の書き方 (`${NAME::=v}` など) と、
  対照 (起動時は良い値で、コマンドが静的に悪い値を書く形) も
- ctxopt: 値の決まらない context option (`$X`・`${X:-d}`・`$(...)`・backtick) × CLI の宣言の option
  × 前置き (なし / 前段の export / 行頭の代入)。対照に値の決まる option も入れる
"""
import itertools
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from oracle import world  # noqa: E402

CLI = {
    "aws": "aws s3 rm s3://b/k",
    "gcloud": "gcloud run deploy svc",
    "kubectl": "kubectl apply -f x",
    "firebase": "firebase deploy",
    "gh": "gh pr create",
}
GOOD, BAD = world.GOOD, world.BAD


# --- lattice ----------------------------------------------------------------------------------

PAIRS = [
    ("aws", "AWS_CONFIG_FILE", "AWS_PROFILE"),
    ("aws", "AWS_PROFILE", "AWS_ACCESS_KEY_ID"),
    ("gcloud", "CLOUDSDK_CONFIG", "CLOUDSDK_ACTIVE_CONFIG_NAME"),
    ("gcloud", "CLOUDSDK_ACTIVE_CONFIG_NAME", "CLOUDSDK_CORE_ACCOUNT"),
    ("kubectl", "KUBECONFIG", "HOME"),
    ("firebase", "XDG_CONFIG_HOME", "HOME"),
    ("gh", "GH_CONFIG_DIR", "GH_TOKEN"),
]
# 値の決まらない値 (シェルでは悪い値になる) のうち、パスの名前の相対パス
DYN_PATH = {
    "AWS_CONFIG_FILE": ".aws/config", "CLOUDSDK_CONFIG": ".gcloud", "KUBECONFIG": "bad.yaml", "HOME": "h",
    "XDG_CONFIG_HOME": "xdgbad", "GH_CONFIG_DIR": "ghbad",
}
LOADERS = [
    "source .env.{n}", ". ./.env.{n}", "set -a; . ./.env.{n}; set +a", "export $(cat .env.{n} | xargs)",
    "export $(grep -v '^#' .env.{n} | xargs)", "while read -r l; do export \"$l\"; done < .env.{n}",
    "eval \"$(cat .env.{n})\"",
]
REWRITES = ["read {n} < f.{n}", "printf -v {n} %s {bad}", "for {n} in {bad}; do :; done"]


def _dyn_values(name):
    var = "V_" + name
    if name in DYN_PATH:
        p = DYN_PATH[name]
        return [f"$PWD/{p}", f"$(pwd)/{p}", f"${var}", f"`pwd`/{p}", "~/nonexistent-" + name.lower()], var
    bad = BAD[name]
    return [f"${var}", f"$(echo {bad})", f"`echo {bad}`", f"\"${var}\"", "~/x"], var


def _static_values(name):
    v = GOOD[name]
    forms = [v, f"'{v}'", f'"{v}"']
    if name == "KUBECONFIG":
        forms.append("k.yaml")  # 相対パス (Bash の作業ディレクトリの k.yaml = 期待値)
    return forms


def _fragment(name, form, k):
    """(前段のセグメント, 行頭の代入, env wrapper の代入, shellvars)"""
    if form == 1:  # 起動時 (書かない)
        return [], [], [], {}
    if form == 2:  # 静的な export
        vals = _static_values(name)
        return [f"export {name}={vals[k % len(vals)]}"], [], [], {}
    if form == 3:  # 値の決まらない export
        vals, var = _dyn_values(name)
        return [f"export {name}={vals[k % len(vals)]}"], [], [], {var: BAD[name]}
    if form == 4:  # 静的な行頭の代入
        return [], [f"{name}={GOOD[name]}"], [], {}
    if form == 5:  # 値の決まらない行頭の代入
        vals, var = _dyn_values(name)
        sv = {var: BAD[name]}
        pick = k % 3
        if pick == 2:
            return [], [], [f"{name}={vals[2]}"], sv
        return [], [f"{name}={vals[pick]}"], [], sv
    if form == 6:  # 名前を書かない読み込み
        return [LOADERS[k % len(LOADERS)].format(n=name)], [], [], {}
    if form == 7:  # 組み込みでの書き換え
        return [REWRITES[k % len(REWRITES)].format(n=name, bad=BAD[name])], [], [], {}
    raise ValueError(form)


def lattice():
    rows = []
    for svc, a, b in PAIRS:
        for fa, fb in itertools.product(range(1, 8), repeat=2):
            for k, (sep, order, launch_kind) in enumerate(
                itertools.product((" && ", "; ", "\n"), (0, 1), ("unset", "bad"))
            ):
                sa, ia, wa, va = _fragment(a, fa, k)
                sb, ib, wb, vb = _fragment(b, fb, k + 1)
                first, second = ((sa, ia, wa), (sb, ib, wb)) if order == 0 else ((sb, ib, wb), (sa, ia, wa))
                inline = first[1] + second[1]
                wrap = first[2] + second[2]
                cli = CLI[svc]
                if wrap:
                    cli = "env " + " ".join(inline + wrap) + " " + cli
                elif inline:
                    cli = " ".join(inline) + " " + cli
                cmd = sep.join(first[0] + second[0] + [cli])
                launch = {x: BAD[x] for x in (a, b) if x != "HOME"} if launch_kind == "bad" else {}
                rows.append({"tag": f"{svc}:{a}/{b}:{fa}{fb}", "cmd": cmd, "launch": launch,
                             "shellvars": {**va, **vb}})
    return rows


# --- boundary ---------------------------------------------------------------------------------

def boundary():
    rows = []
    with open(os.path.join(HERE, "boundary.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            rows.append({"tag": f"{r['tag']}:{r['origin']}", "cmd": r["cmd"], "launch": r.get("launch") or {},
                         "shellvars": r.get("shellvars") or {}})
    return rows


# --- shellvars --------------------------------------------------------------------------------

_MIN_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home"}
# 機械の列挙で拾えない変数 (zsh -f の `${(k)parameters}` に出ない特殊な変数)。列挙の補助だけ
SUPPLEMENT = ("ERRNO", "TMOUT", "PERIOD", "BAUD", "DIRSTACKSIZE", "REPORTTIME", "REPORTMEMORY", "LOGCHECK")


def _sh(argv):
    try:
        return subprocess.run(argv, env=_MIN_ENV, capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def enumerate_shell_vars():
    """{名前: 型の説明} (例 `zsh:integer-special bash:-i`)。シェルが無ければその分は空。"""
    kinds = {}

    def add(name, kind):
        if name and (name[0].isalpha() or name[0] == "_") and all(c.isalnum() or c == "_" for c in name):
            kinds.setdefault(name, []).append(kind)

    zsh = "/bin/zsh" if os.access("/bin/zsh", os.X_OK) else (shutil.which("zsh") or "/bin/zsh")
    out = _sh([zsh, "-f", "-c", 'for k in ${(k)parameters}; do print -r -- "$k ${parameters[$k]}"; done'])
    for line in out.splitlines():
        name, _, kind = line.partition(" ")
        add(name, "zsh:" + kind)
    # bash 3.2 の `declare -p` (引数なし) は属性を出さないので、`compgen -v` の名前ごとの `declare -p` と、
    # 属性ごとの一覧 (`declare -a` など。関数の外で未設定の FUNCNAME は compgen -v に出ない) を合わせる
    script = ('for n in $(compgen -v); do declare -p "$n" 2>/dev/null | head -n 1; done; '
              'declare -a; declare -i; declare -r; declare -x')
    battrs = {}
    for line in _sh(["/bin/bash", "-c", script]).splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "declare":
            battrs.setdefault(parts[2].partition("=")[0], set()).update(parts[1].lstrip("-").replace("-", ""))
    for name, a in battrs.items():
        add(name, "bash:-" + ("".join(sorted(a)) or "-"))
    for name in SUPPLEMENT:
        add(name, "supplement")
    return {n: " ".join(k) for n, k in sorted(kinds.items())}


# 照合用の名前の代表 (CLI ごとに 1 つ)
REP = {"kubectl": "KUBECONFIG", "aws": "AWS_PROFILE", "gcloud": "CLOUDSDK_ACTIVE_CONFIG_NAME"}
# 変数 X への代入の書き方 ({K} = 照合用の名前、{G} = 良い値、{X} = 列挙した変数、{C} = CLI)
X_FORMS = {
    "export-same": "export {K}={G} {X}={K}=0; {C}",
    "export-chain": "export {K}={G} && export {X}={K}=0 && {C}",
    "export-then-inline": "export {K}={G} && {X}={K}=0 {C}",
    "inline-both": "{K}={G} {X}={K}=0 {C}",
    "plain": "export {K}={G}; {X}={K}=0; {C}",
    "typeset": "export {K}={G}; typeset {X}={K}=0; {C}",
    "declare": "export {K}={G}; declare {X}={K}=0; {C}",
    "read": "export {K}={G}; read {X} <<< '{K}=0'; {C}",
    "printf-v": "export {K}={G}; printf -v {X} %s '{K}=0'; {C}",
}
# 照合用の名前の側の書き方 ({B} = 悪い値)
NAME_FORMS = {
    "arg-assign-default": "export {K}={G} && {C} ${{{K}::={B}}}",
    "arg-assign-default-dq": "export {K}={G} && {C} \"${{{K}::={B}}}\"",
    "arg-assign-colon": "export {K}= && {C} ${{{K}:={B}}}",
    "arg-assign-unset": "export {K}={G} && {C} ${{{K}={B}}}",
    "arg-arith": "export {K}={G} && {C} $(({K}=0))",
    "arg-arith-old": "export {K}={G} && {C} $[{K}=0]",
    "prefix-assign": "export {K}={G} && : ${{{K}::={B}}} && {C}",
    "prefix-arith": "export {K}={G}; : $(({K}=0)); {C}",
    "inline-arg-assign": "{K}={G} {C} ${{{K}::={B}}}",
    "inline-arg-arith": "{K}={G} {C} $(({K}=0))",
}
# 対照: 起動時は良い値で、コマンドが静的に悪い値を書く (hook が値を読む形。読み落とすと旧が止めた行を通す)
CONTROL_FORMS = {
    "ctrl-inline-bad": "{K}={B} {C}",
    "ctrl-export-bad": "export {K}={B} && {C}",
}


def shellvars():
    rows = []
    kinds = enumerate_shell_vars()
    for x, kind in kinds.items():
        for cli, k in REP.items():
            if x == k:
                continue
            for fname, form in X_FORMS.items():
                cmd = form.format(K=k, G=GOOD[k], X=x, C=CLI[cli])
                rows.append({"tag": f"{cli}:var:{x}:{fname}", "kind": kind, "cmd": cmd,
                             "launch": {k: BAD[k]}, "shellvars": {}})
    for k in GOOD:
        cli = next((c for c, rep in REP.items() if rep == k), None) or _cli_of(k)
        for fname, form in NAME_FORMS.items():
            cmd = form.format(K=k, G=GOOD[k], B=BAD[k], C=CLI[cli])
            for launch_kind in ("bad", "unset"):
                launch = {k: BAD[k]} if launch_kind == "bad" and k != "HOME" else {}
                rows.append({"tag": f"{cli}:name:{k}:{fname}:{launch_kind}", "cmd": cmd, "launch": launch,
                             "shellvars": {}})
        for fname, form in CONTROL_FORMS.items():
            rows.append({"tag": f"{cli}:name:{k}:{fname}:good", "cmd": form.format(K=k, B=BAD[k], C=CLI[cli]),
                         "launch": {k: GOOD[k]}, "shellvars": {}})
    return rows


def _cli_of(name):
    if name.startswith("AWS_"):
        return "aws"
    if name.startswith("CLOUDSDK_"):
        return "gcloud"
    if name.startswith("GH_"):
        return "gh"
    if name == "XDG_CONFIG_HOME":
        return "firebase"
    return "kubectl"


# --- ctxopt -----------------------------------------------------------------------------------

# 宣言の CONTEXT_OPTIONS (option → 論理名)。名前はここで持たず、decl の出力から読む
VALUE_FORMS = {
    "var": "{o} $X", "var-dq": "{o} \"$X\"", "var-brace": "{o} ${{X}}", "var-default": "{o} ${{X:-d}}",
    "subst": "{o} $(echo {b})", "backtick": "{o} `echo {b}`", "eq-var": "{o}=$X",
    # 対照: 値の決まる context option (hook は値を照合に使う。組は一致するはず)
    "static": "{o} {b}", "eq-static": "{o}={b}",
}
# 照合の既定を決める名前 (前段の export / 行頭の代入で良い値にする)
DEFAULT_NAME = {"aws": "AWS_CONFIG_FILE", "gcloud": "CLOUDSDK_CONFIG", "kubectl": "KUBECONFIG",
                "firebase": "XDG_CONFIG_HOME"}


def ctxopt(decl_map):
    rows = []
    for cli, entry in sorted(decl_map.items()):
        name = DEFAULT_NAME.get(cli)
        if not entry["context"] or name is None:
            continue
        for opt, key in sorted(entry["context"].items()):
            bad = world.BAD_CONTEXT[key]
            for fname, form in VALUE_FORMS.items():
                if fname.startswith("eq-") and not opt.startswith("--"):
                    continue
                arg = form.format(o=opt, b=bad)
                for pname, prefix, launch in (
                    ("launch", "", {name: GOOD[name]}),
                    ("export", f"export {name}={GOOD[name]} && ", {name: BAD[name]}),
                    ("inline", f"{name}={GOOD[name]} ", {name: BAD[name]}),
                ):
                    parts = CLI[cli].split(" ", 1)
                    cmd = f"{prefix}{parts[0]} {arg} {parts[1]}"
                    rows.append({"tag": f"{cli}:ctx:{opt}:{fname}:{pname}", "cmd": cmd, "launch": launch,
                                 "shellvars": {"X": bad}})
    return rows


GENERATORS = {"lattice": lattice, "boundary": boundary, "shellvars": shellvars, "ctxopt": ctxopt}


def generate(name, decl_map=None):
    rows = GENERATORS[name](decl_map) if name == "ctxopt" else GENERATORS[name]()
    for i, r in enumerate(rows, 1):
        r["id"] = f"{name}-{i}"
    return rows


def write(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    name, out = sys.argv[1], sys.argv[2]
    decl_map = None
    if name == "ctxopt":
        from oracle import decl
        decl_map = decl.load(sys.argv[3])
    rows = generate(name, decl_map)
    write(rows, out)
    print(len(rows))
