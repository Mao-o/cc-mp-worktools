# verify-cloud-account (実装者向けガイド)

このファイルは **plugin の保守・拡張者向け**。利用者向け概要は [README.md](./README.md)。

## 目的

`PreToolUse:Bash` フックとして動作し、Bash コマンド実行前にクラウドサービスの
アクティブアカウントがプロジェクトの想定値と一致するかを検証する。
不一致なら deny し、切り替え手順を提示する。

複数の AWS / GCP / Firebase / GitHub / Kubernetes アカウントを切り替えて
作業する際、間違ったアカウントで `gh pr create` や `firebase deploy`
`kubectl apply` 等を実行する事故を防ぐ。プロジェクトルートの
`.claude/accounts.local.json` に期待アカウントを記述しておき、フック実行時に
CLI で現在値を取得・照合する。

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
        │   ├── command_parser.py       Bash コマンド分解 (split / env strip / wrapper strip)
        │   ├── cache.py                検証成功の短期キャッシュ (30 秒 TTL, mtime 無効化)
        │   ├── dispatcher.py           コマンド → サービス振り分け、検証オーケストレーション
        │   └── output.py               deny / warn の hookSpecificOutput JSON ビルダー
        ├── services/
        │   ├── __init__.py             登録済みサービスの ALL リスト
        │   ├── github.py               gh CLI (hostname 別 dict 対応)
        │   ├── firebase.py             firebase CLI + .firebaserc + alias dict 対応
        │   ├── aws.py                  aws sts get-caller-identity
        │   ├── gcloud.py               gcloud config get-value project (+ account)
        │   └── kubectl.py              kubectl config current-context
        └── tests/                      unittest (標準ライブラリのみ)
            ├── _testutil.py            sys.path 整備 (unittest)
            ├── conftest.py             sys.path 整備 (pytest)
            ├── test_command_parser.py
            ├── test_dispatcher.py
            ├── test_services.py
            └── test_cache.py
```

### 実行フロー

1. `__main__.py` が stdin から hook input (tool_input.command, cwd) を受け取る
2. `core.dispatcher.dispatch()` が呼ばれる
3. `core.command_parser.extract_candidates()` でコマンドを分解
   (チェーン分割 → env strip → wrapper strip)
4. 各候補セグメントをサービスにマッチング。readonly 除外・重複 dedup
5. `accounts.local.json` を読み込み、各サービスについて `verify()` を実行
   (キャッシュ hit ならスキップ)
6. 結果を `core.output.deny()` / `warn()` で整形して stdout に返す

Python 3.9+ 想定。標準ライブラリのみ使用 (外部依存なし)。

**起動コマンド**: `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account`
(ディレクトリを渡すことで `__main__.py` が実行される)

## サービスモジュールの契約

`services/*.py` は以下 5 つを公開する:

| 属性 | 型 | 説明 |
|---|---|---|
| `PATTERNS` | `list[str]` | コマンドマッチ用の正規表現 (`re.search`)。**先頭アンカ `^` 必須** |
| `READONLY` | `list[str]` | 検証をスキップする読み取り専用コマンドの正規表現 |
| `ACCOUNT_KEY` | `str` | `accounts.local.json` 上のキー名 |
| `SETUP_HINT` | `str` | `accounts.local.json` 未設定時の deny メッセージに埋め込む案内文 |
| `verify(expected, project_dir)` | `(Any, str) -> str \| None` | 検証関数。成功=`None`、失敗=エラーメッセージ文字列 |

### `verify()` の実装規則

- **成功時は `None`** を返す
- **失敗時はエラー理由 + 解決手順を 1 つの文字列に含めて返す** (deny の reason としてそのまま出る)
- 例外を raise しない。CLI 未インストール (`FileNotFoundError`) や timeout も
  文字列で返す
- `subprocess` の `timeout` は 10〜15 秒を目安に設定する
- `project_dir` を使わないサービスも引数では受け取る (インターフェース統一のため)
- `expected` の型チェック (str または dict) は各 service 内で行う。dispatcher は
  ざっくり `isinstance(entry, (str, dict))` だけ弾く

### PATTERNS / READONLY の簡素化

**ラッパ対応は dispatcher (`core.command_parser.strip_transparent_wrappers`)
に集約**しているため、各 service の PATTERNS は `^<cli>\b` のシンプルな
先頭マッチのみで良い。例:

```python
# NG (0.1.0 時代の Firebase)
PATTERNS = [r"^(npx\s+|pnpm\s+exec\s+|mise\s+exec\s+--\s+)?firebase\b"]

# OK (0.2.0+)
PATTERNS = [r"^firebase\b"]
```

dispatcher 側で `npx firebase ...` → `firebase ...` に剥がしてからマッチする。

## コマンド分解 (core.command_parser)

`extract_candidates(command)` がコマンド文字列を候補セグメントリストに変換する。

| 入力 | 出力 |
|---|---|
| `gh pr list` | `["gh pr list"]` |
| `cd /tmp && gh pr create` | `["cd /tmp", "gh pr create"]` |
| `FOO=bar gh pr list` | `["gh pr list"]` |
| `sudo time gh pr create` | `["gh pr create"]` |
| `npx firebase deploy` | `["firebase deploy"]` |
| `mise exec -- firebase deploy` | `["firebase deploy"]` |
| `echo "gh auth"` | `["echo \"gh auth\""]` (quote 内は保護) |
| `FOO=$(date) gh pr list` | `["FOO=$(date) gh pr list"]` (subshell を持つ値は保守的 stop) |

### 設計判断

- **quote-aware 分割**: `split_on_operators` は single/double quote, `$()`,
  backtick 内の `&&` / `;` / `|` / `\n` を無視する (手書き状態マシン)
- **subshell 値の保守的 stop**: `FOO=$(date) cmd` は剥がすと意味が変わり得る
  ため、値に `$(` や backtick を含む代入は剥がさない
- **env -i は opaque**: `env -i`, `env --`, `env -u NAME` は環境を書き換えるため
  剥がさない (後続コマンドの挙動が変わる)
- **透過 wrapper リスト**: `sudo`, `time`, `nohup`, `command`, `builtin`,
  `exec`, `env`, `npx`, `pnpm exec`, `pnpm dlx`, `mise exec --`, `bun x`
  のみ。`bash`, `sh`, `eval`, `python -c` は内部が script なので剥がさない
  (= 静的解析対象外として allow 相当)
- **多段ネスト対応**: `sudo time mise exec -- firebase deploy` → `firebase deploy`
  を 1 回の strip で解決するため、剥がせなくなるまでループ (max 6 回)

## 短期キャッシュ (core.cache)

### 目的

PreToolUse は Bash の度に発火するため、`gh pr list && gh pr view && gh pr comment`
のような連打で毎回外部 CLI を呼ぶとレイテンシが積み上がる
(`aws sts get-caller-identity` は 1-3 秒)。検証成功のみを 30 秒キャッシュして
連続実行コストを削減する。

### 仕様

- **成功のみキャッシュ**: `verify() -> None` のときだけ保存。失敗 (文字列返却)
  は常に再検証する (切り替え後すぐ使いたいため)
- **キー**: `sha256(service_name + project_dir + expected)`
- **値**: `{"success": True, "accounts_mtime": float, "timestamp": float}`
- **保存先**: `$TMPDIR/cc-mp-verify-cloud-account/<key>.json`
- **無効化**: TTL (30 秒) 超過 / `accounts.local.json` の mtime 不一致 /
  ファイル破損 / ファイル欠損
- **書き込み失敗は無視** (best-effort)。ディレクトリ作成やファイル書き込みが
  失敗しても deny は出さない

### 意図的にしないこと

- 失敗のキャッシュ (= negative cache): 切り替え直後に再検証したいため
- 長時間キャッシュ: 他端末でアカウント切り替えた場合に反映遅れが出るため
  30 秒で打ち切る
- プロセス内キャッシュ (`lru_cache`): hook は毎回別プロセスなので無意味

## サービスを追加する

1. `services/<name>.py` を作成し、上記 5 つの属性・関数を実装する
2. `services/__init__.py` に import と `ALL` 追加:
   ```python
   from . import aws, firebase, gcloud, github, kubectl, <name>
   ALL = [github, firebase, aws, gcloud, kubectl, <name>]
   ```
3. README.md の対応表と accounts.local.json サンプルに新キーを追記
4. `tests/test_services.py` に最低限のテストを追加 (match / mismatch /
   CLI 未インストール / timeout)

動的ディスカバリではなく**明示的 import** にしている理由:
- IDE 補完・型チェッカ・linter が機能する
- import エラーが沈黙せずそのまま surface する
- `ALL` への登録漏れに気づきやすい (コードレビューで見える)

## コマンドマッチングの注意点

### 先頭アンカ `^` 必須

`PATTERNS` は `re.search` で評価される。先頭アンカを付けないと
コマンド文字列のどこかに現れるだけでマッチしてしまう。

```python
PATTERNS = [r"^gh\b"]       # OK
PATTERNS = [r"gh\b"]        # NG: `echo "gh"` 等でも発火
```

dispatcher は `extract_candidates()` で分解後の各セグメントに対してマッチングを
行うため、セグメント単位で先頭アンカが効く。

### マッチ順序

`core.dispatcher._match_service` は `services/__init__.py` の `ALL` を
**先頭から順**に評価し、最初にマッチしたサービスを採用する。2 つのサービスで
`PATTERNS` が競合する設計は避ける (曖昧なコマンドは各サービス側で除外するのが
正解)。

## accounts.local.json の仕様

プロジェクトルートの `.claude/accounts.local.json` を読む。JSON オブジェクトで、
各キーは `ACCOUNT_KEY` と一致する。

### 基本形 (str 値)

```json
{
  "github": "Mao-o",
  "firebase": "my-project-id",
  "aws": "123456789012",
  "gcloud": "my-gcp-project",
  "kubectl": "prod-cluster"
}
```

### 拡張形 (dict 値)

| サービス | str | dict |
|---|---|---|
| github | 単一アカウント | `{"<host>": "<user>", ...}` hostname 別 (GHE 対応) |
| firebase | 単一プロジェクト | `{"<alias>": "<project_id>", ...}` いずれかに一致で OK |
| aws | 単一アカウント | (未対応、str のみ) |
| gcloud | project のみ | `{"project": "p", "account": "a"}` 両方検証 |
| kubectl | 単一 context | (未対応、str のみ) |

### 旧名 `accounts.json` フォールバック

下位互換のため `.claude/accounts.json` も検出するが、検出時は検証は通しつつ
warn でリネームを促す。新規プロジェクトでは必ず `accounts.local.json` を使う
こと (`.gitignore` 対象にするため)。

## 読み取り専用コマンドの重要性

各サービスには「現在の状態を確認するコマンド」がある:

- `gh auth status` / `gh auth list`
- `firebase use` (引数なし)
- `aws sts get-caller-identity`
- `gcloud auth list` / `gcloud config get-value project|account`
- `kubectl config current-context|get-contexts|view|get-clusters|get-users` /
  `kubectl cluster-info`

これらを `READONLY` に登録しないと、`accounts.local.json` が未設定の
プロジェクトで **状態確認すらできない** デッドロックになる
(accounts 未設定 → deny → 設定のため状態確認したい → deny... のループ)。
新規サービス追加時は必ず確認コマンドを `READONLY` に含める。

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
- `timeout` は**秒単位** (Claude Code 公式仕様 /
  https://code.claude.com/docs/en/hooks )。
  過去に「ミリ秒」と誤記して全 hook で 1000 倍スケールずれを起こした実績がある
  ため注意
- AWS STS が遅いケースで `aws.py` の `subprocess timeout=15` が発火したときに、
  外側がほぼ同時に kill してエラー理由を握り潰さないよう、外側は内部より
  数秒長めに取る (20 秒)

### `if` フィールドを使わない理由

以前は各サービスごとに `if: "Bash(gh *)"` 等でフィルタしていたが、
**VS Code 拡張のフックランナーが `if` を無視する** ケースで全 Bash コマンドに
発火し、`direnv export bash` のような無関係なコマンドでも誤 deny する事故が発生。

振り分けは常にスクリプト内部 (`dispatcher._match_service`) で行い、
ランタイム差異に依存しない設計にしている。

## テスト

### 単体テスト (整備済み)

```bash
cd hooks/verify-cloud-account
python3 -m unittest discover tests
```

- `test_command_parser.py`: split / env strip / wrapper strip / candidate
  抽出の網羅的テスト (64 ケース)
- `test_dispatcher.py`: ルーティング / accounts ファイル入力パターン /
  service 統合 / キャッシュ相互作用 (22 ケース)
- `test_services.py`: 各 CLI service の subprocess mock テスト (33 ケース)
- `test_cache.py`: ラウンドトリップ / mtime 無効化 / TTL / 破損検知 (9 ケース)

合計 128 テスト。標準ライブラリのみ (pytest / pip install 不要)。

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

# チェーン: cd + gh → 検証走る
echo '{"tool_input":{"command":"cd /tmp && gh pr list"},"cwd":"/tmp"}' \
  | python3 hooks/verify-cloud-account
```

### E2E

```bash
claude --plugin-dir plugins/verify-cloud-account
```

実セッションで `gh pr list` / `firebase use` / `kubectl get pod` 等を叩き、
`accounts.local.json` の有無・一致/不一致・チェーン・ラッパで挙動を確認する。

## 設計判断の履歴

- **Python 採用** — bash では正規表現・JSON パースが煩雑で保守性が低い。
  標準ライブラリの `subprocess` / `json` / `re` で十分
- **プラグイン分離** — サービスごとに CLI・出力パース・エラーメッセージが異なり、
  JSON 設定ファイルで汎用化しようとすると破綻する (各 CLI の出力フォーマット
  が標準化されていない)
- **明示 import (動的ディスカバリ不採用)** — IDE 補完とデバッグ性を優先。
  追加時の 2 行コストは許容範囲
- **1 エントリ集約 (`if` 廃止)** — VS Code 拡張での `if` 誤動作事故を受けて、
  振り分けをスクリプト内部に一元化
- **コマンド分解を dispatcher に集約 (0.2.0)** — 0.1.0 では Firebase だけ
  `PATTERNS` にラッパ (`npx|pnpm exec|mise exec --`) を詰め込んでいたが、
  他 service にも必要になり露出しきれなかった。`core.command_parser` を
  新設してチェーン / env / wrapper を一元処理し、各 service は
  `^<cli>\b` のシンプルな PATTERNS に統一
- **verify() の expected を str | dict に (0.2.0)** — GHE hostname 別 /
  Firebase alias / GCP account 対応のため、各 service が str と dict の
  両方を受け付けるように拡張。dispatcher は型の事前判別はせず各 service に委ねる
- **成功のみキャッシュ (0.2.0)** — 失敗もキャッシュすると切り替え後の
  即時検証ができなくなるため。30 秒 TTL + mtime 無効化の組み合わせで
  `accounts.local.json` の手動編集にも追随する
- **plugin 化** — ローカル hook だと別マシン再現性がない。
  `/plugin install verify-cloud-account@mao-worktools` で配布できるように
  plugin 化

## 既知制限 (0.2.0 時点)

- `gh auth status` / `firebase use` / `aws sts` / `gcloud config` /
  `kubectl config` の**出力フォーマット**に依存している。CLI 本体の major
  update で壊れる可能性あり
- `bash -c '...'` / `eval` / `python -c` / `sh -c` 内に埋め込まれた CLI 呼び出し
  は静的解析できず検証対象外 (透過 wrapper のリストに入れていないため
  素通り = allow)
- subshell 内のコマンド (`FOO=$(gh ...) cmd` の内側の `gh ...`) は検証対象外
  (値に `$(` を含む代入は保守的に stop するため)
- `kubectl --context foo ...` のような実行時コンテキスト override は検出しない
  (`kubectl config current-context` が返すデフォルトコンテキストだけを照合)
- Firebase の alias object 形式は `.firebaserc` の `projects` マップとの
  対応を前提にするが、key 名 (alias 名) のバリデーションはしない
- AWS の profile 別期待値指定は未対応。単一アカウント ID のみ
- Windows 未対応 (ハードコード想定なし、Bash hook が Windows では別挙動)

## リリース手順

1. `.claude-plugin/plugin.json` の version を semver で bump
2. `README.md` のリリースノート (あれば) を更新
3. `claude plugin validate .` で warning 0 を確認
4. `python3 -m unittest discover hooks/verify-cloud-account/tests` で全 green
5. `../../../.tools/validate-all.sh` で marketplace 全体の健全性を確認
6. commit + tag + push

## 依存関係

Python 3.9+ (annotations / `tuple[Path | None, bool]` 型ヒント)。
標準ライブラリのみ。`pip install` 不要。
