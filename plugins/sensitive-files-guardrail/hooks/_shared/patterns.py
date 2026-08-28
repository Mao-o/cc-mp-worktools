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
from typing import Callable, Iterable, Optional

_PREFERRED_SUBPATH = Path(".claude") / "sensitive-files-guardrail" / "patterns.local.txt"
# rename 前 (sensitive-files-guard) の旧配置。新パスが無いときのみ fallback で読む。
_LEGACY_SUBPATH = Path(".claude") / "sensitive-files-guard" / "patterns.local.txt"

# migrate_warn_callback に渡す固定トークン (パスを含めない — ログ秘密非混入 +
# core.logging の detail 文字種ホワイトリスト `^[A-Za-z0-9_:.\-\[\]!]{0,64}$` 適合)。
LEGACY_LOCAL_PATTERNS_WARN = "legacy_patterns_local_in_use"

_PROJECT_SECTION_PREFIX = "[project:"
_PROJECT_SECTION_SUFFIX = "]"

# 除外案内 (read 側 deny reason / Stop 側 block reason) で見せる表示用パス
# (0.19.0)。実体の解決は ``_resolve_local_patterns_path`` (``Path.home()`` 基準)
# で、表示は ``~`` 表記のまま固定する (reason に絶対パスを出さない)。
LOCAL_PATTERNS_DISPLAY_PATH = "~/.claude/sensitive-files-guardrail/patterns.local.txt"
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
EXCLUDE_SCOPE_WARNING = (
    "影響範囲: path 形 (`!<root 相対パス>`) は**その 1 ファイルだけ**"
    " (プロジェクト root 相対で評価するので root 配下のみ)。"
    "basename 形 (`!<名前>`) は{scope}が**すべて**対象で、"
    "**同名ディレクトリの配下も外れます** (配下が別の include 行に単独一致"
    "する場合はそちらが優先)。`[project:]` は rule の読込先を決めるだけなので、"
    "basename 形は**このセッションが触る絶対パス全部** (他プロジェクト含む) に効きます。"
    "どちらの形も、外れるのは Stop の報告だけでなく"
    " **Read / Bash / Edit / Write の保護そのもの**です。"
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

    値は ``_resolve_project_key`` と**同一** (``$CLAUDE_PROJECT_DIR`` 優先 →
    ``.git`` 上方探索)。同じ関数を別名で公開するのは意図的で、
    「``[project:<key>]`` セクションの key」と「そのセクションに書いた path 形
    rule の基準 root」が定義上同じものであることを呼出側に示すため。両 hook
    (Read / Edit / Bash / Stop) が同じ root で matcher を呼ぶので、どの hook が
    出したレシピも他の hook で同じ 1 ファイルに効く。git の toplevel を Stop
    だけで使うと monorepo (``$CLAUDE_PROJECT_DIR`` がサブディレクトリ) で
    Stop のレシピが Read で効かなくなるため、あえて git には寄せない。
    None (root 不明) なら path 形 rule は評価されない。
    """
    return _resolve_project_key(cwd)


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
    project_key: Optional[str],
    header_warn_callback: Optional[Callable[[str], None]] = None,
) -> list[tuple[str, bool]]:
    """patterns.local.txt を ``[project:<path>]`` セクション対応でパースする。

    ファイル先頭〜最初のセクションヘッダーまでの行は全プロジェクト共通。
    ``[project:<path>]`` 行以降は次のヘッダーか EOF までそのセクション専用になる。
    ``project_key`` に一致するセクションの行だけを共通行と合わせ、**ファイル中の
    元の出現順のまま** 返す (グループ単位で並べ替えない — 出現順が
    last-match-wins の強さを決めるため)。

    ``project_key`` が None (プロジェクト未解決) の場合はどのセクションにも
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
    """
    rules: list[tuple[str, bool]] = []
    active = True  # 現在のセクションが出力対象か (共通行は常に active)
    warned: set[str] = set()
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
            active = project_key is not None and (
                os.path.normpath(header_key) == project_key
            )
            continue
        if not active:
            continue
        if stripped.startswith("!"):
            rules.append((stripped[1:], True))
        else:
            rules.append((stripped, False))
    return rules


def load_patterns(
    patterns_file: Path,
    warn_callback: Optional[Callable[[str], None]] = None,
    migrate_warn_callback: Optional[Callable[[str], None]] = None,
    cwd: str = "",
    header_warn_callback: Optional[Callable[[str], None]] = None,
) -> list[tuple[str, bool]]:
    """既定 patterns.txt + ローカル patterns.local.txt を読んで rules list を返す。

    既定 → ローカルの順で連結 (last match wins なので末尾のローカルが強い)。
    ローカル非存在は無視、読み取り中の OSError (FileNotFound 以外) は
    ``warn_callback(err_name)`` に渡して既定のみ返す。

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

    ``cwd`` (呼出元 hook の envelope 由来、任意): ``_resolve_project_key`` で
    プロジェクト識別子に解決し、ローカルファイル中の ``[project:<key>]``
    セクションのうち一致するものだけを共通行と合わせて読み込む
    (``_parse_local_patterns_text`` 参照)。空文字列 (既定) なら共通行のみ。

    ``header_warn_callback`` (0.19.0、任意): ``[project:]`` ヘッダーが空 / 未展開
    placeholder のときに固定トークンで呼ぶ (``_parse_local_patterns_text`` 参照)。

    Raises:
        FileNotFoundError: 既定 patterns.txt が存在しない
        OSError: 既定 patterns.txt の読み取りに失敗した
    """
    rules = _parse_patterns_text(patterns_file.read_text())
    project_key = _resolve_project_key(cwd)

    local_path = _resolve_local_patterns_path()
    try:
        local_text = local_path.read_text()
    except FileNotFoundError:
        # 新パスが無い → rename 前の旧パスを fallback で試す。
        return _load_legacy_local(
            rules, warn_callback, migrate_warn_callback, project_key,
            header_warn_callback,
        )
    except OSError as e:
        if warn_callback is not None:
            warn_callback(type(e).__name__)
        return rules

    rules.extend(
        _parse_local_patterns_text(local_text, project_key, header_warn_callback)
    )
    return rules


def _load_legacy_local(
    rules: list[tuple[str, bool]],
    warn_callback: Optional[Callable[[str], None]],
    migrate_warn_callback: Optional[Callable[[str], None]],
    project_key: Optional[str] = None,
    header_warn_callback: Optional[Callable[[str], None]] = None,
) -> list[tuple[str, bool]]:
    """新パス不在時に rename 前の旧 patterns.local.txt を fallback 読み込みする。

    旧パスが存在すれば rules に連結し ``migrate_warn_callback`` で移行を促す。
    旧パス非存在は黙殺、FileNotFound 以外の OSError は ``warn_callback`` に委譲。
    いずれも既定 rules (+ 旧ローカル) を返す。``[project:...]`` セクション対応は
    新パスと同じ (``_parse_local_patterns_text``)。
    """
    legacy_path = _resolve_legacy_local_patterns_path()
    try:
        legacy_text = legacy_path.read_text()
    except FileNotFoundError:
        return rules
    except OSError as e:
        if warn_callback is not None:
            warn_callback(type(e).__name__)
        return rules

    rules.extend(
        _parse_local_patterns_text(legacy_text, project_key, header_warn_callback)
    )
    if migrate_warn_callback is not None:
        migrate_warn_callback(LEGACY_LOCAL_PATTERNS_WARN)
    return rules
