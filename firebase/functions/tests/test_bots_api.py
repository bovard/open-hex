"""The bot endpoints of the REST API, against an in-memory Firestore.

Needs Flask (firebase/functions/requirements.txt); skipped without it. The
Firebase and Google Cloud packages are stood in for, so no project or
credentials are needed. Run from the repository root:
    python3 -m unittest discover -s firebase/functions/tests -t firebase/functions
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest import mock

try:
    import flask  # noqa: F401
except ImportError:  # pragma: no cover
    flask = None


# ---------------------------------------------------------------------------
# An in-memory Firestore: the calls main.py's bot endpoints make
# ---------------------------------------------------------------------------

class _Snapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _DocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self.id = doc_id

    def get(self):
        return _Snapshot(self.id, self._store.get(self.id))

    def set(self, data):
        self._store[self.id] = dict(data)

    def update(self, data):
        if self.id not in self._store:
            raise KeyError(self.id)
        self._store[self.id].update(data)


class _Query:
    def __init__(self, store, filters=(), limit=None):
        self._store = store
        self._filters = filters
        self._limit = limit

    def where(self, field, op, value):
        assert op == "=="
        return _Query(self._store, self._filters + ((field, value),), self._limit)

    def limit(self, n):
        return _Query(self._store, self._filters, n)

    def stream(self):
        hits = [
            _Snapshot(doc_id, data) for doc_id, data in self._store.items()
            if all(data.get(f) == v for f, v in self._filters)
        ]
        return iter(hits if self._limit is None else hits[:self._limit])


class _Collection(_Query):
    def document(self, doc_id):
        return _DocRef(self._store, doc_id)


class _FakeDb:
    def __init__(self):
        self.data: dict[str, dict] = {}

    def collection(self, name):
        return _Collection(self.data.setdefault(name, {}))


def _stub_modules() -> dict[str, types.ModuleType]:
    """Stand-ins for the Firebase and Google Cloud packages main.py imports."""
    def module(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        return m

    passthrough = lambda *a, **k: (lambda f: f)  # noqa: E731
    firestore = module(
        "firebase_admin.firestore",
        client=lambda: None,
        Query=types.SimpleNamespace(DESCENDING="DESCENDING", ASCENDING="ASCENDING"),
        transactional=lambda f: f,
    )
    https_fn = module("firebase_functions.https_fn", on_request=passthrough,
                      Request=object, Response=object)
    scheduler_fn = module("firebase_functions.scheduler_fn", on_schedule=passthrough,
                          ScheduledEvent=object)
    options = module("firebase_functions.options",
                     MemoryOption=types.SimpleNamespace(MB_512="512MB"))
    storage = module("google.cloud.storage", Client=object)
    firestore_v1 = module("google.cloud.firestore_v1", FieldFilter=object)
    cloud = module("google.cloud", storage=storage, firestore_v1=firestore_v1)
    return {
        "firebase_admin": module("firebase_admin", initialize_app=lambda *a, **k: None,
                                 firestore=firestore),
        "firebase_admin.firestore": firestore,
        "firebase_functions": module("firebase_functions", https_fn=https_fn,
                                     scheduler_fn=scheduler_fn, options=options),
        "firebase_functions.https_fn": https_fn,
        "firebase_functions.scheduler_fn": scheduler_fn,
        "firebase_functions.options": options,
        "google": module("google", cloud=cloud),
        "google.cloud": cloud,
        "google.cloud.storage": storage,
        "google.cloud.firestore_v1": firestore_v1,
    }


def _import_main():
    names = list(_stub_modules()) + ["main", "services", "services.game_manager",
                                     "services.matchmaker", "services.hidden_info"]
    saved = {n: sys.modules.get(n) for n in names}
    try:
        sys.modules.update(_stub_modules())
        for n in ("main", "services.game_manager", "services.matchmaker"):
            sys.modules.pop(n, None)
        return importlib.import_module("main")
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m


@unittest.skipIf(flask is None, "Flask is not installed")
class UpdateBotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main()

    def setUp(self):
        self.db = _FakeDb()
        patcher = mock.patch.object(self.main, "_get_db", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.main.app.test_client()

    def register(self, name, mode="1v1", **extra):
        r = self.client.post("/api/bots", json={"name": name, "mode": mode, **extra})
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        return body["bot"]["id"], body["api_key"]

    def patch(self, key, body):
        return self.client.patch("/api/bots/me", json=body,
                                 headers={"Authorization": f"Bearer {key}"})

    def test_rename_keeps_id_key_and_rating(self):
        bot_id, key = self.register("old_name", description="first")
        self.db.data["bots"][bot_id].update(elo1v1=1768.5, games1v1=423, wins1v1=290)

        r = self.patch(key, {"name": "new_name"})

        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual(body["id"], bot_id)
        self.assertEqual(body["name"], "new_name")
        self.assertEqual(body["description"], "first")
        self.assertEqual((body["elo_1v1"], body["games_1v1"], body["wins_1v1"]),
                         (1768.5, 423, 290))
        self.assertEqual(self.db.data["bots"][bot_id]["name"], "new_name")
        self.assertEqual(self.db.data["apiKeys"][key]["botName"], "new_name")
        me = self.client.get("/api/bots/me", headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(me.get_json()["name"], "new_name")
        public = self.client.get(f"/api/bots/{bot_id}").get_json()
        self.assertEqual(public["name"], "new_name")

    def test_description_only(self):
        bot_id, key = self.register("my_bot", description="first")
        r = self.patch(key, {"description": "  second  "})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["description"], "second")
        self.assertEqual(self.db.data["bots"][bot_id]["name"], "my_bot")

    def test_name_taken_in_same_mode_refused(self):
        self.register("taken")
        bot_id, key = self.register("mine")
        r = self.patch(key, {"name": "taken"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.db.data["bots"][bot_id]["name"], "mine")

    def test_name_taken_in_other_mode_allowed(self):
        self.register("shared", mode="4p")
        _, key = self.register("mine")
        self.assertEqual(self.patch(key, {"name": "shared"}).status_code, 200)

    def test_same_name_is_not_a_conflict(self):
        _, key = self.register("mine")
        r = self.patch(key, {"name": "mine", "description": "d"})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_registration_still_refuses_a_renamed_bots_name(self):
        _, key = self.register("before")
        self.patch(key, {"name": "after"})
        self.assertEqual(self.client.post("/api/bots", json={"name": "after", "mode": "1v1"}).status_code, 409)
        self.register("before")  # the old name is free again

    def test_bad_requests(self):
        bot_id, key = self.register("mine")
        cases = [
            {},
            {"mode": "4p"},
            {"name": "x", "coordinateFormat": "indices"},
            {"name": "ab"},
            {"name": "x" * 33},
            {"name": 12345},
            {"description": "x" * 281},
            {"description": None},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.patch(key, body).status_code, 400)
        r = self.client.patch("/api/bots/me", data="[1, 2]", content_type="application/json",
                              headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.db.data["bots"][bot_id]["name"], "mine")

    def test_needs_a_valid_key(self):
        self.register("mine")
        self.assertEqual(self.client.patch("/api/bots/me", json={"name": "new"}).status_code, 401)
        self.assertEqual(self.patch("not-a-key", {"name": "new"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
