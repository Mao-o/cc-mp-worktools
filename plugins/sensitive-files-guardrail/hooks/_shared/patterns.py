"""patterns.txt / patterns.local.txt のロード — 両 hook 共通実装。

ログ戦略は呼出側で注入する (read 側は logfile 付き, Stop 側は stderr のみ)。
FileNotFoundError は黙って ``patterns.local.txt`` 不在として扱う。その他 OSError
は ``warn_callback`` に委譲する。

_resolve_local_patterns_path:
- ``~/.claude/sensitive-files-guardrail/patterns.local.txt`` (0.4.0+ 単一パス)
- 0.5.x まで存在した ``$XDG_CONFIG_HOME/.../patterns.local.txt`` /
  ``~/.config/.../patterns.local.txt`` fallback は **0.6.0 で削除**。
  旧パスを使っていた場合は手動で ``mv`` する (README.md / docs/PATTERNS.md 参照)。

旧 plugin 名 fallback (rename 由来 — 0.6.0 で削除した XDG fallback とは別物):
- plugin は ``sensitive-files-guard`` → ``sensitive-files-guardrail`` に rename
  された (0.14.0 直後の commit 52113a1)。これに伴い custom patterns.local.txt
  の参照先ディレクトリも ``~/.claude/sensitive-files-guard/`` →
  ``~/.claude/sensitive-files-guardrail/`` に変わった。
- 旧ディレクトリに custom rule を残したまま upgrade した既存ユーザが、rename
  だけで黙って保護挙動を変えられない (include rule 消失 = 機密が露出 /
  exclude rule 消失 = 以前 allow したものが再び block) ようにするため、新パスが
  無く旧パスが存在する場合は旧パスを fallback で読み込み、``migrate_warn_callback``
  で移行を促す。
- 両方存在する場合は **新パスを優先し旧パスは無視する** (移行済みユーザの現行
  設定を権威とする。last-match-wins セマンティクス上、旧パスを後ろに連結すると
  古い rule が勝ってしまい現行意図を上書きするため、マージはしない)。

プロジェクトスコープの拡張 (0.15.0):
- ``patterns.local.txt`` はユーザー単位の単一ファイルのままだが、
  ``[project:<絶対パス>]`` セクションでプロジェクト別の rule を同じファイル内に
  書けるようにした。セクションヘッダーが無い行 (ファイル先頭〜最初のヘッダーまで)
  は従来通り全プロジェクト共通。ファイル全体を一度に見渡せる利点を保ちつつ、
  「あるプロジェクトのセッションで承認した除外が無関係な他プロジェクトにも
  無条件適用される」問題 (0.14.1 直後に実運用で発覚) に対処する。
- プロジェクト識別は呼出元が渡す ``cwd`` から解決する (``load_patterns`` の
  新規オプション引数)。優先順は ``_resolve_project_key`` 参照。

repo 同梱 tier (0.32.0):
- ``<project root>/.claude/sensitive-files-guardrail/patterns.txt`` を user 単位
  ファイルに**加えて**読む。連結順は **既定 → repo 同梱 → user** (last-match-wins
  なので ``user > project > 既定``)。commit できるので貢献者・CI に共有され、
  「fixture のダミー鍵で全員が毎セッション block される」問題を解決する。
- ``!`` 除外だけでなく include 行も有効 (include は保護を足す方向にしか働かない)。
  詳細な判断根拠は ``docs/PATTERNS.md`` の同節。

git worktree 対応と ``~`` 展開 (0.32.0):
- ``claude --worktree`` / ``--bg`` / sub-agent の ``isolation: worktree`` は
  別 checkout でセッションを開き ``$CLAUDE_PROJECT_DIR`` も worktree 自身の
  パスになるため、main repo のパスで書いた ``[project:...]`` セクションが
  一致せず、承認済みの除外が黙って無効化されていた。``_project_section_keys``
  が **main repo root を第 2 候補**として足す (第 1 候補は従来と同じ値なので、
  worktree のパスをヘッダーに書いていた場合も引き続き一致する)。
- ヘッダーは比較前に ``os.path.expanduser`` を通す (``[project:~/work/repo]``
  が無音で捨てられていた)。``resolve_project_root`` (path 形 rule の基準) は
  第 1 候補のみを返す — 同関数の docstring 参照。
- 出力順は **ファイル中の出現順をそのまま保持** する (グループ単位で並べ替えない)。
  last-match-wins は出現順で決まるため、``[project:...]`` セクションを共通行より
  後ろに置けばプロジェクト側が勝ち、前に置けば共通側が勝つ — 既存の
  「書いた順が強さ」という契約をセクション導入後も変えないため。

除外案内のレシピ (0.19.0):
- 両 hook の deny / block reason が案内する「恒久除外」は ``exclude_recipe_lines``
  が組み立てる ``[project:$CLAUDE_PROJECT_DIR]`` ヘッダー + ``!<basename>`` 行。
  ヘッダー無し (全プロジェクト共通) への追記は明示的な選択にし、既定では
  プロジェクト限定に誘導する (0.18.0 まではヘッダー無し行だけを案内していた
  ため、上記「他プロジェクトにも無条件適用」側に既定で誘導していた)。
- reason に絶対パスを出さない方針のため、ヘッダーは環境変数名のまま示し、
  書き込む側 (ユーザー / LLM) が実際の絶対パスへ置き換える。ヘッダー自体は
  ``_parse_local_patterns_text`` で展開されない (文字列完全一致)。
- 0.24.0 からレシピは **path 形** (``!<root 相対パス>``、承認した 1 ファイルだけ
  を外す) を既定にし、basename 形 (``!<basename>``、同名すべて) は明示的な
  選択として併記する。path 形の基準 root は ``resolve_project_root`` (=
  ``[project:]`` セクションの key と同じ値) で、両 hook の matcher も同じ root
  で評価するため、どの hook が出したレシピも他の hook で同じ 1 ファイルに効く。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Union

class PatternsDecodeError(OSError):
    """patterns ファイルが UTF-8 として読めない (0.34.1)。

    ``OSError`` の subclass にしてあるのは、既存の「読めない patterns」の扱いに
    そのまま乗せるため: 既定 patterns.txt なら全呼出元の ``except OSError``
    (``patterns_unavailable``)、repo 同梱 / user / 旧パスの tier なら
    ``warn_callback`` + その tier を読まない、に倒れる。

    ``errors="replace"`` で読み進めない理由: 保護 rule を**黙って書き換える**ため。
    cp932 で保存された ``秘密.txt`` の rule は U+FFFD の並びになって一致しなく
    なり、警告も出ないまま Read / Edit が通る。読めないことを可視化する方が安全
    (外部レビューの指摘)。
    """


def _read_patterns_text(path: Path) -> str:
    """patterns ファイルを **strict な UTF-8** で読む (locale 非依存、0.34.1)。

    ``Path.read_text()`` の既定は locale の encoding (Windows で ``PYTHONUTF8``
    未設定なら cp1252 等) なので、同梱 patterns.txt の日本語コメントで
    ``UnicodeDecodeError`` になり、``OSError`` ではないため呼出元の except を
    すり抜けて全 tool 呼出が内部エラーに落ちていた。decode 失敗は
    ``PatternsDecodeError`` (``OSError``) に変換する。先頭の BOM は許容する
    (Windows のメモ帳が付けうる。付いたままだと 1 行目の rule が一致しなくなる)。
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise PatternsDecodeError(f"not valid UTF-8: {e.reason}") from e


_PREFERRED_SUBPATH = Path(".claude") / "sensitive-files-guardrail" / "patterns.local.txt"
# rename 前 (sensitive-files-guard) の旧配置。新パスが無いときのみ fallback で読む。
_LEGACY_SUBPATH = Path(".claude") / "sensitive-files-guard" / "patterns.local.txt"
# repo 同梱 tier (0.32.0)。project root 基準の相対パスで、**commit されて
# 貢献者・CI に共有される**ことが目的。``.local.`` を名前に入れないのは、
# この生態系で ``.local.`` が「commit しない」を意味する慣習だから
# (``settings.json`` / ``settings.local.json``)。既定 tier と同じ
# ``patterns.txt`` という名前にしてあるのは、書式も完全に同じであることを
# 名前で示すため。
_PROJECT_SUBPATH = Path(".claude") / "sensitive-files-guardrail" / "patterns.txt"

# migrate_warn_callback に渡す固定トークン (パスを含めない — ログ秘密非混入 +
# core.logging の detail 文字種ホワイトリスト `^[A-Za-z0-9_:.\-\[\]!]{0,64}$` 適合)。
LEGACY_LOCAL_PATTERNS_WARN = "legacy_patterns_local_in_use"

# repo 同梱 tier を実際に読み込んだときに ``project_patterns_callback`` へ渡す
# 固定トークン (0.32.0)。「この repo の同梱ファイルが保護を弱めている」ことを
# 追跡できるようにするための可視化で、判定は変えない。
PROJECT_PATTERNS_IN_USE = "project_patterns_in_use"

_PROJECT_SECTION_PREFIX = "[project:"
_PROJECT_SECTION_SUFFIX = "]"

# 除外案内 (read 側 deny reason / Stop 側 block reason) で見せる表示用パス
# (0.19.0)。実体の解決は ``_resolve_local_patterns_path`` (``Path.home()`` 基準)
# で、表示は ``~`` 表記のまま固定する (reason に絶対パスを出さない)。
LOCAL_PATTERNS_DISPLAY_PATH = "~/.claude/sensitive-files-guardrail/patterns.local.txt"
# repo 同梱 tier の表示用パス (0.32.0)。実体の解決は
# ``_resolve_project_patterns_path`` (project root 基準)。reason に絶対パスを
# 出さない方針のため、root は ``<project root>`` の literal で示す。
PROJECT_PATTERNS_DISPLAY_PATH = (
    "<project root>/.claude/sensitive-files-guardrail/patterns.txt"
)
# 除外案内で勧めるセクションヘッダーの雛形。実パスではなく環境変数名で示す。
# ``_parse_local_patterns_text`` はヘッダーを展開しない (文字列完全一致) ので、
# 書き込む側が ``$CLAUDE_PROJECT_DIR`` を実際の絶対パスに置き換える前提。
PROJECT_SECTION_HEADER_HINT = "[project:$CLAUDE_PROJECT_DIR]"
# ヘッダー雛形に添える注記 (両 hook で同じ文言)。Bash tool の環境では
# ``CLAUDE_PROJECT_DIR`` が未設定なので unquoted echo だと空に展開され、quoted
# heredoc / Write tool だと literal に残る。どちらも「どのプロジェクトにも一致
# しない」ため黙って捨てられる (= 除外が効かない) ので、展開に頼らず絶対パスを
# literal に書くよう明示する (L2 review)。
PROJECT_SECTION_PLACEHOLDER_NOTE = (
    "$CLAUDE_PROJECT_DIR は展開されないので、プロジェクト root の絶対パスを"
    " literal に書く (例: [project:/abs/path/to/repo])。"
    "全プロジェクト共通にしたい場合のみヘッダー無しの行に書く"
)

# 除外行を追加する前にユーザーへ伝えるべき影響範囲 (0.23.0、両 hook 共通。
# 0.24.0 で path 形 / basename 形の 2 形に書き分け)。
#
# 除外行は形で効く範囲が違う:
# - **path 形** (``!config/prod.pem``、``/`` を含む): project root 相対で評価
#   されるので、承認した **その 1 ファイルだけ** に効く (root 配下のみ)
# - **basename 形** (``!.env``): basename と ``pathlib.parts`` で評価されるので
#   プロジェクト内の **すべての** 同名ファイルに効き、同名ディレクトリの配下も
#   外れる。しかも ``[project:]`` は rule の読込先を決めるだけで、読み込まれた
#   後の matcher は operand がプロジェクト配下かを見ないため、そのセッションが
#   触る絶対パス全部 (他プロジェクト含む) に効く
# どちらの形も、効果は Stop の報告に留まらず **Read / Bash / Edit / Write の
# 保護そのもの**が落ちる。
#
# 「以後 ... で報告されなくなります」という従来の文面では、"報告されない" が
# "保護されない" と同義であることも、同名ファイル全部が巻き添えになることも
# 伝わらなかった。0.23.0 で開示を足し、0.24.0 で範囲を絞る path 形を足した。
#
# 文言は **byte 予算を食う** (reason 全体 3KB、UTF-8 で日本語 1 字 3 byte) ので
# 必要最小限に留める。0.24.0 の初版で root 直下の書き方 (`!/<名前>`) まで
# ここに書いたところ、Edit の deny reason で大きな `.env` の minimal info が
# 押し出されて鍵一覧が消えた (既存テスト 2 件が検出)。書き方の説明は docs と
# レシピ生成 (`path_rule_for` が先頭 `/` を付ける) に任せる。
EXCLUDE_SCOPE_WARNING = (
    "影響範囲: path 形 (`!<root 相対パス>`) は**その 1 ファイルだけ** (root 配下のみ)。"
    "basename 形 (`!<名前>`) は{scope}が**すべて**対象で、"
    "**同名ディレクトリの配下も外れます** (配下が別の include 行に単独一致"
    "する場合はそちらが優先)。`[project:]` は rule の読込先を決めるだけなので、"
    "basename 形は**このセッションが触る絶対パス全部** (他プロジェクト含む) に効きます。"
    "外れるのは Stop の報告だけでなく **Read / Bash / Edit / Write の保護そのもの**です。"
)

# ``[project:]`` ヘッダーが書き損じのときに ``header_warn_callback`` へ渡す固定
# トークン (パスを含めない — core.logging の detail ホワイトリスト適合)。
PROJECT_HEADER_WARN_EMPTY = "project_header_empty"
PROJECT_HEADER_WARN_PLACEHOLDER = "project_header_unexpanded_placeholder"


# fnmatch のメタ文字。生成する除外行では literal として扱わせる必要がある。
_GLOB_META = "*?[]"


def escape_glob(name: str) -> str:
    """basename を fnmatch の literal パターンに変換する (0.23.0)。

    rule は ``fnmatchcase`` で評価されるため、basename に ``*`` ``?`` ``[`` ``]``
    が含まれると**生成した除外行が別物になる**。実測 (既定 rules に対し
    ``key[1].pem`` を承認した場合):

    - ``!key[1].pem`` は ``key[1].pem`` に**マッチしない** (承認したファイルの
      保護が残り、レシピが効かない)
    - 代わりに ``key1.pem`` にマッチする (**無関係なファイルの保護が外れる**)

    どちらも影響範囲の開示と矛盾するので、メタ文字を文字クラスで包んで
    literal 化する (``[`` → ``[[]`` / ``]`` → ``[]]`` / ``*`` → ``[*]`` /
    ``?`` → ``[?]``)。メタ文字を含まない名前は**そのまま**返すので、
    通常のレシピの見た目は変わらない。
    """
    if not isinstance(name, str) or not any(c in name for c in _GLOB_META):
        return name if isinstance(name, str) else ""
    return "".join(f"[{c}]" if c in _GLOB_META else c for c in name)


def path_rule_for(relpath: str) -> str:
    """root 相対 path を **path 形** rule の文字列にする (0.24.0)。

    rule は ``/`` を含むかどうかで形が決まる (``_shared.matcher.is_path_rule``)。
    root 直下のファイルは相対 path が ``.env`` のように ``/`` を含まないため、
    そのまま ``!.env`` と書くと basename 形 (同名すべて) に化ける — 案内文が
    「この 1 ファイルだけ」と言いながら、セッションが触る ``.env`` 全部の保護を
    外す最悪の形 (Codex R1 P1)。先頭に ``/`` (root アンカー) を付けて path 形で
    あることを保つ。既に ``/`` を含む path はそのまま。メタ文字の literal 化は
    呼出側 (``escape_glob``) が行う (``/`` は escape の対象外なので順序は問わない)。
    """
    if not relpath:
        return relpath
    return relpath if "/" in relpath else "/" + relpath


def exclude_recipe_lines(names: Iterable[str], limit: int = 20) -> list[str]:
    """``patterns.local.txt`` に追記する恒久除外レシピ (行リスト) を返す。

    ``[project:$CLAUDE_PROJECT_DIR]`` ヘッダー + ``!<name>`` 行。``names`` は
    root 相対 path (path 形、0.24.0 の既定) でも basename (basename 形) でもよく、
    呼出側が解決できた方を渡す。重複は出現順を保って除去し (順序付き list と set
    を並行して持ち線形 — list membership だと二次計算で数万件の repo で Stop の
    15s timeout を超えうる、Codex R5 P2-2)、空文字列は捨てる。``limit`` 件を
    超えた分は ``... (N more)`` に畳む (Stop の block reason が肥大しないため)。
    両 hook が同じレシピを提示するためにここ (patterns.local.txt 形式の所有者)
    に置く。メタ文字は ``escape_glob`` で literal 化する (path 形でも ``/`` は
    そのまま)。
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    lines = [PROJECT_SECTION_HEADER_HINT]
    lines.extend(f"!{escape_glob(name)}" for name in ordered[:limit])
    if len(ordered) > limit:
        lines.append(f"... ({len(ordered) - limit} more)")
    return lines


def _resolve_local_patterns_path() -> Path:
    """ローカル patterns.local.txt の参照先パスを返す。

    0.6.0 から ``~/.claude/sensitive-files-guardrail/patterns.local.txt`` 単一パス。
    """
    return Path.home() / _PREFERRED_SUBPATH


def _resolve_legacy_local_patterns_path() -> Path:
    """rename 前 (sensitive-files-guard) の旧 patterns.local.txt パスを返す。

    ``~/.claude/sensitive-files-guard/patterns.local.txt``。新パスが存在しない
    場合の fallback 読み込み元 (rename だけで保護挙動を変えないため)。
    """
    return Path.home() / _LEGACY_SUBPATH


def _resolve_project_patterns_path(cwd: str) -> Optional[Path]:
    """repo 同梱 patterns.txt のパスを返す (0.32.0)。project root 不明なら None。

    基準は ``resolve_project_root`` (= ``[project:]`` セクションの第 1 候補)。
    worktree セッションでは worktree checkout 側のファイルを読む — commit 済み
    なら main repo と同じ内容が worktree にも存在するため。

    **前提が崩れたときの挙動** (マージ前レビューの指摘): ファイルが未 commit
    (untracked / ignored) だと ``git worktree add`` はそれを持ち込まないので、
    **worktree セッションでは tier が丸ごと消える** (警告も出ない)。
    ``[project:]`` セクションの一致判定 (``_project_section_keys``) が main repo
    root を第 2 候補に足すのに対し、こちらは**第 1 候補のみを探索する**のは
    意図したもの: 全候補を探すと「main repo で untracked のファイルが worktree
    でも効く」= 作者の手元だけで効く状態を延命し、貢献者・CI では依然として
    何も読めないまま、作者が気付ける唯一の signal (worktree で消える) を潰して
    しまう。共有したいなら commit する (``git check-ignore`` で確認できる。
    ``docs/PATTERNS.md`` の repo 同梱 tier の節を参照)。
    """
    root = resolve_project_root(cwd)
    if not root:
        return None
    return Path(root) / _PROJECT_SUBPATH


def _resolve_project_key(cwd: str) -> Optional[str]:
    """``[project:<key>]`` セクションと突き合わせる識別子を解決する。

    優先: ``$CLAUDE_PROJECT_DIR`` 環境変数。hook を spawn する Claude Code が
    注入する安定した project root 基準点で、Bash の ``cd`` 等でセッション中に
    ``cwd`` (hook input JSON フィールド) が変化しても影響を受けない
    (公式 hooks doc: 専用の ``CwdChanged`` event が存在するほど ``cwd`` 自体は
    可変)。CLI 2.1.196+ が必要なため、未設定なら ``cwd`` から遡って探す。

    フォールバック: ``cwd`` から ``.git`` が見つかる階層 (または ``$HOME`` /
    filesystem root) まで遡る。plugin 最低要件 CLI 2.1.100+ でも動く必要がある
    ための経路。subprocess は使わない (``git rev-parse`` 等を呼ぶと PreToolUse
    の <100ms 目標latency に響くため、stat ベースの純 pathlib 走査のみ)。

    ``$HOME`` 自体には到達しない (home 直下は既存のユーザーグローバル tier が
    別途担当するため、ここで一致させると無限に「プロジェクト」扱いになる)。
    どこにも ``.git`` が見つからなければ None (プロジェクトセクションは適用外、
    共通行のみ有効)。
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return os.path.normpath(project_dir)

    if not cwd:
        return None

    home = str(Path.home())
    current = os.path.normpath(cwd)
    while True:
        if current == home:
            return None
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def resolve_project_root(cwd: str) -> Optional[str]:
    """path 形 rule (``config/prod.pem`` 等) の基準となる project root を返す
    (0.24.0)。

    値は ``_resolve_project_key`` (``$CLAUDE_PROJECT_DIR`` 優先 → ``.git``
    上方探索) の結果そのもの。同じ関数を別名で公開するのは意図的で、
    「``[project:<key>]`` セクションの key」と「そのセクションに書いた path 形
    rule の基準 root」が定義上同じものであることを呼出側に示すため。両 hook
    (Read / Edit / Bash / Stop) が同じ root で matcher を呼ぶので、どの hook が
    出したレシピも他の hook で同じ 1 ファイルに効く。git の toplevel を Stop
    だけで使うと monorepo (``$CLAUDE_PROJECT_DIR`` がサブディレクトリ) で
    Stop のレシピが Read で効かなくなるため、あえて git には寄せない。
    None (root 不明) なら path 形 rule は評価されない。

    0.32.0 の worktree 対応で、``[project:]`` セクションの一致判定だけは
    **複数 key** (``_project_section_keys``: 本関数の値 + main repo root) を
    見るようになったため、「セクションの key」と「path 形の基準 root」は
    完全に同一ではなくなった。本関数は**引き続き第 1 候補 (worktree 自身)
    だけを返す** — worktree セッションで実際に触るファイルは worktree 配下に
    あるので、root 相対 path の基準を main repo root にすると
    ``root_relative`` が「root 配下でない」と判定して path 形 rule が一切
    効かなくなる。main repo root 側のセクションに書いた path 形 rule も、
    worktree は同じツリー構成を持つため同じ相対 path で一致する。
    """
    return _resolve_project_key(cwd)


# ``.git`` がファイルのときの参照行 prefix (worktree / submodule 共通)。
_GITDIR_POINTER_PREFIX = "gitdir:"
# worktree の gitdir は ``<main>/.git/worktrees/<name>`` になる。submodule の
# ``.git`` ファイルも同じ ``gitdir:`` 形式 (``<super>/.git/modules/<name>``) な
# ので、**この path 要素の有無**で両者を区別する。区別しないと submodule 内の
# セッションで project key が superproject に差し替わり、読み込む rule が
# 黙って変わる。
_GIT_WORKTREES_COMPONENT = "worktrees"
# gitdir / commondir ファイルから読む最大 byte 数 (壊れた・巨大なファイルを
# そのまま読み込まないための上限。正常な内容は 1 行で数百 byte 以内)。
_GIT_POINTER_READ_LIMIT = 4096


def _read_git_pointer_file(path: str) -> Optional[str]:
    """``.git`` / ``commondir`` のような 1 行テキストを先頭だけ読む。

    読めない (OSError) / 空なら None。デコード不能バイトは置換して読む
    (path 文字列の判定にしか使わないため、例外で落とすより退行しない)。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(_GIT_POINTER_READ_LIMIT)
    except OSError:
        return None
    return head or None


def _gitdir_pointer(git_file: str) -> Optional[str]:
    """``.git`` ファイルの ``gitdir: <path>`` 参照先を絶対パスで返す。

    相対参照 (``gitdir: ../.git/worktrees/x``) は ``.git`` ファイルのある
    ディレクトリ基準で解決する。行が見つからなければ None。
    """
    head = _read_git_pointer_file(git_file)
    if head is None:
        return None
    for line in head.splitlines():
        stripped = line.strip()
        if not stripped.startswith(_GITDIR_POINTER_PREFIX):
            continue
        target = stripped[len(_GITDIR_POINTER_PREFIX):].strip()
        if not target:
            return None
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(git_file), target)
        return os.path.normpath(target)
    return None


def _main_repo_root(dir_path: str) -> Optional[str]:
    """``dir_path`` が git worktree の root なら main repo の root を返す。

    worktree では ``.git`` が**ファイル**で ``gitdir:
    <main>/.git/worktrees/<name>`` を指し、その中の ``commondir`` が共有
    git dir (``<main>/.git``) への参照を持つ。これを辿って main repo root
    (= 共有 git dir の親) を返す。

    None を返す条件 (どれも「worktree ではない」= 追加候補なし):

    - ``.git`` がディレクトリ (通常の checkout)
    - ``gitdir:`` 行が無い / 空
    - gitdir に ``worktrees`` 要素が無い (submodule の
      ``<super>/.git/modules/<name>`` を worktree と誤認しないため)
    - ``commondir`` が読めない / 共有 git dir の basename が ``.git`` でない
      (bare repo の worktree には「main repo の working tree」が存在しない)。
      **submodule の worktree** (``<super>/.git/modules/<name>/worktrees/<wt>``)
      も同じ判定で None になる — commondir が
      ``<super>/.git/modules/<name>`` を指し basename が ``.git`` でないため
      (実測。``worktrees`` 要素 guard は素通りする)
    - 解決結果がディレクトリとして存在しない / ``$HOME`` 自身
      (``_resolve_project_key`` の「home はプロジェクトではない」規約に揃える)

    subprocess は使わない (``git rev-parse --git-common-dir`` を呼ぶと
    PreToolUse の latency 目標に響くため、``_resolve_project_key`` と同じく
    stat / 小さなテキスト読みだけで完結させる)。
    """
    git_file = os.path.join(dir_path, ".git")
    if not os.path.isfile(git_file):
        return None
    gitdir = _gitdir_pointer(git_file)
    if not gitdir:
        return None
    if _GIT_WORKTREES_COMPONENT not in gitdir.split(os.sep):
        return None
    common = _read_git_pointer_file(os.path.join(gitdir, "commondir"))
    if common is None:
        return None
    common = common.strip()
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(gitdir, common)
    common = os.path.normpath(common)
    if os.path.basename(common) != ".git":
        return None
    root = os.path.dirname(common)
    if not root or not os.path.isdir(root):
        return None
    if root == str(Path.home()):
        return None
    return root


def _project_section_keys(cwd: str) -> list[str]:
    """``[project:<key>]`` セクションと突き合わせる key の候補列を返す (0.32.0)。

    第 1 候補は ``_resolve_project_key`` の結果 (従来と同じ値)。git worktree
    セッションでは **main repo root を第 2 候補**として足す。

    worktree 対応が必要な理由: ``claude --worktree`` / ``--bg`` /
    sub-agent の ``isolation: worktree`` はいずれも別 checkout
    (``<repo>/.claude/worktrees/<name>`` 等) でセッションを開き、
    ``$CLAUDE_PROJECT_DIR`` もその worktree 自身のパスになる (実測)。
    main repo のパスで書いた ``[project:...]`` セクションは文字列完全一致
    しないため、0.15.0 で入れたプロジェクト固有の承認済み除外が worktree
    作業では黙って無効化されていた (= 承認済みのファイルで再び block される)。

    第 1 候補を残す (置き換えではなく追加) のは、worktree 自身のパスを
    ヘッダーに書いていた場合の既存の一致挙動を変えないため。
    """
    primary = _resolve_project_key(cwd)
    if primary is None:
        return []
    keys = [primary]
    main_root = _main_repo_root(primary)
    if main_root is not None and main_root != primary:
        keys.append(main_root)
    return keys


def _normalize_project_keys(
    project_key: Union[str, Sequence[str], None],
) -> tuple[str, ...]:
    """``project_key`` 引数 (単一 key / key 列 / None) を tuple に正規化する。

    単一 str を受け付けるのは後方互換のため (``_parse_local_patterns_text``
    を直接呼ぶ既存の呼出・テストがある)。空文字列・None は「プロジェクト
    未解決」として空 tuple にする。
    """
    if project_key is None:
        return ()
    if isinstance(project_key, str):
        return (project_key,) if project_key else ()
    return tuple(k for k in project_key if k)


def _normalize_header_key(header_key: str) -> str:
    """``[project:<key>]`` の key を比較用に正規化する。

    ``~`` / ``~user`` を ``os.path.expanduser`` で展開してから
    ``os.path.normpath`` する (0.32.0)。展開前は ``[project:~/work/repo]`` が
    どのプロジェクトにも一致せず黙って捨てられていた — ``$CLAUDE_PROJECT_DIR``
    の未展開 placeholder と違い ``$`` を含まないので
    ``_bad_header_token`` の警告にも掛からず、完全に無音だった。

    ``expanduser`` は ``~`` で始まらない文字列を素通しするので、絶対パスを
    書いた既存のヘッダーの挙動は変わらない。
    """
    return os.path.normpath(os.path.expanduser(header_key))


def _parse_patterns_text(text: str) -> list[tuple[str, bool]]:
    """patterns.txt 形式のテキストを rules list にパースする。

    - 空行・``#`` で始まる行は無視 (先頭空白 strip 後に判定)
    - ``!pattern`` → ``(pattern, True)`` (exclude)
    - それ以外 → ``(pattern, False)`` (include)
    - 出現順を保持する (last-match-wins で順序が意味を持つため)

    ``[project:...]`` セクションヘッダーは解釈しない (既定 patterns.txt には
    そもそも書かれない想定)。patterns.local.txt 側は ``_parse_local_patterns_text``
    を使う。
    """
    rules: list[tuple[str, bool]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("!"):
            rules.append((stripped[1:], True))
        else:
            rules.append((stripped, False))
    return rules


# ``[project:...]`` ヘッダーを「未展開の変数参照」と見なす構文 (Codex R2 P2-1 /
# R4 P2-2): **standalone 形のみ** — ヘッダー値全体が ``$NAME`` / ``${NAME}``
# そのもの、またはそれで始まり直後が ``/`` (``$CLAUDE_PROJECT_DIR`` /
# ``${CLAUDE_PROJECT_DIR}/sub`` / ``$HOME/x`` / ``${PWD}``)。両 hook の案内を
# そのまま写した形とシェル変数でパスを書こうとした形を拾う。
# ``/work/project$prod`` のように ``$`` を途中に含むだけの literal パスや、
# ``/work/repo$CLAUDE_PROJECT_DIR-prod`` / ``/work/${CLAUDE_PROJECT_DIR}-archive``
# のように予約語を **部分文字列** として含む literal パスは正当なヘッダーとして
# 扱う (当初の「``$`` を含めば placeholder」/ 「``$CLAUDE_PROJECT_DIR`` を含めば
# placeholder」はこれらを無効化し、その repo の project スコープの include /
# exclude が黙って落ちていた)。
_PLACEHOLDER_HEAD_RE = re.compile(
    r"^\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)(?:/|$)"
)


def _bad_header_token(header_key: str) -> Optional[str]:
    """``[project:<key>]`` の key が「書き損じ」かを判定し、警告トークンを返す。

    - 空 (``[project:]``): Bash の unquoted echo で ``$CLAUDE_PROJECT_DIR`` が空に
      展開された典型
    - 未展開の変数参照の standalone 形 (``[project:$CLAUDE_PROJECT_DIR]`` /
      ``${CLAUDE_PROJECT_DIR}`` / ``$HOME/work`` のように値全体が ``$NAME`` 形、
      またはそれで始まり直後が ``/``): 変数名を literal に書いた典型 (hook は
      ヘッダーを展開しない)
    ``$`` や予約語を途中に含むだけの literal パス (``/work/project$prod`` /
    ``/work/repo$CLAUDE_PROJECT_DIR-prod``) は正常 (None) で、通常どおり
    ``project_key`` と比較される。
    """
    if not header_key:
        return PROJECT_HEADER_WARN_EMPTY
    if _PLACEHOLDER_HEAD_RE.match(header_key):
        return PROJECT_HEADER_WARN_PLACEHOLDER
    return None


def _parse_local_patterns_text(
    text: str,
    project_key: Union[str, Sequence[str], None],
    header_warn_callback: Optional[Callable[[str], None]] = None,
    warned: Optional[set[str]] = None,
) -> list[tuple[str, bool]]:
    """patterns.local.txt を ``[project:<path>]`` セクション対応でパースする。

    ファイル先頭〜最初のセクションヘッダーまでの行は全プロジェクト共通。
    ``[project:<path>]`` 行以降は次のヘッダーか EOF までそのセクション専用になる。
    ``project_key`` に一致するセクションの行だけを共通行と合わせ、**ファイル中の
    元の出現順のまま** 返す (グループ単位で並べ替えない — 出現順が
    last-match-wins の強さを決めるため)。

    ``project_key`` は単一 key でも key 列でもよい (0.32.0。worktree では
    「worktree 自身 + main repo root」の 2 候補になる —
    ``_project_section_keys``)。**いずれか 1 つに一致**すればそのセクションは
    active。複数候補のセクションが同一ファイル内に並ぶ場合は、どちらの行も
    出現順のまま採用される (last-match-wins の契約は変わらない)。

    ``project_key`` が None / 空 (プロジェクト未解決) の場合はどのセクションにも
    一致せず、共通行のみが返る (既存ファイル・非 git ディレクトリでの挙動は
    セクション導入前と完全に同一)。

    ``header_warn_callback`` (0.19.0): ヘッダーが空 (``[project:]``) または
    未展開の変数参照 (``[project:$CLAUDE_PROJECT_DIR]`` を literal に書いた、
    先頭が ``$NAME`` / ``${NAME}`` 形。``_bad_header_token`` 参照) の場合に
    固定トークン (``PROJECT_HEADER_WARN_EMPTY`` / ``PROJECT_HEADER_WARN_PLACEHOLDER``)
    で種別ごとに 1 回呼ぶ。両 hook の除外案内が ``$CLAUDE_PROJECT_DIR`` を変数名
    で示すため、Bash の unquoted echo で空に展開される / quoted heredoc や Write
    で literal に残る、のどちらでも **黙って捨てられる** (どのプロジェクトにも
    一致しない) のを可視化する。判定自体は変えない (そのセクションは非 active)。

    ``warned`` (0.32.0、マージ前レビューの指摘): 既に警告した種別の集合。
    ``load_patterns`` が **tier 間で 1 つを共有**して渡す — repo 同梱 tier と
    user tier を別々にパースするようになったため、同じ書き損じヘッダーが両方に
    あると「種別ごとに 1 回」の契約が破れて同じ警告が 2 回出ていた。呼出側が
    渡さなければ呼出ローカルの集合を作る (単体で呼ぶ既存の呼出・テスト互換)。
    """
    rules: list[tuple[str, bool]] = []
    keys = _normalize_project_keys(project_key)
    active = True  # 現在のセクションが出力対象か (共通行は常に active)
    if warned is None:
        warned = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(_PROJECT_SECTION_PREFIX) and stripped.endswith(
            _PROJECT_SECTION_SUFFIX
        ):
            header_key = stripped[
                len(_PROJECT_SECTION_PREFIX) : -len(_PROJECT_SECTION_SUFFIX)
            ].strip()
            bad = _bad_header_token(header_key)
            if bad is not None:
                if header_warn_callback is not None and bad not in warned:
                    warned.add(bad)
                    header_warn_callback(bad)
                active = False
                continue
            active = _normalize_header_key(header_key) in keys
            continue
        if not active:
            continue
        if stripped.startswith("!"):
            rules.append((stripped[1:], True))
        else:
            rules.append((stripped, False))
    return rules


def _load_project_patterns(
    cwd: str,
    warn_callback: Optional[Callable[[str], None]],
    header_warn_callback: Optional[Callable[[str], None]],
    project_key: Union[str, Sequence[str], None],
    project_patterns_callback: Optional[Callable[[str], None]],
    warned: Optional[set[str]] = None,
) -> list[tuple[str, bool]]:
    """repo 同梱 tier (``<root>/.claude/sensitive-files-guardrail/patterns.txt``)
    を読む (0.32.0)。

    非存在は黙殺 (大半の repo には無い)。FileNotFound 以外の OSError は
    ``warn_callback`` に委譲して空を返す (user tier と同じ契約)。1 行以上
    読めたときだけ ``project_patterns_callback(PROJECT_PATTERNS_IN_USE)``。

    書式は user tier と完全に同じ (``_parse_local_patterns_text`` を共有) なので
    ``[project:...]`` セクションも書ける — repo 同梱なので通常は不要だが、
    monorepo でサブプロジェクトごとに書き分けたい場合に効く。

    ``warned`` は ``load_patterns`` が tier 間で共有する「警告済み種別」の集合
    (``_parse_local_patterns_text`` 参照)。
    """
    path = _resolve_project_patterns_path(cwd)
    if path is None:
        return []
    try:
        text = _read_patterns_text(path)
    except FileNotFoundError:
        return []
    except OSError as e:
        if warn_callback is not None:
            warn_callback(type(e).__name__)
        return []
    rules = _parse_local_patterns_text(
        text, project_key, header_warn_callback, warned
    )
    if rules and project_patterns_callback is not None:
        project_patterns_callback(PROJECT_PATTERNS_IN_USE)
    return rules


def load_patterns(
    patterns_file: Path,
    warn_callback: Optional[Callable[[str], None]] = None,
    migrate_warn_callback: Optional[Callable[[str], None]] = None,
    cwd: str = "",
    header_warn_callback: Optional[Callable[[str], None]] = None,
    project_patterns_callback: Optional[Callable[[str], None]] = None,
) -> list[tuple[str, bool]]:
    """既定 patterns.txt + repo 同梱 patterns.txt + ローカル patterns.local.txt を
    読んで rules list を返す。

    連結順 = **既定 → repo 同梱 → user ローカル** (last match wins なので後ろが
    強い = ``user > project > 既定``、0.32.0)。repo 同梱 tier を user tier より
    前に置くのは、repo が持ち込んだ除外をユーザーが自分のファイルで打ち消せる
    (include 行を書き足せば勝てる) ようにするため — 逆順だと clone してきた
    repo の除外が、そのユーザーの明示的な意思より強くなる。
    ローカル非存在は無視、読み取り中の OSError (FileNotFound 以外) は
    ``warn_callback(err_name)`` に渡して既定のみ返す。

    repo 同梱 tier (``<project root>/.claude/sensitive-files-guardrail/patterns.txt``):
    commit できるので**貢献者・CI に共有される**。テスト fixture / サンプル /
    docs 用のダミー鍵を持つ repo で、全員が毎セッション block されるのを防ぐ
    ための tier (それまでは各自がホーム配下に除外を書くしかなく、CI では
    そもそも効かなかった)。``!`` 除外に加えて **include 行も有効** — include は
    保護を足す方向にしか働かないため (詳細は ``docs/PATTERNS.md``)。

    ローカル patterns.local.txt の解決:
    - 新パス ``~/.claude/sensitive-files-guardrail/patterns.local.txt`` が存在
      すればそれを読む (旧パスは見ない)。
    - 新パスが無く旧パス ``~/.claude/sensitive-files-guard/patterns.local.txt``
      (rename 前) が存在すれば、旧パスを fallback で読み込み
      ``migrate_warn_callback(LEGACY_LOCAL_PATTERNS_WARN)`` で移行を促す
      (rename だけで保護挙動が変わらないようにするため)。
    - 両方無ければ既定のみ返す。
    - 旧パスからの読み取りでも FileNotFound 以外の OSError は ``warn_callback``
      に委譲する (新パスと同じ契約)。

    ``cwd`` (呼出元 hook の envelope 由来、任意): ``_project_section_keys`` で
    プロジェクト識別子 (worktree では main repo root を含む 2 候補) に解決し、
    ローカルファイル中の ``[project:<key>]`` セクションのうち一致するものだけを
    共通行と合わせて読み込む (``_parse_local_patterns_text`` 参照)。
    空文字列 (既定) なら共通行のみ。

    ``header_warn_callback`` (0.19.0、任意): ``[project:]`` ヘッダーが空 / 未展開
    placeholder のときに固定トークンで呼ぶ (``_parse_local_patterns_text`` 参照)。

    ``project_patterns_callback`` (0.32.0、任意): repo 同梱 tier から 1 行以上
    読み込んだときに ``PROJECT_PATTERNS_IN_USE`` で呼ぶ。repo 同梱ファイルは
    保護を弱めうる (clone してきた repo の ``!`` 行がそのまま効く) ので、
    「なぜ block されないのか」を追跡できるようにするための可視化。

    Raises:
        FileNotFoundError: 既定 patterns.txt が存在しない
        OSError: 既定 patterns.txt の読み取りに失敗した (UTF-8 として読めない
            ``PatternsDecodeError`` を含む)
    """
    rules = _parse_patterns_text(_read_patterns_text(patterns_file))
    project_key = _project_section_keys(cwd)
    # 「書き損じヘッダーの警告は種別ごとに 1 回」を **tier をまたいで** 保つ
    # (0.32.0、マージ前レビューの指摘)。tier ごとに別の集合を持つと、同じ
    # 書き損じが repo 同梱と user の両方にあるときに同じ警告が 2 回出る。
    warned: set[str] = set()

    rules.extend(
        _load_project_patterns(
            cwd, warn_callback, header_warn_callback, project_key,
            project_patterns_callback, warned,
        )
    )

    local_path = _resolve_local_patterns_path()
    try:
        local_text = _read_patterns_text(local_path)
    except FileNotFoundError:
        # 新パスが無い → rename 前の旧パスを fallback で試す。
        return _load_legacy_local(
            rules, warn_callback, migrate_warn_callback, project_key,
            header_warn_callback, warned,
        )
    except OSError as e:
        if warn_callback is not None:
            warn_callback(type(e).__name__)
        return rules

    rules.extend(
        _parse_local_patterns_text(
            local_text, project_key, header_warn_callback, warned
        )
    )
    return rules


def _load_legacy_local(
    rules: list[tuple[str, bool]],
    warn_callback: Optional[Callable[[str], None]],
    migrate_warn_callback: Optional[Callable[[str], None]],
    project_key: Union[str, Sequence[str], None] = None,
    header_warn_callback: Optional[Callable[[str], None]] = None,
    warned: Optional[set[str]] = None,
) -> list[tuple[str, bool]]:
    """新パス不在時に rename 前の旧 patterns.local.txt を fallback 読み込みする。

    旧パスが存在すれば rules に連結し ``migrate_warn_callback`` で移行を促す。
    旧パス非存在は黙殺、FileNotFound 以外の OSError は ``warn_callback`` に委譲。
    いずれも既定 rules (+ 旧ローカル) を返す。``[project:...]`` セクション対応は
    新パスと同じ (``_parse_local_patterns_text``)。``warned`` は repo 同梱 tier と
    共有する「警告済み種別」の集合。
    """
    legacy_path = _resolve_legacy_local_patterns_path()
    try:
        legacy_text = _read_patterns_text(legacy_path)
    except FileNotFoundError:
        return rules
    except OSError as e:
        if warn_callback is not None:
            warn_callback(type(e).__name__)
        return rules

    rules.extend(
        _parse_local_patterns_text(
            legacy_text, project_key, header_warn_callback, warned
        )
    )
    if migrate_warn_callback is not None:
        migrate_warn_callback(LEGACY_LOCAL_PATTERNS_WARN)
    return rules
