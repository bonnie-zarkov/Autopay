from decimal import Decimal
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def create_wallet_reference():
    return f"APW-{secrets.token_hex(5).upper()}"


class Wallet(models.Model):
    STATUS_CHOICES = [('ACTIVE', 'Active'), ('FROZEN', 'Frozen'), ('CLOSED', 'Closed')]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wallet_account')
    reference = models.CharField(max_length=20, unique=True, default=create_wallet_reference)
    currency = models.CharField(max_length=3, default='KES')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='ACTIVE')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.reference} ({self.user.get_username()})'


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    phone = models.CharField(max_length=20, blank=True)
    payments_frozen = models.BooleanField(default=False)
    auto_pay_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('10000.00'))
    high_value_threshold = models.PositiveIntegerField(default=10000)
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    frozen_at = models.DateTimeField(null=True, blank=True)
    pin_hash = models.CharField(max_length=128, blank=True)
    household = models.ForeignKey('Household', null=True, blank=True, on_delete=models.SET_NULL, related_name='members_direct')

    def __str__(self):
        return f'{self.user.get_username()} profile'


class Bill(models.Model):
    CATEGORIES = [
        ('RENT', 'Rent'), ('SCHOOL', 'School'), ('HOSPITAL', 'Hospital'),
        ('UTILITY', 'Utility'), ('INTERNET', 'Internet'), ('AIRTIME', 'Airtime'),
        ('INSURANCE', 'Insurance'), ('LOAN', 'Loan'), ('TAX', 'Tax'),
        ('SUPPLIER', 'Supplier'), ('OTHER', 'Other'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bills')
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=CATEGORIES, default='OTHER')
    merchant_name = models.CharField(max_length=120)
    merchant_phone = models.CharField(max_length=20)
    account_number = models.CharField(max_length=100, blank=True)
    reference = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    due_date = models.DateField()
    frequency = models.CharField(max_length=20, default='monthly')
    auto_pay_enabled = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    day_of_month = models.PositiveSmallIntegerField(null=True, blank=True)
    day_of_week = models.PositiveSmallIntegerField(null=True, blank=True)
    next_due_at = models.DateTimeField(null=True, blank=True)
    reminder_offsets_min = models.JSONField(default=list, blank=True)
    escalation = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['due_date', 'name']

    def __str__(self):
        return f'{self.name} - KES {self.amount}'


class TrustedPayee(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='payees')
    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20)
    account_reference = models.CharField(max_length=100)
    verification_status = models.CharField(max_length=20, default='VERIFIED')
    last_used = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('user', 'phone', 'account_reference')

    def __str__(self):
        return self.name


class PaymentIntent(models.Model):
    AUTH_CHOICES = [('CREATED', 'Created'), ('AUTHORIZED', 'Authorized'), ('CONFIRMATION_REQUIRED', 'Confirmation required'), ('BLOCKED', 'Blocked'), ('EXPIRED', 'Expired'), ('CANCELLED', 'Cancelled')]
    STATUS_CHOICES = [('CREATED', 'Created'), ('SCHEDULED','Scheduled'), ('EVALUATING','Evaluating'), ('AWAITING_CONFIRMATION','Awaiting Confirmation'), ('EXECUTING','Executing'), ('SUBMITTED', 'Submitted'), ('PENDING', 'Pending'), ('SUCCESS', 'Success'), ('FAILED', 'Failed'), ('RETRY_SCHEDULED','Retry Scheduled'), ('TIMEOUT', 'Timeout'), ('BLOCKED', 'Blocked'), ('CANCELLED','Cancelled'), ('EXPIRED','Expired')]
    LEVEL_CHOICES = [('GREEN', 'Green'), ('YELLOW', 'Yellow'), ('RED', 'Red')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='payment_intents')
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, related_name='intents')
    intent_id = models.CharField(max_length=40, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    recipient = models.CharField(max_length=120)
    phone = models.CharField(max_length=20)
    reference = models.CharField(max_length=100)
    reason = models.CharField(max_length=200)
    risk_score = models.PositiveSmallIntegerField(default=0)
    risk_level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='GREEN')
    risk_reasons = models.JSONField(default=list, blank=True)
    authorization_status = models.CharField(max_length=30, choices=AUTH_CHOICES, default='CREATED')
    payment_status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='CREATED')
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.intent_id

    def transition(self, new_status):
        terminal = {'SUCCESS','FAILED','CANCELLED','BLOCKED','TIMEOUT','EXPIRED'}
        allowed = {
            'CREATED': {'EVALUATING','CANCELLED','BLOCKED'},
            'EVALUATING': {'EXECUTING','AWAITING_CONFIRMATION','BLOCKED','CANCELLED','FAILED'},
            'AWAITING_CONFIRMATION': {'EXECUTING','CANCELLED','EXPIRED'},
            'EXECUTING': {'SUCCESS','FAILED','CANCELLED','BLOCKED'},
            'FAILED': {'RETRY_SCHEDULED','EVALUATING','CANCELLED'},
            'RETRY_SCHEDULED': {'EVALUATING','CANCELLED'},
            'BLOCKED': {'CANCELLED','EVALUATING'},
        }
        if new_status in terminal:
            return True
        if new_status not in allowed.get(self.payment_status, set()):
            raise ValueError(f'Illegal payment transition: {self.payment_status} -> {new_status}')
        self.payment_status = new_status
        self.save(update_fields=['payment_status'])
        return True


class PaymentAttempt(models.Model):
    payment_intent = models.ForeignKey(PaymentIntent, on_delete=models.PROTECT, related_name='attempts')
    provider = models.CharField(max_length=40, default='mock')
    provider_reference = models.CharField(max_length=100, blank=True)
    checkout_reference = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, default='PENDING')
    request_payload_hash = models.CharField(max_length=64)
    response_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['provider', 'provider_reference'], condition=~models.Q(provider_reference=''), name='unique_provider_reference'),
        ]


class AuditEvent(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='audit_events')
    event_type = models.CharField(max_length=80)
    actor = models.CharField(max_length=80, default='system')
    object_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=80)
    previous_state = models.CharField(max_length=40, blank=True)
    new_state = models.CharField(max_length=40, blank=True)
    reason = models.TextField(blank=True)
    risk_score = models.PositiveSmallIntegerField(default=0)
    ip_hash = models.CharField(max_length=64, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    prev_hash = models.CharField(max_length=64, blank=True)
    hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-timestamp']


class Reminder(models.Model):
    CHANNELS = [('IN_APP', 'In app'), ('EMAIL', 'Email'), ('SMS', 'SMS')]
    STATUSES = [('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed'), ('CANCELLED', 'Cancelled')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reminders')
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name='reminders')
    due_at = models.DateTimeField()
    channel = models.CharField(max_length=20, choices=CHANNELS, default='IN_APP')
    status = models.CharField(max_length=20, choices=STATUSES, default='PENDING')
    title = models.CharField(max_length=160)
    message = models.TextField()
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['due_at']
        constraints = [
            models.UniqueConstraint(
                fields=['bill', 'due_at', 'channel'],
                name='unique_bill_reminder',
            ),
        ]


class Household(models.Model):
    name = models.CharField(max_length=120)
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='admin_households')
    approval_threshold = models.PositiveIntegerField(default=10000)
    created_at = models.DateTimeField(auto_now_add=True)


class HouseholdMember(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='household_memberships')
    role = models.CharField(max_length=20, choices=[('ADMIN','Admin'),('MEMBER','Member')], default='MEMBER')
    spend_limit = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        unique_together = ('household', 'user')


class Merchant(models.Model):
    KINDS = [('RENT','Rent'),('HOSPITAL','Hospital'),('AIRTIME','Airtime'),('SCHOOL','School'),('UTILITY','Utility'),('INSURANCE','Insurance'),('SUPPLIER','Supplier'),('OTHER','Other')]
    PAYOUT_TYPES = [('MOBILE','Mobile'),('PAYBILL','Paybill'),('TILL','Till'),('BANK','Bank')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='merchants')
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=KINDS, default='OTHER')
    payout_type = models.CharField(max_length=20, choices=PAYOUT_TYPES, default='MOBILE')
    payout_account = models.CharField(max_length=100)
    account_ref = models.CharField(max_length=100, blank=True)
    trusted = models.BooleanField(default=False)
    blacklisted = models.BooleanField(default=False)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_paid_at = models.DateTimeField(null=True, blank=True)
    clean_successes = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('user', 'name', 'payout_account')


class LedgerEntry(models.Model):
    TYPES = [('DEPOSIT','Deposit'),('PAYMENT','Payment'),('REFUND','Refund'),('FEE','Fee'),('HOLD','Hold'),('RELEASE','Release')]
    STATUSES = [('PENDING','Pending'),('POSTED','Posted'),('REVERSED','Reversed')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ledger_entries')
    household = models.ForeignKey(Household, null=True, blank=True, on_delete=models.PROTECT)
    type = models.CharField(max_length=20, choices=TYPES)
    amount = models.IntegerField()
    ref_type = models.CharField(max_length=40)
    ref_id = models.CharField(max_length=80)
    status = models.CharField(max_length=20, choices=STATUSES, default='POSTED')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class WalletHold(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wallet_holds')
    payment_intent = models.OneToOneField(PaymentIntent, on_delete=models.CASCADE, related_name='wallet_hold')
    amount = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Deposit(models.Model):
    STATUSES = [('QUEUED','Queued'),('SUCCESS','Success'),('FAILED','Failed'),('CANCELLED','Cancelled'),('TIMEOUT','Timeout')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='deposits')
    amount = models.PositiveIntegerField()
    phone = models.CharField(max_length=20)
    external_reference = models.CharField(max_length=80, unique=True)
    payhero_reference = models.CharField(max_length=100, blank=True)
    checkout_request_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='QUEUED')
    provider = models.CharField(max_length=20, default='mock')
    mpesa_receipt = models.CharField(max_length=100, blank=True)
    raw_callback = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Invoice(models.Model):
    SOURCES = [('MANUAL','Manual'),('SMS','SMS'),('EMAIL','Email'),('OCR','OCR')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='invoices')
    merchant = models.ForeignKey(Merchant, null=True, blank=True, on_delete=models.SET_NULL)
    bill = models.ForeignKey(Bill, null=True, blank=True, on_delete=models.SET_NULL)
    invoice_number = models.CharField(max_length=100, blank=True)
    amount = models.PositiveIntegerField()
    due_date = models.DateField(null=True, blank=True)
    account_ref = models.CharField(max_length=100, blank=True)
    source = models.CharField(max_length=20, choices=SOURCES, default='MANUAL')
    raw_text = models.TextField(blank=True)
    parsed_json = models.JSONField(default=dict, blank=True)
    verified = models.BooleanField(default=False)
    verify_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Confirmation(models.Model):
    STATUSES = [('PENDING','Pending'),('APPROVED','Approved'),('REJECTED','Rejected'),('EXPIRED','Expired')]
    payment_intent = models.ForeignKey(PaymentIntent, on_delete=models.CASCADE, related_name='confirmations')
    token_hash = models.CharField(max_length=64)
    otp_hash = models.CharField(max_length=64)
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUSES, default='PENDING')
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    decided_at = models.DateTimeField(null=True, blank=True)


class Notification(models.Model):
    CHANNELS = [('IN_APP','In app'),('SMS','SMS'),('EMAIL','Email'),('PUSH','Push'),('CALL','Call')]
    STATUSES = [('PENDING','Pending'),('SENT','Sent'),('ACKED','Acked'),('FAILED','Failed')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=160)
    body = models.TextField()
    channel = models.CharField(max_length=20, choices=CHANNELS, default='IN_APP')
    status = models.CharField(max_length=20, choices=STATUSES, default='PENDING')
    actions = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    acked_at = models.DateTimeField(null=True, blank=True)


class Receipt(models.Model):
    payment_intent = models.OneToOneField(PaymentIntent, on_delete=models.PROTECT, related_name='receipt')
    number = models.CharField(max_length=40, unique=True)
    json_path = models.CharField(max_length=300, blank=True)
    pdf_path = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ForecastSnapshot(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='forecast_snapshots')
    horizon_days = models.PositiveIntegerField()
    projected_outflow = models.PositiveIntegerField()
    projected_balance = models.IntegerField()
    shortfall = models.IntegerField()
    timeline = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AgentFeedback(models.Model):
    payment_intent = models.ForeignKey(PaymentIntent, on_delete=models.CASCADE)
    agent_decision = models.CharField(max_length=40)
    user_decision = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)


class RuleWeight(models.Model):
    rule_id = models.CharField(max_length=40, unique=True)
    points = models.PositiveSmallIntegerField()
    updated_at = models.DateTimeField(auto_now=True)

# Create your models here.
