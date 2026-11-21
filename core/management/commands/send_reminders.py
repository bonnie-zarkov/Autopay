from django.core.management.base import BaseCommand

from core.tasks import generate_bill_reminders, send_due_reminders


class Command(BaseCommand):
    help = 'Generate upcoming bill reminders and deliver due in-app reminders.'

    def handle(self, *args, **options):
        generated = generate_bill_reminders()
        sent = send_due_reminders()
        self.stdout.write(self.style.SUCCESS(f'Generated {generated}; sent {sent}.'))
