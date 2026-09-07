"""Account archive storage and resumable comment snapshots."""
import sqlite3
from contextlib import closing
from datetime import datetime

try:
    from holeclaw_cache import CacheStore
    from holeclaw_domain import CliError, SHANGHAI
except ModuleNotFoundError:
    from scripts.holeclaw_cache import CacheStore
    from scripts.holeclaw_domain import CliError, SHANGHAI


class ArchiveStore(CacheStore):
    allow_archive = True

    def __init__(self, path, account):
        if path.exists():
            try:
                with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
                    marker = db.execute("SELECT value FROM metadata WHERE key='archive_account'").fetchone()
                    version = db.execute("SELECT value FROM metadata WHERE key='archive_schema_version'").fetchone()
                if marker != (account,):
                    raise CliError("Archive account does not match, or this is a digest cache. Use a separate --cache path.")
                if version not in (('1',), ('2',), ('3',)):
                    raise CliError('Incompatible archive schema. Use a new --cache path.')
            except sqlite3.Error as error:
                raise CliError("Invalid archive database. Use a new --cache path.") from error
        super().__init__(path)
        self.media = None
        with self.transaction():
            self.connection.execute("INSERT OR REPLACE INTO metadata VALUES('archive_account', ?)", (account,))
            self.connection.execute("""CREATE TABLE IF NOT EXISTS comments (
                pid TEXT NOT NULL, cid TEXT NOT NULL, text TEXT NOT NULL,
                timestamp INTEGER NOT NULL, name_tag TEXT NOT NULL, quote_cid TEXT,
                observed_at INTEGER NOT NULL, PRIMARY KEY(pid, cid))""")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS comment_scans (
                pid TEXT PRIMARY KEY, last_page INTEGER NOT NULL,
                complete INTEGER NOT NULL, observed_at INTEGER NOT NULL,
                run_id TEXT NOT NULL)""")
            columns = {row[1] for row in self.connection.execute('PRAGMA table_info(comment_scans)')}
            if 'reply_count' not in columns:
                self.connection.execute('ALTER TABLE comment_scans ADD COLUMN reply_count INTEGER')
                self.connection.execute('''UPDATE comment_scans SET reply_count=(
                    SELECT reply FROM posts WHERE posts.pid=comment_scans.pid) WHERE complete=1''')
            if 'page_size' not in columns:
                # NULL means legacy/unknown pagination; never reuse its offsets.
                self.connection.execute('ALTER TABLE comment_scans ADD COLUMN page_size INTEGER')
            self.connection.execute("INSERT OR REPLACE INTO metadata VALUES('archive_schema_version', '3')")
            self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_candidates (
                run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, pid TEXT NOT NULL,
                timestamp INTEGER NOT NULL, reply INTEGER NOT NULL, favorites INTEGER,
                type TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(run_id, ordinal))''')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_members (
                run_id TEXT NOT NULL, pid TEXT NOT NULL, reply INTEGER NOT NULL,
                PRIMARY KEY(run_id, pid))''')

    def stage_candidates(self, source_path, checkpoint, filters, end):
        clause, parameters = filters.sql_clause()
        self.connection.execute('ATTACH DATABASE ? AS source', (source_path.as_uri() + '?mode=ro',))
        try:
            with self.transaction():
                self.connection.execute('DELETE FROM archive_candidates WHERE run_id=?', (checkpoint['created_at'],))
                self.connection.execute(f'''INSERT INTO archive_candidates
                    SELECT ?, ROW_NUMBER() OVER (ORDER BY timestamp DESC, pid DESC),
                           pid,timestamp,reply,favorites,type,text FROM source.posts
                    WHERE timestamp>=? AND timestamp<? {('AND ' + clause) if clause else ''}''',
                    [checkpoint['created_at'], checkpoint['start_timestamp'], end, *parameters])
                count = self.connection.execute('SELECT COUNT(*) FROM archive_candidates WHERE run_id=?',
                                                (checkpoint['created_at'],)).fetchone()[0]
            return count
        finally:
            self.connection.execute('DETACH DATABASE source')

    def candidate_page(self, run_id, page):
        with self.lock:
            return [dict(row) for row in self.connection.execute('''
                SELECT pid,timestamp,reply,favorites AS likenum,type,text FROM archive_candidates
                WHERE run_id=? AND ordinal>? AND ordinal<=? ORDER BY ordinal''',
                (run_id, (page - 1) * 500, page * 500))]

    def finish_cached_candidates(self, checkpoint):
        """Finish without launching a browser if every selected comment snapshot exists."""
        run_id = checkpoint['created_at']
        count = self.connection.execute('SELECT COUNT(*) FROM archive_candidates WHERE run_id=?', (run_id,)).fetchone()[0]
        if count != checkpoint.get('cached_posts', 0):
            raise CliError('Cached candidate snapshot is missing. Restore it or use --fresh.')
        pending = self.connection.execute('''SELECT COUNT(*) FROM archive_candidates c
            LEFT JOIN comment_scans s ON s.pid=c.pid
            WHERE c.run_id=? AND (s.pid IS NULL OR s.complete!=1 OR s.reply_count IS NULL
                OR s.reply_count!=c.reply)''', (run_id,)).fetchone()[0]
        if pending or (self.media and not self.media.candidates_complete(run_id)):
            return False
        with self.transaction():
            for page in range(1, checkpoint.get('archive_cached_pages', 0) + 1):
                posts = self.candidate_page(run_id, page)
                for post in posts:
                    post['favorites'] = post.pop('likenum')
                self.upsert_posts(posts)
            self.connection.execute('''INSERT INTO archive_members
                SELECT run_id,pid,reply FROM archive_candidates WHERE run_id=?
                ON CONFLICT(run_id,pid) DO UPDATE SET reply=excluded.reply''', (run_id,))
        return True

    def ingest_comments(self, payload):
        post = payload.get('post')
        comments = payload.get('comments')
        page = payload.get('comment_page')
        complete = payload.get('complete')
        page_size = payload.get('comment_page_size', 100)
        if (not isinstance(post, dict) or not isinstance(comments, list)
                or not isinstance(page, int) or page < 1
                or not isinstance(complete, bool)
                or type(page_size) is not int or page_size not in (10, 100)):
            raise CliError('Invalid archive comment chunk.')
        now = int(datetime.now(SHANGHAI).timestamp())
        values = []
        for comment in comments:
            if not isinstance(comment, dict) or not isinstance(comment.get('cid'), str) or not comment['cid']:
                raise CliError('Invalid archive comment ID.')
            values.append((str(post['pid']), comment['cid'], str(comment.get('text') or ''),
                           int(comment.get('timestamp', 0)), str(comment.get('name_tag') or ''),
                           comment.get('quote_cid'), now))
        with self.transaction():
            if self.media:
                self.media.record_comments(payload)
            self.upsert_posts([post])
            self.connection.executemany("""INSERT INTO comments VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(pid,cid) DO UPDATE SET text=excluded.text,
                timestamp=excluded.timestamp, name_tag=excluded.name_tag,
                quote_cid=excluded.quote_cid, observed_at=excluded.observed_at""", values)
            self.connection.execute("""INSERT INTO comment_scans
                (pid,last_page,complete,observed_at,run_id,reply_count,page_size) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(pid) DO UPDATE SET last_page=excluded.last_page,
                complete=excluded.complete, observed_at=excluded.observed_at,
                run_id=excluded.run_id, reply_count=excluded.reply_count,page_size=excluded.page_size""",
                (str(post['pid']), page, int(complete), now, payload['archive_run'], int(post['reply']), page_size))

    def resume_comments(self, pid, run_id, reply=None, fresh=False, page_size=100):
        with self.lock:
            row = self.connection.execute(
                'SELECT last_page, complete, run_id, reply_count, page_size FROM comment_scans WHERE pid=?',
                (str(pid),)).fetchone()
            if row and row[1] and reply is not None and row[3] == reply and (not fresh or row[2] == run_id):
                return {'next_page': 1, 'complete': True}
            if not row or row[2] != run_id:
                return {'next_page': 1, 'complete': False}
            if reply is not None and row[3] != reply:
                return {'next_page': 1, 'complete': False}
            if not row[1] and row[4] != page_size:
                return {'next_page': 1, 'complete': False}
            # Replay the last durable page: new replies/deletions can shift offsets.
            return {'next_page': max(1, row[0]), 'complete': bool(row[1])} if row else {'next_page': 1, 'complete': False}

    def prepare_posts(self, posts, run_id, fresh=False):
        resumes = {}
        with self.transaction():
            for post in posts:
                resume = self.resume_comments(post['pid'], run_id, post['reply'], fresh)
                if self.media:
                    state = self.media.state(post['pid'], post['reply'])
                    if fresh:
                        state.update(post_known=False, unavailable=None)
                    if not state['comments_known'] and resume['complete']:
                        resume = {'next_page': 1, 'complete': False}
                    resume.update(state)
                if not resume['complete'] and resume['next_page'] > 1:
                    resume['saved_comment_ids'] = [row[0] for row in self.connection.execute(
                        'SELECT cid FROM comments WHERE pid=? LIMIT 1000', (str(post['pid']),))]
                resumes[post['pid']] = resume
            self.upsert_posts(posts)
            self.connection.executemany('''INSERT INTO archive_members VALUES(?,?,?)
                ON CONFLICT(run_id,pid) DO UPDATE SET reply=excluded.reply''',
                [(run_id, post['pid'], post['reply']) for post in posts])
        return resumes

    def summary(self):
        return {
            'posts': self.post_count(),
            'comments': self.connection.execute('SELECT COUNT(*) FROM comments').fetchone()[0],
            'posts_with_completed_comment_scan': self.connection.execute(
                'SELECT COUNT(*) FROM comment_scans WHERE complete=1').fetchone()[0],
            'cache_integrity': self.integrity_check(),
        }

    def window_summary(self, checkpoint, filters):
        clause, parameters = filters.sql_clause()
        # Count only selected posts in this run's frozen window; historical
        # archive totals must not be presented as today's collection result.
        if checkpoint.get('archive_summary_version') == 1:
            selected = 'SELECT pid,reply FROM archive_members WHERE run_id=?'
            parameters = [checkpoint['created_at']]
        else:
            selected = f'''SELECT pid, reply FROM posts WHERE timestamp>=? AND timestamp<?
                {('AND ' + clause) if clause else ''}'''
            parameters = [checkpoint['start_timestamp'], checkpoint['end_timestamp'], *parameters]
        with self.lock:
            row = self.connection.execute(f'''WITH selected AS ({selected}) SELECT
                (SELECT COUNT(*) FROM selected) AS posts,
                (SELECT MIN(timestamp) FROM posts JOIN selected USING(pid)) AS oldest_archived_post_timestamp,
                (SELECT COUNT(*) FROM comments JOIN selected USING(pid)) AS comments,
                (SELECT COUNT(*) FROM selected p JOIN comment_scans s USING(pid)
                 WHERE s.complete=1 AND s.reply_count=p.reply) AS completed_comment_posts''',
                parameters).fetchone()
        result = dict(row)
        result['incomplete_comment_posts'] = result['posts'] - result['completed_comment_posts']
        return result


