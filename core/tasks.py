from datetime import timedelta
import hashlib
import json
import uuid
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    Bill, Deposit, LedgerEntry, Notification, PaymentAttempt, PaymentIntent,
    Receipt, Reminder, WalletHold,
)


@shared_task
def send_due_reminders():
    now = timezone.now()
    cutoff = now - timedelta(days=1)
    reminders = Reminder.objects.filter(status='PENDING', due_at__lte=now, due_at__gte=cutoff).select_related('bill', 'user')
    sent = 0
    for reminder in reminders:
        profile = reminder.user.profile
        if profile.quiet_hours_start and profile.quiet_hours_end:
            local_now = timezone.localtime()
            if profile.quiet_hours_start <= local_now.time() <= profile.quiet_hours_end:
                continue
        reminder.status = 'SENT'
        reminder.sent_at = now
        reminder.save(update_fields=['status', 'sent_at'])
        sent += 1
    return sent


@shared_task
def generate_bill_reminders():
    now = timezone.now()
    horizon = now + timedelta(days=7)
    created = 0
    for bill in Bill.objects.filter(active=True, due_date__gte=now.date(), due_date__lte=horizon.date()):
        due_at = timezone.make_aware(
            __import__('datetime').datetime.combine(bill.due_date, __import__('datetime').time(8, 0)),
            timezone.get_current_timezone(),
        )
        for lead_days in (7, 1):
            reminder_at = due_at - timedelta(days=lead_days)
            if reminder_at < now:
                continue
            _, was_created = Reminder.objects.get_or_create(
                bill=bill,
                due_at=reminder_at,
                channel='IN_APP',
                defaults={
                    'user': bill.user,
                    'title': f'{bill.name} is due',
                    'message': f'KES {bill.amount} is due on {bill.due_date}.',
                },
            )
            created += int(was_created)
    return created


@shared_task
@transaction.atomic
def reconcile_stuck_deposits():
    now = timezone.now()
    reconciled = 0
    for deposit in Deposit.objects.select_for_update().filter(status='QUEUED'):
        age = now - deposit.created_at
        if age >= timedelta(minutes=10):
            deposit.status = 'TIMEOUT'
            deposit.save(update_fields=['status','updated_at'])
            Notification.objects.create(
                user=deposit.user,
                title='Deposit timed out',
                body=f'Deposit of KSh {deposit.amount} could not be confirmed.',
                channel='IN_APP',
                status='SENT',
            )
            reconciled += 1
    return reconciled


@shared_task
def process_due_scheduled_remittances():
    now = timezone.now()
    due_ids = list(
        PaymentIntent.objects.filter(
            payment_status='SCHEDULED',
            scheduled_for__lte=now,
            bill__active=True,
        ).values_list('id', flat=True)[:100]
    )
    processed = 0
    for intent_id in due_ids:
        with transaction.atomic():
            intent = PaymentIntent.objects.select_for_update().select_related('bill', 'user').filter(id=intent_id).first()
            if not intent or intent.payment_status != 'SCHEDULED':
                continue
            profile = intent.user.profile
            if profile.payments_frozen:
                intent.payment_status = 'BLOCKED'
                intent.authorization_status = 'BLOCKED'
                intent.risk_reasons = (intent.risk_reasons or []) + ['Payments are frozen']
                intent.save(update_fields=['payment_status', 'authorization_status', 'risk_reasons'])
                Notification.objects.create(user=intent.user, title='Scheduled payment stopped', body=f'{intent.bill.name} was not paid because payments are frozen.', status='SENT')
                processed += 1
                continue
            if intent.authorization_status != 'AUTHORIZED':
                intent.payment_status = 'AWAITING_CONFIRMATION'
                intent.authorization_status = 'CONFIRMATION_REQUIRED'
                intent.save(update_fields=['payment_status', 'authorization_status'])
                Notification.objects.create(
                    user=intent.user,
                    title='Bill payment needs approval',
                    body=f"Confirm KSh {intent.amount} for {intent.bill.name} before it can be remitted.",
                    status='SENT',
                    actions=[{'label': 'Review payment', 'url': f'/intents/{intent.id}/confirm/'}],
                )
                processed += 1
                continue
            if settings.PAYHERO_LIVE_PAYMENTS:
                intent.payment_status = 'FAILED'
                intent.risk_reasons = (intent.risk_reasons or []) + ['Live payout is unavailable until PayHero payout documentation is configured']
                intent.save(update_fields=['payment_status', 'risk_reasons'])
                Notification.objects.create(
                    user=intent.user,
                    title='Scheduled remittance was not sent',
                    body='Live payout is not configured. No money was transferred.',
                    status='SENT',
                )
                processed += 1
                continue
            posted = sum(LedgerEntry.objects.filter(
                user=intent.user,
                status='POSTED',
                type__in=['DEPOSIT', 'PAYMENT', 'REFUND', 'FEE'],
            ).values_list('amount', flat=True))
            held = sum(WalletHold.objects.filter(user=intent.user, active=True).values_list('amount', flat=True))
            if posted - held < int(intent.amount):
                intent.payment_status = 'FAILED'
                intent.risk_reasons = (intent.risk_reasons or []) + ['Insufficient wallet funds']
                intent.save(update_fields=['payment_status', 'risk_reasons'])
                Notification.objects.create(
                    user=intent.user,
                    title='Insufficient wallet funds',
                    body=f"{intent.bill.name} was not remitted. Top up KSh {max(int(intent.amount) - (posted - held), 0)}.",
                    status='SENT',
                    actions=[{'label': 'Open wallet', 'url': '/wallet/'}],
                )
                processed += 1
                continue
            amount = int(intent.amount)
            hold = WalletHold.objects.create(user=intent.user, payment_intent=intent, amount=amount)
            LedgerEntry.objects.create(user=intent.user, type='HOLD', amount=amount, ref_type='PaymentIntent', ref_id=str(intent.id), status='POSTED')
            reference = f"MOCK-{intent.intent_id}"
            request_payload = {
                'amount': amount,
                'phone': intent.phone,
                'reference': intent.reference,
                'intent_id': intent.intent_id,
            }
            PaymentAttempt.objects.create(
                payment_intent=intent,
                provider='mock',
                provider_reference=reference,
                amount=intent.amount,
                status='SUCCESS',
                request_payload_hash=hashlib.sha256(json.dumps(request_payload, sort_keys=True).encode()).hexdigest(),
                response_payload={'mode': 'mock', 'status': 'SUCCESS', 'note': 'No external transfer occurred'},
            )
            intent.payment_status = 'SUCCESS'
            intent.executed_at = now
            intent.save(update_fields=['payment_status', 'executed_at'])
            LedgerEntry.objects.create(user=intent.user, type='PAYMENT', amount=-amount, ref_type='PaymentIntent', ref_id=str(intent.id), status='POSTED')
            hold.active = False
            hold.save(update_fields=['active'])
            LedgerEntry.objects.create(user=intent.user, type='RELEASE', amount=amount, ref_type='PaymentIntent', ref_id=str(intent.id), status='POSTED')
            receipt_number = f"AP-{now.strftime('%Y%m%d')}-{intent.id:04d}"
            receipt_dir = Path(settings.BASE_DIR) / 'receipts' / str(intent.user_id)
            receipt_dir.mkdir(parents=True, exist_ok=True)
            receipt_path = receipt_dir / f'{receipt_number}.json'
            receipt_path.write_text(json.dumps({
                'number': receipt_number,
                'payment': intent.intent_id,
                'merchant': intent.recipient,
                'amount': amount,
                'scheduledFor': intent.scheduled_for.isoformat() if intent.scheduled_for else None,
                'executedAt': now.isoformat(),
                'provider': 'mock',
                'note': 'Simulation only; no external transfer occurred.',
            }, indent=2))
            Receipt.objects.get_or_create(
                payment_intent=intent,
                defaults={'number': receipt_number, 'json_path': str(receipt_path)},
            )
            Notification.objects.create(
                user=intent.user,
                title='Mock remittance completed',
                body=f"KSh {amount} for {intent.bill.name} was processed in the demo ledger only; no external transfer occurred.",
                status='SENT',
            )
            processed += 1
    return processed
