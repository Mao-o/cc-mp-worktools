# Changelog

## 0.7.0

**インライン環境変数の検証 subprocess への伝播 + 診断性改善**: コマンド行頭の
インライン env (`AWS_PROFILE=prod aws ...`) が検証 subprocess に渡されず、SSO
ログイン済みでも永久に deny される問題を解消した。あわせて deny の出所明示・
情報系コマンドの誤検証防止・診断性向上を同梱。

### 変更内容

1. **インライン env の検証 subprocess への伝播** (`core/command_parser.py` +
   `core/dispatcher.py` + 5 service files + `core/cache.py`) — 従来
   `extract_candidates` は先頭 `KEY=VALUE` を剥がして捨てていたが、剥がした env
   を保持して検証 subprocess に渡すようにした。`AWS_PROFILE=prod aws ...` の
   インライン profile 運用で、ログイン済みでも default profile で検証が失敗し
   永久 deny する問題 (剥がすが使わない非対称) を解消:
   - `extract_candidates` が `(候補断片, inline_env dict)` を返す
   - dispatcher が `{**os.environ, **inline_env}` をマージして `verify(env=)` に渡す
     (マージは dispatcher に一元化し、service → core 依存を作らない)
   - 各 service の `verify(expected, project_dir, env=None)` → 内部 `_run_*(env)` →
     `subprocess.run(env=)`。env=None は従来どおり親環境継承 (後方互換)
   - cache キーに inline_env を含め、profile A の成功が profile B で誤 allow されない
   - 値に未展開の `$VAR` を含む env は静的解決不能のため伝播しない (剥がしはする)
2. **deny メッセージの出所明示** (`core/output.py`) — 全 deny の先頭に
   `[verify-cloud-account] ... (CLI 本体のエラーではありません)` タグを付け、
   AWS CLI 等のナマエラー (`Unable to locate credentials` 等) と誤認され CLI
   レベルの切り分けに時間を浪費するのを防ぐ
3. **情報系コマンドの検証スキップ** (5 service files) — `aws --version` /
   `gcloud help` 等のバージョン・ヘルプ表示コマンドを READONLY に追加。
   `command aws --version` 等の診断コマンドが誤って検証対象になり deny される
   問題を解消 (5 サービス共通の穴を一括対応)
4. **検出セグメントの併記** (`core/dispatcher.py`) — verify 失敗の deny に
   `(検出コマンド: ...)` を付け、複合コマンドのどのセグメントが検証を起動した
   かを明示して診断性を改善
5. **direnv / CLAUDE_ENV_FILE の制限を明文化** (README + CLAUDE.local.md) —
   公式仕様 (hooks.md) で `CLAUDE_ENV_FILE` は PreToolUse hook に渡らない
   (SessionStart / Setup / CwdChanged / FileChanged のみ) ため、`.envrc` /
   direnv 経由の env は検証 subprocess に届かない。これは harness 仕様起因で
   plugin では根本解決できないため、回避策 (インライン env / settings.json env /
   起動時 env) を明記する方針とした

テスト 280 件 (新規 19 件: env 抽出 6 + service env 伝播 6 + cache 分離 3 +
dispatch 統合 4)。

## 0.6.0

**self-remediation loop の解消**: deny reason が案内する切替コマンド (例:
`gh auth switch --hostname github.com --user <期待>`) 自体が自サービスの
PATTERNS にマッチして deny され、案内に従えない問題を解消した。
gh / gcloud / firebase / kubectl の 4 サービスで同型の loop を確認し一括対応。

### 変更内容

1. **期待値へ向かう切替コマンドの許可** (`core/dispatcher.py` + 4 service files) —
   各サービスに `is_self_remediation(candidate, expected)` を追加し、dispatcher は
   候補セグメントが全て期待値へ向かう切替のとき検証をスキップして許可する:
   - github: `gh auth switch --user <期待>` (`-u` / `--user=` / dict 期待値の
     `--hostname` 照合に対応、hostname 省略時は github.com)
   - gcloud: `gcloud config set project|account <期待値>`
   - firebase: `firebase use <期待 alias | project ID>`
   - kubectl: `kubectl config use-context <期待値>`
2. **安全性の維持**:
   - 期待値以外への切替・`--user` 無し (インタラクティブ) は通常検証に落ちる
   - 切替 + write の合せ技 (`gh auth switch -u X && gh pr create`) は write 側が
     通常検証される (切替前の現在値で照合)
   - remediation skip は成功キャッシュを書かないため、直後の write は再検証される
   - 期待値が未設定のサービスは従来どおり設定誘導の deny
   - aws は期待値 (Account ID) と切替手段 (profile / SSO) の照合が不能のため対象外
     (主経路 `export AWS_PROFILE` はシェル組込で元々 hook 対象外)
3. **`_collect_targets` の集約形式変更** (`core/dispatcher.py`) — 同一サービスの
   候補セグメントを `(svc, [cand, ...])` に集約 (verify は従来どおりサービスごと 1 回)

テスト 261 件 (新規 31 件: services 24 + dispatcher 7)。

## 0.5.1

**UX 改善 (P3)**: UX 監査の残 P3 フィードバック 9 件を反映。

### 改善内容

1. **Firebase CLI 未インストール検出** (`services/firebase.py`) —
   `shutil.which` で CLI の存在を確認し、未インストール時は
   `npm install -g firebase-tools` を案内 (従来は「プロジェクト取得
   できません」と未設定と区別できなかった)
2. **タイムアウト時の再試行案内** (4 service files) — 全サービスの
   timeout エラーに「再試行するか、ネットワーク接続を確認してください」
   を追加
3. **.gitignore 自動エントリ追加** (`scripts/accounts_builder.py`) —
   `init --commit` / `migrate --commit` 時に `.gitignore` へ
   accounts.local.json のエントリを best-effort で追加。
   `.gitignore` 未存在時は作成しない
4. **GCP 複数エラーの表示階層** (`services/gcloud.py`) — dict 形式で
   project + account の両方がエラーの場合、ヘッダ付きインデントリストで
   表示
5. **SETUP_HINT 重複出力の解消** (5 service files + `core/dispatcher.py`)
   — 共通の init コマンド参照を dispatcher に集約し、SETUP_HINT は
   サービス固有の最小 JSON 例のみに簡素化
6. **README の CLAUDE.md 断リンク修正** (`README.md`) — 存在しない
   `CLAUDE.md` へのリンクを修正
7. **README に `--verbose` デバッグヒント追加** (`README.md`) —
   hook 出力を確認する方法として `claude --verbose` を案内
8. **plugin.json description 短縮** — 冗長な機能列挙を 1 文に集約
9. **output.py warn hookEventName** — 確認済み、変更不要

### スキップした P3

- **deny-first 設計 (SessionStart 早期通知)** — 新規 hook 追加を伴う
  設計変更のため今回はスキップ

### テスト

- Firebase CLI 未インストール検出テスト 1 件追加 (`test_services.py`)
- .gitignore 自動エントリテスト 5 件追加 (`test_accounts_builder.py`)

合計テスト件数: 222 → 228。

## 0.5.0

**UX 改善**: architect-reviewer 4 視点 UX 監査の P2/P3 フィードバックを反映。
deny/warn メッセージの具体性向上、SETUP_HINT の最小 JSON 例追加、
deprecation warn の alert fatigue 対策。

### P2 (8 件)

1. **AWS deny メッセージ改善** (`services/aws.py`) — stderr 先頭行を表示、
   切り替え手順を `AWS_PROFILE` / `aws sso login` / `aws configure` の
   3 パターンで具体化
2. **Firebase deny メッセージ改善** (`services/firebase.py`) — alias → project
   逆引き一覧を表示し `firebase use <alias>` の具体例を案内
3. **Deprecation warn 1 日 1 回制限** (`core/dispatcher.py`) — tmpdir に
   flag ファイルを置き同一プロジェクトへの warn を 86400 秒に制限。
   deny 内の note は制限しない
4. **GitHub str 形式の照合改善** (`services/github.py`) — 複数 host ログイン時
   に `github.com` を優先照合、deny にホスト名を表示、dict 形式への移行案内
5. **SETUP_HINT に最小 JSON 例追加** (5 service files) — 全サービスの
   SETUP_HINT に `{"<key>": "<value>"}` の最小例を追加
6. **skipped ケースの解決手順具体化** (`skills/accounts-init/SKILL.md`) —
   `accounts-show` での比較 → 該当キー削除 → 再実行のステップを案内
7. **marketplace.json category** — 既に設定済みのため変更不要
8. **Skill triggers に英語フレーズ追加** (3 SKILL.md files) — 英語環境での
   自動ロード率を向上

### P3 (1 件)

- **未設定 deny メッセージ改善** (`core/dispatcher.py`) — 「全サービス不要
  なら記述不要」の 1 行を追加し、accounts.local.json が部分記述で OK と明示

### 非互換性

なし。メッセージ文言の改善のみで判定ロジックの変更はない。

### テスト

既存 222 テスト全 green (テストケースの追加なし)。

## 0.4.0

**Feature**: 親ディレクトリ遡及による `accounts.local.json` 発見。
git worktree 配下で作業しているとき、worktree 内に `accounts.local.json`
を複製しなくても親 repo (本体 checkout) の設定を自動で継承して検証する。

### 主要な変更

1. **`core/paths.py` に `discover_accounts_files_with_ancestors()` を追加** —
   `project_dir` から始めて 1 階層ずつ親へ遡り、最初に accounts.local.json が
   見つかった階層を採用する。`max_levels` (既定 10) で安全側の上限を持つ。
2. **`core/dispatcher._find_accounts_file` を親遡及対応に拡張** — 返値に
   `resolved_dir` を追加し、親階層採用時は deny / warn メッセージに
   `accounts.local.json は親ディレクトリ <絶対パス> から継承しています
   (worktree 内に同名ファイルは不要)。` の 1 行注釈を前置きする。
3. **採用優先度** — cwd 階層に何か 1 つでもあればそこを採用 (親階層は見ない)。
   親採用は cwd に何も無いときのフォールバック経路。
4. **3-tier 競合 (D4) 維持** — 同一階層に new/deprecated/legacy が同居する
   場合は従来どおり fail-closed deny。親遡及対象は「最初に見つかった階層」
   のみで、複数階層を横断した競合検出はしない (worktree 親採用は曖昧では
   ないため)。

### 非互換性

なし。cwd に accounts.local.json がある既存プロジェクトの挙動は完全に同じ。
worktree から親 repo の accounts.local.json が継承される挙動は追加機能。

### テスト

- `tests/test_dispatcher.py::TestAncestorLookup` に 6 ケース追加
  (親採用成功 / 失敗 deny + 親注釈 / cwd 優先 / 親階層 D4 競合 /
   親含め未設定 / 親 deprecated パス warn)
- `tests/test_dispatcher.py::TestAncestorDepthLimit` に 1 ケース追加
  (`max_levels` 上限テスト)

合計テスト件数: 215 → 222。

### 想定ユースケース

```
/repo/main-checkout/.claude/verify-cloud-account/accounts.local.json
/repo/main-checkout/.worktrees/feature-x/   ← cwd (worktree)
```

worktree (`/repo/main-checkout/.worktrees/feature-x/`) で `gh pr create` を
叩いても、親 repo の `accounts.local.json` が継承されて検証が走る。

## 0.3.2

**Bug fix**: Firebase の `firebase use` 出力パース修正
(`services/firebase.py`)。アクティブ project が無い状態で `firebase use`
が出力する複数行ヘルプメッセージの末尾トークン (例: "folder.") を
project ID として誤取得し、`.firebaserc` が正しく配置されていても
全 firebase コマンドが「Firebase プロジェクト不一致: 現在=folder.」で
block される回帰を修正。

### 主要な変更

1. **`_from_cli()` 堅牢化** — 単一行・単一トークンのみを project ID と
   みなす。複数行 (ヘルプメッセージ) や空白を含む行は空文字を返す。
2. **`_from_firebaserc()` 優先** — `get_active_account()` および `verify()`
   の評価順序を `_from_firebaserc() or _from_cli()` に逆転。`.firebaserc`
   が JSON で構造化された Firebase CLI 標準設定ファイルであり、CLI 出力
   フォーマットの version 依存性を回避するため。

### 非互換性

なし。`.firebaserc` 配置プロジェクトは bug 解消、未配置プロジェクトは
従来どおり `_from_cli()` にフォールバック (堅牢化により誤値ではなく
`None` を返すように改善)。

### テスト

- `tests/test_active_account.py::TestFirebaseActiveAccount` に 2 ケース追加
  (ヘルプメッセージ単独 / ヘルプメッセージ + `.firebaserc` 優先)
- `tests/test_services.py::TestFirebase` に 1 ケース追加 (`verify()` 経由の
  回帰防止)

合計テスト件数: 212 → 215。

## 0.3.1

**プロジェクト側 `.claude/verify-cloud-account/CLAUDE.md` の builder 同梱**。
Claude (LLM) が `accounts.local.json` を直接 Read / Write / Edit しようとして
sensitive-files-guardrail 等で deny された後も、同ディレクトリの CLAUDE.md を
覗いた瞬間に「builder 経由 (Bash) が正規経路」と理解できるようにする。

### 主要な変更

1. **builder が CLAUDE.md を同梱** —
   `scripts/accounts_builder.py` の `init --commit` および `migrate --commit`
   が成功した直後に、新パスのディレクトリ
   (`.claude/verify-cloud-account/`) に `CLAUDE.md` を配置する。
   既に存在する場合はスキップ (ユーザー編集を尊重)。テンプレートは
   `scripts/templates/project_claude.md`。
2. **best-effort** — テンプレート読み込み失敗・書き込み失敗のいずれも
   warning 1 行を出すだけで builder 自体は成功させる。CLAUDE.md は
   dispatcher が読みに来るパスではないため、欠損しても plugin 本体の動作には
   影響しない。
3. **疎結合の維持** — 本変更は verify-cloud-account 内で完結する。
   sensitive-files-guardrail 側の deny reason やパターンには手を入れない
   (cc-marketplaces の plugin 設計原則)。

### 設計判断

- **D6**: signpost を「埋め込まれた static 文字列」ではなく
  `scripts/templates/project_claude.md` に切り出した。テンプレートだけ更新
  すれば文言を反映できる + diff レビューが容易。
- **D7**: `init --commit` での CLAUDE.md 生成は `action` (add / unchanged /
  skipped) に依存しない。既存 `accounts.local.json` だけ持っていて signpost
  が無いユーザーが、再度 init を流せば後付けで signpost を入れられる経路を
  担保する。
- **D8**: dry-run では生成しない。実際にファイルが書かれる commit 時のみ
  signpost を置く (dry-run と commit の I/O 影響境界を一致させる)。

### 非互換性

なし。CLAUDE.md は dispatcher の判定経路に関与しないため、既存挙動への
影響はない。

### テスト

`tests/test_accounts_builder.py::TestProjectClaudeMd` を新設 (8 ケース):

- init/migrate commit で CLAUDE.md が生成される
- 既存 CLAUDE.md は上書きしない
- dry-run では生成しない
- action=unchanged でも signpost が後付けされる
- template 欠損時に builder が成功する (best-effort)

合計テスト件数: 204 → 212。

## 0.3.0

**accounts.local.json builder + 配置パス刷新**。Claude (LLM) が
accounts.local.json を安全に作成・参照・更新できる正規経路として
`scripts/accounts_builder.py` を新設し、配置パスを
`.claude/verify-cloud-account/accounts.local.json` へ移行。旧パスは
deprecation 案内付きで後方互換、新旧両方存在時は fail-closed で deny。

### 主要な変更

1. **配置パスの 3-tier lookup + 競合時 fail-closed** — `core/paths.py` を新設し
   定数 (`ACCOUNTS_FILE_NEW` / `ACCOUNTS_FILE_DEPRECATED` / `ACCOUNTS_FILE_LEGACY`)
   と helper (`accounts_file_new()` / `discover_all_accounts_files()`) を提供。
   `core/dispatcher._find_accounts_file` を書き直し、複数パス検出時は deny +
   migrate 案内を返す。
2. **builder スクリプト新設** — `scripts/accounts_builder.py` に `init` /
   `show` / `migrate` の 3 サブコマンドを実装:
   - 書込対象パスは `paths.ACCOUNTS_FILE_NEW` に固定 (argv 指定不可、assertion
     で担保) — D2
   - 既定で stdout の値は隠蔽、`--show-values` 明示時のみ露出 — D3
   - 旧 → 新の統合は migrate で行う。値衝突時は自動マージせず deny — D5
3. **Agent Skill 3 本** — `skills/accounts-init/SKILL.md` /
   `skills/accounts-show/SKILL.md` / `skills/accounts-migrate/SKILL.md`。
   いずれも「動作の安定とフォーマット統一のため builder 経由で操作する」
   「値表示前に AskUserQuestion で承認を得る」フローを Claude プロンプトで
   明示 — D1/D3。description のトリガから Claude が自発的にロードする。
4. **services 公開 API 追加** — 5 service 全てに `get_active_account()` と
   `suggest_accounts_entry()` を追加 (scalar / dict は service 側で自動選択)。
   `_parse_active_accounts` は `parse_active_accounts` に昇格 (alias 残置)。
   `services/__init__.py` の契約コメント更新。
5. **SETUP_HINT の書き換え** — 従来の `mkdir -p .claude && echo ...` の手動
   作成案内を削除し、`/verify-cloud-account:accounts-init` への誘導に置換。
6. **テスト拡張**:
   - `tests/test_active_account.py` 新設 (27 ケース: 5 service の
     `get_active_account` / `suggest_accounts_entry` を subprocess mock でテスト)
   - `tests/test_accounts_builder.py` 新設 (28 ケース: D2/D3 特化 + migrate 3
     シナリオ + 値衝突 deny)
   - `tests/test_dispatcher.py` に `TestPathMigration` クラス追加 (6 ケース:
     3-tier lookup + 競合検出 deny)
7. **ドキュメント刷新** — README.md / CLAUDE.md を新パス + builder + 設計判断
   (D1〜D5) に対応。

### 非互換性

- 配置パスが `.claude/accounts.local.json` → `.claude/verify-cloud-account/accounts.local.json`
  に移行。**旧パスのみの環境は後方互換で動作し続ける** が、deny/warn に
  deprecation 案内が付く。**新旧両方存在する環境では fail-closed で deny**
  される (D4)。
- 旧パス → 新パスへの統合は `scripts/accounts_builder.py migrate --commit`
  または `/verify-cloud-account:accounts-migrate` で行う。

### D1〜D5 (設計判断の要点)

- **D1**: 動作の安定とフォーマット統一のため、builder が唯一の正規書込/参照
  経路。Agent Skill が対話フローを提供する。
- **D2**: builder の書込対象は 1 ファイル固定 (argv で変えられない)。
- **D3**: stdout の値表示は `--show-values` 明示時のみ。Agent Skill は
  AskUserQuestion で第二段階の確認フローを提供。
- **D4**: 複数パス存在は deny (自動採用せず)。
- **D5**: migrate で旧 → 新を統合、値衝突は deny。

## 0.2.0

初期公開版 (以前のローカル hook からの plugin 化)。
