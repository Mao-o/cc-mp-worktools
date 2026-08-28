"""両 hook 共有のロジック。

- matcher: last-match-wins 判定 (basename 形 = fnmatch / path 形 = project root
  相対 path に対する gitignore 準拠 translator、0.24.0)
- patterns: patterns.txt / patterns.local.txt のロード

ログや envelope 依存は含めない (両 hook で方針が違うため呼出側に委譲する)。
"""
