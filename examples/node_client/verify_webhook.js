// A ~40-line HTTP server showing how to verify paykit's outbound webhook
// signature (see paykit/service_app/outbound.py's module docstring for the
// receiver-side contract).

const http = require("http");
const crypto = require("crypto");

const SECRET = process.env.PAYKIT_OUTBOUND_WEBHOOK_SECRET;
const PORT = process.env.PORT || 4000;
const MAX_CLOCK_SKEW_SECONDS = 300; // reject anything older than 5 minutes

if (!SECRET) {
  console.error("Set PAYKIT_OUTBOUND_WEBHOOK_SECRET.");
  process.exit(1);
}

function verify(rawBody, timestamp, signature) {
  const expected = crypto
    .createHmac("sha256", SECRET)
    .update(`${timestamp}.${rawBody}`)
    .digest("hex");
  const ok =
    expected.length === signature.length &&
    crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(signature));
  if (!ok) return false;

  const age = Math.abs(Date.now() / 1000 - Number(timestamp));
  return age <= MAX_CLOCK_SKEW_SECONDS;
}

http
  .createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      const signature = req.headers["x-paykit-signature"];
      const timestamp = req.headers["x-paykit-timestamp"];
      const eventId = req.headers["x-paykit-event-id"];

      if (!signature || !timestamp || !verify(body, timestamp, signature)) {
        res.writeHead(400).end("invalid signature");
        return;
      }

      const event = JSON.parse(body);
      console.log(`Verified event ${eventId}: ${event.type}`);
      res.writeHead(200).end("ok");
    });
  })
  .listen(PORT, () => console.log(`Listening on :${PORT}`));
