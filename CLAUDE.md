# worktools marketplace

公開想定の作業支援プラグイン集。独立した git リポジトリ。

## この marketplace のスコープ

- 日常業務・開発作業で再利用したい plugin
- 他者に公開しても差し支えない汎用ツール

## 識別情報

| 項目 | 値 |
|---|---|
| marketplace name | `mao-worktools` |
| リポジトリ (予定) | `Mao-o/cc-mp-worktools` |
| 親ディレクトリ | `~/dev/personal/cc-marketplaces/` (git 管理外) |

## インストール経路 (将来)

```
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install <plugin-name>@mao-worktools
```

バージョン固定:
```
/plugin marketplace add Mao-o/cc-mp-worktools@v0.2.0
```

## 開発フロー

```bash
# 個別 plugin をテスト (marketplace 追加不要)
./scripts/dev.sh example-plugin

# この marketplace を一括検証
claude plugin validate .

# リリース手順
# 1. marketplace.json 該当 plugin の version を bump
# 2. CHANGELOG.md 更新
# 3. git commit / tag / push
```

## この repo 固有の注意点

- `marketplace.json` の `name` は `mao-worktools`
  （予約語回避のため `mao-` プレフィクス）
- plugin の version は **`plugin.json` 側のみ** に書く (相対パスでも)。
  公式ドキュメントの Warning は相対パス plugin で marketplace.json 側を推奨
  するが、CLI 2.1.101 の `plugin validate` は plugin.json に version がないと
  warning を出すため、実装優先で plugin.json 側に一本化。初期開発版は `"0.1.0"`
- plugin ルートの**外**への相対参照は禁止 (cache コピーで壊れる)
- hook スクリプトは `${CLAUDE_PLUGIN_ROOT}` 経由で参照する

## 親ディレクトリのルール

`../CLAUDE.md` を参照すること。
