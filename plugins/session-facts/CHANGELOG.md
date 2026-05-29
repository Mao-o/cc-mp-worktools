# Changelog

## 0.4.0

**出力品質改善 (v0.4 ロードマップ P1〜P3、計 10 件)**。SessionStart injection の
選定基準 (`~/.claude/rules/claude/sessionstart-injection.md`) に沿って、出力の
圧縮・ノイズ削減・言語サポート拡張・進行情報の追加を行った。session-facts 初の
ユニットテスト一式 (77 件) を同梱。

### P1: 出力圧縮・ノイズ削減

1. **Structure ツリーの dynamic depth** (`core/tree.py`, `collectors/structure.py`) —
   固定 depth=3 をやめ、`--max-tree-lines` を超えない範囲で最も深い depth を
   自動選択 (1〜5)。`build_dir_tree` は MAX で 1 回だけ走らせ、`render_tree` を
   depth ごとに試して超過直前を採用する方式 (行数は depth に対し単調増加)。
   採用 depth は見出し `## Structure (dirs only, depth=N)` に反映。
2. **subtree モードの Structure 圧縮** (`collectors/structure.py`) —
   cwd != repo_root のとき、repo 全体の Structure は top-level dir 名のみ
   (depth=1) に圧縮し、詳細は cwd 配下の Subtree 側に寄せる。
3. **chain 圧縮** (`core/tree.py::render_tree`) — 子が 1 つだけの中間ディレクトリ
   を `a/b/c/` の 1 行に畳む。行数を稼ぎつつ可読性を維持。
4. **Repo-Specific Notes の汎用 note 抑制** (`collectors/repo_notes.py`) —
   `api-related files...` の閾値を `>= 5` から `>= 20` に引き上げ。ほぼ全 repo で
   出てノイズ化していたため、本当に api-heavy な repo でのみ出すようにした。
5. **test_dir の共通祖先集約** (`core/util.py::aggregate_paths`, `collectors/tests.py`) —
   sibling な test ディレクトリを `plugins/*/hooks/*/tests` のような glob 1 行に
   集約。単一の test_dir はそのまま個別表示。

### P2: 言語サポート拡張

6. **Flutter/Dart 対応** (`detectors/flutter.py` 新規, `core/constants.py`,
   `collectors/dependencies.py`, `collectors/scripts.py`) — pubspec.yaml 検出で
   `stack: flutter, dart`、`.dart` を CODE_EXTENSIONS に追加 (Service Entry Points /
   Test Snapshot に反映)、pubspec の主要依存 (firebase_core / riverpod / dio 等) を
   major_dependencies に、`flutter pub get` / `flutter run` / `flutter test` を
   Likely Commands に追加。
7. **Python requirements/Pipfile/setup.cfg の依存取得** (`collectors/dependencies.py`) —
   従来 pyproject.toml のみだった major_dependencies を requirements*.txt /
   Pipfile / setup.cfg からも取得 (優先度 pyproject > Pipfile > requirements >
   setup.cfg)。celery / alembic / redis / gunicorn / httpx 等を IMPORTANT_DEPENDENCIES
   に追加。
8. **Makefile target の抽出** (`core/makefile.py` 新規, `collectors/scripts.py`) —
   `make` 1 行だけでなく、Makefile の conventional target (`make test` /
   `make build` / `make dev` 等) を優先度順に Likely Commands へ。変数代入・
   `.PHONY`・recipe 行・非定型 target は除外。

### P3: 価値の高い情報の追加

9. **git 進行情報** (`core/git.py`, `collectors/git_progress.py` 新規,
   `renderer.py`) — Project Facts に `branch` (ahead/behind vs upstream) と
   `recent_commits` (直近 3 件、subject + 相対日時) を追加。デフォルトブランチ
   (main/master) で差分が無いときは branch 行を省略。detached HEAD / upstream
   無し / 非 git は silent skip。
10. **Domain Types 検出のパス緩和** (`collectors/domain_types.py`) —
    対象パスに `/repositories/` `/services/` `/schemas/` `/dto/` 等を追加、
    走査をファイル先頭 200 行に限定、stop_names 拡張 + infra suffix
    (`*Repository` / `*Service` 等) 除外、「unique 型名 5 個以上」を表示条件に
    追加してノイズを抑制。クラスタ判定は打ち切り前の件数で行うため、
    `--max-domain-types` を 5 未満にしても (リポジトリが実際に 5 型以上持つなら)
    型が表示される。

### その他

- **テスト新設** — `hooks/session-facts/tests/` に unittest 一式 (77 件)。
  `python3 -m unittest discover tests` で実行。`_testutil.py` / `conftest.py` が
  sys.path を整備。
- **CLI** — `--tree-depth` は固定深さの override (未指定で dynamic)、
  `--min-tree-depth` / `--max-tree-depth` を追加。未使用化した `DEFAULT_TREE_DEPTH`
  定数を削除。
- **後方互換** — 既存の出力構造・既定挙動は維持。dynamic depth と chain 圧縮は
  全 repo に効くが、セクション構成は不変。

### 見送り

- **P4 (`--exclude-if-in-claudemd`)** — CLAUDE.md 既出情報の出力抑止モードは、
  受け入れ基準に複数プロジェクトでの false-positive ゼロ検証を含むため将来対応に
  見送り。
