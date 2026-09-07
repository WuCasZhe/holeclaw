"""Versioned callback messages and bounded receipts shared by every sink handler."""
import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass

try:
    from holeclaw_domain import CliError, SINK_SCHEMA_VERSION
except ModuleNotFoundError:
    from scripts.holeclaw_domain import CliError, SINK_SCHEMA_VERSION


ENVELOPE_VERSION = 3
ARCHIVE_KINDS = (
    'archive_comments', 'archive_resume', 'archive_prepare', 'archive_source',
    'archive_post_media', 'archive_media_plan', 'archive_media_file', 'archive_media_unavailable',
)
KINDS = (*ARCHIVE_KINDS, 'list_chunk', 'telemetry_final')


@dataclass(frozen=True)
class SinkMessage:
    kind: str
    payload: dict
    request_id: int | str | None
    digest: bytes

    @classmethod
    def decode(cls, raw: dict, run_id: str) -> 'SinkMessage':
        if not isinstance(raw, dict):
            raise CliError('Collector message must be an object.')
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).digest()
        request_id = None
        if raw.get('schema_version') == ENVELOPE_VERSION:
            kind = raw.get('kind')
            request_id = raw.get('request_id')
            if kind not in KINDS or type(request_id) is not int or not 1 <= request_id <= 2**53 - 1:
                raise CliError('Invalid collector message kind or request identity.')
            if raw.get('run_id') != run_id:
                raise CliError('Collector run identity mismatch.')
            body = raw.get('payload')
            if not isinstance(body, dict) or any(key in body for key in
                    (*KINDS, 'schema_version', 'archive_run', 'run_id', 'request_id')):
                raise CliError('Invalid collector message body.')
            payload = dict(body, schema_version=SINK_SCHEMA_VERSION)
            if kind != 'list_chunk':
                payload[kind] = True
            if kind in ARCHIVE_KINDS:
                payload['archive_run'] = run_id
        elif raw.get('schema_version') == SINK_SCHEMA_VERSION:
            flags = [kind for kind in KINDS if raw.get(kind)]
            if len(flags) > 1:
                raise CliError('Collector message has conflicting kinds.')
            kind = flags[0] if flags else 'list_chunk'
            payload = raw
            # Older clients have no request IDs. Exact comment replay is still
            # safe to deduplicate; state queries must always see current cursors.
            if kind == 'archive_comments':
                request_id = 'legacy:' + digest.hex()
        else:
            raise CliError('Collector sink schema mismatch.')
        return cls(kind, payload, request_id, digest)


class ReceiptBook:
    """Keep concurrent responses bounded; expired numbered retries fail closed.

    A sink belongs to one collector process. New processes restart numbering
    with a new sink and resume from durable posts/comment cursors.
    """

    def __init__(self, limit: int = 2048, byte_limit: int = 16 * 1024 * 1024):
        self.limit = limit
        self.byte_limit = byte_limit
        self.entries = OrderedDict()
        self.bytes = 0
        self.expired_through = 0

    def lookup(self, message: SinkMessage) -> tuple[bool, dict | None]:
        key = message.request_id
        if key in self.entries:
            digest, encoded = self.entries[key]
            if digest != message.digest:
                raise CliError('Collector retry changed its payload.')
            return True, json.loads(encoded)
        if type(key) is int and key <= self.expired_through:
            raise CliError('Collector receipt expired; resume from the saved checkpoint.')
        return False, None

    def remember(self, message: SinkMessage, response: dict | None) -> None:
        if message.request_id is None:
            return
        encoded = json.dumps(response, separators=(',', ':')).encode()
        self.entries[message.request_id] = (message.digest, encoded)
        self.bytes += len(encoded)
        while len(self.entries) > self.limit or self.bytes > self.byte_limit:
            key, (_, discarded) = self.entries.popitem(last=False)
            self.bytes -= len(discarded)
            if type(key) is int:
                self.expired_through = max(self.expired_through, key)
