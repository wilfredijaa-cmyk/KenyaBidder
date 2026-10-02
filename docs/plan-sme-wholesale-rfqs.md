# Launch plan — SME wholesale RFQs (WhatsApp-first, verified suppliers, optional escrow)

**Hypothesis:** small buyers (shops, caterers, schools, contractors) will post bulk needs as RFQs, and verified suppliers' agents will quote them down,
producing completed deals with more quotes and better prices than phone/WhatsApp haggling. The platform stays a matchmaker.

## 1. Pick the wedge (week 0–1)
* One category with repeat bulk buying and several local suppliers: **electronics accessories, FMCG/dry goods, or school/office supplies** (choose after 10 buyer + 10 supplier interviews).
* One city (Nairobi CBD / Industrial Area or Mombasa). Aim for **15–25 suppliers on board before any buyer is invited** (supply-first: buyers only return if RFQs get quotes).

## 2. What to build, in order
| # | Work | Why | Effort |
|---|------|-----|--------|
| 1 | **WhatsApp-native RFQ flow**: post an RFQ, receive/accept quotes, confirm match by chat (extends existing router/commands) | SMEs won't use a web console | M |
| 2 | **Supplier onboarding + catalogue seed**: import a price list (CSV/photo→text), set price floors per product, category watch → auto-quote | Supplier agents need floors to quote safely | M |
| 3 | **Verified suppliers by default**: KRA PIN / business-registration check against a registry API (manual fallback) | Trust is the product | M |
| 4 | **Optional escrow**: partner M-Pesa/bank escrow, release on buyer confirmation; keep "platform never holds funds" by using the partner as custodian | Answers the top objection | L (partner-dependent) |
| 5 | **Delivery hook**: courier/transporter booking at match time | Turns an introduction into a completed deal | M |
| 6 | **Quote comparison for buyers**: total landed cost, supplier reputation, delivery time — not just price | Lowest price alone loses deals | S |
| 7 | **Metrics + cohort dashboard** (already have /metrics; add RFQ funnel) | Decide go / no-go with data | S |

Already built and reused: reverse auctions, supplier strategies with price floors, verified badges, verified-only RFQs, disputes, reputation, M-Pesa, SMS codes, Kiswahili shell.

## 3. Measure (the two numbers that matter)
* **Quote depth:** share of RFQs receiving **≥ 3 quotes** within 30 minutes. Target ≥ 60%.
* **Completion:** share of matches reported **COMPLETED** by both sides. Target ≥ 70%.
* Also track: median saving vs the buyer's stated max price, time to first quote, repeat-RFQ rate (buyers posting a second RFQ within 30 days), disputes per 100 matches.

## 4. Go / no-go gates
* **Week 4:** ≥ 20 suppliers active, ≥ 50 RFQs posted. If quote depth < 30%, fix supply before adding buyers.
* **Week 8:** quote depth ≥ 60% and completion ≥ 60% → widen to a second category. Otherwise interview churned users and change the wedge, not the code.

## 5. Revenue tests (only after the gates)
1. Supplier plan (monthly, more agents/decisions) — already built.
2. Success fee on completed matches, or per-RFQ fee for buyers — needs a completion signal (escrow gives a reliable one).
3. Price-intelligence reports for the category.

## 6. Risks
* **Cold start** (no supply → no buyers): supply-first, seed with a friendly supplier association.
* **Off-platform bypass** after the introduction: escrow + delivery + reputation must add value beyond the intro.
* **Agent errors on price floors**: keep the hard guardrail floor; require supplier confirmation of the first N auto-quotes.
* **Compliance**: ODPC registration, lawyer-reviewed Terms, consumer-protection review of any escrow arrangement.
