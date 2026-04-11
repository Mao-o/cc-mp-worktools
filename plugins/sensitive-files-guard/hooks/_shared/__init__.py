"""両 hook 共有のロジック。

- matcher: last-match-wins の fnmatch 判定
- patterns: patterns.txt / patterns.local.txt のロード

ログや envelope 依存は含めない (両 hook で方針が違うため呼出側に委譲する)。
"""
