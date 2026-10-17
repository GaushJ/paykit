import pytest

from paykit.core.models import OrderStatus, PaymentStatus, RefundStatus, SubscriptionStatus
from paykit.core.state import can_transition


class TestPaymentTransitions:
    @pytest.mark.parametrize(
        "old,new",
        [
            (PaymentStatus.CREATED, PaymentStatus.AUTHORIZED),
            (PaymentStatus.CREATED, PaymentStatus.CAPTURED),
            (PaymentStatus.CREATED, PaymentStatus.FAILED),
            (PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED),
            (PaymentStatus.AUTHORIZED, PaymentStatus.FAILED),
            (PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED),
            (PaymentStatus.CAPTURED, PaymentStatus.REFUNDED),
            (PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED),
            (PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.PARTIALLY_REFUNDED),
        ],
    )
    def test_allowed(self, old: PaymentStatus, new: PaymentStatus) -> None:
        assert can_transition(old, new) is True

    @pytest.mark.parametrize(
        "old,new",
        [
            (PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED),  # backward
            (PaymentStatus.CAPTURED, PaymentStatus.CREATED),  # backward
            (PaymentStatus.FAILED, PaymentStatus.CAPTURED),  # terminal
            (PaymentStatus.REFUNDED, PaymentStatus.CAPTURED),  # terminal
            (PaymentStatus.AUTHORIZED, PaymentStatus.CREATED),  # backward
        ],
    )
    def test_disallowed(self, old: PaymentStatus, new: PaymentStatus) -> None:
        assert can_transition(old, new) is False

    def test_same_status_is_idempotent_noop(self) -> None:
        assert can_transition(PaymentStatus.CAPTURED, PaymentStatus.CAPTURED) is True
        assert can_transition(PaymentStatus.FAILED, PaymentStatus.FAILED) is True

    def test_late_authorized_after_captured_is_ignored(self) -> None:
        """A late `authorized` webhook must not move a captured payment backward."""
        assert can_transition(PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED) is False


class TestOrderTransitions:
    def test_created_to_paid(self) -> None:
        assert can_transition(OrderStatus.CREATED, OrderStatus.PAID) is True

    def test_paid_is_terminal(self) -> None:
        assert can_transition(OrderStatus.PAID, OrderStatus.ATTEMPTED) is False
        assert can_transition(OrderStatus.PAID, OrderStatus.EXPIRED) is False


class TestRefundTransitions:
    def test_pending_to_processed(self) -> None:
        assert can_transition(RefundStatus.PENDING, RefundStatus.PROCESSED) is True

    def test_processed_is_terminal(self) -> None:
        assert can_transition(RefundStatus.PROCESSED, RefundStatus.FAILED) is False


class TestSubscriptionTransitions:
    def test_created_to_active_via_authenticated(self) -> None:
        assert can_transition(SubscriptionStatus.CREATED, SubscriptionStatus.AUTHENTICATED) is True
        assert can_transition(SubscriptionStatus.AUTHENTICATED, SubscriptionStatus.ACTIVE) is True

    def test_active_can_halt_and_resume(self) -> None:
        assert can_transition(SubscriptionStatus.ACTIVE, SubscriptionStatus.HALTED) is True
        assert can_transition(SubscriptionStatus.HALTED, SubscriptionStatus.ACTIVE) is True

    def test_cancelled_is_terminal(self) -> None:
        assert can_transition(SubscriptionStatus.CANCELLED, SubscriptionStatus.ACTIVE) is False


def test_mismatched_types_raise_type_error() -> None:
    with pytest.raises(TypeError):
        can_transition(PaymentStatus.CREATED, OrderStatus.CREATED)
