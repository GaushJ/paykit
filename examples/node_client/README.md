# PayKit standalone-service Node example

~40 lines: creates an order against `paykit-service` and verifies an
outbound webhook signature. No dependencies beyond Node's built-ins.

```bash
export PAYKIT_SERVICE_URL=http://localhost:8000
export PAYKIT_SERVICE_API_KEY=devkey1
export PAYKIT_OUTBOUND_WEBHOOK_SECRET=whatever-you-set-PAYKIT_OUTBOUND_WEBHOOK_SECRET-to
node create_order.js
node verify_webhook.js   # run this while POSTing a captured sample webhook at it
```
