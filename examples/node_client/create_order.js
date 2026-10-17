// Creates an order against a running paykit-service, over plain HTTP -
// the whole point of the standalone service is that non-Python backends
// never need paykit's Python package at all.

const SERVICE_URL = process.env.PAYKIT_SERVICE_URL || "http://localhost:8000";
const API_KEY = process.env.PAYKIT_SERVICE_API_KEY;

if (!API_KEY) {
  console.error("Set PAYKIT_SERVICE_API_KEY (one of PAYKIT_SERVICE_API_KEYS).");
  process.exit(1);
}

async function createOrder() {
  const response = await fetch(`${SERVICE_URL}/payments/orders`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${API_KEY}`,
    },
    // Unlike the embedded integration, this backend is trusted, so it
    // passes amount/currency/customer_ref explicitly (§10).
    body: JSON.stringify({
      amount: 49900,
      currency: "INR",
      customer_ref: "node_user_42",
      receipt: `node_${Date.now()}`,
    }),
  });

  if (!response.ok) {
    throw new Error(`create_order failed: ${response.status} ${await response.text()}`);
  }

  const { order, checkout } = await response.json();
  console.log("Created order:", order.id, "provider order:", checkout.order_id);
  console.log("Hand `checkout` to the frontend to open Razorpay Checkout.");
}

createOrder().catch((err) => {
  console.error(err);
  process.exit(1);
});
