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
- `plugin.json` に `version` は書かない。marketplace.json 側で管理する
