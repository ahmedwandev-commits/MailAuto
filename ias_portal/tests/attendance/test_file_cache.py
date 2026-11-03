"""Performance (v5.17): a data file is checked on the disk at most once a second (one network round trip each on the shared
folder), while this program's own writes and reads inside a write transaction always see the disk."""
import json
import os

from calllog.attendance.repo.jsonfiles import JsonCache


def test_a_file_is_checked_once_per_window_and_a_drop_or_fresh_read_checks_again(tmp_path, monkeypatch):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"v": 1}))
    calls = []
    real = os.stat
    monkeypatch.setattr(os, "stat", lambda f, *a, **k: (calls.append(f), real(f, *a, **k))[1])
    c = JsonCache(recheck=60)
    for _ in range(50):
        assert c.read(p, {}) == {"v": 1}
    assert len(calls) == 1                                        # 50 reads, one disk check
    p.write_text(json.dumps({"v": 22}))
    assert c.read(p, {}) == {"v": 1}                              # another PC's change: seen after the window ...
    assert c.read(p, {}, fresh=True) == {"v": 22}                 # ... or at once inside a write transaction
    c.drop(p)
    assert c.read(p, {}) == {"v": 22} and len(calls) == 3
    missing = tmp_path / "none.json"
    for _ in range(10):
        assert c.read(missing, {"d": 1}) == {"d": 1}
    assert len(calls) == 4                                        # a missing file is remembered too


def test_the_repository_sees_its_own_writes_at_once(app, world, repo):
    repo.save_settings({"grace_minutes": 7})
    assert repo.get_settings()["grace_minutes"] == 7
    repo.save_settings({"grace_minutes": 9})
    assert repo.get_settings()["grace_minutes"] == 9
