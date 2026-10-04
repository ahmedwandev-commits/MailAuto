"""End-to-end: run the real bot (headless browser) against the mock portal + mock CEM
and check the final state of every ticket and CEM user."""
import os

import openpyxl
import pytest

pytest.importorskip("playwright")

from cem_bot.config import load_config          # noqa: E402
from cem_bot.runner import Runner               # noqa: E402
from mock import state as S                     # noqa: E402
from mock.server import MockServers             # noqa: E402

PORTAL_PORT, CEM_PORT = 8711, 8712


@pytest.fixture(scope="module")
def servers():
    os.environ.update(PORTAL_USERNAME="233786", PORTAL_PASSWORD="Mock@12345",
                      CEM_USERNAME="233786", CEM_PASSWORD="Mock@12345", OPERATOR_ID="233786")
    m = MockServers(portal_port=PORTAL_PORT, cem_port=CEM_PORT).start()
    yield m
    m.stop()


def make_cfg(tmp_path):
    cfg = load_config(profile="mock")
    cfg.settings["profiles"]["mock"] = {"portal_base": f"http://127.0.0.1:{PORTAL_PORT}",
                                        "cem_base": f"http://127.0.0.1:{CEM_PORT}"}
    cfg.settings["run"]["output_dir"] = str(tmp_path)
    return cfg


def ticket(uid):
    return next(t for t in S.STATE["tickets"] if t["user_id"] == uid)


def user(login):
    return next((u for u in S.STATE["users"].values() if u["login"] == login), None)


def branches(login):
    return {b["identity"]: sorted(b["grants"]) for b in user(login)["branches"]}


def test_dry_run_changes_nothing(servers, tmp_path):
    S.reset()
    before = S.snapshot()
    rep = Runner(make_cfg(tmp_path), dry_run=True, headless=True, slow_mo=0).run()
    after = S.snapshot()
    assert before["tickets"] == after["tickets"]
    assert before["users"] == after["users"]
    c = rep.counts()
    assert c["PLANNED"] == 7 and c["SKIPPED"] == 1   # only the 8 already-assigned tickets are visible


def test_live_run(servers, tmp_path):
    S.reset()
    rep = Runner(make_cfg(tmp_path), dry_run=False, headless=True, slow_mo=0).run()
    c = rep.counts()
    assert (c["DONE"], c["REJECTED"], c["SKIPPED"], c["ERROR"]) == (8, 4, 2, 1), c

    done, not_done = "تم التنفيذ", "لم يتم التنفيذ"
    # ---- MODIFY
    assert ticket("300101")["decision"] == done and ticket("300101")["matrix_checked"]
    u = user("300101")
    assert (u["active"], u["locked"], u["roles"]) == (True, False, [])
    assert branches("300101") == {"151": ["serving"]}                    # old 252/236 removed

    assert ticket("300102")["decision"] == done
    assert user("300102")["roles"] == ["Branch Superviser"]
    assert branches("300102") == {"254": ["alerts_receiving", "monitoring", "serving"]}

    assert ticket("300103")["decision"] == done
    assert user("300103")["roles"] == ["Branch Manager (Component)"]
    assert branches("300103")["167"] == ["alerts_receiving", "monitoring", "supervising"]
    assert "151" in branches("300103")                                   # managers keep old branches

    t = ticket("300104")                                                 # not in CEM (decoy 1300104 exists)
    assert (t["decision"], t["reason"], t["matrix_checked"]) == (not_done, "ليس لدية كود", True)
    assert ticket("300105")["status"] == "open"                          # unmapped job code

    # ---- REACTIVATE / CANCEL
    assert ticket("300106")["decision"] == done
    assert (user("300106")["active"], user("300106")["locked"]) == (True, False)
    assert (ticket("300107")["decision"], ticket("300107")["reason"]) == (not_done, "ليس لدية كود")
    assert ticket("300108")["decision"] == done
    assert (user("300108")["active"], user("300108")["locked"]) == (False, False)
    assert (ticket("300109")["decision"], ticket("300109")["reason"]) == (not_done, "ليس لدية كود")

    # ---- CREATE
    for login, name, roles, br in [
        ("300110", "هاني جديد صبري", [], {"151": ["serving"]}),
        ("300111", "دينا جديدة عادل", ["Branch Superviser"], {"424": ["alerts_receiving", "monitoring", "serving"]}),
        ("300112", "مروان جديد فتحي", ["Branch Manager (Component)"],
         {"521": ["alerts_receiving", "monitoring", "supervising"]}),
    ]:
        assert ticket(login)["decision"] == done
        u = user(login)
        assert u["name_en"] == u["name_ar"] == f"{login} - {name}"
        assert (u["auth_type"], u["department"], u["active"], u["locked"]) == ("Windows", "Main", True, False)
        assert u["roles"] == roles and branches(login) == br

    t = ticket("300113")
    assert (t["decision"], t["reason"]) == (not_done, "Other")
    assert t["notes"] == "لديه كود بالفعل برجاء ارسال طلب تعديل صلاحية وليس إنشاء"

    # ---- untouched
    assert ticket("300114")["status"] == "open"                          # unknown type
    assert ticket("300115")["status"] == "open" and ticket("300115")["assigned_to"] is None   # other system
    assert ticket("900116")["status"] == "open" and user("900116") is None                   # failed -> left open

    # ---- report
    wb = openpyxl.load_workbook(rep.path)
    rows = list(wb["Tickets"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 15
    err = [r for r in rows if r[2] == "ERROR"]
    assert len(err) == 1 and err[0][6] == "900116" and err[0][19]   # screenshot recorded


def test_second_run_is_idempotent(servers, tmp_path):
    """Re-running on the finished state only re-reports what is left open."""
    rep = Runner(make_cfg(tmp_path), dry_run=False, headless=True, slow_mo=0, types=["MODIFY", "CANCEL"]).run()
    c = rep.counts()
    assert c["DONE"] == 0 and c["REJECTED"] == 0 and c["SKIPPED"] == 2
