import argparse
import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "run_digest.py"
SPEC = importlib.util.spec_from_file_location("holeclaw_run_digest", MODULE_PATH)
run_digest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(run_digest)


class ThresholdTests(unittest.TestCase):
    def parse_run(self, *arguments: str) -> argparse.Namespace:
        return run_digest.build_parser().parse_args(["run", *arguments])

    def test_default_comment_threshold_is_kept(self) -> None:
        args = self.parse_run()
        run_digest.resolve_thresholds(args)
        self.assertEqual(args.min_comments, 50)
        self.assertIsNone(args.min_favorites)
        self.assertNotIn("min_favorites", run_digest.window_spec(args))

    def test_favorite_only_does_not_add_comment_threshold(self) -> None:
        args = self.parse_run("--min-favorites", "25")
        run_digest.resolve_thresholds(args)
        self.assertIsNone(args.min_comments)
        self.assertEqual(args.min_favorites, 25)
        self.assertEqual(run_digest.window_spec(args)["min_favorites"], 25)

    def test_combined_thresholds_are_preserved(self) -> None:
        args = self.parse_run("--min-comments", "100", "--min-favorites", "50")
        run_digest.resolve_thresholds(args)
        self.assertEqual(args.min_comments, 100)
        self.assertEqual(args.min_favorites, 50)

    def test_any_match_mode_requires_both_thresholds(self) -> None:
        args = self.parse_run("--min-comments", "100", "--match-mode", "any")
        with self.assertRaisesRegex(run_digest.CliError, "requires both"):
            run_digest.resolve_thresholds(args)

    def test_any_match_mode_is_part_of_checkpoint_spec(self) -> None:
        args = self.parse_run(
            "--min-comments", "100", "--min-favorites", "45", "--match-mode", "any"
        )
        run_digest.resolve_thresholds(args)
        self.assertEqual(run_digest.window_spec(args)["match_mode"], "any")

    def test_cache_chunks_default_to_one_page(self) -> None:
        self.assertEqual(self.parse_run().cache_chunk_pages, 1)

    def test_request_concurrency_defaults_to_two_and_is_configurable(self) -> None:
        self.assertEqual(self.parse_run().concurrency, 2)
        self.assertEqual(self.parse_run("--concurrency", "4").concurrency, 4)

    def test_request_concurrency_rejects_values_above_safety_cap(self) -> None:
        args = self.parse_run("--concurrency", "5")
        with self.assertRaisesRegex(run_digest.CliError, "--concurrency"):
            run_digest.run_digest(args)

    def test_max_telemetry_fields_are_not_summed(self) -> None:
        telemetry = run_digest.empty_telemetry()
        run_digest.merge_telemetry(telemetry, {"max_in_flight": 2, "wall_ms": 10})
        run_digest.merge_telemetry(telemetry, {"max_in_flight": 1, "wall_ms": 20})
        self.assertEqual(telemetry["max_in_flight"], 2)
        self.assertEqual(telemetry["wall_ms"], 30)

    def test_completed_checkpoint_does_not_freeze_rolling_window(self) -> None:
        rolling = self.parse_run("--days", "7", "--min-comments", "100")
        fixed = self.parse_run(
            "--days", "7", "--until", "2026-08-18", "--min-comments", "100"
        )
        completed = {"completed": True}
        unfinished = {"completed": False}

        self.assertTrue(run_digest.is_rolling_window(rolling))
        self.assertFalse(run_digest.should_reuse_checkpoint(rolling, completed))
        self.assertTrue(run_digest.should_reuse_checkpoint(rolling, unfinished))
        self.assertFalse(run_digest.is_rolling_window(fixed))
        self.assertTrue(run_digest.should_reuse_checkpoint(fixed, completed))

    def test_negative_favorite_threshold_is_rejected(self) -> None:
        args = self.parse_run("--min-favorites", "-1")
        with self.assertRaisesRegex(run_digest.CliError, "--min-favorites"):
            run_digest.resolve_thresholds(args)

    def test_checkpoint_inherits_favorite_completeness_from_cache_prefix(self) -> None:
        checkpoint = run_digest.new_checkpoint(
            {"min_comments": 50},
            100,
            200,
            150,
            "test",
            cache_reused=True,
            favorites_complete=False,
        )
        self.assertFalse(checkpoint["favorites_complete"])
        self.assertTrue(checkpoint["cache_reused"])
        self.assertEqual(checkpoint["schema_version"], 4)

    def test_legacy_checkpoint_is_rejected_without_modification(self) -> None:
        args = self.parse_run("--min-comments", "50")
        run_digest.resolve_thresholds(args)
        spec = run_digest.window_spec(args)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            checkpoint = run_digest.new_checkpoint(spec, 100, 200, 100, "test")
            checkpoint["schema_version"] = 3
            run_digest.write_checkpoint(path, checkpoint)
            before = path.read_bytes()
            with self.assertRaisesRegex(run_digest.CliError, "incompatible"):
                run_digest.load_checkpoint(path, spec)
            self.assertEqual(path.read_bytes(), before)

    def test_default_runtime_paths_are_versioned(self) -> None:
        self.assertEqual(
            run_digest.default_cache_path().name, "holeclaw-cache-v5.sqlite3"
        )
        checkpoint = run_digest.default_checkpoint_path({"min_comments": 50})
        self.assertEqual(checkpoint.parent.name, "holeclaw-checkpoints-v4")


class StandaloneTests(unittest.TestCase):
    def parse_standalone(self, *arguments: str) -> argparse.Namespace:
        return run_digest.build_parser().parse_args(["standalone", *arguments])

    def test_standalone_parser_supports_digest_and_scheduler_flags(self) -> None:
        args = self.parse_standalone(
            "--days", "7", "--min-favorites", "25", "--non-interactive"
        )
        self.assertEqual(args.command, "standalone")
        self.assertEqual(args.days, 7)
        self.assertEqual(args.min_favorites, 25)
        self.assertTrue(args.non_interactive)

    def test_find_pwcli_prefers_bundled_wrapper(self) -> None:
        with patch.dict(run_digest.os.environ, {"PWCLI": ""}):
            self.assertEqual(
                run_digest.find_pwcli(),
                MODULE_PATH.with_name("playwright_cli.sh"),
            )

    def test_non_interactive_browser_session_opens_headless(self) -> None:
        with patch.object(
            run_digest, "find_pwcli", return_value=Path("/tmp/playwright-cli")
        ):
            browser = run_digest.BrowserCli("scheduler", headed=False)
        browser.run = MagicMock()
        with patch.object(
            run_digest.subprocess,
            "run",
            return_value=argparse.Namespace(stdout="### Browsers\n", returncode=0),
        ):
            browser.ensure_session()
        browser.run.assert_called_once_with("open", "about:blank")

    def test_non_interactive_mode_rejects_missing_login_state(self) -> None:
        events = []

        class FakeBrowser:
            def __init__(self, session: str, headed: bool = True):
                events.append(("init", session, headed))

            def ensure_session(self) -> None:
                events.append(("ensure",))

            def run(self, *arguments: str, **_kwargs):
                events.append(arguments)
                return argparse.Namespace(stdout="")

        with tempfile.TemporaryDirectory() as directory:
            args = self.parse_standalone("--non-interactive")
            args.state = Path(directory) / "missing.json"
            with patch.object(run_digest, "BrowserCli", FakeBrowser):
                with self.assertRaisesRegex(run_digest.CliError, "--non-interactive"):
                    run_digest.ensure_standalone_login(args)

        self.assertEqual(events, [])

    def test_valid_saved_state_continues_without_prompt(self) -> None:
        class FakeBrowser:
            def __init__(self, _session: str, headed: bool = True):
                self.headed = headed

            def ensure_session(self) -> None:
                pass

            def run(self, *arguments: str, **_kwargs):
                if arguments == ("snapshot",):
                    return argparse.Namespace(
                        stdout=(
                            "Page Title: 北大树洞\n"
                            "https://treehole.pku.edu.cn/ch/web/pc/index"
                        )
                    )
                return argparse.Namespace(stdout="")

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.touch()
            args = self.parse_standalone("--non-interactive")
            args.state = state
            with patch.object(run_digest, "BrowserCli", FakeBrowser):
                with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                    browser = run_digest.ensure_standalone_login(args)
        self.assertIsInstance(browser, FakeBrowser)

    def test_standalone_defers_login_until_network_collection(self) -> None:
        args = self.parse_standalone("--days", "7")
        with patch.object(run_digest, "run_digest") as digest:
            run_digest.run_standalone(args)
        digest.assert_called_once_with(args, standalone=True)


class WorkflowTests(unittest.TestCase):
    def test_fresh_does_not_overwrite_explicit_legacy_checkpoint(self) -> None:
        args = run_digest.build_parser().parse_args([
            "run", "--days", "1", "--min-comments", "100", "--fresh"
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args.cache = root / "cache-v5.sqlite3"
            args.checkpoint = root / "legacy-checkpoint.json"
            args.output = root / "report.md"
            args.checkpoint.write_text(
                '{"schema_version":3,"request":{}}\n', encoding="utf-8"
            )
            before = args.checkpoint.read_bytes()

            with self.assertRaisesRegex(run_digest.CliError, "incompatible"):
                run_digest.run_digest(args)

            self.assertEqual(args.checkpoint.read_bytes(), before)
            self.assertFalse(args.cache.exists())

    def test_new_default_paths_leave_legacy_runtime_files_untouched(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_root = root / "output/playwright"
            legacy_root.mkdir(parents=True)
            legacy_cache = legacy_root / "holeclaw-cache.sqlite3"
            legacy_cache.write_bytes(b"legacy-cache")
            legacy_checkpoint = legacy_root / "holeclaw-checkpoints/legacy.json"
            legacy_checkpoint.parent.mkdir()
            legacy_checkpoint.write_bytes(b"legacy-checkpoint")
            try:
                run_digest.os.chdir(root)
                new_cache = run_digest.default_cache_path()
                new_checkpoint = run_digest.default_checkpoint_path({"min_comments": 50})
                cache = run_digest.CacheStore(new_cache)
                cache.close()
            finally:
                run_digest.os.chdir(original_cwd)

            self.assertEqual(legacy_cache.read_bytes(), b"legacy-cache")
            self.assertEqual(legacy_checkpoint.read_bytes(), b"legacy-checkpoint")
            self.assertTrue(new_cache.is_file())
            self.assertFalse(new_checkpoint.exists())

    def test_standalone_cache_hit_does_not_initialize_browser(self) -> None:
        day = run_digest.datetime.now(run_digest.SHANGHAI).date() - timedelta(days=1)
        args = run_digest.build_parser().parse_args([
            "standalone",
            "--since", day.isoformat(),
            "--until", day.isoformat(),
            "--min-comments", "100",
        ])
        run_digest.resolve_thresholds(args)
        start_ts, end_ts, _label = run_digest.time_window(args)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args.cache = root / "cache.sqlite3"
            args.checkpoint = root / "checkpoint.json"
            args.output = root / "report.md"
            cache = run_digest.CacheStore(args.cache)
            cache.add_coverage(
                start_ts,
                end_ts,
                run_digest.datetime.now(run_digest.SHANGHAI).isoformat(),
                1,
                0,
                True,
            )
            cache.close()

            with patch.object(
                run_digest,
                "ensure_standalone_login",
                side_effect=AssertionError("cache hit must not initialize a browser"),
            ):
                run_digest.run_standalone(args)

            self.assertTrue(args.output.is_file())

    def test_completed_rolling_checkpoint_advances_to_incremental_scan(self) -> None:
        args = run_digest.build_parser().parse_args([
            "standalone", "--days", "7", "--min-comments", "100"
        ])
        run_digest.resolve_thresholds(args)
        spec = run_digest.window_spec(args)
        current_start, current_end, label = run_digest.time_window(args)
        old_start = current_start - 3600
        old_end = current_end - 3600

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args.cache = root / "cache.sqlite3"
            args.checkpoint = root / "checkpoint.json"
            args.output = root / "report.md"
            cache = run_digest.CacheStore(args.cache)
            cache.add_coverage(
                old_start,
                old_end,
                run_digest.datetime.now(run_digest.SHANGHAI).isoformat(),
                10,
                5000,
                True,
            )
            checkpoint = run_digest.new_checkpoint(
                spec, old_start, old_end, old_start, label
            )
            checkpoint.update({
                "completed": True,
                "completed_at": run_digest.datetime.now(run_digest.SHANGHAI).isoformat(),
                "cache_path": str(args.cache.resolve()),
                "cache_instance_id": cache.instance_id,
            })
            run_digest.write_checkpoint(args.checkpoint, checkpoint)
            cache.close()

            with patch.object(
                run_digest,
                "ensure_standalone_login",
                side_effect=RuntimeError("network collection reached"),
            ) as login:
                with self.assertRaisesRegex(RuntimeError, "network collection reached"):
                    run_digest.run_standalone(args)
            login.assert_called_once_with(args)

            advanced = run_digest.load_checkpoint(args.checkpoint, spec)
            self.assertFalse(advanced["completed"])
            self.assertGreater(advanced["end_timestamp"], old_end)
            self.assertEqual(advanced["scan_start_timestamp"], old_end)


class CacheStoreTests(unittest.TestCase):
    def test_cache_uses_one_window_index_for_report_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = run_digest.CacheStore(Path(directory) / "cache.sqlite3")
            try:
                indexes = {
                    row[0]
                    for row in cache.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertIn("posts_window_idx", indexes)
                self.assertNotIn("posts_timestamp_idx", indexes)
                self.assertNotIn("posts_reply_idx", indexes)
                self.assertNotIn("posts_favorites_idx", indexes)
                post_columns = {
                    row[1] for row in cache.connection.execute("PRAGMA table_info(posts)")
                }
                self.assertNotIn("source_page", post_columns)
                plan = " ".join(
                    str(row[3])
                    for row in cache.connection.execute(
                        """
                        EXPLAIN QUERY PLAN
                        SELECT pid, timestamp, reply, favorites, type, text
                        FROM posts
                        WHERE timestamp >= ? AND timestamp < ? AND reply > ?
                        ORDER BY timestamp DESC, pid DESC
                        """,
                        (100, 200, 50),
                    )
                )
                self.assertIn("posts_window_idx", plan)
            finally:
                cache.close()
    def test_strict_independent_and_combined_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = run_digest.CacheStore(Path(directory) / "cache.sqlite3")
            try:
                cache.upsert_posts(
                    [
                        {
                            "pid": "1",
                            "timestamp": 100,
                            "reply": 101,
                            "favorites": 10,
                            "type": "text",
                            "text": "one",
                        },
                        {
                            "pid": "2",
                            "timestamp": 101,
                            "reply": 50,
                            "favorites": 20,
                            "type": "text",
                            "text": "two",
                        },
                        {
                            "pid": "3",
                            "timestamp": 102,
                            "reply": 200,
                            "favorites": 30,
                            "type": "text",
                            "text": "three",
                        },
                    ]
                )

                favorite_only = cache.query_posts(90, 110, None, 20)
                comments_only = cache.query_posts(90, 110, 100, None)
                combined = cache.query_posts(90, 110, 100, 10)
                either = cache.query_posts(90, 110, 100, 20, "any")

                self.assertEqual([row["pid"] for row in favorite_only], ["3"])
                self.assertEqual([row["pid"] for row in comments_only], ["3", "1"])
                self.assertEqual([row["pid"] for row in combined], ["3"])
                self.assertEqual([row["pid"] for row in either], ["3", "1"])
            finally:
                cache.close()

    def test_legacy_cache_is_rejected_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE posts (
                    pid TEXT PRIMARY KEY,
                    timestamp INTEGER NOT NULL,
                    reply INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    source_page INTEGER NOT NULL
                );
                CREATE TABLE coverage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_timestamp INTEGER NOT NULL,
                    end_timestamp INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    source_pages INTEGER NOT NULL,
                    source_scanned INTEGER NOT NULL
                );
                INSERT INTO coverage(
                    start_timestamp, end_timestamp, completed_at, source_pages, source_scanned
                ) VALUES(100, 200, '2026-08-12T00:00:00+08:00', 1, 1);
                INSERT INTO metadata(key, value) VALUES('schema_version', '4');
                """
            )
            connection.commit()
            connection.close()
            before = path.read_bytes()

            with self.assertRaisesRegex(run_digest.CliError, "schema v4"):
                run_digest.CacheStore(path)
            self.assertEqual(path.read_bytes(), before)

    def test_favorite_coverage_is_only_completed_at_run_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = run_digest.CacheStore(Path(directory) / "cache.sqlite3")
            try:
                cache.upsert_posts([
                    {
                        "pid": "known",
                        "timestamp": 150,
                        "reply": 1,
                        "favorites": 5,
                        "type": "text",
                        "text": "known",
                    },
                    {
                        "pid": "missing",
                        "timestamp": 160,
                        "reply": 2,
                        "favorites": None,
                        "type": "image",
                        "text": "",
                    },
                ])
                cache.add_coverage(100, 200, "2026-08-12T01:00:00+08:00", 1, 2, False)
                self.assertIsNone(cache.find_covering(110, 190, require_favorites=True))

                cache.record_favorite_unavailable([
                    {"pid": "missing", "reason": "detail_missing"}
                ])
                self.assertIsNone(
                    cache.find_covering(110, 190, require_favorites=True)
                )
                cache.add_coverage(
                    100, 200, "2026-08-12T02:00:00+08:00", 1, 2, True
                )
                self.assertIsNotNone(
                    cache.find_covering(110, 190, require_favorites=True)
                )
            finally:
                cache.close()

    def test_known_favorite_upsert_clears_unavailable_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = run_digest.CacheStore(Path(directory) / "cache.sqlite3")
            try:
                cache.upsert_posts([{
                    "pid": "1",
                    "timestamp": 150,
                    "reply": 1,
                    "favorites": None,
                    "type": "text",
                    "text": "post",
                }])
                cache.record_favorite_unavailable([{
                    "pid": "1", "reason": "detail_missing"
                }])
                self.assertEqual(len(cache.query_favorite_unavailable(100, 200)), 1)

                cache.upsert_posts([{
                    "pid": "1",
                    "timestamp": 150,
                    "reply": 1,
                    "favorites": 5,
                    "type": "text",
                    "text": "post",
                }])
                self.assertEqual(cache.query_favorite_unavailable(100, 200), [])
            finally:
                cache.close()


class ReportTests(unittest.TestCase):
    def test_report_matches_golden_markdown(self) -> None:
        timestamp = int(
            run_digest.datetime(
                2026, 8, 12, 10, 30, tzinfo=run_digest.SHANGHAI
            ).timestamp()
        )
        data = {
            "collected_at": "2026-08-12T12:00:00+08:00",
            "start_timestamp": int(
                run_digest.datetime(
                    2026, 8, 12, tzinfo=run_digest.SHANGHAI
                ).timestamp()
            ),
            "end_timestamp": int(
                run_digest.datetime(
                    2026, 8, 13, tzinfo=run_digest.SHANGHAI
                ).timestamp()
            ),
            "min_comments": 4,
            "min_favorites": None,
            "match_mode": "all",
            "pages": 1,
            "scanned": 1,
            "candidate_count": 1,
            "candidates": [{
                "pid": "123",
                "timestamp": timestamp,
                "reply": 5,
                "favorites": None,
                "type": "text",
                "text": "测试帖子",
            }],
            "cache_reused": False,
            "favorite_unavailable": [],
        }
        expected = """# 北大树洞高评论帖报告（固定窗口）

- 生成时间：2026-08-12 12:00:00（Asia/Shanghai）
- 时间范围：2026-08-12 00:00:00 至 2026-08-13 00:00:00
- 筛选条件：评论数 > 4
- 本次网络扫描：1 页，1 条帖子
- 命中：1 条

> 评论数为最近一次采集快照。图片帖默认仅摘要文字说明，不对图片做 OCR。

## 2026-08-12

- **#123** · 5 条评论 · 10:30 — 测试帖子
"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"
            run_digest.render_report(data, output, "固定窗口")
            self.assertEqual(output.read_text(encoding="utf-8"), expected)

    def test_favorite_only_report_labels_and_metrics(self) -> None:
        data = {
            "collected_at": "2026-08-12T12:00:00+08:00",
            "start_timestamp": 1_786_422_400,
            "end_timestamp": 1_786_508_800,
            "min_comments": None,
            "min_favorites": 10,
            "pages": 2,
            "scanned": 20,
            "candidate_count": 1,
            "candidates": [
                {
                    "pid": "123",
                    "timestamp": 1_786_465_200,
                    "reply": 5,
                    "favorites": 11,
                    "type": "text",
                    "text": "测试帖子",
                }
            ],
            "cache_reused": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"
            run_digest.render_report(data, output, "近1天")
            rendered = output.read_text(encoding="utf-8")

        self.assertIn("# 北大树洞高收藏帖报告（近1天）", rendered)
        self.assertIn("筛选条件：收藏数 > 10", rendered)
        self.assertIn("5 条评论 · 11 次收藏", rendered)
        self.assertIn("评论数和收藏数为最近一次采集快照", rendered)

    def test_any_report_labels_unavailable_favorites(self) -> None:
        data = {
            "collected_at": "2026-08-12T12:00:00+08:00",
            "start_timestamp": 1_786_422_400,
            "end_timestamp": 1_786_508_800,
            "min_comments": 100,
            "min_favorites": 45,
            "match_mode": "any",
            "pages": 1,
            "scanned": 1,
            "candidate_count": 1,
            "candidates": [{
                "pid": "123",
                "timestamp": 1_786_465_200,
                "reply": 101,
                "favorites": None,
                "type": "text",
                "text": "测试帖子",
            }],
            "cache_reused": True,
            "favorite_unavailable": [{"pid": "123", "reason": "detail_missing"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.md"
            run_digest.render_report(data, output, "近1天")
            rendered = output.read_text(encoding="utf-8")

        self.assertIn("高评论或高收藏帖报告", rendered)
        self.assertIn("评论数 > 100 或 收藏数 > 45", rendered)
        self.assertIn("收藏数不可用：1 条（#123）", rendered)


class RunSinkTests(unittest.TestCase):
    def test_cache_chunk_rolls_back_when_second_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = run_digest.CacheStore(root / "cache.sqlite3")
            checkpoint = run_digest.new_checkpoint(
                {"min_comments": 10}, 100, 200, 100, "test"
            )
            sink = run_digest.RunSink(
                cache, checkpoint, root / "checkpoint.json", 10, None
            )
            payload = {
                "schema_version": run_digest.SINK_SCHEMA_VERSION,
                "start_page": 1,
                "end_page": 1,
                "pages": 1,
                "scanned": 1,
                "rows": [{
                    "pid": "rollback",
                    "timestamp": 150,
                    "reply": 1,
                    "favorites": None,
                    "type": "text",
                    "text": "must roll back",
                }],
                "matched_pids": [],
                "favorite_unavailable": [
                    {"pid": "rollback", "reason": "detail_missing"}
                ],
            }
            try:
                with patch.object(
                    cache,
                    "upsert_posts",
                    side_effect=RuntimeError("simulated second write failure"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "second write failure"):
                        sink.ingest(payload)
                self.assertEqual(cache.post_count(), 0)
                self.assertEqual(
                    cache.connection.execute(
                        "SELECT COUNT(*) FROM favorite_unavailable"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(checkpoint["next_page"], 1)
            finally:
                cache.close()

    def test_invalid_match_is_rejected_before_cache_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = run_digest.CacheStore(root / "cache.sqlite3")
            checkpoint = run_digest.new_checkpoint(
                {"min_comments": 0}, 100, 200, 100, "test"
            )
            sink = run_digest.RunSink(
                cache, checkpoint, root / "checkpoint.json", 0, None
            )
            try:
                with self.assertRaisesRegex(run_digest.CliError, "outside the requested"):
                    sink.ingest({
                        "schema_version": run_digest.SINK_SCHEMA_VERSION,
                        "start_page": 1,
                        "end_page": 1,
                        "pages": 1,
                        "scanned": 1,
                        "rows": [{
                            "pid": "1",
                            "timestamp": 250,
                            "reply": 1,
                            "favorites": 0,
                            "type": "text",
                            "text": "valid cache row",
                        }],
                        "matched_pids": ["1"],
                    })
                self.assertEqual(cache.post_count(), 0)
                self.assertEqual(checkpoint["next_page"], 1)
            finally:
                cache.close()

    def test_matched_pid_must_exist_in_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = run_digest.CacheStore(root / "cache.sqlite3")
            checkpoint = run_digest.new_checkpoint(
                {"min_comments": 0}, 100, 200, 100, "test"
            )
            sink = run_digest.RunSink(
                cache, checkpoint, root / "checkpoint.json", 0, None
            )
            try:
                with self.assertRaisesRegex(run_digest.CliError, "outside its cache rows"):
                    sink.ingest({
                        "schema_version": run_digest.SINK_SCHEMA_VERSION,
                        "start_page": 1,
                        "end_page": 1,
                        "pages": 1,
                        "scanned": 1,
                        "rows": [{
                            "pid": "1",
                            "timestamp": 150,
                            "reply": 1,
                            "favorites": 0,
                            "type": "text",
                            "text": "row",
                        }],
                        "matched_pids": ["missing"],
                    })
                self.assertEqual(cache.post_count(), 0)
                self.assertEqual(checkpoint["next_page"], 1)
            finally:
                cache.close()

    def test_matched_pids_must_use_v2_list_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = run_digest.CacheStore(root / "cache.sqlite3")
            checkpoint = run_digest.new_checkpoint(
                {"min_comments": 0}, 100, 200, 100, "test"
            )
            sink = run_digest.RunSink(
                cache, checkpoint, root / "checkpoint.json", 0, None
            )
            try:
                with self.assertRaisesRegex(run_digest.CliError, "invalid matched PIDs"):
                    sink.ingest({
                        "schema_version": run_digest.SINK_SCHEMA_VERSION,
                        "start_page": 1,
                        "end_page": 1,
                        "pages": 1,
                        "scanned": 1,
                        "rows": [{
                            "pid": "1",
                            "timestamp": 150,
                            "reply": 1,
                            "favorites": 0,
                            "type": "text",
                            "text": "row",
                        }],
                        "matched_pids": "1",
                    })
                self.assertEqual(cache.post_count(), 0)
            finally:
                cache.close()

    def test_ingest_accumulates_collector_and_cache_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = run_digest.CacheStore(root / "cache.sqlite3")
            checkpoint = run_digest.new_checkpoint(
                {"min_comments": 0}, 100, 200, 100, "test"
            )
            sink = run_digest.RunSink(
                cache, checkpoint, root / "checkpoint.json", 0, None
            )
            try:
                sink.ingest({
                    "schema_version": run_digest.SINK_SCHEMA_VERSION,
                    "start_page": 1,
                    "end_page": 1,
                    "pages": 1,
                    "scanned": 1,
                    "rows": [{
                        "pid": "1",
                        "timestamp": 150,
                        "reply": 1,
                        "favorites": 0,
                        "type": "text",
                        "text": "telemetry",
                    }],
                    "matched_pids": ["1"],
                    "telemetry": {
                        "list_requests": 1,
                        "request_ms": 250,
                        "pacing_ms": 600,
                        "response_chars": 1234,
                    },
                })
                self.assertEqual(checkpoint["telemetry"]["list_requests"], 1)
                self.assertEqual(checkpoint["telemetry"]["request_ms"], 250)
                self.assertEqual(checkpoint["telemetry"]["pacing_ms"], 600)
                self.assertEqual(checkpoint["telemetry"]["response_chars"], 1234)
                self.assertGreaterEqual(checkpoint["telemetry"]["cache_write_ms"], 0)
                self.assertEqual(checkpoint["matched_by_pid"], {"1": True})
            finally:
                cache.close()

    def test_favorite_filter_accepts_matching_rows_and_rejects_missing_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = run_digest.CacheStore(root / "cache.sqlite3")
            checkpoint = run_digest.new_checkpoint(
                {"min_comments": None, "min_favorites": 10},
                100,
                200,
                100,
                "test",
            )
            sink = run_digest.RunSink(
                cache, checkpoint, root / "checkpoint.json", None, 10
            )
            try:
                sink.ingest(
                    {
                        "schema_version": run_digest.SINK_SCHEMA_VERSION,
                        "start_page": 1,
                        "end_page": 1,
                        "pages": 1,
                        "scanned": 1,
                        "rows": [
                            {
                                "pid": "1",
                                "timestamp": 150,
                                "reply": 2,
                                "favorites": 11,
                                "type": "text",
                                "text": "match",
                            }
                        ],
                        "matched_pids": ["1"],
                    }
                )
                self.assertIn("1", checkpoint["matched_by_pid"])

                with self.assertRaisesRegex(run_digest.CliError, "favorite counts"):
                    sink.ingest(
                        {
                            "schema_version": run_digest.SINK_SCHEMA_VERSION,
                            "start_page": 2,
                            "end_page": 2,
                            "pages": 1,
                            "scanned": 1,
                            "rows": [
                                {
                                    "pid": "2",
                                    "timestamp": 140,
                                    "reply": 3,
                                    "favorites": None,
                                    "type": "text",
                                    "text": "missing",
                                }
                            ],
                            "matched_pids": [],
                        }
                    )
                self.assertEqual(cache.post_count(), 1)
            finally:
                cache.close()

    def test_any_filter_accepts_explicitly_unavailable_favorite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = run_digest.CacheStore(root / "cache.sqlite3")
            checkpoint = run_digest.new_checkpoint(
                {"min_comments": 100, "min_favorites": 45, "match_mode": "any"},
                100,
                200,
                100,
                "test",
            )
            sink = run_digest.RunSink(
                cache, checkpoint, root / "checkpoint.json", 100, 45, "any"
            )
            try:
                sink.ingest({
                    "schema_version": run_digest.SINK_SCHEMA_VERSION,
                    "start_page": 1,
                    "end_page": 1,
                    "pages": 1,
                    "scanned": 1,
                    "rows": [{
                        "pid": "123",
                        "timestamp": 150,
                        "reply": 101,
                        "favorites": None,
                        "type": "image",
                        "text": "",
                    }],
                    "matched_pids": ["123"],
                    "favorite_unavailable": [
                        {"pid": "123", "reason": "detail_missing"}
                    ],
                })
                self.assertTrue(checkpoint["favorites_complete"])
                self.assertIn("123", checkpoint["matched_by_pid"])
                self.assertEqual(
                    [row["pid"] for row in cache.query_favorite_unavailable(100, 200)],
                    ["123"],
                )
                self.assertEqual(
                    [row["pid"] for row in cache.query_posts(100, 200, 100, 45, "any")],
                    ["123"],
                )
            finally:
                cache.close()


if __name__ == "__main__":
    unittest.main()
