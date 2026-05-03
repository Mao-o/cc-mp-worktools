# Review Tasks (2026-05-03)

`sensitive-files-guard` v0.3.4 のレビュー結果。新規セッションから順に拾って
着手できる粒度で書いた。各タスクは「対象ファイル / 現状 / 修正方針 / 回帰検査 /
Pri / 依存」を独立して持つ。完了したら `## 進捗` に追記する。

レビュー範囲: `hooks/redact-sensitive-reads/handlers/`,
`hooks/redact-sensitive-reads/core/`, `hooks/check-sensitive-files/`,
`docs/`, `README.md`, `CLAUDE.md`。

## 着手前提

- Python 標準ライブラリのみで完結する設計方針 (CLAUDE.md, README.md) は
  **タスク M / B 群を除き維持する**。
- 既存テスト 502 件 (redact 411 + check 27 + その他) は **挙動仕様** として扱う。
  メッセージ文言を変えるタスクはアサーション側も同期更新が必要。
- `permissionDecisionReason` のハード上限 3KB (`core/output.py::MAX_REASON_BYTES`)。
  メッセージ拡充するタスクは byte 数を確認すること。

---

## H (High) — Bug 級・即修正

### H1. Bash deny reason に operand 名が含まれていない

- **対象**: `hooks/redact-sensitive-reads/handlers/bash_handler.py:332-339`
- **現状**:
  ```python
  if _operand_is_sensitive(p, envelope.get("cwd", ""), rules):
      L.log_info("bash_classify", f"match:{first}")
      return output.make_deny(
          f"Bash コマンド ({first}) の operand に機密パターンに一致する "
          "ファイルが含まれています。処理内容に関わらず値が LLM コンテキスト "
          "に露出する可能性があるため block します。許可したい場合は "
          "patterns.local.txt に `!<basename>` を追加してください。"
      )
  ```
  `p` (引っかかった operand) が文言に入っていない。glob 側
  (`bash_handler.py:323-329`) は `({p})` を含めているのに literal 側だけ抜けている。
  LLM は `cat foo bar .env baz` のうちどれが NG か分からず、推奨される
  `!<basename>` 追加も具体化できない。
- **修正方針**:
  1. `f"...operand ({p}) に機密..."` のように `p` を埋め込む。
  2. ついでに `<basename>` プレースホルダを実 basename
     (`os.path.basename(p)` など) に展開して、コピペで `!<name>` が完成する形にする
     (タスク H3 と同期)。
- **回帰検査**:
  - `tests/test_bash_handler.py` の `TestDenyFixed` 系に
    `assertIn(".env", reason)` を追加。
  - 既存の decision-only assertion は通過したまま。
- **Pri**: High
- **依存**: H3 (basename 展開) を同時にやるのが効率的。

### H2. メッセージのトーン・語彙の不統一を解消

- **対象**: 全 handler の reason 文言。代表箇所:
  - `bash_handler.py:262/270/327/337` 「block します」
  - `edit_handler.py:99-101` 「block されました」
  - `__main__.py:111/120/121` 「deny します」
  - `read_handler.py:39/52/66/72/79/89/102` 「一時停止します」
- **現状**: `block / deny / 一時停止` が混在し、能動/受動も揺れる。
- **修正方針**:
  - 動詞を 3 語に固定:
    - `make_deny` 系 → 「拒否しました」(または「block しました」)
    - `ask_or_deny` 系 → 「安全側で確認を求めます」
    - `ask_or_allow` 系 → 「確認を挟みます (auto/bypass/plan は通過)」
  - reason builder (タスク M1) を作る時に語彙ルールを 1 箇所に固定。
  - 「英語混入」は技術用語 (block / deny / hook / operand / glob) のみ許容、
    動詞は日本語に揃える。
- **回帰検査**: 文言 substring 比較しているテストを grep して同期更新。
- **Pri**: High (M1 と同時)

### H3. `!<basename>` 案内に実 basename を埋め込む

- **対象**: `bash_handler.py:328/338`, `edit_handler.py:103-104` の
  `"許可したい場合は patterns.local.txt に \`!<basename>\` を追加してください"`。
- **現状**: プレースホルダ `<basename>` がベタ書きで、LLM がコピペできない。
- **修正方針**:
  - 実 basename (`os.path.basename(p)`) を展開した文字列を渡す。
  - パスが新パス (`~/.claude/sensitive-files-guard/patterns.local.txt`) と
    旧パスの両方ある旨も短く触れる (CLAUDE.md `## patterns.local.txt の 2-tier
    lookup` 参照)。
- **回帰検査**:
  - `assertIn("!.env", reason)` のような basename を含むアサーションを追加。
- **Pri**: High
- **依存**: H1 と同時。

---

## M (Medium) — 品質改善 (1-2 セッション規模)

### M1. deny / ask reason builder を `core/messages.py` に集約

- **対象**: 新規ファイル `hooks/redact-sensitive-reads/core/messages.py`、
  既存 `bash_handler.py` / `edit_handler.py` / `read_handler.py` のメッセージ箇所。
- **現状**:
  - `edit_handler._build_deny_reason` (`edit_handler.py:92-126`) は header /
    hint / extra_note / suggested_keys を統合する良い構造。
  - bash_handler は f-string がインライン散在 (4 箇所)。
  - read_handler は ad-hoc な日本語文。
  - 「`!<basename>` を追加してください」誘導文が複数箇所に重複。
- **修正方針**:
  - `core/messages.py` に以下の builder を切り出す:
    - `bash_deny(first_token, operand, kind, basename) -> str`
      - `kind` は `"literal" | "glob" | "input_redirect" | "input_redirect_glob"`
    - `edit_deny(tool_label, basename, new_keys=None, extra_note="") -> str`
      - 既存 `_build_deny_reason` をそのまま移設
    - `read_ask(reason_kind: str, basename: str) -> str`
      - `reason_kind` は `"symlink" | "special" | "io_error" | "normalize_failed"
        | "redaction_failed" | "open_failed" | "patterns_unavailable"`
    - `policy_unavailable(severity: str) -> str`
      - severity は `"deny"` (Bash) / `"pause"` (Read/Edit)
  - 各 handler は builder のみ呼ぶ。reason 文字列を直接組み立てない。
  - 動詞ルール (タスク H2) はこのモジュールでのみ定義。
- **回帰検査**:
  - 既存の文言 assertion を builder 関数の戻り値に合わせる。
  - 新規テスト `tests/test_messages.py` で各 builder の最低 1 ケース。
- **Pri**: Medium
- **依存**: H1 / H2 / H3 を取り込んで一気に直せる。

### M2. Read handler の「続行しますか？」を LLM 向け文に書き換え

- **対象**: `read_handler.py:62-80` の 4 つの `ask_or_deny`
- **現状**: 「続行しますか？」(symlink/special) は人間 UI に向けた文言。
  `permissionDecisionReason` は LLM が読むため違和感がある。
- **修正方針**:
  - 「続行しますか？」を削除し、「**この Read を再試行する場合は理由をユーザーに
    確認してください**」のように LLM の next action を明示する。
  - `__main__.py:111` 「管理者に連絡してください」、`read_handler.py:39`
    「hook 管理者に連絡してください」も同様に LLM が取れる action に書き換える
    (例: 「`patterns.txt` の配置を確認してください」など具体化)。
- **回帰検査**: 文言比較テスト同期。
- **Pri**: Medium
- **依存**: M1 (builder 経由で行う) があるとまとめてやれる。

### M3. patterns_unavailable のメッセージを統合

- **対象**:
  - `bash_handler.py:374-378` (Bash 用 deny 固定文)
  - `edit_handler.py:147-150` (Edit 用 ask_or_deny 文)
  - `read_handler.py:38-41` (Read 用 ask_or_deny 文)
- **現状**: 3 箇所に類似の独立文言。
- **修正方針**: M1 の `policy_unavailable(severity)` builder に集約。Bash は
  severity=`"deny"`、Read/Edit は severity=`"pause"`。
- **回帰検査**: M1 と同じ。
- **Pri**: Medium
- **依存**: M1。

### M4. Bash deny reason を `<SFG_DENY>` 構造化包装する

- **対象**: bash_handler / edit_handler の make_deny 経由 reason 全部。
  Read 側は既に `<DATA untrusted>` 包装済み (`engine.build_reason`)。
- **現状**: Bash/Edit/Write は plain text reason。LLM 側は文言 substring に
  依存して block 種別を識別するしかない。
- **修正方針**:
  - 例:
    ```
    <SFG_DENY tool="Bash" reason="sensitive_operand_match" guard="sfg-v1">
    matched_operand: .env
    first_token: cat
    suggestion: add `!.env` to ~/.claude/sensitive-files-guard/patterns.local.txt
    note: 値が LLM コンテキストに露出する可能性があるため block しました。
    </SFG_DENY>
    ```
  - スキーマ (`reason` の取り得る値) を docs/PATTERNS.md か新規 docs に列挙する。
  - `core/messages.py` の builder で生成 (M1 と一体化)。
  - `escape_data_tag` (sanitize.py) を流用して外殻破壊を防ぐ。
- **回帰検査**:
  - 既存テストの substring assertion (`"block します"` 等) は構造化後の文言に
    合わせて更新。
  - 新規 `tests/test_sfg_deny_envelope.py` で各 reason 値が出ることを保証。
- **Pri**: Medium
- **設計判断**: スキーマを切る前にユーザー確認するのが良い (タグ名 / 属性名 /
  reason 値の列挙)。M1 着手前に schema 案を 1 セッションで詰める。

### M5. 入力リダイレクト reason に検出形式を含める

- **対象**: `bash_handler.py:259-272`, `handlers/bash/redirects.py::_scan_input_redirect_targets_chars`
- **現状**: 「Bash 入力リダイレクト先 (.env) が機密パターンに一致します」では
  どの形式 (`< .env` / `0< .env` / `cat<.env`) で書かれていたか消える。
- **修正方針**:
  - `_scan_input_redirect_targets_chars` の戻り値を
    `[(target, form)]` (form: `"bare" | "fd_prefixed" | "no_space" | "quoted"`)
    に変更。
  - reason に `form=fd_prefixed` のような短いタグを足す。
  - パーサ拡張は B (bashlex 移行) と関係するので、B 着手するなら一緒にやる。
    bashlex 採用なら `RedirectNode` の attribute から取得できる。
- **回帰検査**: 既存 redirect テスト (`tests/test_input_redirect.py`) に form
  を assert する 1 ケース追加。
- **Pri**: Medium
- **依存**: B 着手なら B 内で吸収。単独実装も可。

---

## L (Low) — 小さい品質改善

### L1. `core/logging.py` の detail に文字種 assertion を入れる

- **対象**: `hooks/redact-sensitive-reads/core/logging.py:20-47`
  (`log_error`, `log_info`)
- **現状**:
  - `bash_handler.py:311` `f"shell_keyword_lenient:{first}"`
  - `bash_handler.py:323/333` `f"glob_match:{first}"` / `f"match:{first}"`
  - `bash_handler.py:408` `f"shlex_fail:{type(e).__name__}"`
  などで token / 例外型名を log detail に流している。設計コメントは「呼出側
  責任」だが、コード側で保証はない。
- **修正方針**:
  - `log_info` / `log_error` の `detail` に `re.match(r"^[A-Za-z0-9_:!.\-]{0,64}$", detail)`
    を assert (CI / dev 時のみ; 本番は silent drop)。
  - 違反したら `_BAD` のような placeholder に置換してログする。
- **回帰検査**: 既存 detail がパターンに合致することを単体で確認。
- **Pri**: Low

### L2. `_extract_dotenv_keys` の bare except を分類

- **対象**: `hooks/redact-sensitive-reads/handlers/edit_handler.py:85-88`
- **現状**:
  ```python
  try:
      info = redact_dotenv(text)
  except Exception:
      return []
  ```
- **修正方針**:
  - `except (ValueError, UnicodeDecodeError, AttributeError) as e:` のように
    狭める。
  - `L.log_info("dotenv_parse_failed", type(e).__name__)` で種別だけ残す。
- **回帰検査**: parse 失敗 case のテストを追加 (バイナリ風 / 非 ASCII /
  異形フォーマット)。
- **Pri**: Low

### L3. `hookSpecificOutput` を `TypedDict` 化

- **対象**: `hooks/redact-sensitive-reads/core/output.py:68-127`
- **現状**: dict 直手書き。schema 変更時に検出が遅れる。
- **修正方針**:
  ```python
  from typing import Literal, TypedDict
  class HookSpecificOutput(TypedDict, total=False):
      hookEventName: Literal["PreToolUse"]
      permissionDecision: Literal["deny", "ask"]
      permissionDecisionReason: str
  class HookResponse(TypedDict, total=False):
      hookSpecificOutput: HookSpecificOutput
  ```
  - mypy / pyright を入れていないなら導入は不要、型注釈だけで OK。
- **Pri**: Low

### L4. `output.is_allow(r)` 述語を導入し、テスト変更耐性を上げる

- **対象**: `hooks/redact-sensitive-reads/core/output.py`、テスト各所
  (`tests/test_*.py` の `assertIsNone(_decision(r))`)
- **現状**: `make_allow()` が `{}` を返す前提のテストが多い。Phase 0 spec が
  `permissionDecision: "allow"` 明示出力に変わると全部死ぬ。
- **修正方針**:
  - `output.is_allow(r) -> bool` を追加 (`"deny"/"ask"` でない、を判定)。
  - テストの `_decision(r)` 呼び出し箇所を `output.is_allow(r)` に置換。
- **Pri**: Low

### L5. テストの reason 文言検証を追加

- **対象**: `hooks/redact-sensitive-reads/tests/test_bash_handler.py` 系
- **現状**: decision (`deny`/`ask`/None) のみテスト、reason 文言は未テスト。
  M1〜M4 で文言を変えるとサイレント regression する。
- **修正方針**:
  - 各 deny / ask テストに最低 `assertIn(<key fragment>, reason)` を 1 行追加。
  - M1 の builder 単体テスト (`tests/test_messages.py`) でメッセージ生成を直接
    テストし、handler 側は呼出のみ確認、の 2 段構造にする。
- **Pri**: Low (M1 と同時)

---

## B (Big) — Bash 解析パッケージの採否 (別セッションで議論)

ユーザー方針: **MIT ライセンス必須。今回は採否を決めず、別セッションで議論する**。

このセクションは新規セッションでの議論再開のための論点整理。

### 現状の手書きパーサ規模

| ファイル | 概算行数 | 担当 |
|---|---|---|
| `handlers/bash/redirects.py::_scan_input_redirect_targets_chars` | ~250 | char-level redirect parser ([[]]/(())/`<(`/`<<`/`<<<`/`<&`/quote/escape/コメント) |
| `handlers/bash/redirects.py::_consume_redirect_target` | ~65 | quote-aware word consumer |
| `handlers/bash/segmentation.py::_split_command_on_operators` | ~60 | quote-aware segment 分割 |
| `handlers/bash/operand_lexer.py` | ~130 | glob / literalize / path 候補抽出 |
| `handlers/bash_handler.py::_normalize_segment_prefix` | ~75 | env/command/builtin 等の透過 prefix 剥がし |
| **合計** | **~600 行** | |

`redirects.py:191-220` 等にセキュリティ regression パッチの注釈が積み重なっており、
bash 文法サポートが増えるたびに読み込みコストが上がる構造になっている。

### 候補パッケージとライセンス確認 (新規セッションで再確認のこと)

| パッケージ | 推定ライセンス | 評価 | 注意点 |
|---|---|---|---|
| `bashlex` | **GPL-3.0** (PyPI 表示) | ◎ 機能カバレッジ | **GPL なら採用不可**。MIT 互換でない。要再確認 |
| `mvdan/sh` (Go) | **BSD-3** | × | Go 実装。Python から subprocess 経由は破綻 |
| `tree-sitter-bash` | **MIT** | △ | Python binding (`tree-sitter`) の native build 必要、配布が複雑 |
| `pyparsing` で bash subset 自作 | MIT (pyparsing) | △ | 結局 grammar 定義を書くので削減量限定的 |
| `lark` で bash subset 自作 | MIT | △ | 同上 |
| `shlex` (標準) | PSF | △ | 既使用。POSIX shell 演算子非対応で前後の手書き必須 |

**最重要確認事項**: `bashlex` のライセンスを最新版で再確認。GPL なら本 plugin
(MIT を想定する worktools marketplace) には load 不可で、別の選択肢に移る必要がある。

### bashlex 採用時の置換イメージ (もしライセンス問題なければ)

```python
import bashlex
trees = bashlex.parse("cat .env && echo done")
# tree.kind == 'list', tree.parts == [CommandNode, OperatorNode('&&'), CommandNode]
# 各 CommandNode.parts: [WordNode('cat'), WordNode('.env')]
# RedirectNode.input == '<' / '<<' / '<<<' / '<&'、output が target
```

- `_split_command_on_operators` → `bashlex.parse` の `ListNode`/`OperatorNode` walk
- `_scan_input_redirect_targets_chars` → `RedirectNode(input='<')` を walk
- `_consume_redirect_target` → 不要 (`WordNode.word` が quote 解決済み)
- `_normalize_segment_prefix` の env prefix → `AssignmentNode`
- `_has_hard_stop` → 不要 (パースで完全に文法理解、失敗時のみ ask_or_allow)

### tree-sitter-bash 採用時の論点

- ライセンスは MIT (Cargo / npm 公開分)。
- Python binding (`tree-sitter` PyPI パッケージ) は MIT。grammar 自体も MIT。
- ただし native build 配布が `pip install tree-sitter tree-sitter-bash` でも
  CI で wheel 提供されているか要確認。配布が崩れると hook 起動コストに直撃。
- vendor 配布する場合は WASM 経由 (browser-compatible build) を検討。

### 別セッションで決めること

1. `bashlex` 最新版の正確なライセンス (PyPI / source repo / setup.py) を確認
2. ライセンス OK なら vendor or pip 依存の選択
3. ライセンス NG なら tree-sitter / 自前 IR 化のどちらに行くか
4. fallback (パース失敗時の挙動) を `ask_or_allow` に固定する設計の確認
5. 既存テスト 502 件を回帰検査に流すワークフロー

---

## 進捗

### 2026-05-03: H1 / H3 / M1 完了 (0.4.1 リリースに統合済)

#### 実装

- **新規** `hooks/redact-sensitive-reads/core/messages.py`
  - `bash_deny(first_token, operand, kind)` builder
    (`kind`: `literal` / `glob` / `input_redirect` / `input_redirect_glob`)
  - `edit_deny(tool_label, basename, new_keys=None, extra_note="")` builder
  - `_exclude_hint(basename)` で `!<basename>` を実 basename に展開
- **改修** `hooks/redact-sensitive-reads/handlers/bash_handler.py`
  - `_scan_input_redirects` / `_analyze_segment` の deny 4 箇所を
    `M.bash_deny(...)` 経由に置換
  - **H1**: literal match の reason に operand `p` が含まれていなかった
    バグを修正
- **改修** `hooks/redact-sensitive-reads/handlers/edit_handler.py`
  - `_build_deny_reason` 関数と `MAX_SUGGESTED_KEYS` 定数を撤去
    (messages.py 側で kwarg デフォルトとして集約)
  - `M.edit_deny(...)` 経由に置換 (3 箇所)

#### テスト

- **新規** `tests/test_messages.py` 13 件
  - `_exclude_hint` の basename 展開と backtick sanitize
  - `bash_deny` の 4 kind それぞれ
  - `edit_deny` の minimal / dotenv keys / extra_note / 30 件超切り詰め
- **追加** `tests/test_bash_handler.py::TestDenyReasonContent` 4 件
  - literal / subdir literal / glob / input_redirect の各経路で reason に
    operand と `!<basename>` が含まれることを保証
- **追加** `tests/test_edit_handler.py::TestDenyReasonBasename` 3 件
  - dotenv / non-dotenv / subdir それぞれの basename 展開
  - `!<basename>` プレースホルダがリテラル文字列として残らないことの確認

#### テスト結果

```
hooks/redact-sensitive-reads: Ran 528 tests in 0.129s — OK
hooks/check-sensitive-files: Ran 27 tests in 1.873s — OK
claude plugin validate: ✔ Validation passed
```

### 2026-05-04: M4 完了 + 0.4.2 リリース

#### 実装

- **拡張** `redaction/sanitize.py`:
  - `escape_xml_tag(text, tag_name)` を追加 (一般化版)
  - `escape_data_tag(text)` を `escape_xml_tag(text, "DATA")` の薄い
    wrapper として残置 (後方互換)
- **拡張** `core/messages.py`:
  - `_wrap_sfg_deny(tool, reason, body_lines)` ヘルパーを新設
  - `_SFG_GUARD = "sfg-v1"` (Read 側 `<DATA>` と統一)
  - `EditDenyKind` 型 (`sensitive_path` / `sensitive_path_symlink` /
    `sensitive_path_special`) を追加
  - `bash_deny` / `edit_deny` / `policy_unavailable("deny")` を構造化包装
    対応に書き換え
  - `edit_deny` に `kind` キーワード引数を追加
- **改修** `handlers/edit_handler.py`: symlink / special 経由の `make_deny`
  呼び出しに `kind="sensitive_path_symlink"` / `kind="sensitive_path_special"`
  を渡す
- **CHANGELOG / plugin.json / CLAUDE.md**: 0.4.2 として bump

#### M4 で確定した schema

```
<SFG_DENY tool="<Bash|Edit|Write|MultiEdit|Hook>" reason="<kind>" guard="sfg-v1">
note: ...
matched_operand: ...   (Bash 系のみ)
first_token: ...       (Bash 系のみ)
basename: ...          (Edit 系のみ)
suggested_keys:        (edit_deny の dotenv 系)
  KEY_NAME=
suggestion_alt: ...    (任意)
extra_note: ...        (任意)
suggestion: ...        (必須)
</SFG_DENY>
```

`reason` 値: `literal` / `glob` / `input_redirect` / `input_redirect_glob`
/ `sensitive_path` / `sensitive_path_symlink` / `sensitive_path_special` /
`policy_unavailable` の **8 種類**。

#### テスト

- **追加** `tests/test_messages.py::TestSfgDenyEnvelope` 12 件
- **追加** `tests/test_sanitize.py::TestEscapeXmlTag` 7 件
- 累計 **575 + 27 = 602 件 OK**
- `claude plugin validate sensitive-files-guard` ✔
- `bash .tools/validate-all.sh` ✔ (marketplace 全体)

#### 残タスク (次リリース以降)

- **M5** (入力リダイレクト形式タグ): bashlex 採否と一緒にやるのが経済的
- **L1〜L5** (小改善): いつでも 30 分以下
- **B** (bashlex 採否): MIT ライセンス再確認の議論セッション

---

### 2026-05-04: H2 / M2 / M3 完了 (0.4.1 リリース済)

#### 実装

- **拡張** `core/messages.py`
  - 語彙ルール (block / 一時停止 / 確認を挟む) を docstring 冒頭に明文化
  - `policy_unavailable(severity, tool_label="")` (M3): Bash 用 deny / 他 pause
  - `read_ask(kind)` (M2): symlink / special / io_error / normalize_failed /
    redaction_failed / open_failed の 6 種類
  - `edit_pause(kind, tool_label)`: normalize_failed / io_error /
    parent_not_directory の 3 種類
  - `bash_lenient(kind, detail="")` (H2): hard_stop / opaque_prefix /
    residual_metachar / shell_keyword / tokenize_failed / normalize_failed の
    6 種類。共通 suffix 「判定不能のため確認を挟みます (auto / bypass / plan
    では通過)」で揃える
  - `hook_invocation_error()` / `stdin_parse_failed()` /
    `unsupported_platform()` / `handler_internal_error(tool, exc_type="")`:
    `__main__` wrapper 用
- **改修** `handlers/read_handler.py`: 7 箇所の reason を builder 経由に。
  「続行しますか？」「hook 管理者に連絡してください」を排除し、LLM が取れる
  next action 文 (「~を確認してから再試行してください」) に統一。
- **改修** `handlers/bash_handler.py`:
  - patterns_unavailable / hard_stop / opaque_prefix / residual_metachar /
    shell_keyword / tokenize_failed / normalize_failed の 7 ケースを builder
    経由に
  - 「block します」「ask します」のような揺れを排除し、共通 suffix で統一
- **改修** `handlers/edit_handler.py`: patterns_unavailable / normalize_failed
  / parent_not_directory / io_error の 4 箇所を builder 経由に。
- **改修** `__main__.py`: unsupported_platform / hook_invocation_error /
  stdin_parse_failed / handler_internal_error を builder 経由に。
  「管理者に連絡してください」「安全側で deny します」を撤去し、settings.json
  / README / `~/.claude/logs/redact-hook.log` への具体導線に置換。

#### テスト

- **追加** `tests/test_messages.py` に 27 件 (累計 40 件):
  - `TestPolicyUnavailable` (3 件): deny / pause / tool_label prefix
  - `TestReadAsk` (6 件): 各 kind と「続行しますか」非含有の検証
  - `TestEditPause` (3 件)
  - `TestBashLenient` (7 件): 共通 suffix と autonomous モード明示の検証
  - `TestHookErrorMessages` (5 件): 「管理者」非含有と具体導線の検証
  - `TestVocabularyConsistency` (3 件): 動詞ルール (block / 再試行 /
    確認を挟む) の最終ガード

#### テスト結果

```
hooks/redact-sensitive-reads: Ran 555 tests in 0.132s — OK
hooks/check-sensitive-files: Ran 27 tests in 1.899s — OK
```

#### 未実施 (新規セッションで判断)

- **CHANGELOG.md エントリ追加** + **plugin.json version bump**: H1/H3/M1 と
  H2/M2/M3 をまとめて 0.4.0 として出すか、別 minor で分けるか判断が必要。
  進捗が完結するまで保留。
- **次タスク (Pri 順)**:
  1. **M4** (`<SFG_DENY>` 構造化): タグスキーマ案を 1 セッションで詰めてから
     実装。最初に `reason` 値の列挙 (今は ``literal`` / ``glob`` /
     ``input_redirect`` / ``input_redirect_glob`` / ``policy_unavailable`` /
     ``symlink`` / ``special`` / ``io_error`` / ``normalize_failed`` /
     ``redaction_failed`` / ``open_failed`` / ``parent_not_directory`` /
     ``hard_stop`` / ``opaque_prefix`` / ``residual_metachar`` /
     ``shell_keyword`` / ``tokenize_failed``) の 17 種類が暗黙に定まっている。
  2. **M5** (入力リダイレクト形式タグ): bashlex 採否と一緒にやる方が経済的。
  3. **L1** (logging detail の文字種 assertion)
  4. **L2** (`_extract_dotenv_keys` の bare except 分類)
  5. **L3** (`hookSpecificOutput` を TypedDict 化)
  6. **L4** (`output.is_allow(r)` 述語導入)
- **B (bashlex 採否) は別セッション議論のまま据え置き**: MIT ライセンス必須の
  ため、最新 PyPI バージョンの正確なライセンスを再確認してから採否判断。

---

## 着手の推奨順序

1. **H1 + H3 + M1 を 1 PR**: operand 名漏れ修正、basename 埋め込み、
   reason builder 集約。テストの substring assertion 追加もここで。
2. **H2 + M2 + M3**: 動詞ルール統一、LLM 向け文言書き換え、policy_unavailable
   集約。M1 の builder 上で実施するので連続して取れる。
3. **M4 (構造化包装)**: schema 案を 1 セッションで詰めてから実装。
4. **L1〜L5**: いつでも。L5 は M1 と同時にやると無料。
5. **M5**: B (bashlex) と一緒にやるのが効率的。B が決まらないなら現行パーサ
   拡張で単独着手。
6. **B**: 別セッションでライセンス確認 → 採否 → 実装。最大の工数。

---

## 新規セッションでこのファイルを開いた時の手順

1. `worktools/plugins/sensitive-files-guard/docs/REVIEW_TASKS_2026-05-03.md`
   を読む (このファイル)。
2. 関連の読み込み順:
   - `CLAUDE.md` — 設計原則とハンドラ責務
   - `docs/DESIGN.md` — Phase 0 実測ログ
   - `docs/MATRIX.md` — 判定マトリクス
   - 該当 handler ファイル
3. 着手前に `## 進捗` を確認。重複作業を避ける。
4. テストを必ず通す: 各 hook ディレクトリで `python3 -m unittest discover tests`。
5. 完了したら `## 進捗` に追記し、CHANGELOG.md と plugin.json の version を bump。
