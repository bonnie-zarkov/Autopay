import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.test import override_settings
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from django.utils import timezone

from config.settings import _allowed_hosts_from_environment
from .payhero import PayHeroClient

from .models import Bill, Deposit, LedgerEntry, PaymentAttempt, PaymentIntent, Reminder, TrustedPayee
from .tasks import process_due_scheduled_remittances


class DeploymentHostTests(SimpleTestCase):
    def test_allowed_hosts_include_configured_and_vercel_domains(self):
        hosts = _allowed_hosts_from_environment({
            'ALLOWED_HOSTS': 'autopay.example.com, api.autopay.example.com ',
            'VERCEL_URL': 'autopay-preview.vercel.app',
            'VERCEL_PROJECT_PRODUCTION_URL': 'autopay.example.com',
            'VERCEL_BRANCH_URL': 'feature-autopay.vercel.app',
        })
        self.assertEqual(hosts, [
            '127.0.0.1',
            'localhost',
            'autopay-2nkb.vercel.app',
            'autopay-2nkb-eu9c9fwah-autopay1.vercel.app',
            'autopay.eucossahacks.xyz',
            'autopay.example.com',
            'api.autopay.example.com',
            'autopay-preview.vercel.app',
            'feature-autopay.vercel.app',
        ])


class StaticAssetTests(SimpleTestCase):
    @override_settings(DEBUG=False, ALLOWED_HOSTS=['testserver'], WHITENOISE_USE_FINDERS=True)
    def test_dashboard_css_uses_absolute_url_and_is_served(self):
        client = Client()
        page = client.get(reverse('login'))
        self.assertContains(page, 'href="/static/css/app.css"')

        stylesheet = client.get('/static/css/app.css')
        self.assertEqual(stylesheet.status_code, 200)
        self.assertTrue(stylesheet['Content-Type'].startswith('text/css'))


class AutoPayFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='demo', password='safe-password-123')
        self.client.login(username='demo', password='safe-password-123')
        self.bill = Bill.objects.create(
            user=self.user, name='December rent', category='RENT',
            merchant_name='John Kamau', merchant_phone='254712345678',
            reference='RENT-DEC', amount=Decimal('15000'),
            due_date=date(2026, 12, 1),
        )
        TrustedPayee.objects.create(
            user=self.user, name='John Kamau', phone='254712345678',
            account_reference='RENT-DEC',
        )
        LedgerEntry.objects.create(
            user=self.user,
            type='DEPOSIT',
            amount=50000,
            ref_type='DemoSeed',
            ref_id='SEED',
            status='POSTED',
        )

    def create_intent(self, amount='15000', phone='254712345678'):
        response = self.client.post(
            reverse('create_intent', args=[self.bill.id]),
            {'amount': amount, 'phone': phone, 'recipient': 'John Kamau'},
        )
        self.assertEqual(response.status_code, 302)
        return PaymentIntent.objects.latest('id')

    def test_normal_payment_is_authorized(self):
        intent = self.create_intent()
        self.assertEqual(intent.risk_level, 'GREEN')
        self.assertEqual(intent.authorization_status, 'AUTHORIZED')

    def test_modified_amount_is_yellow_or_red(self):
        intent = self.create_intent(amount='22500')
        self.assertIn(intent.risk_level, {'YELLOW', 'RED'})
        self.assertNotEqual(intent.authorization_status, 'AUTHORIZED')

    def test_new_recipient_is_red(self):
        intent = self.create_intent(phone='254700000000')
        self.assertEqual(intent.risk_level, 'RED')
        self.assertEqual(intent.authorization_status, 'BLOCKED')

    def test_freeze_blocks_green_payment(self):
        self.user.profile.payments_frozen = True
        self.user.profile.save(update_fields=['payments_frozen'])
        intent = self.create_intent()
        self.assertEqual(intent.authorization_status, 'BLOCKED')

    def test_duplicate_webhook_is_processed_once(self):
        intent = self.create_intent()
        self.client.post(reverse('simulate_payment', args=[intent.id]))
        attempt = PaymentAttempt.objects.get(payment_intent=intent)
        payload = {
            'provider_reference': attempt.provider_reference,
            'intent_id': intent.intent_id,
            'status': 'SUCCESS',
        }
        first = self.client.post(
            reverse('payhero_webhook'), json.dumps(payload),
            content_type='application/json',
        )
        second = self.client.post(
            reverse('payhero_webhook'), json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json()['duplicate'], True)
        intent.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(intent.payment_status, 'SUCCESS')
        self.assertEqual(attempt.status, 'SUCCESS')

    def test_bill_creation_generates_reminders(self):
        response = self.client.post(reverse('create_bill'), {
            'name': 'Internet',
            'category': 'INTERNET',
            'merchant_name': 'Safaricom',
            'merchant_phone': '254700000001',
            'account_number': '1001',
            'reference': 'NET-1001',
            'amount': '3000',
            'due_date': '2026-12-10',
            'frequency': 'monthly',
        })
        self.assertEqual(response.status_code, 302)
        bill = Bill.objects.get(reference='NET-1001')
        self.assertTrue(Reminder.objects.filter(bill=bill).exists())

    def test_changed_recipient_name_is_blocked(self):
        intent = self.create_intent(phone='254712345678')
        intent.recipient = 'Unknown Person'
        intent.save(update_fields=['recipient'])
        bill = intent.bill
        score, level, reasons = __import__('core.views', fromlist=['assess_risk']).assess_risk(
            type('Request', (), {'user': self.user, 'user_profile': self.user.profile})(),
            bill,
            intent.amount,
            'Unknown Person',
            intent.phone,
        )
        self.assertIn('Recipient name differs', '; '.join(reasons))


class PayHeroAdapterTests(TestCase):
    @override_settings(PAYHERO_LIVE_PAYMENTS=False)
    def test_live_mode_is_disabled_by_default(self):
        client = PayHeroClient()
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            client.initiate_payment(
                phone='0712345678',
                amount=Decimal('100'),
                reference='AP-TEST',
                callback_url='https://example.com/callback',
            )

    @override_settings(
        PAYHERO_LIVE_PAYMENTS=True,
        PAYHERO_CHANNEL_ID='15692',
        PAYHERO_CALLBACK_URL='https://example.com/callback',
        PAYHERO_BASIC_AUTH_TOKEN='Basic test-token',
    )
    @patch('core.payhero.requests.post')
    def test_stk_payload_is_normalized_and_submitted(self, post):
        post.return_value.ok = True
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            'provider_reference': 'PH-123',
            'status': 'PENDING',
        }

        result = PayHeroClient().initiate_payment(
            phone='0712345678',
            amount=Decimal('100'),
            reference='AP-TEST',
            callback_url='https://example.com/callback',
            customer_name='John Doe',
        )

        self.assertEqual(result['provider_reference'], 'PH-123')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['phone_number'], '254712345678')
        self.assertEqual(payload['channel_id'], 15692)
        self.assertEqual(payload['external_reference'], 'AP-TEST')


class ExpandedFeatureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='feature', password='feature-password')
        self.client.login(username='feature', password='feature-password')

    @override_settings(PAYHERO_LIVE_PAYMENTS=False)
    def test_wallet_deposit_fails_when_live_payhero_is_disabled(self):
        response = self.client.post(
            reverse('deposit_api'),
            json.dumps({'amount': 1000, 'phone': '0712345678'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'PAYHERO_NOT_READY')
        self.assertFalse(Deposit.objects.exists())

    @override_settings(PAYHERO_LIVE_PAYMENTS=False)
    def test_wallet_page_hides_top_up_form_until_payhero_is_ready(self):
        response = self.client.get(reverse('wallet'))
        self.assertContains(response, 'Real wallet top-ups are unavailable')
        self.assertNotContains(response, 'Pay with M-Pesa STK')
        self.assertNotContains(response, 'Complete mock')

    @override_settings(
        PAYHERO_LIVE_PAYMENTS=True,
        PAYHERO_CALLBACK_URL='https://autopay.example.com/api/webhooks/payhero/',
        PAYHERO_CHANNEL_ID='123',
        PAYHERO_BASIC_AUTH_TOKEN='test-token',
        PAYHERO_CALLBACK_SECRET='',
    )
    def test_wallet_deposit_requires_callback_authentication(self):
        response = self.client.post(
            reverse('deposit_api'),
            json.dumps({'amount': 1000, 'phone': '0712345678'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Deposit.objects.exists())

    @override_settings(
        PAYHERO_LIVE_PAYMENTS=True,
        PAYHERO_CALLBACK_URL='https://autopay.example.com/api/webhooks/payhero/',
        PAYHERO_CHANNEL_ID='123',
        PAYHERO_BASIC_AUTH_TOKEN='test-token',
        PAYHERO_CALLBACK_SECRET='test-callback-secret',
    )
    @patch('core.views.PayHeroClient.collect')
    def test_wallet_deposit_starts_payhero_stk(self, collect):
        collect.return_value = {
            'providerReference': 'PH-DEP-1',
            'checkoutRequestId': 'CHECKOUT-1',
        }
        response = self.client.post(
            reverse('deposit_api'),
            json.dumps({'amount': 1000, 'phone': '0712345678'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        deposit = Deposit.objects.get()
        self.assertEqual(deposit.provider, 'payhero')
        self.assertEqual(deposit.status, 'QUEUED')
        self.assertEqual(deposit.payhero_reference, 'PH-DEP-1')
        collect.assert_called_once()

    @override_settings(
        PAYHERO_LIVE_PAYMENTS=True,
        PAYHERO_CALLBACK_SECRET='test-callback-secret',
    )
    @patch('core.views.PayHeroClient.status', return_value={'status': 'SUCCESS'})
    def test_verified_payhero_callback_credits_wallet_once(self, status):
        deposit = Deposit.objects.create(
            user=self.user, amount=1000, phone='254712345678',
            external_reference='dep_verified_test', payhero_reference='PH-DEP-2',
            provider='payhero',
        )
        payload = {
            'ExternalReference': deposit.external_reference,
            'provider_reference': deposit.payhero_reference,
            'ResultCode': '0',
            'Amount': 1000,
            'MpesaReceiptNumber': 'QWERTY123',
        }
        first = self.client.post(
            reverse('payhero_webhook'), json.dumps(payload),
            content_type='application/json',
            HTTP_X_PAYHERO_SECRET='test-callback-secret',
        )
        second = self.client.post(
            reverse('payhero_webhook'), json.dumps(payload),
            content_type='application/json',
            HTTP_X_PAYHERO_SECRET='test-callback-secret',
        )
        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.json()['duplicate'])
        self.assertEqual(LedgerEntry.objects.filter(ref_type='Deposit', ref_id=str(deposit.id), status='POSTED').count(), 1)
        status.assert_called_once_with('PH-DEP-2')

    @override_settings(PAYHERO_LIVE_PAYMENTS=False)
    def test_legacy_mock_completion_cannot_credit_wallet(self):
        deposit = Deposit.objects.create(
            user=self.user, amount=1000, phone='254712345678',
            external_reference='dep_mock_test', provider='mock',
        )
        response = self.client.post(reverse('deposit_complete_api', args=[deposit.id]))
        self.assertEqual(response.status_code, 410)
        self.assertFalse(LedgerEntry.objects.filter(ref_type='Deposit', ref_id=str(deposit.id)).exists())

    @override_settings(
        PAYHERO_LIVE_PAYMENTS=True,
        PAYHERO_CALLBACK_SECRET='test-callback-secret',
    )
    @patch('core.views.PayHeroClient.status', return_value={'status': 'SUCCESS'})
    def test_payhero_callback_with_wrong_amount_does_not_credit_wallet(self, status):
        deposit = Deposit.objects.create(
            user=self.user, amount=1000, phone='254712345678',
            external_reference='dep_amount_test', provider='payhero',
        )
        response = self.client.post(
            reverse('payhero_webhook'),
            json.dumps({
                'ExternalReference': deposit.external_reference,
                'ResultCode': '0',
                'Amount': 999,
            }),
            content_type='application/json',
            HTTP_X_PAYHERO_SECRET='test-callback-secret',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LedgerEntry.objects.filter(ref_type='Deposit', ref_id=str(deposit.id)).exists())
        status.assert_not_called()

    def test_chat_freeze_is_safe_and_deterministic(self):
        response = self.client.post(
            reverse('chat_api'),
            json.dumps({'text': 'freeze everything'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['intent'], 'FREEZE')
        self.user.profile.refresh_from_db()
        self.assertTrue(self.user.profile.payments_frozen)

    def test_bearer_login_and_me(self):
        login = self.client.post(
            reverse('auth_login_api'),
            json.dumps({'username': 'feature', 'password': 'feature-password'}),
            content_type='application/json',
        )
        self.assertEqual(login.status_code, 200)
        token = login.json()['token']
        me = self.client.get(
            reverse('me_api'),
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()['phone'], '')

    def test_user_has_active_wallet_reference(self):
        wallet = self.user.wallet_account
        self.assertTrue(wallet.reference.startswith('APW-'))
        self.assertEqual(wallet.currency, 'KES')
        self.assertEqual(wallet.status, 'ACTIVE')


class ScheduledRemittanceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='scheduled', password='scheduled-password')
        self.bill = Bill.objects.create(
            user=self.user, name='Rent', category='RENT',
            merchant_name='Landlord', merchant_phone='254712345678',
            reference='RENT-1', amount=Decimal('6000'),
            due_date=timezone.localdate(), auto_pay_enabled=True,
        )
        LedgerEntry.objects.create(
            user=self.user, type='DEPOSIT', amount=10000,
            ref_type='Test', ref_id='FUNDS', status='POSTED',
        )

    @override_settings(PAYHERO_LIVE_PAYMENTS=False)
    def test_due_remittance_debits_wallet_once(self):
        due = timezone.now() - timedelta(minutes=1)
        intent = PaymentIntent.objects.create(
            user=self.user, bill=self.bill,
            intent_id='AP-SCHEDULED-1', amount=Decimal('6000'),
            recipient='Landlord', phone='254712345678',
            reference='RENT-1', reason='Scheduled rent',
            authorization_status='AUTHORIZED',
            payment_status='SCHEDULED',
            scheduled_for=due,
            expires_at=due + timedelta(hours=1),
        )
        with TemporaryDirectory() as temp_dir, override_settings(BASE_DIR=Path(temp_dir)):
            self.assertEqual(process_due_scheduled_remittances(), 1)
            self.assertEqual(process_due_scheduled_remittances(), 0)
        intent.refresh_from_db()
        self.assertEqual(intent.payment_status, 'SUCCESS')
        self.assertEqual(PaymentAttempt.objects.filter(payment_intent=intent).count(), 1)
        from core.views import posted_balance
        self.assertEqual(posted_balance(self.user)['available'], 4000)

    def test_scheduled_remittance_waits_for_confirmation_without_opt_in(self):
        self.bill.auto_pay_enabled = False
        self.bill.save(update_fields=['auto_pay_enabled'])
        due = timezone.now() - timedelta(minutes=1)
        intent = PaymentIntent.objects.create(
            user=self.user, bill=self.bill,
            intent_id='AP-SCHEDULED-2', amount=Decimal('6000'),
            recipient='Landlord', phone='254712345678',
            reference='RENT-1', reason='Scheduled rent',
            authorization_status='CREATED',
            payment_status='SCHEDULED',
            scheduled_for=due,
            expires_at=due + timedelta(hours=1),
        )
        self.assertEqual(process_due_scheduled_remittances(), 1)
        intent.refresh_from_db()
        self.assertEqual(intent.payment_status, 'AWAITING_CONFIRMATION')
        from core.views import posted_balance
        self.assertEqual(posted_balance(self.user)['available'], 10000)

# Create your tests here.
