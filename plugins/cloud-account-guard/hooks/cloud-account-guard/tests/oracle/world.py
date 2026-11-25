"""比較の世界: 設定ファイルを置いたディレクトリと、偽の verify が一致 / 不一致を決める規則。

oracle (シェル側) と hookside (hook 側) は、それぞれ自分の世界のディレクトリ W を作る。同じ中身を
作るので、組に出るパスは W を `<W>` に置き換えて比べる (`decl.short`)。

この世界の規則は「旧版と新版が deny / allow のどちらにするか」を決めるためだけにある。比較器が
数える「組の違い」は、この規則に依らない (シェルの CLI が受け取った env と argv から決める)。

ディレクトリ:
  W/home (起動時の HOME): aws default=222 / prod=123 (期待値) / dev=222、gcloud default=other / prod=期待値、
         kube=other、gh=other、firebase=otherproj
  W/home2 (前段で export する良い HOME): 全部期待値
  W/good: 絶対パスで export する良い設定 (aws の config は dev=222 だけ悪い)
  W/project (Bash の作業ディレクトリ・起動 repo): ./.aws/config は prod=333 / default=444、.gcloud は evil、
         k.yaml=ctx / bad.yaml=other、h/.kube/config=other、.env 系は悪い値
  W/hookcwd (hook プロセスの作業ディレクトリ): 相対パスをここで読むと期待値に見える罠 (aws / gcloud / kube)
"""
import json
import os

# 期待値 (W/project/.claude/cloud-account-guard/accounts.local.json)
ACCOUNTS = {"aws": "123456789012", "github": "me", "firebase": "proj", "kubectl": "ctx",
            "gcloud": "a@example.com"}
KEYS = {"AKIAGOOD": "123456789012"}
TOKENS = {"tokgood": "me"}

# 名前ごとの「良い値」(前段の export / 行頭の代入で書く、期待値になる値)
GOOD = {
    "AWS_CONFIG_FILE": "{W}/good/aws/config",
    "AWS_PROFILE": "prod",
    "AWS_ACCESS_KEY_ID": "AKIAGOOD",
    "CLOUDSDK_CONFIG": "{W}/good/gcloud",
    "CLOUDSDK_ACTIVE_CONFIG_NAME": "prod",
    "CLOUDSDK_CORE_ACCOUNT": "a@example.com",
    "KUBECONFIG": "{W}/good/k.yaml",
    "HOME": "{W}/home2",
    "XDG_CONFIG_HOME": "{W}/good/xdg",
    "GH_CONFIG_DIR": "{W}/good/gh",
    "GH_TOKEN": "tokgood",
}
# 名前ごとの「悪い値」(起動時の期待外の値・.env.<名前> / f.<名前> の中身)
BAD = {
    "AWS_CONFIG_FILE": "{W}/project/.aws/config",
    "AWS_PROFILE": "dev",
    "AWS_ACCESS_KEY_ID": "AKIABAD",
    "CLOUDSDK_CONFIG": "{W}/project/.gcloud",
    "CLOUDSDK_ACTIVE_CONFIG_NAME": "dev",
    "CLOUDSDK_CORE_ACCOUNT": "evil@example.com",
    "KUBECONFIG": "{W}/project/bad.yaml",
    "HOME": "{W}/project/h",
    "XDG_CONFIG_HOME": "{W}/project/xdgbad",
    "GH_CONFIG_DIR": "{W}/project/ghbad",
    "GH_TOKEN": "tokbad",
}
# context option の悪い値 (値の決まらない context option のシェル変数に入れる)
BAD_CONTEXT = {
    "profile": "dev",
    "context": "other",
    "kubeconfig": "{W}/project/bad.yaml",
    "project": "other-proj",
    "account": "evil@example.com",
    "configuration": "dev",
    "config": "{W}/project/a/firebase.json",
}


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _gcloud(d, active, configs):
    _w(os.path.join(d, "active_config"), active)
    for name, acct in configs.items():
        _w(os.path.join(d, "configurations", "config_" + name), "account=" + acct)


def paths(w):
    """(project, home, hookcwd)"""
    return os.path.join(w, "project"), os.path.join(w, "home"), os.path.join(w, "hookcwd")


def build(w):
    """W に世界を作り、W/project を返す。何度呼んでも同じ中身になる。"""
    project, home, hookcwd = paths(w)
    home2 = os.path.join(w, "home2")
    good = os.path.join(w, "good")
    for d in (project, os.path.join(project, "a")):
        _w(os.path.join(d, "k.yaml"), "ctx")
        _w(os.path.join(d, "bad.yaml"), "other")
        _w(os.path.join(d, ".aws", "config"), "default=444444444444\nprod=333333333333\n")
        _w(os.path.join(d, "firebase.json"), "{}")
        _w(os.path.join(d, ".firebaserc"), '{"projects": {"default": "proj"}}')
    _w(os.path.join(project, "a", ".firebaserc"), '{"projects": {"default": "otherproj"}}')
    os.makedirs(os.path.join(project, "sub"), exist_ok=True)
    _w(os.path.join(project, "sub", "k.yaml"), "other")
    _w(os.path.join(project, "launch.yaml"), "ctx")
    _w(os.path.join(project, "a", "launch.yaml"), "other")
    _gcloud(os.path.join(project, ".gcloud"), "default",
            {"default": "evil@example.com", "prod": "evil@example.com"})
    os.makedirs(os.path.join(project, ".git"), exist_ok=True)
    _w(os.path.join(project, "key.txt"), "AKIABAD")
    _w(os.path.join(project, ".claude", "cloud-account-guard", "accounts.local.json"), json.dumps(ACCOUNTS))
    _w(os.path.join(project, "h", ".kube", "config"), "other")
    _w(os.path.join(project, "ghbad", "hosts"), "other")
    _w(os.path.join(project, "xdgbad", "configstore", "firebase-tools.json"), "otherproj")
    _w(os.path.join(project, "xdgbad", "gh", "hosts"), "other")
    # 名前を書かない読み込み (dotenv) の材料。どれも悪い値
    _w(os.path.join(project, ".env"), "AWS_PROFILE=dev\nAPP=x\n")
    _w(os.path.join(project, ".env.keys"), "AWS_ACCESS_KEY_ID=AKIABAD\nAWS_SECRET_ACCESS_KEY=s\n")
    _w(os.path.join(project, ".env.cf"), "AWS_CONFIG_FILE=" + os.path.join(project, ".aws", "config") + "\n")
    _w(os.path.join(project, ".env.kube"), "KUBECONFIG=bad.yaml\n")
    _w(os.path.join(project, ".env.gc"), "CLOUDSDK_ACTIVE_CONFIG_NAME=default\n")
    _w(os.path.join(project, ".env.gccfg"), "CLOUDSDK_CONFIG=" + os.path.join(project, ".gcloud") + "\n")
    _w(os.path.join(project, ".env.app"), "APP=x\nSTAGE=dev\n")
    _w(os.path.join(project, "names.txt"), "AWS_PROFILE\n")
    for name, bad in BAD.items():
        value = bad.replace("{W}", w)
        _w(os.path.join(project, ".env." + name), f"{name}={value}\n")
        _w(os.path.join(project, "f." + name), value + "\n")
    # home (起動時)
    _w(os.path.join(home, ".aws", "config"), "default=222222222222\nprod=123456789012\ndev=222222222222\n")
    _w(os.path.join(home, "bad", "aws", "config"), "default=555555555555\nprod=555555555555\n")
    _gcloud(os.path.join(home, ".config", "gcloud"), "default",
            {"default": "other@example.com", "prod": "a@example.com"})
    _w(os.path.join(home, ".kube", "config"), "other")
    _w(os.path.join(home, ".config", "gh", "hosts"), "other")
    _w(os.path.join(home, ".config", "configstore", "firebase-tools.json"), "otherproj")
    # home2 (良い HOME)
    _w(os.path.join(home2, ".aws", "config"), "default=123456789012\nprod=123456789012\n")
    _gcloud(os.path.join(home2, ".config", "gcloud"), "default",
            {"default": "a@example.com", "prod": "a@example.com"})
    _w(os.path.join(home2, ".kube", "config"), "ctx")
    _w(os.path.join(home2, ".config", "gh", "hosts"), "me")
    _w(os.path.join(home2, ".config", "configstore", "firebase-tools.json"), "proj")
    # good (絶対パスの良い設定)
    _w(os.path.join(good, "aws", "config"), "default=123456789012\nprod=123456789012\ndev=222222222222\n")
    _gcloud(os.path.join(good, "gcloud"), "default",
            {"default": "a@example.com", "prod": "a@example.com", "dev": "other@example.com"})
    _w(os.path.join(good, "k.yaml"), "ctx")
    _w(os.path.join(good, "gh", "hosts"), "me")
    _w(os.path.join(good, "xdg", "configstore", "firebase-tools.json"), "proj")
    _w(os.path.join(good, "xdg", "gh", "hosts"), "me")
    # hookcwd (相対パスの罠)
    _w(os.path.join(hookcwd, ".aws", "config"), "default=123456789012\nprod=123456789012\n")
    _gcloud(os.path.join(hookcwd, ".gcloud"), "default", {"default": "a@example.com", "prod": "a@example.com"})
    for f in ("k.yaml", "bad.yaml", "launch.yaml"):
        _w(os.path.join(hookcwd, f), "ctx")
    return project


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(4096)
    except (OSError, ValueError):
        return None


def _abs(path, run_dir, home):
    if path == "~" or path.startswith("~/"):
        path = (home or "") + path[1:]
    return path if os.path.isabs(path) else os.path.join(run_dir, path)


# --- 偽の verify の規則 (一致なら True) -------------------------------------------------------

def aws_ok(env, run_dir, ctx):
    profile_opt = ctx.get("profile")
    if env.get("AWS_ACCESS_KEY_ID") and not profile_opt:
        return KEYS.get(env["AWS_ACCESS_KEY_ID"], "555555555555") == ACCOUNTS["aws"]
    profile = profile_opt or env.get("AWS_PROFILE") or env.get("AWS_DEFAULT_PROFILE") or "default"
    cfg = env.get("AWS_CONFIG_FILE") or os.path.join(env.get("HOME") or "", ".aws", "config")
    text = _read(os.path.realpath(_abs(cfg, run_dir, env.get("HOME")))) or ""
    table = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return table.get(profile) == ACCOUNTS["aws"]


def gcloud_ok(env, run_dir, ctx):
    d = env.get("CLOUDSDK_CONFIG") or os.path.join(env.get("HOME") or "", ".config", "gcloud")
    d = os.path.realpath(_abs(d, run_dir, env.get("HOME")))
    name = (ctx.get("configuration") or env.get("CLOUDSDK_ACTIVE_CONFIG_NAME")
            or (_read(os.path.join(d, "active_config")) or "default").strip())
    acct = ctx.get("account") or env.get("CLOUDSDK_CORE_ACCOUNT")
    if not acct:
        text = _read(os.path.join(d, "configurations", "config_" + name)) or ""
        acct = text.strip().partition("=")[2] or None
    project = ctx.get("project") or env.get("CLOUDSDK_CORE_PROJECT")
    return acct == ACCOUNTS["gcloud"] and project in (None, "proj")


def kube_ok(env, run_dir, ctx):
    if ctx.get("context") is not None:
        return ctx["context"] == ACCOUNTS["kubectl"]
    if ctx.get("kubeconfig"):
        path = ctx["kubeconfig"]
    elif env.get("KUBECONFIG"):
        path = ([p for p in env["KUBECONFIG"].split(":") if p][:1] or [""])[0]
    else:
        path = os.path.join(env.get("HOME") or "", ".kube", "config")
    path = os.path.realpath(_abs(path, run_dir, env.get("HOME")))
    return (_read(path) or "").strip() == ACCOUNTS["kubectl"]


def gh_ok(env, run_dir, ctx):
    if env.get("GH_TOKEN"):
        return TOKENS.get(env["GH_TOKEN"], "other") == ACCOUNTS["github"]
    d = env.get("GH_CONFIG_DIR")
    if not d:
        base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or "", ".config")
        d = os.path.join(base, "gh")
    d = os.path.realpath(_abs(d, run_dir, env.get("HOME")))
    return (_read(os.path.join(d, "hosts")) or "").strip() == ACCOUNTS["github"]


def firebase_ok(env, run_dir, ctx):
    if ctx.get("project") is not None:
        return ctx["project"] in (ACCOUNTS["firebase"], "default")
    if ctx.get("config"):
        cfg = _abs(ctx["config"], run_dir, env.get("HOME"))
        rc = _read(os.path.join(os.path.dirname(cfg), ".firebaserc")) or ""
        if '"proj"' not in rc:
            return False
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or "", ".config")
    base = os.path.realpath(_abs(base, run_dir, env.get("HOME")))
    proj = (_read(os.path.join(base, "configstore", "firebase-tools.json")) or "").strip() or None
    return proj == ACCOUNTS["firebase"]


OK = {"aws": aws_ok, "gcloud": gcloud_ok, "kubectl": kube_ok, "gh": gh_ok, "firebase": firebase_ok}
