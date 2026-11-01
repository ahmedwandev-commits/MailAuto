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
