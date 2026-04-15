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
| `doc-researcher` | Claude/AI SDK/Firebase 公式ドキュメントの段階的調査スキル (llms.txt progressive loader) |
| `example-plugin` | Reference / template plugin |

## Development

```bash
# Load a single plugin for testing (no marketplace registration needed)
./scripts/dev.sh <plugin-name>

# Validate all plugins
claude plugin validate .
```

See [CLAUDE.md](CLAUDE.md) for detailed development guidelines.

## License

MIT
