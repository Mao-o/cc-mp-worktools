# Changelog

## 0.3.2

Bash handler を **誤爆ガード緩和** 方向に再設計。autonomous 実行モード
(`--permission-mode auto` / `bypassPermissions`) を選んだユーザーが日常コマンドで
意図せず止められる問題を解消する。`__main__.py` の catch-all 緩和は次バージョン
(0.3.3) 以降に分離。

### 主要な変更

1. **三態判定 `ask_or_allow` を新設** — 静的解析不能ケースを default=ask /
   auto/bypass=allow に切り替える。対象:
   - hard-stop metachar (`$`, バッククォート, `(`, `)`, `{`, `}`, `<<`, `<(`, fd-dup)
   - shell wrapper / インタプリタ (`bash -c`, `eval`, `python3 -c`, `sudo`,
     `awk`, `sed`, `xargs`, `parallel`, `time`, `!`, `exec` 等)
   - shell keyword / 制御構文 (`if`, `for`, `while`, `do`, `coproc` 等)
   - basename が透過対象でない絶対/相対パス実行 (`/bin/cat`, `./script`)
   - 残留 metachar (非安全リダイレクト `> out.txt`, quoted `'&2'` 等)
   - shlex.split / normalize 失敗
   - `env -i` / `env -u` / `env --` / `command -p` / `command --` 等のオプション付き
2. **前置き正規化を新設** — 以下の prefix は剥がして再判定し、確定 match なら
   全 mode で `deny` 固定:
   - 環境変数 prefix (`FOO=1`)
   - `env [ASSIGNMENTS...]` (option 無しのみ)
   - `command` / `builtin` / `nohup` (option 無しのみ)
   - 連鎖 (`nohup command cat .env`, `command env FOO=1 cat .env`)
   - 絶対/相対パスでも basename が `env` / `command` / `builtin` / `nohup` のもの
     (`/usr/bin/env FOO=1 cat .env`, `/bin/command cat .env`)
3. **glob 候補列挙 `_glob_operand_is_sensitive` を新設** — operand が glob
   (`*` `?` `[`) を含む場合、(a) operand 自身の literal stem と (b) 既定 rules の
   pt_stem で operand glob に fnmatch する候補を生成し、`is_sensitive` で 1 つでも
   True なら **deny 固定**。include/exclude の last-match-wins は既存ロジックで整合。
   - deny 例: `cat .env*`, `cat .env.*`, `cat *.envrc`, `cat id_rsa*`, `cat id_*`,
     `cat *.key`, `cat cred*.json`, `cat .e[n]v`, `cat [.]env`, `cat .en?`
   - allow 例: `cat *.log`, `cat .env.example*` (全候補が exclude 決着)
4. **`<` 入力リダイレクトの target 抽出** — hard-stop に該当する `< file` 形式は
   target を regex 抽出して先に operand scan に流す。target が機密一致なら全 mode
   で deny。heredoc (`<<EOF`), process substitution (`<(...)`), fd dup (`<&N`),
   数値 fd 前置 (`0<`) は regex で除外され opaque (`ask_or_allow`) に倒る。
5. **`time` / `!` / `exec` を opaque へ移動** — `_SHELL_KEYWORDS` から
   `_OPAQUE_WRAPPERS` に移動 (shell 文法要素 / プロセス置換挙動として統一)。
6. **`patterns.txt` 読込失敗 = bash handler は全 mode で `make_deny` 固定** —
   policy 欠如時に lenient で素通りすることを避ける。Read/Edit handler は
   `ask_or_deny` のまま (regression guard あり)。

### 挙動変更 (0.3.1 → 0.3.2)

| コマンド | mode | 0.3.1 | 0.3.2 |
|---|---|---|---|
| `cat .env*` | 全 mode | ask_or_deny | **deny** (glob 候補列挙) |
| `cat .e[n]v` | 全 mode | ask_or_deny | **deny** (glob 候補列挙) |
| `cat *.log` | 全 mode | ask_or_deny | **allow** (rules と非交差) |
| `FOO=1 cat .env` | 全 mode | ask_or_deny | **deny** (前置き剥がし) |
| `env cat .env` | 全 mode | ask_or_deny | **deny** (前置き剥がし) |
| `command cat .env` | 全 mode | ask_or_deny | **deny** (前置き剥がし) |
| `nohup cat .env` | 全 mode | deny (operand match) | deny (前置き剥がしで同じ結果) |
| `/usr/bin/env FOO=1 cat .env` | 全 mode | ask_or_deny | **deny** (basename=env 透過) |
| `cat < .env` | 全 mode | ask_or_deny | **deny** (target 抽出) |
| `bash -c 'date'` | default | ask | ask |
| `bash -c 'date'` | auto/bypass | deny (旧 ask が bypass で deny) | **allow** |
| `if true; then echo x; fi` | auto/bypass | deny (shell keyword fail-closed) | **allow** |
| `/bin/cat README.md` | auto/bypass | deny (path exec fail-closed) | **allow** |
| `echo foo > out.txt` | auto/bypass | deny (residual metachar) | **allow** |
| `cat .env` 等の確定 match | 全 mode | deny | deny (維持) |
| `patterns.txt 読込失敗` (bash) | default/auto | ask | **deny** |
| `patterns.txt 読込失敗` (bash) | bypass | deny | deny (維持) |

### 既知制限の追記

- `<` 入力リダイレクトの target 抽出は単純 regex。quote を厳密処理しないため
  `cat < "a file.env"` のような quoted space 名は false negative に倒る場合あり
- `__main__.py` catch-all (内部例外) の auto/bypass 緩和は **0.3.3 以降**に分離。
  0.3.2 では従来通り `ask_or_deny` を維持
- `_glob_candidates` は (op_stem + pt_stem) / (pt_stem + op_stem) の連結候補は
  **採用しない** (採用すると `cat *.log` が `.env.log` 候補で false-deny になるため)。
  代わりに「rule 自体が operand glob と直接 fnmatch する」場合のみ候補化する

### 次バージョン (0.3.3) 以降の予定

- `__main__.py` catch-all を auto/bypass で `ask_or_allow` に緩和
- shell wrapper (sudo / awk / sed / bash -c) の更に細かい script 解析

## 0.3.1

unified operand scan による未知コマンド bypass 解消。詳細は README.md の
「0.3.1 リリースノート」参照。

## 0.3.0

セグメント分割 + 安全リダイレクト剥離。詳細は README.md の「0.3.0 リリース
ノート」参照。

## 0.2.0

Case-insensitive matching, Bash handler, Edit/Write handler, fd ベース reader 等。
詳細は README.md の「0.2.0 リリースノート」参照。
