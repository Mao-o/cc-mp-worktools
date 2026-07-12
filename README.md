# cc-mp-worktools

Work-related Claude Code plugins.

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

| Plugin | Description |
|---|---|
| `llms-docs` | Claude/AI SDK/Firebase 公式ドキュメントの段階的調査スキル (llms.txt progressive loader) |
| `file-split-advisor` | Write/Edit 後に行数 tier + 責務混在シグナルを組み合わせて分割検討メモを返す非 block hook |

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
