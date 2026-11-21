from django.db import migrations


def revoke_mock_wallet_deposits(apps, schema_editor):
    Deposit = apps.get_model('core', 'Deposit')
    LedgerEntry = apps.get_model('core', 'LedgerEntry')
    database = schema_editor.connection.alias

    deposits = Deposit.objects.using(database).filter(provider='mock', status='SUCCESS')
    for deposit in deposits.iterator():
        LedgerEntry.objects.using(database).filter(
            user_id=deposit.user_id,
            type='DEPOSIT',
            ref_type='Deposit',
            ref_id=str(deposit.pk),
            status='POSTED',
        ).update(status='REVERSED')
        deposit.status = 'FAILED'
        deposit.mpesa_receipt = ''
        deposit.raw_callback = {
            **(deposit.raw_callback if isinstance(deposit.raw_callback, dict) else {}),
            'creditRevokedReason': 'Simulated wallet deposits are not real M-Pesa payments.',
        }
        deposit.save(
            using=database,
            update_fields=['status', 'mpesa_receipt', 'raw_callback', 'updated_at'],
        )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_wallet'),
    ]

    operations = [
        migrations.RunPython(revoke_mock_wallet_deposits, migrations.RunPython.noop),
    ]
