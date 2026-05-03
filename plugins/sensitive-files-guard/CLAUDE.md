# sensitive-files-guard (実装者向けガイド)

このファイルは **plugin の保守・拡張者向け**。概要は [README.md](./README.md)、
設計詳細は [docs/DESIGN.md](./docs/DESIGN.md)、判定マトリクスは
[docs/MATRIX.md](./docs/MATRIX.md)、パターン設定は
[docs/PATTERNS.md](./docs/PATTERNS.md) を参照。

## 目的と非目的

### 目的

1. `Read` を `PreToolUse` で先取りし、機密パスに触れる操作を検出
2. 検出時は **実値を hook プロセスのメモリだけに留め**、モデルには **鍵名・順序・
   型・件数のみ** (= minimal info) を返す
3. `Stop` で tracked / untracked いずれの機密ファイルも検出し、`.gitignore` 追加
   (+ tracked なら `git rm --cached`) を促す
4. 両 hook は同一 `patterns.txt` を共有 (単一ソース) + ローカル拡張点
   (`patterns.local.txt`) を提供

### 非目的

- **敵対的防御ではない**。プロンプトインジェクションや悪意のある agent 攻撃は対象外
- **完全な情報遮断ではない**。ファイル名 (basename) と鍵名は漏れる
- **`Grep` / `Glob` / `NotebookEdit` は対応外**。`Bash` / `Edit` / `Write` /
  `MultiEdit` は 0.2.0 から static 解析ベースで判定する handler を追加したが、
  シェル動的展開 (`cat "$X"`, `bash -c`, `source .env` 間接経路) は
  autonomous/plan で allow に倒す境界があり、完全な書き込み防止は Claude Code
  側の sandbox が無いと塞ぎきれない。スコープ外として明示
- **race condition の完全排除ではない**。TOCTOU 限界あり

## ディレクトリ構成

```
sensitive-files-guard/
├── .claude-plugin/plugin.json       # version 0.5.0
├── README.md                        # 利用者向け概要
├── CLAUDE.md                        # 本ファイル (保守者向け)
├── CHANGELOG.md                     # 全バージョン統合リリースノート
├── docs/
│   ├── DESIGN.md                    # 設計原則 / Phase 0 実測 / 既知制限
│   ├── MATRIX.md                    # 判定結果の完全マトリクス (6 mode 列)
│   └── PATTERNS.md                  # パターン仕様・設定例
└── hooks/
    ├── hooks.json                   # PreToolUse(Read/Bash/Edit/Write) + Stop
    ├── _shared/                     # 両 hook 共有ロジック (0.2.0 新設)
    │   ├── matcher.py               is_sensitive (case-insensitive + last-match-wins)
    │   └── patterns.py              _parse_patterns_text / _resolve_local_patterns_paths (2-tier: 0.4.0+)
    ├── check-sensitive-files/       # Stop hook
    │   ├── __main__.py
    │   ├── checker.py               _shared 経由 + git ls-files --recurse-submodules
    │   ├── patterns.txt             ← 両 hook で共有するパターン
    │   └── tests/                   # unittest + conftest.py (git repo 作成 + XDG 隔離)
    └── redact-sensitive-reads/      # PreToolUse hook
        ├── __main__.py              # fail-closed wrapper + Windows SIGALRM チェック
        ├── core/
        │   ├── logging.py           秘密非混入ログ (LOG_PATH=~/.claude/logs/redact-hook.log)
        │   ├── output.py            deny/ask JSON builder + ask_or_deny / ask_or_allow
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
        │   ├── bash_handler.py      Bash orchestration + plugin ステート依存 + test seam (0.3.3 で責務分離)
        │   ├── bash/                # pure helper / compile-time 定数 (0.3.3 新設)
        │   │   ├── __init__.py
        │   │   ├── constants.py     regex / frozenset (hard-stop, opaque wrappers, shell keywords 等)
        │   │   ├── segmentation.py  quote-aware セグメント分割 / hard-stop 検出
        │   │   ├── operand_lexer.py glob 判定 / literalize / path 候補抽出
        │   │   └── redirects.py     安全リダイレクト剥離 / 残留 metachar 判定
        │   └── edit_handler.py      Edit / Write (deny 固定 + dotenv キー名ガイド付き)
        └── tests/                   unittest + conftest.py + fixtures/envelopes/
```

### `handlers/bash/` の責務境界 (0.3.3)

`handlers/bash/` 配下のモジュールは **副作用なし・plugin ステート非依存**。
`is_sensitive` / `load_patterns` / envelope 操作などの plugin ステート依存処理は
すべて `bash_handler.py` 側に残す。

既存テスト (0.3.4 時点 502 件) は `handlers.bash_handler.X` から
以下を import する patch seam として依存している。`bash_handler.py` は
`handlers.bash.*` からこれらを **再 export** して従来の import path を維持する:

| test ファイル | 参照 symbol |
|---|---|
| `test_bash_handler.py` | `handle` |
| `test_input_redirect.py` | `_extract_input_redirect_targets`, `handle` |
| `test_prefix_normalize.py` | `_normalize_segment_prefix` |
| `test_glob_candidates.py` | `_glob_candidates`, `_glob_operand_is_sensitive`, `_literalize` |
| `test_failclosed.py` | `handle`, `mock.patch("handlers.bash_handler.load_patterns", ...)` |

pure helper をさらに別モジュールへ移すときは、必ず `bash_handler.py` 側で
再 export を追加して既存 patch seam を維持する。

## 設計原則

1. **Fail-closed in doubt** (Read/Edit は ask_or_deny, Bash は ask_or_allow)
2. **情報量最小化** (minimal info)
3. **Secrets never in logs**
4. **Latency <100ms 目標** (timeout 2 秒)
5. **情報注入は `permissionDecisionReason` 一択**

設計根拠と Phase 0 実測ログは [docs/DESIGN.md](./docs/DESIGN.md) に集約。

## Bash handler 判定フロー

```mermaid
flowchart TD
    A[Bash command] --> B{empty?}
    B -- yes --> Z1[allow]
    B -- no --> C{patterns.txt<br/>load OK?}
    C -- no --> Z2[deny 固定<br/>policy 欠如]
    C -- yes --> D{has hard-stop<br/>$/`/(/)/{/}/&lt;}
    D -- yes --> E{`< target` 抽出<br/>機密一致?}
    E -- yes --> Z3[deny 固定]
    E -- no --> Z4[ask_or_allow<br/>default/acceptEdits/dontAsk=ask<br/>auto/bypass/plan=allow]
    D -- no --> F[segment split<br/>&amp;&amp; / ‖ / ; / ‖ / \\n]
    F --> G[per-segment 解析]
    G --> H{shlex.split OK?}
    H -- no --> Z5[ask_or_allow]
    H -- yes --> I[strip safe redirects]
    I --> J[normalize prefix<br/>env / command / builtin / nohup]
    J --> K{None?<br/>opaque wrapper}
    K -- yes --> Z6[ask_or_allow]
    K -- no --> L{empty?}
    L -- yes --> Z7[allow]
    L -- no --> M{residual metachar?}
    M -- yes --> Z8[ask_or_allow]
    M -- no --> N{shell keyword?<br/>if/for/do/coproc 等}
    N -- yes --> Z9[ask_or_allow]
    N -- no --> O[operand scan]
    O --> P{has glob?}
    P -- yes --> Q{_glob_operand_is_sensitive}
    Q -- True --> Z10[deny 固定]
    Q -- False --> Z11[allow]
    P -- no --> R{_operand_is_sensitive}
    R -- True --> Z12[deny 固定]
    R -- False --> Z13[allow]
```

コマンド別の deny / allow / ask の完全表は [docs/MATRIX.md](./docs/MATRIX.md)。

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
- 全体 **1-2KB 目標**、ハード上限 3KB
- Markdown 不使用

## CLI バージョンアップ時の再実測手順 (Runbook)

`core/output.py::LENIENT_MODES` と `tests/fixtures/envelopes/README.md:22` の
列挙は Claude Code CLI の `permission_mode` 値に依存する。CLI メジャーアップデート
のたびに以下を走らせて乖離が無いか確認する。

### 1. envelope 採取用の一時 probe スクリプトを作成

`hooks/_debug/capture_envelope.py` として以下を配置する (実測後に削除するため
git commit しない):

```python
#!/usr/bin/env python3
"""stdin JSON を /tmp/envelope-<tool>-<ts>.json に保存し no-op allow を返す。"""
import argparse, json, sys, time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--tool", required=True)
args = p.parse_args()
raw = sys.stdin.read()
ts = time.strftime("%Y%m%dT%H%M%S")
out = Path("/tmp") / f"envelope-{args.tool}-{ts}.json"
try:
    envelope = json.loads(raw) if raw.strip() else {}
except Exception as e:
    envelope = {"_probe_parse_error": str(e), "_raw": raw}
with out.open("w") as f:
    json.dump(envelope, f, indent=2, ensure_ascii=False)
sys.stdout.write("{}")
```

### 2. hooks.json の matcher を差し替え

本番 hook を壊さないよう別コピーで作業するか、git で変更を戻せる状態にする:

```json
{
  "matcher": "Bash",
  "hooks": [
    {
      "type": "command",
      "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/_debug/capture_envelope.py --tool bash",
      "timeout": 2
    }
  ]
}
```

### 3. 各 permission_mode で 1 回ずつ Bash tool を呼ぶ

`claude --plugin-dir .` で起動し、以下を実行:
- `/default` もしくは `--permission-mode default` で `date`
- `/auto` で `date`
- `/plan` モードに入って Bash (例: `date`)
- `/acceptEdits` で `date`
- `bypassPermissions` モードで `date`

`/tmp/envelope-bash-*.json` が作られたか、`permission_mode` の値を確認する。

### 4. 結果をテスト回帰検知 assert と突合

`tests/test_envelope_shapes.py` の `TestLenientModesSubset` が red になれば、
CLI が新しい mode を追加したサイン。red になったら:

1. 実測で確認した新 mode を `_KNOWN_PERMISSION_MODES` に追加
2. 許容可能なら `LENIENT_MODES` にも追加 (Bash opaque ケースで allow に倒したいか判断)
3. `fixtures/envelopes/README.md:22` の列挙を更新
4. `docs/DESIGN.md` と `docs/MATRIX.md` の mode 列を追加
5. このセクションに実測日を追記

### 5. debug 差し戻し

`hooks.json` を元に戻し、`hooks/_debug/` を削除する。

### Phase 0 実測ログ

| 日付 | CLI version | 実測内容 | 結果 |
|---|---|---|---|
| 2026-04-11 | 2.1.101 | `permissionDecisionReason` / `systemMessage` / `ask` reason | `~/shared-context/security/claude-code-pretooluse-hook-spec.md` に恒久記録 |
| 2026-04-22 | 2.1.101 系 | plan mode での Bash hook 発火有無 | **Case C**: plan mode で hook は発火しない。`LENIENT_MODES` に追加した `"plan"` は現行 CLI では dead entry、将来 CLI 変更への前方互換層として機能 |

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

### patterns.local.txt の 2-tier lookup (0.4.0)

`_shared/patterns.py::_resolve_local_patterns_paths` が 2 つのパスを返す:

1. **preferred**: `~/.claude/sensitive-files-guard/patterns.local.txt`
2. **fallback (deprecated)**: `$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt`
   (未設定時 `~/.config/sensitive-files-guard/patterns.local.txt`)

`load_patterns()` は preferred → fallback の順に `read_text()` を試し、最初に
成功したものを採用する (preferred と fallback の両方が存在する場合は preferred
のみ採用)。fallback 採用時は `warn_callback("deprecated_config_dir")` を呼び、
呼出側が各自の流儀で通知する:

- **Read hook (redact-sensitive-reads)**: `core.logging.log_info` で LOG_PATH
  のみに 1 行 (`permissionDecisionReason` に載せない — LLM 文脈毎回混入ノイズ回避)
- **Stop hook (check-sensitive-files)**: stderr に 1 行 (Claude Code UI で可視化)

**0.6.0 で fallback を削除予定**。それまでに利用者には新パスへの移行を案内する
(docs/PATTERNS.md / README.md に手順記載)。

旧 `_resolve_local_patterns_path()` (単数) は preferred を返す後方互換 alias。

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

## 拡張ポイント

- **Bash handler** (`handlers/bash_handler.py` + `handlers/bash/*`): 認識対象
  コマンドを増やすには `handlers/bash/constants.py` の `_SAFE_READ_CMDS` /
  `_SOURCE_CMDS` に追加。`_OPAQUE_WRAPPERS` や `_UNSAFE_METACHARS` を変えるときは
  `docs/MATRIX.md` も同期更新
- **Edit/Write handler** (`handlers/edit_handler.py`): 3 tool とも `file_path`
  キー前提で共通 dispatch。NotebookEdit は `file_path` を持つが `edits` 形状が
  違うため未対応
- 新しい format 対応 (ini, properties, etc.) は
  `redaction/engine.py::_detect_format` の分岐を増やすのが入口
- パターン追加時の同期チェックリスト (patterns.txt / `_detect_format` /
  DEFAULT_RULES) は [docs/PATTERNS.md](./docs/PATTERNS.md) 参照

## リリース手順

1. `.claude-plugin/plugin.json` の version を semver で bump
2. `CHANGELOG.md` にバージョンエントリを追加
3. `claude plugin validate .` で warning 0 を確認
4. `../../../.tools/validate-all.sh` で marketplace 全体の健全性を確認
5. commit + tag (`v0.3.4` 等) + push

## Step 0-c 実測結果 (将来更新予定)

プラン v3 の Step 0-c (outer timeout 発火時の Claude 挙動実測) は未実施。
暫定方針として Case A (timeout kill → allow/fail-open の最悪ケース想定) で
Windows (SIGALRM 非対応) を hook 冒頭で deny exit にしている。

実測後、以下のいずれかに方針確定:
- **Case A 確定**: Windows 非対応、README の既知制限に明記継続
- **Case B 確定**: timeout kill → deny で Claude が継続するなら、Windows でも
  hang = 自動 deny となり安全。`__main__._is_unsupported_platform` ガードを解除可能

実測手順:
1. `hooks.json` の `timeout: 2` の hook に `time.sleep(5)` を仕込む
2. `claude --plugin-dir .` で起動して Read 実行
3. timeout kill 後、Claude Code が allow / ask / deny のどれを返すか観察
4. 結果を [docs/DESIGN.md](./docs/DESIGN.md) と本ファイルに追記

## 依存関係

標準ライブラリのみ。`pip install` 不要。tomllib は 3.11+ 標準、
未満は opaque フォールバック。

## 最低依存バージョン

- **Python 3.11+**
- **Git 1.7+** (submodule scan `--recurse-submodules` 用)
- **Claude Code CLI 2.1.100+** (PreToolUse hook + permissionDecisionReason)
- **OS**: macOS / Linux。Windows は現状 fail-closed で deny
