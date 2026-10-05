"""End-to-end: export the mock "الاستفسار عن الطلبات" grid to CSV and check that
the extractor walks every server page (5 rows/page) and writes all of them."""
import csv
import os

import pytest

pytest.importorskip("playwright")

from cem_bot.config import load_config          # noqa: E402
from cem_bot.inquiry import extract_requests     # noqa: E402
from mock import state as S                      # noqa: E402
from mock.server import MockServers              # noqa: E402

PORTAL_PORT, CEM_PORT = 8721, 8722


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
    cfg.settings["inquiry"]["output_dir"] = str(tmp_path)
    return cfg


def test_extract_all_pages(servers, tmp_path):
    S.reset()
    out = tmp_path / "requests.csv"
    res = extract_requests(make_cfg(tmp_path), date_from="01-07-2026", date_to="30-09-2026",
                           out=str(out), headless=True, slow_mo=0)

    expected = len(S.inquiry_rows())                     # 12 rows, 5 per page -> 3 pages
    assert res["total_reported"] == expected
    assert res["rows"] == expected
    assert res["pages"] == 3                              # ceil(12 / 5)

    with open(out, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == S.INQUIRY_HEADERS + [""]            # header row (+ the action column)
    data = rows[1:]
    assert len(data) == expected
    # the request-date column is carried through and every row is distinct
    assert data[0][0] == "01/07/2026"
    assert len({tuple(r) for r in data}) == expected


def test_extract_date_filter_narrows(servers, tmp_path):
    """A one-day range returns only that day's rows (fewer than one page)."""
    S.reset()
    out = tmp_path / "one_day.csv"
    res = extract_requests(make_cfg(tmp_path), date_from="01-07-2026", date_to="01-07-2026",
                           out=str(out), headless=True, slow_mo=0)
    assert res["pages"] == 1
    assert res["rows"] == res["total_reported"] >= 1
    with open(out, encoding="utf-8-sig", newline="") as fh:
        data = list(csv.reader(fh))[1:]
    assert all(r[0] == "01/07/2026" for r in data)
