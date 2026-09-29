"""Kiswahili (and English) for the core screens.

Strings are looked up by their exact English text, so the UI code stays readable and an untranslated string simply shows in English.
``install`` wraps the handful of NiceGUI constructors that carry static text (labels, buttons, inputs, tabs…) so pages need no
per-string changes. Dynamic sentences (built with f-strings) stay English for now — the catalogue below covers navigation, sign-in,
listing/bidding, wallet, profile, verification and status words.

⚠ These translations are a first draft made without a native reviewer. Have a Kiswahili speaker review them before launch
(``tests/test_i18n.py`` keeps the catalogue honest: no stale keys, placeholders preserved).
"""
from __future__ import annotations

LANGS = {"en": "English", "sw": "Kiswahili"}

SW: dict[str, str] = {
    # navigation
    "Auctions": "Minada", "My agents": "Mawakala wangu", "Wallet": "Pochi", "Inbox": "Ujumbe", "Matches": "Mechi", "Activity": "Shughuli",
    "Platform": "Jukwaa", "Admin": "Msimamizi", "Sign out": "Toka",
    # sign in / up
    "Sign in": "Ingia", "Create account": "Fungua akaunti", "Name": "Jina", "Password": "Nenosiri", "Forgot password?": "Umesahau nenosiri?",
    "Password (8+ characters)": "Nenosiri (herufi 8+)", "Email": "Barua pepe", "Phone": "Simu",
    "Wrong name or password": "Jina au nenosiri si sahihi",
    "AI agents that buy and sell for you. The platform finds the match — payment and delivery stay between the two of you.":
        "Mawakala wa AI wanaonunua na kuuza kwa niaba yako. Jukwaa linatafuta mechi — malipo na uwasilishaji ni kati yenu wawili.",
    "Reset your password": "Weka upya nenosiri lako", "Send code": "Tuma msimbo", "Set new password": "Weka nenosiri jipya",
    "6-digit code": "Msimbo wa tarakimu 6", "New password (8+ characters)": "Nenosiri jipya (herufi 8+)",
    "Password changed — sign in with your new password": "Nenosiri limebadilishwa — ingia kwa nenosiri jipya",
    # common actions
    "Save": "Hifadhi", "Cancel": "Ghairi", "Confirm": "Thibitisha", "Buy": "Nunua", "Approve": "Idhinisha", "Reject": "Kataa", "Saved": "Imehifadhiwa",
    "Delete": "Futa", "Send": "Tuma", "Back": "Rudi", "Continue": "Endelea",
    # listing
    "New listing": "Bidhaa mpya", "Product": "Bidhaa", "Category": "Aina", "Quantity": "Kiasi", "Auction type": "Aina ya mnada",
    "Reserve (hidden from bidders)": "Bei ya chini kabisa (imefichwa)", "Start price": "Bei ya kuanzia", "Min increment": "Ongezeko la chini",
    "Floor price": "Bei ya chini", "Drop per step": "Punguzo kwa hatua", "Step (seconds)": "Hatua (sekunde)", "Duration (minutes)": "Muda (dakika)",
    "Create listing": "Weka bidhaa", "Ask my agent": "Muulize wakala wangu", "Listing created": "Bidhaa imewekwa",
    "Only verified businesses may bid": "Biashara zilizothibitishwa pekee ndizo zinaweza kuzabuni",
    "Auctions & requests for quotes": "Minada na maombi ya nukuu", "No listings yet — create one above.": "Bado hakuna bidhaa — weka moja hapo juu.",
    # RFQ
    "Request quotes (RFQ)": "Omba nukuu (RFQ)", "What do you need?": "Unahitaji nini?", "Maximum total price (KES)": "Bei ya juu kabisa kwa jumla (KES)",
    "Min. undercut (KES)": "Punguzo la chini (KES)", "Post request": "Tuma ombi", "Format": "Muundo",
    "Only verified businesses may quote": "Biashara zilizothibitishwa pekee ndizo zinaweza kutoa nukuu",
    "Request posted — supplier agents have been notified": "Ombi limetumwa — mawakala wa wasambazaji wamearifiwa",
    # auction page
    "All auctions": "Minada yote", "No bids yet.": "Bado hakuna zabuni.", "Bid": "Zabuni", "Quote": "Nukuu", "Sealed bids": "Zabuni zilizofungwa",
    "Bid manually": "Toa zabuni mwenyewe", "Quote manually": "Toa nukuu mwenyewe", "Manual bid (KES)": "Zabuni ya mkono (KES)", "Manual quote (KES)": "Nukuu ya mkono (KES)",
    "Let my agent bid for me": "Mruhusu wakala wangu atoe zabuni", "Let my agent quote for me": "Mruhusu wakala wangu atoe nukuu",
    "Bid placed": "Zabuni imetolewa", "Quote placed": "Nukuu imetolewa", "Withdraw listing": "Ondoa bidhaa", "Withdraw request": "Ondoa ombi",
    "✓ verified business": "✓ biashara imethibitishwa", "verified bidders only": "wazabuni waliothibitishwa pekee", "yours": "yako",
    "sold": "imeuzwa", "unsold": "haijauzwa", "cancelled": "imefutwa", "active": "inaendelea", "scheduled": "imepangwa", "extending": "imeongezwa muda",
    "closing…": "inafungwa…",
    # matches
    "Confirm match": "Thibitisha mechi", "How did it go?": "Ilikwendaje?", "Proposed": "Imependekezwa", "Seller confirmed": "Muuzaji amethibitisha",
    "Buyer confirmed": "Mnunuzi amethibitisha", "Contact revealed": "Mawasiliano yamefunuliwa",
    "No matches yet. When an auction closes with a winner, the match appears here.": "Bado hakuna mechi. Mnada ukifungwa na mshindi, mechi itaonekana hapa.",
    "Something went wrong? Open a dispute": "Kuna tatizo? Fungua mgogoro", "Open dispute": "Fungua mgogoro", "Withdraw dispute": "Ondoa mgogoro",
    # wallet
    "Buy tokens & plans": "Nunua tokeni na mipango", "My orders": "Oda zangu", "tokens": "tokeni", "No orders yet.": "Bado hakuna oda.",
    "No token packs are on sale yet.": "Bado hakuna vifurushi vya tokeni vinavyouzwa.",
    # profile
    "My profile": "Wasifu wangu", "Contact details": "Mawasiliano", "Change password": "Badilisha nenosiri", "Current password": "Nenosiri la sasa",
    "Password changed": "Nenosiri limebadilishwa", "Verification": "Uthibitisho", "verified": "imethibitishwa", "not verified": "haijathibitishwa",
    "Confirm ": "Thibitisha ", "Verified business": "Biashara iliyothibitishwa", "Apply for the badge": "Omba nembo", "Business or legal name": "Jina la biashara au la kisheria",
    "Registration number": "Nambari ya usajili", "Registration type": "Aina ya usajili", "Your data": "Data yako",
    "Download my data (JSON)": "Pakua data yangu (JSON)", "Delete my account": "Futa akaunti yangu", "Delete my account permanently": "Futa akaunti yangu kabisa",
    "Your password": "Nenosiri lako", "Getting started": "Kuanza",
    "Add and verify your phone number": "Ongeza na uthibitishe nambari yako ya simu",
    "Shared with a counterparty only after BOTH of you confirm a match.": "Huonyeshwa mhusika mwingine baada ya NYOTE wawili kuthibitisha mechi.",
    # agents
    "Save rules": "Hifadhi sheria", "Save brain, tools & knowledge": "Hifadhi ubongo, zana na maarifa", "Brain configuration saved": "Mipangilio ya ubongo imehifadhiwa",
    "Hard limits (enforced by the deterministic guardrail — the LLM cannot override them)": "Mipaka ngumu (inatekelezwa na kinga isiyotumia AI — LLM haiwezi kuivuka)",
}

# SMS / email bodies, by template id (server side; chosen by the recipient's language)
TEMPLATES = {
    "phone_code": {"en": "KenyaBidder code: {code}. Valid 10 minutes. Never share it.",
                   "sw": "Msimbo wa KenyaBidder: {code}. Unatumika dakika 10. Usimpe mtu yeyote."},
    "reset_sms": {"en": "KenyaBidder password reset code: {code}. Valid 10 minutes. If you did not ask, ignore this.",
                  "sw": "Msimbo wa kuweka upya nenosiri la KenyaBidder: {code}. Unatumika dakika 10. Kama hukuomba, puuza."},
    "email_code": {"en": "Your KenyaBidder verification code is {code}. It is valid for 10 minutes. If you did not ask for it, ignore this message.",
                   "sw": "Msimbo wako wa uthibitisho wa KenyaBidder ni {code}. Unatumika dakika 10. Kama hukuuomba, puuza ujumbe huu."},
    "reset_email": {"en": "Your KenyaBidder password reset code is {code}. It is valid for 10 minutes. If you did not ask for it, ignore this message.",
                    "sw": "Msimbo wako wa kuweka upya nenosiri la KenyaBidder ni {code}. Unatumika dakika 10. Kama hukuuomba, puuza ujumbe huu."},
}


def translate(text: str, lang: str | None) -> str:
    return SW.get(text, text) if lang == "sw" else text


def template(name: str, lang: str | None, **kw) -> str:
    t = TEMPLATES[name]
    return t.get(lang or "en", t["en"]).format(**kw)


def install(ui_module, get_lang) -> None:
    """Wrap NiceGUI's static-text constructors so exact catalogue matches render in the user's language. Idempotent."""
    if getattr(ui_module, "_kb_i18n", False):
        return
    ui_module._kb_i18n = True

    def tr(s):
        try:
            return translate(s, get_lang()) if isinstance(s, str) else s
        except Exception:  # noqa: BLE001  no client context (tests, background work): stay English
            return s

    for name in ("label", "button", "input", "number", "textarea", "checkbox", "switch", "tab", "select", "expansion", "menu_item", "link", "badge"):
        orig = getattr(ui_module, name, None)
        if orig is None or not callable(orig):
            continue

        def make(orig=orig, name=name):
            def wrapped(*a, **kw):
                if a and isinstance(a[0], str) and name != "select":
                    a = (tr(a[0]), *a[1:])
                if isinstance(kw.get("label"), str):
                    kw["label"] = tr(kw["label"])
                if isinstance(kw.get("placeholder"), str):
                    kw["placeholder"] = tr(kw["placeholder"])
                return orig(*a, **kw)
            wrapped.__wrapped__ = orig
            return wrapped
        setattr(ui_module, name, make())
    orig_notify = ui_module.notify

    def notify(message, *a, **kw):
        return orig_notify(tr(message), *a, **kw)
    ui_module.notify = notify
