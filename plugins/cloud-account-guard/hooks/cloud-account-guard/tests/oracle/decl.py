"""「照合を決める組」の定義: 名前の一覧は各 service の宣言から引く (この道具は名前を二重に持たない)。

組 = その CLI が読む環境変数 (`IDENTITY_ENV_VARS` に当たる名前と `IDENTITY_ENV_PREFIXES` で始まる名前)
   + context option (`CONTEXT_OPTIONS` の option。キーは宣言の論理名 `profile` / `context` など)。

シェル側 (oracle) は偽の CLI が受け取った env と argv から、hook 側 (hookside) は verify に渡った env と
context から、同じ関数 (`tuple_from_env`) で組を作る。パスの値は、その CLI が走るディレクトリで解決した
実体のパスにして比べる (`canon`)。

宣言の読み出しは別プロセスで行う (`python3 -B -I decl.py <hook_pkg_dir>` が JSON を出す)。版の違う
`services` パッケージを、比べる版と同じプロセスに import しないため。

パスとして解決する名前 (`PATH_VALUED`) だけはこのファイルが持つ: 宣言には「値がパスか」の区別が無い。
ここに無い名前の値は、絶対パスなら実体のパスに、それ以外は書いたままの文字列で比べる (相対パスの値を
持つ新しい名前が宣言に増えたら、ここにも足す。足さないと、走るディレクトリの違いを数えない)。
"""
import json
import os
import re
import sys

# CLI の名前 (偽の CLI を置く名前)。service との対応は宣言の PATTERNS で決める
CLIS = ("aws", "gcloud", "kubectl", "firebase", "gh")

# 値がパスの名前 (env) と context のキー。`:` 区切りのリストは LIST_VALUED
PATH_VALUED = frozenset({
    "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CA_BUNDLE",
    "AWS_DATA_PATH", "CLOUDSDK_CONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "KUBECONFIG", "HOME",
    "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "APPDATA",
    # context のキー
    "kubeconfig", "config",
})
LIST_VALUED = frozenset({"KUBECONFIG", "AWS_DATA_PATH"})


def dump(pkg):
    """hook パッケージ dir の services の宣言を {cli: {...}} にする (別プロセスで呼ぶ)。"""
    sys.path.insert(0, pkg)
    import services  # noqa: E402

    out = {}
    for svc in services.ALL:
        cli = next((c for c in CLIS if any(re.search(p, c + " x") for p in svc.PATTERNS)), None)
        if cli is None:
            raise SystemExit(f"CLI が決まらない service: {svc.__name__}")
        out[cli] = {
            "service": svc.__name__.rsplit(".", 1)[-1],
            "names": sorted(getattr(svc, "IDENTITY_ENV_VARS", ())),
            "prefixes": list(getattr(svc, "IDENTITY_ENV_PREFIXES", ())),
            "context": dict(getattr(svc, "CONTEXT_OPTIONS", {})),
        }
    return out


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def short(text, w):
    """W (実体のパス) を `<W>` に置き換える (世界のディレクトリが違っても同じ文字列にする)。"""
    return text.replace(w, "<W>") if isinstance(text, str) else text


def _canon_one(value, base):
    if value == "" or value.startswith("~"):
        return value
    if os.path.isabs(value):
        return os.path.realpath(value)
    return os.path.realpath(os.path.join(base, value))


def canon(name, value, base):
    """組に入れる値。PATH_VALUED の名前は base (走るディレクトリ) で解決した実体のパス。"""
    if not isinstance(value, str):
        return value
    if name in PATH_VALUED:
        if name in LIST_VALUED:
            return ":".join(_canon_one(part, base) for part in value.split(":"))
        return _canon_one(value, base)
    if os.path.isabs(value):
        return os.path.realpath(value)
    return value


def identity_names(entry, env):
    names = set(entry["names"])
    prefixes = tuple(entry["prefixes"])
    return sorted(n for n in env if n in names or (prefixes and n.startswith(prefixes)))


def tuple_from_env(entry, env, ctx, base, w):
    """(env の組, context の組) を JSON にできる dict で返す。値は `<W>` に置き換え済み。"""
    keys = set(entry["context"].values())
    e = {n: short(canon(n, env[n], base), w) for n in identity_names(entry, env)}
    c = {k: short(canon(k, v, base), w) for k, v in sorted((ctx or {}).items()) if k in keys}
    return {"env": e, "ctx": c}


def context_from_argv(entry, argv):
    """偽の CLI が受け取った argv から context option の値を拾う (CLI の解析の近似)。

    `--opt v` / `--opt=v`、1 文字の option は `-P v` / `-Pv` と、前に値を取らない 1 文字の option を
    続けた結合形 (`-jP v` / `-jPv`)。`--` より後ろは見ない。同じ option は最後が有効。
    """
    opts = entry["context"]
    longs = {o: k for o, k in opts.items() if o.startswith("--")}
    shorts = {o[1]: k for o, k in opts.items() if len(o) == 2 and o[0] == "-" and o[1] != "-"}
    ctx = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name in longs:
                if eq:
                    ctx[longs[name]] = val
                elif i + 1 < len(argv):
                    ctx[longs[name]] = argv[i + 1]
                    i += 1
        elif a.startswith("-") and len(a) > 1 and shorts:
            for j in range(1, len(a)):
                key = shorts.get(a[j])
                if key is None:
                    continue
                rest = a[j + 1:]
                if rest:
                    ctx[key] = rest
                elif i + 1 < len(argv):
                    ctx[key] = argv[i + 1]
                    i += 1
                break
        i += 1
    return ctx


def key(tup):
    return json.dumps(tup, sort_keys=True, ensure_ascii=False)


def diff_names(a, b):
    """2 つの組で値が違う名前 (`env:NAME` / `ctx:key`)。"""
    out = []
    for part in ("env", "ctx"):
        for n in sorted(set(a[part]) | set(b[part])):
            if a[part].get(n) != b[part].get(n):
                out.append(f"{part}:{n}")
    return out


if __name__ == "__main__":
    print(json.dumps(dump(os.path.realpath(sys.argv[1])), ensure_ascii=False, sort_keys=True))
