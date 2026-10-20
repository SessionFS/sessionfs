"""In-memory stand-in for the parts of the Stripe SDK the billing routes use.

Webhook handlers treat events only as triggers and apply the subscription's
CURRENT state, fetched with ``stripe.Subscription.retrieve_async`` before any
database transaction opens; tests set that canonical state with
:meth:`FakeStripe.subscription`. ``on_call`` lets a test observe (or assert
on) every Stripe call, e.g. that no database transaction is open at the
time. Nothing touches the network.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Callable


class InvalidRequestError(Exception):
    """Named like Stripe's permanent error (no such object)."""


class APIConnectionError(Exception):
    """Named like Stripe's transient network error."""


class FakeStripe:
    def __init__(self) -> None:
        self.subscriptions: dict[str, dict] = {}
        self.products: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.retrieve_error: Exception | None = None
        self.product_error: Exception | None = None
        self.session_retrieve_error: Exception | None = None
        self.checkout_error: Exception | None = None
        self.list_error: Exception | None = None
        self.retrieve_calls: list[str] = []
        self.checkout_calls: list[dict] = []
        self.customer_calls: list[dict] = []
        self.portal_customers: list[str] = []
        self.customers: list[str] = []  # ids created, unique across runs
        self._tag = uuid.uuid4().hex[:8]
        self.on_call: Callable[[str], None] | None = None
        fake = self

        def _note(name: str) -> None:
            if fake.on_call is not None:
                fake.on_call(name)

        class _Subscription:
            @staticmethod
            def retrieve(sub_id):
                _note("Subscription.retrieve")
                fake.retrieve_calls.append(sub_id)
                if fake.retrieve_error is not None:
                    raise fake.retrieve_error
                if sub_id not in fake.subscriptions:
                    raise InvalidRequestError(f"No such subscription: {sub_id}")
                return fake.subscriptions[sub_id]

            @staticmethod
            async def retrieve_async(sub_id):
                return _Subscription.retrieve(sub_id)

            @staticmethod
            async def list_async(customer=None, status=None):
                _note("Subscription.list")
                if fake.list_error is not None:
                    raise fake.list_error
                rows = [
                    s for s in fake.subscriptions.values()
                    if (customer is None or s.get("customer") == customer)
                    and (status is None or s.get("status") == status)
                ]
                return {"data": rows}

        class _Product:
            @staticmethod
            def retrieve(product_id):
                _note("Product.retrieve")
                if fake.product_error is not None:
                    raise fake.product_error
                if product_id not in fake.products:
                    raise InvalidRequestError(f"No such product: {product_id}")
                return fake.products[product_id]

            @staticmethod
            async def retrieve_async(product_id):
                return _Product.retrieve(product_id)

        class _Customer:
            @staticmethod
            def create(**kwargs):
                _note("Customer.create")
                fake.customer_calls.append(kwargs)
                customer_id = f"cus_fake_{fake._tag}_{len(fake.customer_calls)}"
                fake.customers.append(customer_id)
                return SimpleNamespace(id=customer_id)

            @staticmethod
            async def create_async(**kwargs):
                return _Customer.create(**kwargs)

        class _CheckoutSession:
            @staticmethod
            def create(**kwargs):
                _note("checkout.Session.create")
                if fake.checkout_error is not None:
                    raise fake.checkout_error
                fake.checkout_calls.append(kwargs)
                n = len(fake.checkout_calls)
                session = {"id": f"cs_{fake._tag}_{n}", "status": "open",
                           "url": f"https://checkout.example/{fake._tag}/{n}",
                           "customer": kwargs.get("customer")}
                fake.sessions[session["id"]] = session
                return SimpleNamespace(id=session["id"], url=session["url"])

            @staticmethod
            async def create_async(**kwargs):
                return _CheckoutSession.create(**kwargs)

            @staticmethod
            async def retrieve_async(session_id):
                _note("checkout.Session.retrieve")
                if fake.session_retrieve_error is not None:
                    raise fake.session_retrieve_error
                if session_id not in fake.sessions:
                    raise InvalidRequestError(f"No such checkout session: {session_id}")
                return fake.sessions[session_id]

            @staticmethod
            async def list_async(customer=None, status=None):
                _note("checkout.Session.list")
                if fake.list_error is not None:
                    raise fake.list_error
                rows = [
                    s for s in fake.sessions.values()
                    if (customer is None or s.get("customer") == customer)
                    and (status is None or s.get("status") == status)
                ]
                return {"data": rows}

        class _PortalSession:
            @staticmethod
            def create(**kwargs):
                _note("billing_portal.Session.create")
                fake.portal_customers.append(kwargs["customer"])
                return SimpleNamespace(url="https://portal.example/session")

            @staticmethod
            async def create_async(**kwargs):
                return _PortalSession.create(**kwargs)

        self.Subscription = _Subscription
        self.Product = _Product
        self.Customer = _Customer
        self.checkout = SimpleNamespace(Session=_CheckoutSession)
        self.billing_portal = SimpleNamespace(Session=_PortalSession)

    def product(self, product_id: str, tier: str | None) -> None:
        self.products[product_id] = {"id": product_id, "metadata": {"tier": tier} if tier else {}}

    def subscription(
        self,
        sub_id: str,
        *,
        status: str = "active",
        tier: str | None = "team",
        quantity: int = 1,
        customer: str | None = None,
        product: str | None = None,
    ) -> dict:
        """Set the subscription's canonical (current) state."""
        product_id = product or f"prod_{tier or 'none'}"
        if product_id not in self.products:
            self.product(product_id, tier)
        state = {
            "id": sub_id,
            "status": status,
            "customer": customer,
            "items": {"data": [{"price": {"product": product_id}, "quantity": quantity}]},
        }
        self.subscriptions[sub_id] = state
        return state

    def session(self, session_id: str, status: str, customer: str | None = None) -> None:
        """Set a checkout session's state (open | complete | expired)."""
        row = self.sessions.setdefault(session_id, {"id": session_id, "url": None})
        row["status"] = status
        if customer is not None:
            row["customer"] = customer
