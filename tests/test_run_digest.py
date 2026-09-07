import argparse
import json
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

    def test_cache_and_progress_default_to_one_page(self) -> None:
        for command in ('run', 'standalone', 'archive'):
            arguments = [command, '-a', 'test'] if command == 'archive' else [command]
            args = run_digest.build_parser().parse_args(arguments)
            self.assertEqual(args.cache_chunk_pages, 1)
            self.assertEqual(args.progress_pages, 1)

    def test_short_options_preserve_long_option_values(self):
        parser = run_digest.build_parser()
        short = parser.parse_args(['-s', 'login.json', '-l', 'session', 'archive',
            '-a', 'my-account', '-d', '300', '-f', '15', '-p', '5', '-B', '5',
            '-P', '10', '-t', '120', '-C', 'archive.db', '-S', 'source.db',
            '-k', 'cp.json', '-o', 'out.json', '-j', '2', '-K', '50', '-n', '2000', '-y'])
        long = parser.parse_args(['--state', 'login.json', '--session', 'session', 'archive',
            '--account', 'my-account', '--days', '300', '--min-favorites', '15',
            '--progress-pages', '5', '--cache-chunk-pages', '5', '--comment-batch-pages', '10',
            '--progress-seconds', '120', '--cache', 'archive.db', '--source-cache', 'source.db',
            '--checkpoint', 'cp.json', '--output', 'out.json', '--concurrency', '2',
            '--checkpoint-pages', '50', '--max-total-pages', '2000', '--non-interactive'])
        self.assertEqual(vars(short), vars(long))

    def test_progress_validation(self):
        for option, value in [('-p', '0'), ('-p', '-1'), ('-t', '-1'), ('-t', '9')]:
            with self.subTest(option=option, value=value), self.assertRaises(run_digest.CliError):
                run_digest.validate_progress_arguments(self.parse_run(option, value))
        run_digest.validate_progress_arguments(self.parse_run('-t', '0'))
        run_digest.validate_progress_arguments(self.parse_run('-t', '10'))

    def test_request_concurrency_defaults_to_eight_and_is_configurable(self) -> None:
        for command in ('run', 'standalone', 'archive'):
            arguments = [command, '-a', 'test'] if command == 'archive' else [command]
            self.assertEqual(run_digest.build_parser().parse_args(arguments).concurrency, 8)
        self.assertEqual(self.parse_run("--concurrency", "1").concurrency, 1)
        self.assertEqual(self.parse_run('-j', '8').concurrency, 8)

    def test_checkpoint_interval_defaults_to_one_hundred_pages(self) -> None:
        self.assertEqual(self.parse_run().checkpoint_pages, 100)

    def test_total_page_limit_is_unbounded_by_default(self) -> None:
        self.assertIsNone(self.parse_run().max_total_pages)

    def test_total_page_limit_remains_available_as_an_opt_in_safety_cap(self) -> None:
        self.assertEqual(
            self.parse_run("--max-total-pages", "6000").max_total_pages,
            6000,
        )

    def test_unbounded_resume_has_no_remaining_page_cap(self) -> None:
        self.assertIsNone(run_digest.remaining_page_limit(None, 2000))

    def test_explicit_page_limit_is_total_across_resumes(self) -> None:
        self.assertEqual(run_digest.remaining_page_limit(6000, 2000), 4000)
        with self.assertRaisesRegex(run_digest.CliError, "max-total-pages=2000"):
            run_digest.remaining_page_limit(2000, 2000)

    def test_request_concurrency_rejects_values_above_safety_cap(self) -> None:
        args = self.parse_run("--concurrency", "9")
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

    def test_open_ended_since_and_future_until_do_not_reuse_completed_checkpoint(self):
        today = run_digest.datetime.now(run_digest.SHANGHAI).date()
        for options in (
            ["--since", "2026-01-01"],
            ["--since", "2026-01-01", "--until", today.isoformat()],
            ["--days", "7", "--until", (today + run_digest.timedelta(days=2)).isoformat()],
        ):
            with self.subTest(options=options):
                args = self.parse_run(*options)
                self.assertFalse(run_digest.should_reuse_checkpoint(args, {"completed": True}))
                self.assertTrue(run_digest.should_reuse_checkpoint(args, {"completed": False}))

    def test_completed_checkpoint_clipped_before_until_must_extend_after_deadline(self):
        args = self.parse_run("--since", "2026-01-01", "--until", "2026-01-02")
        end = int(run_digest.datetime(2026, 1, 3, tzinfo=run_digest.SHANGHAI).timestamp())
        self.assertFalse(run_digest.should_reuse_checkpoint(
            args, {"completed": True, "end_timestamp": end - 3600}
        ))
        self.assertTrue(run_digest.should_reuse_checkpoint(
            args, {"completed": True, "end_timestamp": end}
        ))

    def test_future_until_keeps_the_requested_rolling_duration(self) -> None:
        tomorrow = run_digest.datetime.now(run_digest.SHANGHAI).date() + timedelta(days=1)
        args = self.parse_run("--days", "30", "--until", tomorrow.isoformat())

        start, end, label = run_digest.time_window(args)

        self.assertEqual(end - start, 30 * 24 * 60 * 60)
        self.assertEqual(label, "近30天")

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
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                run_digest.os.environ,
                {"HOLECLAW_RUNTIME_DIR": directory},
            ):
                runtime_root = Path(directory)
                self.assertEqual(run_digest.default_runtime_root(), runtime_root)
                self.assertEqual(
                    run_digest.default_cache_path(),
                    runtime_root / "holeclaw-cache-v5.sqlite3",
                )
                checkpoint = run_digest.default_checkpoint_path({"min_comments": 50})
                self.assertEqual(checkpoint.parent.name, "holeclaw-checkpoints-v4")
                self.assertEqual(checkpoint.parent.parent, runtime_root)

    def test_default_runtime_paths_do_not_depend_on_working_directory(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as runtime_directory:
            with tempfile.TemporaryDirectory() as first_directory:
                with tempfile.TemporaryDirectory() as second_directory:
                    with patch.dict(
                        run_digest.os.environ,
                        {"HOLECLAW_RUNTIME_DIR": runtime_directory},
                    ):
                        try:
                            run_digest.os.chdir(first_directory)
                            first_cache = run_digest.default_cache_path()
                            run_digest.os.chdir(second_directory)
                            second_cache = run_digest.default_cache_path()
                        finally:
                            run_digest.os.chdir(original_cwd)
        self.assertEqual(first_cache, second_cache)


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
            wrapper_name = (
                "playwright_cli.cmd"
                if run_digest.running_on_windows()
                else "playwright_cli.sh"
            )
            self.assertEqual(
                run_digest.find_pwcli(),
                MODULE_PATH.with_name(wrapper_name),
            )

    def test_find_pwcli_uses_native_windows_wrapper(self) -> None:
        with (
            patch.dict(run_digest.os.environ, {"PWCLI": ""}),
            patch.object(run_digest, "running_on_windows", return_value=True),
            patch.object(run_digest.shutil, "which", return_value="npx.cmd"),
        ):
            self.assertEqual(
                run_digest.find_pwcli(),
                MODULE_PATH.with_name("playwright_cli.cmd"),
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

    def test_browser_cli_decodes_playwright_output_as_utf8(self) -> None:
        completed = argparse.Namespace(stdout="北大树洞", stderr="", returncode=0)
        with patch.object(
            run_digest, "find_pwcli", return_value=Path("/tmp/playwright-cli")
        ):
            browser = run_digest.BrowserCli("utf8-session")
        with patch.object(
            run_digest.subprocess, "run", return_value=completed
        ) as execute:
            result = browser.run("snapshot")
        self.assertEqual(result.stdout, "北大树洞")
        self.assertEqual(execute.call_args.kwargs["encoding"], "utf-8")

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


class RuntimeRoutingTests(unittest.TestCase):
    def test_windows_short_paths_support_separate_equals_and_attached_values(self):
        with patch.object(run_digest, 'windows_native_path', side_effect=lambda value: 'WIN:' + value):
            self.assertEqual(run_digest.windows_cli_arguments([
                '-s', '/home/state.json', 'archive', '-a', 'test', '-C=/mnt/c/archive.db',
                '-S/mnt/c/source.db', '-k', '/home/cp.json', '-o/home/out.json', '-d300']),
                ['-s', 'WIN:/home/state.json', 'archive', '-a', 'test', '-C=WIN:/mnt/c/archive.db',
                 '-S', 'WIN:/mnt/c/source.db', '-k', 'WIN:/home/cp.json', '-o', 'WIN:/home/out.json', '-d300'])

    def test_windows_cli_arguments_convert_only_path_options(self) -> None:
        converter = lambda value: f"WIN:{value}" if value.startswith("/") else value
        with patch.object(run_digest, "windows_native_path", side_effect=converter):
            converted = run_digest.windows_cli_arguments([
                "--state", "/home/user/state.json",
                "run",
                "--cache=/mnt/c/cache.sqlite3",
                "--output", "report.md",
                "--days", "7",
            ])
        self.assertEqual(converted, [
            "--state", "WIN:/home/user/state.json",
            "run",
            "--cache=WIN:/mnt/c/cache.sqlite3",
            "--output", "report.md",
            "--days", "7",
        ])

    def test_wsl_with_native_npx_stays_in_wsl(self) -> None:
        with (
            patch.object(run_digest, "is_wsl", return_value=True),
            patch.object(run_digest, "playwright_npx_path", return_value="/usr/bin/npx"),
            patch.object(run_digest.subprocess, "run") as execute,
        ):
            self.assertIsNone(run_digest.maybe_reexec_windows_runtime(["run"]))
        execute.assert_not_called()

    def test_native_wsl_paths_stay_posix(self):
        path = Path("scripts/collect.js")
        with (
            patch.object(run_digest, "is_wsl", return_value=True),
            patch.object(run_digest, "windows_native_path") as convert,
        ):
            self.assertEqual(run_digest.native_path(path), str(path.resolve()))
        convert.assert_not_called()

    def test_wsl_with_windows_npx_reexecutes_windows_python(self) -> None:
        completed = argparse.Namespace(returncode=7)
        with (
            patch.object(run_digest, "is_wsl", return_value=True),
            patch.object(
                run_digest,
                "playwright_npx_path",
                return_value="/mnt/c/Program Files/nodejs/npx",
            ),
            patch.object(
                run_digest,
                "windows_python_command",
                return_value=["/mnt/c/Python/python.exe"],
            ),
            patch.object(
                run_digest,
                "windows_native_path",
                side_effect=lambda value: f"WIN:{value}" if value.startswith("/") else value,
            ),
            patch.object(run_digest.subprocess, "run", return_value=completed) as execute,
            patch.dict(
                run_digest.os.environ,
                {"PWCLI": "/tmp/pwcli", "PLAYWRIGHT_CLI_NPX": "/mnt/c/npx"},
            ),
        ):
            result = run_digest.maybe_reexec_windows_runtime([
                "--state", "/home/user/state.json", "run", "--days", "7"
            ])

        self.assertEqual(result, 7)
        command = execute.call_args.args[0]
        child_environment = execute.call_args.kwargs["env"]
        self.assertEqual(command[0], "/mnt/c/Python/python.exe")
        self.assertIn("WIN:/home/user/state.json", command)
        self.assertEqual(child_environment["PYTHONUTF8"], "1")
        self.assertNotIn("PWCLI", child_environment)
        self.assertNotIn("PLAYWRIGHT_CLI_NPX", child_environment)

    def test_windows_native_path_detection_does_not_treat_unc_cwd_as_wsl(self) -> None:
        with (
            patch.object(run_digest, "running_on_windows", return_value=True),
            patch.dict(run_digest.os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}),
        ):
            self.assertFalse(run_digest.is_wsl())


class ProcessInterruptionTests(unittest.TestCase):
    def test_stop_collector_process_terminates_and_waits(self) -> None:
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(run_digest.os, "name", "posix"), patch.object(run_digest.os, "killpg", create=True) as killpg, patch.object(run_digest.signal, "SIGKILL", 9, create=True):
            run_digest.stop_collector_process(process, timeout=1)
        self.assertEqual([call.args[1] for call in killpg.call_args_list], [run_digest.signal.SIGTERM, 9])
        self.assertEqual(process.wait.call_count, 2)
        process.kill.assert_not_called()

    def test_stop_collector_process_kills_after_timeout(self) -> None:
        process = MagicMock()
        process.poll.side_effect = [None, None]
        process.wait.side_effect = [
            run_digest.subprocess.TimeoutExpired("collector", 1),
            0,
        ]
        with patch.object(run_digest.os, "name", "posix"), patch.object(run_digest.os, "killpg", create=True) as killpg, patch.object(run_digest.signal, "SIGKILL", 9, create=True):
            run_digest.stop_collector_process(process, timeout=1)
        self.assertEqual([call.args[1] for call in killpg.call_args_list],
                         [run_digest.signal.SIGTERM, 9])
        self.assertEqual(process.wait.call_count, 2)


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

    def test_keyboard_interrupt_flushes_resumable_checkpoint(self) -> None:
        args = run_digest.build_parser().parse_args([
            "standalone",
            "--days", "1",
            "--min-comments", "100",
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args.cache = root / "cache.sqlite3"
            args.checkpoint = root / "checkpoint.json"
            args.output = root / "report.md"
            server = MagicMock()
            server.url = "http://127.0.0.1:12345/ingest?token=test"
            with (
                patch.object(run_digest, "ensure_standalone_login", return_value=MagicMock()),
                patch.object(run_digest, "SinkServer", return_value=server),
                patch.object(
                    run_digest,
                    "run_persistent_collector",
                    side_effect=KeyboardInterrupt,
                ),
            ):
                with self.assertRaisesRegex(run_digest.CliError, "检查点已保存"):
                    run_digest.run_standalone(args)

            checkpoint = run_digest.read_checkpoint(args.checkpoint)
            self.assertEqual(checkpoint["next_page"], 1)
            self.assertFalse(checkpoint["completed"])
            server.close.assert_called_once_with()

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
            runtime_root = root / "stable-runtime"
            with patch.dict(
                run_digest.os.environ,
                {"HOLECLAW_RUNTIME_DIR": str(runtime_root)},
            ):
                try:
                    run_digest.os.chdir(root)
                    new_cache = run_digest.default_cache_path()
                    new_checkpoint = run_digest.default_checkpoint_path(
                        {"min_comments": 50}
                    )
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

    def test_completed_checkpoint_uses_the_shared_cache_report_branch(self) -> None:
        day = run_digest.datetime.now(run_digest.SHANGHAI).date() - timedelta(days=1)
        args = run_digest.build_parser().parse_args([
            "standalone",
            "--since", day.isoformat(),
            "--until", day.isoformat(),
            "--min-comments", "100",
        ])
        run_digest.resolve_thresholds(args)
        spec = run_digest.window_spec(args)
        start_ts, end_ts, label = run_digest.time_window(args)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args.cache = root / "cache.sqlite3"
            args.checkpoint = root / "checkpoint.json"
            args.output = root / "report.md"
            cache = run_digest.CacheStore(args.cache)
            checkpoint = run_digest.new_checkpoint(
                spec, start_ts, end_ts, start_ts, label
            )
            checkpoint.update({
                "cache_path": str(args.cache.resolve()),
                "cache_instance_id": cache.instance_id,
                "total_pages": 3,
                "total_scanned": 12,
                "completed": True,
                "completed_at": checkpoint["updated_at"],
            })
            run_digest.write_checkpoint(args.checkpoint, checkpoint)
            cache.close()

            with (
                patch.object(run_digest, "emit_cached_report") as emit,
                patch.object(
                    run_digest,
                    "ensure_standalone_login",
                    side_effect=AssertionError("cache reuse must not initialize a browser"),
                ),
            ):
                run_digest.run_standalone(args)

            emitted = emit.call_args.args
            self.assertEqual(emitted[3:7], (3, 12, False, "reused_completed_checkpoint"))

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
    def make_sink(
        self,
        *,
        min_comments: int | None = 0,
        min_favorites: int | None = None,
        match_mode: str = "all",
        request: dict | None = None,
    ) -> tuple[run_digest.CacheStore, dict, run_digest.RunSink]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        cache = run_digest.CacheStore(root / "cache.sqlite3")
        self.addCleanup(cache.close)
        checkpoint = run_digest.new_checkpoint(
            request
            if request is not None
            else {
                "min_comments": min_comments,
                "min_favorites": min_favorites,
                "match_mode": match_mode,
            },
            100,
            200,
            100,
            "test",
        )
        sink = run_digest.RunSink(
            cache,
            checkpoint,
            root / "checkpoint.json",
            min_comments,
            min_favorites,
            match_mode,
        )
        return cache, checkpoint, sink

    def retry_payload(self):
        return {
            "schema_version": run_digest.SINK_SCHEMA_VERSION,
            "start_page": 1, "end_page": 1, "pages": 1, "scanned": 1,
            "rows": [{"pid": "1", "timestamp": 150, "reply": 1,
                      "favorites": 0, "text": "retry"}],
            "matched_pids": ["1"], "telemetry": {"list_requests": 1},
            "terminal": True, "reached_start": True,
        }

    def test_committed_chunk_retry_is_idempotent(self):
        cache, checkpoint, sink = self.make_sink()
        payload = self.retry_payload()
        sink.ingest(payload)
        snapshot = json.dumps(checkpoint, sort_keys=True)
        sink.ingest(json.loads(json.dumps(payload)))
        self.assertEqual(json.dumps(checkpoint, sort_keys=True), snapshot)
        self.assertEqual(cache.post_count(), 1)
        self.assertEqual(sink.progress_sequence, 1)
        self.assertTrue(sink.result()["reached_start"])
        payload["rows"][0]["reply"] = 2
        with self.assertRaisesRegex(run_digest.CliError, "non-sequential"):
            sink.ingest(payload)

    def test_retry_recovers_checkpoint_write_failure_without_double_counting(self):
        cache, checkpoint, sink = self.make_sink()
        payload = self.retry_payload()
        with patch("scripts.holeclaw_sink.write_checkpoint", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                sink.ingest(payload)
        sink.ingest(payload)
        self.assertEqual(checkpoint["total_pages"], 1)
        self.assertEqual(checkpoint["telemetry"]["list_requests"], 1)
        self.assertEqual(cache.post_count(), 1)
        self.assertEqual(run_digest.read_checkpoint(sink.checkpoint_path)["next_page"], 2)

    def test_outside_window_missing_favorites_do_not_invalidate_coverage(self):
        for min_favorites in (None, 10):
            with self.subTest(min_favorites=min_favorites):
                _cache, checkpoint, sink = self.make_sink(min_favorites=min_favorites)
                payload = self.retry_payload()
                payload["rows"][0].update(timestamp=250, favorites=None)
                payload["matched_pids"] = []
                sink.ingest(payload)
                self.assertTrue(checkpoint["favorites_complete"])

    def test_cache_chunk_rolls_back_when_second_write_fails(self) -> None:
        cache, checkpoint, sink = self.make_sink(
            min_comments=10, request={"min_comments": 10}
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

    def test_invalid_match_is_rejected_before_cache_mutation(self) -> None:
        cache, checkpoint, sink = self.make_sink()
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

    def test_matched_pid_must_exist_in_rows(self) -> None:
        cache, checkpoint, sink = self.make_sink()
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

    def test_matched_pids_must_use_v2_list_shape(self) -> None:
        cache, _checkpoint, sink = self.make_sink()
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

    def test_ingest_accumulates_collector_and_cache_telemetry(self) -> None:
        _cache, checkpoint, sink = self.make_sink()
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

    def test_favorite_filter_accepts_matching_rows_and_rejects_missing_counts(self) -> None:
        cache, checkpoint, sink = self.make_sink(
            min_comments=None,
            min_favorites=10,
            request={"min_comments": None, "min_favorites": 10},
        )
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

    def test_any_filter_accepts_explicitly_unavailable_favorite(self) -> None:
        cache, checkpoint, sink = self.make_sink(
            min_comments=100,
            min_favorites=45,
            match_mode="any",
            request={"min_comments": 100, "min_favorites": 45, "match_mode": "any"},
        )
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


if __name__ == "__main__":
    unittest.main()
