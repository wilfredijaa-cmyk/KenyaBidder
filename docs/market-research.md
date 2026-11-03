# Market research — what similar products do well, and what we took

Researched October 2026 (web search; sources at the bottom). Findings are summarised, not quoted.

| Product / source | Best ideas | What KenyaBidder does with it |
|---|---|---|
| **SAP Ariba / enterprise reverse auctions** | Live bidding with transparent updates; event rules (timing, extensions); "bid transformation" (compare on total cost, not unit price); heavy setup is their weakness. | We keep the rules engine (anti-sniping, decrements) but make setup one form. **Total-cost view** and savings-vs-max shown on RFQ results. |
| **Alibaba.com RFQ** | Free for buyers; ~15 quotes in hours; only *verified* suppliers can quote; quote comparison; payment held until delivery (Trade Assurance). | Verified-only RFQs exist. Added: **quote comparison with supplier scorecards**, repost-RFQ, and an escrow-ready match model (escrow partner = roadmap). |
| **Jiji (Kenya classifieds)** | Ad moderation before publish; AI/behaviour-based fraud detection; verified-seller badges; cooperation with police. | Added **fraud signals** (shill-bid / same-identity detection, velocity limits, new-account caps), listing moderation hooks (report + admin takedown), verified badges (already). |
| **Twiga Foods (Kenyan B2B)** | Mobile ordering, fast delivery promise, retailer credit; moved to asset-light, partnering with distributors. | Confirms the wedge (SME wholesale). Roadmap: delivery hook; we stay asset-light (matchmaker). |
| **Pactum (AI negotiation)** | Buyer-defined guardrails and walk-away rules *before* negotiating; trade-offs across price/terms; game-theory not just LLM text. | Our design already matches (LLM proposes, rules decide). Added **explainable decisions** (why a bid was blocked) and per-agent **walk-away** semantics via floors/ceilings. |
| **B2B RFQ platforms (Infor, Oracle, Avetti, Origami)** | Alerts when an RFQ matches; reminders if nobody responds; response-time as a ranking/trust metric; structured comparable offers (8-15% better prices); reorder / saved baskets. | Added **match alerts for watchers (even without auto-bid)**, **supplier response-time & win-rate scorecards**, **repost/reorder** buttons. |
| **Marketplace fraud practice (velocity limits, device/behaviour signals, risk scoring)** | Rate limits by account/IP/device; multi-signal risk; friction only when risky. | Per-IP and per-account rate limits, risk-based step-up (OTP before high-value), shill-bid detection, audit trail. |
| **OWASP ASVS** | CSP / nosniff / HSTS; session timeout + rotation on login; no banners; request size limits; rate limiting; secure defaults. | Implemented as the security baseline (see `SECURITY.md`). |
| **NiceGUI docs** | Behind a TLS reverse proxy with sticky sessions; `storage_secret` required; session cookie options via middleware kwargs; Python-level XSS patterns (`ui.html`) are the app's responsibility. | Proxy config shipped (Caddy), cookie flags, no unescaped user HTML. |

## Ideas we generated beyond competitors
1. **Explainable agent decisions** — every blocked/escalated/skipped action shows a plain-language reason and which hard limit caused it.
2. **Savings ledger** — per buyer, how much below the stated maximum/typical price their RFQs cleared at (the metric that sells the product).
3. **Supplier "response race" insight** — show suppliers how fast the winning quote arrived, so they tune strategies.
4. **Two-sided kill switch** — one tap to pause all agents of a user (and admin global pause) for incidents or fraud waves.
5. **Platform-wide circuit breakers** already exist per category; extended to a global emergency stop.

## Sources
* [SAP Ariba reverse auction overview](https://www.rfp.wiki/vendors/vendorful/sap-ariba)
* [Alibaba.com RFQ](https://reads.alibaba.com/nl/unlock-business-opportunities-with-request-for-quotation-rfq-on-alibaba-com)
* [Jiji Kenya security measures](https://techtrendske.co.ke/2020/11/23/jiji-online-market-place-kenya-new-security-measures-to-protect-its-users-in-a-bid-to-dominate-kenyas-e-commerce-space/)
* [Twiga asset-light pivot](https://techcabal.com/2025/05/20/twiga-kenyan-b2b-startup-shifts-strategy-asset-light/)
* [Pactum on AI negotiation](https://pactum.com/blog/can-ai-negotiate)
* [RFQ response time](https://medium.com/@sustern/why-your-rfq-response-time-is-losing-you-buyers-b0b4a6a7ba4c) · [Infor RFQ alerts](https://docs.infor.com/se/12.1.x/en-us/secuhelp/ktu1576147559172.html)
* [OWASP ASVS overview](https://www.hexnode.com/blogs/explained/what-is-owasp-asvs/) · [NiceGUI deployment notes](https://nicegui.io/documentation/section_configuration_deployment)
