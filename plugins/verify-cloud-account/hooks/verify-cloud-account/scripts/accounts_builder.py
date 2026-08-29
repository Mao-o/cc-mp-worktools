"""accounts.local.json の builder (唯一の正規書込経路)。

accounts.local.json の編集は builder 経由で行う運用に統一する。動作の安定や
フォーマット統一のため、書込先パス・JSON フォーマット・既存キーの扱い・
stdout の値表示制御を builder 側で一元管理する。Agent Skill (`accounts-init`
`accounts-show` `accounts-migrate`) が対話フローを提供し、Claude は skill
経由で builder を呼ぶ。

設計判断 (D1〜D13):

- **D1**: builder が唯一の正規経路。書込パスの固定、JSON フォーマットの
  一貫化、既存キーの温存、CLI 現在値との突合、旧パス統合を一元管理する。
- **D2**: 書込対象は 3-tier の **new パス** (`core/paths.accounts_file_new()`
  = `<anchor>/.claude/verify-cloud-account/accounts.local.json`) に固定。
  basename は `_ALLOWED_BASENAME` の assertion で保証する。
  **D14 で `--path` を追加したため「argv から一切変えられない」ではなくなった**
  が、`--path` が受け付けるのは `_PATH_TIERS` の配置だけで、書込を伴う
  コマンドでは new パスに限る。つまり builder が書くファイルは常に
  「どこかの project ディレクトリ配下の
  `.claude/verify-cloud-account/accounts.local.json`」= dispatcher が読む
  配置であり、任意パスへの書込には使えない。
- **D3**: stdout の値表示は既定で隠蔽。`--show-values` 明示時のみ値を
  stdout に出す。
- **D4**: 3-tier lookup で競合検出時は deny。migrate で統合する。
- **D5**: `migrate` サブコマンドで旧パス → 新パスへの統合を提供。
- **D6**: `set` / `remove` サブコマンドで既存値の更新・削除を提供する。
  `--host` で dict 値の特定キー (hostname / alias) だけを追加・上書き・削除
  できる。最後の 1 キーを remove すると空 dict を残さずキー自体を削除する
  (空 dict は github/firebase/gcloud の `verify()` が permanent deny する形の
  ため)。
- **D7**: `init` / `set` / `migrate` は書込前に `_validate_entry_shape()` で
  値の形 (str または service ごとの許容 dict 形) を検証し、不正なら exit 1。
  `migrate` は取り込み元 (旧パス) の値だけを検証対象にし、かつ dict の未知
  キーと個々の値の不正 (dict 内に使える値が 1 つでも残っていれば良い) の
  両方を許容する (`strict_keys=False`) — `verify()` 自体が寛容な形を migrate
  でだけ厳格化すると、migrate が触ってすらいない既存データや、verify() が
  実際には許容する部分的に壊れたデータが理由で、無関係な統合作業まで
  exit 1 になる退行を生むため。
- **D8**: `migrate` の衝突判定は `_migrate_keep_new_without_loss()` で
  「new 側を採用しても old 側の情報を失わないか」だけを見る。既存の
  `_entries_equal()` (show/verify 用、CLI 実測値が期待値を満たすかの非対称な
  述語) をそのまま流用すると、new=scalar "USER" / old=multi-host dict
  `{"github.com":"USER","ghe.example.com":"example-org"}` のような入力で
  `next(iter(old.values()))` (= 最初の host の値) だけを見て一致と判定し、
  `ghe.example.com` の値を conflict 検出も警告もなく消してしまう
  (advisor レビューで検出)。migrate は「意味的に同じアカウントか」ではなく
  「情報を捨てて良いか」を判定する必要があるため、別関数として実装した。
- **D9**: `_validate_entry_shape()` の dict 内の値チェックは、`strict_keys`
  (D7) の他に「dict 全体で有効な値が 1 つも無いときだけ拒否する」形にした。
  gcloud/firebase の verify() は使える値が 1 つでも残っていれば動く (例:
  `{"default":"proj-dev","old":null}` は firebase.verify() が "old" を
  無視して成立させる)。migrate だけに厳格さを適用すると、verify() では
  通っていた形を後から書けなくする退行を生む。
- **D10**: ただし D9 の leniency は **service ごとの `DICT_VALUE_CHECK` 契約**
  (services/__init__.py) の範囲に限る。当初は全 service に一律で適用していたが、
  それだと**その service の verify() が形を理由に deny する値**まで書けてしまい、
  書込時検証をすり抜けて実行時 deny に化けていた (Codex R1 P2:
  `{"project":"p","account":123}` は gcloud.verify() が truthy 非 str の account
  を reject するのに builder は通していた)。守る不変条件は
  「`_validate_entry_shape()` が受理した形は、その service の `verify()` が
  **形を理由に** deny しない」。tests/test_accounts_builder.py の
  `TestBuilderAcceptedShapesPassVerify` が service × 形状の表で固定する。
  空文字・空白のみの値は例外的に全 service で常に拒否する — どの service でも
  実在の CLI 値と一致し得ず永久 deny になるだけの形だから (空文字キーと同じ扱い)。
- **D11**: `_migrate_keep_new_without_loss()` の scalar↔dict 比較は
  service の `SCALAR_EQUIVALENT_DICT_KEY` を使う。値と dict 長だけで比較して
  いた当初の実装は **host 意味論を持たない** (Codex R1 P1): scalar "USER" は
  github.com (active なら) を照合し、`{"ghe.example.com":"USER"}` は名指しの
  GHE ホストを照合するので、値が同じでも制約が違う。値一致だけで非損失と
  判定すると GHE の制約を無警告で捨てていた。キーを宣言しない service と、
  キーが一致しない組は conflict に倒す。
  **宣言できるのは「scalar 分岐の照合先が実行時の状態に依らず固定」の service
  だけ** (Codex R4 P1)。現状の宣言は gcloud の `"project"` のみ:
  `gcloud.verify()` の str 分岐は `_check_project()` しか呼ばず account を
  見ないため、scalar と `{"project": <同値>}` は静的に等価になる。
  一方 `github` は当初 `"github.com"` を宣言していたが**撤回した** —
  `github.verify()` の str 分岐は「github.com が active ならそれ、無ければ
  最初の active host」を照合する動的な意味論で、ghe だけが active な環境では
  scalar "USER" が allow・`{"github.com":"USER"}` が deny になる。等価でない
  ものを等価と宣言すると、migrate が明示された github.com の要求を無警告で
  捨てる/書き換える。`firebase` も未宣言 (alias は verdict に効かず、
  畳み込むと自己回復の案内先が消える)。未宣言 service の scalar/dict 混在は
  **両方向とも** conflict。
- **D12**: D9 の「dict 内に使える値が 1 つでも残っていれば良い」は、
  `DICT_ALLOWED_KEYS` を宣言する service では**許可キーの値だけ**を数える
  (Codex R3 P2): `{"gcloud":{"region":"us-central1"}}` は "region" が非空
  文字列なので D9 の基準では「使える値がある」と誤認して受理していたが、
  `gcloud.verify()` は `DICT_ALLOWED_KEYS` 外のキーを読まないため project も
  account も無いとして deny する — migrate は成功と報告したのに、書いた
  entry がそのままでは使えない状態になっていた。未知キーの値そのものは
  (D10 の `DICT_VALUE_CHECK` 契約に従う形である限り) 引き続き許容するが、
  「使える値が 1 つでもあるか」の判定からは除外する。`DICT_ALLOWED_KEYS` を
  宣言しない service (`github`/`firebase`) は影響を受けない — allowed_keys が
  `None` のときは従来どおり全キーの値を対象にする。
- **D13**: `remove --host` で dict 値の特定キーを削除した後の**残り dict**も
  書込前に形を再検証する。D6 は「最後の 1 キーを消して空 dict になる」場合
  だけキー自体を削除していたが、それ以外でも残り dict がその service に
  とって `_validate_entry_shape(strict_keys=False)` (D9/D12 と同じ lenient
  判定) に拒否される形になることがある。例: `DICT_ALLOWED_KEYS` を宣言する
  gcloud で `{"project":"p","region":"x"}` から `project` を削除すると
  `{"region":"x"}` が残るが、`region` は許可キー外で `gcloud.verify()` が
  読まないため project も account も無いとして deny する — 空 dict と同じく
  「書込は成功するのに verify() が fail-closed deny する」状態になる。
  lenient 検証に拒否される残り dict は、空 dict の場合と同じくキー自体を
  削除し、なぜ消したかを `_validate_entry_shape` の理由文字列をそのまま
  stdout に明示する (D3 の値隠蔽とは衝突しない — 理由文字列はキー名/型名の
  みを含み、dict の値そのものは含まない)。
- **D14**: 全サブコマンドは **dispatcher と同じ解決** (`_resolve_target()` →
  `core/paths.resolve_accounts_file()` と同じ 3-tier + 親遡及) で
  「hook が実際に読むファイル」を対象にする (Codex R4 P2)。従来は常に
  `accounts_file_new(cwd)` を対象にしていたため、祖先の accounts.local.json を
  継承している worktree / サブディレクトリで `set --commit` が**編集した
  service だけを含む子ファイル**を作り、dispatcher の遡及がその子ファイルで
  止まって継承していた他 service が一斉に未設定 (deny) になっていた
  (`remove` も継承 entry を「存在しません」と報告していた)。
  対象パスは dry-run / commit の両方で先頭に表示する (`_target_note()`)。
  `init` は継承中に cwd 直下へ新規作成しようとすると shadowing になるため
  exit 2 で拒否し、`set` / `remove` / `--path` を案内する。
  `--path <file>` は解決を飛ばして対象を明示する escape hatch
  (worktree 専用設定を意図的に作る場合)。受け付ける配置は `_PATH_TIERS` に
  限る (D2 参照)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import IO, Any, NamedTuple

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from core import paths  # noqa: E402
from services import ALL as SERVICES  # noqa: E402

_SERVICE_NAMES = [svc.ACCOUNT_KEY for svc in SERVICES]
_SERVICE_BY_KEY = {svc.ACCOUNT_KEY: svc for svc in SERVICES}

_VALUE_HIDDEN_MARK = "(value hidden. use --show-values to reveal)"

# accounts.local.json と同じディレクトリに同梱する Claude 向け案内ファイル。
# sensitive-files-guardrail 等で *.local.json への直接アクセスが deny される事情と
# builder 経由の正規経路を Claude (LLM) に signpost する。
_PROJECT_CLAUDE_MD_FILENAME = "CLAUDE.md"
_PROJECT_CLAUDE_MD_TEMPLATE = _HERE / "templates" / "project_claude.md"


class _BuilderError(Exception):
    """builder 内部のビジネスエラー。既定で exit 1 に繋げる。

    `exit_code` で使い方の誤り (exit 2) と実行時の業務エラー (exit 1) を
    区別する。argparse の usage error と同じ 2 を使い分けることで、呼び出し側
    (skill / スクリプト) が「引数を直せば良い」のか「環境を直す必要がある」の
    かを終了コードだけで判別できる。
    """

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _project_dir() -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


# `--path` が受け付ける配置 (project ディレクトリからの相対)。dispatcher が
# 読むのはこの 3 つだけなので、ここに当てはまらないパスを許すと「builder は
# 書けるが hook は一生読まない」ファイルを作れてしまう。D2 (書込先固定) は
# `--path` の導入で「argv から一切変えられない」ではなくなったが、
# **書込先は必ず 3-tier の new パス (basename は accounts.local.json)** という
# 形で維持する。
_PATH_TIERS = (
    ("new", paths.ACCOUNTS_FILE_NEW),
    ("deprecated", paths.ACCOUNTS_FILE_DEPRECATED),
    ("legacy", paths.ACCOUNTS_FILE_LEGACY),
)


class _Target(NamedTuple):
    """操作対象の accounts.local.json と、それが属する project ディレクトリ。

    - path: 対象ファイルの絶対パス
    - anchor: そのファイルが属する project ディレクトリ (絶対パス)。
      3-tier 探索・.gitignore 更新・CLAUDE.md 同梱の基準になる
    - kind: "new" / "deprecated" / "legacy"
    - origin: どう決まったか。
      "explicit" (--path) / "cwd" (cwd 階層で発見) /
      "ancestor" (祖先階層から継承) / "fresh" (どこにも無いので新規作成)
    """

    path: Path
    anchor: Path
    kind: str
    origin: str


def _split_tier_path(path: Path) -> tuple[str, Path] | None:
    """絶対パスを (kind, anchor) に分解する。3-tier のどれでもなければ None。"""
    parts = path.parts
    for kind, rel in _PATH_TIERS:
        rel_parts = rel.parts
        if len(parts) > len(rel_parts) and parts[-len(rel_parts):] == rel_parts:
            return kind, Path(*parts[: -len(rel_parts)])
    return None


def _resolve_target(
    project_dir: str,
    explicit: str | None,
    *,
    require_new: bool,
) -> _Target:
    """**hook が実際に読むファイル**を対象として決める (Codex R4 P2)。

    `--path` 未指定なら `core/paths.resolve_accounts_file()` と同じ探索
    (3-tier lookup + 親ディレクトリ遡及) を使い、dispatcher が読むのと同じ
    ファイルを対象にする。builder が常に cwd 直下の新パスを対象にしていた
    従来の実装は、**祖先の accounts.local.json を継承している worktree /
    サブディレクトリで shadowing を起こしていた**: `set --commit` が編集した
    service だけを含む子ファイルを作り、dispatcher の遡及がその子ファイルで
    止まるため、継承していた他 service が一斉に未設定 (deny) になる。
    `remove` も継承 entry を「存在しません」と報告していた。

    `--path` 指定時は探索を飛ばしてそのファイルを対象にする (worktree 専用
    設定を意図的に作るための escape hatch)。ただし dispatcher が読む配置
    (`_PATH_TIERS`) に限る — それ以外は「書けるが読まれない」設定になるため。
    書込を伴うコマンド (`require_new=True`) では new パスのみ許可する。
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        # 解決経路 (`discover_accounts_files_with_ancestors`) 側も resolve() を
        # 通すので、比較・表示のために同じ正規化を掛ける。揃えないと symlink
        # (macOS の /var → /private/var 等) を挟んだだけで「別ファイル」と
        # 誤判定し、shadowing 警告が誤発火する。
        try:
            path = path.resolve()
        except OSError:
            path = Path(os.path.normpath(str(path)))
        split = _split_tier_path(path)
        if split is None:
            raise _BuilderError(
                f"--path は dispatcher が読む配置を指してください (指定: {path})。"
                "許容する末尾: "
                + " / ".join(str(rel) for _kind, rel in _PATH_TIERS)
                + "。これ以外の場所に書いても hook は読み込みません。",
                exit_code=2,
            )
        kind, anchor = split
        if require_new and kind != "new":
            raise _BuilderError(
                f"--path の書込先は {paths.ACCOUNTS_FILE_NEW} で終わる新パスに"
                f"してください (指定: {path} は {kind} パス)。"
                "旧パスへの新規書込は複数パス conflict の原因になります。",
                exit_code=2,
            )
        return _Target(path=path, anchor=anchor, kind=kind, origin="explicit")

    try:
        project = Path(project_dir).resolve()
    except OSError:
        project = Path(project_dir)
    found, resolved_dir = paths.discover_accounts_files_with_ancestors(project_dir)
    if not found:
        return _Target(
            path=paths.accounts_file_new(str(project)),
            anchor=project,
            kind="new",
            origin="fresh",
        )
    # found は new → deprecated → legacy の優先度順。複数 tier が同居する
    # (D4 conflict) 場合も先頭を代表として返し、拒否は呼び出し側が行う
    # (`_refuse_if_legacy_paths_exist` / show の複数パス error / migrate の統合)。
    kind, path = found[0]
    anchor = resolved_dir if resolved_dir is not None else project
    return _Target(
        path=path,
        anchor=anchor,
        kind=kind,
        origin="ancestor" if anchor != project else "cwd",
    )


def _hook_reads_instead(target: _Target, project_dir: str) -> Path | None:
    """`--path` 指定が、解決なら選ばれていたファイルと食い違うならそのパスを返す。

    `--path` は「解決を飛ばす escape hatch」なので、指定先が hook の読むファイルと
    ずれることがある。ずれの向きで結果が変わる:

    - 指定先が cwd に**近い**: 書いた瞬間から hook はそちらを読む = 現在有効な
      設定を覆い隠す (指定先に記載しない service は未設定 = deny)。`init` が
      exit 2 で止めている shadowing を、`--path` は意図的に許す形になるため、
      **その代償をその場で必ず言う**
    - 指定先が cwd から**遠い**: hook は近い方を読み続けるので、書いても効かない

    どちらも黙って進むと「設定したのに検証されない / 別 service が急に deny
    される」になるので、区別せず 1 本の警告にまとめる。
    """
    if target.origin != "explicit":
        return None
    try:
        resolved = _resolve_target(project_dir, None, require_new=False)
    except _BuilderError:
        return None
    if resolved.origin == "fresh" or resolved.path == target.path:
        return None
    return resolved.path


def _target_note(target: _Target, project_dir: str) -> str:
    """出力の先頭に置く「どのファイルを対象にしたか」の 1 行 (+ 必要なら警告)。

    親から継承しているケースを利用者が見落とすと、意図せず親 repo の設定を
    書き換える / 書き換えたつもりが効かない、のどちらかが起きるため、
    dry-run と commit の両方で必ず表示する。
    """
    if target.origin == "ancestor":
        return f"対象: {target.path} (祖先ディレクトリ {target.anchor} から継承)"
    if target.origin == "explicit":
        note = f"対象: {target.path} (--path で明示指定)"
        other = _hook_reads_instead(target, project_dir)
        if other is not None:
            note += (
                f"\n警告: hook が現在読むのは {other} です。--path はそれとは別の"
                "ファイルを対象にするため、指定先が cwd に近ければ現在の設定を"
                "覆い隠し (指定先に記載しない service は未設定 = deny)、遠ければ"
                "書いても読まれません。"
            )
        return note
    if target.origin == "fresh":
        return f"対象: {target.path} (新規作成)"
    return f"対象: {target.path}"


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise _BuilderError(
            f"既存 {path} の JSON が不正です: {e.msg} (行 {e.lineno})。"
            "手動で修正してから再実行してください。"
        )
    except OSError as e:
        raise _BuilderError(f"{path} の読み込みに失敗しました: {e}")
    if not isinstance(data, dict):
        raise _BuilderError(
            f"{path} は JSON オブジェクト ({{...}}) である必要があります。"
        )
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _ensure_gitignore_entry(target: _Target, stdout: IO[str]) -> None:
    """accounts.local.json を .gitignore に追加する (best-effort)。

    既に該当エントリがあればスキップ。.gitignore が存在しなければ作成しない
    (プロジェクトに .gitignore が無い環境で勝手に作ると意図しない副作用になる)。

    対象は **書き込んだファイルが属する project ディレクトリ** (`target.anchor`)
    の .gitignore で、エントリもそこからの相対パスにする。cwd 固定にすると、
    祖先から継承しているとき「親 repo のファイルを書いたのに cwd 側の
    .gitignore を編集する」ちぐはぐな挙動になる (D14)。
    """
    gitignore = target.anchor / ".gitignore"
    try:
        entry = target.path.relative_to(target.anchor).as_posix()
    except ValueError:
        return
    if not gitignore.exists():
        return
    try:
        content = gitignore.read_text(encoding="utf-8")
        if any(line.strip() == entry for line in content.splitlines()):
            return
        if not content.endswith("\n"):
            content += "\n"
        content += f"{entry}\n"
        gitignore.write_text(content, encoding="utf-8")
        print(f"updated: {gitignore} ({entry} を追加)", file=stdout)
    except OSError as e:
        print(f"warning: .gitignore の更新に失敗しました: {e}", file=stdout)


def _ensure_project_claude_md(target: _Target, stdout: IO[str]) -> None:
    """書き込んだファイルと同じディレクトリに Claude 向け signpost
    (`CLAUDE.md`) を同梱する。

    - 既に CLAUDE.md が存在する場合は何もしない (ユーザー編集を尊重)
    - テンプレート読み込み・書き込みのいずれかが失敗しても警告を 1 行出すだけで
      builder 自体は成功させる (best-effort)
    - dispatcher 等が読みに来るパスではないため、ここで失敗しても plugin 本体
      の動作に影響しない

    plugin 同士の疎結合を保つための signpost: sensitive-files-guardrail が
    `*.local.json` への直接アクセスを deny する事情と、builder 経由の正規経路
    (Agent Skill / Bash) を Claude (LLM) に伝える。
    """
    target_dir = target.path.parent
    md_path = target_dir / _PROJECT_CLAUDE_MD_FILENAME
    if md_path.exists():
        print(f"(skipped: {md_path} already exists)", file=stdout)
        return
    try:
        content = _PROJECT_CLAUDE_MD_TEMPLATE.read_text(encoding="utf-8")
    except OSError as e:
        print(
            f"warning: project CLAUDE.md template の読み込みに失敗しました: {e}",
            file=stdout,
        )
        return
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"warning: {md_path} の書き込みに失敗しました: {e}", file=stdout)
        return
    print(f"created: {md_path}", file=stdout)


def _format_value_for_display(value: Any, show_values: bool) -> str:
    if show_values:
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return f"<dict with {len(value)} key(s)> {_VALUE_HIDDEN_MARK}"
    return _VALUE_HIDDEN_MARK


def _print_change_line(
    status: str,
    key: str,
    value: Any,
    show_values: bool,
    stdout: IO[str],
) -> None:
    display = _format_value_for_display(value, show_values)
    if show_values:
        print(f"{status}: {key} -> {display}", file=stdout)
    else:
        print(f"{status}: {key}", file=stdout)
        print(f"  {display}", file=stdout)


def _parse_value(raw: str) -> Any:
    """`--value` の生文字列を解釈する。

    dict 形 (JSON object) だけを構造化データとして受理し、それ以外
    (JSON として不正 / dict 以外の JSON 型) は生文字列のまま返す。
    services の期待値スキーマは str または dict[str, str] のみ
    (services/*.py の verify() 参照) で、数値・真偽値・配列を暗黙変換すると
    "12345" のような数字だけの username が int になり検証が壊れる
    (内部バックログ: --value の JSON が文字列保存されたまま gcloud.verify が
    dict 期待値と JSON 文字列を比較して永久不一致になる不具合の修正)。
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if isinstance(parsed, dict):
        return parsed
    return raw


def _dict_value_shape_denied(service, key: str, val: Any, *, allowed_keys) -> bool:
    """非 str の dict 値を、その service の `verify()` が**形を理由に** deny するか。

    services/*.py の `DICT_VALUE_CHECK` 契約 (services/__init__.py 参照) を読む。
    builder の緩和モード (strict_keys=False) が「verify() は拒否するのに builder は
    書ける」形を作らないための対応表:

    - ``"all"`` (github): verify() は dict の**全キー**の値を `isinstance(v, str)`
      で検査するので、非 str は falsy でも deny 要因 → 常に True
    - ``"truthy"`` (gcloud): verify() は `if <key>_want:` で値を拾うため falsy は
      黙って無視し、truthy な非 str だけ「文字列で指定してください」で reject
      → truthy のときだけ True
    - ``"none"`` (firebase): verify() は使えない値を filter で捨てるだけで、値の形を
      理由に deny しない → 常に False

    `DICT_ALLOWED_KEYS` を宣言している service では、宣言外のキーは verify() が
    そもそも読まない (gcloud は未知キーを無視する) ため、値の形は問わない。
    未宣言時の既定は最も厳しい ``"all"`` — 新しい service が宣言を忘れたときに
    「素通し」ではなく「厳しすぎる」方向で失敗させるため (穴を作らない側)。
    """
    mode = getattr(service, "DICT_VALUE_CHECK", "all")
    if mode == "none":
        return False
    if allowed_keys is not None and key not in allowed_keys:
        return False
    if mode == "truthy":
        return bool(val)
    return True


def _validate_entry_shape(service, value: Any, *, strict_keys: bool = True) -> str | None:
    """value が service の accounts.local.json 値として許容される**形**か検証する。

    services/*.py の verify() が受理する型 (str または dict) と揃えた形だけの
    チェック。実 verify() (CLI を叩く) は書込時に対象 CLI へ到達できるとは
    限らないため呼ばない。不正なら理由文字列、OK なら None を返す。

    strict_keys=False (migrate が旧パスから取り込む既存データ向け) では dict の
    **未知キー**と**個々の値の不正**の両方を緩める — gcloud.verify() は宣言外
    キーを黙って無視するだけで拒否しないし、gcloud/firebase の verify() は
    「使える値が dict 内に 1 つでも残っていれば全体としては動く」形の寛容さを
    持つ (firebase: `isinstance(v, str) and v` を満たす値だけを filter して
    1 つも無ければ拒否、gcloud: project/account が両方 falsy なら拒否・
    片方が truthy で型不正なら拒否だが falsy な方は黙って無視)。migrate でだけ
    厳格化すると verify() では通っていた形 (例: `{"default":"proj-dev",
    "old":null}` は firebase.verify() が "old" を無視して "default" だけで
    成立させる) を後から書けなくする退行になる (migrate が触っていない新パス側
    の既存エントリまで巻き込んで書込全体を deny すると、この plugin が解消
    しようとしている「手動 JSON 編集の要求」を復活させてしまう)。
    ただし leniency の範囲は **service ごとの `DICT_VALUE_CHECK` 契約**に合わせる
    (D10)。「dict 全体で有効な値が 1 つも無いときだけ拒否」を全 service に一律
    適用すると、その service の verify() が**形を理由に deny する**値まで書けて
    しまい、書込時検証をすり抜けて実行時 deny に化ける (Codex R1 P2:
    `{"project":"p","account":123}` は gcloud.verify() が truthy 非 str の
    account を reject するのに builder が通していた)。
    さらに「使える値が 1 つでもあるか」の判定は、`DICT_ALLOWED_KEYS` 宣言時は
    **許可キーの値だけ**を対象にする (D12)。未知キーの非空文字列値まで
    数えると、`{"region":"us-central1"}` のように project/account が無い
    entry を「使える値がある」と誤認して受理してしまう。
    型不正 (list/int/None 等トップレベルの型) と空 dict は verify() 自体が
    拒否するため strict_keys に関わらず常に検証する。空文字・空白のみの
    **値**も同様に常に拒否する — どの service でも実在の CLI 値と一致し得ず
    永久 deny になる形で、空文字キーと同じ失敗形だから (Codex R1 P2:
    scalar 側の `--value "$UNSET_VAR"` と同じ穴の dict 版)。
    """
    if isinstance(value, str):
        if not value.strip():
            # dispatcher は `entry == ""` を**キー欠落と同じ**扱いにして恒久 deny
            # する (core/dispatcher.py)。`set --value "$UNSET_VAR"` のように未定義の
            # 変数が空文字に展開されるとここを素通りし、書込時検証を通ったのに
            # 次の CLI 操作が deny される状態を作ってしまう。dict の値・キーと
            # 同じ規則 (strip 後に非空) を scalar にも課す。
            return (
                f"{service.ACCOUNT_KEY}: 値に空文字・空白のみの文字列は使えません"
                " (未設定の環境変数を渡していないか確認してください)。"
            )
        return None
    if isinstance(value, dict):
        if not getattr(service, "ACCEPTS_DICT", False):
            return (
                f"{service.ACCOUNT_KEY}: この service はオブジェクト形式の値を"
                "受け付けません。文字列で指定してください。"
            )
        if not value:
            return f"{service.ACCOUNT_KEY}: オブジェクトが空です。"
        allowed_keys = getattr(service, "DICT_ALLOWED_KEYS", None)
        good_values = 0
        for k, v in value.items():
            if not isinstance(k, str):
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトのキーは文字列である"
                    "必要があります。"
                )
            if not k.strip():
                # 空文字だけでなく空白のみの文字列も弾く。github.verify() 等は
                # dict のキーをそのまま hostname/alias として照合するため、
                # 空白のみのキーも実在の CLI 値と一致し得ず永久 deny になる
                # (`--host ''` と同じ失敗形。`set --value` で dict を直接渡す
                # 経路や migrate の取り込みは `--host` の CLI guard を経由しない
                # ためここで弾く必要がある)。
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトのキーに空文字・"
                    "空白のみの文字列は使えません。"
                )
            if strict_keys and allowed_keys is not None and k not in allowed_keys:
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトのキー '{k}' は未対応です"
                    f" (許容: {', '.join(sorted(allowed_keys))})。"
                )
            if isinstance(v, str):
                if v.strip():
                    # 未知キー (DICT_ALLOWED_KEYS 宣言時) の値は形として許すが、
                    # 「使える値」の数には入れない (Codex R3 P2): verify() は
                    # 宣言外のキーをそもそも読まないため、許可キーに使える値が
                    # 1 つも無いのに未知キーの値だけで受理すると、migrate は
                    # 成功と報告したのに verify() は project も account も
                    # 無いとして deny する entry を書いてしまう。
                    if allowed_keys is None or k in allowed_keys:
                        good_values += 1
                    continue
                # 空文字・空白のみの値は strict / lenient を問わず拒否する。
                # 空文字キー (上) と同じく、実在の CLI 値と一致し得ず永久 deny に
                # なるだけの形なので、verify() が形として通すかに関係なく弾く。
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトの値に空文字・空白のみの"
                    f"文字列は使えません (キー '{k}')。"
                )
            if strict_keys:
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトの値は空でない文字列で"
                    f"ある必要があります (キー '{k}')。"
                )
            # strict_keys=False: 非 str 値をどこまで許すかは service の
            # DICT_VALUE_CHECK 契約に従う。verify() が形を理由に deny する値は
            # ここで止め (書込時検証をすり抜けさせない)、verify() が黙って無視
            # するだけの値は good_values のカウントに任せる (D9 の leniency)。
            if _dict_value_shape_denied(service, k, v, allowed_keys=allowed_keys):
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトの値は文字列である必要が"
                    f"あります (キー '{k}', 現在: {type(v).__name__})。"
                    " この service の verify() はこの形を検証時に拒否します。"
                )
        if not strict_keys and good_values == 0:
            if allowed_keys is not None:
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトにこの service が参照する"
                    f"キーの有効な値がありません (許容: "
                    f"{', '.join(sorted(allowed_keys))})。"
                )
            return f"{service.ACCOUNT_KEY}: 有効な値を持つキーがありません。"
        return None
    return (
        f"{service.ACCOUNT_KEY}: 値は文字列またはオブジェクトで指定してください "
        f"(現在: {type(value).__name__})。"
    )


def _cmd_init(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    service_key = args.service
    svc = _SERVICE_BY_KEY.get(service_key)
    if svc is None:
        print(
            f"error: unknown service '{service_key}'. "
            f"Available: {', '.join(_SERVICE_NAMES)}",
            file=stderr,
        )
        return 2

    project_dir = _project_dir()
    try:
        target = _resolve_target(project_dir, args.path, require_new=True)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code

    # 祖先の accounts.local.json を継承している階層で init すると、cwd 直下に
    # 2 つ目のファイルを作って**継承していた設定を丸ごと覆い隠す** (dispatcher の
    # 遡及は cwd 側で止まるため、編集していない service まで一斉に未設定に
    # なる)。set / remove は継承先を直接編集するのでこの問題が無い (D14)。
    if target.origin == "ancestor":
        print(
            f"error: accounts.local.json は祖先ディレクトリ {target.anchor} から"
            f"継承しています ({target.path})。"
            "init で cwd 直下に作ると継承中の設定を覆い隠し、"
            "記載していない service が一斉に未設定 (deny) になるため拒否します。",
            file=stderr,
        )
        print(
            f"継承中の {target.path} を編集するには set / remove を使ってください。"
            "この階層専用の設定を作る場合は --path で明示してください。",
            file=stderr,
        )
        return 2

    # 旧パス (deprecated/legacy) が存在する場合、init で新パスに書くと
    # dispatcher の _find_accounts_file が複数パス conflict で fail-closed deny に
    # 回帰するため refuse。先に migrate --commit で統合してから init を実行させる。
    if _refuse_if_legacy_paths_exist("init", target, stderr):
        return 1

    try:
        existing = _load_existing(target.path)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code

    if args.value is not None:
        new_entry: Any = _parse_value(args.value)
    else:
        new_entry = svc.suggest_accounts_entry(project_dir)
        if new_entry is None:
            print(
                f"error: {service_key} の現在値を CLI から取得できませんでした。"
                " --value で明示するか、CLI ログイン後に再実行してください。",
                file=stderr,
            )
            return 1

    shape_error = _validate_entry_shape(svc, new_entry)
    if shape_error:
        print(f"error: {shape_error}", file=stderr)
        return 1

    existing_value = existing.get(service_key)
    if existing_value is None:
        action = "add"
    elif existing_value == new_entry:
        action = "unchanged"
    else:
        action = "skipped"

    print(_target_note(target, project_dir), file=stdout)
    print(f"=== changes to {target.path} ===", file=stdout)
    if action == "add":
        _print_change_line("+ add", service_key, new_entry, args.show_values, stdout)
    elif action == "unchanged":
        _print_change_line("= unchanged", service_key, existing_value, args.show_values, stdout)
    else:
        print(
            f"! skipped: {service_key} already exists with a different value.",
            file=stdout,
        )
        print(
            "  init does not overwrite existing entries. Use "
            f"`set --service {service_key} --value <new-value> --commit` "
            "(or `--from-cli`) to update it, or "
            f"`remove --service {service_key} --commit` to delete it first.",
            file=stdout,
        )
        _print_change_line("  existing", service_key, existing_value, args.show_values, stdout)
        _print_change_line("  proposed", service_key, new_entry, args.show_values, stdout)

    if args.commit:
        updated = dict(existing)
        if action == "add":
            updated[service_key] = new_entry
        try:
            _write_json(target.path, updated)
        except OSError as e:
            print(f"error: 書き込みに失敗しました: {e}", file=stderr)
            return 1
        print(f"\nwritten: {target.path}", file=stdout)
        _ensure_project_claude_md(target, stdout)
        _ensure_gitignore_entry(target, stdout)
    else:
        print("\n(dry-run; pass --commit to write)", file=stdout)

    return 0


def _refuse_if_legacy_paths_exist(
    command: str, target: _Target, stderr: IO[str]
) -> bool:
    """対象階層に旧パスが存在すれば True を返しつつ refuse 理由を stderr に書く。

    新パスへの書込で dispatcher が複数パス conflict の fail-closed deny に
    回帰するため、`init` / `set` / `remove` の全てで適用する。

    判定は **解決した対象階層 (`target.anchor`)** で行う (D14)。cwd 固定だと、
    祖先を継承しているときにその祖先階層の旧パスを見落として、fail-closed
    deny を誘発する書込を通してしまう。同一階層に複数 tier が同居する D4 の
    conflict もここで捕まる (2 つ以上なら必ず non-new が 1 つ以上含まれる)。
    """
    found = paths.discover_all_accounts_files(str(target.anchor))
    legacy_paths = [(kind, path) for kind, path in found if kind != "new"]
    if not legacy_paths:
        return False
    print(
        "error: 旧パスに accounts.local.json が存在します。"
        f"{command} で新パスを操作すると複数パス conflict で fail-closed deny に"
        "回帰するため拒否します。",
        file=stderr,
    )
    for kind, path in legacy_paths:
        print(f"  - {path} ({kind})", file=stderr)
    print(
        f"先に migrate --commit で新パスへ統合してから {command} を実行してください: "
        "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/"
        "accounts_builder.py migrate --commit",
        file=stderr,
    )
    return True


def _cmd_set(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    service_key = args.service
    svc = _SERVICE_BY_KEY.get(service_key)
    if svc is None:
        print(
            f"error: unknown service '{service_key}'. "
            f"Available: {', '.join(_SERVICE_NAMES)}",
            file=stderr,
        )
        return 2

    if args.host is not None and not getattr(svc, "ACCEPTS_DICT", False):
        print(
            f"error: {service_key} はオブジェクト形式の値を受け付けないため "
            "--host は使えません。--host を外して --value に文字列を指定して"
            "ください。",
            file=stderr,
        )
        return 2

    if args.host is not None and not args.host.strip():
        print(
            "error: --host に空文字は使えません。",
            file=stderr,
        )
        return 2

    if args.host is not None and args.from_cli:
        print(
            "error: --host 指定時は --value で値を明示してください (CLI の"
            "現在値がどの host のものかを builder は判別できません)。",
            file=stderr,
        )
        return 2

    project_dir = _project_dir()
    try:
        target = _resolve_target(project_dir, args.path, require_new=True)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code

    if _refuse_if_legacy_paths_exist("set", target, stderr):
        return 1

    try:
        existing = _load_existing(target.path)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code

    if args.value is not None:
        raw_new_value: Any = _parse_value(args.value)
    else:
        raw_new_value = svc.suggest_accounts_entry(project_dir)
        if raw_new_value is None:
            print(
                f"error: {service_key} の現在値を CLI から取得できませんでした。"
                " --value で明示するか、CLI ログイン後に再実行してください。",
                file=stderr,
            )
            return 1

    existing_value = existing.get(service_key)

    if args.host is not None:
        if not isinstance(raw_new_value, str):
            print(
                "error: --host 指定時は書き込む値が文字列である必要があります "
                f"(現在: {type(raw_new_value).__name__})。--from-cli が複数"
                "host/alias 分の dict を返す場合は --host を外して実行するか、"
                "--value で単一の値を明示してください。",
                file=stderr,
            )
            return 1
        if not isinstance(existing_value, (dict, type(None))):
            print(
                f"error: {service_key} の既存値がオブジェクトではありません "
                f"(現在: {type(existing_value).__name__})。--host は既存値が"
                " オブジェクトのとき (または未設定のとき) だけ使えます。先に "
                f"`remove --service {service_key} --commit` で削除してから "
                "オブジェクト形式で作り直すか、--host を外して上書きして"
                "ください。",
                file=stderr,
            )
            return 1
        base_dict = dict(existing_value) if isinstance(existing_value, dict) else {}
        new_entry: Any = {**base_dict, args.host: raw_new_value}
    else:
        new_entry = raw_new_value

    shape_error = _validate_entry_shape(svc, new_entry)
    if shape_error:
        print(f"error: {shape_error}", file=stderr)
        return 1

    if existing_value is None:
        action = "add"
    elif existing_value == new_entry:
        action = "unchanged"
    else:
        action = "update"

    print(_target_note(target, project_dir), file=stdout)
    print(f"=== changes to {target.path} ===", file=stdout)
    if action == "add":
        _print_change_line("+ add", service_key, new_entry, args.show_values, stdout)
    elif action == "unchanged":
        _print_change_line(
            "= unchanged", service_key, existing_value, args.show_values, stdout
        )
    else:
        _print_change_line(
            "- current", service_key, existing_value, args.show_values, stdout
        )
        _print_change_line("+ new", service_key, new_entry, args.show_values, stdout)

    if args.commit:
        updated = dict(existing)
        updated[service_key] = new_entry
        try:
            _write_json(target.path, updated)
        except OSError as e:
            print(f"error: 書き込みに失敗しました: {e}", file=stderr)
            return 1
        print(f"\nwritten: {target.path}", file=stdout)
        _ensure_project_claude_md(target, stdout)
        _ensure_gitignore_entry(target, stdout)
    else:
        print("\n(dry-run; pass --commit to write)", file=stdout)

    return 0


def _cmd_remove(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    service_key = args.service
    svc = _SERVICE_BY_KEY.get(service_key)
    if svc is None:
        print(
            f"error: unknown service '{service_key}'. "
            f"Available: {', '.join(_SERVICE_NAMES)}",
            file=stderr,
        )
        return 2

    if args.host is not None and not getattr(svc, "ACCEPTS_DICT", False):
        print(
            f"error: {service_key} はオブジェクト形式の値を受け付けないため "
            "--host は使えません。--host を外してキー全体を削除してください。",
            file=stderr,
        )
        return 2

    if args.host is not None and not args.host.strip():
        print(
            "error: --host に空文字は使えません。",
            file=stderr,
        )
        return 2

    project_dir = _project_dir()
    try:
        target = _resolve_target(project_dir, args.path, require_new=True)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code

    if _refuse_if_legacy_paths_exist("remove", target, stderr):
        return 1

    try:
        existing = _load_existing(target.path)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code

    # キー自体が無い場合と `{"github": null}` のように値が None (壊れた
    # entry) の場合を区別する (Codex R2 P2)。`.get(service_key) is None` だと
    # 両者を同一視してしまい、dispatcher がまさに deny するこの null entry
    # (dispatcher は None も未設定と同じ扱いで deny する) を remove で
    # 削除できなくなる — 直す唯一の手段である remove がここで早期 return
    # すると、設定を直せないまま `deny` が固定化される。
    if service_key not in existing:
        print(_target_note(target, project_dir), file=stdout)
        print(
            f"{service_key} は {target.path} に存在しません。何もしません。",
            file=stdout,
        )
        return 0
    existing_value = existing[service_key]

    if args.host is not None:
        if not isinstance(existing_value, dict):
            print(
                f"error: {service_key} の既存値はオブジェクトではありません "
                f"(現在: {type(existing_value).__name__})。--host は既存値が"
                "オブジェクトのときだけ使えます。--host を外してキー全体を"
                "削除してください。",
                file=stderr,
            )
            return 1
        if args.host not in existing_value:
            print(_target_note(target, project_dir), file=stdout)
            print(
                f"{service_key} に host/alias '{args.host}' は存在しません。"
                "何もしません。",
                file=stdout,
            )
            return 0
        removed_value = existing_value[args.host]
        remaining = {k: v for k, v in existing_value.items() if k != args.host}
        shape_error: str | None = None
        if remaining:
            # 最後の 1 つでなくても、残り dict がこの service にとって
            # 「使える値が無い」形になっていることがある (D13): 例えば gcloud
            # の {"project":"p","region":"x"} から project を消すと
            # {"region":"x"} が残るが、region は DICT_ALLOWED_KEYS 外で
            # verify() が読まないため project も account も無いとして deny
            # する。migrate の取り込み判定と同じ lenient 検証
            # (strict_keys=False) で残り dict を再検証し、拒否されるなら
            # 最後の 1 つを消したときと同じくキーごと削除する — 空 dict を
            # 残さないのと同じ理由 (書込は成功するのに verify() が
            # fail-closed deny する状態を作らない)。
            shape_error = _validate_entry_shape(svc, remaining, strict_keys=False)
        drop_whole_key = not remaining or shape_error is not None
    else:
        removed_value = existing_value
        remaining = {}
        drop_whole_key = True
        shape_error = None

    print(_target_note(target, project_dir), file=stdout)
    print(f"=== changes to {target.path} ===", file=stdout)
    if args.host is not None and not drop_whole_key:
        _print_change_line(
            "- remove", f"{service_key}[{args.host}]", removed_value,
            args.show_values, stdout,
        )
    elif args.host is not None and shape_error is not None:
        # 残り dict はあるが、この service にとって使える値が無い形になった
        # (上のコメント参照)。空 dict を残さないのと同じ理由でキー全体を
        # 削除し、なぜ消したかを validator のメッセージでそのまま明示する
        # (D3: このメッセージはキー名・型名のみを含み値そのものは含まない)。
        _print_change_line(
            "- remove (残りの entry に使える値が無いため "
            f"{service_key} をキーごと削除します。理由: {shape_error})",
            f"{service_key}[{args.host}]", existing_value,
            args.show_values, stdout,
        )
    elif args.host is not None:
        # 最後の 1 つの host/alias を消す = キー自体を残す意味が無い
        # (dict が空になると github/firebase/gcloud の verify() は
        # 「オブジェクトが空です」で永久 deny になるため、空 dict を残さず
        # 未記載 = 「キーがありません」の設定誘導 deny に戻す。dispatcher は
        # 未記載サービスも fail-closed で deny するが、「オブジェクトが
        # 空です」より案内が明確)。
        _print_change_line(
            "- remove (最後の host/alias のためキー全体を削除)",
            f"{service_key}[{args.host}]", existing_value,
            args.show_values, stdout,
        )
    else:
        _print_change_line(
            "- remove", service_key, existing_value, args.show_values, stdout
        )

    if args.commit:
        updated = dict(existing)
        if drop_whole_key:
            updated.pop(service_key, None)
        else:
            updated[service_key] = remaining
        try:
            _write_json(target.path, updated)
        except OSError as e:
            print(f"error: 書き込みに失敗しました: {e}", file=stderr)
            return 1
        print(f"\nwritten: {target.path}", file=stdout)
        _ensure_project_claude_md(target, stdout)
        _ensure_gitignore_entry(target, stdout)
    else:
        print("\n(dry-run; pass --commit to write)", file=stdout)

    return 0


def _entries_equal(expected: Any, current: Any) -> bool:
    if expected == current:
        return True
    if isinstance(expected, str) and isinstance(current, dict):
        # services/github.py::verify と整合: scalar expected の場合は
        # multi-host current の最初のホスト (= next(iter(active.values())))
        # のみと比較する。show と verify (実 hook) で挙動を一致させ、
        # multi-host で show が [match] と表示するのに hook 側で deny される
        # 乖離を防ぐ。
        return next(iter(current.values()), None) == expected
    if isinstance(expected, dict) and isinstance(current, str):
        # Firebase の alias map (例: {"default":"p1","prod":"p2"}) に対し、
        # CLI 由来の current が scalar (アクティブ project ID) のとき、
        # map の任意 value に一致すれば match (firebase.verify と同じ意味論)
        return any(v == current for v in expected.values())
    if isinstance(expected, dict) and isinstance(current, dict):
        return all(current.get(k) == v for k, v in expected.items())
    return False


def _migrate_keep_new_without_loss(service, new_val: Any, old_val: Any) -> bool:
    """migrate で new_val (書込先に残る側) を採用しても old_val の情報を
    失わないときだけ True を返す (True なら conflict にせず new を維持)。

    `_entries_equal` は「CLI 実測値 (current) が accounts.local.json の期待値
    (expected) を満たすか」を判定する述語で、意図的に非対称 (dict の余剰
    キーを無視する) — アカウント一致の判定としては正しいが、ここで必要な
    「old 側の値を切り捨てて良いか」の判定にそのまま使うと**情報が失われる
    方向**に倒れる。実例 (内部バックログ: advisor レビューで検出):
    new="USER" (scalar) / old={"github.com":"USER","ghe.example.com":
    "example-org"} (multi-host dict) で `_entries_equal("USER", old)` は
    `next(iter(old.values()))` (= "USER") だけを見て True を返すため、
    `ghe.example.com` の値がconflict 検出も警告もなく消えていた。

    ここでは「old の持つ情報が new に全て含まれているか」だけを見る
    (どちらの型が expected/current かという役割を持たない対称寄りの判定)。
    old が new に無い情報を 1 つでも持っていれば False (conflict のまま)。

    **scalar と dict の混在は値だけでは比較できない** (D11 / Codex R1 P1)。
    scalar 期待値と dict 期待値は「どの host/alias/フィールドを照合するか」と
    いう制約が違うので、値が一致しても同じ制約とは限らない — 例えば github の
    scalar "USER" は active な状態に応じた 1 ホストを照合するが
    `{"ghe.example.com":"USER"}` は名指しの GHE ホストを照合する。値の一致だけで
    非損失と判定すると、GHE の制約を conflict 検出も警告もなく捨てていた。
    そこで service が宣言する `SCALAR_EQUIVALENT_DICT_KEY`
    (= scalar 期待値と等価になる dict キー。services/__init__.py 参照) を使う:

    - scalar new / dict old: 非損失 ⇔ `old == {SCALAR_KEY: new}`
      (キーがそれ 1 つだけで値も一致)
    - dict new / scalar old: 非損失 ⇔ `new.get(SCALAR_KEY) == old`
      (old の制約が new にそのまま含まれる)
    - キー未宣言の service と上に当てはまらない組は conflict。
      自動で畳み込まず、利用者が両側を見て選ぶ方が安全側に倒れる

    現状キーを宣言しているのは **gcloud (`"project"`) だけ**。`github` は
    Codex R4 P1 で宣言を撤回した — scalar 分岐の照合先が「github.com が active
    ならそれ、無ければ最初の active host」と**実行時の状態で変わる**ため、
    どの静的 hostname とも等価にならない (ghe だけが active なら scalar は
    allow・`{"github.com":...}` は deny)。`firebase` も未宣言 (alias は
    verify() の verdict に効かない一方、畳み込むと `firebase use <alias>` の
    自己回復案内が読む情報が消える)。よって github/firebase の scalar↔dict は
    **両方向とも** conflict になる。
    """
    if new_val == old_val:
        return True
    scalar_key = getattr(service, "SCALAR_EQUIVALENT_DICT_KEY", None)
    if isinstance(new_val, str) and isinstance(old_val, dict):
        # scalar 期待値が照合するのは SCALAR_EQUIVALENT_DICT_KEY の 1 か所だけ。
        # old がそれ以外のキーを 1 つでも持てば、そのキーの制約は new では
        # 失われる (別 host の単一 dict も「照合先が違う」ので非損失ではない)。
        if scalar_key is None:
            return False
        return old_val == {scalar_key: new_val}
    if isinstance(new_val, dict) and isinstance(old_val, str):
        # old (scalar) が課していた制約は SCALAR_EQUIVALENT_DICT_KEY の照合。
        # new の同じキーが同じ値を持つときだけ、その制約が引き継がれる
        # (別キーに同じ値があっても照合先が違うので引き継ぎにならない)。
        if scalar_key is None:
            return False
        return new_val.get(scalar_key) == old_val
    if isinstance(new_val, dict) and isinstance(old_val, dict):
        # old の全キーが new に同じ値で存在するかどうかだけを見る
        # (new 側の余剰キーは old 由来ではないのでここでは無関係)。
        # **キーの存在を先に要求する** (Codex R4 P2): `.get()` だけで比較すると
        # 「new にキーが無い」と「new にキーがあり値が null」が両方 None になり、
        # old が `{"ghe.example.com": null}` のような壊れた値を持つとき
        # (new がその host を省略していても) 非損失と誤判定していた。old 側の
        # 値は addition ではないので `_validate_entry_shape` の検査も通らず、
        # host entry が黙って失われたまま旧ファイル削除まで案内していた。
        return all(k in new_val and new_val[k] == v for k, v in old_val.items())
    return False


def _cmd_show(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    project_dir = _project_dir()
    # show は読み取り専用なので旧パスの内容も直接見せてよい (require_new=False)。
    try:
        target = _resolve_target(project_dir, args.path, require_new=False)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code

    if target.origin == "explicit":
        found = [(target.kind, target.path)] if target.path.is_file() else []
    else:
        found = paths.discover_all_accounts_files(str(target.anchor))

    if not found:
        print(_target_note(target, project_dir), file=stdout)
        print(f"no accounts.local.json found at {target.path}", file=stdout)
        print(
            "run `accounts_builder.py init --service <name> --commit` to create one.",
            file=stdout,
        )
        return 0

    if len(found) >= 2:
        print(
            "error: 複数のパスに accounts.local.json が存在します (fail-closed).",
            file=stderr,
        )
        for kind, path in found:
            print(f"  - {path} ({kind})", file=stderr)
        print(
            "run `accounts_builder.py migrate --commit` to integrate.",
            file=stderr,
        )
        return 1

    kind, path = found[0]
    try:
        existing = _load_existing(path)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code

    print(_target_note(target, project_dir), file=stdout)
    print(f"=== {path} ({kind}) ===", file=stdout)
    if not existing:
        print("(empty)", file=stdout)
        return 0

    services_filter = [args.service] if args.service else None

    for key in sorted(existing.keys()):
        if services_filter and key not in services_filter:
            continue
        expected = existing[key]
        svc = _SERVICE_BY_KEY.get(key)

        expected_display = _format_value_for_display(expected, args.show_values)
        status_marker = ""
        detail = ""

        if svc is not None:
            try:
                current = svc.get_active_account(project_dir)
            except Exception as e:  # noqa: BLE001 — CLI 失敗は握り潰す
                current = None
                detail = f" (CLI error: {e})"
            if current is None:
                status_marker = "[CLI unavailable or not logged in]"
            elif _entries_equal(expected, current):
                status_marker = "[match]"
            else:
                status_marker = "[mismatch]"
                if args.show_values:
                    detail = f" current={json.dumps(current, ensure_ascii=False)}"
        else:
            status_marker = "[unknown service]"

        print(f"{key}: {expected_display}  {status_marker}{detail}", file=stdout)

    if kind != "new":
        print("", file=stdout)
        print(
            f"warning: このファイルは {kind} パスです。"
            " migrate --commit で新パスへ統合してください。",
            file=stdout,
        )

    return 0


def _cmd_migrate(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    project_dir = _project_dir()
    try:
        target = _resolve_target(project_dir, args.path, require_new=True)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return e.exit_code
    # 統合先は「解決した階層」の新パス。祖先の旧パスを継承している状態で
    # cwd 直下に統合すると、その場で複数パス conflict を作るか、祖先の設定を
    # 覆い隠す (D14)。
    new_path = paths.accounts_file_new(str(target.anchor))
    found = paths.discover_all_accounts_files(str(target.anchor))

    print(_target_note(target, project_dir), file=stdout)

    if not found:
        print("no accounts.local.json found in any path. nothing to migrate.", file=stdout)
        return 0

    if len(found) == 1 and found[0][0] == "new":
        print(f"only new path exists; nothing to migrate:\n  {new_path}", file=stdout)
        return 0

    sources: dict[str, dict[str, Any]] = {}
    source_paths: dict[str, Path] = {}
    for kind, path in found:
        try:
            sources[kind] = _load_existing(path)
        except _BuilderError as e:
            print(f"error: {e}", file=stderr)
            return 1
        source_paths[kind] = path

    merged: dict[str, Any] = dict(sources.get("new", {}))
    additions: list[tuple[str, Any, str]] = []  # (key, value, source_kind)
    conflicts: list[tuple[str, Any, Any, str]] = []  # (key, new_val, old_val, old_kind)

    for kind in ("deprecated", "legacy"):
        if kind not in sources:
            continue
        for key, value in sources[kind].items():
            if key not in merged:
                merged[key] = value
                additions.append((key, value, kind))
            elif not _migrate_keep_new_without_loss(
                _SERVICE_BY_KEY.get(key), merged[key], value
            ):
                # new="my-project" (scalar) / old={"project":"my-project"}
                # (gcloud) のように、old の情報が new に全て含まれる場合だけ
                # conflict にせず new 側を維持する (内部バックログ: 意味的に
                # 等価な新旧値も値衝突として手動解決を要求していた不具合の修正)。
                # `_entries_equal` (show/verify 用、CLI 実測値との一致判定) は
                # 非対称 (dict の余剰キーを無視する) なので流用せず、
                # `_migrate_keep_new_without_loss` で情報欠落の有無を直接見る —
                # old が multi-host/multi-alias dict で new (scalar) に無い
                # 値を持つ場合は、conflict のまま手動解決に落とす。
                # service を渡すのは scalar↔dict の等価性が service ごとの
                # 意味論 (SCALAR_EQUIVALENT_DICT_KEY) に依存するため。未知キーは
                # None が渡り、cross-type は常に conflict になる (安全側)。
                conflicts.append((key, merged[key], value, kind))

    if conflicts:
        print(
            "error: 同一キーで値が衝突しています (自動マージは安全でないため deny):",
            file=stderr,
        )
        for key, new_val, old_val, old_kind in conflicts:
            new_display = _format_value_for_display(new_val, args.show_values)
            old_display = _format_value_for_display(old_val, args.show_values)
            print(
                f"  - {key}: new={new_display}, {old_kind}={old_display}",
                file=stderr,
            )
        print(
            "手動で正しい値に合わせてから再実行してください。",
            file=stderr,
        )
        return 1

    # additions (旧パスにしかなかったキー) だけを書込前に形チェックする。
    # merged 全体 (new 側の既存エントリを含む) を検証すると、migrate が触って
    # すらいない既存データの形が (verify() では許容されている緩い形でも) 不合格
    # というだけで、無関係な統合作業まで exit 1 にしてしまう
    # (strict_keys=False: gcloud の未知キー等、verify() が黙って無視するだけの
    # 形は migrate では許容する。型不正 (list/int/None 等)・空 dict・空文字値に
    # 加え、DICT_ALLOWED_KEYS 宣言時は許可キーに使える値が 1 つも無い場合も
    # 拒否する — 詳細は `_validate_entry_shape` の docstring (D9/D10/D12) 参照)。
    # 内部バックログ: 旧ファイルの値型を検証せず list 等がそのまま merged に入り、
    # 後で dispatcher が deny する (書込時ではなく実行時に初めて発覚する) 不具合の修正。
    invalid: list[tuple[str, str]] = []
    for key, value, _kind in additions:
        svc = _SERVICE_BY_KEY.get(key)
        if svc is None:
            continue
        reason = _validate_entry_shape(svc, value, strict_keys=False)
        if reason:
            invalid.append((key, reason))
    if invalid:
        print(
            "error: 旧パスから取り込む値の形式が不正です (書き込みを中止します):",
            file=stderr,
        )
        for _key, reason in invalid:
            print(f"  - {reason}", file=stderr)
        print(
            "旧ファイルの該当キーを手動で修正するか削除してから再実行してください。",
            file=stderr,
        )
        return 1

    print(f"=== migrate to {new_path} ===", file=stdout)
    existing_new = sources.get("new", {})
    for key in sorted(merged.keys()):
        in_new = key in existing_new
        if in_new:
            _print_change_line("= unchanged", key, merged[key], args.show_values, stdout)
        else:
            source_kind = next((k for k, v, kk in additions if k == key), "old")
            matching = [(v, kk) for (k2, v, kk) in additions if k2 == key]
            if matching:
                source_kind = matching[0][1]
            _print_change_line(
                f"+ merged from {source_kind}",
                key,
                merged[key],
                args.show_values,
                stdout,
            )

    if args.commit:
        try:
            _write_json(new_path, merged)
        except OSError as e:
            print(f"error: 書き込みに失敗しました: {e}", file=stderr)
            return 1
        print(f"\nwritten: {new_path}", file=stdout)
        written = _Target(
            path=new_path, anchor=target.anchor, kind="new", origin=target.origin
        )
        _ensure_project_claude_md(written, stdout)
        _ensure_gitignore_entry(written, stdout)
        retained = [
            (kind, path)
            for kind, path in source_paths.items()
            if kind != "new"
        ]
        if retained:
            print(
                "\n旧パスは保持されています (安全のため自動削除しません)。"
                "不要なら手動削除してください:",
                file=stdout,
            )
            for _kind, path in retained:
                print(f"  rm {path}", file=stdout)
    else:
        print("\n(dry-run; pass --commit to write)", file=stdout)

    return 0


_PATH_HELP = (
    "対象ファイルを明示指定して 3-tier lookup / 親ディレクトリ遡及を"
    "スキップする (worktree 専用設定を意図的に作る場合の escape hatch)。"
    "dispatcher が読む配置 (.claude/verify-cloud-account/accounts.local.json 等) "
    "のみ受け付ける"
)


def _add_path_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", default=None, metavar="FILE", help=_PATH_HELP)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accounts_builder",
        description="accounts.local.json の唯一の書込経路 (D1-D14).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="新規 service entry を追加")
    p_init.add_argument(
        "--service",
        required=True,
        choices=_SERVICE_NAMES,
        help="対象サービス",
    )
    p_init.add_argument(
        "--value",
        default=None,
        help=(
            "値を明示指定 (省略時は CLI から suggest_accounts_entry() で取得)。"
            "JSON object (例: '{\"project\":\"p\",\"account\":\"a\"}') は dict "
            "として保存、それ以外 (不正な JSON や dict 以外の JSON 型) は"
            "生文字列として保存する"
        ),
    )
    mx_init = p_init.add_mutually_exclusive_group()
    mx_init.add_argument("--dry-run", action="store_true")
    mx_init.add_argument("--commit", action="store_true")
    _add_path_arg(p_init)
    p_init.add_argument(
        "--show-values",
        action="store_true",
        help="stdout に値を露出する (デフォルトは隠蔽)",
    )

    p_show = sub.add_parser("show", help="現在の accounts.local.json を表示")
    p_show.add_argument(
        "--service",
        default=None,
        choices=_SERVICE_NAMES,
        help="対象サービスで絞り込む (省略時は全件)",
    )
    _add_path_arg(p_show)
    p_show.add_argument("--show-values", action="store_true")

    p_migrate = sub.add_parser("migrate", help="旧パスから新パスへ統合")
    mx_migrate = p_migrate.add_mutually_exclusive_group()
    mx_migrate.add_argument("--dry-run", action="store_true")
    mx_migrate.add_argument("--commit", action="store_true")
    _add_path_arg(p_migrate)
    p_migrate.add_argument("--show-values", action="store_true")

    p_set = sub.add_parser(
        "set", help="既存 service entry を更新 (無ければ新規追加)"
    )
    p_set.add_argument(
        "--service",
        required=True,
        choices=_SERVICE_NAMES,
        help="対象サービス",
    )
    mx_set_value = p_set.add_mutually_exclusive_group(required=True)
    mx_set_value.add_argument(
        "--value",
        default=None,
        help=(
            "新しい値を明示指定。JSON object は dict として保存、それ以外は"
            "生文字列として保存する (init と同じ解釈)"
        ),
    )
    mx_set_value.add_argument(
        "--from-cli",
        action="store_true",
        help="CLI から現在値を取得して使用する (suggest_accounts_entry())",
    )
    p_set.add_argument(
        "--host",
        default=None,
        help=(
            "dict 値の特定キー (GitHub の hostname / Firebase の alias 等) "
            "だけを追加・上書きする。省略時はキー全体を置き換える"
        ),
    )
    mx_set = p_set.add_mutually_exclusive_group()
    mx_set.add_argument("--dry-run", action="store_true")
    mx_set.add_argument("--commit", action="store_true")
    _add_path_arg(p_set)
    p_set.add_argument(
        "--show-values",
        action="store_true",
        help="stdout に値を露出する (デフォルトは隠蔽)",
    )

    p_remove = sub.add_parser(
        "remove", help="既存 service entry (または dict の特定キー) を削除"
    )
    p_remove.add_argument(
        "--service",
        required=True,
        choices=_SERVICE_NAMES,
        help="対象サービス",
    )
    p_remove.add_argument(
        "--host",
        default=None,
        help=(
            "dict 値の特定キー (GitHub の hostname / Firebase の alias 等) "
            "だけを削除する。省略時はキー全体を削除する"
        ),
    )
    mx_remove = p_remove.add_mutually_exclusive_group()
    mx_remove.add_argument("--dry-run", action="store_true")
    mx_remove.add_argument("--commit", action="store_true")
    _add_path_arg(p_remove)
    p_remove.add_argument(
        "--show-values",
        action="store_true",
        help="stdout に値を露出する (デフォルトは隠蔽)",
    )

    return parser


def main(
    argv: list[str] | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    parser = _build_parser()
    # argparse が stderr に直書きするため、stderr injection を反映するには
    # parse 中のみ sys.stderr を差し替える
    original_stderr = sys.stderr
    try:
        sys.stderr = stderr
        try:
            args = parser.parse_args(argv)
        except SystemExit as e:
            return int(e.code) if e.code is not None else 2
    finally:
        sys.stderr = original_stderr

    if args.command == "init":
        return _cmd_init(args, stdout, stderr)
    if args.command == "show":
        return _cmd_show(args, stdout, stderr)
    if args.command == "migrate":
        return _cmd_migrate(args, stdout, stderr)
    if args.command == "set":
        return _cmd_set(args, stdout, stderr)
    if args.command == "remove":
        return _cmd_remove(args, stdout, stderr)

    parser.print_help(file=stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
