from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from django.db import transaction

from .models import Bill, Deposit, LedgerEntry, Notification, Reminder


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
