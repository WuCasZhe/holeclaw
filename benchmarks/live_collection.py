"""Opt-in real collection with a newly created, native temporary cache.

Uses the existing CLI workflow unchanged; never reads authentication contents.
Example: python benchmarks/live_collection.py --state /path/to/state.json -j 8 -n 8
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import run_digest as runtime
from scripts.holeclaw_archive import run_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--mode', choices=['standalone', 'archive'], default='standalone')
    parser.add_argument('-j', '--concurrency', type=int, choices=range(1, 9), default=8)
    parser.add_argument('-n', '--pages', type=int, default=8)
    parser.add_argument('-c', '--min-comments', type=int, default=100)
    parser.add_argument('--days', type=int, default=7)
    options = parser.parse_args()
    if not 1 <= options.pages <= 8:
        parser.error('live benchmark accepts 1–8 list pages')
    if runtime.is_wsl() and runtime.is_windows_mounted_path(runtime.playwright_npx_path()):
        parser.error('Launch in the native environment used by the browser; do not auto-transfer benchmark paths.')
    directory = Path(tempfile.mkdtemp(prefix='holeclaw-live-bench-'))
    checkpoint = directory / 'checkpoint.json'
    cache = directory / 'cache.sqlite3'
    source = directory / 'source.sqlite3'
    session = 'hc-perf-' + directory.name.removeprefix('holeclaw-live-bench-')
    arguments = ['--state', str(options.state.resolve()), '--session', session,
        options.mode, '--non-interactive', '--days', str(options.days),
        '--concurrency', str(options.concurrency), '--max-total-pages', str(options.pages),
        '--min-comments', str(options.min_comments), '--cache', str(cache),
        '--checkpoint', str(checkpoint), '--progress-seconds', '30',
        '--output', str(directory / ('archive.json' if options.mode == 'archive' else 'report.md'))]
    if options.mode == 'archive':
        arguments.extend(['--account', 'performance-benchmark', '--source-cache', str(source)])
    timings = {}
    for name in ['ensure_standalone_login', 'run_persistent_collector']:
        original = getattr(runtime, name)
        def measured(*args, _name=name, _original=original, **kwargs):
            started = time.perf_counter()
            try:
                return _original(*args, **kwargs)
            finally:
                timings[_name + '_seconds'] = round(time.perf_counter() - started, 3)
        setattr(runtime, name, measured)
    args = runtime.build_parser().parse_args(arguments)
    result = dict(mode=options.mode, concurrency=options.concurrency,
                  page_limit=options.pages, directory=str(directory), session=session,
                  fresh_database=True, min_comments=options.min_comments,
                  process_platform=sys.platform, pid=os.getpid())
    print('LIVE_BENCHMARK ' + json.dumps(result), flush=True)
    started = time.perf_counter()
    try:
        if options.mode == 'archive':
            run_archive(args, runtime)
        else:
            runtime.run_standalone(args)
    except runtime.CliError as error:
        result['collector_message'] = str(error)
    finally:
        result['external_wall_seconds'] = round(time.perf_counter() - started, 3)
        result['timings'] = timings
        if checkpoint.exists():
            state = json.loads(checkpoint.read_text(encoding='utf-8'))
            for field in ['completed', 'total_pages', 'total_scanned', 'telemetry',
                          'archive_metrics', 'cache_reused', 'next_page']:
                result[field] = state.get(field)
        result['databases'] = []
        for database in [cache, source]:
            if database.exists():
                with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
                    record = dict(path=str(database), integrity=connection.execute('PRAGMA integrity_check').fetchone()[0])
                    record['posts'] = connection.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
                    record['reply_counts_above_threshold'] = [row[0] for row in connection.execute(
                        'SELECT reply FROM posts WHERE reply>? ORDER BY reply DESC', (options.min_comments,))]
                    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='comments'").fetchone():
                        record['comments'] = connection.execute('SELECT COUNT(*) FROM comments').fetchone()[0]
                        record['completed_comment_posts'] = connection.execute('SELECT COUNT(*) FROM comment_scans WHERE complete=1').fetchone()[0]
                    result['databases'].append(record)
        output = directory / 'benchmark.json'
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('LIVE_RESULT ' + json.dumps(result, ensure_ascii=False), flush=True)
        # Close only the dedicated session opened by this benchmark.
        runtime.BrowserCli(session, headed=False).run('close', check=False)


if __name__ == '__main__':
    main()
