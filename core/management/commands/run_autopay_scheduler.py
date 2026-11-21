import time

from django.core.management.base import BaseCommand

from core.tasks import (
    generate_bill_reminders,
    process_due_scheduled_remittances,
    reconcile_stuck_deposits,
    send_due_reminders,
)


class Command(BaseCommand):
    help = 'Run AutoPay reminder and scheduled-remittance jobs every minute.'

    def handle(self, *args, **options):
        self.stdout.write('AutoPay scheduler running; press Ctrl+C to stop.')
        while True:
            generate_bill_reminders()
            sent = send_due_reminders()
            remitted = process_due_scheduled_remittances()
            timed_out = reconcile_stuck_deposits()
            self.stdout.write(
                f'Reminders sent: {sent}; due remittances processed: {remitted}; deposits timed out: {timed_out}'
            )
            time.sleep(60)
