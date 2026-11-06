# Requested security and release checks

| Request | Implemented control / validation | Remaining external check |
| --- | --- | --- |
| Remove test data | Empty runtime database; fixtures live only in temporary test databases. No seeded user, market or order data. | Inspect deployment imports and backups before release. |
| Hide API keys | Server-side per-user Fernet encryption; secrets never returned; ignored local config; no keys in JavaScript. | Inject production secrets securely and verify hosting logs. |
| Protect admin routes | Server-side role check; shell-only promotion; no public role assignment. | Configure administrator MFA before public launch. |
| Auth and permissions | Scrypt passwords, signed secure cookies, CSRF, logout, password-change session revocation, disabled-account enforcement, ownership filters. | Verified-email/recovery delivery and external identity checks are not implemented. |
| Database rules | Parameterized SQLite statements, foreign keys, owner-scoped queries, service-only database permissions. | Backup restoration, persistent storage, host isolation. Multi-host deployment needs database/rate-limit redesign. |
| Input validation | Typed payloads, bounded fields and positive finite decimal amounts; original CLI rejects NaN/infinity. | Binance enforces current symbol lot/tick rules. |
| API rate limits | Shared transactional limits across application workers; per-account and IP limits. | Tune limits for the deployment's explicitly trusted proxy topology. |
| File uploads | 2 MB body limit, decoded PNG/JPEG/WebP only, dimensions limited, metadata removed, JPEG re-encode; owner-only retrieval. Malicious and oversized uploads tested. | Confirm reverse-proxy request limits. |
| API errors | Bounded network timeouts; safe user errors; uncertain trade outcomes never auto-retried; persisted idempotency keys. | Verify real Binance rejection and connection-loss scenarios. |
| Debug logs | Web debug disabled; no request-body/provider-error logging; CLI logs rotated at INFO without raw provider responses. | Check host/APM logging configuration. |
| Sensitive errors | Generic unexpected errors with incident IDs; no raw exception contents in responses. | Review deployment error handlers and monitoring. |
| Mobile layouts | Browser smoke at 390, 768 and 1440 pixels; no horizontal page overflow. | Device-specific accessibility and Safari testing. |
| Slow internet | Delayed-response and offline browser tests; pending controls disabled; 25-second browser timeout; no automatic order retry. | Broader real-world network and device testing. |
| Payments and webhooks | Hosted optional support subscription; server-owned Price/customer identity; verified signatures, replay protection, canonical provider state, reused checkout sessions, test-mode default. | Stripe account, recurring Price, portal and HTTPS webhook configuration; real test-mode checkout/refund/renewal tests. |
| Try to break it | Automated CSRF, privilege escalation, cross-user access, invalid numeric input, duplicate order, timeout, malicious upload, rate limit, webhook forgery/replay and raw-error leak tests. | Independent penetration test; this checklist does not certify security. |

Trading stays free. Support subscriptions do not unlock trading. No real money orders or charges were made during development.
