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

ref 固定 (marketplace repo の branch / tag を指定する。commit を直接 pin する `sha` は
plugin source 側のフィールドで、marketplace source には無い):
```
/plugin marketplace add Mao-o/cc-mp-worktools@main
```

`@<ref>` が固定するのは **marketplace 側の checkout** で、plugin 個々のバージョンは
`plugin.json` の `version` が決める (両者は独立)。詳細は
[README.md](README.md) の「バージョンの固定 (ref pinning)」節を参照。

## 開発フロー

```bash
# 個別 plugin をテスト (marketplace 追加不要)
claude --plugin-dir ./plugins/<plugin-name>

# この marketplace を一括検証
claude plugin validate .

# lint (ruff。設定は root の pyproject.toml)
make lint

# リリース手順
# 1. plugin.json の version を bump
# 2. CHANGELOG.md 更新
# 3. git commit / push → PR → main へ merge
#    (tag は main への merge 後に CI が自動で打つ。手で打たない)
```

## リリース tag の規約

tag 名は **`<plugin-name>/v<version>`** (例: `sensitive-files-guardrail/v0.33.1`)。
`plugin.json` の `version` と 1:1 に対応させる。

- **手で打たない。** main への push で CI (`.github/workflows/validate.yml` の
  `tag-plugin-releases` job) が、各 plugin の `plugin.json` の version に対応する
  tag が無ければ annotated tag を作って push する。validate / lint / tests が
  green のときだけ打つ
- 既存 tag は**打ち替えない**。bump を忘れた場合は「打つ tag が無い」だけで、
  次に bump した push で作られる
- 判定・作成ロジックは `scripts/backfill-plugin-tags.sh` に一本化 (既定は
  dry-run、`--apply` で作成 + push)。規約導入時の遡及付与と通常運用で同じコードを使う
- **旧規約の bare tag (`v0.2.0` 〜 `v0.14.0`) はそのまま残すが、今後は打たない。**
  marketplace 全体を指すもの (`v0.2.0`) と個別 plugin のリリースを指すもの
  (`v0.3.2` = verify-cloud-account、`v0.11.0` / `v0.12.0` / `v0.14.0` =
  sensitive-files-guardrail) が名前から区別できず混在していたのが、規約を
  変えた理由

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
