# パターン設定 (PATTERNS.md)

`patterns.txt` (plugin 同梱) と `patterns.local.txt` (ユーザー個別) の両方が
合流して rules を構成する。設計背景は [DESIGN.md](./DESIGN.md) 参照。

## 既定 patterns.txt

`hooks/check-sensitive-files/patterns.txt` が plugin 同梱。両 hook で共有される。
fnmatch 書式、`!` プレフィクスは除外。

```
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

> **0.14.0 で `*.local.json` / `*.local.yaml` / `*.local.yml` / `*.local.toml`
> を既定から撤去**。Claude Code エコシステムでは `settings.local.json`
> (Claude Code 本体の個人設定) や `accounts.local.json` 等、「local = git に
> 入れない個人設定 (非機密)」の命名が支配的で、離脱分析 (2026-05) で実 deny の
> 100% がこのパターン起因・防いだ機密露出は 0 件だった。機密値を
> `*.local.json` に置く運用 (例: .NET の `appsettings.local.json` に接続文字列)
> なら、`patterns.local.txt` に以下を追記して復活できる:
>
> ```
> # *.local.* を機密扱いに戻す (last-match-wins で既定の後に評価される)
> *.local.json
> *.local.yaml
> *.local.yml
> *.local.toml
> ```

## ローカル拡張 `patterns.local.txt` (0.6.0 から単一パス)

ユーザー個別のパターンは plugin を fork せずに `patterns.local.txt` に書ける。
両 hook が自動で合流して読み込む。

### 配置パス

`~/.claude/sensitive-files-guardrail/patterns.local.txt`

`~/.claude/sensitive-files-guardrail/` は Claude Code の plugin cache
(`~/.claude/plugins/cache/sensitive-files-guardrail/`) とは別物であることに注意
(前者はユーザー設定、後者は plugin 実体)。

### 初回作成手順

```bash
# 1. 設定ディレクトリを用意
mkdir -p ~/.claude/sensitive-files-guardrail

# 2. 用途別のパターンを追記
cat >> ~/.claude/sensitive-files-guardrail/patterns.local.txt <<'EOF'
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
cat ~/.claude/sensitive-files-guardrail/patterns.local.txt
```

### プロジェクトスコープの rule (`[project:<path>]` セクション、Unreleased)

`patterns.local.txt` はユーザー単位の単一ファイルのままだが、ファイル内に
`[project:<絶対パス>]` セクションを書くと、その rule は該当プロジェクトで
Claude Code が動いているときだけ適用される。ヘッダーより前 (またはヘッダーが
一度も出ない) の行は従来通り**全プロジェクト共通**。

```bash
cat >> ~/.claude/sensitive-files-guardrail/patterns.local.txt <<'EOF'
# 全プロジェクト共通 (ヘッダーなし)
!ca-bundle.pem

[project:/Users/mao/dev/hirokiriko/Asset-Manager]
# Asset-Manager セッションで承認 (2026-08-01) — pnpm 設定のみでトークン非含有
!.npmrc

[project:/Users/mao/dev/other-repo]
!some-other-basename
EOF
```

**プロジェクト識別**: `$CLAUDE_PROJECT_DIR` 環境変数 (設定済みならそれ) →
未設定なら hook 発火時の `cwd` から `.git` が見つかる階層まで遡って解決する
(サブディレクトリで hook が発火しても monorepo のプロジェクト直下を見失わない)。
`$HOME` 自体はプロジェクトとして扱わない。`[project:...]` のパスはこの解決結果
と**文字列完全一致**する必要がある (末尾スラッシュは正規化されるが、シンボリック
リンク解決や相対パスの `~` 展開はしない — 絶対パスをそのまま書く)。

**評価順序**: 共通行 → 一致した `[project:...]` セクションの行、の順で
**ファイル中の出現順のまま**評価する (last-match-wins は出現順で決まるため、
セクション単位で並べ替えたりしない)。上記の例のように共通行を先・
`[project:...]` を後に書けば、プロジェクト側の rule が既定 + 共通ローカルより
強くなる (= プロジェクトで個別に許可・再禁止できる)。

**なぜ single file のままか**: セッションごとに承認した除外パターンが
「このプロジェクトだけの話のつもりが、実は `~/.claude/` 配下のグローバル
1 ファイルなので他の全プロジェクトにも無条件適用されていた」という実運用上の
気付きに基づく。プロジェクト直下に別ファイルを置く方式 (`$CLAUDE_PROJECT_DIR
/.claude/sensitive-files-guardrail/patterns.local.txt` 等) も検討したが、
N プロジェクト = N ファイルに設定が分散して「自分が何を許可しているか」の
一覧性が落ちるため、既存の単一ファイルにセクションを足す形を採用した。

### 旧パスからの移行 (0.5.x → 0.6.0)

0.4.0〜0.5.x で fallback として参照していた
`~/.config/sensitive-files-guardrail/patterns.local.txt` /
`$XDG_CONFIG_HOME/sensitive-files-guardrail/patterns.local.txt` は **0.6.0 で撤去**。
旧パスを使っていた場合は手動で移す:

```bash
mkdir -p ~/.claude/sensitive-files-guardrail
mv "${XDG_CONFIG_HOME:-$HOME/.config}/sensitive-files-guardrail/patterns.local.txt" \
   ~/.claude/sensitive-files-guardrail/patterns.local.txt
```

## 評価方式: last-match-wins (大文字小文字無視)

rules は `既定 → ローカル (共通行 → 一致した [project:...] 行)` の順で連結し、
**最後にマッチしたルール**の include/exclude で判定する (gitignore 風)。
どれにもマッチしなければ非機密。
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

### `_resolve_local_patterns_path() -> Path` (0.6.0 から単一パス)

`patterns.local.txt` の参照先を返す。

- `~/.claude/sensitive-files-guardrail/patterns.local.txt`

呼出側 (`load_patterns`) は ``read_text()`` を試して、FileNotFoundError は
ローカル拡張なしとして黙殺、それ以外の OSError は ``warn_callback`` に渡す。

0.4.0〜0.5.x で存在した複数形 ``_resolve_local_patterns_paths`` (preferred +
fallback の 2-tier) は 0.6.0 で撤去した。

### `_resolve_project_key(cwd) -> str | None` (Unreleased)

`[project:<key>]` セクションと突き合わせる識別子を解決する。

1. `$CLAUDE_PROJECT_DIR` 環境変数があればそれを正規化して返す (優先)
2. 無ければ `cwd` から `.git` が見つかる階層まで遡る (`$HOME` / filesystem root
   に達したら諦めて `None`)
3. `cwd` が空文字列なら `None`

subprocess (`git rev-parse` 等) は呼ばない。

### `_parse_local_patterns_text(text, project_key) -> list[tuple[str, bool]]` (Unreleased)

`patterns.local.txt` 専用のパーサ。`_parse_patterns_text` と違い
`[project:<path>]` セクションヘッダーを解釈する。ヘッダーより前の行は常に出力に
含め (共通行)、ヘッダー以降は `project_key` と一致する場合だけそのセクションの
行を出力に含める。**出現順を保持したまま**返す (グループ単位で並べ替えない)。
`project_key` が `None` ならどのセクションにも一致せず、共通行のみを返す —
ヘッダーを含まない既存ファイルは `_parse_patterns_text` と完全に同じ結果になる。
