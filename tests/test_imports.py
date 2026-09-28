import importlib
import pkgutil

import kenyabidder


def test_every_module_imports():
    """The UI/server modules are otherwise only exercised by the slow browser tests — a syntax error must fail the fast suite."""
    names = [m.name for m in pkgutil.walk_packages(kenyabidder.__path__, "kenyabidder.")]
    assert "kenyabidder.ui.pages" in names and "kenyabidder.__main__" in names
    for n in names:
        importlib.import_module(n)
