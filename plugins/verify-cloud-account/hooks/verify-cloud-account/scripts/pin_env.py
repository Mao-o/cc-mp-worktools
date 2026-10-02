"""プロジェクトごとのアカウント固定の提案 (builder の `pin-env`。読み取り専用)。

**なぜ必要か**: プロジェクトごとに別のアカウントを使う運用では、CLI の公式の仕組みで
「そのリポジトリでは最初から正しいアカウントで動く」ように固定できる。VCA は
切り替えず (DEVELOPMENT.md の D31)、期待値 (accounts.local.json) から固定に使う値を
出すだけにする:

- aws: `AWS_PROFILE` — 期待 Account ID に対応する profile (`aws.profiles_for_account`)
- gcloud: `CLOUDSDK_ACTIVE_CONFIG_NAME` — 期待値に一致する名前付き構成。無ければ
  `CLOUDSDK_CORE_PROJECT` (+ 期待値に account があれば `CLOUDSDK_CORE_ACCOUNT`)。
  構成名を先に勧めるのは、VCA がその構成の設定ファイルを直接読めて速いため
  (`CLOUDSDK_CORE_*` があると検証のたびに `gcloud config get-value` を起動する)
- firebase: project を選ぶ環境変数が無い (公式 docs に記載なし)。`firebase use
  <alias>` が作業ディレクトリごとに記録されるので、それを案内する

置き場所は Claude Code が読む `.claude/settings.local.json` の `env`。git リポジトリの
ルートから読まれ、worktree からは main checkout のルートのファイルが使われる
(Claude Code の docs。`claude -p` で実測)。

**書かない**: builder は accounts.local.json 専用の writer (builder の D2) なので、
このモジュールも settings.local.json を書かない。書き込みは skill の手順で、
ユーザーの承認を得てから Claude が行う。値は builder の D3 に従い既定で隠す
(profile 名・構成名・alias 名は期待値ではないので出す)。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from services import aws, firebase, gcloud

# 固定を提案する service (accounts.local.json のキー名)。
PIN_SERVICES = ("aws", "gcloud", "firebase")
SETTINGS_LOCAL_REL = Path(".claude") / "settings.local.json"
HIDDEN = "(value hidden. use --show-values to reveal)"
_GIT_TIMEOUT_SEC = 5

# `firebase use` に渡す alias / project ID の許容形。出したコマンドは skill の手順で
# Claude がそのまま実行するので、値に `;` / `$()` / 空白 / 改行などのシェルの構文や、
# option と解釈される先頭の `-` があると、期待値 (accounts.local.json) やリポジトリの
# `.firebaserc` に書かれた文字列がそのままコマンドとして走る。許容形から外れた値は
# コマンドに出さない (クォートは念のための二重化)。hook が切替の案内として認める形
# (services/firebase.py の REMEDIATION_PATTERNS) にも収まる。
# `$` が末尾の改行の前でも一致する罠を避けるため、照合は fullmatch で行う。
_FIREBASE_TARGET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_FIREBASE_TARGET_RULE = "使えるのは英数字で始まり、英数字と . _ - だけからなる名前"

AWS_PROFILE = "AWS_PROFILE"
GCLOUD_CONFIG_NAME = "CLOUDSDK_ACTIVE_CONFIG_NAME"
GCLOUD_PROJECT = "CLOUDSDK_CORE_PROJECT"
GCLOUD_ACCOUNT = "CLOUDSDK_CORE_ACCOUNT"


@dataclass(frozen=True)
class Pin:
    """settings の `env` に足す 1 変数の提案。

    - value: 足す値。候補が複数で決められないときは None (candidates から選ぶ)
    - candidates: 選べる値 (profile 名・構成名)。1 つなら value と同じ
    - secret: 値が期待値そのもの (D3 で既定は隠す)
    """

    name: str
    value: str | None
    candidates: tuple[str, ...] = ()
    secret: bool = False


@dataclass(frozen=True)
class Plan:
    """1 service 分の提案。pins (env) か command (実行するコマンド) のどちらか。"""

    service: str
    pins: tuple[Pin, ...] = ()
    command: str | None = None
    command_secret: bool = False
    notes: tuple[str, ...] = ()
    problem: str | None = None


def plan_aws(expected, env=None) -> Plan:
    if expected is None:
        return Plan("aws", problem="aws の期待値が未設定です (accounts-init で設定)")
    if not isinstance(expected, str) or not expected.strip():
        return Plan("aws", problem="aws の期待値は Account ID の文字列で書いてください")
    profiles = tuple(aws.profiles_for_account(expected.strip(), env))
    if not profiles:
        return Plan(
            "aws",
            problem=(
                "期待 Account ID の sso_account_id / role_arn を持つ profile が AWS config に"
                "見つかりません (profile 名は aws configure list-profiles で確認)"
            ),
        )
    value = profiles[0] if len(profiles) == 1 else None
    notes = ()
    if value is None:
        notes = (
            "同じアカウントに profile が複数あります。role (権限) が違いうるので、"
            "どれにするかをユーザーに確かめてください",
        )
    notes += (
        "固定してもログインはしません。SSO のトークンが切れていれば aws sso login が要ります",
    )
    return Plan("aws", pins=(Pin(AWS_PROFILE, value, profiles),), notes=notes)


def plan_gcloud(expected, env=None) -> Plan:
    if expected is None:
        return Plan("gcloud", problem="gcloud の期待値が未設定です (accounts-init で設定)")
    names = gcloud.configurations_matching(expected, env)
    if names is None:
        return Plan(
            "gcloud",
            problem="gcloud の期待値の形が不正です (project の文字列か project / account の dict)",
        )
    if names:
        value = names[0] if len(names) == 1 else None
        notes = ()
        if value is None:
            notes = ("期待値に一致する構成が複数あります。どれにするかをユーザーに確かめてください",)
        return Plan("gcloud", pins=(Pin(GCLOUD_CONFIG_NAME, value, tuple(names)),), notes=notes)
    if isinstance(expected, str):
        project, account = expected.strip(), None
    else:
        project = expected.get("project") if isinstance(expected.get("project"), str) else None
        account = expected.get("account") if isinstance(expected.get("account"), str) else None
    pins = []
    if project and project.strip():
        pins.append(Pin(GCLOUD_PROJECT, project.strip(), secret=True))
    if account and account.strip():
        pins.append(Pin(GCLOUD_ACCOUNT, account.strip(), secret=True))
    notes = (
        "期待値に一致する名前付き構成がありません。構成を作れば CLOUDSDK_ACTIVE_CONFIG_NAME で"
        "固定できます (VCA が設定ファイルを直接読めるので検証が速い)",
        "CLOUDSDK_CORE_* で固定すると、VCA は検証のたびに gcloud config get-value を"
        "起動します (1 回 1 秒前後)",
    )
    if account:
        notes += ("account はログイン済み (gcloud auth list に出ている) である必要があります",)
    return Plan("gcloud", pins=tuple(pins), notes=notes)


def _firebase_use(target: str) -> str | None:
    """`firebase use <target>` のコマンド文字列。target が許容形でなければ None。"""
    if not _FIREBASE_TARGET_RE.fullmatch(target):
        return None
    return f"firebase use {shlex.quote(target)}"


def plan_firebase(expected, project_dir: str) -> Plan:
    if expected is None:
        return Plan("firebase", problem="firebase の期待値が未設定です (accounts-init で設定)")
    notes = (
        "firebase には project を選ぶ環境変数がありません。firebase use は作業ディレクトリごとに"
        "記録されるので、このディレクトリで 1 回実行すれば次からも有効です",
        "別の worktree は別のディレクトリなので、そこでも最初に 1 回要ることがあります",
        "アカウントも firebase login:use <email> でこのディレクトリ用に選べます"
        " (VCA は firebase のアカウントは検証しません)",
    )
    if isinstance(expected, dict):
        aliases = sorted(
            alias for alias, project in expected.items()
            if isinstance(alias, str) and isinstance(project, str) and project.strip()
        )
        if not aliases:
            return Plan("firebase", problem="firebase の期待値に有効な project がありません")
        # コマンドに渡せない alias は候補から外す (どの alias でも通る、の一覧にも出さない)。
        usable = [alias for alias in aliases if _firebase_use(alias) is not None]
        if not usable:
            return Plan(
                "firebase",
                problem=(
                    "firebase の期待値の alias に、firebase use に渡せる名前がありません"
                    f" ({_FIREBASE_TARGET_RULE})"
                ),
            )
        if len(usable) < len(aliases):
            notes += (
                f"firebase use に渡せない文字を含む alias は除きました ({_FIREBASE_TARGET_RULE})",
            )
        if len(usable) > 1:
            notes = (f"期待値のどの alias でも通ります: {', '.join(usable)}",) + notes
        return Plan("firebase", command=_firebase_use(usable[0]), notes=notes)
    if not isinstance(expected, str) or not expected.strip():
        return Plan("firebase", problem="firebase の期待値の形が不正です")
    project = expected.strip()
    # `.firebaserc` はリポジトリのファイルなので、alias 名は信頼できる入力ではない。
    # 渡せない alias は使わず、無ければ alias が無いときと同じく project ID を使う。
    aliases = firebase.aliases_for(project_dir, project)
    usable = [alias for alias in aliases if _firebase_use(alias) is not None]
    if len(usable) < len(aliases):
        notes += (
            ".firebaserc の alias のうち、firebase use に渡せない文字を含むものは使いません"
            f" ({_FIREBASE_TARGET_RULE})",
        )
    if usable:
        return Plan("firebase", command=_firebase_use(usable[0]), notes=notes)
    command = _firebase_use(project)
    if command is None:
        return Plan(
            "firebase",
            problem=(
                "firebase の期待値 (project ID) に firebase use に渡せない文字が含まれます"
                f" ({_FIREBASE_TARGET_RULE})"
            ),
        )
    return Plan("firebase", command=command, command_secret=True, notes=notes)


def plan_for(service: str, expected, project_dir: str, env=None) -> Plan:
    if service == "aws":
        return plan_aws(expected, env)
    if service == "gcloud":
        return plan_gcloud(expected, env)
    if service == "firebase":
        return plan_firebase(expected, project_dir)
    raise ValueError(f"unsupported service: {service}")


def settings_local_target(project_dir: str) -> tuple[Path | None, str]:
    """Claude Code がこのプロジェクトで読む `.claude/settings.local.json` と、その説明。

    決められる (Claude Code の docs で書き込み先が一意に決まる) のは、普通の git
    リポジトリ (common dir が `<root>/.git`) で、ルートがホームではなく、自分の所有の
    ときだけ。それ以外 (git の外 / bare / submodule / ルートがホーム / 所有者が違う /
    Windows) は Claude Code が起動したディレクトリのファイルを読むなど条件が分かれる
    ので、None と理由を返す (skill はユーザーに確かめる)。
    """
    if os.name == "nt":
        return None, "Windows では Claude Code が起動したディレクトリのファイルを読むため、書き込み先を決められません"
    try:
        result = subprocess.run(
            [
                "git", "-C", project_dir, "rev-parse", "--path-format=absolute",
                "--git-dir", "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None, "git を実行できないため、書き込み先を決められません"
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) != 2:
        return None, (
            "git リポジトリの外です。Claude Code はセッションを起動したディレクトリの"
            " .claude/settings.local.json を読みます"
        )
    git_dir, common = Path(lines[0]), Path(lines[1])
    if common.name != ".git":
        return None, "bare リポジトリか submodule のため、書き込み先を決められません"
    root = common.parent
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        home = None
    if home is not None and _same_path(root, home):
        return None, (
            "リポジトリのルートがホームディレクトリなので、Claude Code はセッションを"
            "起動したディレクトリのファイルを読みます"
        )
    if hasattr(os, "getuid"):
        for path in (root, common, root / ".claude"):
            try:
                if path.exists() and path.stat().st_uid != os.getuid():
                    return None, f"{path} の所有者が自分ではないため、書き込み先を決められません"
            except OSError:
                return None, f"{path} の状態を読めないため、書き込み先を決められません"
    note = "リポジトリのルート。commit されない個人設定"
    if not _same_path(git_dir, common):
        note += "。worktree から実行しています — main checkout のこのファイルが全 worktree で使われます"
    else:
        note += "。このリポジトリの全 worktree で使われます"
    return root / SETTINGS_LOCAL_REL, note


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except (OSError, RuntimeError):
        return a == b


def settings_env(path: Path | None) -> tuple[dict[str, str], str | None]:
    """書き込み先の settings.local.json の `env` (無ければ空)。読めなければ理由を返す。"""
    if path is None or not path.is_file():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}, "JSON として読めません (手で直してから書き足してください)"
    env = data.get("env") if isinstance(data, dict) else None
    if env is None:
        return {}, None
    if not isinstance(env, dict):
        return {}, '"env" がオブジェクトではありません (手で直してから書き足してください)'
    return {k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str)}, None


def _env_member(name: str, value: str) -> str:
    """settings の `env` に足す 1 組 (`"NAME": "value"`) を JSON として組み立てる。

    Claude はこの断片を settings.local.json に写す。値の `"` / 改行 / `\\` をそのまま
    埋め込むと、別のキーを足した形 (`BASH_ENV` のように Bash や hook の動きを変える env)
    や JSON として読めない形になる。`ensure_ascii=False` なので、普通の値 (日本語の
    案内を含む) の出力は変わらない。
    """
    return f"{json.dumps(name, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}"


def render(
    plans: list[Plan],
    target: Path | None,
    target_note: str,
    session_env,
    file_env: dict[str, str],
    file_problem: str | None,
    *,
    show_values: bool,
) -> list[str]:
    """提案を人 (と Claude) が読む行にする。値は既定で隠す (D3)。"""

    def shown(value: str, secret: bool) -> str:
        return value if (show_values or not secret) else HIDDEN

    lines = []
    if target is not None:
        lines.append(f"書き込み先: {target} ({target_note})")
    else:
        lines.append(f"書き込み先: 決められません — {target_note}。どのファイルに書くかユーザーに確かめてください")
    if file_problem:
        lines.append(f"  注意: 書き込み先の {file_problem}")
    lines.append("  builder はこのファイルを書きません (書くのは skill の手順で、ユーザーの承認を得てから)")
    snippet = []
    for plan in plans:
        lines.append("")
        lines.append(f"[{plan.service}]")
        if plan.problem:
            lines.append(f"  固定できません: {plan.problem}")
            continue
        for pin in plan.pins:
            if pin.value is None:
                lines.append(f"  {pin.name}: 候補 {', '.join(pin.candidates)} (1 つ選ぶ)")
                snippet.append(
                    _env_member(pin.name, f"<{' / '.join(pin.candidates)} のどれか>")
                )
            else:
                value = shown(pin.value, pin.secret)
                lines.append(f"  {pin.name}: {value}")
                snippet.append(_env_member(pin.name, value))
            now = session_env.get(pin.name)
            in_file = file_env.get(pin.name)
            lines.append(
                "    現在: このセッション="
                + (shown(now, pin.secret) if now is not None else "未設定")
                + " / 書き込み先="
                + (shown(in_file, pin.secret) if in_file is not None else "未設定")
            )
        if plan.command:
            command = plan.command if (show_values or not plan.command_secret) else f"firebase use {HIDDEN}"
            lines.append(f"  このディレクトリで 1 回実行: {command}")
        for note in plan.notes:
            lines.append(f"  - {note}")
    if snippet:
        lines.append("")
        lines.append('settings.local.json の "env" に足す内容 (既存のキーは残す):')
        for i, item in enumerate(snippet):
            lines.append(f"  {item}{',' if i < len(snippet) - 1 else ''}")
    return lines
