# Changelog

## 0.8.0

**Likely Commands の根拠強化 / 出力サイズ上限 / 非プロジェクトディレクトリ抑止 /
test_dir 集約修正 (v0.8)**。2026-08 精査バックログの続き。不具合修正 4 件と
ドキュメント修正 1 件。

### 不具合修正

1. **test_dir 集約が全ワイルドカードに退化する問題を修正** (`core/util.py`) —
   `aggregate_paths()` は同じセグメント数のパス群を位置ごとに比較し、異なる
   位置を `*` に置き換えて集約するが、全位置が異なると `*/*` のような
   情報量ゼロのパターンになっていた (実測:
   `['api/tests','web/__tests__','sdks/spec']` → `['*/*']`)。集約結果に
   literal セグメントが一つも残らない場合は先頭ディレクトリで再分割して
   から集約し直し、なお集約できない残りは元パスのまま列挙する (上限 4 件 +
   `... (+N more)`)
2. **Likely Commands が根拠の無いコマンドを出す問題を修正**
   (`collectors/scripts.py`) — `docker compose up` は Dockerfile 単体でも
   出ていた (compose ファイル存在時のみに変更、単体は `docker build .`)。
   `mise install` は `.tool-versions` (asdf 形式) 単体でも出ていた (mise
   config 存在時のみに変更、単体は `asdf install`)。pytest 系コマンドは
   test_files の有無に関わらず無条件に出ていた (test_snapshot.test_files
   が実在する時のみに変更)。pytest が pyproject.toml の依存として検出され
   ない場合は、root 直下の `tests/` に `test_*.py` がある時に限り
   `python3 -m unittest discover tests` を代替提案する
3. **出力全体に文字数上限が無く harness の注入上限超過の恐れがあった問題を
   修正** (`cli.py`, `core/context.py`, `collectors/scripts.py`) —
   `AnalysisConfig` に `max_output_chars` (既定 8,000。CLI:
   `--max-output-chars`) を追加し、超過時は `## Structure` の末尾行 →
   `## Scripts` → `## Env Keys` → `## Repo-Specific Notes` の順で段階的に
   削る (`## Test Snapshot` / `## Service Entry Points` /
   `## Likely Commands` は対象外)。個々の script command も 120 文字で
   切り詰める
4. **非プロジェクトディレクトリ (ホーム等) で起動すると走査由来の無意味な
   facts を大量注入していた問題を修正** (`cli.py`, `core/constants.py`,
   `core/fs.py`, `core/runtime.py`) — 非 git かつ root 直下に project
   marker (`package.json`/`pyproject.toml`/`Makefile` 等) が一つも無い
   場合は最小ヘッダー 2 行 (`git_repo: false` /
   `no project markers found; facts skipped`) のみを出力する
   (`--force-walk` で従来の無条件走査に戻せる)。あわせて
   `.config/mise/config.toml` (XDG グローバル設定) は root が $HOME の
   ときのみ除外するようにした (グローバルな tool pin が repo のものと
   誤認されるのを防ぐ)

### ドキュメント

5. **SKILL.md が Codex 専用の `${PLUGIN_ROOT}` を Claude Code 向けの案内
   としてそのまま書いていた問題を修正** (`skills/session-facts/SKILL.md`) —
   Claude Code は `${CLAUDE_PLUGIN_ROOT}`、Codex は `${PLUGIN_ROOT}` と
   両ハーネスを併記し、どちらも未定義な場合のフォールバック順序
   (環境変数 → 自動注入された `- more:` 行の絶対パス → SKILL.md 自身の
   場所を起点にした相対パス) を明確化した

## 0.7.0

**Hub Files / `--help` ポインタの正式反映 + 例外隔離・エンコーディング耐性 (v0.7)**。
0.6.0 の bump 後、version を上げないまま Hub Files collector と `- more:` ヒント /
`invoked_as` の 2 コミットが main に積み上がっていた (CHANGELOG・README・version が
未追従)。本 version でこれらを正式に取り込んで記載し、あわせて 2026-08 精査
バックログで見つかった不具合を修正する。

### 新機能 (0.6.0 bump 後、無 version のまま先行 merge されていたものを正式反映)

1. **Hub Files collector** (`core/imports.py` 新規, `collectors/hub_files.py` 新規) —
   opt-in `--include-hub-files` で、JS/TS・Python の import 文を正規表現で抽出して
   相対 import を解決し、被参照数の多い順に `## Hub Files` セクションへ表示する。
   件数上限は `--max-hub-files`。リファクタ前に「どのファイルが中心的か」を
   把握する用途で、AST は使わない軽量ヒューリスティック (tsconfig の path alias や
   node_modules 解決、複雑な Python package 構成は対象外)
2. **`- more:` --help ポインタ** (`core/context.py`, `renderer.py`, `cli.py`) —
   自動注入された Project Facts を読むだけでは opt-in オプションの存在に気付けない
   問題に対処。`RepoContext.invoked_as` (= `sys.argv[0]`) 経由で、ヘッダー末尾に
   実行可能な追加コマンドを案内するようにした。`SKILL.md` の description も
   フラグ列挙をやめ what/when の記述に戻した

### 不具合修正 (2026-08 精査バックログ)

3. **`- more:` ヒントの案内コマンドが実行不能だった問題を修正**
   (`renderer.py`, `core/context.py`, `skills/session-facts/SKILL.md`) —
   実際の hook 起動 (`python3 <dir>`) では `sys.argv[0]` がディレクトリを指すため、
   案内された `<invoked_as> --help` をそのまま実行すると `permission denied`
   になっていた (ディレクトリを直接実行しようとするため)。`python3 <invoked_as>
   --help` の形に修正し、`tests/test_cli.py` に実行可能な形で始まることを
   検証するテストを追加
4. **collector/detector の例外が隔離されず出力が丸ごと消える問題を修正**
   (`cli.py`, `registry.py`) — 1 つの detector/collector が例外を投げると
   traceback 付きで exit 1 になり、facts が全セクション消えていた。
   `detector.detect()` と `collector.should_run()`/`collect()` の呼び出しを
   それぞれ try/except で隔離し、失敗した plugin 名を `[session-facts] WARNING:
   ...` として stderr に出しつつ他のセクションは継続するよう変更。
   `registry.py` 側のモジュール import / `register()` 呼び出しも同様に隔離し、
   1 モジュールの読み込み失敗が同ディレクトリの他モジュールの検出を道連れに
   しないようにした。`summarize_repo()` 自体が上記以外の理由で失敗した場合の
   最終防御として、`main()` が最低限 `## Project Facts` ヘッダーのみ出力して
   exit 0 にするフォールバックも追加
5. **非 UTF-8 ファイル名・非 UTF-8 stdout ターゲットでの `UnicodeEncodeError` を修正**
   (`core/git.py`, `cli.py`) — `git ls-files` が返す非 UTF-8 バイト列を
   `errors="surrogateescape"` で decode するとローンサロゲート (U+DCxx) になり、
   標準出力への `print()` が `UnicodeEncodeError` で失敗して facts が丸ごと
   失われていた。decode を `errors="replace"` (表示用途のため元バイトへの
   往復は不要) に変更。あわせて `main()` 冒頭で
   `sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")` を
   行い、`PYTHONIOENCODING=ascii` のような非 UTF-8 stdout ターゲットでも
   ツリー罫線 (`├──` 等) や日本語テキストで落ちないようにした

### 保守

6. **Codex 向け manifest との version 統一 + bump 手順の明文化**
   (`.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `README.md`) —
   Claude 向けと Codex 向けの 2 manifest の version が乖離しうる (bump 漏れ事故)
   問題に対応。両 manifest の version を統一し、README に「リリース手順」節を
   新設して「2 manifest 同時 bump」を明記した。あわせて `.codex-plugin/plugin.json`
   の `interface.longDescription` (Codex 向け UI 表示文言) が Hub Files 機能を
   欠いたままだったのも更新し、既存の「ドメイン型」と同じ扱いに揃えた
7. **Python 互換性表記を 3.11+ に統一** (`README.md`,
   `skills/session-facts/SKILL.md`) — marketplace 全体の 3.11+ 方針
   (旧 3.8+ 表記からの統一) に合わせて修正
8. **CHANGELOG に 0.1.0〜0.3.0 を追補** — 本ファイル下部を参照。plugin.json の
   version 履歴はあったが CHANGELOG の記録が 0.4.0 からしか無かったため、
   git log から要約を補った。以後「機能コミット = version bump + CHANGELOG
   同時更新」を [hooks/session-facts/CLAUDE.md](./hooks/session-facts/CLAUDE.md)
   に明記し、本 version のような後追い記録を防ぐ

テスト 177 件 (新規 14 件: `tests/test_registry.py` 新設 (5 件、registry.py の
import/register 隔離)、`tests/test_fs.py` 新設 (4 件、非 UTF-8 decode と
stdout エンコーディング耐性)、`tests/test_cli.py` に `- more:` ヒント・
例外隔離・フォールバックのテストを追加 (5 件))。

## 0.6.0

**実行コンテキスト (runtime/venv) の可視化 + 依存収集のハイブリッド化 (v0.6)**。
実セッションで「kaggle は入っているか」を調べた際、`.venv` 内に kaggle が
インストールされていたにもかかわらず Project Facts に venv/依存/runtime の情報が
無く、エージェントがグローバル基準で「未インストール」と誤検出した。出力に
実行コンテキストと主要依存を載せ、グローバル基準での誤判定を防ぐ。

> **制約**: session-facts は `.venv`/`venv` 内部 (インストール済みパッケージ) を
> git ls-files でも walk_files でも見られない (SKIP_DIRS)。よって依存検出は
> `requirements.txt` / `pyproject.toml` 等の**宣言**経由のみ。venv は「存在」を
> `pyvenv.cfg` で確認し、エージェントに確認を促す形で伝える。

1. **runtime/venv 検出** (`core/runtime.py` 新規, `collectors/runtime_env.py` 新規,
   `detectors/mise.py`, `renderer.py`, `core/context.py`) — mise/asdf のツール
   バージョン (`.mise.toml` / 旧 `mise.toml` / `.config/mise/config.toml` /
   `.tool-versions`)、`.python-version`、`.venv`/`venv` の存在 (`pyvenv.cfg`
   ガードで `venv/` という名のソースディレクトリ誤検出を防止) を検出し、header に
   1 行追加:
   `- runtime: mise (python 3.12); venv .venv present (python 3.12.3); run tools via .venv/bin/`。
   mise detector は共有 `has_mise()` 経由に変更し、従来拾えなかったドット無し
   `mise.toml` と `.config/mise/` を認識するようになった (返り値 `["mise"]` は不変)。
2. **major_dependencies のハイブリッド化** (`collectors/dependencies.py`) —
   従来の allow-list (IMPORTANT_DEPENDENCIES) マッチのみだと kaggle 等の宣言依存が
   出なかった。allow-list マッチを最優先 (tier 0) に並べ、枠 (`--max-major-deps`,
   既定 8) が余れば `requirements`/`pyproject` の直接宣言 runtime 依存で埋め (tier 1)、
   dev 依存 (pytest 等) は後置 (tier 2) する方式に変更。枠埋めは **Python のみ**に
   スコープ (JS package.json / pubspec / go.mod は uncurated なため allow-list 据置)。
   cap は tier 安定ソート**後**に適用し、source 順で遅く現れる allow-list マッチが
   切られないようにした。あわせて pyproject を tomllib 不使用の table-scoped パーサ
   (`[project] dependencies` / `[project.optional-dependencies]` / poetry 各テーブル /
   Pipfile グループ) で正確化。副次的に、旧「どこでもマッチ」正規表現が poetry の
   dev グループ依存を runtime と誤表示していた潜在バグも解消した。
3. **Likely Commands の runtime 補正** (`collectors/scripts.py`) — venv があれば
   `.venv/bin/python -m pytest`、mise-python なら `mise exec -- python -m pytest` を
   出す (venv 優先)。`uv run` / `poetry run` は自前 env 管理のため不変。pyproject も
   lockfile も無い bare-python (.py 比率検出) でも、runner が確定するときだけ pytest
   行を補う。

テスト 138 件 (新規 48 件: `test_runtime.py` / `test_scripts.py` 新設、
`test_dependencies.py` にハイブリッド/parser テスト追加)。

### Codex plugin 兼用対応 (hook + skill)

session-facts を **Codex plugin としても**配布できるよう、エージェント非依存の
コア (`hooks/session-facts/`) を無改造のまま、Codex 用の配線レイヤを追加した。
Codex の hooks framework は Claude Code とほぼ同型 (`SessionStart` +
`hookSpecificOutput.additionalContext`、plain stdout も additionalContext として
受理) のため、コアの再利用が成立する。

- **`.codex-plugin/plugin.json`** — Codex plugin manifest。`skills` / `hooks` /
  `interface` フィールドを持つ (`hooks: "./hooks/codex-hooks.json"`)。
- **`hooks/codex-hooks.json`** (新規) — `SessionStart` (matcher `startup|resume`)
  で `${PLUGIN_ROOT}/hooks/session-facts` を実行し自動注入 (Claude の SessionStart
  自動注入と同等)。Claude の `hooks/hooks.json` とは別ファイルで干渉しない。
  Claude 側の `--no-recent-commits` は付けない (Codex が git 情報を自動注入するか
  未確認のため、subagent と同じく recent_commits を残す保守側に倒す)。
- **`skills/session-facts/SKILL.md`** — オンデマンド再生成用の skill (構成変更後の
  手動更新)。自動注入は hook が担うため両者は補完関係。
- **`.agents/plugins/marketplace.json`** — ローカル marketplace 登録
  (`codex plugin marketplace add <repo>` 用)。

実機 `codex-cli 0.142.2` で検証済み (隔離 `CODEX_HOME`): `plugin add` で
`installed, enabled 0.6.0`、manifest/marketplace/hook を error なく受理。version は
plugin.json から解決。

> **保留**: `SubagentStart` hook は Codex の subagent matcher 値が未確認のため
> 見送り (誤 matcher での silent 失敗 / 全 subagent への過剰注入を避ける)。
> SessionStart 自動注入で主要価値はカバー。`--emit subagent-json` モードは
> Codex SubagentStart にも流用可能 (将来対応)。

### 見送り

- **tomllib fast-path** (Python ≥ 3.11 の構造的パース) — house style の正規表現
  一本を優先し、バージョン間差異リスクを回避。将来の選択肢。
- **setup.cfg `[options.extras_require]` の dev 取り込み** — dev 後置の主目的は
  他ソースで達成済みのため YAGNI。

## 0.5.0

**harness 注入との棲み分け + SubagentStart 注入修復 (v0.5)**。実セッションでの
出力評価フィードバック (実行者視点) を受け、harness が無条件注入する情報との
重複排除と、機能していなかった SubagentStart 注入の修復を行った。

1. **SubagentStart 注入修復** (`cli.py`, `hooks/hooks.json`) — SubagentStart では
   plain stdout がモデルに届かない (公式仕様: plain stdout の自動注入は
   SessionStart のみの特権。SubagentStart は `hookSpecificOutput.additionalContext`
   JSON が必須)。`--emit subagent-json` を追加し、hooks.json の SubagentStart
   (Explore/Plan) 側を JSON 包装に切替。従来登録は dead config だった
   (Explore subagent 自身にコンテキストを報告させる実測で確認)。
2. **recent_commits の SessionStart 抑制** (`core/context.py`,
   `collectors/git_progress.py`, `cli.py`, `hooks/hooks.json`) — main セッション
   には harness が gitStatus (直近 5 commit) を常時注入しており、recent_commits
   (3 件) はその完全サブセットだった。`--no-recent-commits` を SessionStart 側に
   のみ付与。subagent には harness の git 情報が一切注入されない (実測) ため、
   SubagentStart 側では維持する。
3. **purpose の dirname fallback 廃止** (`cli.py`) — fallback chain
   (package.json description → README 先頭行 → ディレクトリ名) の最終段は
   repo_root の再掲で情報量ゼロのため、field ごと省略する。
4. **Test Snapshot の「テスト無し」明示** (`collectors/tests.py`) —
   test_files=0 のとき code_files 単独表示 (ミスリード) をやめ、
   `- tests: none detected` の 1 行に置換。

テスト 90 件 (新規 9 件: `test_cli.py` 新設、git_progress / collectors に追加)。

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
   `collectors/dependencies.py`, `collectors/scripts.py`) — tracked な
   pubspec.yaml 検出 (monorepo の `apps/<name>/pubspec.yaml` 等、repo root 直下
   以外も対象) で `stack: flutter, dart`、`.dart` を CODE_EXTENSIONS に追加
   (Service Entry Points / Test Snapshot に反映)、pubspec の主要依存
   (firebase_core / riverpod / dio 等) を
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

## 0.3.0

**出力品質改善 7 件**。エージェント文脈への注入精度を 7 軸で改善した (0.7.0 時点の
追記: 本節以下 0.1.0 までは git log からの要約で、リリース当時に CHANGELOG へは
未記載だった)。

1. **purpose 長さ上限** (140 chars) + Markdown 強調・HTML タグ・YAML frontmatter の
   strip、先頭文優先の抽出に変更。
2. **`claude_plugin` detector 新設** — `.claude-plugin/plugin.json` /
   `marketplace.json` を検出し、stack に `claude-code-plugin` /
   `claude-code-marketplace` を追加。
3. **`python_stack` の fallback 追加** — `pyproject.toml` が無くても `.py` 比率
   20% 以上かつ 10 ファイル以上で python を検出するようにし、plugin repo 等の
   検出漏れを解消。
4. **Service Entry Points のノイズ抑制** — `__init__.py` / `index.*` / `types.*`
   を減点、`__main__.py` / `main.*` / `app.py` / `server.*` を加点するスコアリング
   を追加。
5. **cwd スコープの分離** — cwd != repo_root のとき Service Entry Points を
   cwd-scoped + repo-wide に分離、Test Snapshot も cwd-scoped に切替
   (cwd 配下に code が無ければ repo-wide にフォールバック)。
6. **firebase 判定の根拠強化** — `firebase.json` / `.firebaserc` / 依存 /
   pyproject の evidence を要求し、件数に応じて minimal/moderate/substantial の
   段階表現に変更。meta-tooling 由来の誤検出 note を抑制。
7. **`walk_files` に `respect_subgit=True`** — 非 git な親ディレクトリで子 git
   repo に侵入しないようにし、子 repo のファイルが親の Service/Test セクションに
   混入する問題を解消。

## 0.2.0

**cwd != repo_root 時に cwd 行と Subtree ブロックを追加**。monorepo /
サブプロジェクト構成で呼び出したとき、リポジトリ全体のファクトしか注入されず
「カレントの作業範囲」と「リポジトリ全体」をエージェントが区別できない問題を
解消した。

- `core/context.py`: `RepoContext` に `cwd` フィールドと `cwd_relative`
  プロパティを追加。
- `cli.py`: `main()` で `cwd` を保持し `RepoContext` に渡すよう変更。
- `renderer.py`: `cwd != root` のとき `- cwd: <relative> (subdirectory of
  repo_root)` をヘッダーに追加。
- `collectors/cwd_subtree.py` (新規): priority=15 で `## Subtree (cwd: ...)` を
  `## Structure` の直後に出力。
- `README.md`: 「cwd != repo_root のとき」節を新設。

cwd == repo_root のときは追加出力なし、従来挙動と完全に一致する。

## 0.1.0

**初回リリース**。git tracked files ベースでリポジトリを分析し、スタック検出・
主要スクリプト・env キー・ディレクトリ構造・Test Snapshot・ドメイン型などを
Markdown でまとめる中核パイプラインを実装した。

- `cli.py` / `registry.py` / `renderer.py` / `__main__.py` によるオーケストレーション
  (引数解析 → detector 実行 → collector 実行 → header + セクション結合 → 出力)。
- `core/`: `context.py`（`RepoContext`）, `constants.py`,
  `pm.py`（パッケージマネージャ検出）, `fs.py`, `git.py`, `tree.py`, `util.py`。
- `detectors/`: deno / docker / firebase / go / java / mise / nextjs /
  node_typescript / php / prisma / python / react_vite / ruby / rust /
  taskrunner / testing の各スタック検出。
- `collectors/`: dependencies / domain_types / env_keys / nextjs_facts /
  repo_notes / scripts / services / structure / tests の各セクション。
- テストはまだ同梱していない (unittest 一式は 0.4.0 で新設)。
