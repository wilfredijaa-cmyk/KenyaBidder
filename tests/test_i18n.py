import re
from pathlib import Path

from kenyabidder import i18n

UI = Path(__file__).parent.parent / "kenyabidder" / "ui"
SOURCE = "\n".join(p.read_text() for p in UI.glob("*.py"))


def placeholders(s):
    return sorted(re.findall(r"\{[^}]*\}", s))


def test_catalogue_has_no_empty_or_placeholder_drift():
    for en, sw in i18n.SW.items():
        assert sw.strip() and placeholders(en) == placeholders(sw), en
    for name, langs in i18n.TEMPLATES.items():
        assert placeholders(langs["en"]) == placeholders(langs["sw"]) == ["{code}"], name


def test_every_catalogue_key_is_used_by_the_ui():
    """A translated string that no longer exists in the UI is dead weight — remove it or fix the typo."""
    def used(key):
        return f'"{key}"' in SOURCE or f"'{key}'" in SOURCE or f'"{key.strip()}"' in SOURCE or key.strip() in SOURCE
    dynamic = {"scheduled", "extending"}  # auction status words rendered from data (status.lower())
    missing = [k for k in i18n.SW if k not in dynamic and not used(k)]
    assert not missing, missing


def test_translate_falls_back_to_english_and_only_switches_on_sw():
    assert i18n.translate("Sign in", "sw") == "Ingia"
    assert i18n.translate("Sign in", "en") == i18n.translate("Sign in", None) == "Sign in"
    assert i18n.translate("Some new string", "sw") == "Some new string"


def test_sms_templates_follow_the_users_language():
    assert "Msimbo" in i18n.template("phone_code", "sw", code="123456") and "123456" in i18n.template("phone_code", "sw", code="123456")
    assert i18n.template("phone_code", "fr", code="1").startswith("KenyaBidder code")  # unknown language → English


def test_install_translates_static_text_only_when_swahili(monkeypatch):
    import types
    made = []
    fake = types.SimpleNamespace(label=lambda text="", **kw: made.append(text) or text, button=lambda text="", **kw: made.append(text),
                                 notify=lambda m, *a, **k: made.append(m))
    for n in ("input", "number", "textarea", "checkbox", "switch", "tab", "select", "expansion", "menu_item", "link", "badge"):
        setattr(fake, n, lambda *a, **k: None)
    lang = {"v": "sw"}
    i18n.install(fake, lambda: lang["v"])
    fake.label("Wallet")
    fake.button("Sign in")
    fake.notify("Saved")
    fake.label(f"Dynamic {1}")
    assert made == ["Pochi", "Ingia", "Imehifadhiwa", "Dynamic 1"]
    lang["v"] = "en"
    made.clear()
    fake.label("Wallet")
    assert made == ["Wallet"]
    i18n.install(fake, lambda: "sw")  # idempotent: no double wrapping
    assert fake.label.__wrapped__.__name__ == "<lambda>"
