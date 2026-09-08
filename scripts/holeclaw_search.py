"""Optional trigram indexes, preserving exact, case-sensitive substring search."""
import sqlite3


def install_search_indexes(archive):
    try:
        with archive.transaction():
            for table in ('posts', 'comments'):
                index = table + '_search'
                if archive.connection.execute(
                        'SELECT 1 FROM sqlite_master WHERE name=?', (index,)).fetchone():
                    continue
                archive.connection.execute(f'''CREATE VIRTUAL TABLE {index} USING fts5(
                    text, content='{table}', content_rowid='rowid', tokenize='trigram case_sensitive 1')''')
                archive.connection.execute(f'''CREATE TRIGGER {index}_insert AFTER INSERT ON {table} BEGIN
                    INSERT INTO {index}(rowid,text) VALUES(new.rowid,new.text); END''')
                archive.connection.execute(f'''CREATE TRIGGER {index}_delete AFTER DELETE ON {table} BEGIN
                    INSERT INTO {index}({index},rowid,text) VALUES('delete',old.rowid,old.text); END''')
                archive.connection.execute(f'''CREATE TRIGGER {index}_update AFTER UPDATE OF text ON {table}
                    WHEN old.text IS NOT new.text BEGIN
                    INSERT INTO {index}({index},rowid,text) VALUES('delete',old.rowid,old.text);
                    INSERT INTO {index}(rowid,text) VALUES(new.rowid,new.text); END''')
                archive.connection.execute(f"INSERT INTO {index}({index}) VALUES('rebuild')")
    except sqlite3.OperationalError as error:
        if 'no such module' not in str(error) and 'no such tokenizer' not in str(error):
            raise
        # Older SQLite builds retain the existing substring search path.


def search_rows(db, query, limit):
    indexed = len(query) >= 3 and '\x00' not in query and db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('posts_search','comments_search')"
    ).fetchone()[0] == 2
    phrase = '"' + query.replace('"', '""') + '"'
    branches, parameters = [], []
    for table, kind, cid in (('posts', 'post', 'NULL'), ('comments', 'comment', 'cid')):
        condition = ''
        if indexed:
            condition = f'rowid IN (SELECT rowid FROM {table}_search WHERE {table}_search MATCH ?) AND '
            parameters.append(phrase)
        branches.append(f"SELECT '{kind}' AS kind,pid,{cid} AS cid,timestamp,text FROM {table} "
                        f'WHERE {condition}instr(text,?)>0')
        parameters.append(query)
    parameters.append(limit)
    return db.execute(' UNION ALL '.join(branches) + ' ORDER BY timestamp DESC LIMIT ?', parameters).fetchall()
