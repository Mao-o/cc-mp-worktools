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

**reason からの誘導 (0.19.0 / 0.24.0)**: Read / Bash / Edit / Write の deny reason と
Stop の block reason は、除外の恒久化として `[project:$CLAUDE_PROJECT_DIR]` ヘッダー +
除外行のレシピを案内する (ヘッダー無し行 = 全プロジェクト共通は明示的な選択)。
0.24.0 から除外行は **path 形** `!<root 相対パス>` (承認した 1 ファイルだけを外す)
を既定とし、basename 形 `!<basename>` (同名すべてを外す) は「同名ファイルをすべて
外したい場合だけ」の明示的な選択として併記する (両形の違いは後述「rule の形で
比較対象が決まる」)。root 直下のファイルは `!/.env` のように**先頭 `/` 付き**で
案内する (`!.env` だと basename 形に化けて同名すべてが外れるため、
`_shared.patterns.path_rule_for` が付ける)。root 相対 path を確定できないとき (root 不明 / root 配下でない /
glob operand / VCS pathspec) は basename 形だけを案内する。
`$CLAUDE_PROJECT_DIR` は **プレースホルダ** で、書くときはそのプロジェクトの
絶対パスに置き換える (hook はヘッダーを展開しない。reason に絶対パスを出さない
方針のため変数名で示している)。Stop の block reason は検出した全ファイルの
除外行をまとめて出す (20 件で畳む)。サブディレクトリで発火しても表示の file 一覧は
cwd 相対、レシピは root 相対 (`!sub/.env`)。

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

## rule の形で比較対象が決まる (basename 形 / path 形)

| 形 | 例 | 比較対象 | 効く範囲 |
|---|---|---|---|
| **basename 形** (`/` を含まない) | `.env` / `*.pem` / `!ca-*.pem` | basename (fnmatch)。Read / Edit / Stop では親 dir 名 (parts) も補助的に評価 | **同名すべて** — どのディレクトリでも、`[project:]` セクション内に書いても他プロジェクトの同名 path にも効く |
| **path 形** (`/` を含む、0.24.0) | `!config/prod.pem` / `!fixtures/` / `secrets/**` | **project root からの相対 path 全体** (gitignore 準拠の translator) | **root 配下のその path だけ** |

両形は 1 本のリストとして **出現順に last-match-wins** で評価する (形ごとに階層を
分けない)。既定 `patterns.txt` はすべて basename 形なので、ローカルに書いた
`!config/prod.pem` は既定の `*.pem` より後ろで評価され、その 1 ファイルだけを
外す。0.23.0 までは path 形の行は格納されても**一度も一致しなかった**
(matcher が basename と parts しか見なかったため)。

```
# 承認した 1 ファイルだけを外す (0.24.0)
!config/prod.pem

# root 直下の 1 ファイル — 先頭 / を付けないと basename 形 (同名すべて) になる
!/.env

# fixtures ディレクトリ配下すべて (任意の深さ)
!fixtures/

# basename 形: 同名ファイルをすべて外す (0.23.0 までの唯一の書き方)
!ca-bundle.pem
!fixture-*.pem
```

### path 形の意味論 (gitignore 準拠、fnmatch ではない)

| 書き方 | 意味 | 一致する | 一致しない |
|---|---|---|---|
| `config/prod.pem` | root 相対の path 1 本。途中に `/` があれば root アンカー | `config/prod.pem` | `x/config/prod.pem`、`config/prod.pem/inner` |
| `/config/prod.pem`、`./config/prod.pem` | 同上 (先頭 `/` `./` はアンカーの明示) | `config/prod.pem` | `x/config/prod.pem` |
| `/.env` | **root 直下**の 1 ファイル。root 直下は相対 path に `/` が無いので**先頭 `/` が必須** (`.env` だけだと basename 形 = 同名すべて) | `.env` | `sub/.env`、root 外の `.env` |
| `fixtures/` | 末尾 `/` はディレクトリ。`/` が末尾だけなら任意の深さ | `fixtures/a.pem`、`a/fixtures/b/c.pem` | `fixtures` (同名ファイル)、`fixtures2/x` |
| `/fixtures/`、`certs/fixtures/` | アンカー付きディレクトリ | `certs/fixtures/x.pem` | `a/certs/fixtures/x.pem` |
| `fixtures/*.pem` | `*` は `/` を跨がない | `fixtures/a.pem` | `fixtures/deep/a.pem` |
| `fixtures/**/*.pem` | 中間 `/**/` は 0 個以上のディレクトリ | `fixtures/a.pem`、`fixtures/d/e/a.pem` | `other/a.pem` |
| `**/fixtures/*.pem` | 先頭 `**/` は任意の深さ | `a/b/fixtures/x.pem` | `a/fixturesx/x.pem` |
| `secrets/**` | 末尾 `/**` は配下すべて | `secrets/a`、`secrets/d/b` | `secrets` |
| `k?y/a.pem`、`certs/[ab].pem` | `?` と `[...]` は `/` 以外の 1 文字 | `key/a.pem`、`certs/a.pem` | `k/y/a.pem`、`certs/c.pem` |

- **root** は `[project:...]` セクションの key と同じ解決 (`$CLAUDE_PROJECT_DIR` →
  無ければ `cwd` から `.git` を上方探索。`_shared.patterns.resolve_project_root`)。
  root を解決できない (非 git ディレクトリ等) / path が root 配下でない場合、
  path 形 rule は**一度も一致しない** (0.23.0 までと同じ挙動)
- 相対 path の基準は **root であって cwd ではない**。サブディレクトリで発火しても
  同じ rule が同じファイルに効く
- **末尾 `/` の無い rule は path 1 本だけ**に一致し、同名ディレクトリの配下には
  及ばない (gitignore は「ディレクトリに一致したら配下も」だが、除外は狭い方が
  安全なので採らない)。配下を含めたければ `fixtures/` か `fixtures/**` と書く
- `\` はエスケープではなく literal (basename 形の fnmatch と同じ)。メタ文字を
  literal にしたいときは `[*]` のように文字クラスで包む (両 hook のレシピ生成は
  `escape_glob` で自動的にそうする)
- 大文字小文字は basename 形と同じく既定で無視 (`SFG_CASE_SENSITIVE=1` で区別)
- 比較は lexical (symlink 解決も実在確認もしない)。`$CLAUDE_PROJECT_DIR` が
  symlink 経由のパスで `cwd` が実体パス (またはその逆) のように**文字列として
  root 配下にならない**組み合わせでは path 形は一致しない (basename 形は従来どおり)
- Bash operand も path 形 rule を評価する (`cat config/prod.pem`、
  `cat ../config/prod.pem`、`git show HEAD -- config/prod.pem`)。ただし
  **コロンを含む operand** (`git show HEAD:config/prod.pem` の pathspec、
  `user@host:path`、`file://...`) には適用しない — git の `<rev>:<path>` は
  tree root 相対で hook の cwd 基準と一致せず、別ファイルを承認した rule が
  未承認のファイルを許可しうるため (basename 形は従来どおり効く)。
  途中に `/` を含む rule は root アンカーなので、`sed 's/secrets/x/'` の式が
  合成 path になっても `secrets/*.yaml` のような rule には当たらない
  (`**/` で始める rule だけは当たりうる)
- 正規表現として不正な文字クラス (逆順の範囲 `[z-a]` 等) を含む path 形 rule は
  **何にも一致しない** (basename 形の fnmatch と同じ扱い)。壊れた行 1 つで hook
  全体が止まることはない

### parts (親 dir 名) は basename 形の補助

Read / Edit / Stop は実在ファイルの実パスを扱うため、basename が決着しなければ
親 dir 名 (`pathlib.parts`) も basename 形 rule で評価する (`/foo/.env/bar` を
検出する経路)。**Bash operand は 0.22.0 から parts を見ない** — sed / awk の式や
option の値のような「path とは限らない文字列」が親 dir 名の `.env` で deny に
なるのを避けるため。path 形 rule は parts 経由では評価しない (root 相対 path
全体との比較のみ)。Stop は 0.24.0 から root 相対 path で parts を評価するので、
サブディレクトリで発火しても root で発火したときと同じ verdict になる
(0.23.0 までは cwd 相対だったため、cwd と root の間のディレクトリ名は見て
いなかった)。

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
| `shopt -s dotglob; cat *`, `GLOBIGNORE=x; cat *`, `setopt globdots; cat *` | **deny / deny** | 同一コマンド内で dotglob 系を有効化していれば `*` は dotfile にも展開されるので 0.21.x の意味論に戻す。profile (`.bashrc` / `.zshenv`) で常時有効な環境は hook から見えない |
| `cat *.json`, `cat cred*.json`, `cat *.key`, `cat id_rsa*` | ask / allow | 既定 rules との交差は見ない (`ask_or_allow`) |
| `cat *.log`, `cat .env.*`, `cat .env.example*` | ask / allow | 同上 (`.env.*` は `.env` 自体に一致しない) |

つまり `cat *.json` は **deny されない** (default / acceptEdits / dontAsk では
確認ダイアログ、auto / bypassPermissions / plan では素通り)。確定判定が欲しければ
glob を使わずリテラル path に書き換える (`cat credentials.json` は deny、
`cat package.json` は allow)。

`patterns.local.txt` の除外行 (`!<basename>` / `!<root 相対パス>`) は **glob operand
の判定には効かない** (glob は既定 / ローカル rules と照合せず dotenv stem とだけ
比較するため、除外する対象がない)。除外が効くのはリテラル path の operand scan と
Read / Edit / Write / Stop の判定で、そこでも `!credentials*.json` のような
**既定 rule を丸ごと打ち消す**書き方は推奨しない (新しい機密 basename が入ったとき
見落とす)。承認した 1 ファイルなら path 形、同名をまとめて外すなら具体 basename の
exclude を重ねる運用が安全:

```
# 承認した 1 ファイルだけ (0.24.0)
!config/myapp-config.json

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

### `resolve_project_root(cwd) -> str | None` (0.24.0)

path 形 rule の基準 root。値は `_resolve_project_key` と**同一** (別名で公開して
いるのは、`[project:<key>]` の key とそのセクションに書いた path 形 rule の基準
root が定義上同じものであることを示すため)。Read / Edit / Bash / Stop の 4 箇所が
同じ root で matcher を呼ぶので、どの hook が出したレシピも他の hook で同じ
1 ファイルに効く。Stop だけ git の toplevel を使うと、monorepo
(`$CLAUDE_PROJECT_DIR` がサブディレクトリ) で Stop のレシピが Read で効かなくなる。

### `_shared/matcher.py` の path 形対応 (0.24.0)

- `is_sensitive(path, rules, *, parts=True, root=None)` — `root` を渡すと `/` を
  含む rule を root 相対 path と比較する。`path` が相対なら root 相対と見なす
  (Stop hook が `git ls-files` の cwd 相対 path を root 相対に組み立てて渡す経路)
- `root_relative(path, root)` — lexical な相対化 (symlink 解決なし)。root 自身・
  配下でない・`..` で外に出るものは `None`
- `_translate_path_pattern(pattern)` — gitignore 準拠の translator。`fnmatch` の
  `*` は `/` を跨ぐので継承せず、自前で正規表現に変換する (キャッシュは
  basename 形と同様に上限なし)
