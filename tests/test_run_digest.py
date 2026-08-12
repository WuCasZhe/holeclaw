import argparse
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


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
            500,
            {"favorites_complete": 0},
        )
        self.assertFalse(checkpoint["favorites_complete"])


class CacheStoreTests(unittest.TestCase):
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
                            "source_page": 1,
                        },
                        {
                            "pid": "2",
                            "timestamp": 101,
                            "reply": 50,
                            "favorites": 20,
                            "type": "text",
                            "text": "two",
                            "source_page": 1,
                        },
                        {
                            "pid": "3",
                            "timestamp": 102,
                            "reply": 200,
                            "favorites": 30,
                            "type": "text",
                            "text": "three",
                            "source_page": 1,
                        },
                    ]
                )

                favorite_only = cache.query_posts(90, 110, None, 20)
                comments_only = cache.query_posts(90, 110, 100, None)
                combined = cache.query_posts(90, 110, 100, 10)

                self.assertEqual([row["pid"] for row in favorite_only], ["3"])
                self.assertEqual([row["pid"] for row in comments_only], ["3", "1"])
                self.assertEqual([row["pid"] for row in combined], ["3"])
            finally:
                cache.close()

    def test_legacy_cache_migration_does_not_claim_favorite_coverage(self) -> None:
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
                """
            )
            connection.commit()
            connection.close()

            cache = run_digest.CacheStore(path)
            try:
                post_columns = {
                    row[1] for row in cache.connection.execute("PRAGMA table_info(posts)")
                }
                coverage_columns = {
                    row[1] for row in cache.connection.execute("PRAGMA table_info(coverage)")
                }
                self.assertIn("favorites", post_columns)
                self.assertIn("favorites_complete", coverage_columns)
                self.assertIsNotNone(cache.find_covering(110, 190))
                self.assertIsNone(cache.find_covering(110, 190, require_favorites=True))

                cache.add_coverage(100, 200, "2026-08-12T01:00:00+08:00", 1, 1, True)
                self.assertIsNotNone(
                    cache.find_covering(110, 190, require_favorites=True)
                )
            finally:
                cache.close()


class ReportTests(unittest.TestCase):
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


class RunSinkTests(unittest.TestCase):
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
                500,
                None,
            )
            sink = run_digest.RunSink(
                cache, checkpoint, root / "checkpoint.json", None, 10
            )
            try:
                sink.ingest(
                    {
                        "schema_version": 1,
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
                                "source_page": 1,
                            }
                        ],
                        "matches": [
                            {
                                "pid": "1",
                                "timestamp": 150,
                                "reply": 2,
                                "favorites": 11,
                                "type": "text",
                                "text": "match",
                            }
                        ],
                    }
                )
                self.assertIn("1", checkpoint["matched_by_pid"])

                with self.assertRaisesRegex(run_digest.CliError, "favorite counts"):
                    sink.ingest(
                        {
                            "schema_version": 1,
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
                                    "source_page": 2,
                                }
                            ],
                            "matches": [],
                        }
                    )
                self.assertEqual(cache.post_count(), 1)
            finally:
                cache.close()


if __name__ == "__main__":
    unittest.main()
