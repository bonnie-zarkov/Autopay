# PayHero integration contract

Set credentials in `.env`; never place secrets in this repository file.

- `PAYHERO_USERNAME`
- `PAYHERO_PASSWORD`
- `PAYHERO_BASIC_AUTH_TOKEN`
- `PAYHERO_CHANNEL_ID`
- `PAYHERO_CALLBACK_URL`
- `PAYHERO_WEBHOOK_SECRET`
- `PAYHERO_LIVE_PAYMENTS=false`

STK initiation endpoint:

`POST https://backend.payhero.co.ke/api/v2/payments?is_active=true`

Required payload fields:

- `amount`
- `phone_number`
- `channel_id`
- `provider` (`m-pesa`)
- `external_reference`
- `customer_name`
- `callback_url`

Use Basic authentication from `PAYHERO_BASIC_AUTH_TOKEN`, or build it from
`PAYHERO_USERNAME` and `PAYHERO_PASSWORD`.

Live initiation is disabled by default. Enabling `PAYHERO_LIVE_PAYMENTS=True`
may initiate real-money payments and must only be used for controlled testing.

Wallet top-ups are live-only: the app does not create simulated credits. Enable
live payments only after setting a public HTTPS `PAYHERO_CALLBACK_URL`, valid
PayHero credentials and channel ID, and a non-empty `PAYHERO_CALLBACK_SECRET`
(or `PAYHERO_WEBHOOK_SECRET`). Configure the callback to reach
`/api/webhooks/payhero/`. The callback must include the same secret in the
`X-PayHero-Secret` header or an `s` query parameter; if PayHero cannot set a
header, include `?s=<PAYHERO_CALLBACK_SECRET>` in the callback URL. A wallet
deposit remains uncredited until the authenticated callback is confirmed
against PayHero's transaction status.
