# Northstar web workspace

Northstar adds an authenticated web interface to the existing trading CLI. Trading remains free. Optional recurring subscriptions support the service; no trading feature depends on payment. The exchange is hard-pinned to Binance USDT-M Futures Testnet. No real-money trading or price-feed simulation is included.

## Development

Python 3.10+ is required; validated with Python 3.12.

```sh
cd /workspace/trading-bot-assignment
python3 -m venv venv
venv/bin/python -m pip install --upgrade "pip>=26.2"
venv/bin/python -m pip install -r requirements-dev.txt
venv/bin/python scripts/dev.py
```

The development runner binds only to loopback, disables debug mode, and creates private generated secrets and an empty SQLite database under ignored `instance/`. It relaxes secure cookies only for loopback HTTP development. It never seeds accounts, orders, payments, or market data. Register through the UI. User exchange keys are stored encrypted, separately per account. Reconnecting replaces the encrypted credentials; disconnecting deletes them. Credentials are not returned to the browser. Use only testnet keys with the minimum permissions needed.

To promote a registered local development account, use trusted shell access:

```sh
venv/bin/python -m flask --app scripts.dev:app promote-admin you@example.com
```

Promotion invalidates existing sessions; sign in again. Web registration cannot set roles. Admins can disable regular accounts; they cannot disable themselves or other administrators through the web endpoint.

## Validation

```sh
venv/bin/python -m pytest -q
venv/bin/python -m pip check
venv/bin/python -m pip_audit --vulnerability-service pypi --cache-dir /tmp/northstar-audit --progress-spinner off
node --check web/static/app.js
venv/bin/python tests/browser_smoke.py
```

Browser smoke requires Chromium at `/usr/bin/chromium` (override with `CHROMIUM_PATH`). It uses a temporary database and screenshots under `/tmp`, never the application database. It covers registration, navigation, unavailable billing, delayed requests, offline messaging, and overflow at 1440, 768 and 390 pixels. Unit/integration tests use isolated temporary databases and mocked exchange/payment requests. They do not prove external service behavior. Test-only credentials and webhook signing secrets are fixtures, never runtime defaults.

## Production configuration

Inject the variables described in `.env.example` into the server process using your host's secret store. `.env` is ignored and not automatically loaded. Generate an application signing key using `secrets.token_urlsafe(48)` and an encryption key using `cryptography.fernet.Fernet.generate_key()`. Keep these values stable and back them up securely. Losing the encryption key requires users to reconnect exchange credentials. Do not use proxy placeholders for encryption or webhook signing secrets: these values are used locally.

Run behind a trusted HTTPS reverse proxy:

```sh
venv/bin/gunicorn --bind 127.0.0.1:5000 --workers 2 --threads 4 --timeout 60 'web:create_app()'
```

The application defaults to secure, HttpOnly, SameSite=Lax cookies; an HTTP public deployment will not work. Terminate TLS on the reverse proxy, redirect HTTP to HTTPS, configure HSTS there, and forward only to the loopback backend. No forwarding headers are trusted for rate limits. At this small deployment size, the proxy's source IP shares a conservative IP rate limit across its visitors; configure an explicitly trusted proxy topology before changing client-IP handling. Do not blindly enable ProxyFix. Apply proxy request limits and timeouts as well. Restrict the database directory to the service account, and back it up using SQLite's backup API. This implementation targets one application host with persistent local storage; multiple hosts need a shared transactional database and distributed rate limiter.

`GET /healthz` verifies application and database availability. It does not validate exchange credentials, payments or outbound connectivity. Runtime logs omit raw provider errors and credentials. Disable debug mode and avoid access logging of sensitive query strings. The application accepts secrets in POST bodies, not URLs. The legacy CLI still accepts optional credential flags for backwards compatibility; prefer environment variables so keys do not enter shell history.

## Optional support subscriptions

1. Create a recurring Stripe Price and set `STRIPE_PRICE_ID` on the server. The browser cannot choose prices or customer IDs. Configure the customer billing portal for cancellation and payment-method management.
2. Inject `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`. Start with Stripe test-mode keys. Live keys are rejected unless `BILLING_LIVE_ENABLED=true` is explicitly configured.
3. Set `PUBLIC_URL` to the actual HTTPS origin, including no untrusted request-derived host.
4. Configure Stripe delivery to `/api/billing/webhook` for `customer.subscription.created`, `customer.subscription.updated` and `customer.subscription.deleted`. Use this endpoint's signing secret. Grant outbound HTTPS access to `api.stripe.com`.
5. Test checkout, cancellation, a declined card, renewal, duplicate delivery, delayed delivery and webhook outage with your Stripe test account. Checkout return URLs never grant a subscription. Signed webhooks synchronize canonical subscription state from Stripe; duplicate event IDs are ignored transactionally, and events cannot switch another account's ownership. Subscription state is limited to the configured Price.
6. Test mode does not collect real payments. Obtain your own approval of pricing, customer terms, taxes, refunds and service identity before enabling real billing.

Payments fail closed when configuration is missing; the application does not pretend a payment succeeded. Checkout sessions are reused to reduce accidental duplicate subscriptions. Stripe network requests time out; billing failures expose a generic message, not provider diagnostics.

## Trade behavior and limitations

The web order desk supports MARKET and LIMIT orders. The original CLI also supports STOP_LIMIT; this is intentionally not exposed in the web desk pending exchange compatibility validation. No order is automatically retried after a timeout. The submission key is stored before contacting Binance, and Binance receives a deterministic client order ID. Reusing the same key cannot place a second order; changing the payload with that key is rejected. `UNKNOWN` or `SUBMITTING` can mean an exchange accepted an order but the response was lost. Check the Binance testnet account before making another submission. There is no automatic reconciliation worker or order cancellation UI yet. Quantities are sent as validated decimal strings; Binance additionally enforces symbol-specific lot and tick sizes.

Profile images accept JPEG, PNG and WebP only, with a 2 MB request limit and a 16-megapixel decode limit. Uploaded content is decoded, cropped, stripped of original metadata and re-encoded to a 256-pixel JPEG. Original filenames and arbitrary paths are not used. Only the owning authenticated account can retrieve its image. SVG, documents and arbitrary files are not supported.

Passwords use Werkzeug scrypt hashes. CSRF tokens protect state-changing requests; Stripe webhooks instead require provider signatures. Login, account creation, credential updates, uploads, billing and orders have persistent transactional rate limits. Password changes invalidate every session. Admin account changes take effect on subsequent requests. SQL is parameterized, user data is rendered as text, and CSP prohibits inline scripts, framing and external code.

This is a tested initial web application, not a claim of a security audit or completed production rollout. Before a public launch: configure and test real infrastructure, add verified-email delivery and account recovery if required, add administrator MFA, verify backup restoration, monitor suspicious activity and webhook failures, and perform an independent penetration test. Exchange execution and live payments remain unverified until real configuration and authorized end-to-end tests are available.
