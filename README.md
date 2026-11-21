# AutoPay

AutoPay is a Django MVP for bill reminders and guarded payment authorization.
It uses a mock PayHero adapter so no real money is moved.

## Run

```bash
python3 manage.py migrate
python3 manage.py runserver 127.0.0.1:8000
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
