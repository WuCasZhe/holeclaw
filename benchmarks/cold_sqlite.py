"""Measure actual cold SQLite stores on native temp storage, never user caches.

Run from repository root: python3 benchmarks/cold_sqlite.py [output.json]
"""
import json
import platform
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.holeclaw_cache import CacheStore
from scripts.holeclaw_archive import ArchiveStore


def measure(kind, batch_size):
    timings = []
    for _ in range(5):
        with tempfile.TemporaryDirectory(prefix='holeclaw-cold-bench-') as directory:
            store = (CacheStore(Path(directory) / 'cold.sqlite3') if kind == 'posts'
                     else ArchiveStore(Path(directory) / 'cold.sqlite3', 'synthetic'))
            try:
                post = dict(pid='1', timestamp=1700000000, reply=1000,
                            favorites=10, type='text', text='synthetic ' * 20)
                size = 50000 if kind == 'posts' else 10000
                # Build data before timing; include validation, SQL and commits.
                rows = ([dict(post, pid=str(i)) for i in range(size)] if kind == 'posts' else
                        [dict(cid=str(i), text='synthetic comment ' * 10,
                              timestamp=1700000000, name_tag='A') for i in range(size)])
                started = time.perf_counter()
                for offset in range(0, size, batch_size):
                    batch = rows[offset:offset + batch_size]
                    if kind == 'posts':
                        store.upsert_posts(batch)
                    else:
                        # Each synthetic post has 1000 comments, mirroring the cap.
                        store.ingest_comments(dict(post=dict(post, pid=str(offset // 1000)),
                            comments=batch, comment_page=(offset % 1000) // batch_size + 1,
                            complete=(offset + batch_size) % 1000 == 0, archive_run='benchmark'))
                elapsed = time.perf_counter() - started
                assert store.integrity_check() == 'ok'
                actual = store.connection.execute(f'SELECT COUNT(*) FROM {kind}').fetchone()[0]
                assert actual == size
                timings.append(elapsed * 1000)
            finally:
                store.close()
    return dict(kind=kind, rows=size, batch_size=batch_size, repeats=5,
                median_ms=round(statistics.median(timings), 2),
                min_ms=round(min(timings), 2), max_ms=round(max(timings), 2),
                integrity='ok')


if __name__ == '__main__':
    result = dict(platform=platform.platform(), python=platform.python_version(),
                  sqlite=sqlite3.sqlite_version, storage=tempfile.gettempdir(),
                  results=[measure(kind, batch) for kind, batch in
                           [('posts', 500), ('posts', 5000), ('comments', 10), ('comments', 100), ('comments', 1000)]])
    output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(output, encoding='utf-8')
    print(output)
