"""Small file layer for the JSON repository: atomic writes, a thread + OS file lock, and a cache that re-checks the file's
signature (mtime, size) on every read so a change made by another server process on the shared folder is seen at once.

Same techniques as OpsHub's ``calllog/storage.py`` (temp file + ``os.replace``, ``msvcrt`` / ``fcntl`` lock) but with no
dependency on it, so the module and its contract tests run on their own."""
import contextlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

try:  # Windows
    import msvcrt
    fcntl = None
except ImportError:  # Linux / macOS
    msvcrt = None
    import fcntl


class FileStoreError(Exception):
    pass


class FileLock:
    """Thread lock + OS file lock. Not re-entrant: the repository's transaction handles nesting."""

    def __init__(self, path):
        self.path = str(path)
        self._thread = threading.Lock()

    @contextlib.contextmanager
    def __call__(self, timeout=30):
        with self._thread:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                self._acquire(fd, timeout)
                try:
                    yield
                finally:
                    self._release(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _acquire(fd, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if msvcrt:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise FileStoreError("Could not lock the attendance data (another server busy?).")
                time.sleep(0.05)

    @staticmethod
    def _release(fd):
        if msvcrt:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _swap_in(tmp, path):
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:      # Windows: a reader has the file open for a moment
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def write_bytes_atomic(path, data: bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    _swap_in(tmp, path)


def write_json_atomic(path, data):
    write_bytes_atomic(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class JsonCache:
    """Parsed JSON files, validated against the file signature (mtime, size). Treat what ``read`` returns as read-only.

    v5.17 (performance): a page used to check the same file on the disk hundreds of times - one network round trip each when the
    program runs from the shared folder (~2 s for the Today page). A file is now checked at most once every ``recheck`` seconds;
    a change written by this program is seen at once (``drop``), one made by another PC within ``recheck`` seconds, and a read
    with ``fresh=True`` (inside a write transaction, under the file lock) always checks the disk."""

    def __init__(self, limit=400, recheck=1.0):
        self._data = {}
        self._checked = {}                                   # path -> (monotonic time of the last disk check, signature or None)
        self._lock = threading.Lock()
        self.limit = limit
        self.recheck = recheck

    def read(self, path, default, fresh=False):
        key = str(path)
        now = time.monotonic()
        if not fresh:
            with self._lock:
                seen = self._checked.get(key)
                if seen and now - seen[0] < self.recheck:
                    if seen[1] is None:                      # missing a moment ago
                        return default
                    hit = self._data.get(key)
                    if hit and hit[0] == seen[1]:
                        return hit[1]
        try:
            st = os.stat(path)
        except FileNotFoundError:
            with self._lock:
                self._data.pop(key, None)
                self._checked[key] = (now, None)
            return default
        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            self._checked[key] = (now, sig)
            hit = self._data.get(key)
            if hit and hit[0] == sig:
                return hit[1]
        try:
            with open(path, encoding="utf-8-sig") as f:      # tolerate a BOM added by Notepad
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise FileStoreError(f"{path} is damaged and cannot be read: {exc}") from exc
        with self._lock:
            if len(self._data) >= self.limit:
                self._data.pop(next(iter(self._data)))
            self._data[key] = (sig, data)
            if len(self._checked) > 4 * self.limit:
                self._checked.clear()
        return data

    def drop(self, path):
        with self._lock:
            self._data.pop(str(path), None)
            self._checked.pop(str(path), None)
