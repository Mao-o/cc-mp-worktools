# codex-pr-review

GitHub の PR に付く **Codex の自動レビュー** (`chatgpt-codex-connector[bot]`) を待ち、指摘に対応して
`@codex review` で再レビューを依頼する、という往復を Claude Code に回させるための Skill です。

- Skill: `iterating-pr-codex-reviews` — 待ち方・指摘の優先度・merge 前の確認・打ち切り方の手順
- 同梱スクリプト (`scripts/`):

| スクリプト | 用途 |
|---|---|
| `pr-codex-status.sh <PR> [SINCE]` | reviews / inline 指摘 / 全箇所のリアクション / Codex のエラーコメント / mergeable / checks を 1 回で集計する |
| `pr-codex-trigger.sh <PR> [サマリ本文 or ファイル]` | 修正サマリと `@codex review` を**別々のコメント**で投稿する |
| `pr-commit-safe.sh <メッセージファイル> [--keep]` | `git commit -F` のラッパー。commit メッセージの本文が Bash を検査する hook に引っかかる環境向け |

## 前提

- GitHub リポジトリに Codex の GitHub App が入っていて、PR に自動レビューが付くこと
- `gh` が認証済みであること
- 待機は `ScheduleWakeup` (`/loop` の動的ペース) を使う想定です

## この Skill が扱う罠

実際の運用で踏んだものを手順に織り込んであります。

- Codex は問題が無いとき、コメントを残さず **PR 本文への 👍 リアクションだけ**で終わることがある。
  コメントや review だけを見ていると永遠に待つ
- **利用上限や内部エラーは issue comment で届き**、リアクションも付かない。`pr-codex-status.sh` は
  最新の `@codex review` 以降に Codex のエラーコメントがあれば `Codex verdict: ERROR` と出す
- **PR 本文の 👍 は前のサイクルのものが残る** (同じ人の同じリアクションは 1 つしか付かない)。
  `pr-codex-status.sh` の verdict は最新の `@codex review` 以降の Codex の応答 (review /
  trigger コメントと PR 本文のリアクション / エラーコメント) だけで決める
- `gh api` は既定で 30 件しか返さず、切られるのは常に最新の応答。スクリプトはすべて
  `--paginate` で取得する
- `@codex review` にサマリを混ぜたり、review への返信で依頼したりしても再レビューは走らない
- コマンドを静的に解析する hook などでは、Codex が「別の書き方ならすり抜けられる」という指摘を
  出し続けて収束しないことがある。打ち切り基準を先に決め、脅威モデルを明記して区切る

## 外部送信

`gh` 経由で GitHub API を呼びます。`pr-codex-trigger.sh` は PR にコメントを投稿します
(引数で渡したサマリと `@codex review`)。それ以外のスクリプトは読み取りのみです。

## 要件

- bash / gh / git
