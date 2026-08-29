"""レビュー対象の組み立て (パス解決 / 除外 / 重複抑止 / 予算 / truncate / REVIEW_CLEAN 判定)。"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _testutil
from _testutil import init_repo, load_entry, write

import exclusion

NO_EXCLUSION = exclusion.Policy(
    default_globs=(), default_words=(), extra_globs=(), include_globs=(), code_only=False
)


class ReviewSetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, _testutil.HERMETIC_GIT_ENV)
        self._env.start()
        self.repo = init_repo(os.path.join(self._tmp.name, "repo"))
        self.entry = load_entry()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def resolve(self, claimed, policy=None):
        return self.entry._resolve_paths(self.repo, claimed, policy or NO_EXCLUSION)


class TestResolvePaths(ReviewSetTestCase):
    def test_drops_outside_worktree(self):
        rels, overflow, excluded = self.resolve(
            [os.path.join(self.repo, "a.py"), "/etc/hosts", "/tmp/elsewhere.py"]
        )
        self.assertEqual(rels, ["a.py"])
        self.assertEqual(overflow, [])
        self.assertEqual(excluded, [], "ツリー外は除外ではなく黙って落とす")

    def test_drops_directories(self):
        """v0.3.0 が state に書いた入れ子 repo のディレクトリを掴まないこと。"""
        nested = os.path.join(self.repo, ".claude", "worktrees", "inner")
        os.makedirs(nested)
        rels, overflow, _ = self.resolve(
            [nested, nested + "/", os.path.join(self.repo, "a.py")]
        )
        self.assertEqual(rels, ["a.py"])
        self.assertEqual(overflow, [])

    def test_dedupes_equivalent_paths(self):
        link = os.path.join(self._tmp.name, "link")
        os.symlink(self.repo, link)
        rels, _, _ = self.resolve(
            [os.path.join(self.repo, "a.py"), os.path.join(link, "a.py")]
        )
        self.assertEqual(rels, ["a.py"])

    def test_keeps_claim_order(self):
        """ソートせず claim 順 (= pending に積まれた順) を保つ。

        繰り越し (pending に戻した) パスは次ターンの先頭に来るので、予算超過で繰り越された
        ファイルが新しい編集に毎回追い越されて永久に残ることがない。
        """
        claimed = [os.path.join(self.repo, name) for name in ("z.py", "m.py", "a.py")]
        rels, _, _ = self.resolve(claimed)
        self.assertEqual(rels, ["z.py", "m.py", "a.py"])

    def test_overflow_is_carried_over_not_dropped(self):
        """上限超過分を黙って捨てない (silent truncation にしない)。溢れるのは claim 順の末尾。"""
        count = self.entry.MAX_REVIEW_PATHS + 5
        names = [f"f{i:03d}.py" for i in reversed(range(count))]  # 逆順に claim
        rels, overflow, _ = self.resolve([os.path.join(self.repo, n) for n in names])

        self.assertEqual(rels, names[: self.entry.MAX_REVIEW_PATHS])
        self.assertEqual(
            [os.path.basename(p) for p in overflow],
            names[self.entry.MAX_REVIEW_PATHS :],
            "claim 順の末尾 5 件 (アルファベット順ではない) が次回に繰り越されること",
        )


class TestResolvePathsExclusion(ReviewSetTestCase):
    """zh5.9: 除外パスは rels にも overflow にも入らず、(rel, 理由) として返る。"""

    def test_default_globs_exclude_secrets(self):
        claimed = [
            os.path.join(self.repo, ".env"),
            os.path.join(self.repo, "certs", "server.pem"),
            os.path.join(self.repo, "src", "app.py"),
        ]
        rels, overflow, excluded = self.resolve(claimed, exclusion.load_policy({}))
        self.assertEqual(rels, ["src/app.py"])
        self.assertEqual(overflow, [])
        self.assertEqual([rel for rel, _ in excluded], [".env", "certs/server.pem"])
        for _, reason in excluded:
            self.assertTrue(reason.startswith("既定除外: "), reason)

    def test_excluded_paths_do_not_consume_review_slots(self):
        """除外ファイルが MAX_REVIEW_PATHS の枠を食って、コードが overflow に回らないこと。"""
        self.entry.MAX_REVIEW_PATHS = 2
        claimed = [
            os.path.join(self.repo, ".env"),
            os.path.join(self.repo, "id_rsa"),
            os.path.join(self.repo, "a.py"),
            os.path.join(self.repo, "b.py"),
        ]
        rels, overflow, excluded = self.resolve(claimed, exclusion.load_policy({}))
        self.assertEqual(rels, ["a.py", "b.py"])
        self.assertEqual(overflow, [], "除外分が枠を食っていない")
        self.assertEqual(len(excluded), 2)

    def test_symlink_is_excluded_by_either_name(self):
        """リンク名と実体のどちらが機密に見えても除外する。"""
        os.makedirs(os.path.join(self.repo, "vault"))
        write(self.repo, "vault/c.json", '{"k": 1}\n')
        os.symlink(
            os.path.join(self.repo, "vault", "c.json"),
            os.path.join(self.repo, "credentials.json"),  # リンク名が機密 (実体は無害な名前)
        )
        write(self.repo, ".env", "SECRET=1\n")
        os.symlink(os.path.join(self.repo, ".env"), os.path.join(self.repo, "settings.txt"))
        # 実体が機密 (リンク名は無害)

        policy = exclusion.load_policy({})
        rels, _, excluded = self.resolve(
            [
                os.path.join(self.repo, "credentials.json"),
                os.path.join(self.repo, "settings.txt"),
                os.path.join(self.repo, "a.py"),
            ],
            policy,
        )
        self.assertEqual(rels, ["a.py"])
        self.assertEqual(
            dict(excluded),
            {"credentials.json": '既定除外: 語 "credentials"', ".env": "既定除外: .env"},
            "symlink は実体 (realpath) で扱いつつリンク名でも判定し、当たった名前を通知に出す",
        )

    def test_symlink_target_claimed_before_link_is_still_excluded(self):
        """実体が先に claim され、機密名のリンクが後から claim されても実体ごと除外する。"""
        os.makedirs(os.path.join(self.repo, "vault"))
        write(self.repo, "vault/c.json", '{"k": 1}\n')
        os.symlink(
            os.path.join(self.repo, "vault", "c.json"),
            os.path.join(self.repo, "credentials.json"),
        )
        rels, _, excluded = self.resolve(
            [
                os.path.join(self.repo, "vault", "c.json"),
                os.path.join(self.repo, "credentials.json"),
            ],
            exclusion.load_policy({}),
        )
        self.assertEqual(rels, [])
        self.assertEqual([name for name, _ in excluded], ["credentials.json"])

    def _symlinked_credentials_dir(self) -> None:
        """`credentials/` → `ordinary/` の symlink ディレクトリと tracked 風の実体を用意する。"""
        os.makedirs(os.path.join(self.repo, "ordinary"))
        write(self.repo, "ordinary/data.json", '{"token": "sk-live-DO-NOT-SEND"}\n')
        os.symlink(os.path.join(self.repo, "ordinary"), os.path.join(self.repo, "credentials"))

    def test_symlinked_directory_component_is_judged_lexically(self):
        """Codex P1: symlink ディレクトリ経由の claim は途中の名前 (credentials) で除外する。"""
        self._symlinked_credentials_dir()
        rels, _, excluded = self.resolve(
            [os.path.join(self.repo, "credentials", "data.json")], exclusion.load_policy({})
        )
        self.assertEqual(rels, [], "realpath は ordinary/data.json だが lexical 名で除外される")
        self.assertEqual(excluded, [("credentials/data.json", '既定除外: 語 "credentials"')])

    def test_symlinked_directory_via_root_alias_is_judged_lexically(self):
        """root の別名 (symlink された親) + 途中の symlink ディレクトリの組み合わせでも除外する。"""
        self._symlinked_credentials_dir()
        alias = os.path.join(self._tmp.name, "alias")
        os.symlink(self.repo, alias)
        rels, _, excluded = self.resolve(
            [os.path.join(alias, "credentials", "data.json")], exclusion.load_policy({})
        )
        self.assertEqual(rels, [])
        self.assertEqual([name for name, _ in excluded], ["credentials/data.json"])

    def test_real_path_claim_is_excluded_via_symlink_alias(self):
        """Codex R2 P1: 実体名 (ordinary/data.json) だけで claim されても (Bash 経由の変更)、
        repo 内の symlink `credentials/` → `ordinary/` から別名を作って除外する。"""
        self._symlinked_credentials_dir()
        rels, _, excluded = self.resolve(
            [os.path.join(self.repo, "ordinary", "data.json")], exclusion.load_policy({})
        )
        self.assertEqual(rels, [])
        self.assertEqual(excluded, [("credentials/data.json", '既定除外: 語 "credentials"')])

    def test_real_path_claim_without_symlink_is_reviewed(self):
        os.makedirs(os.path.join(self.repo, "ordinary"))
        write(self.repo, "ordinary/data.json", '{"k": 1}\n')
        rels, _, excluded = self.resolve(
            [os.path.join(self.repo, "ordinary", "data.json")], exclusion.load_policy({})
        )
        self.assertEqual(rels, ["ordinary/data.json"])
        self.assertEqual(excluded, [])

    def test_lexical_relative_keeps_components_below_root(self):
        entry = self.entry
        root = os.path.realpath(self.repo)
        self.assertEqual(
            entry._lexical_relative(root, os.path.join(self.repo, "a", "b", "c.py")), "a/b/c.py"
        )
        self.assertEqual(
            entry._lexical_relative(root, os.path.join(self.repo, "a", "..", "x.py")), "x.py"
        )
        self.assertIsNone(entry._lexical_relative(root, self.repo))
        self.assertIsNone(entry._lexical_relative(root, os.path.join(self._tmp.name, "out.py")))
        # root 配下に root 自身へ戻る symlink があっても、浅い側で切るので途中の名前が残る
        os.symlink(self.repo, os.path.join(self.repo, "self"))
        self.assertEqual(
            entry._lexical_relative(root, os.path.join(self.repo, "self", "secret.txt")),
            "self/secret.txt",
        )

    def test_link_name_is_judged_through_symlinked_root(self):
        """claim 側が symlink 経由の root 表記 (macOS の /tmp → /private/tmp 等) でもリンク名を見る。"""
        os.makedirs(os.path.join(self.repo, "vault"))
        write(self.repo, "vault/c.json", '{"k": 1}\n')
        os.symlink(
            os.path.join(self.repo, "vault", "c.json"),
            os.path.join(self.repo, "credentials.json"),
        )
        alias = os.path.join(self._tmp.name, "alias")
        os.symlink(self.repo, alias)
        rels, _, excluded = self.resolve(
            [os.path.join(alias, "credentials.json")], exclusion.load_policy({})
        )
        self.assertEqual(rels, [], "alias root 経由でもリンク名 credentials.json で除外される")
        self.assertEqual([name for name, _ in excluded], ["credentials.json"])

    def test_code_only_policy_and_env_globs(self):
        policy = exclusion.load_policy(
            {
                exclusion.ENV_CODE_ONLY: "1",
                exclusion.ENV_EXCLUDE: "docs/, *.csv",
            }
        )
        claimed = [
            os.path.join(self.repo, "docs", "design.yaml"),
            os.path.join(self.repo, "notes.md"),
            os.path.join(self.repo, "data", "rows.csv"),
            os.path.join(self.repo, "src", "app.py"),
        ]
        rels, _, excluded = self.resolve(claimed, policy)
        self.assertEqual(rels, ["src/app.py"])
        self.assertEqual(
            dict(excluded),
            {
                "docs/design.yaml": f"{exclusion.ENV_EXCLUDE}: docs/*",
                "notes.md": "CODE_ONLY: .md",
                "data/rows.csv": f"{exclusion.ENV_EXCLUDE}: *.csv",
            },
        )


class TestCollectDiffs(ReviewSetTestCase):
    def test_skips_paths_with_identical_hash(self):
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        batch = self.entry._collect_diffs(self.repo, ["seed.txt"], {})
        self.assertEqual(len(batch.sections), 1)

        again = self.entry._collect_diffs(self.repo, ["seed.txt"], batch.hashes)
        self.assertEqual(again.sections, [], "同一 diff は再レビューに載せない")
        self.assertEqual(again.submitted, [])

    def test_changed_content_reappears(self):
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        first = self.entry._collect_diffs(self.repo, ["seed.txt"], {})
        write(self.repo, "seed.txt", "alpha\nBETA\nGAMMA\n")
        batch = self.entry._collect_diffs(self.repo, ["seed.txt"], first.hashes)
        self.assertEqual(len(batch.sections), 1)
        self.assertEqual(batch.submitted, [os.path.join(self.repo, "seed.txt")])

    def test_empty_diff_is_not_submitted(self):
        """commit 済み / revert 済みのパスはレビューにも復元対象にも入れない。"""
        batch = self.entry._collect_diffs(self.repo, ["seed.txt"], {})
        self.assertEqual(batch.sections, [])
        self.assertEqual(batch.submitted, [])
        self.assertEqual(batch.hashes, {})
        self.assertEqual(batch.deferred, [])

    def test_untracked_and_tracked_are_both_collected(self):
        write(self.repo, "seed.txt", "alpha\nBETA\ngamma\n")
        write(self.repo, "fresh.txt", "new file\n")
        batch = self.entry._collect_diffs(self.repo, ["fresh.txt", "seed.txt"], {})
        joined = "\n".join(batch.sections)
        self.assertIn("fresh.txt", joined)
        self.assertIn("seed.txt", joined)
        self.assertEqual(len(batch.submitted), 2)


class TestCollectBudget(ReviewSetTestCase):
    """時間予算を超えたら打ち切り、未処理パスを deferred として返す。"""

    def test_budget_exhausted_defers_remaining(self):
        for i in range(4):
            write(self.repo, f"f{i}.txt", f"new {i}\n")
        rels = [f"f{i}.txt" for i in range(4)]

        self.entry.COLLECT_BUDGET_SEC = -1  # 1 パス目の判定で即打ち切り
        batch = self.entry._collect_diffs(self.repo, rels, {})
        self.assertEqual(batch.sections, [])
        self.assertEqual(batch.submitted, [])
        self.assertEqual(
            [os.path.basename(p) for p in batch.deferred_time],
            rels,
            "打ち切ったパスは捨てずに (claim 順のまま) deferred として返すこと",
        )
        self.assertEqual(batch.deferred, batch.deferred_time)

    def test_no_deferral_within_budget(self):
        write(self.repo, "f.txt", "new\n")
        batch = self.entry._collect_diffs(self.repo, ["f.txt"], {})
        self.assertEqual(len(batch.sections), 1)
        self.assertEqual(batch.deferred, [])


class TestByteBudget(ReviewSetTestCase):
    """zh5.8: 合計 MAX_DIFF_BYTES はファイル単位で当て、収まらないファイルは hash を記録しない。"""

    def _big(self, rel: str, kib: int, seed: str = "x") -> str:
        """kib KiB ちょうどの新規ファイルを書く (1 行 64 バイト。行境界の切り詰めが効く diff)。"""
        return write(self.repo, rel, _testutil.content_kib(kib, seed))

    def test_file_that_does_not_fit_is_deferred_without_hash(self):
        self._big("a.py", 30)
        self._big("b.py", 30, seed="y")
        batch = self.entry._collect_diffs(self.repo, ["a.py", "b.py"], {})

        self.assertEqual(batch.submitted, [os.path.join(self.repo, "a.py")])
        self.assertEqual(list(batch.hashes), [os.path.join(self.repo, "a.py")])
        self.assertEqual(batch.deferred_size, [os.path.join(self.repo, "b.py")])
        self.assertEqual(batch.deferred, batch.deferred_size)
        self.assertEqual(batch.truncated, [], "30 KB はファイル上限内なので丸ごと送る")
        self.assertLessEqual(
            len("\n".join(batch.sections).encode()), self.entry.MAX_DIFF_BYTES
        )

    def test_smaller_later_file_still_fits(self):
        """収まらないファイルを飛ばしても、残り予算に入る後続ファイルは送る (first-fit)。"""
        self._big("a.py", 30)
        self._big("b.py", 30, seed="y")
        write(self.repo, "c.py", "tiny\n")
        batch = self.entry._collect_diffs(self.repo, ["a.py", "b.py", "c.py"], {})
        self.assertEqual(
            [os.path.basename(p) for p in batch.submitted], ["a.py", "c.py"]
        )
        self.assertEqual([os.path.basename(p) for p in batch.deferred_size], ["b.py"])

    def test_single_huge_file_is_truncated_and_hashed(self):
        self._big("huge.py", 50)
        batch = self.entry._collect_diffs(self.repo, ["huge.py"], {})

        self.assertEqual(batch.submitted, [os.path.join(self.repo, "huge.py")])
        self.assertEqual(len(batch.truncated), 1)
        rel, full_size = batch.truncated[0]
        self.assertEqual(rel, "huge.py")
        self.assertGreater(full_size, self.entry.MAX_FILE_DIFF_BYTES)
        section = batch.sections[0]
        self.assertLessEqual(len(section.encode()), self.entry.MAX_FILE_DIFF_BYTES)
        self.assertIn("(truncated for review", section)
        self.assertIn(os.path.join(self.repo, "huge.py"), batch.hashes, "切り詰め時は hash を記録する")

        # hash は全文で取っているので、未送信の末尾だけ変わっても再掲される
        again = self.entry._collect_diffs(self.repo, ["huge.py"], batch.hashes)
        self.assertEqual(again.sections, [], "変わらなければ再掲しない")
        with open(os.path.join(self.repo, "huge.py"), "a") as f:
            f.write("tail change\n")
        changed = self.entry._collect_diffs(self.repo, ["huge.py"], batch.hashes)
        self.assertEqual(len(changed.sections), 1)

    def test_file_cap_fits_in_total_budget(self):
        """先頭のファイルが必ず収まる (= 永久繰り越しが無い) ための制約。"""
        self.assertLessEqual(self.entry.MAX_FILE_DIFF_BYTES, self.entry.MAX_DIFF_BYTES)


class TestTimeoutBudgets(ReviewSetTestCase):
    """内部 git timeout は hooks.json の hook timeout に収まっていること。

    超えるとハーネスの kill が先に来て、自前の fail-open 経路に到達しない。
    """

    def _hook_timeouts(self) -> dict[str, int]:
        import json as _json
        import pathlib

        hooks = _json.loads(
            (pathlib.Path(__file__).resolve().parents[2] / "hooks.json").read_text()
        )
        found = {}
        for entries in hooks["hooks"].values():
            for entry in entries:
                for h in entry["hooks"]:
                    cmd = h.get("command", "")
                    if "post-implementation-review" not in cmd:
                        continue
                    phase = cmd.rsplit("--phase ", 1)[-1].strip()
                    found[phase] = h["timeout"]
        return found

    def test_pre_and_post_tool_fit_in_hook_budget(self):
        """post-tool は Bash 経路と Edit 系経路で git 呼び出し数が異なる
        (`__main__.py` の `_record_bash_changes` / `handle_post_tool` 参照)。
        どちらも同じ `post-tool` hook timeout を共有するため、大きい方
        (Bash: worktree_root + status_snapshot) で判定する。

        実測 (`gitscan._git` をカウンタでラップして pre-tool / post-tool(Bash) /
        post-tool(Edit) を個別に起動): pre-tool と post-tool/Bash はどちらも
        rev-parse 1 回 + status 1 回、post-tool/Edit,Write,NotebookEdit は
        git 呼び出し 0 回だった。
        """
        import gitscan

        timeouts = self._hook_timeouts()

        pre_tool_worst = gitscan.REV_PARSE_TIMEOUT_SEC + gitscan.STATUS_TIMEOUT_SEC
        self.assertIn("pre-tool", timeouts)
        self.assertLess(
            pre_tool_worst,
            timeouts["pre-tool"],
            f"pre-tool の内部 git timeout 合計 {pre_tool_worst}s が hook timeout に収まっていない",
        )

        # post-tool / Bash: _record_bash_changes が worktree_root (rev-parse) +
        # status_snapshot (status) を呼ぶ。
        post_tool_bash_worst = gitscan.REV_PARSE_TIMEOUT_SEC + gitscan.STATUS_TIMEOUT_SEC
        # post-tool / Edit,Write,NotebookEdit: handle_post_tool は git を一切呼ばない
        # (_edited_paths はパス整形のみ、state.record_pending も git 非依存)。
        post_tool_edit_worst = 0
        post_tool_worst = max(post_tool_bash_worst, post_tool_edit_worst)
        self.assertIn("post-tool", timeouts)
        self.assertLess(
            post_tool_worst,
            timeouts["post-tool"],
            f"post-tool の内部 git timeout 合計 {post_tool_worst}s が hook timeout に収まっていない",
        )

    def test_stop_git_budget_fits_beside_cursor(self):
        import cursor
        import gitscan
        from _common import subproc

        timeouts = self._hook_timeouts()
        git_worst = (
            gitscan.REV_PARSE_TIMEOUT_SEC * 2
            + gitscan.LS_FILES_TIMEOUT_SEC  # symlink 一覧 (symlink_map)
            + gitscan.LS_FILES_TIMEOUT_SEC  # untracked 判定 (untracked_among)
            + self.entry.COLLECT_BUDGET_SEC
            + gitscan.PATH_DIFF_TIMEOUT_SEC  # 予算判定後に走る最後の 1 パス
        )
        # cursor の timeout 後に process group を止める経路 (SIGTERM 待ち / SIGKILL 待ち /
        # 最後の wait) も Stop の hook timeout 内に収める。超えると restore_claim に到達しない。
        # **既定値 (`TIMEOUT_SEC`) ではなく上限 (`MAX_TIMEOUT_SEC`) で見る**: 0.6.0 で
        # `EXTERNAL_AI_POST_REVIEW_TIMEOUT` により env から伸ばせるようになったので、
        # 既定値で見ると「上限まで設定した最悪ケース」が誰にも守られなくなる
        cursor_worst = cursor.MAX_TIMEOUT_SEC + 3 * subproc.KILL_GRACE_SEC
        self.assertLess(
            cursor_worst + git_worst,
            timeouts["stop"],
            "cursor (kill 猶予込み) + git の最悪ケースが Stop の hook timeout を超えている",
        )

    def test_default_timeout_does_not_exceed_ceiling(self):
        import cursor

        self.assertLessEqual(cursor.TIMEOUT_SEC, cursor.MAX_TIMEOUT_SEC)


class TestSectionTruncate(ReviewSetTestCase):
    def test_short_diff_passes_through(self):
        self.assertEqual(self.entry._truncate_section("small", 100), "small")

    def test_long_diff_is_cut_within_limit_with_marker(self):
        text = "\n".join("line %04d" % i for i in range(2000))
        result = self.entry._truncate_section(text, 1000)
        self.assertLessEqual(len(result.encode()), 1000, "marker 込みで上限に収まる")
        self.assertIn("(truncated for review", result)
        self.assertIn(f"{len(text.encode())} bytes in total", result)

    def test_cuts_on_line_boundary(self):
        """byte 境界が行の途中 (905 % 10 = 5) に落ちる limit で、直前の行末まで戻ること。"""
        text = "\n".join("line %04d" % i for i in range(2000))  # 1 行 10 バイト
        marker_len = len(self.entry._TRUNCATED_MARKER.format(full=len(text.encode())).encode())
        result = self.entry._truncate_section(text, marker_len + 905)
        body = result.split("\n... (truncated", 1)[0]
        self.assertEqual(body, "\n".join("line %04d" % i for i in range(90)))
        self.assertLessEqual(len(result.encode()), marker_len + 905)

    def test_single_long_line_is_cut_at_byte_boundary(self):
        result = self.entry._truncate_section("x" * 5000, 1000)
        self.assertLessEqual(len(result.encode()), 1000)
        self.assertIn("x" * 100, result)

    def test_multibyte_boundary_does_not_raise(self):
        result = self.entry._truncate_section("あ" * 5000, 1000)
        self.assertLessEqual(len(result.encode()), 1000)
        self.assertIn("(truncated for review", result)


class TestIsCleanReview(ReviewSetTestCase):
    """判定規則の網羅は hooks/_common/tests/test_sentinel.py。ここは hook 側の配線確認。"""

    def test_bare_sentinel(self):
        self.assertTrue(self.entry.is_clean_review("REVIEW_CLEAN"))
        self.assertTrue(self.entry.is_clean_review("  `REVIEW_CLEAN`  "))
        self.assertTrue(self.entry.is_clean_review(""))

    def test_fenced_sentinel_with_preamble_is_clean(self):
        """zh5.1: 2026-08-20 の実出力相当 (前置き 1 文 + フェンス付き sentinel)。"""
        self.assertTrue(self.entry.is_clean_review("```\nREVIEW_CLEAN\n```"))
        self.assertTrue(
            self.entry.is_clean_review("critical 指摘はない\n\n```\nREVIEW_CLEAN\n```\n")
        )

    def test_sentinel_with_trailing_findings_is_not_clean(self):
        self.assertFalse(
            self.entry.is_clean_review("REVIEW_CLEAN\n\n1. **直接影響** — 実は壊れる")
        )
        self.assertFalse(
            self.entry.is_clean_review("critical 指摘はないが、`retry()` は無限ループする\nREVIEW_CLEAN")
        )

    def test_findings_only(self):
        self.assertFalse(self.entry.is_clean_review("1. **直接影響** — 壊れる"))


class TestEditedPaths(ReviewSetTestCase):
    def test_absolute_file_path(self):
        target = os.path.join(self.repo, "a.py")
        self.assertEqual(
            self.entry._edited_paths({"file_path": target}, self.repo), [target]
        )

    def test_relative_path_is_joined_with_cwd(self):
        self.assertEqual(
            self.entry._edited_paths({"file_path": "a.py"}, self.repo),
            [os.path.join(self.repo, "a.py")],
        )

    def test_notebook_path_fallback(self):
        """NotebookEdit は現環境に非搭載だが、搭載環境の `notebook_path` も拾う。"""
        target = os.path.join(self.repo, "nb.ipynb")
        self.assertEqual(
            self.entry._edited_paths({"notebook_path": target}, self.repo), [target]
        )

    def test_missing_path(self):
        self.assertEqual(self.entry._edited_paths({}, self.repo), [])
        self.assertEqual(self.entry._edited_paths({"file_path": None}, self.repo), [])


if __name__ == "__main__":
    unittest.main()
