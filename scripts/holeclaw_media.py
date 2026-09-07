"""Account-local image references and atomic, content-verified downloads."""
import base64
import binascii
import hashlib
import os
import re
import time

try:
    from holeclaw_domain import CliError
except ModuleNotFoundError:
    from scripts.holeclaw_domain import CliError


MAX_IMAGE_BYTES = 20 * 1024 * 1024


def image_extension(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    if data[:2] == b'BM':
        return '.bmp'
    if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'):
        return '.avif'
    return None


class MediaStore:
    def __init__(self, archive, directory, download=False):
        self.archive = archive
        self.db = archive.connection
        self.directory = directory.resolve()
        self.download = download
        self.reserved = set()
        self.plan_receipts = {}
        with archive.transaction():
            self.db.execute('''CREATE TABLE IF NOT EXISTS media_refs (
                pid TEXT NOT NULL, cid TEXT NOT NULL, media_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL, source_url TEXT NOT NULL,
                PRIMARY KEY(pid,cid,media_key))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS media_files (
                media_key TEXT PRIMARY KEY, path TEXT, mime TEXT, bytes INTEGER,
                sha256 TEXT, status TEXT NOT NULL, observed_at INTEGER NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS media_scans (
                pid TEXT PRIMARY KEY, post_complete INTEGER NOT NULL DEFAULT 0,
                comments_complete INTEGER NOT NULL DEFAULT 0, reply_count INTEGER)''')

            columns = {row[1] for row in self.db.execute('PRAGMA table_info(media_scans)')}
            if 'unavailable' not in columns:
                self.db.execute('ALTER TABLE media_scans ADD COLUMN unavailable TEXT')

    def unavailable(self, pid, reason):
        with self.archive.transaction():
            self.db.execute("""INSERT INTO media_scans(pid,unavailable) VALUES(?,?)
                ON CONFLICT(pid) DO UPDATE SET unavailable=excluded.unavailable""", (pid, reason))

    def state(self, pid, reply):
        row = self.db.execute('SELECT * FROM media_scans WHERE pid=?', (pid,)).fetchone()
        return dict(unavailable=row['unavailable'] if row else None, post_known=bool(row and row['post_complete']),
                    comments_known=bool(row and row['comments_complete'] and row['reply_count'] == reply))

    def references(self, pid, cid, ids, legacy=False):
        if not isinstance(ids, list) or len(ids) > 100:
            raise CliError('Invalid image references.')
        keys = []
        for raw in ids:
            if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', raw):
                raise CliError('Invalid image ID.')
            keys.append('id:' + raw)
        if legacy and not keys:
            if not re.fullmatch(r'\d+', pid):
                raise CliError('Invalid legacy image PID.')
            keys = ['pid:' + pid]
        self.db.execute('DELETE FROM media_refs WHERE pid=? AND cid=?', (pid, cid))
        for ordinal, key in enumerate(dict.fromkeys(keys)):
            kind, value = key.split(':', 1)
            url = 'https://treehole.pku.edu.cn/chapi/api/v3/media/getMediaBinary?' + kind + '=' + value
            self.db.execute('INSERT INTO media_refs VALUES(?,?,?,?,?)', (pid, cid, key, ordinal, url))

    def record_post(self, post):
        pid = str(post['pid'])
        with self.archive.transaction():
            self.references(pid, '', post['media_ids'], post.get('type') == 'image')
            self.db.execute('''INSERT INTO media_scans(pid,post_complete) VALUES(?,1)
                ON CONFLICT(pid) DO UPDATE SET post_complete=1,unavailable=NULL''', (pid,))

    def record_comments(self, payload):
        # Called inside the same transaction as text and comment checkpoints.
        pid = str(payload['post']['pid'])
        for comment in payload['comments']:
            self.references(pid, comment['cid'], comment['media_ids'])
        self.db.execute('''INSERT INTO media_scans(pid,comments_complete,reply_count) VALUES(?,?,?)
            ON CONFLICT(pid) DO UPDATE SET comments_complete=excluded.comments_complete,
            reply_count=excluded.reply_count''', (pid, int(payload['complete']), payload['post']['reply']))

    def pending(self, pid):
        if not self.download:
            return []
        result = []
        for row in self.db.execute("""SELECT DISTINCT r.media_key,r.source_url,f.path,f.bytes,f.status
                FROM media_refs r LEFT JOIN media_files f USING(media_key) WHERE r.pid=?""", (pid,)):
            if row['status'] in ('unavailable', 'not_image'):
                continue
            path = self.directory / (row['path'] or '__missing__')
            if row['status'] == 'downloaded' and path.is_file() and path.stat().st_size == row['bytes']:
                continue
            result.append(dict(media_key=row['media_key'], url=row['source_url']))
        return result

    def plan(self, pid, request_id=None):
        # A lost HTTP response must replay the reservation, not hide its images.
        previous = self.plan_receipts.get(pid)
        if request_id is not None and previous and previous[0] == request_id:
            return previous[1]
        result = [row for row in self.pending(pid) if row['media_key'] not in self.reserved][:20]
        self.reserved.update(row['media_key'] for row in result)
        if request_id is not None and result:
            self.plan_receipts[pid] = (request_id, result)
        return result

    def cancel_plans(self):
        self.reserved.clear()
        self.plan_receipts.clear()

    def candidates_complete(self, run_id):
        for row in self.db.execute('SELECT pid,reply FROM archive_candidates WHERE run_id=?', (run_id,)):
            state = self.state(row['pid'], row['reply'])
            if not state['unavailable'] and not (state['post_known'] and state['comments_known']):
                return False
            if self.pending(row['pid']):
                return False
        return True

    def save(self, pid, payload):
        key = payload.get('media_key')
        if not self.download or not self.db.execute(
                'SELECT 1 FROM media_refs WHERE pid=? AND media_key=?', (pid, key)).fetchone():
            raise CliError('Unrequested image download.')
        status = payload.get('status', 'downloaded')
        relative = mime = digest = None
        size = 0
        if status == 'downloaded':
            encoded = payload.get('data', '')
            if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                raise CliError('Image exceeds the 20 MiB limit.')
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as error:
                raise CliError('Invalid image encoding.') from error
            extension = image_extension(data)
            if not extension:
                status = 'not_image'
            else:
                digest = hashlib.sha256(data).hexdigest()
                relative = digest + extension
                size = len(data)
                mime = str(payload.get('mime', ''))[:128]
                self.directory.mkdir(parents=True, exist_ok=True)
                target = self.directory / relative
                temporary = target.with_suffix(target.suffix + '.part')
                try:
                    with temporary.open('wb') as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    temporary.chmod(0o600)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        elif status not in ('unavailable', 'not_image'):
            raise CliError('Invalid image download status.')
        with self.archive.transaction():
            self.db.execute('''INSERT INTO media_files VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(media_key) DO UPDATE SET path=excluded.path,mime=excluded.mime,
                bytes=excluded.bytes,sha256=excluded.sha256,status=excluded.status,
                observed_at=excluded.observed_at''',
                (key, relative, mime, size, digest, status, int(time.time())))
        self.reserved.discard(key)
        receipt = self.plan_receipts.get(pid)
        if receipt and all(row['media_key'] not in self.reserved for row in receipt[1]):
            del self.plan_receipts[pid]

    def summary(self, run_id):
        rows = self.db.execute('''SELECT DISTINCT r.media_key,f.path,f.bytes,f.status
            FROM media_refs r JOIN archive_members m USING(pid)
            LEFT JOIN media_files f USING(media_key) WHERE m.run_id=?''', (run_id,)).fetchall()
        downloaded = [r for r in rows if r['status'] == 'downloaded' and
                      (self.directory / r['path']).is_file() and
                      (self.directory / r['path']).stat().st_size == r['bytes']]
        scans = self.db.execute("""SELECT m.pid,s.post_complete,s.comments_complete,s.reply_count,m.reply,s.unavailable
            FROM archive_members m LEFT JOIN media_scans s USING(pid) WHERE m.run_id=?""", (run_id,)).fetchall()
        return dict(extraction_complete_posts=sum(bool(r['post_complete'] and r['comments_complete'] and r['reply_count'] == r['reply']) for r in scans),
                    unavailable_posts=[dict(pid=r['pid'], reason=r['unavailable']) for r in scans if r['unavailable']],
                    directory=str(self.directory), images=len(rows), downloaded=len(downloaded),
                    files=len({r['path'] for r in downloaded}), bytes=sum({r['path']: r['bytes'] for r in downloaded}.values()),
                    unavailable=sum(r['status'] == 'unavailable' for r in rows),
                    not_image=sum(r['status'] == 'not_image' for r in rows),
                    pending=sum(r['status'] not in ('unavailable', 'not_image') for r in rows) - len(downloaded))
