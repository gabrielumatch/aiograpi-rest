import json

import pytest
from tinydb import Query

from aiograpi_rest.storages import ClientStorage


class FakeClient:
    def __init__(self):
        self.sessionid = "sid"
        self.settings = {"authorization_data": {"sessionid": self.sessionid}}
        self.proxy = None
        self.timeline_called = False

    def set_settings(self, settings):
        self.settings = settings
        return True

    def set_proxy(self, proxy):
        self.proxy = proxy
        return True

    def get_settings(self):
        return self.settings

    async def get_timeline_feed(self):
        self.timeline_called = True
        return {"ok": True}


class FakeClientWithPrivateProxy(FakeClient):
    class Private:
        proxy = "http://private-proxy.example:8080"

    def __init__(self):
        super().__init__()
        self.proxy = "http://public-proxy.example:8080"
        self.private = self.Private()


@pytest.mark.asyncio
async def test_get_restores_settings_and_validates_timeline(tmp_path, monkeypatch):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    storage.db.insert({"sessionid": "sid", "settings": json.dumps({"x": 1})})

    client = await storage.get("sid")

    assert client.settings == {"x": 1}
    assert client.timeline_called is True


@pytest.mark.asyncio
async def test_get_can_skip_timeline_warmup(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    storage.db.insert({"sessionid": "sid", "settings": json.dumps({"x": 1}), "proxy": "http://old.example:8080"})

    client = await storage.get("sid", warm_timeline=False)

    assert client.settings == {"x": 1}
    assert client.proxy == "http://old.example:8080"
    assert client.timeline_called is False


def test_set_persists_client_settings(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    assert storage.set(FakeClient()) is True
    row = storage.db.all()[0]
    assert row["sessionid"] == "sid"
    assert json.loads(row["settings"]) == {"authorization_data": {"sessionid": "sid"}}


def test_set_replaces_existing_settings_and_persists_proxy(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    original = FakeClient()
    assert storage.set(original) is True

    updated = FakeClient()
    updated.settings = {"authorization_data": {"sessionid": "sid"}, "locale": "en_US"}
    updated.proxy = "http://proxy.example:8080"
    assert storage.set(updated) is True

    rows = storage.db.search(Query().sessionid == "sid")
    assert len(rows) == 1
    assert json.loads(rows[0]["settings"]) == {"authorization_data": {"sessionid": "sid"}, "locale": "en_US"}
    assert rows[0]["proxy"] == "http://proxy.example:8080"


def test_set_prefers_private_transport_proxy(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClientWithPrivateProxy)

    assert storage.set(FakeClientWithPrivateProxy()) is True

    row = storage.db.all()[0]
    assert row["proxy"] == "http://private-proxy.example:8080"


@pytest.mark.asyncio
async def test_get_restores_persisted_proxy(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    storage.db.insert(
        {
            "sessionid": "sid",
            "settings": json.dumps({"authorization_data": {"sessionid": "sid"}}),
            "proxy": "http://proxy.example:8080",
        }
    )

    client = await storage.get("sid")

    assert client.proxy == "http://proxy.example:8080"
    assert client.timeline_called is True


def test_storage_path_can_come_from_environment(tmp_path, monkeypatch):
    db_path = tmp_path / "env-db.json"
    monkeypatch.setenv("AIOGRAPI_REST_DB_PATH", str(db_path))

    storage = ClientStorage(client_factory=FakeClient)
    storage.db.insert({"sessionid": "sid", "settings": "{}"})

    assert db_path.exists()


@pytest.mark.asyncio
async def test_get_missing_session_raises_helpful_error(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    with pytest.raises(Exception, match="Session not found"):
        await storage.get("missing")


def test_client_factory_produces_configured_client(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    cl = storage.client()
    assert isinstance(cl, FakeClient)
    assert cl.request_timeout == 0.1


def test_close_is_a_no_op(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    assert storage.close() is None


@pytest.mark.asyncio
async def test_timeline_on_get_always_calls_every_get(tmp_path, monkeypatch):
    monkeypatch.delenv("AIOGRAPI_REST_TIMELINE_ON_GET", raising=False)
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    storage.db.insert({"sessionid": "sid", "settings": json.dumps({"x": 1})})

    first = await storage.get("sid")
    second = await storage.get("sid")

    assert first.timeline_called is True
    assert second.timeline_called is True


@pytest.mark.asyncio
async def test_timeline_on_get_never_skips_warmup(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOGRAPI_REST_TIMELINE_ON_GET", "never")
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    storage.db.insert({"sessionid": "sid", "settings": json.dumps({"x": 1})})

    client = await storage.get("sid")

    assert client.timeline_called is False


@pytest.mark.asyncio
async def test_timeline_on_get_interval_throttles_per_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOGRAPI_REST_TIMELINE_ON_GET", "interval")
    monkeypatch.setenv("AIOGRAPI_REST_TIMELINE_INTERVAL_SECONDS", "60")
    db_path = tmp_path / "db.json"
    ClientStorage(db_path=db_path, client_factory=FakeClient).db.insert(
        {"sessionid": "sid", "settings": json.dumps({"x": 1})}
    )
    now = [100.0]
    monkeypatch.setattr("aiograpi_rest.storages.time.time", lambda: now[0])

    # A new storage per request, as get_clients() does: the last warmup lives on the row.
    first = await ClientStorage(db_path=db_path, client_factory=FakeClient).get("sid")
    now[0] = 130.0
    second = await ClientStorage(db_path=db_path, client_factory=FakeClient).get("sid")
    now[0] = 200.0
    third = await ClientStorage(db_path=db_path, client_factory=FakeClient).get("sid")

    assert first.timeline_called is True
    assert second.timeline_called is False
    assert third.timeline_called is True
    row = ClientStorage(db_path=db_path, client_factory=FakeClient).db.all()[0]
    assert row["timeline_at"] == 200.0


@pytest.mark.asyncio
async def test_timeline_interval_warms_again_after_clock_goes_back(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOGRAPI_REST_TIMELINE_ON_GET", "interval")
    monkeypatch.setenv("AIOGRAPI_REST_TIMELINE_INTERVAL_SECONDS", "60")
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=FakeClient)
    storage.db.insert({"sessionid": "sid", "settings": json.dumps({"x": 1}), "timeline_at": 500.0})
    monkeypatch.setattr("aiograpi_rest.storages.time.time", lambda: 100.0)

    client = await storage.get("sid")

    assert client.timeline_called is True


class RefreshingClient(FakeClient):
    """Instagram renewed the session during the request (new token, new mid)."""

    def __init__(self):
        super().__init__()
        self.sessionid = "123:token:1:sig"

    def set_settings(self, settings):
        self.settings = dict(settings)
        return True

    def refresh(self, **fields):
        self.settings = {**self.settings, **fields}


def _session_row(sessionid="123:token:1:sig", **settings):
    base = {"cookies": {"sessionid": sessionid}, "mid": "old-mid"}
    return {"sessionid": sessionid, "settings": json.dumps({**base, **settings}), "proxy": "http://p.example:8080"}


@pytest.mark.asyncio
async def test_persist_changes_saves_what_instagram_refreshed(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=RefreshingClient)
    storage.db.insert(_session_row())

    cl = await storage.get("123:token:1:sig", warm_timeline=False)
    cl.refresh(mid="new-mid", authorization_data={"sessionid": "123:token:1:sig2"})

    assert storage.persist_changes() == 1
    row = storage.db.all()[0]
    saved = json.loads(row["settings"])
    assert saved["mid"] == "new-mid"
    assert saved["authorization_data"] == {"sessionid": "123:token:1:sig2"}
    assert row["proxy"] == "http://p.example:8080"
    assert storage.persist_changes() == 0  # handed clients are forgotten once saved


@pytest.mark.asyncio
async def test_persist_changes_keeps_the_row_when_nothing_changed(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=RefreshingClient)
    storage.db.insert(_session_row())

    await storage.get("123:token:1:sig", warm_timeline=False)

    assert storage.persist_changes() == 0


@pytest.mark.asyncio
async def test_persist_changes_follows_a_resigned_sessionid(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=RefreshingClient)
    storage.db.insert(_session_row())

    cl = await storage.get("123:token:1:sig", warm_timeline=False)
    cl.sessionid = "123%3Atoken%3A1%3Anewsig"  # the same session, signed again
    cl.refresh(cookies={"sessionid": "123:token:1:newsig"})

    assert storage.persist_changes() == 1
    assert storage.db.all()[0]["sessionid"] == "123:token:1:sig"  # still found by its key


@pytest.mark.asyncio
async def test_persist_changes_never_overwrites_with_another_session(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=RefreshingClient)
    storage.db.insert(_session_row())

    cl = await storage.get("123:token:1:sig", warm_timeline=False)
    cl.sessionid = "999:other:1:sig"
    cl.refresh(mid="new-mid")

    assert storage.persist_changes() == 0
    assert json.loads(storage.db.all()[0]["settings"])["mid"] == "old-mid"


@pytest.mark.asyncio
async def test_persist_changes_keeps_the_stored_session_cookie(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=RefreshingClient)
    storage.db.insert(_session_row())

    cl = await storage.get("123:token:1:sig", warm_timeline=False)
    cl.refresh(cookies={}, mid="new-mid")  # e.g. a login_required response cleared the jar

    assert storage.persist_changes() == 0
    assert json.loads(storage.db.all()[0]["settings"])["cookies"] == {"sessionid": "123:token:1:sig"}


@pytest.mark.asyncio
async def test_persist_changes_skips_a_deleted_session_and_survives_errors(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=RefreshingClient)
    storage.db.insert(_session_row())
    cl = await storage.get("123:token:1:sig", warm_timeline=False)
    storage.db.truncate()
    cl.refresh(mid="new-mid")
    assert storage.persist_changes() == 0

    storage.db.insert(_session_row())
    broken = await storage.get("123:token:1:sig", warm_timeline=False)

    def boom():
        raise RuntimeError("no settings")

    broken.get_settings = boom
    assert storage.persist_changes() == 0


def test_get_clients_dependency_yields_storage(monkeypatch, tmp_path):
    import aiograpi_rest.storages as storages
    from aiograpi_rest.dependencies import get_clients

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(storages, "Client", FakeClient)

    gen = get_clients()
    storage = next(gen)
    try:
        assert isinstance(storage, storages.ClientStorage)
    finally:
        with pytest.raises(StopIteration):
            next(gen)


def test_session_user_id():
    assert ClientStorage._session_user_id("123%3Atoken%3A1%3Asig") == "123"
    assert ClientStorage._session_user_id(None) == ""


@pytest.mark.asyncio
async def test_persist_changes_keeps_a_renewed_session_of_the_same_user(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=RefreshingClient)
    storage.db.insert(_session_row())

    cl = await storage.get("123:token:1:sig", warm_timeline=False)
    cl.sessionid = "123:newtoken:1:sig"  # Instagram handed this device a new token
    cl.refresh(cookies={"sessionid": "123:newtoken:1:sig"})

    assert storage.persist_changes() == 1
    row = storage.db.all()[0]
    assert row["sessionid"] == "123:token:1:sig"
    assert json.loads(row["settings"])["cookies"] == {"sessionid": "123:newtoken:1:sig"}


@pytest.mark.asyncio
async def test_persist_changes_skips_a_client_without_session(tmp_path):
    storage = ClientStorage(db_path=tmp_path / "db.json", client_factory=RefreshingClient)
    storage.db.insert(_session_row())

    cl = await storage.get("123:token:1:sig", warm_timeline=False)
    cl.sessionid = None  # logged out
    cl.refresh(mid="new-mid")

    assert storage.persist_changes() == 0


def test_timeline_interval_falls_back_on_a_bad_value(monkeypatch):
    monkeypatch.setenv("AIOGRAPI_REST_TIMELINE_INTERVAL_SECONDS", "soon")
    assert ClientStorage._timeline_interval_seconds() == 1800


def test_atomic_storage_replaces_the_file_and_reads_by_path(tmp_path):
    from aiograpi_rest.storages import AtomicJSONStorage

    path = tmp_path / "db.json"
    storage = AtomicJSONStorage(path)
    assert storage.read() is None  # no file yet
    path.write_text("  ")
    assert storage.read() is None  # empty file

    storage.write({"_default": {"1": {"sessionid": "a"}}})
    other = AtomicJSONStorage(path)
    other.write({"_default": {"1": {"sessionid": "b"}}})

    assert storage.read() == {"_default": {"1": {"sessionid": "b"}}}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["db.json"]


def test_two_storages_on_one_file_see_each_other(tmp_path):
    path = tmp_path / "db.json"
    first = ClientStorage(db_path=path, client_factory=FakeClient)
    second = ClientStorage(db_path=path, client_factory=FakeClient)

    first.db.insert({"sessionid": "a", "settings": "{}"})
    second.db.insert({"sessionid": "b", "settings": "{}"})

    assert sorted(r["sessionid"] for r in first.db.all()) == ["a", "b"]
