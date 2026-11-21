import base64
from decimal import Decimal
from typing import Protocol

import requests
from django.conf import settings


class PaymentProvider(Protocol):
    def initiate_payment(self, *, phone: str, amount: Decimal, reference: str,
                         callback_url: str, customer_name: str = '') -> dict: ...


class PayHeroClient:
    def __init__(self):
        self.base_url = settings.PAYHERO_BASE_URL.rstrip('/')
        self.channel_id = settings.PAYHERO_CHANNEL_ID
        self.basic_auth_token = settings.PAYHERO_BASIC_AUTH_TOKEN
        self.timeout = 20

    def _headers(self):
        token = self.basic_auth_token.strip()
        if token and not token.lower().startswith('basic '):
            token = f'Basic {token}'
        if not token:
            credentials = f"{settings.PAYHERO_USERNAME}:{settings.PAYHERO_PASSWORD}"
            token = 'Basic ' + base64.b64encode(credentials.encode()).decode()
        return {'Authorization': token, 'Content-Type': 'application/json'}

    def initiate_payment(self, *, phone: str, amount: Decimal, reference: str,
                         callback_url: str, customer_name: str = '') -> dict:
        if not settings.PAYHERO_LIVE_PAYMENTS:
            raise RuntimeError('Live PayHero payments are disabled. Set PAYHERO_LIVE_PAYMENTS=True only for a controlled live payment test.')
        if not self.channel_id:
            raise RuntimeError('PAYHERO_CHANNEL_ID is not configured.')
        if not callback_url:
            raise RuntimeError('PAYHERO_CALLBACK_URL is not configured.')
        if not self.basic_auth_token and not (settings.PAYHERO_USERNAME and settings.PAYHERO_PASSWORD):
            raise RuntimeError('PayHero credentials are not configured.')

        normalized_phone = phone.strip().replace(' ', '').replace('-', '')
        if normalized_phone.startswith('0'):
            normalized_phone = '254' + normalized_phone[1:]
        elif normalized_phone.startswith('+'):
            normalized_phone = normalized_phone[1:]

        payload = {
            'amount': int(amount),
            'phone_number': normalized_phone,
            'channel_id': int(self.channel_id),
            'provider': 'm-pesa',
            'external_reference': reference,
            'customer_name': customer_name,
            'callback_url': callback_url,
        }
        response = requests.post(
            f'{self.base_url}/api/v2/payments?is_active=true',
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        try:
            body = response.json()
        except requests.JSONDecodeError:
            body = {'raw': response.text[:500]}
        if not response.ok:
            raise RuntimeError(f'PayHero initiation failed ({response.status_code}): {body}')
        return body

    def collect(self, *, amount: int, phone: str, external_reference: str, customer_name: str = '') -> dict:
        result = self.initiate_payment(
            phone=phone,
            amount=Decimal(amount),
            reference=external_reference,
            callback_url=settings.PAYHERO_CALLBACK_URL,
            customer_name=customer_name,
        )
        return {
            'providerReference': result.get('provider_reference') or result.get('reference') or '',
            'checkoutRequestId': result.get('checkoutRequestID') or result.get('CheckoutRequestID'),
            'status': 'QUEUED',
            'raw': result,
        }

    def payout(self, *, amount: int, merchant: dict, external_reference: str, narration: str) -> dict:
        raise RuntimeError(
            'Live payout endpoint is not available until PayHero payout/withdraw documentation '
            'is supplied. MockProvider is the supported demo payout.'
        )

    def status(self, reference: str) -> dict:
        if not settings.PAYHERO_LIVE_PAYMENTS:
            return {'status': 'QUEUED', 'raw': {'reference': reference}}
        response = requests.get(
            f'{self.base_url}/transaction-status',
            params={'reference': reference},
            headers=self._headers(),
            timeout=self.timeout,
        )
        try:
            raw = response.json()
        except requests.JSONDecodeError:
            raw = {'raw': response.text[:500]}
        if not response.ok:
            return {'status': 'QUEUED', 'raw': raw}
        normalized = str(raw.get('status') or raw.get('Status') or '').upper()
        status = 'SUCCESS' if normalized in {'SUCCESS','SUCCESSFUL','0'} else ('FAILED' if normalized in {'FAILED','FAILURE','CANCELLED'} else 'QUEUED')
        return {'status': status, 'raw': raw}

    def balance(self) -> dict:
        return {'available': 0, 'raw': {'provider': 'payhero'}}
