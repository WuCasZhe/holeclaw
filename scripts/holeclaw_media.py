"""Account-local image references and atomic, content-verified downloads."""
import base64
import binascii
import hashlib
import os
import re
import stat
import time
import tempfile
from contextlib import contextmanager
from pathlib import Path
from collections import OrderedDict, deque

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
        self.plan_receipts = OrderedDict()
        self.pending_queues = OrderedDict()
        with archive.transaction():
            self.db.execute('''CREATE TABLE IF NOT EXISTS media_refs (
                pid TEXT NOT NULL, cid TEXT NOT NULL, media_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL, source_url TEXT NOT NULL,
                PRIMARY KEY(pid,cid,media_key))''')
            self.db.execute('CREATE INDEX IF NOT EXISTS media_refs_key_idx ON media_refs(pid,media_key)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS media_files (
                media_key TEXT PRIMARY KEY, path TEXT, mime TEXT, bytes INTEGER,
                sha256 TEXT, status TEXT NOT NULL, observed_at INTEGER NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS media_scans (
                pid TEXT PRIMARY KEY, post_complete INTEGER NOT NULL DEFAULT 0,
                comments_complete INTEGER NOT NULL DEFAULT 0, reply_count INTEGER)''')

            columns = {row[1] for row in self.db.execute('PRAGMA table_info(media_scans)')}
            if 'unavailable' not in columns:
                self.db.execute('ALTER TABLE media_scans ADD COLUMN unavailable TEXT')

    def unavailable(self, pid, reason, reply=None):
        with self.archive.transaction():
            self.db.execute("""INSERT INTO media_scans(pid,unavailable,reply_count) VALUES(?,?,?)
                ON CONFLICT(pid) DO UPDATE SET unavailable=excluded.unavailable,
                reply_count=excluded.reply_count""", (pid, reason, reply))

    def state(self, pid, reply):
        return self.states([dict(pid=str(pid), reply=reply)])[str(pid)]

    def states(self, posts):
        rows = self.archive.rows_by_pid('media_scans', (post['pid'] for post in posts))
        result = {}
        for post in posts:
            pid = str(post['pid'])
            row = rows.get(pid)
            result[pid] = dict(unavailable=row['unavailable'] if row and
                              (row['reply_count'] is None or row['reply_count'] == post['reply']) else None,
                              post_known=bool(row and row['post_complete'] and not row['unavailable']),
                              comments_known=bool(row and row['comments_complete'] and row['reply_count'] == post['reply']))
        return result

    def references(self, pid, cid, ids, legacy=False, existing=None):
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
        values = []
        for ordinal, key in enumerate(dict.fromkeys(keys)):
            kind, value = key.split(':', 1)
            url = 'https://treehole.pku.edu.cn/chapi/api/v3/media/getMediaBinary?' + kind + '=' + value
            values.append((pid, cid, key, ordinal, url))
        if existing is None:
            existing = [tuple(row) for row in self.db.execute('''SELECT pid,cid,media_key,ordinal,source_url
                FROM media_refs WHERE pid=? AND cid=? ORDER BY ordinal''', (pid, cid))]
        if existing == values:
            return
        self.pending_queues.pop(pid, None)
        self.db.execute('DELETE FROM media_refs WHERE pid=? AND cid=?', (pid, cid))
        self.db.executemany('INSERT INTO media_refs VALUES(?,?,?,?,?)', values)

    def record_post(self, post):
        pid = str(post['pid'])
        with self.archive.transaction():
            self.references(pid, '', post['media_ids'], post.get('type') == 'image')
            self.db.execute('''INSERT INTO media_scans(pid,post_complete) VALUES(?,1)
                ON CONFLICT(pid) DO UPDATE SET post_complete=1,unavailable=NULL''', (pid,))

    def record_comments(self, payload):
        # Called inside the same transaction as text and comment checkpoints.
        pid = str(payload['post']['pid'])
        existing = {}
        ids = [comment['cid'] for comment in payload['comments']]
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset + 500]
            for row in self.db.execute(f'''SELECT pid,cid,media_key,ordinal,source_url FROM media_refs
                    WHERE pid=? AND cid IN ({','.join('?' for _ in batch)}) ORDER BY cid,ordinal''', [pid, *batch]):
                existing.setdefault(row['cid'], []).append(tuple(row))
        for comment in payload['comments']:
            # A repeated CID within one payload reads its just-written state.
            prior = existing.pop(comment['cid'], [])
            self.references(pid, comment['cid'], comment['media_ids'], existing=prior)
            existing[comment['cid']] = None
        self.db.execute('''INSERT INTO media_scans(pid,comments_complete,reply_count) VALUES(?,?,?)
            ON CONFLICT(pid) DO UPDATE SET comments_complete=excluded.comments_complete,
            reply_count=excluded.reply_count''', (pid, int(payload['complete']), payload['post']['reply']))

    def pending(self, pid):
        return self.pending_many([str(pid)])[str(pid)]

    def file_available(self, row, checked):
        if row['status'] != 'downloaded' or not row['path']:
            return False
        key = (row['path'], row['bytes'])
        if key not in checked:
            try:
                info = (self.directory / row['path']).stat()
                checked[key] = stat.S_ISREG(info.st_mode) and info.st_size == row['bytes']
            except OSError:
                checked[key] = False
        return checked[key]

    def pending_many(self, pids):
        pids = list(dict.fromkeys(str(pid) for pid in pids))
        result = {pid: [] for pid in pids}
        if not self.download:
            return result
        checked = {}
        for offset in range(0, len(pids), 500):
            batch = pids[offset:offset + 500]
            for row in self.db.execute(f'''SELECT DISTINCT r.pid,r.media_key,r.source_url,f.path,f.bytes,f.status
                    FROM media_refs r LEFT JOIN media_files f USING(media_key)
                    WHERE r.pid IN ({','.join('?' for _ in batch)})''', batch):
                if row['status'] in ('unavailable', 'not_image') or self.file_available(row, checked):
                    continue
                result[row['pid']].append(dict(media_key=row['media_key'], url=row['source_url']))
        return result

    def plan(self, pid, request_id=None):
        # A lost HTTP response must replay the reservation, not hide its images.
        previous = self.plan_receipts.get(pid)
        if request_id is not None and previous and previous[0] == request_id:
            return previous[1]
        result = (self.next_images(pid) if request_id is not None else
                  [row for row in self.pending(pid) if row['media_key'] not in self.reserved][:20])
        self.reserved.update(row['media_key'] for row in result)
        if request_id is not None:
            self.plan_receipts.pop(pid, None)
            self.plan_receipts[pid] = (request_id, result)
            while len(self.plan_receipts) > 256:
                self.plan_receipts.popitem(last=False)
        return result

    def next_images(self, pid):
        """Bounded keyset queues; refresh file state before reserving each batch."""
        if not self.download:
            return []
        state = self.pending_queues.pop(pid, dict(after='', rows=deque(), exhausted=False))
        self.pending_queues[pid] = state
        while len(self.pending_queues) > 64:
            self.pending_queues.popitem(last=False)
        result, checked = [], {}
        while len(result) < 20:
            if not state['rows']:
                if state['exhausted']:
                    break
                rows = list(self.db.execute('''SELECT DISTINCT media_key,source_url FROM media_refs
                    WHERE pid=? AND media_key>? ORDER BY media_key LIMIT 128''', (pid, state['after'])))
                state['rows'].extend(rows)
                state['exhausted'] = len(rows) < 128
                if rows:
                    state['after'] = rows[-1]['media_key']
                else:
                    break
            batch = list(state['rows'])
            files = {row['media_key']: row for row in self.db.execute(
                f"SELECT * FROM media_files WHERE media_key IN ({','.join('?' for _ in batch)})",
                [row['media_key'] for row in batch])}
            while state['rows'] and len(result) < 20:
                row = state['rows'].popleft()
                file = files.get(row['media_key'])
                if row['media_key'] in self.reserved or (file and
                        (file['status'] in ('unavailable', 'not_image') or self.file_available(file, checked))):
                    continue
                result.append(dict(media_key=row['media_key'], url=row['source_url']))
        if not result:
            self.pending_queues.pop(pid, None)
        return result

    def cancel_plans(self):
        self.reserved.clear()
        self.plan_receipts.clear()
        self.pending_queues.clear()

    def candidates_complete(self, run_id):
        cursor = self.db.execute('SELECT pid,reply FROM archive_candidates WHERE run_id=?', (run_id,))
        while posts := cursor.fetchmany(500):
            states = self.states(posts)
            pending = self.pending_many([post['pid'] for post in posts])
            for post in posts:
                state = states[post['pid']]
                if not state['unavailable'] and not (state['post_known'] and state['comments_known']):
                    return False
                if pending[post['pid']]:
                    return False
        return True

    def validate_download(self, pid, key):
        if not self.download or not self.db.execute(
                'SELECT 1 FROM media_refs WHERE pid=? AND media_key=?', (pid, key)).fetchone():
            raise CliError('Unrequested image download.')

    @contextmanager
    def receive(self, stream, length, mime):
        """Stream/hash/fsync outside the sink lock; only publishing needs it."""
        if type(length) is not int or not 0 <= length <= MAX_IMAGE_BYTES:
            raise CliError('Image exceeds the 20 MiB limit.')
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix='.upload-', suffix='.part', delete=False) as output:
                temporary = Path(output.name)
                digest = hashlib.sha256()
                remaining, header = length, b''
                while remaining:
                    chunk = stream.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise CliError('Incomplete image upload.')
                    remaining -= len(chunk)
                    header = (header + chunk[:32])[:32]
                    digest.update(chunk)
                    output.write(chunk)
                extension = image_extension(header)
                if extension:
                    output.flush()
                    os.fsync(output.fileno())
            yield dict(temporary=temporary, digest=digest.hexdigest(), extension=extension,
                       bytes=length, mime=str(mime)[:128])
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save_received(self, pid, key, prepared):
        self.validate_download(pid, key)
        extension, digest = prepared['extension'], prepared['digest']
        relative = digest + extension if extension else None
        if relative:
            prepared['temporary'].replace(self.directory / relative)
        with self.archive.transaction():
            self.db.execute('''INSERT INTO media_files VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(media_key) DO UPDATE SET path=excluded.path,mime=excluded.mime,
                bytes=excluded.bytes,sha256=excluded.sha256,status=excluded.status,
                observed_at=excluded.observed_at''',
                (key, relative, prepared['mime'] if extension else None,
                 prepared['bytes'] if extension else 0, digest if extension else None,
                 'downloaded' if extension else 'not_image', int(time.time())))
        self.release_plan(pid, key)

    def save(self, pid, payload):
        key = payload.get('media_key')
        self.validate_download(pid, key)
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
        self.release_plan(pid, key)

    def release_plan(self, pid, key):
        self.reserved.discard(key)
        receipt = self.plan_receipts.get(pid)
        if receipt and all(row['media_key'] not in self.reserved for row in receipt[1]):
            del self.plan_receipts[pid]

    def summary(self, run_id):
        rows = self.db.execute('''SELECT DISTINCT r.media_key,f.path,f.bytes,f.status
            FROM media_refs r JOIN archive_members m USING(pid)
            LEFT JOIN media_files f USING(media_key) WHERE m.run_id=?''', (run_id,)).fetchall()
        checked = {}
        downloaded = [r for r in rows if self.file_available(r, checked)]
        scans = self.db.execute("""SELECT m.pid,s.post_complete,s.comments_complete,s.reply_count,m.reply,s.unavailable
            FROM archive_members m LEFT JOIN media_scans s USING(pid) WHERE m.run_id=?""", (run_id,)).fetchall()
        return dict(extraction_complete_posts=sum(bool(r['post_complete'] and r['comments_complete'] and r['reply_count'] == r['reply']) for r in scans),
                    unavailable_posts=[dict(pid=r['pid'], reason=r['unavailable']) for r in scans if r['unavailable']],
                    directory=str(self.directory), images=len(rows), downloaded=len(downloaded),
                    files=len({r['path'] for r in downloaded}), bytes=sum({r['path']: r['bytes'] for r in downloaded}.values()),
                    unavailable=sum(r['status'] == 'unavailable' for r in rows),
                    not_image=sum(r['status'] == 'not_image' for r in rows),
                    pending=sum(r['status'] not in ('unavailable', 'not_image') for r in rows) - len(downloaded))
