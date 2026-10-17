"""Minimal example: mount paykit's router in a FastAPI app.

Run (test-mode Razorpay keys):

    PAYKIT_RAZORPAY_KEY_ID=rzp_test_xxx \
    PAYKIT_RAZORPAY_KEY_SECRET=xxx \
    PAYKIT_RAZORPAY_WEBHOOK_SECRET=xxx \
    uvicorn examples.fastapi_embedded.main:app --reload

Then open http://localhost:8000/ and click Pay - it opens Razorpay
Checkout, and on success posts the result to /payments/confirm.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from paykit import Money, build_service_from_env
from paykit.integrations.fastapi import create_payments_router, paykit_lifespan

# A real app has its own auth (JWT, session cookies, ...). HTTP Basic here is
# only so this example is runnable standalone with zero extra setup - any
# username/password is accepted and becomes the "customer_ref".
security = HTTPBasic()


def get_current_user(credentials: HTTPBasicCredentials = Depends(security)) -> dict[str, str]:
    return {"id": credentials.username}


def amount_resolver(request_body: dict[str, object], user: dict[str, str]) -> Money:
    # A real app prices this from `request_body` (a cart, a plan id, ...)
    # looked up server-side. Never trust a client-sent amount (§9).
    return Money(amount=49900, currency="INR")


service = build_service_from_env()


@service.events.on("payment.captured")
async def _log_payment_captured(event):
    print(f"\n>>> WEBHOOK RECEIVED: payment.captured for order {event.payment.order_id}\n")


app = FastAPI(title="PayKit example", lifespan=paykit_lifespan(service))
app.include_router(
    create_payments_router(
        service,
        auth_dependency=get_current_user,
        customer_ref_getter=lambda user, body: user["id"],
        amount_resolver=amount_resolver,
    )
)


@app.get("/")
def checkout_page() -> FileResponse:
    return FileResponse(Path(__file__).parent / "checkout.html")
