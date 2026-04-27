# Changelog

## 0.3.1

**プロジェクト側 `.claude/verify-cloud-account/CLAUDE.md` の builder 同梱**。
Claude (LLM) が `accounts.local.json` を直接 Read / Write / Edit しようとして
sensitive-files-guard 等で deny された後も、同ディレクトリの CLAUDE.md を
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
   sensitive-files-guard 側の deny reason やパターンには手を入れない
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
