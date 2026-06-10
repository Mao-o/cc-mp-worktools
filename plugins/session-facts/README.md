# session-facts

セッション開始時にリポジトリを分析し、スタック・スクリプト・env キー・ドメイン型などの
**コンパクトなファクトバンドル** を Markdown で生成してエージェントのコンテキストに注入する hook。

| タイミング | 用途 | コマンド差分 |
|---|---|---|
| `SessionStart` | 本体セッション開始時に 1 回だけ注入 | `--no-recent-commits` 付き |
| `SubagentStart` (matcher: `Explore`) | 探索系サブエージェント起動時に注入 | `--emit subagent-json` 付き |
| `SubagentStart` (matcher: `Plan`) | 設計系サブエージェント起動時に注入 | `--emit subagent-json` 付き |

ベースコマンド (timeout は各 hook で 15 秒):

```
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/session-facts --format markdown --include-domain-types
```

### タイミング別コマンド差分の理由

- **`SessionStart` の `--no-recent-commits`** — Claude Code CLI 本体が main セッションの
  system prompt に gitStatus (current branch / 直近 commit / status) を常時注入するため、
  recent_commits を重ねると同一事実の二重ソースになる (実セッションで重複を確認済み)。
  この注入は hook 機構ではなく system prompt 構築の一部のため hooks ドキュメントには
  載っていない。gitStatus 注入が無い環境で使う場合は hooks.json から
  `--no-recent-commits` を外せば従来出力に戻る (CLI 単体のデフォルトは出力する側)。
- **`SubagentStart` の `--emit subagent-json`** — SubagentStart では plain stdout が
  モデルに届かない (公式仕様: plain stdout の自動注入は SessionStart のみ)。
  `hookSpecificOutput.additionalContext` JSON に包んで注入する。subagent には
  gitStatus が注入されないため、こちらの recent_commits は維持する。

## インストール

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install session-facts@mao-worktools
```

有効化すると `SessionStart` と `SubagentStart(Explore|Plan)` の hook が自動登録される。
`settings.json` を手で編集する必要はない。

開発時はローカルパスを直接ロードする方が速い:

```bash
claude --plugin-dir /path/to/cc-mp-worktools/plugins/session-facts
```

## 挙動

起動時に以下のパイプラインを実行し、Markdown 文字列を stdout に出力する。

1. `git ls-files` (fallback: filesystem walk) で tracked files 一覧を取得
2. パッケージマネージャ検出 (mise / pnpm / bun / npm 等)
3. Purpose 推定 (package.json `description` → README 先頭行。どちらも無ければ field ごと省略)
4. **Detector** 群 (priority 昇順) を走らせて stack 情報を蓄積
5. **Collector** 群 (priority 昇順) を走らせて各セクションの Markdown を生成
6. ヘッダーと各セクションを結合して出力

出力例 (抜粋):

```markdown
## Project Facts
- purpose: a monorepo of web apps and shared packages
- repo_root: /absolute/path/to/repo
- stack: typescript, next, python
- major_dependencies: next@15.1, react@19.0, firebase@11.0
- branch: feat/login (ahead 3, behind 1 vs origin/main)
- recent_commits:
  - a1b2c3d feat(auth): add login flow (8 hours ago)
  - d4e5f6g fix(api): handle empty payload (1 day ago)

## Structure (dirs only, depth=4)
├── apps/web/src/
└── packages/
    ├── core/
    └── ui/

## Service Entry Points
- apps/web/src/api/users/route.ts

## Test Snapshot
- code_files: 38
- test_files: 8
- test_to_code_ratio: 0.21
- test_dir: packages/*/tests
```

各 Collector が出すセクション粒度は `--max-*` 引数で制限可能。

### ツリー描画の挙動

- **深さは行数に応じて自動調整** (depth 1〜5)。`--max-tree-lines` を超えない範囲で
  最も深い depth を採用し、薄い repo では深く、巨大 repo では浅く出る。採用 depth は
  見出しの `depth=N` に反映される
- **子が 1 つだけの中間ディレクトリは `a/b/c/` に圧縮** して 1 行にまとめる
- **進行情報** (`branch` / `recent_commits`) は git repo のとき自動付与。デフォルト
  ブランチ (main/master) で upstream と差分が無いときは `branch` 行を省略する。
  同梱 hooks.json の `SessionStart` は `--no-recent-commits` により recent_commits を
  抑制する (前述のとおり harness の gitStatus と重複するため)

### 検出できるスタック / 依存

- **JS/TS**: package.json (deps / scripts / package manager)
- **Python**: pyproject.toml / **requirements*.txt / Pipfile / setup.cfg** の主要依存
- **Flutter/Dart**: pubspec.yaml (`stack: flutter, dart` + firebase_core / riverpod 等)
- **Go**: go.mod
- **タスクランナー**: Makefile の conventional target (`make test` 等) を Likely Commands へ

### cwd != repo_root のとき (monorepo / サブプロジェクト構成)

呼び出し時の cwd が repo_root と異なる場合 (cwd が repo_root 配下のサブディレクトリ
の場合)、エージェントが「リポジトリ全体」と「カレントの作業範囲」を区別できるよう、
2 つの情報を**追加**する (既存ブロックは変更しない)。

1. ヘッダーに `- cwd: <relative path> (subdirectory of repo_root)` 行
2. `## Subtree (cwd: <relative path>, dirs only, depth=N)` ブロックを `## Structure` の直後に挿入

subtree モードでは repo 全体の `## Structure` は **top-level ディレクトリ名のみ
(depth=1)** に圧縮され、詳細は cwd 配下の `## Subtree` 側に寄せる (横断作業の地図と
しての最小限を保ちつつトークンを節約)。

```markdown
## Project Facts
- repo_root: /absolute/path/to/my-monorepo
- cwd: packages/core (subdirectory of repo_root)

## Structure (dirs only, depth=1)
├── apps/
├── packages/
└── services/

## Subtree (cwd: packages/core, dirs only, depth=4)
└── src/
    └── domain/
```

cwd == repo_root のときはどちらも出力されず、従来挙動と完全に一致する。
`Service Entry Points` などの既存ブロックは引き続きリポジトリ全体スコープで生成され、
横断的な作業のニーズも維持される。

## CLI オプション

本体の `__main__.py` は以下の引数を受け付ける (hook から呼ぶときは `--format markdown
--include-domain-types` のみ指定)。

| オプション | デフォルト | 内容 |
|---|---|---|
| `--root` | `Path.cwd()` | 解析対象のリポジトリ内パス (git root 自動解決) |
| `--format` | `markdown` | `markdown` / `json` / `human` |
| `--tree-depth` | (auto) | 固定深さを強制する override。未指定なら動的に自動選択 |
| `--min-tree-depth` | 1 | 動的選択の下限 |
| `--max-tree-depth` | 5 | 動的選択の上限 |
| `--max-tree-lines` | (定数) | ツリーの最大行数 (この行数を超えない最深 depth を採用) |
| `--max-service-entries` | (定数) | Service entry 最大数 |
| `--max-script-entries` | (定数) | scripts セクション最大数 |
| `--max-env-keys` | (定数) | env キー最大数 |
| `--max-notes` | (定数) | Notes セクション最大数 |
| `--max-major-deps` | 8 | 主要依存表示数 |
| `--include-domain-types` | false | ドメイン型 Collector を有効化 |
| `--max-domain-types` | 10 | ドメイン型最大数 |
| `--no-recent-commits` | false | `recent_commits` 行を抑制 (gitStatus を注入する main セッション向け) |
| `--emit` | `stdout` | 出力エンベロープ。`subagent-json` で SubagentStart 用 `hookSpecificOutput` JSON に包む |

既定値の実体は `core/constants.py` を参照。

## カスタム detector / collector の書き方

`hooks/session-facts/custom/*.py` にモジュールを置くと `registry.discover_custom_plugins()`
が起動時に動的 import する。各モジュールは `register()` を公開する。

### Detector

```python
class MyDetector:
    name = "my_detector"
    priority = 50

    def detect(self, ctx):
        return ["my_stack"]

def register():
    return MyDetector()
```

### Collector

```python
class MyCollector:
    name = "my_collector"
    section_title = "## My Section"
    priority = 50

    def should_run(self, ctx):
        return True

    def collect(self, ctx):
        return f"{self.section_title}\n- hello"

def register():
    return MyCollector()    # リストで複数返しても良い
```

### priority 採番ガイド

| 帯域 | 用途 |
|---|---|
| 1-10 | 基盤ツール (mise, node, python 等) |
| 11-30 | フレームワーク・ランタイム |
| 31-60 | ライブラリ・サービス層 |
| 61-90 | 分析・観察系 |
| 91-99 | インフラ・ツールチェーン |

詳細は [hooks/session-facts/CLAUDE.md](./hooks/session-facts/CLAUDE.md) (実装者向けガイド)。

## 拡張ポイントの運用方針

- `custom/` は `.gitkeep` のみ同梱。本 plugin を `/plugin install` すると cache 配下に
  コピーされるため、cache 内 custom/ を直接編集してもアップデートで消える
- カスタム detector/collector を永続化したい場合は **この plugin を fork して** `custom/`
  に足すか、`detectors/` / `collectors/` に直接モジュールを追加する
- 固有ドメインロジックは plugin に混ぜず、別 plugin として新規作成するのも選択肢

## 設計上のトレードオフ

- **ファイル探索は `git ls-files` ベース** — ファイルシステム直接走査は避け、tracked files
  のみ対象。`.gitignore` されたファイルは無視される
- **出力はコンパクト優先** — エージェントのコンテキストを消費するため、`--max-*` 引数で
  常に上限を持つ。全量ダンプは非目的
- **敵対的入力は非対象** — リポジトリ内容が信頼できる前提。prompt injection を仕掛けた
  README 等への防御はしない
- **標準ライブラリのみ** — `pip install` 不要。3.8 以降を想定

## 互換性

- Claude Code CLI 2.1.100+
- Python 3.8+ (標準ライブラリのみ)
- macOS / Linux (`git ls-files` が使えれば動作)

## ログ

hook 自身はログを書かない。出力は stdout のみ。SessionStart では plain stdout が
そのままコンテキストに入り、SubagentStart では `--emit subagent-json` による
`hookSpecificOutput.additionalContext` JSON を Claude Code 側が消費する。
