# sensitive-files-guard (実装者向けガイド)

このファイルは **plugin の保守・拡張者向け**。利用者向け概要は [README.md](./README.md)。

## 目的と非目的

### 目的

1. `Read` を `PreToolUse` で先取りし、機密パスに触れる操作を検出
2. 検出時は **実値を hook プロセスのメモリだけに留め**、モデルには
   **鍵名・順序・型・件数のみ** (= minimal info) を返す
3. `Stop` で tracked / untracked いずれの機密ファイルも検出し、
   `.gitignore` 追加 (+ tracked なら `git rm --cached`) を促す
4. 両 hook は同一 `patterns.txt` を共有 (単一ソース) + ローカル拡張点
   (`patterns.local.txt`) を提供

### 非目的

- **敵対的防御ではない**。プロンプトインジェクションや悪意のある agent 攻撃は対象外
- **完全な情報遮断ではない**。ファイル名 (basename) と鍵名は漏れる
- **`Grep` / `Glob` / `NotebookEdit` は対応外**。`Bash` / `Edit` / `Write` /
  `MultiEdit` は 0.2.0 から static 解析ベースで ask_or_deny する handler を
  追加したが、シェル動的展開 (`cat "$X"`, `bash -c`, `source .env` 間接経路)
  は fail-closed に倒す境界があり、完全な書き込み防止は Claude Code 側の
  sandbox が無いと塞ぎきれない。スコープ外として明示
- **race condition の完全排除ではない**。TOCTOU 限界あり

## ディレクトリ構成

```
sensitive-files-guard/
├── .claude-plugin/plugin.json       # version 0.2.0
├── README.md
├── CLAUDE.md
└── hooks/
    ├── hooks.json                   # PreToolUse(Read/Bash/Edit/Write) + Stop (MultiEdit は Claude Code 現行版で非搭載のため matcher 除外)
    ├── _shared/                     # 両 hook 共有ロジック (0.2.0 新設)
    │   ├── __init__.py
    │   ├── matcher.py               is_sensitive (case-insensitive + last-match-wins)
    │   └── patterns.py              _parse_patterns_text / _resolve_local_patterns_path
    ├── check-sensitive-files/       # Stop hook
    │   ├── __main__.py
    │   ├── checker.py               _shared 経由 + git ls-files --recurse-submodules
    │   ├── patterns.txt             ← 両 hook で共有するパターン
    │   └── tests/                   # unittest + conftest.py (git repo 作成 + XDG 隔離)
    └── redact-sensitive-reads/      # PreToolUse hook
        ├── __main__.py              # fail-closed wrapper + Windows SIGALRM チェック
        ├── core/
        │   ├── logging.py           秘密非混入ログ (LOG_PATH=~/.claude/logs/redact-hook.log)
        │   ├── matcher.py           _shared.matcher の互換 re-export
        │   ├── output.py            deny/ask JSON builder + ask_or_deny (bypass 対応)
        │   ├── patterns.py          _shared.patterns + Read 側の warn_callback
        │   └── safepath.py          normalize / classify / open_regular (fd) / is_regular_directory
        ├── redaction/
        │   ├── engine.py            fd 経由 redact / build_reason (guard=sfg-v1)
        │   ├── sanitize.py          鍵名・basename sanitize + escape_data_tag
        │   ├── dotenv.py            鍵名・順序・型・件数 (inline comment 対応)
        │   ├── jsonlike.py          鍵名・構造・件数、値は一律マスク
        │   ├── tomllike.py          tomllib (3.11+) / 未満は opaque
        │   ├── opaque.py            YAML/unknown/大ファイル fallback
        │   └── keyonly_scan.py      streaming 鍵名抽出 (fd 経由 scan_stream)
        ├── handlers/
        │   ├── read_handler.py      Read (fd ベース, TOCTOU 緩和)
        │   ├── bash_handler.py      Bash (static 解析, 間接経路は fail-closed)
        │   └── edit_handler.py      Edit / Write (deny 固定 + dotenv キー名ガイド付き)。MultiEdit 対応も実装済みだが現行 Claude Code で非搭載のため matcher 未登録
        └── tests/                   unittest + conftest.py + fixtures/envelopes/
```

## Phase 0 実測結果 (2026-04-11)

実測詳細は `~/shared-context/security/claude-code-pretooluse-hook-spec.md`
に恒久記録。要点のみ:

- `permissionDecisionReason` は deny 時に 1KB/8KB/32KB までモデルに完全配信される
- `systemMessage` トップレベルは **モデルに届かない** (公式 docs の誤り)。依存禁止
- `ask` reason はモデルには届かず、ユーザー UI のみ。bypass モードでは自動 allow
- envelope には `permission_mode` フィールドがあり bypass 検出に使える
- tool_input 形状は `Read:file_path` / `Bash:command,description` など

## 設計原則

1. **Fail-closed in doubt** — read 側の内部失敗は `ask` (bypass モード時は `deny`) にフォールバック。
   Stop 側は応答停止を招かないため fail-open (stderr warning + 空出力)
2. **情報量最小化** — minimal info (鍵名・順序・型・件数) のみ返却、値は bool/小整数含めて原則マスク
3. **Secrets never in logs** — path・値・展開後情報を一切記録しない
4. **Latency <100ms 目標** — timeout 2 秒、文字列処理のみ、外部コマンド呼び出しなし
5. **情報注入は `permissionDecisionReason` 一択** — systemMessage 非依存

## 判定ロジック

### Read handler

| ケース | 判定 |
|---|---|
| パターン非該当 | no-op |
| 機密 + 通常ファイル成功 | `deny` + minimal info を `permissionDecisionReason` |
| 機密 + symlink | `ask_or_deny` |
| 機密 + 特殊ファイル (FIFO/socket/device) | `ask_or_deny` |
| 機密 + 読み取り失敗 (権限/IO) | `ask_or_deny` (fail-closed) |
| redaction engine 内部例外 | `ask_or_deny` (fail-closed) |
| patterns.txt 読込失敗 | `ask_or_deny` + stderr 警告 |
| サイズ 32KB 超 | keyonly_scan で streaming 鍵名抽出 |

### Bash handler (0.2.0, deny 固定)

| ケース | 判定 |
|---|---|
| `cat .env` / `source .env` 等で機密 path 検出 | **`deny` 固定** |
| `.env.example` / 非機密 path | allow |
| 未知コマンド (`echo` `npm` `git` 等) | allow |
| 変数展開 / `$()` / heredoc / リダイレクト / 複合 | `ask_or_deny` (判定不能、fail-closed) |
| 絶対/相対パス実行 / env prefix / shell wrapper | `ask_or_deny` (判定不能、fail-closed) |
| patterns.txt 読込失敗 / shlex.split 失敗 | `ask_or_deny` (fail-closed) |

### Edit/Write handler (0.2.0, deny 固定)

MultiEdit は現行 Claude Code 非搭載のため hooks.json matcher から除外しているが、
handler 側は将来復活時用に ``tool_label="MultiEdit"`` 分岐を残している。

| ケース | 判定 |
|---|---|
| 機密 path への新規/既存 書き込み (通常ファイル) | **`deny` 固定** + dotenv ならキー名を reason に添える |
| 機密 path + symlink / special | **`deny` 固定** + 対応の extra note |
| `.env.example` 等テンプレ除外 | allow |
| 親ディレクトリが symlink / 特殊 / 不在 | `ask_or_deny` (判定不能、fail-closed) |
| patterns.txt 読込失敗 / normalize 失敗 / stat 失敗 | `ask_or_deny` (fail-closed) |

deny reason のキー名ガイド:
- dotenv 系 basename (``_detect_format(basename) == "dotenv"``) の時だけ
  ``tool_input`` からキー名抽出 (Edit=new_string / Write=content /
  MultiEdit=edits[].new_string 連結)
- 抽出結果を reason に箇条書きで添え、``.env.example`` への移行を促す
- 値そのものは一切 reason に含めない (キー名のみ、既存の minimal-info 原則と一致)

`ask_or_deny`: `permission_mode == "bypassPermissions"` なら `deny`、それ以外は `ask`。
**機密検出済み** のケースは `ask` を挟まず常に `deny` 固定 (うっかり承認防止)。

### Stop handler

| ケース | 判定 |
|---|---|
| `stop_hook_active=true` | exit 0 (ループ防止) |
| cwd が git 管理下でない | exit 0 |
| tracked でパターン一致 | `decision: block` (`.gitignore` 済みでも) |
| untracked でパターン一致 + `.gitignore` 未登録 | `decision: block` |
| patterns.txt 読込失敗 (FileNotFoundError / OSError) | exit 0 + stderr warning (fail-open) |

tracked / untracked は block reason で別セクションに分けて表示し、
それぞれ対応手順 (`git rm --cached` / `.gitignore` 追加) を添える。

## `permissionDecisionReason` フォーマット

```
<DATA untrusted="true" source="redact-hook">
NOTE: sanitized data from a sensitive file. Real values are NOT in context.
file: .env
format: dotenv
entries: 4
keys (in order):
  1. DATABASE_URL  <type=str>
  2. JWT_SECRET    <type=jwt>
  3. DEBUG         <type=bool>
  4. PORT          <type=num>
note: all values and comments removed for safety.
</DATA>
```

- `<DATA untrusted="true">` 包装でプロンプトインジェクション緩和
- path は **basename のみ** (顧客名・環境名リーク対策)
- 全体 **1-2KB 目標**、ハード上限 4KB
- Markdown 不使用

## パターンファイル仕様

### `_parse_patterns_text(text) -> list[tuple[str, bool]]`

`patterns.txt` / `patterns.local.txt` の 1 ファイル分テキストをパースする関数。
両 hook で同じ仕様 (`core/patterns.py` と `check-sensitive-files/checker.py` に
論理コピー)。

- 空行・`#` で始まる行は無視 (先頭空白 strip 後に判定)
- `!pattern` → `(pattern, True)` (exclude)
- `pattern` → `(pattern, False)` (include)
- 出現順を保持する (last-match-wins で順序が意味を持つため)

### `_resolve_local_patterns_path() -> Path`

`patterns.local.txt` のパス解決。

- `$XDG_CONFIG_HOME` があれば `$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt`
- 未設定なら `~/.config/sensitive-files-guard/patterns.local.txt`
- 返り値は実在しなくてもよい (呼出側で `FileNotFoundError` を処理)

### 評価方式: last-match-wins

- `load_patterns()` は `既定 → ローカル` の順で rules を連結する
- `is_sensitive(path, rules)` は basename を先頭から全件走査し、
  **最後にマッチしたルール**の符号 (include/exclude) で決着
  - どのルールにもマッチしない場合のみ parts (親 dir 名) を続けて評価
  - basename が exclude 決着なら parts は見ない (明示除外優先)
- gitignore の挙動と揃えた: 既定で exclude したパターンを、ユーザーが
  ローカルで再 include に戻せる

この方式を採用したのは、`!*.pub` を既定で入れたいが、「pub も見たい」環境の
ユーザーがローカルで打ち消せる必要があったため。従来の `(includes, excludes)`
タプル返却 + exclude-wins では、ローカル include を追加しても既定 exclude に
常に負ける構造になっていた。

### 実装上の注意

- ローカル読込失敗時: `FileNotFoundError` は黙って既定のみ返す。
  その他の `OSError` (PermissionError 等) は:
  - `core/patterns.py` → `core.logging.log_error` 経由で stderr + logfile warning
  - `check-sensitive-files/checker.py` → `sys.stderr.write` 直書き (hook 間の
    Python 依存を作らない)
- `check-sensitive-files/__main__.py` の `load_patterns` 呼出は `OSError` を
  try/except で包み、Stop hook 全体を fail-open にする (stderr warning + 空出力)

## ログ規則 (重要)

`core/logging.py::log_error` には **以下を絶対に渡さない**:

| NG | OK |
|---|---|
| ファイル内容 | エラー種別 (FileNotFoundError, etc.) |
| 値 (鍵・秘密) | 関数名・hook バージョン |
| 展開後の絶対パス | 処理時間 |
| basename | `classify()` 結果 (regular/symlink/special) |
| Bash command 文字列 | Bash の判定分類 (simple/complex/fail) |

違反は即 PR reject。`log_error` 呼出時は第二引数に渡す文字列を**目視確認**すること。

## Fail-closed / deny 動作表 (0.2.0)

### 機密検出済み (deny 固定 — 非 bypass / bypass どちらも同じ)

| 検出元 | 判定 |
|---|---|
| Read: 機密 + regular | `deny` + minimal info |
| Bash: 機密 path への単純読み取り (`cat .env` 等) | `deny` |
| Edit/Write/MultiEdit: 機密 path への書き込み (通常/symlink/special) | `deny` |

### 判定不能 (fail-closed、非 bypass = `ask` / bypass = `deny`)

| 失敗箇所 | 非 bypass | bypass |
|---|---|---|
| stdin JSON parse 失敗 | `deny` (最厳) | `deny` |
| patterns.txt 読込失敗 | `ask` + stderr | `deny` + stderr |
| envelope に `permission_mode` キーなし | `ask` | 同左 (bypass 判定不能なので ask) |
| matcher / safepath 例外 | `ask` | `deny` |
| redaction engine 例外 | `ask` | `deny` |
| handler 内の未捕捉例外 | `ask` | `deny` |
| Bash: 変数展開 / $() / heredoc / shell wrapper | `ask` | `deny` |
| Edit: 親ディレクトリが symlink / missing | `ask` | `deny` |
| hook timeout (2s) | allow (介在不能) | 同左 |
| Stop hook の patterns.txt OSError | **exit 0 + stderr warning + 空出力** (fail-open) | 同左 |

**timeout だけ fail-open**: hook プロセス自体が応答不能だと deny/ask を返せない。
代わりに timeout を短く (2 秒) し発生頻度を抑える。

**Stop hook は全体的に fail-open**: 応答を止め続ける害のほうが大きいため、
patterns.txt が読めない場合も stderr 警告のみで通常 Stop にする。
read 側 (fail-closed) と意図的に非対称。

## 既知制限 (0.2.0 時点)

1. **MCP 経路は対象外** — MCP server 経由のファイルアクセスは hook が介在しない
2. **Bash 間接アクセス** — `< .env`, `command cat`, `env VAR=... cat`,
   `xargs -a .env`, `$VAR`, `$(...)`, heredoc, base64 decode, `/bin/cat`,
   `bash -c`, `bash -lc`, `FOO=1 source .env`, 改行区切り複数コマンドは
   全て **ask (fail-closed)**。allow ではない点に注意
3. **親ディレクトリ差し替え race** — `O_NOFOLLOW` は最終要素のみ保護し、
   途中要素の symlink 差し替え race は対象外 (原理的に完全防御不能)
4. **TOCTOU 完全排除は非目的** — hook 読取と Claude 実 Read/Write の分離は範囲外。
   0.2.0 で fd ベース reader により「同一 hook プロセス内の再 open」race は排除済
5. **`<DATA untrusted>` モデル解釈保証なし** — 包装 + sanitize + DATA タグ
   エスケープで多段防御するが、モデルが敵対的文脈として扱う保証は無い
6. **Windows は fail-closed で deny exit** — SIGALRM 非対応のため hook 冒頭で
   deny exit する (Step 0-c 実測結果確定前の暫定方針)
7. **submodule 内 untracked は非対象** — `git ls-files --recurse-submodules` は
   tracked のみ。untracked を submodule 内まで拾う git native オプションは無い
8. **Git バージョン依存** — `--recurse-submodules` は git 1.7+ が必要。古い
   環境では fallback で素の `ls-files` を使うが、submodule 検査は効かない
9. **SIGALRM はプロセス global** — 同一プロセス内で複数スレッドから同時に
   呼ぶと timeout が混線する (hook 1 回の実行では問題なし)
10. **`keyonly_scan` の YAML リスト非対応** — `- name: x` 形式は拾わない
11. **感度差 (軽微)** — 0.2.0 で Stop 側も parts 評価にしたが、両 hook が
    異なる envelope / 実行タイミングを扱うため完全対称ではない
12. **`patterns.txt` 変更の影響範囲** — 変更すると両 hook が同時に影響を受ける

## 最低依存バージョン

- **Python 3.11+** (標準ライブラリのみ、`pip install` 不要。tomllib は 3.11 標準)
- **Git 1.7+** (submodule scan `--recurse-submodules` 用)
- **Claude Code CLI 2.1.100+** (PreToolUse hook + permissionDecisionReason)
- **OS**: macOS / Linux。Windows は現状 fail-closed で deny

## Edit/Write hook の発火経路 (2026-04-18 実機観測)

Claude Code CLI 2.1.112 における **Edit/Write tool の PreToolUse hook** は、tool
呼び出しの状況によって発火の有無が変わる:

| 操作 | 既存ファイル | 新規作成 |
|---|---|---|
| `Edit` | **hook 未到達** (Claude Code の Read 前提チェックで先に `File must be read first` エラー) | — (Edit は既存前提) |
| `Write` | **hook 未到達** (同上、`Error writing file`) | **hook 発火** (redact-sensitive-reads deny で block) |

つまり現在の防御は二層構造で成立している:

1. **本線 (hook)**: 新規作成 Write → edit_handler → deny
2. **副次 (Claude Code 内蔵)**: 既存ファイル Edit/Write → Read 前提チェック → 内部エラー

redact hook が Read を deny している状態では、Claude が Read を試みると失敗 →
Claude が Edit/Write を試みても「Read 済み」にならないため Claude Code が先に弾く。
この **Read 前提チェックの副次防御** により、既存機密ファイルの Edit/Write は hook
まで到達しなくても block される。

将来 Claude Code がこの仕様を変更した場合 (例: bypass モードで Read 前提を緩和)、
副次防御が消えるため**本線の hook が唯一の防御になる**。したがって Edit/Write の
matcher と edit_handler は dead code ではなく、**設計上の必須コンポーネント**。

## Step 0-c 実測結果 (将来更新予定)

プラン v3 の Step 0-c (outer timeout 発火時の Claude 挙動実測) は未実施。
暫定方針として Case A (timeout kill → allow/fail-open の最悪ケース想定) で
Windows (SIGALRM 非対応) を hook 冒頭で deny exit にしている。

実測後、以下のいずれかに方針確定:
- **Case A 確定**: Windows 非対応、README の既知制限に明記継続
- **Case B 確定**: timeout kill → deny で Claude が継続するなら、Windows でも
  hang = 自動 deny となり安全。`__main__._is_unsupported_platform` ガードを
  解除可能

実測手順は以下 (手動):
1. `hooks.json` の `timeout: 2` の hook に `time.sleep(5)` を仕込む
2. `claude --plugin-dir .` で起動して Read 実行
3. timeout kill 後、Claude Code が allow / ask / deny のどれを返すか観察
4. 結果をこの section に追記する

## テスト実行

```bash
# redact-sensitive-reads
cd hooks/redact-sensitive-reads
python3 -m unittest discover tests

# check-sensitive-files (git repo を tmpdir に作って検査)
cd hooks/check-sensitive-files
python3 -m unittest discover tests
```

`tests/_testutil.py` が plugin 内各 hook dir を sys.path に挿入するため、
追加の環境変数設定は不要。テスト中の `XDG_CONFIG_HOME` / `HOME` は
`unittest.mock.patch.dict` で tmpdir に差し替えて実ホームを汚染しない。

## 手動スモーク

```bash
mkdir -p /tmp/sfg-smoke && cd /tmp/sfg-smoke
cat > .env <<'EOF'
DATABASE_URL=postgresql://u:p@h/d
JWT_SECRET=eyJ...
DEBUG=true
EOF
# 実 Claude Code セッションで `.env を見せて` などを試す
```

patterns.local.txt の合流:

```bash
export XDG_CONFIG_HOME=/tmp/sfg-xdg
mkdir -p "$XDG_CONFIG_HOME/sensitive-files-guard"
echo '*.foo' > "$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt"
# Claude Code セッションで custom.foo に触ると deny される
```

## 拡張ポイント

- **Bash handler** (`handlers/bash_handler.py`) は 0.2.0 実装済み。認識対象コマンド
  を増やすには `_SAFE_READ_CMDS` / `_SOURCE_CMDS` に追加。`_SHELL_WRAPPERS` や
  `_UNSAFE_METACHARS` を変えるときは README matrix も同期更新
- **Edit/Write/MultiEdit handler** (`handlers/edit_handler.py`) は 0.2.0 実装済み。
  3 tool とも `file_path` キー前提で共通 dispatch している。NotebookEdit は
  `file_path` を持つが `edits` 形状が違うため未対応
- 新しい format 対応 (ini, properties, etc.) は `redaction/engine.py::_detect_format`
  の分岐を増やすのが入口
- **親ディレクトリ差し替え race** を拾う `is_parent_safe` (path を root まで
  辿って symlink を含まないか再帰チェック) は将来拡張候補。現状は
  `is_regular_directory(parent)` で親 1 段のみ保護 (最終要素の protection に比べ
  コストが大きく、README の既知制限で明示している)

### パターン追加時の同期チェックリスト

新しい機密拡張子やファミリーを追加するときは、以下の 3 箇所を**同時に**更新する
(どれか 1 つだけ変えると検出と redaction 品質が剥離する):

| 更新対象 | 役割 | 変更例 (direnv の `.envrc` 追加時) |
|---|---|---|
| `hooks/check-sensitive-files/patterns.txt` | matcher: fnmatch 対象 | `.envrc` / `*.envrc` を追加 |
| `hooks/redact-sensitive-reads/redaction/engine.py::_detect_format` | redaction 品質: format 判定 | `endswith(".envrc")` を dotenv に分岐 |
| `hooks/redact-sensitive-reads/tests/test_matcher.py::DEFAULT_RULES` | matcher の回帰テスト定数 | `(".envrc", False)` / `("*.envrc", False)` 追加 |

同期漏れの兆候:
- 新規拡張子で matcher は効くが reason が opaque 扱いになる → engine の `_detect_format` 漏れ
- test_matcher の既存テストが pass するのに、実 `patterns.txt` と乖離している → DEFAULT_RULES の更新漏れ
- 機密検出されない → patterns.txt の更新漏れ

追加後は:
1. `python3 -m unittest discover hooks/redact-sensitive-reads/tests`
2. `python3 -m unittest discover hooks/check-sensitive-files/tests`
3. `claude plugin validate .`

の 3 点を走らせて warning 0 / all green を確認する。

## リリース手順

1. `.claude-plugin/plugin.json` の version を semver で bump
2. `README.md` の「リリースノート」を更新
3. `CLAUDE.md` の「Step 0-c 実測結果」に実測値があれば追記
4. `claude plugin validate .` で warning 0 を確認
5. `../../../.tools/validate-all.sh` で marketplace 全体の健全性を確認
6. commit + tag (`v0.2.0` 等) + push

## 依存関係

標準ライブラリのみ。`pip install` 不要。tomllib は 3.11+ 標準、
未満は opaque フォールバック。
