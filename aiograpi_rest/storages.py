import json
import os
import time
from urllib import parse

from aiograpi import Client
from tinydb import Query, TinyDB
from tinydb.storages import Storage


class AtomicJSONStorage(Storage):
    """TinyDB storage that never leaves a half-written file.

    TinyDB's default storage rewrites the file in place through a handle it keeps open, so a
    crash in the middle of a write corrupts every session. Here each write goes to a temporary
    file in the same directory, is flushed to disk and then renamed over the old one, and each
    read opens the file by path (a kept handle would still point at the replaced file).
    """

    def __init__(self, path):
        self.path = os.fspath(path)

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            return None
        return json.loads(text) if text.strip() else None

    def write(self, data) -> None:
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)


class ClientStorage:
    def __init__(self, db_path=None, client_factory=Client):
        db_path = db_path or os.getenv("AIOGRAPI_REST_DB_PATH", "./db.json")
        self.db = TinyDB(db_path, storage=AtomicJSONStorage)
        self.client_factory = client_factory
        # Clients handed out by get(), by stored key: persist_changes() saves what Instagram
        # refreshed on them during the request.
        self._handed: dict[str, object] = {}

    def client(self):
        """Get new client (helper)
        """
        cl = self.client_factory()
        cl.request_timeout = 0.1
        return cl

    @staticmethod
    def _clean_sessionid(sessionid: str) -> str:
        return parse.unquote(sessionid.strip(" \""))

    @staticmethod
    def _client_proxy(cl: Client):
        private = getattr(cl, "private", None)
        if private is not None:
            return getattr(private, "proxy", None)
        return getattr(cl, "proxy", None)

    @staticmethod
    def _timeline_on_get_mode() -> str:
        return (os.getenv("AIOGRAPI_REST_TIMELINE_ON_GET") or "always").strip().lower()

    @staticmethod
    def _timeline_interval_seconds() -> int:
        raw = os.getenv("AIOGRAPI_REST_TIMELINE_INTERVAL_SECONDS", "1800")
        try:
            return max(0, int(raw))
        except ValueError:
            return 1800

    async def _maybe_get_timeline_feed(self, session_key: str, cl: Client, row: dict) -> None:
        mode = self._timeline_on_get_mode()
        if mode == "never":
            return
        if mode == "interval":
            # Kept on the session's row: a ClientStorage lives for one request only.
            now = time.time()
            last = row.get("timeline_at")
            interval = self._timeline_interval_seconds()
            if isinstance(last, (int, float)) and 0 <= (now - last) < interval:
                return
            await cl.get_timeline_feed()
            self.db.update({"timeline_at": now}, Query().sessionid == session_key)
            return
        await cl.get_timeline_feed()

    async def get(self, sessionid: str, warm_timeline: bool = True) -> Client:
        """Get client settings

        ``warm_timeline=False`` restores the client without touching Instagram. Callers that
        are about to replace the proxy need this: the stored proxy may be dead, and warming
        the timeline through it would fail before the new proxy can be applied.
        """
        key = self._clean_sessionid(sessionid)
        rows = self.db.search(Query().sessionid == key)
        if not rows:
            raise Exception('Session not found (e.g. after reload process), please relogin')
        row = rows[0]
        settings = json.loads(row['settings'])
        cl = self.client_factory()
        cl.set_settings(settings)
        if row.get("proxy"):
            cl.set_proxy(row["proxy"])
        if warm_timeline:
            await self._maybe_get_timeline_feed(key, cl, row)
        self._handed[key] = cl
        return cl

    @staticmethod
    def _session_user_id(value) -> str:
        """The user id a sessionid (`<user id>:<token>:<n>:<signature>`) belongs to."""
        return parse.unquote(str(value or "").strip(" \"")).split(":")[0]

    def persist_changes(self) -> int:
        """Save what Instagram refreshed on the clients handed out by get().

        Responses renew the session as the app goes (``ig-set-authorization``, ``ig-set-x-mid``,
        cookies); the client applies them in memory. Without this, every request would restore
        the settings saved at login and send that state again, however old. Saved under the key
        the client was restored from (the caller keeps sending it), only while the client still
        holds a session of the same user: a re-signed or renewed sessionid is kept, a cleared
        one is not.
        """
        saved = 0
        for key, cl in self._handed.items():
            try:
                rows = self.db.search(Query().sessionid == key)
                if not rows:
                    continue
                stored = json.loads(rows[0]["settings"])
                current = json.loads(json.dumps(cl.get_settings()))
                if current == stored:
                    continue
                user_id = self._session_user_id(getattr(cl, "sessionid", None))
                if not user_id or user_id != self._session_user_id(key):
                    continue
                stored_cookies = stored.get("cookies") if isinstance(stored, dict) else None
                current_cookies = current.get("cookies") if isinstance(current, dict) else None
                if (stored_cookies or {}).get("sessionid") and not (current_cookies or {}).get("sessionid"):
                    continue  # Instagram dropped the session cookie: keep the stored one
                self.db.update({"settings": json.dumps(current)}, Query().sessionid == key)
                saved += 1
            except Exception:  # never fail the request over this
                continue
        self._handed.clear()
        return saved

    def set(self, cl: Client) -> bool:
        """Set client settings
        """
        key = self._clean_sessionid(cl.sessionid)
        self.db.upsert(
            {
                "sessionid": key,
                "settings": json.dumps(cl.get_settings()),
                "proxy": self._client_proxy(cl) or "",
            },
            Query().sessionid == key,
        )
        return True

    def close(self):
        pass
