"""SQLite connection ownership and explicit, nestable transaction boundaries."""
import sqlite3
import threading
from contextlib import contextmanager


class DatabaseSession:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=30,
                                          uri=True, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.depth = 0
        self.serial = 0
        try:
            # Re-enter Python during long SQL operations to deliver Ctrl+C.
            self.connection.set_progress_handler(lambda: 0, 1000)
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=NORMAL')
            self.connection.execute('PRAGMA temp_store=MEMORY')
        except BaseException:
            self.connection.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.serial += 1
            savepoint = f'holeclaw_{self.serial}' if self.depth else None
            self.connection.execute(f'SAVEPOINT {savepoint}' if savepoint else 'BEGIN')
            self.depth += 1
            try:
                yield
                self.connection.execute(f'RELEASE SAVEPOINT {savepoint}' if savepoint else 'COMMIT')
            except BaseException:
                if self.connection.in_transaction:
                    if savepoint:
                        self.connection.execute(f'ROLLBACK TO SAVEPOINT {savepoint}')
                        self.connection.execute(f'RELEASE SAVEPOINT {savepoint}')
                    else:
                        self.connection.rollback()
                raise
            finally:
                self.depth -= 1

    def close(self):
        with self.lock:
            # Closing never commits work that escaped its transaction boundary.
            self.connection.close()
