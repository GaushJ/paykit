import json
from pathlib import Path

from paykit.core.models import OrderStatus, PaymentStatus, RefundStatus, SubscriptionStatus
from paykit.providers.razorpay.webhooks import build_payment_event, normalize_event_type

FIXTURES = Path(__file__).parent.parent / "fixtures" / "razorpay"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class TestNormalizeEventType:
    def test_known_event_passes_through(self) -> None:
        assert normalize_event_type("payment.captured") == "payment.captured"
        assert normalize_event_type("subscription.halted") == "subscription.halted"

    def test_unknown_event_gets_provider_prefix(self) -> None:
        assert normalize_event_type("payment.dispute.created") == "provider.payment.dispute.created"


class TestBuildPaymentEvent:
    def test_payment_authorized(self) -> None:
        body = load_fixture("payment_authorized.json")
        event = build_payment_event(body, event_id="evt_1")

        assert event.id == "evt_1"
        assert event.type == "payment.authorized"
        assert event.provider == "razorpay"
        assert event.payment is not None
        assert event.payment.status == PaymentStatus.AUTHORIZED
        assert event.payment.provider_payment_id == "pay_FS7Y0hIeE1AJXQ"
        assert event.order is None

    def test_payment_captured_includes_no_order_entity(self) -> None:
        body = load_fixture("payment_captured.json")
        event = build_payment_event(body, event_id="evt_2")

        assert event.type == "payment.captured"
        assert event.payment is not None
        assert event.payment.status == PaymentStatus.CAPTURED
        assert event.order is not None
        assert event.order.status == OrderStatus.PAID

    def test_payment_failed_carries_error_fields(self) -> None:
        body = load_fixture("payment_failed.json")
        event = build_payment_event(body, event_id="evt_3")

        assert event.payment is not None
        assert event.payment.status == PaymentStatus.FAILED
        assert event.payment.error_code == "BAD_REQUEST_ERROR"
        assert event.payment.error_description is not None

    def test_order_paid_has_both_order_and_payment(self) -> None:
        body = load_fixture("order_paid.json")
        event = build_payment_event(body, event_id="evt_4")

        assert event.type == "order.paid"
        assert event.order is not None
        assert event.payment is not None

    def test_refund_created(self) -> None:
        body = load_fixture("refund_created.json")
        event = build_payment_event(body, event_id="evt_5")

        assert event.type == "refund.created"
        assert event.refund is not None
        assert event.refund.status == RefundStatus.PROCESSED
        assert event.payment is not None
        assert event.payment.status == PaymentStatus.REFUNDED

    def test_refund_processed_partial(self) -> None:
        body = load_fixture("refund_processed.json")
        event = build_payment_event(body, event_id="evt_6")

        assert event.refund is not None
        assert event.payment is not None
        assert event.payment.status == PaymentStatus.PARTIALLY_REFUNDED

    def test_refund_failed(self) -> None:
        body = load_fixture("refund_failed.json")
        event = build_payment_event(body, event_id="evt_7")

        assert event.refund is not None
        assert event.refund.status == RefundStatus.FAILED

    def test_subscription_activated_links_payment_to_subscription(self) -> None:
        body = load_fixture("subscription_activated.json")
        event = build_payment_event(body, event_id="evt_8")

        assert event.type == "subscription.activated"
        assert event.subscription is not None
        assert event.subscription.status == SubscriptionStatus.ACTIVE
        assert event.payment is not None
        assert event.payment.subscription_id == "sub_DEX6xcJ1HSW4CR"
        assert event.payment.order_id is None

    def test_subscription_charged(self) -> None:
        body = load_fixture("subscription_charged.json")
        event = build_payment_event(body, event_id="evt_9")

        assert event.type == "subscription.charged"
        assert event.subscription is not None
        assert event.subscription.paid_count == 2
        assert event.payment is not None
        assert event.payment.subscription_id == "sub_DEX6xcJ1HSW4CR"

    def test_subscription_halted_has_no_payment(self) -> None:
        body = load_fixture("subscription_halted.json")
        event = build_payment_event(body, event_id="evt_10")

        assert event.type == "subscription.halted"
        assert event.subscription is not None
        assert event.subscription.status == SubscriptionStatus.HALTED
        assert event.payment is None

    def test_subscription_cancelled(self) -> None:
        body = load_fixture("subscription_cancelled.json")
        event = build_payment_event(body, event_id="evt_11")

        assert event.subscription is not None
        assert event.subscription.status == SubscriptionStatus.CANCELLED

    def test_unknown_event_gets_provider_prefixed_type_but_still_parses(self) -> None:
        body = load_fixture("unknown_event.json")
        event = build_payment_event(body, event_id="evt_12")

        assert event.type == "provider.payment.dispute.created"
        assert event.payment is not None
        assert event.raw == body
