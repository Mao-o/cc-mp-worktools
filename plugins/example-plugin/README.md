# example-plugin

`mao-worktools` marketplace のリファレンス / ひな形 plugin。
新しい plugin を作るときのコピー元として使える。

## Components

| 種類 | パス |
|---|---|
| Skill | `skills/example-skill/SKILL.md` |
| Agent | `agents/example-agent.md` |
| Command | `commands/hello.md` |

## 動作確認

リポジトリルートから:
```bash
./scripts/dev.sh example-plugin
```

## ファイル配置のルール

- `.claude-plugin/` には `plugin.json` のみ。
  `skills/` `agents/` `commands/` `hooks/` は plugin ルート直下に置く
- `version` は `plugin.json` 側に書く (marketplace.json には書かない)。
  詳細: `.claude/rules/version-placement.md`
