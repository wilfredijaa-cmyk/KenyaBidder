import itertools

import pytest

from kenyabidder.app import create_app
from kenyabidder.clock import FakeClock
from kenyabidder.store import Store

_n = itertools.count(1)


class Env:
    def __init__(self, **kw):
        self.clock = FakeClock()
        self.app = create_app(store=Store(), clock=self.clock, **kw)

    def __getattr__(self, k):
        return getattr(self.app, k)

    def seller(self, reserve_floor=0, memory=None, name=None, config=None, constraints=None):
        u = self.app.agents.create_user(name=name or f"Amina{next(_n)}", password="password123", phone="+254700000001", email="amina@example.com")
        a = self.app.agents.create_agent(user_id=u["id"], type="SELLER", constraints={"reserve_floor": reserve_floor, **(constraints or {})}, memory=memory, config=config)
        return u, a

    def bidder(self, ceiling=100_000, name=None, constraints=None, memory=None, config=None, phone=None):
        u = self.app.agents.create_user(name=name or f"David{next(_n)}", password="password123", phone=phone or "+254700000002", email="d@example.com")
        a = self.app.agents.create_agent(user_id=u["id"], type="BIDDER", constraints={"budget_ceiling": ceiling, **(constraints or {})}, memory=memory, config=config)
        return u, a

    def link_whatsapp(self, agent, number):
        """Link a WhatsApp number the way production allows it: it must be the account's own VERIFIED phone."""
        from kenyabidder.phone import normalize_phone
        u = self.store.users[agent["principal_user_id"]]
        u.update(phone=normalize_phone(number), phone_verified=True)
        return self.app.agents.link_channel(agent["agent_id"], "WHATSAPP", number)

    def english(self, seller_agent_id, **over):
        base = dict(seller_agent_id=seller_agent_id, product_spec={"category": "electronics", "title": "Samsung A15 phones", "quantity": 10},
                    auction_type="ENGLISH", reserve_price=1000, start_price=1000, min_increment=100, duration_ms=60_000)
        base.update(over)
        return self.app.engine.create_listing(**base)


@pytest.fixture
def env():
    return Env()


@pytest.fixture
def make_env():
    return Env
