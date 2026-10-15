"""Small TTL cache backed by SQLite (standard library, no new dependency).

Only successful, complete results should be stored by callers. Expired rows are never returned.
Storage is a file in the server's temp folder, falling back to memory if the file cannot be used.
On Render the disk is temporary: the cache is lost on redeploy/restart, and it is not shared
between servers. It saves API calls; it is not permanent storage.

Environment variables:
  CACHE_TTL_SECONDS  how long a saved answer stays valid (default 86400 = 24h; 0 turns caching off)
  CACHE_PATH         file path, or ':memory:' (default: <temp dir>/rank_agent_cache.sqlite3)"""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time


def _env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def make_key(**parts):
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TTLCache:
    def __init__(self, path=None, ttl=86400, max_rows=2000, clock=time.time):
        self.ttl, self.max_rows, self.clock = ttl, max_rows, clock
        self._lock = threading.Lock()
        self.persistent = False
        self._conn = self._open(path or os.path.join(tempfile.gettempdir(), "rank_agent_cache.sqlite3"))

    def _open(self, path):
        for target in (path, ":memory:"):
            try:
                conn = sqlite3.connect(target, check_same_thread=False)
                conn.execute("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, v TEXT NOT NULL, stored REAL NOT NULL, expires REAL NOT NULL)")
                conn.commit()
                self.persistent = target != ":memory:"
                return conn
            except sqlite3.Error:
                continue
        raise RuntimeError("could not open any cache store")

    def get_entry(self, key):
        """Return {'value', 'stored_at'} for a live entry, else None (expired entries are deleted)."""
        if self.ttl <= 0:
            return None
        now = self.clock()
        with self._lock:
            try:
                row = self._conn.execute("SELECT v, stored, expires FROM c WHERE k=?", (key,)).fetchone()
                if not row:
                    return None
                if row[2] <= now:
                    self._conn.execute("DELETE FROM c WHERE k=?", (key,))
                    self._conn.commit()
                    return None
                return {"value": json.loads(row[0]), "stored_at": row[1]}
            except (sqlite3.Error, ValueError):
                return None

    def get(self, key):
        entry = self.get_entry(key)
        return entry["value"] if entry else None

    def set(self, key, value, ttl=None):
        ttl = self.ttl if ttl is None else ttl
        if ttl <= 0:
            return False
        now = self.clock()
        with self._lock:
            try:
                self._conn.execute("INSERT OR REPLACE INTO c (k, v, stored, expires) VALUES (?,?,?,?)",
                                   (key, json.dumps(value, ensure_ascii=False), now, now + ttl))
                self._conn.execute("DELETE FROM c WHERE expires <= ?", (now,))
                self._conn.execute("DELETE FROM c WHERE k IN (SELECT k FROM c ORDER BY stored DESC LIMIT -1 OFFSET ?)", (self.max_rows,))
                self._conn.commit()
                return True
            except (sqlite3.Error, TypeError, ValueError):
                return False


_default = None
_default_lock = threading.Lock()


def default_cache():
    global _default
    with _default_lock:
        if _default is None:
            _default = TTLCache(os.environ.get("CACHE_PATH") or None, ttl=_env_int("CACHE_TTL_SECONDS", 86400))
        return _default
