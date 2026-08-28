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

### プロジェクトスコープの rule (`[project:<path>]` セクション、0.15.0)

`patterns.local.txt` はユーザー単位の単一ファイルのままだが、ファイル内に
`[project:<絶対パス>]` セクションを書くと、その rule は該当プロジェクトで
Claude Code が動いているときだけ適用される。ヘッダーより前 (またはヘッダーが
一度も出ない) の行は従来通り**全プロジェクト共通**。

```bash
cat >> ~/.claude/sensitive-files-guardrail/patterns.local.txt <<'EOF'
# 全プロジェクト共通 (ヘッダーなし)
!ca-bundle.pem

[project:/path/to/project-a]
# project-a のセッションで承認 — pnpm 設定のみでトークン非含有
!.npmrc

[project:/path/to/project-b]
!some-other-basename
EOF
```

**プロジェクト識別**: `$CLAUDE_PROJECT_DIR` 環境変数 (設定済みならそれ) →
未設定なら hook 発火時の `cwd` から `.git` が見つかる階層まで遡って解決する
(サブディレクトリで hook が発火しても monorepo のプロジェクト直下を見失わない)。
`$HOME` 自体はプロジェクトとして扱わない。`[project:...]` のパスはこの解決結果
と**文字列完全一致**する必要がある (末尾スラッシュは正規化されるが、シンボリック
リンク解決や相対パスの `~` 展開はしない — 絶対パスをそのまま書く)。

**reason からの誘導 (0.19.0)**: Read / Bash / Edit / Write の deny reason と Stop の
block reason は、除外の恒久化として `[project:$CLAUDE_PROJECT_DIR]` ヘッダー +
`!<basename>` 行のレシピを案内する (ヘッダー無し行 = 全プロジェクト共通は明示的な
選択)。`$CLAUDE_PROJECT_DIR` は **プレースホルダ** で、書くときはそのプロジェクトの
絶対パスに置き換える (hook はヘッダーを展開しない。reason に絶対パスを出さない
方針のため変数名で示している)。Stop の block reason は検出した全ファイルの
`!<basename>` 行をまとめて出す (20 件で畳む)。

**書き損じの警告**: Bash tool の環境では `CLAUDE_PROJECT_DIR` が未設定なので、
unquoted の `echo "[project:$CLAUDE_PROJECT_DIR]" >> patterns.local.txt` は
`[project:]` に、quoted heredoc や Write tool は literal の
`[project:$CLAUDE_PROJECT_DIR]` になる。どちらもどのプロジェクトにも一致せず
そのセクションは無視されるが、黙らず `local_patterns_header_invalid:
project_header_empty` / `project_header_unexpanded_placeholder` を stderr
(Read / Bash 側は `~/.claude/logs/redact-hook.log` にも) に出す。
`pwd` で得たプロジェクト root の絶対パスを literal に書くこと。判定は変数参照の
standalone 形 (ヘッダー値が `$NAME` / `${NAME}` そのもの、またはそれで始まり直後が
`/`。`$CLAUDE_PROJECT_DIR` もこの形のみ) に限定しており、`/work/project$prod` や
`/work/repo$CLAUDE_PROJECT_DIR-prod` のように `$` や予約語を途中に含む literal
パスは正当なヘッダーとして通常どおり比較される。

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
Read 側と同じく親 dir 名の parts も補助的に評価する。ただし **Bash operand の
判定は 0.22.0 から basename のみ** — sed / awk の式や option の値のような「path
とは限らない文字列」が親 dir 名の `.env` で deny になるのを避けるため)。
ディレクトリ固有の exclude は書けない:

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

operand に glob (`*` / `?` / `[`) を含む Bash コマンドの判定は **0.8.0 で
「dotenv stem 一致のみ deny」に縮小** した。0.3.2〜0.7.x の既定 rules 候補列挙
(`cat *.json` を `credentials*.json` と交差させて deny) は `cat *.log` のような
日常 glob まで巻き込む false positive があり撤廃済み (経緯は [DESIGN.md](./DESIGN.md)
の「glob operand 判定の歴史」)。

| コマンド | 挙動 (default / autonomous) | 備考 |
|---|---|---|
| `cat .env*`, `cat .e[n]v`, `cat .en?`, `cat .envrc*` | **deny / deny** | glob が shell の展開で `.env` / `.envrc` の literal stem に一致 |
| `cat */.env`, `cat **/.env` | **deny / deny** | 展開は path 要素ごと。basename 側が `.env` (0.22.0。0.21.x までは ask / allow) |
| `cat *`, `git add *`, `cp * dst/`, `cat ?env`, `cat [.]env`, `cat *.envrc` | ask / allow | shell の `*` / `?` / bracket 式はファイル名先頭の `.` に一致しない (POSIX、bash / zsh 実測)。0.21.x までは fnmatch の意味論で **deny** になっていた (0.22.0 で修正) |
| `cat *.json`, `cat cred*.json`, `cat *.key`, `cat id_rsa*` | ask / allow | 既定 rules との交差は見ない (`ask_or_allow`) |
| `cat *.log`, `cat .env.*`, `cat .env.example*` | ask / allow | 同上 (`.env.*` は `.env` 自体に一致しない) |

つまり `cat *.json` は **deny されない** (default / acceptEdits / dontAsk では
確認ダイアログ、auto / bypassPermissions / plan では素通り)。確定判定が欲しければ
glob を使わずリテラル path に書き換える (`cat credentials.json` は deny、
`cat package.json` は allow)。

`patterns.local.txt` の `!<basename>` 除外は **glob operand の判定には効かない**
(glob は既定 / ローカル rules と照合せず dotenv stem とだけ比較するため、除外する
対象がない)。除外が効くのはリテラル path の operand scan と Read / Edit / Write /
Stop の判定で、そこでも `!credentials*.json` のような **既定 rule を丸ごと打ち消す**
書き方は推奨しない (新しい機密 basename が入ったとき見落とす)。具体 basename の
exclude を重ねる運用が安全:

```
# 機密 rule は残したまま、非機密と分かっている具体 basename だけ除外
!myapp-config.json
```

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

以下の関数はすべて `hooks/_shared/patterns.py` にある (0.2.0 で両 hook の実装を
`_shared` に集約)。`redact-sensitive-reads/core/patterns.py` と
`check-sensitive-files/checker.py` はそれを import して warn callback を注入する
薄い wrapper で、論理コピーは持たない。

### `_parse_patterns_text(text) -> list[tuple[str, bool]]`

既定 `patterns.txt` の 1 ファイル分テキストをパースする関数 (`[project:...]`
セクションは解釈しない。`patterns.local.txt` は 0.15.0 から後述の
`_parse_local_patterns_text` が担当)。

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

### `_resolve_project_key(cwd) -> str | None` (0.15.0)

`[project:<key>]` セクションと突き合わせる識別子を解決する。

1. `$CLAUDE_PROJECT_DIR` 環境変数があればそれを正規化して返す (優先)
2. 無ければ `cwd` から `.git` が見つかる階層まで遡る (`$HOME` / filesystem root
   に達したら諦めて `None`)
3. `cwd` が空文字列なら `None`

subprocess (`git rev-parse` 等) は呼ばない。

### `_parse_local_patterns_text(text, project_key) -> list[tuple[str, bool]]` (0.15.0)

`patterns.local.txt` 専用のパーサ。`_parse_patterns_text` と違い
`[project:<path>]` セクションヘッダーを解釈する。ヘッダーより前の行は常に出力に
含め (共通行)、ヘッダー以降は `project_key` と一致する場合だけそのセクションの
行を出力に含める。**出現順を保持したまま**返す (グループ単位で並べ替えない)。
`project_key` が `None` ならどのセクションにも一致せず、共通行のみを返す —
ヘッダーを含まない既存ファイルは `_parse_patterns_text` と完全に同じ結果になる。
