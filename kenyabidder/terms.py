"""Default Terms & Privacy Notice shown at sign-up. Operators MUST have counsel review and edit this (Admin → Legal)."""

DEFAULT_TERMS = """\
KenyaBidder connects buyers and sellers. Read this before creating an account.

1. What we do — and do not do
KenyaBidder runs auctions to find a counterparty and a price. We do NOT hold, transfer or guarantee payment for goods, and we do not arrange or guarantee delivery. Once a match is confirmed, you and the other party settle payment and delivery directly, at your own risk.

2. Contact sharing
When BOTH sides confirm a match, we reveal your name, phone number and email to the other party so you can complete the deal. Do not confirm a match if you are not willing to share these details. We keep an audit log of these reveals.

3. Your agent acts for you
Your AI agent bids and negotiates within the limits you set (budget ceiling, allowed auction types, approvals). You are responsible for those limits and for the bids your agent places within them. Agents can make mistakes.

4. LLM tokens
AI decisions consume LLM tokens that you buy from us in packs. Tokens are used up as your agents work, are tied to the model they were bought for, and are not exchangeable for cash. Unless required by law, purchased tokens are non-refundable once used. Payments for tokens are processed via M-Pesa or manual transfer.

5. Your data
We store your account details, agent settings, activity and payment references to run the service. Listing text you submit is shown to other users and may be processed by AI models (including third-party providers) on their behalf. You may ask for your data to be corrected or deleted, subject to legal and audit obligations.

6. Acceptable use
No fake listings, shill bidding, harassment or attempts to attack or overload the service. We may suspend accounts that break these rules.

7. Changes
We may update these terms; continuing to use the service after notice means you accept the update.
"""


def get_terms(store) -> str:
    return store.settings.get("terms_text") or DEFAULT_TERMS


def set_terms(store, text: str) -> None:
    text = (text or "").strip()
    if len(text) > 20_000:
        from .errors import bad
        raise bad("TERMS_TOO_LONG", "terms text is limited to 20,000 characters")
    store.settings["terms_text"] = text
