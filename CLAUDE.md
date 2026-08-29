# worktools marketplace

公開想定の作業支援プラグイン集。独立した git リポジトリ。

## この marketplace のスコープ

- 日常業務・開発作業で再利用したい plugin
- 他者に公開しても差し支えない汎用ツール

## 識別情報

| 項目 | 値 |
|---|---|
| marketplace name | `mao-worktools` |
| リポジトリ | `Mao-o/cc-mp-worktools` |

## インストール経路

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
claude --plugin-dir ./plugins/<plugin-name>

# この marketplace を一括検証
claude plugin validate .

# リリース手順
# 1. plugin.json の version を bump
# 2. CHANGELOG.md 更新
# 3. git commit / tag / push
```

## この repo 固有の注意点

- `marketplace.json` の `name` は `mao-worktools`
  （予約語回避のため `mao-` プレフィクス）
- plugin の version は **`plugin.json` 側のみ** に書く (相対パスでも)。
  version の解決順は plugin.json → marketplace entry → git commit SHA で、
  **両方に書くと `plugin.json` の値が警告なく勝つ** (marketplace.json 側で
  bump したつもりが反映されない事故のもと)。entry 側のみに書き plugin.json に
  version が無いと `claude plugin validate` が warning を出すため、warning
  ゼロ運用のために plugin.json 側に一本化する。初期開発版は `"0.1.0"`
- plugin ルートの**外**への相対参照は禁止 (cache コピーで壊れる)
- hook スクリプトは `${CLAUDE_PLUGIN_ROOT}` 経由で参照する
- plugin を rename する場合は、**旧名の marketplace entry を 1 リリース残し
  SessionStart hook で deprecation 通知を出す** (orphan 化防止)。旧名のまま
  install 済みのユーザーは `/plugin update` で新名に追従できないため。過去の
  rename 実績は [README.md](README.md) の「Renamed / removed plugins」節を参照

## 親ディレクトリのルール

`../CLAUDE.md` を参照すること。
