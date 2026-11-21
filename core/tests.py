import json
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.test import override_settings
from unittest.mock import patch

from .payhero import PayHeroClient

from .models import Bill, LedgerEntry, PaymentAttempt, PaymentIntent, Reminder, TrustedPayee


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

    def test_mock_wallet_deposit_requires_valid_phone(self):
        response = self.client.post(
            reverse('deposit_api'),
            json.dumps({'amount': 1000, 'phone': '0712345678'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201)
        deposit_id = response.json()['depositId']
        complete = self.client.post(
            reverse('deposit_complete_api', args=[deposit_id]),
            {'receipt': 'TEST-RECEIPT'},
        )
        self.assertEqual(complete.status_code, 200)
        self.assertEqual(complete.json()['balance']['available'], 1000)

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

# Create your tests here.
