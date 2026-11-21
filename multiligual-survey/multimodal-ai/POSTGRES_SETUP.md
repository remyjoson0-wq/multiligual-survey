# Multimodal AI — PostgreSQL development update

This ZIP replaces the earlier SQLite starter; it is NOT production ready and cannot safely accept paying customers yet. No old SQLite accounts, surveys, or balances are automatically transferred. Existing SQLite data must be migrated separately if it is important.

## GitHub / Render

1. Make a backup of your old GitHub code and any SQLite data you need to keep.
2. Upload the contents of `multimodal-ai/` to the **root directory Render is configured to build**, retaining `templates/` and `static/` folder structure. If you keep the outer folder, set Render Root Directory to `multimodal-ai`; if uploaded at repository root, leave Root Directory empty.
3. Render build command: `pip install -r requirements.txt`
4. Render start command: `gunicorn app:app`
5. Set the **secret** `DATABASE_URL` to the Render PostgreSQL **internal** URL (database and service must be in the same region). Never publish the URL or commit to GitHub.
6. Set `ADMIN_EMAIL` and a strong `ADMIN_PASSWORD` (at least 12 characters); set `SECRET_KEY` to a separately generated strong random value; set `HTTPS=1`, `DEMO_MODE=0`, `LIVE_PAYMENTS=0`, `REGISTRATION_OPEN=0`, `TASKS_OPEN=0`.
7. Deploy and inspect Render logs. Confirm tables were created in PostgreSQL and verify an administrator can sign in. Check migrations before changing to production settings.

## Restrictions / limitations

- Previous SQLite data is NOT imported. The schema is PostgreSQL-only and intended for a fresh environment.
- Payments disabled; do not share activation links or accept customers yet. A real launch requires production hardening: signed NOWPayments IPN validation; idempotent payment settlement; verified payout and admin controls; tests; transaction audit ledger; responsible data retention; backups; abuse/CSRF/rate-limit protections; secure password resets; and compliance review for activation fees, international rewards and referrals.
- All monetary values stored in integer cents, not floating point.
- Current reward logic credits immediately on submission. If you require review-before-payment, that must be built before a real launch.
- Credits for referrals require an independently funded allocation and administrator approval.
- The app has not been integration-tested against a live PostgreSQL instance in this environment; confirm all workflows in a separate staging environment before release.
- Do not treat the database size or hosting instance as an assurance of traffic capacity.
