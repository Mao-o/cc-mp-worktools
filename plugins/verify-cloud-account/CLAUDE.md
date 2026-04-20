# verify-cloud-account (実装者向けガイド)

このファイルは **plugin の保守・拡張者向け**。利用者向け概要は [README.md](./README.md)。

## 目的

`PreToolUse:Bash` フックとして動作し、Bash コマンド実行前にクラウドサービスの
アクティブアカウントがプロジェクトの想定値と一致するかを検証する。
不一致なら deny し、切り替え手順を提示する。

複数の AWS / GCP / Firebase / GitHub アカウントを切り替えて作業する際、
間違ったアカウントで `gh pr create` や `firebase deploy` 等を実行する事故を防ぐ。
プロジェクトルートの `.claude/accounts.local.json` に期待アカウントを記述しておき、
フック実行時に CLI で現在値を取得・照合する。

## ディレクトリ構成

```
verify-cloud-account/
├── .claude-plugin/
│   └── plugin.json
├── README.md                           利用者向け概要
├── CLAUDE.md                           このドキュメント
└── hooks/
    ├── hooks.json                      PreToolUse:Bash の単一エントリを定義
    └── verify-cloud-account/
        ├── __main__.py                 エントリポイント。stdin から hook input を読み取り dispatch を呼ぶ
        ├── core/
        │   ├── __init__.py
        │   ├── dispatcher.py           コマンド → サービス振り分け、accounts 読込、検証オーケストレーション
        │   └── output.py               deny / warn の hookSpecificOutput JSON ビルダー
        └── services/
            ├── __init__.py             登録済みサービスの ALL リスト（追加時はここに 2 行足す）
            ├── github.py               gh CLI
            ├── firebase.py             firebase CLI + .firebaserc フォールバック
            ├── aws.py                  aws sts get-caller-identity
            └── gcloud.py               gcloud config get-value project
```

**実行フロー**: `__main__.py` が stdin から hook input を受け取り `core.dispatcher.dispatch()` を呼ぶ。dispatcher は `services.ALL` を走査してコマンドにマッチするサービスを見つけ、該当サービスの `verify()` を実行する。結果を `core.output.deny()` / `warn()` で整形して stdout に返す。

Python 3.9+ 想定。標準ライブラリのみ使用（外部依存なし）。

**起動コマンド**: `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account`
(ディレクトリを渡すことで `__main__.py` が実行される)

## サービスモジュールの契約

`services/*.py` は以下 5 つを公開する:

| 属性 | 型 | 説明 |
|---|---|---|
| `PATTERNS` | `list[str]` | コマンドマッチ用の正規表現（`re.search`）。**先頭アンカ `^` 必須** |
| `READONLY` | `list[str]` | 検証をスキップする読み取り専用コマンドの正規表現 |
| `ACCOUNT_KEY` | `str` | `accounts.local.json` 上のキー名 |
| `SETUP_HINT` | `str` | `accounts.local.json` 未設定時の deny メッセージに埋め込む案内文 |
| `verify(expected, project_dir)` | `(str, str) -> str \| None` | 検証関数。成功=`None`、失敗=エラーメッセージ文字列 |

### `verify()` の実装規則

- **成功時は `None`** を返す
- **失敗時はエラー理由 + 解決手順を 1 つの文字列に含めて返す**（deny の reason としてそのまま出る）
- 例外を raise しない。CLI 未インストール（`FileNotFoundError`）や timeout も文字列で返す
- `subprocess` の `timeout` は 10〜15 秒を目安に設定する
- `project_dir` を使わないサービスも引数では受け取る（インターフェース統一のため）

## サービスを追加する

1. `services/<name>.py` を作成し、上記 5 つの属性・関数を実装する
2. `services/__init__.py` に 2 行追加:
   ```python
   from . import <name>
   ALL = [..., <name>]
   ```
3. `accounts.local.json` の例示に新キーを追記（各 service の `SETUP_HINT` 内）

動的ディスカバリではなく**明示的 import**にしている理由:
- IDE 補完・型チェッカ・linter が機能する
- import エラーが沈黙せずそのまま surface する
- `ALL` への登録漏れに気づきやすい（コードレビューで見える）

## コマンドマッチングの注意点

### 先頭アンカ `^` 必須

`PATTERNS` は `re.search` で評価される。先頭アンカを付けないと
コマンド文字列のどこかに現れるだけでマッチしてしまう。

```python
PATTERNS = [r"^gh\b"]       # OK
PATTERNS = [r"gh\b"]        # NG: `echo "gh"` 等でも発火
```

### ラッパ経由コマンドの考慮

`npx`, `pnpm exec`, `mise exec --` 等のラッパ経由実行も想定する場合は
オプショナル部分として記述する:

```python
PATTERNS = [r"^(npx\s+|pnpm\s+exec\s+|mise\s+exec\s+--\s+)?firebase\b"]
```

### マッチ順序

`core.dispatcher._match_service` は `services/__init__.py` の `ALL` を**先頭から順**に
評価し、最初にマッチしたサービスを採用する。2 つのサービスで `PATTERNS` が
競合する設計は避ける（曖昧なコマンドは各サービス側で除外するのが正解）。

## `accounts.local.json` の仕様

プロジェクトルートの `.claude/accounts.local.json` を読む。JSON オブジェクトで、
各キーは `ACCOUNT_KEY` と一致する。

```json
{
  "github": "Mao-o",
  "firebase": "my-project-id",
  "aws": "123456789012",
  "gcloud": "my-gcp-project"
}
```

**旧名 `accounts.json` フォールバック**: 下位互換のため `.claude/accounts.json` も
検出するが、検出時は検証は通しつつ warn でリネームを促す。新規プロジェクトでは
必ず `accounts.local.json` を使うこと（`.gitignore` 対象にするため）。

## 読み取り専用コマンドの重要性

各サービスには「現在の状態を確認するコマンド」がある:

- `gh auth status` / `gh auth list`
- `firebase use`（引数なし）
- `aws sts get-caller-identity`
- `gcloud auth list` / `gcloud config get-value project|account`

これらを `READONLY` に登録しないと、`accounts.local.json` が未設定のプロジェクトで
**状態確認すらできない**デッドロックになる（accounts 未設定 → deny → 設定のため状態確認
したい → deny... のループ）。新規サービス追加時は必ず確認コマンドを `READONLY` に含める。

## hook 登録方法

plugin として install すれば `hooks/hooks.json` が自動適用される。ユーザーが
`~/.claude/settings.json` を手で編集する必要はない。

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account",
            "timeout": 20
          }
        ]
      }
    ]
  }
}
```

- `matcher: "Bash"` のみ。`if` フィールドは**使わない**
- `timeout` は**秒単位**（Claude Code 公式仕様 / https://code.claude.com/docs/en/hooks ）。
  過去に「ミリ秒」と誤記して全 hook で 1000 倍スケールずれを起こした実績があるため注意
- AWS STS が遅いケースで `aws.py` の `subprocess timeout=15` が発火したときに、
  外側がほぼ同時に kill してエラー理由を握り潰さないよう、外側は内部より数秒長めに取る（20 秒）

### `if` フィールドを使わない理由

以前は各サービスごとに `if: "Bash(gh *)"` 等でフィルタしていたが、
**VS Code 拡張のフックランナーが `if` を無視する**ケースで全 Bash コマンドに
発火し、`direnv export bash` のような無関係なコマンドでも誤 deny する事故が発生。

振り分けは常にスクリプト内部（`dispatcher._match_service`）で行い、
ランタイム差異に依存しない設計にしている。

## テスト

### スモーク (stdin に hook input JSON を流す)

開発時 (`--plugin-dir` でロード) に、plugin 配下の python hook を直接呼ぶ:

```bash
cd plugins/verify-cloud-account

# 対象外 → 出力なし、exit 0
echo '{"tool_input":{"command":"git status"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account

# 対象 + accounts 未設定 → deny JSON
echo '{"tool_input":{"command":"gh pr list"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account

# readonly → 出力なし、exit 0
echo '{"tool_input":{"command":"gh auth status"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account
```

期待動作:
- 対象外コマンド → 出力なし、exit 0
- 対象コマンド + accounts 未設定 → `permissionDecision: deny` の JSON
- readonly コマンド → 出力なし、exit 0（検証スキップ）
- 対象コマンド + accounts 一致 → 出力なし、exit 0
- 対象コマンド + accounts 不一致 → `permissionDecision: deny` の JSON

### 開発時の E2E

```bash
claude --plugin-dir plugins/verify-cloud-account
```

実セッションで `gh auth status` / `firebase use` 等を叩き、
`accounts.local.json` の有無・一致/不一致で挙動を確認する。

### 単体テスト

現状未整備。将来的に `hooks/verify-cloud-account/tests/` 配下に
`unittest.mock` でサブプロセスを差し替えたテストを置く想定
(`sensitive-files-guard` の `tests/` 構成が参考)。

## 設計判断の履歴

- **Python 採用** — bash では正規表現・JSON パースが煩雑で保守性が低い。標準ライブラリの `subprocess` / `json` / `re` で十分
- **プラグイン分離** — サービスごとに CLI・出力パース・エラーメッセージが異なり、JSON 設定ファイルで汎用化しようとすると破綻する（各 CLI の出力フォーマットが標準化されていない）
- **明示 import（動的ディスカバリ不採用）** — IDE 補完とデバッグ性を優先。追加時の 2 行コストは許容範囲
- **1 エントリ集約（`if` 廃止）** — VS Code 拡張での `if` 誤動作事故を受けて、振り分けをスクリプト内部に一元化
- **plugin 化** — ローカル hook だと別マシン再現性がない。`/plugin install verify-cloud-account@mao-worktools` で配布できるように plugin 化

## リリース手順

1. `.claude-plugin/plugin.json` の version を semver で bump
2. `README.md` のリリースノート (あれば) を更新
3. `claude plugin validate .` で warning 0 を確認
4. `../../../.tools/validate-all.sh` で marketplace 全体の健全性を確認
5. commit + tag + push

## 依存関係

Python 3.9+ (annotations / `tuple[str, int]` 型ヒント)。標準ライブラリのみ。`pip install` 不要。
