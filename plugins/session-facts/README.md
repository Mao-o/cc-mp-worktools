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

### 独自 agent への拡張

同梱の `SubagentStart` hook は `matcher: "Explore"` / `matcher: "Plan"` の 2 つに固定
されている。他のカスタム subagent (`.claude/agents/` 配下に自作したもの) の起動時にも
同じ facts 注入を効かせたい場合、公式の `matcher` はビルトイン名だけでなくカスタム
subagent の frontmatter `name` フィールドの値にもマッチできる (ファイル名ではない)。
`~/.claude/settings.json` (または project の `.claude/settings.json`) に自分で
同じコマンドを登録すればよい。例えば `name: MyAgent` という agent なら:

```json
{
  "hooks": {
    "SubagentStart": [
      {
        "matcher": "MyAgent",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /absolute/path/to/plugins/session-facts/hooks/session-facts --format markdown --include-domain-types --emit subagent-json",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

`${CLAUDE_PLUGIN_ROOT}` は plugin 同梱の hooks.json 内でこの plugin 用に解決される
変数なので、settings.json に直接書く場合は展開に頼らず絶対パスを使う (自動注入された
`- more:` 行に出る `invoked_as` の絶対パスがそのまま使える)。`--emit subagent-json` は
SubagentStart で plain stdout がモデルに届かないための必須フラグ (前述の理由を参照)。

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

`detectors/` の各ファイルが `ctx.stack` にタグを追加する (priority 昇順で実行、複数
detector が同じ repo にヒットしてよい)。

| 判定ファイル | stack タグ | 判定条件 |
|---|---|---|
| `mise.py` | `mise` | mise config (`.mise.toml` / `mise.toml` / `.config/mise/config.toml` / `.tool-versions` (asdf 形式も mise が解釈する)。`$HOME` 直下の XDG グローバル設定は除外) |
| `node_typescript.py` | `node`, `typescript` | `package.json`、`tsconfig(.base).json`、または依存に `typescript` |
| `claude_plugin.py` | `claude-code-marketplace`, `claude-code-plugin`, `hooks`/`skills`/`agents`/`commands` | `marketplace.json` / `.claude-plugin/marketplace.json` / `.claude-plugin/plugin.json` + 各サブディレクトリの存在 |
| `deno.py` | `deno` | `deno.json(c)` |
| `nextjs.py` | `nextjs` | 依存に `next`、または `next.config.*` |
| `react_vite.py` | `react`, `vite` | 依存に `react`、`vite.config.(ts\|js)` |
| `firebase.py` | `firebase`, `firebase-functions` | `firebase.json`/`.firebaserc`、npm の `firebase`/`firebase-admin`/`firebase-functions`/`@firebase/*`、Python の `firebase-admin`/`firebase-functions` (pyproject.toml / requirements*.txt。依存名の完全一致で判定)、Flutter の `firebase_core` (pubspec.yaml の `dependencies`/`dev_dependencies`/`dependency_overrides` 直下のみ判定。`flutter:` 等の無関係なセクションや深いネストにある同名キーは対象外) |
| `prisma.py` | `prisma` | 依存に `prisma`/`@prisma/client`、または `prisma/` ディレクトリ |
| `python_stack.py` | `python`, `uv`, `poetry`, `fastapi`, `django`, `flask`, `pytest` | `pyproject.toml` の有無 (無ければ `.py` ファイル比率で代替判定)、`uv.lock`/`uv.toml`/`poetry.lock`、pyproject 内の framework 名 |
| `testing.py` | `zod`, `vitest`, `jest`, `playwright`, `cypress`, `monorepo` | 各種依存 / config file、`pnpm-workspace.yaml`・`turbo.json` |
| `go_stack.py` | `go` | `go.mod` |
| `swift_stack.py` | `swift` | `Package.swift`、または `*.xcodeproj`/`*.xcworkspace` + tracked な `.swift` ファイルが 1 件以上 (Package.swift の無い Xcode-only project。`.swift` が無い Objective-C 専用 project は Xcode bundle があっても `swift` にならない) |
| `java_stack.py` | `java`, `gradle`, `maven` | `gradlew`/`build.gradle(.kts)`、`pom.xml` |
| `scala_stack.py` | `scala` | `build.sbt` |
| `rust_stack.py` | `rust` | `Cargo.toml` |
| `elixir_stack.py` | `elixir` | `mix.exs` |
| `ruby_stack.py` | `ruby` | `Gemfile` |
| `dotnet_stack.py` | `dotnet` | ルート直下の `*.csproj`/`*.fsproj`/`*.vbproj`/`*.sln`/`*.slnx` |
| `taskrunner.py` | `makefile`, `justfile`, `taskfile`, `nx` | `Makefile`、`Justfile`/`justfile`、`Taskfile.y(a)ml`、`nx.json` |
| `flutter.py` | `flutter`, `dart` | `pubspec.yaml` (`sdk: flutter` または `flutter:` セクション。それ以外の pubspec は `dart` のみ) |
| `php_stack.py` | `php` | `composer.json` |
| `cmake_stack.py` | `cmake` | `CMakeLists.txt` (パッケージマネージャ扱いはせず Likely Commands への提案も無し。ビルドディレクトリ/generator の慣習が定まらないため) |
| `docker.py` | `docker` | `Dockerfile` / `docker-compose.y(a)ml` / `compose.y(a)ml` |

依存の中身 (バージョン付き一覧) は上記のスタックタグとは別に `major_dependencies` /
`## Repo-Specific Notes` 側で収集する。Python は `pyproject.toml` /
`requirements*.txt` / `Pipfile` / `setup.cfg` の主要依存を横断的に見る。Makefile の
conventional target (`make test` 等) は `## Likely Commands` へ反映される。
`scala`/`elixir`/`swift`/`dotnet` の Likely Commands (`sbt test` 等) は他スタック
と異なり検出された `ctx.stack` を根拠にする (repo 直下の primary package manager
が別スタック、例えば `package-lock.json` と `App.sln` の同居で npm になっていても
`dotnet test` は出る)。ただし `swift` はその中でも例外で、`ctx.stack` に `swift`
があるだけでは `swift build`/`swift test` を出さない -- `Package.swift` が実在する
ときだけ出す。`*.xcodeproj`/`*.xcworkspace` のみの Xcode-only project (SwiftPM の
package manifest が無い) はこの条件を満たさないため、Likely Commands に何も足さ
ない (`xcodebuild` は `-scheme` の明示指定が要り、安全に推定できないため)。

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

本体の `__main__.py` は以下の引数を受け付ける。hook から呼ぶときの実際のコマンドは
冒頭の表と「ベースコマンド」を参照 (Claude Code 向け `hooks/hooks.json` は、共通の
`--format markdown --include-domain-types` に加えて timing ごとに
`--no-recent-commits` または `--emit subagent-json` を付ける)。Codex 向け
`hooks/codex-hooks.json` はこのベースコマンドをそのまま (追加フラグ無しで) 使う。

| オプション | デフォルト | 内容 |
|---|---|---|
| `--root` | `Path.cwd()` | 解析対象のリポジトリ内パス (git root 自動解決) |
| `--format` | `markdown` | `markdown` のみ (唯一の受理値。機械可読な出力が要る場合は `--emit subagent-json` を使う) |
| `--tree-depth` | (auto) | 固定深さを強制する override。未指定なら動的に自動選択 |
| `--min-tree-depth` | 1 | 動的選択の下限 |
| `--max-tree-depth` | 5 | 動的選択の上限 |
| `--max-tree-lines` | (定数) | ツリーの最大行数 (この行数を超えない最深 depth を採用) |
| `--max-service-entries` | (定数) | Service entry 最大数 |
| `--max-script-entries` | (定数) | scripts セクション最大数 |
| `--max-env-keys` | (定数) | env キー最大数 |
| `--max-notes` | (定数) | Notes セクション最大数 |
| `--max-major-deps` | 8 | 主要依存表示数 |
| `--max-output-chars` | 8,000 | 出力全体の文字数上限 (下記「出力サイズ上限」参照) |
| `--include-domain-types` | false | ドメイン型 Collector を有効化 |
| `--max-domain-types` | 10 | ドメイン型最大数 |
| `--include-hub-files` | false | Hub Files Collector を有効化 (被参照数ランキング) |
| `--max-hub-files` | 8 | Hub Files 最大数 |
| `--no-recent-commits` | false | `recent_commits` 行を抑制 (gitStatus を注入する main セッション向け) |
| `--force-walk` | false | 非 git かつ project marker が無いディレクトリでもフルの走査解析を強制 (下記「非プロジェクトディレクトリ」参照) |
| `--emit` | `stdout` | 出力エンベロープ。`subagent-json` で SubagentStart 用 `hookSpecificOutput` JSON に包む |

既定値の実体は `core/constants.py` を参照。

### 非プロジェクトディレクトリでの挙動

非 git かつ `--root` 直下に project marker (`package.json` / `pyproject.toml` /
`go.mod` / `Cargo.toml` / `pubspec.yaml` / `Makefile` 等。全量は
`core/constants.py` の `PROJECT_MARKERS` を参照) が一つも無い場合、通常の
走査・解析は行わず次の最小ヘッダーのみを出力する:

```markdown
## Project Facts
- repo_root: /path/to/analyzed/dir
- git_repo: false
- no project markers found; facts skipped
- more: run `python3 <invoked_as> --root /path/to/analyzed/dir --force-walk` to force the full analysis anyway
```

`$HOME` や `Desktop` など、質問目的でコーディング用途以外のディレクトリから
起動したときに、無関係なファイル/ディレクトリ名から作られる無意味な
facts (Structure・Test Snapshot 等 100 行超) が注入されるのを避けるため。
解析対象ディレクトリ (`repo_root`) と、従来どおりフル解析したい場合の
`--force-walk` フラグは、この最小ヘッダー自身にも常に記載される。この
最小ヘッダーも次節の「出力サイズ上限」と同じ `_enforce_output_budget()`
を経由するため、`--max-output-chars` に極端に小さい値を指定した場合でも
その上限を超えない。

git repo (`.git` 検出済み) の場合はこの判定自体が働かない — project marker
の有無に関わらず常にフル解析する (git ls-files ベースの解析は元々軽量なため)。

### 出力サイズ上限

Claude Code の `SessionStart` hook が plain stdout / `additionalContext` として
注入できる分量には実際の上限 (約 10,000 文字) があり、超過分はファイル保存 +
プレビュー置換にフォールバックする (ハーネス自身の挙動)。`--max-output-chars`
(既定 8,000) はこの上限より小さく設定してあり、通常は facts バンドル全体が
そのまま注入される側に倒す。

上限を超える場合は優先度の低いセクションから段階的に削る:
`## Subtree` / `## Structure` の末尾行 (cwd スコープ時は構造セクションが
depth=1 に自己縮小し本体が `## Subtree` に移るため、両方が対象) →
`## Scripts` → `## Env Keys` → `## Repo-Specific Notes` の順で、削った
場合は末尾に `... (truncated)` を付ける。`## Test Snapshot` /
`## Service Entry Points` / `## Likely Commands` はこの段階的削減の対象外
(agent が自力で再構成しにくい情報のため)。

`--emit subagent-json` は JSON エンベロープ・エスケープ分だけ `--max-output-chars`
の外側にバイト数が増える。ハーネス側の 10,000 文字上限がペイロード全体
(エンベロープ込み) にかかる場合は、その分の余白を見込んだ値に調整すること。

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
- **標準ライブラリのみ** — `pip install` 不要。3.11 以降を想定

## 互換性

- Claude Code CLI 2.1.100+
- Python 3.11+ (標準ライブラリのみ)
- macOS / Linux (`git ls-files` が使えれば動作)

## ログ

hook 自身はログを書かない。出力は stdout のみ。SessionStart では plain stdout が
そのままコンテキストに入り、SubagentStart では `--emit subagent-json` による
`hookSpecificOutput.additionalContext` JSON を Claude Code 側が消費する。

## リリース手順

session-facts は Claude Code 向け (`.claude-plugin/plugin.json`) と Codex 向け
(`.codex-plugin/plugin.json`) の 2 つの manifest を持つ。**version は必ず両方を
同時に bump し、同じ値にする** (CI が不一致を検知して落とす)。

1. `.claude-plugin/plugin.json` と `.codex-plugin/plugin.json` の両方で
   `version` を bump する (同じ値にする。片方だけ更新すると CI が落ちる)
2. 挙動変更・機能追加を伴うコミットは、同じ batch で [CHANGELOG.md](./CHANGELOG.md)
   を更新する (実装だけ先に入れて記録を後回しにしない)
3. `claude plugin validate plugins/session-facts` で warning ゼロを確認する

`description` は Codex 側がプラットフォーム固有の記述 (トリガーとなる hook イベント等)
を持つため文言を完全一致させる必要はないが、明らかに古い記述のまま放置しない。
