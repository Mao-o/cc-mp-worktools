# cc-mp-worktools

> **Note:** Documentation is in Japanese.

Work-related Claude Code plugins.

## Requirements

- **Python 3.11+** (PATH 上の `python3`) — hook の実行に必要
- **git**

## Install

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install <plugin-name>@mao-worktools
```

Local development:

```bash
/plugin marketplace add /path/to/this/repo
```

### バージョンの固定 (ref pinning)

`@<ref>` は **この marketplace repo の git ref を branch か tag に固定する**指定です。
固定されるのは `marketplace.json` のカタログと、そこから相対パスで参照している各
plugin の中身 (= その ref 時点の内容) です。

```bash
# main を追う (ref を省略した場合と同じ)
/plugin marketplace add Mao-o/cc-mp-worktools@main
```

- 公式仕様が marketplace source の ref として案内しているのは **branch / tag** です
  (commit を直接 pin する `sha` は plugin source 側のフィールドで、marketplace source
  には無い)。
- branch / tag を指定した marketplace も `/plugin marketplace update` でその ref の
  **最新 commit に追従**します (tag を打ち替えない限り、tag 指定は実質固定)。
- git URL 形式で追加する場合の ref 指定は `@` ではなく `#` です
  (`.../cc-mp-worktools.git#<ref>`)。
- **リリース tag は plugin ごとに分かれています** (`<plugin-name>/v<version>` 形式。
  詳細は [リリース tag](#リリース-tag))。ある plugin の tag が指しているのは
  「その plugin がその version だった時点の **marketplace 全体**」なので、ref に
  使っても**他の plugin まで同じ時点に固定されます**。「特定の plugin だけを古い
  version で止める」用途には使えません (それは plugin ごとの install 側の話です)。
- ref を指定しない運用 (= `main` を追う) が既定です。

plugin 個々のバージョンは marketplace の ref とは別管理で、各 plugin の
`plugin.json` の `version` が更新判定のキーになります。**version が bump されない限り
`/plugin update` は何も取得しません**。

## Plugins

| Plugin | Description | Trigger | Docs |
|---|---|---|---|
| `llms-docs` | Claude/AI SDK/Firebase 公式ドキュメントの段階的調査スキル (llms.txt progressive loader) | Skill (on-demand) + SessionStart | [README](plugins/llms-docs/README.md) |
| `sensitive-files-guardrail` | 機密ファイル (.env, 秘密鍵等) のうっかり露出を予防する多段 hook (実値 redaction + .gitignore 未登録検出) | PreToolUse (Read/Bash/Edit/Write) + Stop | [README](plugins/sensitive-files-guardrail/README.md) |
| `session-facts` | セッション開始時にリポジトリの分析結果 (スタック/スクリプト/env キー等) を Markdown で注入する hook | SessionStart + SubagentStart (Explore/Plan) | [README](plugins/session-facts/README.md) |
| `external-ai-assist` | Cursor / Codex などの外部 AI CLI を並走・クロスレビューに使う hook 集 | PreToolUse (Agent/ExitPlanMode/Bash) + PostToolUse (Agent/Bash/Write/Edit/NotebookEdit) + Stop | [README](plugins/external-ai-assist/README.md) |
| `verify-cloud-account` | Bash 実行前にクラウド CLI (gh/firebase/aws/gcloud/kubectl) のアクティブアカウントを検証する hook | PreToolUse (Bash) | [README](plugins/verify-cloud-account/README.md) |
| `file-split-advisor` | 行数 tier + 責務混在シグナルを組み合わせて分割検討メモを返す非 block hook | PostToolUse (Write/Edit) | [README](plugins/file-split-advisor/README.md) |
| `verify-plugin-release` | `gh pr create` / `gh pr ready` の前に plugin repo の完了条件 (version bump・CHANGELOG・テスト・validate・競合) を検査し、満たさなければ PR 作成を止める hook | PreToolUse (Bash) | [README](plugins/verify-plugin-release/README.md) |
| `worktree-cwd-guard` | linked worktree で動く session が、同じ repo の別の checkout (main 側や他の worktree) を git の書き込み操作や Write/Edit で書き換えるのを止める hook | PreToolUse (Bash/Write/Edit/MultiEdit/NotebookEdit) | [README](plugins/worktree-cwd-guard/README.md) |
| `codex-pr-review` | GitHub PR の Codex 自動レビューを待ち、指摘対応と `@codex review` での再レビューを回す手順とスクリプト | Skill (on-demand) | [README](plugins/codex-pr-review/README.md) |

## Privacy & data flow

**「外部送信」は plugin 自身のコードがマシン外へデータを送るかどうか**を指します。
どの hook も出力 (`additionalContext` / deny reason など) を Claude Code の会話に
入れるため、**Claude Code 本体はそれを Anthropic の API へ送ります**。下表の「なし」は
「plugin が独自に送信しない」という意味で、「一切マシンから出ない」ではありません。

| Plugin | 外部送信 | 送信先 | 送る内容 | 無効化方法 |
|---|---|---|---|---|
| `llms-docs` | **あり** (HTTPS GET) | `code.claude.com` / `platform.claude.com` / `ai-sdk.dev` / `firebase.google.com` | リクエストヘッダのみ (User-Agent, Accept-Encoding, 条件付き `If-None-Match` / `If-Modified-Since`)。リポジトリの内容・ファイル内容は送らない | 専用の無効化変数は無い。Skill を呼ばなければ取得は走らず、キャッシュが新しい間は再取得もしない。完全に止めるなら plugin を無効化する |
| `external-ai-assist` | **あり** (第三者 AI CLI 経由) | Cursor CLI (`cursor-agent` / `cursor`) と Codex CLI (`codex`) が各社のクラウドへ送る | Explore サブエージェントの prompt、ExitPlanMode の plan 本文、そのターンに編集したパスの `git diff HEAD` | `EXTERNAL_AI_EXPLORE_PARALLEL=0` / `EXTERNAL_AI_PLAN_REVIEW=0` / `EXTERNAL_AI_POST_REVIEW=0` (既定はいずれも有効)。対象 CLI が PATH に無ければ何もしない |
| `verify-cloud-account` | **あり** (間接) | 起動するクラウド CLI 経由。`gh auth status` はトークンを API で検証するため往復が入り、`aws sts get-caller-identity` は AWS STS を呼ぶ。`gcloud` / `kubectl` / `firebase` の照合はローカル設定の読み取り | CLI に渡すのは固定引数のみ。リポジトリの内容・`accounts.local.json` の中身・Firebase configstore のトークンは送らない | `VERIFY_CLOUD_ACCOUNT_MODE=off` (既定 `enforce`)。`off` は照合自体を行わず CLI も起動しない |
| `sensitive-files-guardrail` | なし | — | ローカルの読み取りと read-only な `git` 呼び出しのみ | 該当なし |
| `session-facts` | なし | — | ローカルの read-only な `git` 呼び出しのみ。env は `.env.example` 等のテンプレートから**キー名だけ**を読む (値は読まない) | 該当なし |
| `file-split-advisor` | なし | — | 外部プロセスを一切起動しない | 該当なし |
| `verify-plugin-release` | **あり** (間接) | 起動する `git fetch` (origin) / `gh pr view` (GitHub API) / `claude plugin validate` 経由 | コマンドに渡すのは base branch 名・PR 番号などの固定引数のみ。リポジトリの内容は plugin 自身は送らない (`git fetch` は取得のみ) | `VERIFY_PLUGIN_RELEASE_MODE=off` (既定 `enforce`)。fetch だけ止めるなら repo の設定で `"fetch": false` |
| `worktree-cwd-guard` | なし | — | ローカルの read-only な `git` 呼び出し (`rev-parse` / `worktree list`) のみ | 該当なし |
| `codex-pr-review` | **あり** (GitHub API) | `gh` 経由で GitHub | 状態確認は読み取りのみ。`pr-codex-trigger.sh` は引数のサマリと `@codex review` を PR にコメントとして投稿する | Skill を呼ばなければ何もしない |

`external-ai-assist` には送信内容の除外規則 (`.env*` / `*.pem` / `*service-account*.json`
など) がありますが、**これは plugin が渡す差分の範囲を絞るだけ**です。起動された外部 AI
CLI は読み取り権限を持つエージェントなので、作業ツリー内の他のファイルを自ら読むことが
できます (read-only 起動が禁じるのは書き込みで、読み取りではありません)。外部に出せない
ファイルがあるリポジトリでは、この plugin を無効化してください。

脆弱性の報告方法と各 plugin の脅威モデルの前提は [SECURITY.md](SECURITY.md) にあります。

## Codex support

この marketplace は Claude Code 向けが主体です。Codex (`codex-cli`) 向けにも配線されて
いるのは現状 `session-facts` のみで、Codex の hooks framework が Claude Code の
`SessionStart` とほぼ同型であることを利用しています (詳細:
[plugins/session-facts/CHANGELOG.md](plugins/session-facts/CHANGELOG.md) の
「Codex plugin 兼用対応」節)。他 plugin は hook 専用または Skill 主体で、Codex 側の
配線・動作検証を未実施のため対象外です。

| Plugin | Codex 対応 | 非対応の理由 / 備考 |
|---|---|---|
| `llms-docs` | 非対応 | Skill が主体 (SessionStart hook は補助)。Codex 向けの配線・検証は未実施 |
| `sensitive-files-guardrail` | 非対応 | PreToolUse/Stop hook 専用。Codex 側の対応可否は未検証 |
| `session-facts` | **対応** (`.codex-plugin/`) | コアが必要とするのは SessionStart のみで Codex の hooks framework と互換。`SubagentStart`相当は matcher 未確認のため保留 |
| `external-ai-assist` | 非対応 | PreToolUse/PostToolUse/Stop hook 専用。Claude 固有のツール名 (Agent/ExitPlanMode/NotebookEdit) に依存するため移植は未着手 |
| `verify-cloud-account` | 非対応 | PreToolUse hook が主機能 (同梱 Skill 3 本は補助)。Codex 側の対応可否は未検証 |
| `file-split-advisor` | 非対応 | PostToolUse hook 専用。Codex 側の対応可否は未検証 |
| `verify-plugin-release` | 非対応 | PreToolUse (Bash) hook 専用。Codex 側の対応可否は未検証 |
| `worktree-cwd-guard` | 非対応 | PreToolUse hook 専用。Codex 側の対応可否は未検証 |
| `codex-pr-review` | 非対応 | Skill 主体。Codex 向けの配線・検証は未実施 |

### Install for Codex (session-facts)

```bash
codex plugin marketplace add Mao-o/cc-mp-worktools
```

続けて `codex plugin add` で `session-facts` を有効化してください。**上記を含め、
Codex 側のコマンド引数はこの repo では未検証です。**正確な構文は Codex CLI の
バージョンで変わりうるため `codex plugin --help` で確認してください。

裏が取れているのは「`codex-cli 0.142.2` で `session-facts` が `installed, enabled`
になり、manifest / hook が error なく受理された」ところまでです
(詳細は [plugins/session-facts/CHANGELOG.md](plugins/session-facts/CHANGELOG.md))。

## Renamed / removed plugins

過去に名前が変わった、または削除された plugin です。旧名のまま install 済みの場合は
`/plugin update` では追従できず古いバージョンのまま残るため、手動で入れ替えてください。

| 旧名 | 現在 | 対応 |
|---|---|---|
| `doc-researcher` | `llms-docs` (0.9.0 で rename) | `/plugin uninstall doc-researcher@mao-worktools` → `/plugin install llms-docs@mao-worktools` |
| `sensitive-files-guard` | `sensitive-files-guardrail` (0.14.x で rename) | `/plugin uninstall sensitive-files-guard@mao-worktools` → `/plugin install sensitive-files-guardrail@mao-worktools` |
| `agent-org` | 別 marketplace へ分離 (本 marketplace では削除) | `/plugin uninstall agent-org@mao-worktools` (後継 plugin は本 marketplace の対象外) |

`sensitive-files-guard` → `sensitive-files-guardrail` の rename に伴い、
`patterns.local.txt` の設置先も `~/.claude/sensitive-files-guard/` から
`~/.claude/sensitive-files-guardrail/` に変わっています。旧パスは互換のため
fallback 読み込みされ移行警告が出ますが (新旧どちらも存在する場合は新パスのみを
採用し旧パスは無視される)、次のコマンドで新パスに移してください:

```bash
mkdir -p ~/.claude/sensitive-files-guardrail
mv -n ~/.claude/sensitive-files-guard/patterns.local.txt ~/.claude/sensitive-files-guardrail/patterns.local.txt
```

新パスに既にファイルがある場合、`-n` により上書きせず何もしません。
新パス側が権威なので、旧パスにしか無い行が必要なときだけ手で追記してください。

plugin README にパス設定の詳細があります
([plugins/sensitive-files-guardrail/README.md](plugins/sensitive-files-guardrail/README.md))。

過去の rename は旧 entry を残さない clean-cut 方針でしたが、今後の rename は
1 リリース旧 entry を残し deprecation 通知を出す方針に変更しています
([CLAUDE.md](CLAUDE.md) 参照)。

## Development

```bash
# Load a single plugin for testing (no marketplace registration needed)
claude --plugin-dir ./plugins/<plugin-name>

# Validate the marketplace manifest, every plugin manifest, and repo consistency
make validate

# Lint (ruff; config lives in pyproject.toml)
make lint

# Run every plugin's unit tests
make test
```

CI (`.github/workflows/validate.yml`) は `make validate` 相当のチェックに加え、
`ruff check` と、**Python 3.11 / 3.12 / 3.13 の matrix** でのユニットテストを回します。
加えて、sensitive-files-guardrail の 2 suite を `windows-latest` で回す
`tests-windows` job が **PR / main push のゲート**に入っています (リリース tag の
付与もこの job の green を待ちます)。Windows で CI を回しているのはこの plugin だけで、
他の plugin の Windows 対応は未検証です。

開発フロー・テスト規約・version と CHANGELOG の運用は
[CONTRIBUTING.md](CONTRIBUTING.md)、この repo 固有の設計上の注意点は
[CLAUDE.md](CLAUDE.md) にあります。

## リリース tag

リリース tag は **plugin ごと**に打ち、名前は `<plugin-name>/v<version>` です
(`plugin.json` の `version` と 1:1)。

```
sensitive-files-guardrail/v0.33.1
session-facts/v0.12.0
```

- **tag は手で打ちません。** main への push で CI が、各 plugin の `plugin.json` の
  version に対応する tag が無ければ annotated tag を作って push します
  (validate / lint / tests が green のときだけ)。
- 既存 tag は打ち替えません。
- **旧規約の `v0.2.0` 〜 `v0.14.0` はそのまま残していますが、今後は打ちません。**
  marketplace 全体を指すもの (`v0.2.0`) と個別 plugin のリリースを指すもの
  (`v0.3.2` / `v0.11.0` / `v0.12.0` / `v0.14.0`) が名前から区別できず混在して
  いたため、plugin 別の形式に切り替えました。
- plugin の更新判定に使われるのは tag ではなく `plugin.json` の `version` です
  (tag は「その版がどの commit に入っているか」を後から辿るための印)。

## Changelog

変更履歴は**粒度で置き場所が分かれています**。

| 変更の種類 | 置き場所 |
|---|---|
| plugin の機能追加・修正 | `plugins/<name>/CHANGELOG.md` |
| plugin entry の追加 / rename / 削除、root ドキュメント、CI、共通スクリプト、repo 衛生 | root の [CHANGELOG.md](CHANGELOG.md) |

root の `CHANGELOG.md` は **marketplace 全体の変更だけ**を日付見出しで記録します。
marketplace 自体はバージョン番号を持たず (更新判定のキーは各 plugin の `plugin.json`
の `version`)、plugin のバージョンと混同されないようにするためです。

## License

MIT
