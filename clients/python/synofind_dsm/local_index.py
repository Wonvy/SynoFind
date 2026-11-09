"""Persistent, read-only folder catalogue. No elevation or file-content indexing."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid

from .client import DSMError
from .local_paths import validate_local_profile, resolve_local_path, open_local_content

PREFIX = '/@local/'
SKIP = {'@eadir', '#recycle', '$recycle.bin', 'system volume information'}


class LocalIndex:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'local-index.sqlite3'
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.progress = {}
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, root TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, mode TEXT NOT NULL, executable TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, source TEXT NOT NULL,
                    rel TEXT NOT NULL, parent TEXT NOT NULL, name TEXT NOT NULL,
                    folded TEXT NOT NULL, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
                    isdir INTEGER NOT NULL, UNIQUE(source,rel));
                CREATE INDEX IF NOT EXISTS files_parent ON files(source,parent);
                CREATE VIRTUAL TABLE IF NOT EXISTS names USING fts5(folded, content='files',
                    content_rowid='id', tokenize='trigram');
                CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
                    INSERT INTO names(rowid,folded) VALUES(new.id,new.folded); END;
                CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
                    INSERT INTO names(names,rowid,folded) VALUES('delete',old.id,old.folded); END;
                CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
                    INSERT INTO names(names,rowid,folded) VALUES('delete',old.id,old.folded);
                    INSERT INTO names(rowid,folded) VALUES(new.id,new.folded); END;
            ''')
        self.worker = threading.Thread(target=self._run, daemon=True, name='local-catalogue')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start(self):
        self.worker.start()
        self.wake.set()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.worker.is_alive():
            self.worker.join(timeout=5)

    def _run(self):
        while not self.stop_event.is_set():
            self.wake.wait(900)
            self.wake.clear()
            if self.stop_event.is_set():
                return
            try:
                self.scan()
            except (OSError, sqlite3.Error):
                with self.lock:
                    self.progress['error'] = '本机索引更新未完成，请稍后刷新。'

    def sources(self):
        with self.db() as db:
            rows = db.execute('SELECT s.*, (SELECT count(*) FROM files f WHERE f.source=s.id) count FROM sources s ORDER BY s.name').fetchall()
        with self.lock:
            return [dict(row, **self.progress.get(row['id'], {})) for row in rows]

    def add(self, root, mode='web', executable=''):
        if not isinstance(root, str) or not root.strip():
            raise DSMError('请输入要收录的本机文件夹完整路径。')
        root = str(Path(root.strip()).resolve()) if Path(root.strip()).is_absolute() else root.strip()
        validate_local_profile({'local_root': root, 'preview_mode': mode, 'preview_executable': executable})
        with self.db() as db:
            existing = db.execute('SELECT id FROM sources WHERE root=? COLLATE NOCASE', (root,)).fetchone()
            source_id = existing['id'] if existing else uuid.uuid4().hex
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,executable=excluded.executable',
                       (source_id, root, Path(root).name or root, mode, executable))
        self.wake.set()
        return source_id

    def remove(self, source_id):
        with self.lock, self.db() as db:
            db.execute('DELETE FROM files WHERE source=?', (source_id,))
            db.execute('DELETE FROM sources WHERE id=?', (source_id,))
            self.progress.pop(source_id, None)

    def profile(self, path):
        parts = path.split('/')
        if len(parts) < 4 or parts[:2] != ['', '@local']:
            raise DSMError('本机文件地址不正确。')
        with self.db() as db:
            row = db.execute('SELECT * FROM sources WHERE id=?', (parts[2],)).fetchone()
        if row is None:
            raise DSMError('此本机数据源已移除，请重新搜索。')
        return {'root': PREFIX + row['id'], 'local_root': row['root'],
                'preview_mode': row['mode'], 'preview_executable': row['executable']}

    def info(self, path):
        profile = self.profile(path)
        candidate, _ = resolve_local_path(profile, path)
        value = candidate.stat()
        return {'path': path, 'name': candidate.name, 'isdir': candidate.is_dir(),
                'source_kind': 'local', 'display_path': str(candidate), 'smb_path': str(candidate),
                'preview_mode': profile['preview_mode'],
                'additional': {'size': value.st_size, 'time': {'mtime': int(value.st_mtime)}}}

    @contextmanager
    def content(self, path):
        profile = self.profile(path)
        with open_local_content(profile, path) as response:
            yield self.info(path), response

    def scan(self):
        if not self.scan_lock.acquire(blocking=False):
            return
        try:
            for source in self.sources():
                if self.stop_event.is_set():
                    break
                sid, root = source['id'], Path(source['root'])
                started, errors, visited = time.monotonic(), 0, 0
                with self.lock:
                    self.progress[sid] = {'scanning': True, 'errors': 0, 'visited': 0}
                stack = [root]
                while stack and not self.stop_event.is_set():
                    folder = stack.pop()
                    relparent = folder.relative_to(root).as_posix()
                    rows, children, seen = [], [], set()
                    try:
                        # A directory may have been replaced by a link since enumeration.
                        if not folder.resolve(strict=True).is_relative_to(root):
                            raise OSError('Directory boundary changed')
                        if folder != root and getattr(folder.lstat(), 'st_file_attributes', 0) & 0x400:
                            raise OSError('Directory became a reparse point')
                        with os.scandir(folder) as entries:
                            for entry in entries:
                                if self.stop_event.is_set():
                                    break
                                rel = (folder / entry.name).relative_to(root).as_posix()
                                if entry.name.casefold() in SKIP:
                                    continue
                                try:
                                    info = entry.stat(follow_symlinks=False)
                                    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                                        continue
                                    directory = stat.S_ISDIR(info.st_mode)
                                    if not directory and not stat.S_ISREG(info.st_mode):
                                        continue
                                    seen.add(rel)
                                    rows.append((sid, rel, relparent, entry.name, rel.casefold(), info.st_size, int(info.st_mtime), int(directory)))
                                    if directory:
                                        children.append(Path(entry.path))
                                except OSError:
                                    seen.add(rel)  # An unreadable child is not a deletion.
                                    errors += 1
                    except OSError:
                        errors += 1
                        continue  # Preserve the entire unreadable subtree.
                    if self.stop_event.is_set():
                        break
                    with self.lock, self.db() as db:
                        if not db.execute('SELECT 1 FROM sources WHERE id=?', (sid,)).fetchone():
                            break
                        old = db.execute('SELECT rel,isdir FROM files WHERE source=? AND parent=?', (sid, relparent)).fetchall()
                        new_files = {row[1] for row in rows if not row[7]}
                        for row in old:
                            if row['rel'] not in seen or (row['isdir'] and row['rel'] in new_files):
                                # Prefix comparison is literal, including %, _ and [ in names.
                                prefix = row['rel'] + '/'
                                db.execute('DELETE FROM files WHERE source=? AND (rel=? OR substr(rel,1,?)=?)', (sid, row['rel'], len(prefix), prefix))
                        db.executemany('''INSERT INTO files(source,rel,parent,name,folded,size,mtime,isdir) VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(source,rel) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,isdir=excluded.isdir
                            WHERE size!=excluded.size OR mtime!=excluded.mtime OR isdir!=excluded.isdir''', rows)
                        visited += len(rows)
                        self.progress[sid] = {'scanning': True, 'visited': visited, 'errors': errors}
                    stack.extend(children)
                with self.lock:
                    if sid in self.progress:
                        self.progress[sid] = {'scanning': False, 'visited': visited, 'errors': errors,
                                              'seconds': round(time.monotonic()-started, 2), 'updated': int(time.time())}
        finally:
            self.scan_lock.release()

    def search(self, params, source_id=None):
        terms = str(params.get('pattern', '')).casefold().split()
        where, args = [], []
        if source_id:
            where.append('f.source=?'); args.append(source_id)
        # FTS narrows candidates; LIKE applies exact substring/wildcard semantics.
        literals = [part for term in terms for part in re.split(r'[*?]', term) if len(part) >= 3]
        if literals:
            where.append('f.id IN (SELECT rowid FROM names WHERE names MATCH ?)')
            args.append(' AND '.join('"' + part.replace('"', '""') + '"' for part in literals))
        for term in terms:
            pattern = ''.join('%' if c == '*' else '_' if c == '?' else '\\'+c if c in '%_\\' else c for c in term)
            where.append("f.folded LIKE ? ESCAPE '\\'"); args.append('%'+pattern+'%')
        extensions = [x.lower().lstrip('.') for x in (params.get('extension') or '').split(',') if x]
        if extensions:
            where.append('('+' OR '.join('lower(f.name) LIKE ?' for _ in extensions)+')')
            args.extend('%.'+ext for ext in extensions)
        for key, column, operator in [('min_size','size','>='), ('max_size','size','<='), ('modified_after','mtime','>='), ('modified_before','mtime','<=')]:
            if key in params:
                where.append(f'f.{column}{operator}?'); args.append(params[key])
        clause = ' WHERE '+' AND '.join(where) if where else ''
        order = {'name':'f.name COLLATE NOCASE', 'size':'f.size', 'mtime':'f.mtime'}.get(params.get('sort_by'), 'f.name COLLATE NOCASE')
        direction = ' DESC' if params.get('descending') else ' ASC'
        with self.db() as db:
            cancel = params.get('cancel_event')
            if cancel is not None:
                db.set_progress_handler(lambda: int(cancel.is_set()), 1000)
            total = db.execute('SELECT count(*) FROM files f JOIN sources s ON s.id=f.source'+clause, args).fetchone()[0]
            rows = db.execute('SELECT f.*,s.root,s.name source_name,s.mode FROM files f JOIN sources s ON s.id=f.source'+clause+
                              ' ORDER BY '+order+direction+',f.rel,f.source LIMIT ? OFFSET ?', args+[params['limit'],params['offset']]).fetchall()
        files = []
        for row in rows:
            actual = str(Path(row['root']).joinpath(*row['rel'].split('/')))
            files.append({'path': PREFIX+row['source']+'/'+row['rel'], 'name':row['name'], 'isdir':bool(row['isdir']),
                          'display_path':actual, 'smb_path':actual, 'source_kind':'local', 'source_name':row['source_name'],
                          'preview_mode':row['mode'], 'additional':{'size':row['size'], 'time':{'mtime':row['mtime']}}})
        return files, total
