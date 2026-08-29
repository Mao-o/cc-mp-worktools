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

## Plugins

| Plugin | Description | Trigger | Docs |
|---|---|---|---|
| `llms-docs` | Claude/AI SDK/Firebase 公式ドキュメントの段階的調査スキル (llms.txt progressive loader) | Skill (on-demand) + SessionStart | [README](plugins/llms-docs/README.md) |
| `sensitive-files-guardrail` | 機密ファイル (.env, 秘密鍵等) のうっかり露出を予防する多段 hook (実値 redaction + .gitignore 未登録検出) | PreToolUse (Read/Bash/Edit/Write) + Stop | [README](plugins/sensitive-files-guardrail/README.md) |
| `session-facts` | セッション開始時にリポジトリの分析結果 (スタック/スクリプト/env キー等) を Markdown で注入する hook | SessionStart + SubagentStart (Explore/Plan) | [README](plugins/session-facts/README.md) |
| `external-ai-assist` | Cursor / Codex などの外部 AI CLI を並走・クロスレビューに使う hook 集 | PreToolUse (Agent/ExitPlanMode/Bash) + PostToolUse (Agent/Bash/Write/Edit/NotebookEdit) + Stop | [README](plugins/external-ai-assist/README.md) |
| `verify-cloud-account` | Bash 実行前にクラウド CLI (gh/firebase/aws/gcloud/kubectl) のアクティブアカウントを検証する hook | PreToolUse (Bash) | [README](plugins/verify-cloud-account/README.md) |
| `file-split-advisor` | 行数 tier + 責務混在シグナルを組み合わせて分割検討メモを返す非 block hook | PostToolUse (Write/Edit) | [README](plugins/file-split-advisor/README.md) |

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

### Install for Codex (session-facts)

```bash
codex plugin marketplace add Mao-o/cc-mp-worktools
```

続けて `codex plugin add` で `session-facts` を有効化してください。引数の正確な構文は
Codex CLI のバージョンで変わりうるため `codex plugin --help` で確認してください
(実機 `codex-cli 0.142.2` で marketplace 登録から `installed, enabled` までは動作確認済み。
詳細は [plugins/session-facts/CHANGELOG.md](plugins/session-facts/CHANGELOG.md) を参照)。

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

# Validate all plugins
claude plugin validate .
```

See [CLAUDE.md](CLAUDE.md) for detailed development guidelines.

## License

MIT
