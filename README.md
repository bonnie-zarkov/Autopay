# AutoPay

AutoPay is a Django MVP for bill reminders and guarded payment authorization.
It uses a mock PayHero adapter so no real money is moved.

## Run

```bash
python3 manage.py migrate
python3 manage.py runserver 127.0.0.1:8000
```

Run the one-minute reminder/remittance scheduler in a second terminal:

```bash
source venv/bin/activate
python manage.py run_autopay_scheduler
```

Open `http://127.0.0.1:8000/signup/` and create an account.

Demo login:

- Username: `demo`
- Password: `demo12345`

Create a bill, then use **Demo controls** to test changed amounts or recipient details.
Authorized payments can be submitted with **Pay** and completed with the dashboard callback
buttons. Red and frozen payments remain blocked by the backend.

## Test

```bash
python3 manage.py test core
```

## Deployment hosts

`ALLOWED_HOSTS` accepts a comma-separated list of hostnames (without schemes).
Vercel's `VERCEL_URL`, `VERCEL_PROJECT_PRODUCTION_URL`, and `VERCEL_BRANCH_URL`
are included automatically when present. For other hosts, set `ALLOWED_HOSTS`
in the deployment environment; do not use `*` in production. Django debug mode
defaults to off; keep `DJANGO_DEBUG=False` in production to avoid exposing
tracebacks and request details.

## PayHero and reminders

Credentials and channel settings belong in `.env`, which is ignored by Git:

```text
PAYHERO_USERNAME=
PAYHERO_PASSWORD=
PAYHERO_BASIC_AUTH_TOKEN=
PAYHERO_CHANNEL_ID=
PAYHERO_CALLBACK_URL=
PAYHERO_WEBHOOK_SECRET=
PAYHERO_LIVE_PAYMENTS=False
REDIS_URL=redis://127.0.0.1:6379/0
CELERY_TASK_ALWAYS_EAGER=True
```

The adapter remains in mock mode until `PAYHERO_LIVE_PAYMENTS=True` and all required
PayHero values are configured. Live mode can initiate real-money M-Pesa payments.

Generate and deliver in-app reminders with:

```bash
python3 manage.py send_reminders
```

## Wallet and scheduled remittances

Open `/wallet/` to request an STK deposit, inspect ledger balances, and complete mock
deposits. In mock mode the "complete mock" control credits demo funds only; it is not
a real M-Pesa transfer. Live STK requires a public HTTPS `PAYHERO_CALLBACK_URL`.
Never mark a live deposit successful from the demo UI.

Add a bill, then choose **Schedule remittance** on its dashboard card and set its
date/time. Turn on the bill's explicit auto-remittance consent to allow a green,
funded bill to execute automatically in mock mode; otherwise the scheduler requests
confirmation at the scheduled time. Current scheduled remittances debit the internal
demo ledger only. Real bill payouts are disabled until PayHero's official payout
contract and production verification are implemented.
