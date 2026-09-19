# `paths` 自動ロードと `context: fork` の相互作用 (実測)

計測: **Claude Code 2.1.276** / plugin 0.24.0 を `--plugin-dir` でセッション限定ロード / 2026-09-19

## 背景

3 SKILL はいずれも `context: fork` で、本文は 200 行超ある。`researching-claude-docs` の
`paths` は `**/SKILL.md` / `**/.claude/skills/**` / `**/hooks.json` など広く取っているため、
SKILL.md が多い repo では「マッチするファイルを触るたびに SKILL.md 本文が親 context に
注入され、1 回あたり数千トークンの固定費になるのでは」という懸念があった。

公式記述は `paths` を次のように説明するだけで、"loads" が本文注入を指すのかは読み取れない:

> Glob patterns that limit when this skill is activated. (...) When set, Claude loads the
> skill automatically only when working with files matching the patterns.
> — [Extend Claude with skills / Frontmatter reference](https://code.claude.com/docs/en/skills#frontmatter-reference)

## 計測方法

headless の nested セッションを 2 本流し、イベント列を突合した。

- **run A**: cwd = SKILL.md を多数含む repo。`**/SKILL.md` にマッチする SKILL.md を
  1 回だけ Read させ、他のツールを使わせないタスク
- **run B**: cwd = マッチするファイルが 1 つも無い空ディレクトリ。ツール不使用のタスク

どちらも権限モードを自動判定にし stdin を閉じて起動する (headless ではこの 2 つが必須。
編集自動承認モードは subagent 起動を承認できずハングし、権限バイパスは呼び出し側で拒否される)。
出力は stream-json + hook イベント込みで全件記録した。

判定には **各 SKILL.md の本文にしか現れない文字列** (H1 見出しなど) を使う。description に
も出る語は skill 一覧由来の出現と区別できないため使わない。

## 結果

| 観測項目 | run A (マッチする Read あり) | run B (マッチ無し) |
|---|---|---|
| fork skill の SKILL.md **本文**の出現 | **0 回** | **0 回** |
| subagent / fork の起動 | なし | なし |
| 3 skill が起動時の skill 一覧にあるか | ある | **ある** |
| マッチする Read の直後のイベント | `commands_changed` (89 entries / 約 35KB) | 発生せず |

1. **`paths` は SKILL.md 本文を親 context に注入しない。** マッチするファイルを読んだ後でも
   fork skill の本文はイベント列に 1 度も現れず、fork の自動起動も起きない
2. `paths` マッチで観測できた変化は **skill 一覧 (name + description) の差し替え**のみ。
   この plugin の 3 skill の一覧エントリは合計 1,089 文字
   (`researching-claude-docs` は 337 文字 / description 210 文字) で、description 段階の
   情報しか含まない
3. **plugin skill は `paths` にマッチするファイルが 1 つも無い環境でも、起動時から一覧に載る**
   (run B)。対照として、同じ環境にある個人スキル (`paths` 有り・fork 無し) は起動時の一覧に
   無く、マッチするファイルを Read した後の `commands_changed` で初めて現れた

## 結論: 設定変更なし

「注入型なら `paths` を `**/.claude-plugin/**` と `hooks.json` に絞る」「`paths` 専用の軽量な
非 fork skill に分離する」という案は**不要**と判定した。

- 本文が注入されないため、`paths` を絞っても親 context のトークンは減らない
- 逆に狭めると、3 skill が一覧から外れて発火機会が減る副作用だけが残る
- README「設計判断: subagent fork + Sonnet は維持する」と矛盾しない

## 裏が取れていないこと

- stream-json は system prompt を出力しないため、**起動時の skill 一覧が実際に消費する
  トークン量は直接測れていない**。測れたのは `commands_changed` payload (description 段階) の
  サイズのみ。本文が注入されないという結論には影響しない
- plugin skill が起動時から一覧に載り、個人スキルは載らない理由は切り分けていない
  (plugin と個人スキルの差なのか別要因なのか)。本 plugin の判断には影響しないが、
  **`paths` を「一覧を絞ってトークンを節約する手段」として使うつもりなら、plugin skill では
  期待どおりに効かない可能性がある**
- 計測は 1 バージョン (2.1.276) の 2 run のみ。`paths` の扱いは実装依存なので、挙動を前提に
  した設計をするなら再計測すること
