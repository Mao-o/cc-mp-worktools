# D16: 透過 wrapper × インライン env 伝播の監査

このドキュメントは v0.7.0 (D11) で導入した「行頭インライン env を検証 subprocess に
伝播する」設計について、**透過 wrapper のランタイム semantics と静的解析の乖離**を
網羅的に監査した結果を記録する。コードの分類宣言は
`core/command_parser.py` の `_WRAPPER_ENV_CLASS`、回帰テストは
`tests/test_command_parser.py` の `TestWrapperEnvClassificationGuard` /
`TestWrapperEnvPropagationContract` に対応する。

実装者ガイド本体は `CLAUDE.local.md` (gitignore 済みのため worktree には来ない)。
本ファイルは公開 repo に追跡される監査記録。

## 背景: なぜ監査したか

D11 は「静的に解析した行頭インライン env = コマンド実行時の env」という前提で、
剥がした `AWS_PROFILE=prod` 等を検証 subprocess (`aws sts get-caller-identity`
など) に渡す。この前提は **透過 wrapper を跨ぐと崩れる**ことが PR #33 の Codex
レビューで連続して露呈した:

- round1: 複合コマンドの per-service 集約で後段 profile を検証せず誤 allow (c542c18)
- round2: 透過 wrapper 跨ぎの env override 漏れ (c731faf, inner-wins)
- round3 / 8zr: `sudo` が `-E` 無しに継承 env を **scrub** する挙動を未考慮
  → 「検証は prod / 実行は別アカウント」の false-allow (cd13724, `_sudo_preserves_env`)

懸念は **whack-a-mole 化** — wrapper を足すたびに env 挙動の穴が出るのではないか。
本監査でリスト全体を体系化し、再発防止の guard を入れる。

## wrapper × env 挙動の体系化

判定軸: **pre-wrapper の行頭インライン env が、後続コマンドの実行環境に実際に届くか**。

| wrapper | 実行時の env 挙動 | parser の扱い | 伝播可否 | 根拠 |
|---|---|---|---|---|
| `sudo` (preserve 無し) | 継承 env を **scrub** (root の安全環境にリセット) | pre-sudo env を **破棄** (8zr) | **不可** → 破棄 | 実機: `PROBE=x sudo env` に PROBE 出ない (sudoers env_reset 既定) |
| `sudo -E` / `--preserve-env` / `--preserve-env=LIST` | 継承 env を保持 (LIST 形式は一部) | pre-sudo env を収集 | 可 | `_sudo_preserves_env` が flag 領域を走査 |
| `time` (shell keyword / `/usr/bin/time`) | 透過 (子プロセスは env 継承) | pre-wrapper env を収集 | 可 | 実機: `PROBE=x time env` / `/usr/bin/time env` に PROBE 出る |
| `nohup` | 透過 (SIGHUP 無視のみ、env は継承) | 収集 | 可 | 実機確認 |
| `command` | 透過 (shell builtin、外部 CLI を env 継承で起動) | 収集 | 可 | 実機: `PROBE=x command env` に PROBE 出る |
| `builtin` | shell builtin **のみ**起動 (外部 CLI を起動しない) | 収集 (無害) | 可 (無害) | service の PATTERNS は外部 CLI のみ match するため `builtin <cli>` は実質発生しない |
| `exec` | 透過 (現プロセスを置換、env 継承) | 収集 | 可 | 実機: `bash -c 'exec env'` に PROBE 出る |
| `npx` | 透過 (node を env 継承で起動 → 対象を起動) | 収集 | 可 | 実機: `PROBE=x npx node -e ...` に PROBE 届く |
| `pnpm exec` / `pnpm dlx` | 透過 (対象を env 継承で起動) | 収集 | 可 | node ランチャ semantics (env filter なし) |
| `mise exec --` | 透過 (mise の env 注入 **に加えて** 継承 env も渡す) | 収集 | 可 | 実機: `PROBE=x mise exec -- env` に PROBE 出る |
| `bun x` | 透過 (対象を env 継承で起動) | 収集 | 可 | bun ランチャ semantics |
| `env` (オプション無し: `env FOO=bar cmd`) | 継承 env + 指定 `KEY=VAL` を渡す | 剥がして collect (pre/post 両方) | 可 | POSIX env |
| `env -i [...]` | 環境を **空にリセット**してから起動 | **剥がさない** (opaque) | 不可 → スキップ | 実機: `PROBE=x env -i env` に PROBE 出ない |
| `env -u NAME [...]` | NAME のみ **unset** | **剥がさない** (opaque) | 不可 → スキップ | POSIX env |
| `env -- [...]` | flag 終端 (以降を素の env で起動) | **剥がさない** (opaque) | スキップ (安全側) | parser は `-` 始まりトークンで env strip を中止 |

(実機 probe は darwin / bash / mise 2.x / node 25 で 2026-06 に確認。詳細は本監査の
コミットメッセージ参照。)

## 伝播してよい env / 保守的にスキップすべき経路の方針

### 1. passthrough wrapper — 伝播してよい

`time` / `nohup` / `command` / `builtin` / `exec` / `npx` / `pnpm exec` /
`pnpm dlx` / `mise exec --` / `bun x` は継承 env を素通すため、pre-wrapper の
インライン env を収集して検証 subprocess に渡す (D11 本来の意図どおり)。
`_WRAPPER_ENV_CLASS` で `"passthrough"` と宣言する。

### 2. conditional_scrub wrapper — フラグ依存、既定はスキップ側

`sudo` のみ。`-E` / `--preserve-env` / `--preserve-env=LIST` があれば継承 env を
保持するので伝播してよいが、無ければ scrub するので **pre-sudo env を破棄する**
(8zr の `_normalize_segment` 補正)。`--preserve-env=LIST` のリスト内容や sudoers の
`env_keep` / `env_reset` まで静的には不可知なので、**preserve 指定があれば保守的に
伝播を許す** (保持しすぎ方向は誤 deny を増やすだけで安全側)。

### 3. env のリセット系 — wrapper として剥がさない (opaque)

`env -i` / `env -u NAME` / `env --` は **透過 wrapper として剥がさない**。剥がして
後続を裸のコマンド扱いにすると「実行は空/縮小環境 / 検証は親環境」の非対称が生じ、
本来 deny すべきケースを誤って検証スキップしてしまう。剥がさず opaque のまま残すと、
セグメント (`env -i aws ...`) は service の PATTERNS (`^aws\b`) に match せず、検証
自体がスキップされる。これは「静的解析不能なら検証しない (= allow 相当)」という
既存ポリシー (`bash -c`/`eval` と同じ) に合致する。

### スキップに倒すと安全側である理由 (誤 deny 回避ポリシーとの整合)

verify-cloud-account の方針は **誤 allow より誤 deny を回避** (ログイン済みなら通す)。
env を伝播しない方向に倒したとき:

- **env を破棄 (sudo scrub)**: 検証はデフォルト env で走る。
  - ログイン済み & デフォルト env でアカウント一致 → allow (誤 deny にならない)
  - 不一致 / 未ログイン → deny (安全側。false-allow を防ぐ本来の目的)
- **検証スキップ (env -i 等)**: そのセグメントは検証されず素通り (allow 相当)。
  - これは「静的解析できない経路は検証しない」既存ポリシーと同じ譲歩。env -i で
    環境を空にして cloud CLI を叩く運用は稀で、叩いても CLI 側が credentials を
    見つけられず失敗するため実害は限定的。

いずれも **「検証 env = 実行 env」を保てない経路では、誤った env で検証を通すより
保守的に倒す**という一貫した判断。誤 allow (未承認アカウントで mutating 実行) だけは
確実に避ける。

## 結論: 現リストは健全。再設計は不要

8zr の `sudo` scrub 補正で、現行 wrapper リストの env 挙動はすべて正しく分類・処理
されている。**sudo が唯一の conditional_scrub であり、env -i/-u/-- が唯一の reset
形式で、どちらも対応済み**。残る passthrough wrapper は実機で env 素通しを確認済み。

過剰な再設計 (例: 全 wrapper を allow-list 化 / 汎用 scrub 検出) は誤 deny を増やす
リスクがあり、誤 deny 回避ポリシーに反するため **採用しない**。代わりに、再発防止の
**分類 guard** (`_WRAPPER_ENV_CLASS` + テスト) を入れて、将来の wrapper 追加時に
env 挙動の分類を機械的に強制する。

## 将来 wrapper を追加するときのチェックリスト

`ssh` / `docker run -e` / `kubectl exec` / `xargs` / `timeout` / `stdbuf` /
`setsid` などを透過 wrapper に足したくなったら、**必ず以下を順に実施**する。
途中を飛ばすと `TestWrapperEnvClassificationGuard` が落ちる。

1. **env 挙動を実機で確認**する。`PROBE=x <wrapper> <args> env` (または
   `/usr/bin/env`) に `PROBE` が出るか:
   - 出る → `passthrough`
   - 出ない / 一部のみ → `conditional_scrub` (または非対応)
2. `_WRAPPERS_SINGLE` / `_WRAPPERS_TWO` / `_WRAPPERS_THREE` に wrapper を追加する。
3. `_WRAPPER_ENV_CLASS` に **同じキーで分類を追加**する
   (`"passthrough"` / `"conditional_scrub"`)。
4. `conditional_scrub` の場合は **scrub 補正ロジックと回帰テストを追加**する
   (`sudo` の `_sudo_preserves_env` + `_normalize_segment` の `collected.clear()`
   が雛形)。`test_only_sudo_is_conditional_scrub` も更新する。
5. wrapper が **値を取るフラグ** (`-X value`) を持つなら
   `_WRAPPER_FLAGS_WITH_VALUE` に登録する (`sudo -u deploy` の `gh` 誤消費を防ぐ)。
6. `TestWrapperEnvPropagationContract` の `PASSTHROUGH_CASES` 等に
   **env 伝播/非伝播の固定化ケースを追加**する。
7. 本ドキュメントの表と README の wrapper 節を更新する。

### 特に注意が要る将来 wrapper

| 候補 | env 挙動の罠 | 推奨分類 |
|---|---|---|
| `ssh host cmd` | リモートで実行され **ローカル env は届かない** (`SendEnv`/`AcceptEnv` 次第)。そもそも別ホストなのでローカル CLI 検証の意味が薄い | 透過 wrapper に **足さない** (検証スキップが妥当) |
| `docker run -e FOO ...` | コンテナ内 env はホスト行頭 env と無関係。`-e`/`--env`/`--env-file` を解析しないと誤伝播 | 足すなら専用解析が必須。安易な passthrough は不可 |
| `kubectl exec -- cmd` | Pod 内で実行。ローカル env は届かない | 足さない (検証スキップ) |
| `xargs cmd` | stdin からの引数で cmd を起動。env は継承するが起動回数・引数が動的 | passthrough だが segment 抽出が別問題 |
| `timeout 5 cmd` | 透過 (env 継承)。値を取る第1引数 (duration) の消費に注意 | `passthrough` + `_WRAPPER_FLAGS_WITH_VALUE` 相当の引数処理 |
| `stdbuf -oL cmd` / `setsid cmd` | 透過 (env 継承) | `passthrough` |

`ssh` / `docker` / `kubectl exec` のように **「別の実行コンテキストへ移送する」
wrapper は、ローカル行頭 env が届かないので透過 wrapper に足さない**のが原則
(足すと「検証 env ≠ 実行 env」を再生産する)。検証スキップ (allow 相当) に倒すのが
誤 deny 回避ポリシーと整合する。
