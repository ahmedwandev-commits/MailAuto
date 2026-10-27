"""The read caches of calllog/ops/store.OpsStore and calllog/storage.Store: a read that started before this process
wrote a file and ends after it must never put the old content back in the cache - two managers marking mails done at
the same moment once saw one mark come back undone (test_notifications' concurrency test, on a busy machine)."""
import json

from calllog.ops.store import OpsStore
from calllog.storage import Store


def racing_load(monkeypatch, module, write):
    """``json.load`` that, the first time, lets ``write`` land while the reader holds what it read."""
    real, fired = json.load, []

    def load(f, *a, **kw):
        data = real(f, *a, **kw)
        if not fired:
            fired.append(1)
            write()
        return data
    monkeypatch.setattr(module.json, "load", load)


def test_the_ops_store_never_caches_what_a_write_replaced_while_it_was_read(tmp_path, monkeypatch):
    from calllog.ops import store as module
    store = OpsStore(tmp_path / "data").init()
    store.set_override("assignments", "<m1@x>", {"to": "a", "done_at": ""})
    racing_load(monkeypatch, module, lambda: store.set_override("assignments", "<m1@x>", {"to": "a", "done_at": "now"}))
    assert store.overrides("assignments")["<m1@x>"]["done_at"] == "now"       # read again after the write
    assert store.overrides("assignments")["<m1@x>"]["done_at"] == "now"       # and that is what stays cached


def test_the_call_log_store_never_caches_what_a_write_replaced_while_it_was_read(tmp_path, monkeypatch):
    from calllog import storage as module
    store = Store(str(tmp_path))
    path = tmp_path / "probe.json"
    store._write_json(path, {"v": 1})
    racing_load(monkeypatch, module, lambda: store._write_json(path, {"v": 2}))
    assert store._read_json(path, {}) == {"v": 2}
    assert store._read_json(path, {}, ttl=60) == {"v": 2}
