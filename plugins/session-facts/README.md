# session-facts

セッション開始時にリポジトリを分析し、スタック・スクリプト・env キー・ドメイン型などの
**コンパクトなファクトバンドル** を Markdown で生成してエージェントのコンテキストに注入する hook。

| タイミング | 用途 |
|---|---|
| `SessionStart` | 本体セッション開始時に 1 回だけ注入 |
| `SubagentStart` (matcher: `Explore`) | 探索系サブエージェント起動時に注入 |
| `SubagentStart` (matcher: `Plan`) | 設計系サブエージェント起動時に注入 |

すべて同一コマンド:

```
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/session-facts --format markdown --include-domain-types
```

timeout は各 hook で 15 秒。

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
3. Purpose 推定 (package.json `description` → README 先頭行 → ディレクトリ名)
4. **Detector** 群 (priority 昇順) を走らせて stack 情報を蓄積
5. **Collector** 群 (priority 昇順) を走らせて各セクションの Markdown を生成
6. ヘッダーと各セクションを結合して出力

出力例 (抜粋):

```markdown
## Project Facts
- purpose: cc-marketplaces
- repo_root: /absolute/path/to/repo
- git_repo: false (using filesystem walk)

## Structure (dirs only, depth=3)
├── private/
│   └── plugins/
└── worktools/
    └── plugins/

## Service Entry Points
- private/plugins/ai-game/skills/.../route.ts

## Test Snapshot
- code_files: 38
- test_files: 8
- test_to_code_ratio: 0.21
```

各 Collector が出すセクション粒度は `--max-*` 引数で制限可能。

## CLI オプション

本体の `__main__.py` は以下の引数を受け付ける (hook から呼ぶときは `--format markdown
--include-domain-types` のみ指定)。

| オプション | デフォルト | 内容 |
|---|---|---|
| `--root` | `Path.cwd()` | 解析対象のリポジトリ内パス (git root 自動解決) |
| `--format` | `markdown` | `markdown` / `json` / `human` |
| `--tree-depth` | 3 | ディレクトリツリーの深さ |
| `--max-tree-lines` | (定数) | ツリーの最大行数 |
| `--max-service-entries` | (定数) | Service entry 最大数 |
| `--max-script-entries` | (定数) | scripts セクション最大数 |
| `--max-env-keys` | (定数) | env キー最大数 |
| `--max-notes` | (定数) | Notes セクション最大数 |
| `--max-major-deps` | 8 | 主要依存表示数 |
| `--include-domain-types` | false | ドメイン型 Collector を有効化 |
| `--max-domain-types` | 10 | ドメイン型最大数 |

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

hook 自身はログを書かない。出力は stdout のみ (Claude Code 側が SessionStart の
`additionalContext` として消費する)。
