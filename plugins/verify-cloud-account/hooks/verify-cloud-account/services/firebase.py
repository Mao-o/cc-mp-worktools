"""Firebase アカウント (プロジェクト) 検証。

accounts.local.json の "firebase" は 2 形式を受け付ける:
- 文字列: `"firebase": "my-project"` — 単一プロジェクト
- オブジェクト: `"firebase": {"default": "proj-dev", "prod": "proj-prod"}`
  alias 名 → project ID のマップ。現在のアクティブがいずれかの値に一致すれば OK
  (`.firebaserc` の projects マップ形式と対応。複数環境運用向け)

現在値の解決順は firebase-tools 本体 (lib/command.js applyRC) に合わせる:

1. `firebase use` (非 TTY) が 1 行で出力する**解決済み project ID**。
   `firebase use <alias|project>` の切替先は configstore (activeProjects) にしか
   保存されず、`.firebaserc` は `--add` / `--alias` 時しか更新されない。
   つまり切替後の現在値を知っているのは CLI と configstore だけ。
   CLI の cwd は `firebase.json` を親方向に探した project root (無ければ
   project_dir) に固定し、プロセスの cwd を継承しない (2. の起点と同じ)。
2. CLI から取れないとき (PATH に無い / 実行不可 / 非ゼロ終了 / 出力が空 / 複数行
   ヘルプ) は、CLI と同じローカル設定ファイルから同じ規則で解決する:
   `firebase.json` を親方向に探した project root (無ければ project_dir) を起点に、
   configstore の activeProjects (親方向に探索) を `.firebaserc` の alias で解決
   → 無ければ `.firebaserc` の alias が 1 つならその値 → `default`。
   configstore を読むのは、`npx firebase ...` 等で hook の PATH に `firebase` が
   無い環境でも `firebase use` の切替を見落とさないため (`.firebaserc` だけを
   読むと default のまま照合して false-allow になる)。
3. CLI が timeout したときは fallback せず専用メッセージで deny する
   (fail-closed。他 service の timeout と同じ扱い)。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

PATTERNS = [r"^firebase\b"]
READONLY = [
    r"^firebase\s+use\s*$",
    # 認証操作 (login / login:ci / login:add / login:use / logout) は project を
    # 変更しない。未ログインだと `firebase use` が requireAuth で失敗して現在値を
    # CLI から取れず、login 自体が deny されるデッドロックになるため素通しする。
    r"^firebase\s+(login|logout)(:\S+)?\b",
    # 情報系 (バージョン / ヘルプ表示) はアカウント検証不要。
    r"^firebase\s+(--version|--help|version|help)\b",
]
ACCOUNT_KEY = "firebase"
SETUP_HINT = (
    'Firebase 最小例: {"firebase": "my-project-id"}。'
    "firebase use で現在値を確認可。"
    '複数 alias: {"firebase": {"default":"proj-dev","prod":"proj-prod"}}'
)
TIMEOUT_REASON = (
    "Firebase: firebase use がタイムアウトしました。"
    "再試行するか、ネットワーク接続を確認してください。"
)


def _from_cli(project_dir: str, env=None) -> tuple[str, str | None]:
    """`firebase use` (非 TTY) を実行し (project_id, error) を返す。

    非 TTY の `firebase use` はアクティブ project があれば解決済み project ID を
    1 行で出力し、無ければ非ゼロ終了する (stdout は空)。project_id は
    「終了コード 0 かつ単一行・単一トークン」のときだけ採用し、それ以外
    (CLI 未検出 / 実行不可 (権限・形式不正等の OSError) / 非ゼロ終了 / 空 /
    複数行ヘルプ) は "" を返す (呼び出し側がローカル設定に fallback する)。
    timeout だけは error に専用メッセージを入れて返す (fallback しない)。

    cwd は `_project_root(project_dir)` (firebase.json のある root、無ければ
    project_dir) に固定し、ローカル設定 fallback と解決の起点を揃える。hook /
    builder プロセスの cwd を継承すると、builder を project_dir の外から起動した
    ときに無関係なディレクトリの project を報告・書込しうる。project_dir が
    存在しなければ cwd 指定で OSError になり、CLI 不可として扱う。
    """
    try:
        result = subprocess.run(
            ["firebase", "use"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            cwd=_project_root(project_dir),
        )
    except subprocess.TimeoutExpired:
        return "", TIMEOUT_REASON
    except OSError:
        # FileNotFoundError / PermissionError / "Exec format error" / cwd 不在等。
        # 例外を漏らすと hook が異常終了して無音 fail-open になる。
        return "", None
    if result.returncode != 0:
        return "", None
    out = result.stdout.strip()
    if not out:
        return "", None
    # 単一行・単一トークン以外は project ID とみなさない (ヘルプ末尾の
    # "folder." 等の誤検出を防ぐ)。
    lines = out.splitlines()
    if len(lines) != 1:
        return "", None
    tokens = lines[0].split()
    if len(tokens) != 1:
        return "", None
    return tokens[0], None


def _project_root(project_dir: str) -> str:
    """firebase-tools の detectProjectRoot と同じく、`firebase.json` を親方向に探す。

    見つかればそのディレクトリ、無ければ project_dir (CLI は cwd) を返す。
    `.firebaserc` (loadRC) と configstore 探索の起点として使う。
    """
    start = os.path.abspath(project_dir)
    cur = start
    while True:
        if os.path.isfile(os.path.join(cur, "firebase.json")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return start
        cur = parent


def _firebaserc_aliases(root: str) -> dict[str, str]:
    """`.firebaserc` の projects マップ (alias → project ID)。読めなければ空 dict。

    不正な形 (top-level が list / projects が dict でない / 値が非文字列) も
    例外にせず空 or 該当 alias 除外として扱う。
    """
    path = Path(root) / ".firebaserc"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return {}
    return {
        alias: project
        for alias, project in projects.items()
        if isinstance(alias, str) and isinstance(project, str) and project
    }


def _configstore_path(env=None) -> Path | None:
    """firebase-tools の configstore (`$XDG_CONFIG_HOME` または `~/.config` 配下)。

    env (行頭インライン env をマージした CLI 用 env) が渡されたときはその env だけを
    見て CLI と同じ解釈にする。XDG_CONFIG_HOME も HOME も無ければ None。
    """
    if env is None:
        base = os.environ.get("XDG_CONFIG_HOME")
        if not base:
            home = os.environ.get("HOME") or str(Path.home())
            base = os.path.join(home, ".config")
    else:
        base = env.get("XDG_CONFIG_HOME")
        if not base:
            home = env.get("HOME")
            if not home:
                return None
            base = os.path.join(home, ".config")
    return Path(base) / "configstore" / "firebase-tools.json"


def _from_configstore(root: str, env=None) -> str:
    """configstore の activeProjects から `firebase use` の切替先 (alias または project ID) を返す。

    firebase-tools の configstoreProject と同じく root から親方向に探索する
    (論理パスと実体パスの両方を試す)。このファイルには認証トークンも含まれるため、
    JSON として読んだ後 activeProjects 以外は使わず、内容をメッセージに出さない。
    """
    try:
        path = _configstore_path(env)
        if path is None:
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, RuntimeError):
        return ""
    active = data.get("activeProjects") if isinstance(data, dict) else None
    if not isinstance(active, dict):
        return ""
    starts = [os.path.abspath(root)]
    real = os.path.realpath(root)
    if real not in starts:
        starts.append(real)
    for start in starts:
        cur = start
        while True:
            value = active.get(cur)
            if isinstance(value, str) and value:
                return value
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return ""


def _from_local(project_dir: str, env=None) -> str:
    """CLI が答えられないとき、firebase-tools と同じローカル設定から現在値を解決する。

    applyRC と同じ順: configstore の切替先を `.firebaserc` の alias で解決
    (alias に無ければ project ID そのもの) → alias が 1 つならその値 → `default`。
    起点は `firebase.json` のある project root (無ければ project_dir)。
    """
    root = _project_root(project_dir)
    aliases = _firebaserc_aliases(root)
    switched = _from_configstore(root, env)
    if switched:
        return aliases.get(switched, switched)
    if len(aliases) == 1:
        return next(iter(aliases.values()))
    return aliases.get("default", "")


def _resolve(project_dir: str, env=None) -> tuple[str, str | None]:
    """現在の Firebase project ID を (current, error) で返す。

    解決順は `firebase use` → ローカル設定 (モジュール docstring 参照)。
    error は CLI timeout のときだけ非 None で、その場合 current は "" (fallback しない)。
    """
    current, err = _from_cli(project_dir, env)
    if err:
        return "", err
    if current:
        return current, None
    return _from_local(project_dir, env), None


def get_active_account(project_dir: str) -> str | None:
    """現在アクティブな Firebase project ID を返す。取得不可 (timeout 含む) なら None。"""
    current, _err = _resolve(project_dir)
    return current or None


def suggest_accounts_entry(project_dir: str) -> str | None:
    """accounts.local.json の "firebase" キーに書く値を提案する (現状は scalar のみ)。"""
    return get_active_account(project_dir)


def verify(expected, project_dir: str, env=None) -> str | None:
    current, err = _resolve(project_dir, env)
    if err:
        return err
    if not current:
        if shutil.which("firebase") is None:
            return (
                "Firebase: firebase コマンドが見つかりません。"
                "npm install -g firebase-tools でインストールしてください。"
            )
        hint = expected if isinstance(expected, str) else "YOUR_PROJECT"
        return (
            f"Firebase: 現在のプロジェクトを取得できません。"
            f"firebase login && firebase use {hint} を実行してください。"
        )

    if isinstance(expected, dict):
        valid = [v for v in expected.values() if isinstance(v, str) and v]
        if not valid:
            return (
                'Firebase: accounts.local.json の "firebase" オブジェクトに'
                " 有効な (文字列値の) project ID が見つかりません。"
            )
        if current in valid:
            return None
        expected_display = ", ".join(sorted(set(valid)))
        alias_list = [
            f"  firebase use {k}  # → {v}"
            for k, v in expected.items()
            if isinstance(v, str) and v
        ]
        alias_hint = "\n".join(alias_list)
        return (
            f"Firebase プロジェクト不一致: 現在={current}, "
            f"期待={expected_display} のいずれか\n"
            f"切り替え:\n{alias_hint}"
        )

    if not isinstance(expected, str):
        return (
            f'Firebase: accounts.local.json の "firebase" は文字列または '
            f'オブジェクトで指定してください (現在: {type(expected).__name__})。'
        )

    if current != expected:
        return (
            f"Firebase プロジェクト不一致: 現在={current}, 期待={expected}"
            f" — 切り替え: firebase use {expected}"
        )

    return None


_USE_RE = re.compile(r"^firebase\s+use\s+(\S+)\s*$")


def is_self_remediation(candidate: str, expected) -> bool:
    """deny reason が案内する「期待プロジェクト / alias への firebase use」なら True。

    dict 期待値は alias 名 (キー) と project ID (値) の両方を受け付ける
    (deny メッセージが `firebase use <alias>` を案内するため)。
    """
    m = _USE_RE.match(candidate)
    if not m:
        return False
    target = m.group(1)
    if isinstance(expected, str):
        return target == expected
    if isinstance(expected, dict):
        for alias, project in expected.items():
            if not (isinstance(project, str) and project):
                continue
            if target in (alias, project):
                return True
    return False
