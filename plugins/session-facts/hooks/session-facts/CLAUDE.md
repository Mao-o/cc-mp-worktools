# session-facts

リポジトリを分析し、セッション開始時にエージェントへ注入するコンパクトなファクトバンドルを生成するツール。

## アーキテクチャ

```
cli.py          — オーケストレーション（引数解析 → 検出 → 収集 → 出力）
registry.py     — プラグイン自動検出（detectors/ collectors/ custom/）
renderer.py     — ヘッダー（## Project Facts）のレンダリング
__main__.py     — エントリポイント（sys.path 設定 + cli.main()）

core/
  context.py    — RepoContext データクラス（全フェーズ共有）
  constants.py  — 定数・デフォルト値
  pm.py         — パッケージマネージャ検出
  fs.py         — ファイル I/O ユーティリティ
  git.py        — Git コマンドラッパー
  tree.py       — ディレクトリツリー構築・描画
  util.py       — テキスト正規化・パス判定

detectors/      — スタック検出プラグイン
collectors/     — セクション収集プラグイン
custom/         — ユーザー拡張プラグイン（.gitkeep）
```

## 実行フロー

1. `git ls-files` → `ctx.tracked_files`
2. パッケージマネージャ検出 → `ctx.results["package_manager"]`
3. Purpose 推定 → `ctx.results["purpose"]`
4. Detector（priority 昇順）→ `ctx.stack` に追加
5. Collector（priority 昇順）→ セクション生成 + `ctx.results` への書き込み
6. ヘッダーレンダリング → セクション結合 → 出力

## プラグイン規約

### Detector

```python
class MyDetector:
    name = "my_detector"      # 一意な識別子
    priority = 50             # 小さいほど先に実行

    def detect(self, ctx: RepoContext) -> List[str]:
        # ctx.stack に追加される文字列のリストを返す
        return ["my_stack"]

def register():
    return MyDetector()
```

### Collector

```python
class MyCollector:
    name = "my_collector"
    section_title = "## My Section"   # Markdown 見出し
    priority = 50

    def should_run(self, ctx: RepoContext) -> bool:
        return True  # 実行条件

    def collect(self, ctx: RepoContext) -> Optional[str]:
        # Markdown 文字列を返す（section_title を含む）
        # ctx.results に値を入れるとヘッダーに反映できる
        return None

def register():
    return MyCollector()           # リストで複数返すことも可
```

### Priority 採番ガイド

| 帯域 | 用途 | 例 |
|------|------|-----|
| 1-10 | 基盤ツール（mise, node, python 等の検出） | mise=5, node_typescript=10 |
| 11-30 | フレームワーク・ランタイム | deno=12, nextjs=20 |
| 31-60 | ライブラリ・サービス層 | python_stack=50, java_stack=55 |
| 61-90 | 分析・観察系 | tests=60, domain_types=80 |
| 91-99 | インフラ・ツールチェーン | taskrunner=92, docker=95 |

Collector も同様の考え方で、dependencies=5 → structure=10 → ... → likely_commands=90 の順。

### register() の規則

- 各モジュールは `register()` 関数を公開する
- 単一インスタンスまたはリストを返す（例: `scripts.py` は `[ScriptsCollector(), LikelyCommandsCollector()]`）
- `_` 始まりのモジュール（`_base.py`）は自動読み込みされない

## core/ モジュールの責務

| モジュール | 置くもの | 置かないもの |
|-----------|---------|------------|
| `context.py` | RepoContext のフィールド・プロパティ | ビジネスロジック |
| `constants.py` | 全体で共有する定数・デフォルト値 | ロジックを含む関数 |
| `pm.py` | パッケージマネージャ検出ロジック | PM 以外の検出 |
| `fs.py` | ファイル読み書きの安全ラッパー | パス判定ロジック |
| `git.py` | Git コマンド実行 | Git 結果の解釈 |
| `util.py` | 汎用ヘルパー（テキスト処理、パス判定） | 特定ドメインのロジック |

新しい共有ロジックが必要な場合は `core/` に専用モジュールを作る。`cli.py` にベタ書きしない。

## 開発時の注意

- 出力はエージェントのコンテキストを消費するため、常にコンパクトさを意識する
- `--max-*` 引数で出力量を制限可能にしておく
- ファイル探索は `ctx.tracked_files`（git ls-files ベース）を使う — ファイルシステム直接走査は避ける
- `ctx.results` はフェーズ間通信に使う辞書。Collector からヘッダーに情報を渡す際に利用
