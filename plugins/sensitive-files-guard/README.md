# sensitive-files-guard

機密ファイル (`.env`, `*.secret`, `*.local.*`, 秘密鍵, 証明書, クレデンシャル) が
Claude Code セッション経由で漏れる事故を、1 プラグインで予防する多段 hook セット。

| 事故 | 対応 hook | タイミング |
|---|---|---|
| `Read` で `.env` の**実値**が LLM コンテキストに載る | `redact-sensitive-reads` | `PreToolUse` (Read) |
| `Bash` の `cat .env` / `source .env` で実値が観測される | `redact-sensitive-reads` | `PreToolUse` (Bash) |
| `Edit` / `Write` で機密パスに書き込み | `redact-sensitive-reads` | `PreToolUse` (Edit/Write) |
| `.env` / 秘密鍵を **tracked / untracked** のまま残す | `check-sensitive-files` | `Stop` |

> **MultiEdit について**: 現行 Claude Code CLI (2.1.x) には `MultiEdit` tool が
> 搭載されていないため、本 plugin の `hooks.json` からも matcher を除外しています。
> Edit の `replace_all` オプションで同等の複数箇所書き換えがカバーされる仕様です。
> 将来 MultiEdit が再搭載された場合、handler (`handlers/edit_handler.py`) と
> argparse choices は既に multiedit 対応が残っているため、`hooks.json` に matcher
> を 1 エントリ追加するだけで復活できます。

両 hook は同一の `patterns.txt` (本 plugin 内
`hooks/check-sensitive-files/patterns.txt`) を共有する。パターンを 1 箇所で管理できる。
Matcher ロジックは `hooks/_shared/` に集約されており、Read 側と Stop 側で判定が剥離しない。

## インストール

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install sensitive-files-guard@mao-worktools
```

有効化すると `PreToolUse(Read | Bash | Edit | Write | MultiEdit)` / `Stop` の
hook が自動登録される。`settings.json` を手で編集する必要はない。

## 挙動

### `PreToolUse(Read)` — redact-sensitive-reads

Claude が `Read` で機密パターンに一致するファイルを開こうとすると、

1. 通常ファイル → `deny` + `permissionDecisionReason` に **鍵名・順序・型・件数のみ** を返す
2. symlink / FIFO / 特殊ファイル → `ask` (bypass モード下は `deny`)
3. 32KB 超の大ファイル → streaming で鍵名のみ抽出

返却される reason 例:

```
<DATA untrusted="true" source="redact-hook" guard="sfg-v1">
NOTE: sanitized data from a sensitive file. Real values are NOT in context.
file: .env
format: dotenv
entries: 3
keys (in order):
  1. DATABASE_URL  <type=str>
  2. JWT_SECRET    <type=jwt>
  3. DEBUG         <type=bool>
note: all values and comments removed for safety.
</DATA>
```

値は一切含まれない (bool/null も型のみ)。包装 DATA タグは本文中の `</DATA>` や
`<DATA` をエンティティ化して外殻が破壊されないよう保護する (`guard="sfg-v1"` は
識別用の固定マーカー)。

### `PreToolUse(Bash)` — redact-sensitive-reads (0.3.1)

Claude が `Bash` で機密ファイルに触れるコマンドを実行しようとすると、
hook が静的解析し **deny 固定** (bypass 有無に関わらず) で block する。
判定不能 (間接経路) は fail-closed で **ask_or_deny** (non-bypass は ask、bypass は deny)。

0.3.0 で **セグメント分割** (`&&` `||` `;` `|` `\n`) と **安全リダイレクト剥離**
(`>/dev/null` `2>/dev/null` `2>&1` `&>/dev/null` など) に対応。
0.3.1 で **unified operand scan** に変更 — コマンド名ベースの allow-list
(`_SAFE_READ_CMDS`) を廃止し、**全てのセグメントで非 option トークンの basename を
機密判定**する。`grep SECRET .env`, `base64 .env`, `timeout 1 cat .env`,
`git show HEAD:.env` のような未知コマンド / wrapper / VCS pathspec 経由の
bypass を塞ぐ。非機密 operand なら未知コマンドでも allow を維持 (ergonomics)。

**カバー範囲 (matrix)**:

| コマンド | 判定 | 備考 |
|---|---|---|
| `cat .env`, `less .env`, `head .env`, `tail .env`, `source .env`, `. .env` | **deny** | 単純読み取り / dotenv source |
| `head -n 1 .env`, `cat -- .env`, `tail -f .env` | **deny** | option 付き |
| `cat .env && pwd`, `false \|\| cat .env`, `cat .env; pwd`, `cat .env \| head` | **deny** | 複合セグメント中に機密 path (0.3.0) |
| `cat .env 2>/dev/null`, `cat .env > /dev/null` | **deny** | 安全リダイレクト剥離後も機密 path (0.3.0) |
| `grep SECRET .env`, `base64 .env`, `xxd .env`, `od -An -tx1 .env`, `hexdump -C .env` | **deny** | 未知コマンド + 機密 operand (0.3.1) |
| `timeout 1 cat .env`, `nohup cat .env`, `nice cat .env`, `busybox cat .env` | **deny** | wrapper 経由 (0.3.1) |
| `cp .env /tmp/x`, `mv .env .env.old` | **deny** | 機密 path を operand に取るコピー/移動 (0.3.1) |
| `curl file://.env`, `git show HEAD:.env`, `git cat-file -p :.env` | **deny** | URI / VCS pathspec (0.3.1) |
| `cat .env.example`, `head README.md` | allow | テンプレ除外 / 非機密 |
| `echo foo`, `ls -la`, `npm test`, `date`, `pwd`, `make build` | allow | 非機密 operand |
| `grep foo README.md`, `cat README.md 2>/dev/null`, `ls \| head` | allow | 未知コマンド + 非機密 operand (0.3.1) |
| `git status && git log 2>/dev/null \|\| true` | allow | 全セグメント非機密 (0.3.0) |
| `git commit -m 'update docs'` | allow | operand basename が機密パターン不一致 |
| `cat .env*`, `cat [.]env`, `cat *.log` | **ask_or_deny** | glob (0.3.1, 展開後不明) |
| `cat $X`, `cat "$X"`, `cat $(echo .env)` | **ask_or_deny** | 変数展開 (hard-stop) |
| `cat \`echo .env\`` | **ask_or_deny** | コマンド置換 (hard-stop) |
| `< .env cat`, `cat << EOF ... EOF` | **ask_or_deny** | 入力リダイレクト / heredoc (hard-stop) |
| `(cat .env)`, `{ cat .env; }` | **ask_or_deny** | グループ化 (hard-stop) |
| `for i in 1; do cat .env; done`, `if true; then cat .env; fi` | **ask_or_deny** | シェル制御構文 (予約語で始まるセグメント) |
| `while cat .env; do pwd; done`, `case 1 in 1) cat .env;; esac`, `coproc cat .env` | **ask_or_deny** | シェル制御構文 |
| `time cat .env`, `! cat .env`, `eval cat .env` | **ask_or_deny** | 予約語 / 評価ラッパ |
| `echo foo > out.txt`, `cat foo >> bar.txt` | **ask_or_deny** | /dev/null 以外へのリダイレクト |
| `/bin/cat .env`, `./cat .env`, `../bin/cat .env` | **ask_or_deny** | 絶対/相対パス実行 |
| `FOO=1 cat .env`, `env X=1 cat .env` | **ask_or_deny** | env prefix |
| `bash -c "cat .env"`, `sh -c "..."`, `zsh -c "..."` | **ask_or_deny** | shell wrapper |
| `sudo cat .env`, `command cat .env`, `xargs -a .env cat` | **ask_or_deny** | 権限/ラッパ |
| `python -c "..."`, `node -e "..."`, `ruby -e "..."` | **ask_or_deny** | インタプリタ経由 |

**静的に機密 operand を検出したケースは deny 固定**。
間接経路 (動的展開 / glob / 制御構文) は判定不能なので ask_or_deny でユーザー介在。
許したい basename は `patterns.local.txt` に `!<basename>` exclude を追加する運用。

**False positive の注意**: 0.3.1 の unified operand scan は「コマンドが実際に
file を読むかどうか」を判別しないため、`echo .env` `ls .env` のように文字列
表示だけの呼び出しでも operand が機密パターンに一致すれば deny される。許可したい
場合は `patterns.local.txt` に `!<basename>` exclude を追加するか、引数を
リテラルに埋め込まない形に書き換える。

**クォート内の metachar も保守的に fail-closed** になる場合があります:
`git log --format='{"sha":"%H"}'` は `{` を含むため hard-stop で ask_or_deny に
倒れます (クォート内でも文字列レベルで検出)。一方 `echo "a && b"` のような
quote 内のセグメント演算子は splitter が quote を尊重するため誤分割しません。
機密 path を触らない用途なら非 bypass モードで allow 可能、bypass モード下では
deny されます。

### `PreToolUse(Edit | Write)` — redact-sensitive-reads (0.2.0)

`tool_input.file_path` が機密パターン一致なら **新規/既存問わず deny 固定**。
書き込み経路から機密データが混入/置換される事故を防ぐ。ask は挟まない
(実機観測でうっかり承認による既存値喪失が発生した教訓から)。

| ケース | 判定 |
|---|---|
| 既存 `.env` を Edit/Write | **deny** |
| **新規** `.env` を Write (作成) | **deny** |
| `.env.example` / `config.template` を Edit/Write | allow (テンプレ除外) |
| path 最終要素が symlink (機密一致) | **deny** |
| path 最終要素が special (FIFO/socket/device) | **deny** |
| 親ディレクトリが symlink / 特殊 / 不在 | **ask_or_deny** (判定不能、fail-closed) |
| patterns.txt 読込失敗 / normalize 失敗 / stat 失敗 | **ask_or_deny** (判定不能、fail-closed) |

テンプレ用途で自作の名前 (`foo.template.env` 等) を除外したい場合は
`patterns.local.txt` に `!foo.template.env` を追加する。
`ask_or_deny` になるのは「判定ロジック自体が失敗した」ケースのみで、
機密判定済みの書き込みは常に deny。

#### deny reason のキー名ガイド (0.2.0)

dotenv 系 (`.env` / `.env.*` / `foo.env` / `.envrc`) を Edit/Write で block した
際、`tool_input` から追加予定のキー名を抽出して reason に代替案として添える。
ユーザーは reason を見て「どのキーを `.env.example` に移せばよいか」がわかる。

例 (Write で `DATABASE_URL` / `JWT_SECRET` / `DEBUG` を書こうとした場合):

```
Write: 機密パターン一致のファイル (.env) への書き込みは block されました
(値喪失や機密流出防止のため)。

代替案: 追加予定のキー名を `.env.example` に追記すると、差分把握がしやすく
なります (値は後で個別設定):
  DATABASE_URL=
  JWT_SECRET=
  DEBUG=

許可したい場合は `patterns.local.txt` に `!<basename>` を追加してください。
```

値そのものは reason に含まれない (キー名のみ)。非 dotenv (例: `credentials.json`)
では keys 抽出をスキップし、基本メッセージのみ返す。キー数が 30 を超える場合は
先頭 30 + `... (N more)` で切り詰めて `MAX_REASON_BYTES=3072` に収める。

#### Edit/Write hook の発火経路 (実機観測)

現行 Claude Code CLI 2.1.112 の Edit/Write は内部に **「Read 済み前提」チェック** が
あり、未 Read の既存ファイルに対する Edit/Write は **hook 到達前に Claude Code
自身がエラーを返す**。つまり:

- **既存 `.env` への Edit/Write**: Read 前提チェック (Claude Code 内蔵) で block。
  hook は発火しない
- **新規 `.env` の Write**: Read 前提なし → **hook が発火して deny で block**
- **`.env.example` への Edit/Write**: exclude 対象 → allow

既存ファイルへの Edit/Write は **redact の Read deny が連鎖的な副次防御** として
機能する (Claude が Read できない → Edit/Write の前提を満たせない → Claude Code
が block)。hook の Edit/Write matcher は **新規作成経路の防御本線** として必要。

#### Read と Edit/Write の symlink 対応の非対称性

同じ「機密 path + symlink」でも tool によって判定が違います:

| tool | 機密 + symlink | 理由 |
|---|---|---|
| `Read` | `ask_or_deny` (非 bypass は ask) | symlink 先が意図した参照 (共有 template / 外部参照) の可能性がある。ユーザー介在で判断 |
| `Edit` / `Write` | **`deny` 固定** | 書き込み先が意図せず外部 path を向くと実害が不可逆。ask なしで block |

これは意図した設計 (破壊的操作は保守的に倒す) です。許可したいケースは
`patterns.local.txt` で exclude 追加する運用。

### `Stop` — check-sensitive-files

応答が終わるたびに cwd が git 管理下なら、**tracked / untracked を問わず**機密パターンに
一致するファイルを検出して `decision: block` で Claude に再確認を促す。

- **tracked**: `.gitignore` 済みでも block される
  (`git rm --cached` が必要なため)。対応は「`.gitignore` に追加 + `git rm --cached <path>`」
- **untracked**: `.gitignore` 済みのものは `git ls-files --others --exclude-standard`
  により既に除外済み。対応は「`.gitignore` に追加 or 意図的に管理対象化」
- **submodule**: 0.2.0 以降、`git ls-files --recurse-submodules` で submodule 内の
  **tracked** も検査対象。submodule 内の **untracked** は現状範囲外 (既知制限)

block reason には tracked / untracked を別セクションで列挙し、それぞれ対応手順を添える。

**注意**: 2 回目以降の `Stop` は `stop_hook_active=true` で素通りする (無限ループ防止)。
**block が見えたら必ず対応する**。無視して次のターンに進むと、以降はチェックが効かなくなる。

## パターン設定

### 既定 patterns.txt

`hooks/check-sensitive-files/patterns.txt` が plugin 同梱。fnmatch 書式、
`!` プレフィクスは除外。

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

### ローカル拡張 (`patterns.local.txt`)

ユーザー個別のパターンは plugin を fork せずに
`$XDG_CONFIG_HOME/sensitive-files-guard/patterns.local.txt` (未設定時
`~/.config/sensitive-files-guard/patterns.local.txt`) に書ける。
両 hook が自動で合流して読み込む。

#### 初回作成手順

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

### 評価方式: **last-match-wins** (大文字小文字無視)

rules は `既定 → ローカル` の順で連結し、**最後にマッチしたルール**の include/exclude で
判定する (gitignore 風)。どれにもマッチしなければ非機密。
0.2.0 以降 **既定で case-insensitive** (`.ENV` や `ID_RSA` も検出)。

これにより:
- 既定除外をローカル側で打ち消せる (例: 既定 `!*.pub` をローカル `*.pub` で include に戻す)
- ローカルで exclude を追加して特定 basename を除外できる (例: `!fixture-*.pem`)
- OS による大文字小文字の扱い差 (macOS HFS+ / Linux ext4) に依存しない

### Case-sensitive opt-out

旧 0.1.x 系の挙動 (OS 依存 case) に戻したい場合は環境変数
`SFG_CASE_SENSITIVE=1` を設定する。既定は unset (= case-insensitive)。

```bash
export SFG_CASE_SENSITIVE=1  # 旧挙動に戻す
```

### basename のみで判定される (parts は補助)

両 hook ともパターンは **basename** に対して fnmatch する (0.2.0 以降、Stop 側も
Read 側と同じく親 dir 名の parts も補助的に評価する)。ディレクトリ固有の exclude は
書けない:

```
# NG: パスセグメントは効かない
!fixtures/*.pem

# OK: basename だけで区別する
!fixture-*.pem
!test-*.pem
!ca-*.pem
!ca-bundle.pem
```

### `*.pem` / `*.key` の false positive 対策例

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

## 既知制限 (0.3.0 時点)

1. **MCP 経路は対象外** — MCP server 経由のファイルアクセスは hook が介在しない
2. **Bash 間接アクセス** — `< .env`, `command cat`, `env VAR=... cat`,
   `xargs -a .env`, `$VAR`, `$(...)`, heredoc, base64 decode, `/bin/cat`,
   `bash -c`, `bash -lc`, `FOO=1 source .env` などは静的解析不能のため全て
   **ask (fail-closed)** で倒す。
   0.3.0 で `&&` `||` `;` `|` `\n` による複合コマンドと、`>/dev/null`
   `2>&1` 等の安全リダイレクトは静的解析対象に含まれた (セグメント単位で判定)
3. **親ディレクトリ差し替え race** — `O_NOFOLLOW` は最終要素のみ保護し、
   途中要素の symlink 差し替え race は対象外 (原理的に completely 防御不能)
4. **TOCTOU 完全排除は非目的** — hook での読取と Claude 実 Read/Write の分離は
   範囲外 (fd ベース reader により「同一プロセス内の再 open」race は排除済み)
5. **`<DATA untrusted>` モデル解釈保証なし** — 包装 + sanitize + DATA タグ
   エスケープで多段防御するが、モデルが敵対的文脈として扱う保証は無い
6. **Windows は現状非対応** — SIGALRM 依存。Windows 起動時は fail-closed で
   deny exit する (Step 0-c 実測結果で方針更新予定)
7. **submodule 内 untracked は非対象** — `git ls-files --recurse-submodules` は
   tracked のみ。untracked を submodule 内まで拾う git native オプションは無い
8. **Git バージョン依存** — `--recurse-submodules` は git 1.7+ が必要。古い
   環境では fallback で素の `ls-files` を使うが、submodule 検査は効かない
9. **`!` プレフィックス (Claude Code bash mode) は防御対象外** — ユーザーが
   プロンプトに `! cat .env` と直接入力してシェルコマンドを実行した場合、
   公式仕様により **stdout が transcript に追加されて LLM コンテキストに流れ込む**
   (`interactive-mode.md` の Quick commands 参照)。これはユーザーの明示的な
   意思操作なので hook の介在外。「`!` で実行すれば Claude に見られない」という
   案内は誤りなので注意

### 感度差と品質のズレ

- **basename vs parts 感度差は残る (軽微)** — 0.2.0 で Stop 側も parts 評価する
  ようになったが、両 hook が異なる envelope / 実行タイミングを扱うため完全対称
  ではない
- **カスタム patterns と redaction 品質のズレ** — ユーザーが `patterns.local.txt`
  で `foo.env` を追加すると、0.2.0 以降は `foo.env` / `*.envrc` も dotenv 扱いで
  型抽出される (0.1.x では opaque だった)

### Fail-closed vs fail-open

| hook | 機密検出時 | 判定不能時 (fail-closed) | 備考 |
|---|---|---|---|
| `redact-sensitive-reads` (Read) | **deny** + minimal info | **ask_or_deny** | non-bypass は ask、bypass は deny |
| `redact-sensitive-reads` (Bash/Edit/Write/MultiEdit) | **deny 固定** | **ask_or_deny** | ask を挟まない (うっかり承認防止) |
| `check-sensitive-files` (Stop) | `decision: block` | **fail-open** (exit 0 + 空出力) | patterns.txt 読込失敗時は stderr warning のみ |

> `MultiEdit` は現行 Claude Code 非搭載のため本表から除外しています (同梱 handler と
> argparse choices は残っており、将来復活時は hooks.json に matcher を追加するだけ)。

Stop 側で fail-closed にすると Claude の応答を止め続けることになるため、
fail-open + stderr 警告で固定。

## 設計上のトレードオフ

- **Vibe Coder の誤操作予防**が目的。敵対的防御 (prompt injection, 悪意ある
  agent) は非目的
- 完全な情報遮断ではない。basename と鍵名は LLM に見える
- TOCTOU race は完全には防げない
- Python 3.11+ で tomllib を使う。3.11 未満は opaque fallback
- Git 1.7+ が submodule scan に必要

詳細は [CLAUDE.md](./CLAUDE.md) 参照。

## 0.3.1 リリースノート

0.3.0 の bash_handler を **unified operand scan** に再設計し、未知コマンド /
wrapper / VCS pathspec / 制御構文セグメント経由の機密 path bypass を塞ぐ。
Codex 自動レビューで累計 4 件の P1 指摘を受けた内容を一括解決している。

### 主要な変更

1. **unified operand scan** — コマンド名ベースの allow-list (`_SAFE_READ_CMDS`)
   を廃止。全セグメントの非 option トークンを機密判定し、一致すれば **deny 固定**。
   未知コマンド + 機密 operand の bypass (`grep SECRET .env`, `base64 .env`,
   `timeout cat .env`, `busybox cat .env`) を全て塞ぐ。非機密 operand なら
   未知コマンドでも allow を維持 (`grep foo README.md`, `npm test` 等)。
2. **VCS pathspec / URI 対応** — `git show HEAD:.env`, `curl file://.env`,
   `user@host:/etc/.env` のようにコロンを含む operand は分割して各片の basename
   も機密判定する。
3. **glob operand → fail-closed** — `cat .env*`, `grep SECRET .e[n]v` のように
   `*` `?` `[` を含む operand は shell 展開結果を静的に追えないため **ask**。
4. **予約語追加** — `_SHELL_KEYWORDS` に `coproc` を追加 (0.3.0 で漏れていた)。
5. **escape 数え直し** — `_split_command_on_operators` のダブルクォート閉じ
   判定を「連続バックスラッシュの偶奇」でやり直し。`echo "\\"; cat .env` のような
   偶数バックスラッシュ run で閉じクォートを見落とす bypass を塞いだ (0.3.0.1
   に相当)。

### 挙動変更 (0.3.0 → 0.3.1)

| コマンド | 0.3.0 | 0.3.1 |
|---|---|---|
| `grep SECRET .env` | allow | **deny** |
| `base64 .env` / `xxd .env` / `hexdump .env` / `od .env` | allow | **deny** |
| `timeout 1 cat .env` / `nohup cat .env` / `busybox cat .env` | allow | **deny** |
| `git show HEAD:.env` / `curl file://.env` | allow | **deny** |
| `cp .env /tmp` / `mv .env backup` | allow | **deny** |
| `coproc cat .env` | allow | **ask_or_deny** |
| `cat .env*` / `cat [.]env` / `cat *.log` | allow | **ask_or_deny** |
| `echo "\\"; cat .env` (偶数 backslash) | allow | **deny** |
| `grep foo README.md` (非機密) | allow | allow (維持) |
| `npm test`, `date`, `pwd` | allow | allow (維持) |
| `git status && git log 2>/dev/null \|\| true` | allow | allow (維持) |

### 既知の false positive (0.3.1)

unified operand scan は「コマンドが実際に file を読むか」を判別しない。以下は
**deny になるが実際には値漏れしない** ケース:

- `echo .env` (文字列 `.env` を stdout に表示するだけ)
- `ls .env` (ファイル名メタデータを表示するだけ)
- `mkdir .env` (ディレクトリ作成のみ)
- `touch .env.new` (空ファイル作成のみ)

これらは hook の対象外にしたい場合、`patterns.local.txt` に `!.env.new` のような
明示 exclude を追加するか、引数をリテラル含まない形に書き換える運用になる。

## 0.3.0 リリースノート

0.3.0 は Bash handler の静的解析を拡張し、以下の挙動を追加・変更する。
0.2.0 からの breaking change は「同じコマンドで ask だったものが deny / allow
に確定する」方向のみで、値が新たに LLM に露出する方向の緩和は無い。

### 主要な変更

1. **セグメント分割** — `&&` `||` `;` `|` `\n` を quote-aware に分割して各
   セグメントを独立判定。`git status && git log || true` のような日常複合コマンドが
   allow 可能に。ただし任意のセグメントに機密 path が現れれば **deny 固定**
   (0.2.0 の「単一コマンド時の deny」と整合)。
2. **安全リダイレクト剥離** — `>/dev/null` / `1>/dev/null` / `2>/dev/null`
   / `&>/dev/null` / `>/dev/stderr` / `>/dev/stdout` / `2>&1` / `>&2` 等を
   トークン列から除外してから判定。`cat README.md 2>/dev/null` のような
   単なる stderr 黙殺が allow に。`> out.txt` のような通常ファイルへの
   リダイレクトは剥がさず **ask_or_deny**。
3. **hard-stop の縮小** — `$` `` ` `` `<` `(` `)` `{` `}` `\r` のみ hard-stop。
   `&` `|` `;` `>` `\n` はセグメント分割 / リダイレクト剥離で扱われる。
4. **クォート尊重のセグメント分割** — `echo "a && b"` のようにクォート内に
   演算子がある場合は分割しない。

### 挙動変更 (0.2.0 → 0.3.0)

| コマンド | 0.2.0 | 0.3.0 |
|---|---|---|
| `cat .env && pwd` | ask | **deny** |
| `false \|\| cat .env` | ask | **deny** |
| `cat .env; pwd` | ask | **deny** |
| `cat .env \| head` | ask | **deny** |
| `pwd\ncat .env` | ask | **deny** |
| `git status && git log 2>/dev/null` | ask | allow |
| `cat README.md 2>/dev/null` | ask | allow |
| `ls -la \| head -n 5` | ask | allow |

## 0.2.0 リリースノート (0.1.x からの breaking change)

0.2.0 は複数のセキュリティ強化と false-positive 増加方向の調整を含み、
全体として **breaking release** として扱う。`SFG_CASE_SENSITIVE=1` の opt-out を
除けば、感度は上がる方向にのみ変化する。

### 主要な変更

1. **Case-insensitive 評価を既定化** — `.ENV`, `ID_RSA`, `CREDENTIALS.JSON` を
   検出。旧挙動は `SFG_CASE_SENSITIVE=1` で復帰可能
2. **Bash handler を新設** — `cat .env`, `source .env` 等を **deny 固定**。
   間接アクセス (動的展開) は ask_or_deny (fail-closed)。上記 matrix 参照
3. **Edit/Write handler を新設** — 新規/既存問わず機密 path への
   書き込みを **deny 固定** (ask を挟まない)。MultiEdit は Claude Code 現行版
   で非搭載のため matcher 除外 (handler は復活時のため保持)
4. **deny reason にキー名ガイド** — dotenv 系への Write/Edit block 時、
   追加予定のキー名を reason に列挙して `.env.example` への移行を案内
5. **Submodule 内 tracked を Stop hook の検査対象に追加** — git 1.7+ 必要
6. **`foo.env` / `*.envrc` を dotenv 扱い + patterns.txt に `.envrc`/`*.envrc`** —
   matcher と engine 双方で direnv 対応
7. **fd ベース reader (TOCTOU 緩和)** — path の再 open を排除
8. **DATA タグ強化** — 固定 `guard="sfg-v1"` marker、body の `</DATA>` /
   `<DATA` / `<data>` をエンティティ化、MAX_REASON_BYTES を 4KB → 3KB に縮小
9. **Matcher / patterns のロジックを `hooks/_shared/` に集約** — 両 hook で
   同じ実装を参照 (剥離防止)
10. **Windows fail-closed** — hook 冒頭で SIGALRM 非対応環境は deny exit

## テスト

プラグイン開発時は以下で unittest を実行する:

```bash
# redact-sensitive-reads
cd hooks/redact-sensitive-reads
python3 -m unittest discover tests

# check-sensitive-files
cd hooks/check-sensitive-files
python3 -m unittest discover tests
```

pytest でも動く (各 tests ディレクトリの `conftest.py` が sys.path を整える)。

## ログ

`redact-sensitive-reads` の動作ログは `~/.claude/logs/redact-hook.log` に書かれる
(plugin cache が消えても残るよう `$HOME` 側に固定)。ログには鍵名・パス・値を
一切書かない (エラー種別・classify 結果のみ)。

## 互換性

- Claude Code CLI 2.1.100+ 想定
- Python 3.11+ 想定 (標準ライブラリのみ、`pip install` 不要)
- Git 1.7+ (submodule scan 用)
- macOS / Linux 対応、Windows 非対応 (現状 fail-closed で deny)
