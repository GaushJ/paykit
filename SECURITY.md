# Security Policy

OpenPayKit handles payment state, so security reports get priority.

## Reporting a vulnerability

**Do not open a public issue.** Report it privately through GitHub:
**Security → Report a vulnerability** on this repository (private vulnerability reporting).

Please include:

- the affected version or commit,
- a description of the issue and its impact (for example: forged webhook, wrong order marked
  paid, cross-customer data access, over-refund),
- steps or a proof of concept to reproduce it.

You'll get an acknowledgement within 3 working days. We aim to ship a fix for confirmed high or
critical issues within 14 days, and we will credit you in the release notes unless you prefer
otherwise.

## Supported versions

| Version | Supported |
|---|---|
| 0.x (latest release) | ✅ |
| older 0.x releases | ❌ (upgrade to the latest) |

## Scope

In scope: the `paykit` package (signature verification, webhook handling, state transitions,
storage, the FastAPI integration and the standalone service), plus the Dockerfile and CI
workflows in this repository.

Out of scope: vulnerabilities in Razorpay itself, in your application's own authentication
(`auth_dependency` / `admin_dependency`), and deployments that ignore the README's "What you still
own" list (e.g. running `InMemoryRepository` or `PAYKIT_PROVIDER=dummy` in production).
