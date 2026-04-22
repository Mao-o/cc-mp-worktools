# パターン設定 (PATTERNS.md)

`patterns.txt` (plugin 同梱) と `patterns.local.txt` (ユーザー個別) の両方が
合流して rules を構成する。設計背景は [DESIGN.md](./DESIGN.md) 参照。

## 既定 patterns.txt

`hooks/check-sensitive-files/patterns.txt` が plugin 同梱。両 hook で共有される。
fnmatch 書式、`!` プレフィクスは除外。

```
# ローカル設定
*.local.json
*.local.yaml
*.local.yml
*.local.toml

# 機密情報
*.secret*

# 環境変数
.env
.env.*
.envrc
*.envrc

# 鍵・証明書
*.pem
*.key
*.p12
*.pfx
*.keystore
*.jks
id_rsa*
id_dsa*
id_ecdsa*
id_ed25519*

# クレデンシャル
credentials*.json
service-account*.json
.npmrc
.pypirc
.netrc

# 除外: テンプレートファイル
!*.example
!*.template
!*.sample
!*.dist
!*.example.*
!*.template.*
!*.sample.*
!*.dist.*
!*.pub
```

## ローカル拡張 `patterns.local.txt`

ユーザー個別のパターンは plugin を fork せずに
`$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt` (未設定時
`~/.config/sensitive-files-guard/patterns.local.txt`) に書ける。両 hook が自動で
合流して読み込む。

### 初回作成手順

```bash
# 1. 設定ディレクトリを用意 (XDG_CONFIG_HOME 未設定時は ~/.config/)
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/sensitive-files-guard"

# 2. 用途別のパターンを追記
cat >> "${XDG_CONFIG_HOME:-$HOME/.config}/sensitive-files-guard/patterns.local.txt" <<'EOF'
# 自作テンプレート除外
!my-config.env
!config.sample.yaml

# 追加検出
*.auth.json

# CA バンドル除外
!ca-*.pem
EOF

# 3. 反映は即時 (hook は毎回 patterns を読み直す)
#    次回の Read / Bash / Edit / Write から効く
```

設定ファイル確認:

```bash
cat "${XDG_CONFIG_HOME:-$HOME/.config}/sensitive-files-guard/patterns.local.txt"
```

## 評価方式: last-match-wins (大文字小文字無視)

rules は `既定 → ローカル` の順で連結し、**最後にマッチしたルール**の
include/exclude で判定する (gitignore 風)。どれにもマッチしなければ非機密。
0.2.0 以降 **既定で case-insensitive** (`.ENV` や `ID_RSA` も検出)。

これにより:
- 既定除外をローカル側で打ち消せる (例: 既定 `!*.pub` をローカル `*.pub` で
  include に戻す)
- ローカルで exclude を追加して特定 basename を除外できる
  (例: `!fixture-*.pem`)
- OS による大文字小文字の扱い差 (macOS HFS+ / Linux ext4) に依存しない

## Case-sensitive opt-out

旧 0.1.x 系の挙動 (OS 依存 case) に戻したい場合は環境変数
`SFG_CASE_SENSITIVE=1` を設定する。既定は unset (= case-insensitive)。

```bash
export SFG_CASE_SENSITIVE=1  # 旧挙動に戻す
```

## basename のみで判定される (parts は補助)

両 hook ともパターンは **basename** に対して fnmatch する (0.2.0 以降、Stop 側も
Read 側と同じく親 dir 名の parts も補助的に評価する)。ディレクトリ固有の
exclude は書けない:

```
# NG: パスセグメントは効かない
!fixtures/*.pem

# OK: basename だけで区別する
!fixture-*.pem
!test-*.pem
!ca-*.pem
!ca-bundle.pem
```

## `*.pem` / `*.key` の false positive 対策例

証明書バンドルや test fixture には `*.pem` が多用される。誤検出を抑えるには
`patterns.local.txt` に具体的な basename 除外を重ねる:

```
# CA バンドル
!ca-bundle.pem
!root-ca.pem
!intermediate-ca.pem

# テストフィクスチャ (basename 化)
!test-*.pem
!fixture-*.pem

# ビルド成果物
!build-*.pem
```

## Bash の glob false positive 対策

0.3.2 以降の glob 候補列挙は、operand の glob が既定 rules の literal stem と
交差すると deny する。

| コマンド | 挙動 | 備考 |
|---|---|---|
| `cat *.json` | **deny** | `credentials*.json` と交差 |
| `cat *.log` | allow | 既定 rules と非交差 |

project 固有の非機密 JSON を allow したい場合は `patterns.local.txt` で個別
exclude するか、リテラル path に書き換える:

```
# 機密 JSON 全般をすくった上で、特定のものだけ個別除外
!myapp-config.json
!package-lock.json
```

ただし `!credentials*.json` のような **既定 rule を丸ごと打ち消す** 書き方は
推奨しない (新しい機密 basename が入ったとき見落とす)。具体 basename の
exclude を重ねる運用が安全。

## `_detect_format` との同期

新しい機密拡張子やファミリーを追加するときは、以下の 3 箇所を **同時に** 更新
する (どれか 1 つだけ変えると検出と redaction 品質が剥離する):

| 更新対象 | 役割 | 変更例 (direnv の `.envrc` 追加時) |
|---|---|---|
| `hooks/check-sensitive-files/patterns.txt` | matcher: fnmatch 対象 | `.envrc` / `*.envrc` を追加 |
| `hooks/redact-sensitive-reads/redaction/engine.py::_detect_format` | redaction 品質: format 判定 | `endswith(".envrc")` を dotenv に分岐 |
| `hooks/redact-sensitive-reads/tests/test_matcher.py::DEFAULT_RULES` | matcher の回帰テスト定数 | `(".envrc", False)` / `("*.envrc", False)` 追加 |

同期漏れの兆候:
- 新規拡張子で matcher は効くが reason が opaque 扱いになる → engine の
  `_detect_format` 漏れ
- test_matcher の既存テストが pass するのに、実 `patterns.txt` と乖離している
  → DEFAULT_RULES の更新漏れ
- 機密検出されない → patterns.txt の更新漏れ

追加後は:
1. `python3 -m unittest discover hooks/redact-sensitive-reads/tests`
2. `python3 -m unittest discover hooks/check-sensitive-files/tests`
3. `claude plugin validate .`

の 3 点を走らせて warning 0 / all green を確認する。

## 実装詳細

### `_parse_patterns_text(text) -> list[tuple[str, bool]]`

`patterns.txt` / `patterns.local.txt` の 1 ファイル分テキストをパースする関数。
両 hook で同じ仕様 (`core/patterns.py` と `check-sensitive-files/checker.py` に
論理コピー)。

- 空行・`#` で始まる行は無視 (先頭空白 strip 後に判定)
- `!pattern` → `(pattern, True)` (exclude)
- `pattern` → `(pattern, False)` (include)
- 出現順を保持する (last-match-wins で順序が意味を持つため)

### `_resolve_local_patterns_path() -> Path`

`patterns.local.txt` のパス解決。

- `$XDG_CONFIG_HOME` があれば
  `$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt`
- 未設定なら `~/.config/sensitive-files-guard/patterns.local.txt`
- 返り値は実在しなくてもよい (呼出側で `FileNotFoundError` を処理)
