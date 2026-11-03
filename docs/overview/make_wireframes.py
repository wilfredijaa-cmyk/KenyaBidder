"""Generates the low-fidelity wireframes (SVG) in wireframes/. Run: python docs/overview/make_wireframes.py"""
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).parent / "wireframes"
W, H = 1000, 640
INK, MUTE, LINE, FILL, ACC = "#1f2937", "#6b7280", "#9ca3af", "#f3f4f6", "#0b7a4b"


class Sheet:
    def __init__(self, title, note=""):
        self.title, self.note, self.p = title, note, []

    def rect(self, x, y, w, h, fill="#fff", stroke=LINE, r=6, dash=False, sw=1.5):
        self.p.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"' + (' stroke-dasharray="6 4"' if dash else "") + "/>")

    def text(self, x, y, s, size=14, fill=INK, weight="400", anchor="start"):
        self.p.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" font-weight="{weight}" text-anchor="{anchor}">{escape(s)}</text>')

    def btn(self, x, y, w, label, primary=False):
        self.rect(x, y, w, 30, fill=ACC if primary else "#fff", stroke=ACC if primary else LINE, r=15)
        self.text(x + w / 2, y + 20, label, 13, "#fff" if primary else INK, "600", "middle")

    def field(self, x, y, w, label, value=""):
        self.text(x, y - 5, label, 11, MUTE)
        self.rect(x, y, w, 28, fill="#fff")
        if value:
            self.text(x + 8, y + 19, value, 13, MUTE)

    def card(self, x, y, w, h, title, lines=(), badge=None):
        self.rect(x, y, w, h, fill="#fff")
        self.text(x + 12, y + 24, title, 14, INK, "600")
        for i, ln in enumerate(lines):
            self.text(x + 12, y + 46 + i * 20, ln, 12, MUTE)
        if badge:
            self.rect(x + w - 92, y + 10, 80, 20, fill=FILL, r=10)
            self.text(x + w - 52, y + 24, badge, 11, INK, "600", "middle")

    def chrome(self, active=""):
        self.rect(0, 0, W, 54, fill=FILL, stroke=LINE, r=0)
        self.text(20, 34, "Kenya", 20, INK, "700")
        self.text(82, 34, "Bidder", 20, ACC, "700")
        x = 190
        for n in ("Auctions", "My agents", "Wallet", "Inbox", "Matches", "Activity", "Admin"):
            self.text(x, 33, n, 13, ACC if n == active else MUTE, "700" if n == active else "400")
            x += len(n) * 8 + 26
        self.rect(860, 14, 40, 26, fill="#fff", r=13)
        self.text(880, 32, "EN|SW", 10, MUTE, "600", "middle")
        self.text(920, 33, "12.4k tokens", 12, ACC, "600")

    def note_box(self, x, y, w, s):
        self.rect(x, y, w, 26 + 16 * s.count("\n"), fill="#fffbeb", stroke="#f59e0b", dash=True)
        for i, ln in enumerate(s.split("\n")):
            self.text(x + 10, y + 18 + i * 16, ln, 11, "#92400e")

    def save(self, name):
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="system-ui,Segoe UI,Arial,sans-serif">'
               f'<rect width="{W}" height="{H}" fill="#fff"/>{"".join(self.p)}'
               f'<text x="20" y="{H - 14}" font-size="12" fill="{MUTE}">{escape(self.title)}{(" — " + escape(self.note)) if self.note else ""}</text></svg>')
        (OUT / f"{name}.svg").write_text(svg)


def login():
    s = Sheet("Sign in / create account", "EN | SW toggle top right")
    s.rect(330, 90, 340, 430)
    s.text(500, 140, "Kenya", 26, INK, "700", "middle")
    s.text(500, 168, "Bidder — AI agents that buy and sell for you", 12, MUTE, anchor="middle")
    s.text(360, 205, "Sign in", 14, ACC, "700"); s.text(450, 205, "Create account", 14, MUTE)
    s.field(360, 235, 280, "Name"); s.field(360, 290, 280, "Password"); s.field(360, 345, 280, "Phone (sign up)")
    s.text(360, 400, "☐ I accept the Terms & Privacy Notice", 12, MUTE)
    s.btn(360, 415, 280, "Sign in", True); s.btn(360, 455, 280, "Forgot password?  → code by SMS/email")
    s.text(840, 40, "EN | SW", 12, MUTE)
    s.save("01-login")


def auctions():
    s = Sheet("Auctions & requests for quotes", "buyer view; sellers see 'New listing' instead of 'Request quotes'")
    s.chrome("Auctions")
    s.rect(24, 70, 952, 34, fill=FILL); s.text(40, 92, "Getting started:  ☑ watch spec   ☐ top up tokens   ☑ verify phone", 12, MUTE)
    s.rect(24, 116, 952, 40); s.text(40, 141, "▸ Request quotes (RFQ)   what you need · max total price · format · verified suppliers only", 13, INK, "600")
    for i, (t, d, b) in enumerate([("Samsung A15 phones x10", "electronics · qty 10 · english", "active"), ("50 phone chargers", "RFQ — suppliers quote down (max KES 20,000)", "RFQ"),
                                   ("Maize flour 90kg", "food · dutch · sealed", "sold")]):
        x = 24 + i * 322
        s.card(x, 176, 306, 150, t, [d, "KES 1,300" if i == 0 else ("KES 18,000" if i == 1 else "KES 3,400"), "4 bids · 2m left", "✓ verified business"], b)
    s.note_box(24, 350, 620, "Cards refresh live. Badges: yours · verified business · verified bidders only.\nClick a card → auction detail. Reverse cards show the buyer's maximum.")
    s.save("02-auctions")


def detail():
    s = Sheet("Auction detail")
    s.chrome("Auctions")
    s.rect(24, 70, 600, 150); s.text(40, 100, "Samsung A15 phones x10", 20, INK, "700"); s.text(40, 122, "electronics · qty 10 · english", 12, MUTE)
    s.text(40, 170, "KES 1,300", 34, ACC, "700"); s.text(40, 200, "2m left · next valid bid KES 1,400", 12, MUTE)
    s.rect(24, 232, 600, 190); s.text(40, 258, "Bids (4)", 14, INK, "600")
    for i, (w, a) in enumerate([("agent 8f21c3", "KES 1,300"), ("You", "KES 1,200"), ("agent 5a9e02", "KES 1,100")]):
        s.text(40, 286 + i * 26, w, 13, MUTE); s.text(300, 286 + i * 26, a, 13, INK)
    s.rect(644, 70, 332, 352); s.text(660, 98, "Bid", 15, INK, "600")
    s.btn(660, 112, 300, "Let my agent bid for me", True); s.text(660, 162, "uses the heuristic algorithm for english", 11, MUTE)
    s.field(660, 190, 300, "Manual bid (KES)", "1400"); s.btn(660, 230, 160, "Bid manually")
    s.text(660, 290, "Hard ceiling KES 5,000. Manual bids still", 11, MUTE); s.text(660, 306, "pass the guardrail.", 11, MUTE)
    s.note_box(24, 440, 952, "Sealed auctions hide other bids until close. RFQs show 'Quotes' and 'next quote must be at most …'.\nThe LLM never places bids: a deterministic engine fires the plan after the guardrail approves it.")
    s.save("03-auction-detail")


def agent():
    s = Sheet("My agent — Rules · Brain, tools & knowledge · Channels · Talk")
    s.chrome("My agents")
    for i, t in enumerate(("Rules", "Brain, tools & knowledge", "Channels", "Talk to agent")):
        s.text(30 + i * 150, 88, t, 13, ACC if i == 1 else MUTE, "700" if i == 1 else "400")
    s.rect(24, 100, 470, 250); s.text(40, 126, "Brain", 15, INK, "600")
    s.field(40, 150, 300, "Default LLM", "Claude (sonnet)")
    for i, (a, b) in enumerate([("english", "llm — tool-using model"), ("dutch", "heuristic"), ("first price sealed", "baseline"), ("vickrey", "heuristic")]):
        s.text(40, 214 + i * 34, a, 12, MUTE); s.rect(170, 196 + i * 34, 300, 26, fill="#fff"); s.text(180, 214 + i * 34, b, 12, INK)
    s.rect(510, 100, 466, 250); s.text(526, 126, "Tools (from registered MCP servers)", 15, INK, "600")
    for i, t in enumerate(["☑ get_historical_clearing_prices", "☑ get_demand_signal", "☐ search_knowledge_base", "☐ (external MCP) fx_rates"]):
        s.text(526, 156 + i * 26, t, 13, INK if t[0] == "☑" else MUTE)
    s.text(526, 280, "Knowledge bases:  ☑ Pricing guide   ☐ Returns policy", 12, MUTE)
    s.rect(24, 362, 952, 96); s.text(40, 388, "Rules (hard limits — enforced by the guardrail, not the LLM)", 14, INK, "600")
    s.field(40, 410, 200, "Budget ceiling / bid (KES)", "5000"); s.field(260, 410, 200, "Ask me above % of ceiling", "50"); s.field(480, 410, 200, "Category", "electronics")
    s.btn(720, 410, 150, "Save rules", True)
    s.note_box(24, 476, 952, "Sellers see: price floor, auto-relist, evening summary, RFQ watch + a strategy per RFQ type (baseline / heuristic / llm).\nToken status shows whether the agent can run its LLM (balance, daily cap, hourly limit).")
    s.save("04-agent")


def matches():
    s = Sheet("Matches, contact reveal and disputes")
    s.chrome("Matches")
    s.card(24, 70, 952, 230, "Samsung A15 phones x10 — KES 1,400", [], "confirmed")
    for i, t in enumerate(("Proposed", "Seller confirmed", "Buyer confirmed", "Contact revealed")):
        s.rect(40 + i * 150, 110, 140, 24, fill=ACC if i < 4 else FILL, r=12); s.text(110 + i * 150, 127, t, 11, "#fff", "600", "middle")
    s.text(40, 165, "Seller: Amina Traders ✓ verified business · +254 700 000 002 · amina@…", 13, INK)
    s.text(40, 190, "Buyer: David · +254 712 000 003", 13, INK)
    s.text(40, 222, "How did it go?", 12, MUTE); s.btn(140, 205, 110, "completed"); s.btn(260, 205, 110, "fell through"); s.btn(380, 205, 110, "no response")
    s.rect(40, 245, 920, 40, fill=FILL); s.text(52, 270, "▸ Something went wrong? Open a dispute — category + what happened (other party answers within 7 days; admin rules)", 12, INK)
    s.note_box(24, 318, 952, "Nothing is decided until both sides report (72h grace). Contradicting reports → DISPUTED, nobody penalised.\nA dispute ruling (completed / seller at fault / buyer at fault / both / no fault) updates reputation. No funds ever move on the platform.")
    s.save("05-matches")


def wallet():
    s = Sheet("Wallet — tokens, plans, M-Pesa")
    s.chrome("Wallet")
    s.card(24, 70, 300, 110, "Claude (sonnet)", ["12,400 tokens", "An agent needs ≥ 2,400 to decide"])
    s.card(340, 70, 300, 110, "Pro plan — active", ["Ends 30 Oct 2026", "25 agents · 240 decisions/agent/h"], "plan")
    for i, (n, t, p) in enumerate([("Starter", "100,000 tokens", "KES 500"), ("Pro (30-day plan)", "500,000 tokens", "KES 2,000"), ("Team", "2,000,000 tokens", "KES 6,500")]):
        x = 24 + i * 322
        s.card(x, 200, 306, 120, n, [t, p + " · ≈ KES 5.00 / 1k"]); s.btn(x + 12, 280, 90, "Buy", True)
    s.rect(24, 336, 952, 130); s.text(40, 362, "My orders", 14, INK, "600")
    s.text(40, 392, "KB-3F2A1C · Starter · KES 500 · M-Pesa STK · PAID", 12, MUTE)
    s.text(40, 416, "KB-91B7D0 · Pro · KES 2,000 · waiting for code   [ M-Pesa code ______ ]  [Submit]", 12, MUTE)
    s.note_box(24, 484, 952, "STK push to the phone; the callback only triggers an authenticated status query. One M-Pesa code = one purchase.\nLedger + CSV exports below. Free-trial tokens arrive when the phone is verified.")
    s.save("06-wallet")


def profile():
    s = Sheet("Profile — verification, business badge, privacy")
    s.chrome()
    s.card(24, 70, 470, 130, "Contact details", ["Phone +254 712 000 003  ✓ verified", "Email david@… (not verified)  [Send code] [____] [Confirm]"])
    s.card(24, 212, 470, 150, "Verified business", ["Business name · registration type · number", "Admin checks it on iTax / BRS, then approves"], "apply")
    s.card(510, 70, 466, 130, "Change password", ["Current · New (8+ chars)", "Ends every other session"])
    s.card(510, 212, 466, 150, "Your data (Kenya DPA 2019)", ["[Download my data (JSON)]", "▸ Delete my account (password; blocked while deals are live)"])
    s.note_box(24, 380, 952, "Forgot password? on the sign-in page sends a code to a VERIFIED phone/email and never reveals whether the account exists.")
    s.save("07-profile")


def admin():
    s = Sheet("Admin console")
    s.chrome("Admin")
    tabs = ["LLMs", "MCP servers", "Knowledge bases", "Billing", "Users", "Trust & disputes", "Messaging", "Health & backups", "Legal & demo", "Audit log"]
    x = 24
    for i, t in enumerate(tabs):
        s.text(x, 88, t, 12, ACC if i == 0 else MUTE, "700" if i == 0 else "400"); x += len(t) * 7 + 22
    s.card(24, 104, 952, 90, "Claude (sonnet) — Anthropic · metered ×1.0", ["key stored · roles: buyer, seller agents · max 1024 tokens"], "enabled")
    s.btn(760, 150, 90, "Test"); s.btn(860, 150, 100, "Edit")
    s.btn(24, 208, 130, "Add LLM", True)
    s.rect(24, 260, 470, 200); s.text(40, 286, "Trust & disputes", 14, INK, "600")
    s.text(40, 314, "Verification required above KES [ 500,000 ]", 12, MUTE); s.text(40, 342, "Applications waiting (2)  [Approve] [Reject]", 12, INK); s.text(40, 366, "Open disputes (1)  [Rule on this dispute]", 12, INK)
    s.rect(510, 260, 466, 200); s.text(526, 286, "Health & backups", 14, INK, "600")
    s.text(526, 314, "✓ database  ✓ tick loop  ✓ state flush  ✓ writer lease", 12, INK); s.text(526, 340, "✓ token ledger matches balances", 12, INK); s.btn(526, 364, 130, "Back up now", True)
    s.note_box(24, 476, 952, "Also: Messaging (SMS/email gateways, daily SMS budget, verified-phone policy), Billing (packs, plans, signup grant, limits, revenue & margin),\nUsers (suspend, reset password, grant tokens), Audit log. Metrics: /metrics · Health: /healthz /readyz.")
    s.save("08-admin")


def architecture():
    s = Sheet("How a bid happens — the LLM proposes, rules and a deterministic engine decide")
    boxes = [(30, 90, "Web · WhatsApp\nSMS · FastMCP"), (200, 90, "Channel router\n(agent = identity)"), (370, 90, "Orchestrator\npre-filter"), (540, 90, "Strategy\nbaseline·heuristic·LLM"), (710, 90, "Token meter\n(no tokens, no LLM)")]
    for x, y, t in boxes:
        s.rect(x, y, 150, 70, fill=FILL); [s.text(x + 75, y + 30 + i * 18, ln, 12, INK, "600", "middle") for i, ln in enumerate(t.split("\n"))]
    for x in (180, 350, 520, 690):
        s.text(x + 5, 130, "→", 20, MUTE)
    for x, y, t, c in [(200, 250, "Guardrail interceptor\n(rules, never an LLM)", "#ecfdf5"), (400, 250, "Deterministic\nexecution engine", "#ecfdf5"), (600, 250, "Auction engine\nforward · reverse", "#ecfdf5")]:
        s.rect(x, y, 170, 70, fill=c, stroke=ACC); [s.text(x + 85, y + 30 + i * 18, ln, 12, INK, "600", "middle") for i, ln in enumerate(t.split("\n"))]
    s.text(378, 290, "→", 20, MUTE); s.text(578, 290, "→", 20, MUTE); s.text(600, 200, "LLM proposal (untrusted input)  ↓", 12, MUTE)
    s.rect(200, 380, 570, 60, fill=FILL); s.text(485, 416, "Match service → confirm → contact reveal → outcome → dispute → reputation", 13, INK, "600", "middle")
    s.rect(200, 460, 570, 60, fill=FILL); s.text(485, 496, "DuckDB (PostgreSQL-ready): ledger · orders · state · backups   |   /metrics /readyz", 13, INK, "600", "middle")
    s.note_box(30, 545, 940, "Tools an LLM can call are read-only and assigned per agent; state-changing tools exist only on the internal server. Tool output and listing text are untrusted data.")
    s.save("09-architecture")


OUT.mkdir(exist_ok=True)
for f in (login, auctions, detail, agent, matches, wallet, profile, admin, architecture):
    f()
names = sorted(p.stem for p in OUT.glob("*.svg"))
html = ['<!doctype html><meta charset="utf-8"><title>KenyaBidder wireframes</title><style>body{font-family:system-ui;background:#f8fafc;margin:24px}img{width:100%;max-width:1000px;border:1px solid #d1d5db;border-radius:8px;background:#fff}h2{margin:28px 0 8px}</style><h1>KenyaBidder — wireframes</h1>']
html += [f'<h2>{n}</h2><img src="{n}.svg" alt="{n}">' for n in names]
(OUT / "index.html").write_text("\n".join(html))
print("wrote", len(names), "wireframes")
