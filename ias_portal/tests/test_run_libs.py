"""run.py: a 'libs' folder made by another Python (e.g. click 8.5 on Python 3.9) is detected and reinstalled, not a crash at start."""
import importlib
import sys
from unittest import mock


def test_libs_that_import_are_kept(monkeypatch):
    import run
    monkeypatch.setattr(run, "STAMP", type("S", (), {"read_text": lambda self: run.requirements_hash()})())
    assert run.libs_work() is True and run.needs_install() is False


def test_libs_made_for_another_python_are_installed_again(monkeypatch):
    import run
    real = importlib.import_module

    def broken(name, *a, **k):
        if name == "flask":
            raise SyntaxError("match message:")          # what click 8.5 does on Python 3.9
        return real(name, *a, **k)
    monkeypatch.setattr(run.importlib, "import_module", broken)
    with mock.patch.dict(sys.modules):                    # the purge must not leak into the other tests
        assert run.libs_work() is False and run.needs_install() is True
        assert "flask" not in sys.modules                 # nothing half-imported is left for the fresh install


def test_pdf_export_needs_pillow_too_and_its_requirements_file_exists(monkeypatch):
    """reportlab without Pillow cannot draw a page: the export must fall back to the browser's Print, not an error page."""
    import types
    from pathlib import Path
    from calllog.attendance.services import pdf_export
    for name in ("reportlab", "arabic_reshaper", "bidi"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "PIL", None)                     # "import PIL" raises ImportError
    monkeypatch.setattr(pdf_export, "find_font", lambda configured=None: "/some/font.ttf")
    assert pdf_export.available() is False
    req = Path(__file__).resolve().parents[1] / "program" / "requirements-pdf.txt"
    assert req.is_file() and "reportlab" in req.read_text() and "pillow" in req.read_text()
