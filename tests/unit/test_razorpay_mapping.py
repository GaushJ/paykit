from paykit.core.models import OrderStatus, PaymentStatus, RefundStatus, SubscriptionStatus
from paykit.providers.razorpay.mapping import (
    map_order_status,
    map_payment_status,
    map_refund_status,
    map_subscription_status,
    order_from_razorpay,
    payment_from_razorpay,
    plan_from_razorpay,
    refund_from_razorpay,
    subscription_from_razorpay,
)


class TestMapOrderStatus:
    def test_known_values(self) -> None:
        assert map_order_status("created") == OrderStatus.CREATED
        assert map_order_status("attempted") == OrderStatus.ATTEMPTED
        assert map_order_status("paid") == OrderStatus.PAID

    def test_unknown_value_falls_back_to_attempted(self) -> None:
        assert map_order_status("something_new") == OrderStatus.ATTEMPTED


class TestMapPaymentStatus:
    def test_created_authorized_captured_failed(self) -> None:
        assert map_payment_status("created", amount=100, amount_refunded=0) == PaymentStatus.CREATED
        assert (
            map_payment_status("authorized", amount=100, amount_refunded=0)
            == PaymentStatus.AUTHORIZED
        )
        assert (
            map_payment_status("captured", amount=100, amount_refunded=0)
            == PaymentStatus.CAPTURED
        )
        assert map_payment_status("failed", amount=100, amount_refunded=0) == PaymentStatus.FAILED

    def test_full_refund_by_status(self) -> None:
        assert (
            map_payment_status("refunded", amount=100, amount_refunded=100)
            == PaymentStatus.REFUNDED
        )

    def test_partial_refund_inferred_from_amount_refunded(self) -> None:
        # Razorpay leaves `status` as "captured" for a partial refund.
        assert (
            map_payment_status("captured", amount=100, amount_refunded=40)
            == PaymentStatus.PARTIALLY_REFUNDED
        )

    def test_full_refund_inferred_when_amount_refunded_equals_amount(self) -> None:
        assert (
            map_payment_status("captured", amount=100, amount_refunded=100)
            == PaymentStatus.REFUNDED
        )


class TestMapRefundStatus:
    def test_known_values(self) -> None:
        assert map_refund_status("pending") == RefundStatus.PENDING
        assert map_refund_status("processed") == RefundStatus.PROCESSED
        assert map_refund_status("failed") == RefundStatus.FAILED


class TestMapSubscriptionStatus:
    def test_known_values(self) -> None:
        assert map_subscription_status("active") == SubscriptionStatus.ACTIVE
        assert map_subscription_status("halted") == SubscriptionStatus.HALTED
        assert map_subscription_status("cancelled") == SubscriptionStatus.CANCELLED

    def test_resumed_event_name_maps_to_active_status(self) -> None:
        assert map_subscription_status("resumed") == SubscriptionStatus.ACTIVE


class TestOrderFromRazorpay:
    def test_maps_fields(self) -> None:
        payload = {
            "id": "order_FS7X6f5hIeYaXd",
            "amount": 49900,
            "currency": "INR",
            "receipt": "inv_1001",
            "status": "paid",
            "notes": {"plan": "pro"},
            "created_at": 1567674000,
        }
        order = order_from_razorpay(payload)
        assert order.provider_order_id == "order_FS7X6f5hIeYaXd"
        assert order.amount.amount == 49900
        assert order.amount.currency == "INR"
        assert order.status == OrderStatus.PAID
        assert order.receipt == "inv_1001"
        assert order.notes == {"plan": "pro"}


class TestPaymentFromRazorpay:
    def test_maps_fields(self) -> None:
        payload = {
            "id": "pay_FS7Y0hIeE1AJXQ",
            "amount": 49900,
            "currency": "INR",
            "status": "captured",
            "order_id": "order_FS7X6f5hIeYaXd",
            "amount_refunded": 0,
            "method": "upi",
            "error_code": None,
            "error_description": None,
            "created_at": 1567674599,
        }
        payment = payment_from_razorpay(payload)
        assert payment.provider_payment_id == "pay_FS7Y0hIeE1AJXQ"
        assert payment.provider_order_id == "order_FS7X6f5hIeYaXd"
        assert payment.provider_subscription_id is None
        assert payment.amount.amount == 49900
        assert payment.method == "upi"

    def test_carries_explicit_subscription_id(self) -> None:
        payload = {
            "id": "pay_FS8AbCdEfGhIjK",
            "amount": 49900,
            "currency": "INR",
            "status": "captured",
            "order_id": None,
            "amount_refunded": 0,
            "method": "card",
            "created_at": 1570266601,
        }
        payment = payment_from_razorpay(payload, provider_subscription_id="sub_DEX6xcJ1HSW4CR")
        assert payment.provider_subscription_id == "sub_DEX6xcJ1HSW4CR"


class TestRefundFromRazorpay:
    def test_maps_fields(self) -> None:
        payload = {
            "id": "rfnd_FS8TWyPrCsa0OB",
            "payment_id": "pay_FS7Y0hIeE1AJXQ",
            "amount": 20000,
            "currency": "INR",
            "status": "processed",
            "notes": {"reason": "requested_by_customer"},
            "created_at": 1567675000,
        }
        refund = refund_from_razorpay(payload)
        assert refund.provider_refund_id == "rfnd_FS8TWyPrCsa0OB"
        assert refund.provider_payment_id == "pay_FS7Y0hIeE1AJXQ"
        assert refund.status == RefundStatus.PROCESSED


class TestPlanFromRazorpay:
    def test_maps_fields(self) -> None:
        payload = {
            "id": "plan_DDMCa3E1i5J8mm",
            "period": "monthly",
            "interval": 1,
            "item": {"name": "Pro plan", "amount": 49900, "currency": "INR"},
        }
        plan = plan_from_razorpay(payload)
        assert plan.provider_plan_id == "plan_DDMCa3E1i5J8mm"
        assert plan.name == "Pro plan"
        assert plan.amount.amount == 49900
        assert plan.period == "monthly"
        assert plan.interval == 1


class TestSubscriptionFromRazorpay:
    def test_maps_fields(self) -> None:
        payload = {
            "id": "sub_DEX6xcJ1HSW4CR",
            "plan_id": "plan_DDMCa3E1i5J8mm",
            "status": "active",
            "current_start": 1567674600,
            "current_end": 1570266600,
            "total_count": 12,
            "paid_count": 1,
            "short_url": "https://rzp.io/i/abcdef",
            "notes": {"user_id": "user_42"},
        }
        sub = subscription_from_razorpay(payload)
        assert sub.provider_subscription_id == "sub_DEX6xcJ1HSW4CR"
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.total_count == 12
        assert sub.paid_count == 1
        assert sub.notes == {"user_id": "user_42"}
