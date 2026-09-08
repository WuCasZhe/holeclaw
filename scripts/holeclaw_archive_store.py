"""Account archive storage and resumable comment snapshots."""
import sqlite3
from contextlib import closing
from datetime import datetime

try:
    from holeclaw_cache import CacheStore
    from holeclaw_domain import CliError, SHANGHAI
    from holeclaw_search import install_search_indexes
except ModuleNotFoundError:
    from scripts.holeclaw_cache import CacheStore
    from scripts.holeclaw_domain import CliError, SHANGHAI
    from scripts.holeclaw_search import install_search_indexes


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
            if 'unavailable' not in columns:
                self.connection.execute('ALTER TABLE comment_scans ADD COLUMN unavailable TEXT')
            self.connection.execute("INSERT OR REPLACE INTO metadata VALUES('archive_schema_version', '3')")
            self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_candidates (
                run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, pid TEXT NOT NULL,
                timestamp INTEGER NOT NULL, reply INTEGER NOT NULL, favorites INTEGER,
                type TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(run_id, ordinal))''')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS archive_members (
                run_id TEXT NOT NULL, pid TEXT NOT NULL, reply INTEGER NOT NULL,
                PRIMARY KEY(run_id, pid))''')
            columns = {row[1] for row in self.connection.execute('PRAGMA table_info(archive_candidates)')}
            if 'observed_at' not in columns:
                self.connection.execute('ALTER TABLE archive_candidates ADD COLUMN observed_at INTEGER')
        install_search_indexes(self)

    def stage_candidates(self, source_path, checkpoint, filters, end):
        clause, parameters = filters.sql_clause()
        self.connection.execute('ATTACH DATABASE ? AS source', (source_path.as_uri() + '?mode=ro',))
        try:
            with self.transaction():
                self.connection.execute('DELETE FROM archive_candidates WHERE run_id=?', (checkpoint['created_at'],))
                self.connection.execute(f'''INSERT INTO archive_candidates
                    (run_id,ordinal,pid,timestamp,reply,favorites,type,text,observed_at)
                    SELECT ?, ROW_NUMBER() OVER (ORDER BY timestamp DESC, pid DESC),
                           pid,timestamp,reply,favorites,type,text,observed_at FROM source.posts
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
                SELECT pid,timestamp,reply,favorites AS likenum,type,text,
                       COALESCE(observed_at, 0) AS observed_at FROM archive_candidates
                WHERE run_id=? AND ordinal>? AND ordinal<=? ORDER BY ordinal''',
                (run_id, (page - 1) * 500, page * 500))]

    def finish_cached_candidates(self, checkpoint):
        """Finish without launching a browser if every selected comment snapshot exists."""
        run_id = checkpoint['created_at']
        count = self.connection.execute('SELECT COUNT(*) FROM archive_candidates WHERE run_id=?', (run_id,)).fetchone()[0]
        if count != checkpoint.get('cached_posts', 0):
            raise CliError('Cached candidate snapshot is missing. Restore it or use --fresh.')
        pending = self.connection.execute('''SELECT 1 FROM archive_candidates c
            LEFT JOIN comment_scans s ON s.pid=c.pid
            WHERE c.run_id=? AND (s.pid IS NULL OR (s.complete!=1 AND s.unavailable IS NULL) OR s.reply_count IS NULL
                OR s.reply_count!=c.reply) LIMIT 1''', (run_id,)).fetchone()
        if pending or (self.media and not self.media.candidates_complete(run_id)):
            return False
        with self.transaction():
            for page in range(1, checkpoint.get('archive_cached_pages', 0) + 1):
                posts = self.candidate_page(run_id, page)
                for post in posts:
                    post['favorites'] = post.pop('likenum')
                self.record_posts(posts, run_id)
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
                run_id=excluded.run_id, reply_count=excluded.reply_count,page_size=excluded.page_size,unavailable=NULL""",
                (str(post['pid']), page, int(complete), now, payload['archive_run'], int(post['reply']), page_size))

    def resume_comments(self, pid, run_id, reply=None, fresh=False, page_size=100):
        with self.lock:
            row = self.connection.execute(
                'SELECT last_page, complete, run_id, reply_count, page_size, unavailable FROM comment_scans WHERE pid=?',
                (str(pid),)).fetchone()
            return self._resume(row, run_id, reply, fresh, page_size)

    @staticmethod
    def _resume(row, run_id, reply, fresh=False, page_size=100):
        if (row and row['unavailable'] and row['reply_count'] == reply
                and (not fresh or row['run_id'] == run_id)):
            return {'next_page': 1, 'complete': False, 'unavailable': row['unavailable']}
        if row and row['complete'] and reply is not None and row['reply_count'] == reply and (not fresh or row['run_id'] == run_id):
            return {'next_page': 1, 'complete': True}
        if (not row or row['run_id'] != run_id
                or (reply is not None and row['reply_count'] != reply)
                or (not row['complete'] and row['page_size'] != page_size)):
            return {'next_page': 1, 'complete': False}
        # Replay the last durable page: new replies/deletions can shift offsets.
        return {'next_page': max(1, row['last_page']), 'complete': bool(row['complete'])}

    def record_unavailable(self, post, run_id):
        """Keep inaccessible posts explicit; never claim their comments were read."""
        with self.transaction():
            self.record_posts([post], run_id)
            self.connection.execute('''INSERT INTO comment_scans
                (pid,last_page,complete,observed_at,run_id,reply_count,page_size,unavailable)
                VALUES(?,1,0,?,?,?,100,'post_not_found') ON CONFLICT(pid) DO UPDATE SET
                complete=0,observed_at=excluded.observed_at,run_id=excluded.run_id,
                reply_count=excluded.reply_count,unavailable=excluded.unavailable''',
                (str(post['pid']), int(datetime.now(SHANGHAI).timestamp()), run_id, post['reply']))
            if self.media:
                self.media.unavailable(str(post['pid']), 'post_not_found', post['reply'])

    def resume_batch(self, posts, run_id, fresh=False):
        scans = self.rows_by_pid('comment_scans', (post['pid'] for post in posts))
        media_states = self.media.states(posts) if self.media else {}
        resumes = {}
        for post in posts:
            pid = str(post['pid'])
            resume = self._resume(scans.get(pid), run_id, post['reply'], fresh)
            if self.media:
                state = media_states[pid]
                if fresh:
                    state.update(post_known=False, unavailable=None)
                if not state['comments_known'] and resume['complete']:
                    resume = {'next_page': 1, 'complete': False}
                unavailable = resume.get('unavailable')
                resume.update(state)
                if unavailable:
                    resume['unavailable'] = unavailable
            resumes[pid] = resume
        return resumes

    def record_posts(self, posts, run_id):
        with self.transaction():
            self.upsert_posts(posts)
            for offset in range(0, len(posts), 500):
                batch = posts[offset:offset + 500]
                members = dict(self.connection.execute(
                    f'''SELECT pid,reply FROM archive_members WHERE run_id=?
                        AND pid IN ({','.join('?' for _ in batch)})''',
                    [run_id, *(str(post['pid']) for post in batch)]))
                values = []
                for post in batch:
                    pid, reply = str(post['pid']), post['reply']
                    if members.get(pid) != reply:
                        values.append((run_id, pid, reply))
                        members[pid] = reply
                self.connection.executemany('''INSERT INTO archive_members VALUES(?,?,?)
                    ON CONFLICT(run_id,pid) DO UPDATE SET reply=excluded.reply''', values)

    def cached_work_page(self, checkpoint, page):
        """Keep snapshot ordinals stable; return only posts needing browser work."""
        posts = self.candidate_page(checkpoint['created_at'], page)
        expected = min(500, checkpoint['cached_posts'] - (page - 1) * 500)
        if len(posts) != expected:
            raise CliError('Cached candidate snapshot is missing. Restore it or use --fresh.')
        for post in posts:
            post['favorites'] = post['likenum']
        with self.transaction():
            resumes = self.resume_batch(posts, checkpoint['created_at'], checkpoint.get('fresh', False))
            self.complete_empty_posts(posts, checkpoint['created_at'], resumes)
            pending_media = self.media.pending_many([post['pid'] for post in posts]) if self.media else {}
            complete, pending = [], []
            for post in posts:
                resume = resumes[post['pid']]
                done = resume['complete'] or bool(resume.get('unavailable'))
                if self.media:
                    done = (bool(resume['unavailable']) or (done and resume['post_known'])) and not pending_media[post['pid']]
                (complete if done else pending).append(post)
            self.add_saved_comment_ids(pending, resumes)
            self.record_posts(posts, checkpoint['created_at'])
        return dict(posts=pending, source_count=len(posts),
                    oldest=min((post['timestamp'] for post in posts), default=0),
                    reused=len(complete), resumes={str(post['pid']): resumes[str(post['pid'])] for post in pending})

    def prepare_posts(self, posts, run_id, fresh=False):
        with self.transaction():
            resumes = self.resume_batch(posts, run_id, fresh)
            self.complete_empty_posts(posts, run_id, resumes)
            self.add_saved_comment_ids(posts, resumes)
            self.record_posts(posts, run_id)
        return resumes

    def complete_empty_posts(self, posts, run_id, resumes):
        empty = [post for post in posts if post['reply'] == 0 and not post.get('unavailable')
                 and not resumes[str(post['pid'])].get('unavailable')
                 and not resumes[str(post['pid'])]['complete']]
        if not empty:
            return
        now = int(datetime.now(SHANGHAI).timestamp())
        self.connection.executemany('''INSERT INTO comment_scans
            (pid,last_page,complete,observed_at,run_id,reply_count,page_size,unavailable)
            VALUES(?,1,1,?,?,0,100,NULL) ON CONFLICT(pid) DO UPDATE SET
            last_page=1,complete=1,observed_at=excluded.observed_at,run_id=excluded.run_id,
            reply_count=0,page_size=100,unavailable=NULL''',
            [(str(post['pid']), now, run_id) for post in empty])
        if self.media:
            self.connection.executemany('''INSERT INTO media_scans(pid,comments_complete,reply_count)
                VALUES(?,1,0) ON CONFLICT(pid) DO UPDATE SET comments_complete=1,reply_count=0''',
                [(str(post['pid']),) for post in empty])
        for post in empty:
            resumes[str(post['pid'])].update(complete=True, next_page=1, empty_completed=True)
            if self.media:
                resumes[str(post['pid'])]['comments_known'] = True

    def add_saved_comment_ids(self, posts, resumes):
        for post in posts:
            resume = resumes[str(post['pid'])]
            if not resume['complete'] and resume['next_page'] > 1:
                resume['saved_comment_ids'] = [row[0] for row in self.connection.execute(
                    'SELECT cid FROM comments WHERE pid=? LIMIT 1000', (str(post['pid']),))]

    def summary(self, *, verify=False):
        return {
            'posts': self.post_count(),
            'comments': self.connection.execute('SELECT COUNT(*) FROM comments').fetchone()[0],
            'posts_with_completed_comment_scan': self.connection.execute(
                'SELECT COUNT(*) FROM comment_scans WHERE complete=1').fetchone()[0],
            'cache_integrity': self.integrity_check() if verify else 'not_checked',
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
                 WHERE s.complete=1 AND s.reply_count=p.reply) AS completed_comment_posts,
                (SELECT COUNT(*) FROM selected p JOIN comment_scans s USING(pid)
                 WHERE s.unavailable IS NOT NULL AND s.reply_count=p.reply) AS unavailable_posts''',
                parameters).fetchone()
        result = dict(row)
        result['incomplete_comment_posts'] = result['posts'] - result['completed_comment_posts'] - result['unavailable_posts']
        return result
