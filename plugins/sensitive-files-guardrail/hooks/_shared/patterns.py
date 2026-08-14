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

プロジェクトスコープの拡張 (Unreleased):
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
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

_PREFERRED_SUBPATH = Path(".claude") / "sensitive-files-guardrail" / "patterns.local.txt"
# rename 前 (sensitive-files-guard) の旧配置。新パスが無いときのみ fallback で読む。
_LEGACY_SUBPATH = Path(".claude") / "sensitive-files-guard" / "patterns.local.txt"

# migrate_warn_callback に渡す固定トークン (パスを含めない — ログ秘密非混入 +
# core.logging の detail 文字種ホワイトリスト `^[A-Za-z0-9_:.\-\[\]!]{0,64}$` 適合)。
LEGACY_LOCAL_PATTERNS_WARN = "legacy_patterns_local_in_use"

_PROJECT_SECTION_PREFIX = "[project:"
_PROJECT_SECTION_SUFFIX = "]"


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


def _parse_local_patterns_text(
    text: str, project_key: Optional[str]
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
    """
    rules: list[tuple[str, bool]] = []
    active = True  # 現在のセクションが出力対象か (共通行は常に active)
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
            rules, warn_callback, migrate_warn_callback, project_key
        )
    except OSError as e:
        if warn_callback is not None:
            warn_callback(type(e).__name__)
        return rules

    rules.extend(_parse_local_patterns_text(local_text, project_key))
    return rules


def _load_legacy_local(
    rules: list[tuple[str, bool]],
    warn_callback: Optional[Callable[[str], None]],
    migrate_warn_callback: Optional[Callable[[str], None]],
    project_key: Optional[str] = None,
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

    rules.extend(_parse_local_patterns_text(legacy_text, project_key))
    if migrate_warn_callback is not None:
        migrate_warn_callback(LEGACY_LOCAL_PATTERNS_WARN)
    return rules
