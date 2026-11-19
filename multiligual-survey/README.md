# Multiligual Survey — starter application

Python Flask + SQLite. English interface using the requested spelling. Demo mode is default; no real money is accepted and simulated balances must never be moved into production. Text-answer surveys, publishing/editing, one response per member, reward budget accounting, member balances, withdrawal requests, admin review, provider-based registration verification.

## Run locally (Windows)
1. Install Python 3.11 or newer. Open a terminal in this folder.
2. `python -m venv .venv`
3. `.venv\Scripts\activate`
4. `pip install -r requirements.txt`
5. `copy .env.example .env` (macOS/Linux: `cp .env.example .env`; activate with `source .venv/bin/activate`).
6. Edit .env: choose your own admin email/password (12+ characters), and generate SECRET_KEY with `python -c "import secrets; print(secrets.token_hex(32))"`.
7. `python app.py` and open http://127.0.0.1:8000. Admin signs in on the regular login page. Create a survey with a demo budget, register a separate member, activate demo membership, answer it and request a simulated withdrawal.

## Live integration (not tested against provider)
NOWPayments direct payment API creates a USD 10 quote payable in USDT TRC20; this is NOT a guaranteed exact 10-token amount. Provider fees and quotations may vary. Confirm provider minimum and availability first. Callback is a notification only; the server fetches authoritative status using the merchant API key and checks payment ID, order ID, currency and paid amount. A finished fully paid transaction activates membership. The member can also refresh payment status. Never use a private wallet key in the application.

Before launch: get merchant approval and API credentials, configure HTTPS/public callback, implement webhook signature validation and rate limits, test in provider sandbox including underpayment, duplicates, expiry and failed transactions, ensure persistent database and backups, separate demo and live databases, add email verification/password reset, audit logs, proper role provisioning and security review. Use a production WSGI server e.g. `gunicorn app:app` on Linux behind HTTPS. Only after all checks set DEMO_MODE=0, LIVE_PAYMENTS=1, HTTPS=1. Never commit .env. The live payment switch should remain off until legal, consumer protection, privacy and provider requirements are satisfied. Publish clear terms, refund and privacy policies, support contact, eligibility rules, and actual survey availability before taking money. Consider free registration instead: charging people for access to paid work creates serious consumer trust and compliance risks. Do not fund rewards using new member fees; secure independent research-client funding. Budget entries are not verified cash reserves.

Withdrawals: admin transfers USDT externally from a funded wallet after checking address/network and eligibility, then records the real transaction hash. No automated sending or on-chain payout verification is implemented. Validate address checksum, payout finality, fees and applicable compliance before production. No additional fee to unlock withdrawals. Multiple-choice questions, translations, CSV exports, email and hosting are not included in this version. Surveys with existing answers retain question wording; publish a new survey for changes. This is a starter, not a production-ready financial system.

Provider documentation: https://nowpayments.zendesk.com/hc/en-us/articles/21345824322717-API-and-endpoint-description
