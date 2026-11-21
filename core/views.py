from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid
import hmac
import re
import calendar
import csv
from pathlib import Path
from io import StringIO
from datetime import datetime, date as date_type, time, timedelta

import requests

from django.contrib import messages
from django.contrib.auth import authenticate as django_authenticate
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.core import signing
from django.contrib.auth import get_user_model
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (
    AgentFeedback, AuditEvent, Bill, Confirmation, Deposit, ForecastSnapshot,
    Household, HouseholdMember, Invoice, LedgerEntry, Merchant, Notification,
    PaymentAttempt, PaymentIntent, Receipt, Reminder, RuleWeight, TrustedPayee, WalletHold,
)
from .payhero import PayHeroClient
from .tasks import generate_bill_reminders, send_due_reminders


def audit(user, event_type, obj, previous='', new='', reason='', risk=0, actor='user', request=None):
    ip = request.META.get('REMOTE_ADDR', '') if request else ''
    ip_hash = hashlib.sha256(ip.encode()).hexdigest() if ip else ''
    previous_event = AuditEvent.objects.filter(user=user).order_by('-timestamp', '-id').first()
    prev_hash = previous_event.hash if previous_event else ''
    timestamp = timezone.now().isoformat()
    payload = {
        'ts': timestamp,
        'actor': actor,
        'userId': user.id,
        'action': event_type,
        'entityType': obj.__class__.__name__,
        'entityId': str(obj.pk),
        'previousState': previous,
        'newState': new,
        'reasonText': reason,
        'riskScore': risk,
    }
    chain_hash = hashlib.sha256((prev_hash + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
    AuditEvent.objects.create(
        user=user, event_type=event_type, actor=actor,
        object_type=obj.__class__.__name__, object_id=str(obj.pk),
        previous_state=previous, new_state=new, reason=reason,
        risk_score=risk, ip_hash=ip_hash,
        prev_hash=prev_hash,
        hash=chain_hash,
    )


def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            return redirect('dashboard')
    else:
        form = AuthenticationForm()
    return render(request, 'core/auth.html', {'form': form, 'mode': 'login'})


def signup_view(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect('dashboard')
    else:
        form = UserCreationForm()
    return render(request, 'core/auth.html', {'form': form, 'mode': 'signup'})


@login_required
def logout_view(request):
    logout(request)
    return redirect('login')


@login_required
def dashboard(request):
    bills = Bill.objects.filter(user=request.user, active=True)
    intents = PaymentIntent.objects.filter(user=request.user).select_related('bill')[:20]
    return render(request, 'core/dashboard.html', {
        'bills': bills,
        'intents': intents,
        'profile': request.user.profile,
        'today': timezone.localdate(),
        'reminders': Reminder.objects.filter(user=request.user, status='PENDING')[:5],
        'monthly_bills': sum((bill.amount for bill in bills), Decimal('0')),
    })


@login_required
def reminders(request):
    items = Reminder.objects.filter(user=request.user).select_related('bill')
    return render(request, 'core/reminders.html', {'reminders': items})


@login_required
@require_POST
def toggle_freeze(request):
    profile = request.user.profile
    profile.payments_frozen = not profile.payments_frozen
    profile.save(update_fields=['payments_frozen'])
    audit(request.user, 'PAYMENT_FREEZE_CHANGED', profile, reason='Payments frozen' if profile.payments_frozen else 'Payments unfrozen', request=request)
    messages.success(request, 'Payments frozen.' if profile.payments_frozen else 'Payments unfrozen.')
    return redirect('dashboard')


@login_required
def create_bill(request):
    if request.method == 'POST':
        try:
            amount = Decimal(request.POST['amount'])
            due_date = date_type.fromisoformat(request.POST['due_date'])
        except (KeyError, InvalidOperation):
            messages.error(request, 'Enter a valid amount.')
            return redirect('create_bill')
        except ValueError:
            messages.error(request, 'Enter a valid due date.')
            return redirect('create_bill')
        bill = Bill.objects.create(
            user=request.user,
            name=request.POST['name'].strip(),
            category=request.POST.get('category', 'OTHER'),
            merchant_name=request.POST['merchant_name'].strip(),
            merchant_phone=request.POST['merchant_phone'].strip(),
            account_number=request.POST.get('account_number', '').strip(),
            reference=request.POST.get('reference', '').strip(),
            amount=amount,
            due_date=due_date,
            frequency=request.POST.get('frequency', 'monthly'),
            day_of_month=due_date.day,
            next_due_at=timezone.make_aware(
                datetime.combine(due_date, time(8, 0)),
                timezone.get_current_timezone(),
            ),
            reminder_offsets_min=[4320, 60],
        )
        TrustedPayee.objects.get_or_create(
            user=request.user,
            name=bill.merchant_name,
            phone=bill.merchant_phone,
            account_reference=bill.reference or bill.account_number,
        )
        due_at = timezone.make_aware(
            datetime.combine(bill.due_date, time(8, 0)),
            timezone.get_current_timezone(),
        )
        for lead_days in (7, 1):
            reminder_at = due_at - timedelta(days=lead_days)
            if reminder_at >= timezone.now():
                Reminder.objects.get_or_create(
                    bill=bill,
                    due_at=reminder_at,
                    channel='IN_APP',
                    defaults={
                        'user': request.user,
                        'title': f'{bill.name} is due',
                        'message': f'KES {bill.amount} is due on {bill.due_date}.',
                    },
                )
        PaymentIntent.objects.get_or_create(
            user=request.user,
            bill=bill,
            intent_id=f"AP-SCH-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}",
            defaults={
                'amount': bill.amount,
                'recipient': bill.merchant_name,
                'phone': bill.merchant_phone,
                'reference': bill.reference,
                'reason': f"Scheduled {bill.category.lower()} payment",
                'authorization_status': 'AUTHORIZED' if bill.auto_pay_enabled else 'CREATED',
                'payment_status': 'SCHEDULED',
                'expires_at': due_at,
            },
        )
        audit(request.user, 'BILL_CREATED', bill, new='ACTIVE', request=request)
        messages.success(request, 'Bill created.')
        return redirect('dashboard')
    return render(request, 'core/create_bill.html')


def assess_risk(request, bill, amount, recipient, phone):
    score = 0
    reasons = []
    payee = TrustedPayee.objects.filter(
        user=request.user, phone=phone,
        account_reference=bill.reference,
    ).first()
    if not payee:
        score += 55
        reasons.append('Recipient has not been verified')
    elif recipient and recipient.strip().lower() not in {
        payee.name.strip().lower(),
        bill.merchant_name.strip().lower(),
    }:
        score += 55
        reasons.append('Recipient name differs from the verified payee')
    if amount != bill.amount:
        difference = abs(amount - bill.amount)
        score += 45 if difference >= bill.amount * Decimal('0.5') else 20
        reasons.append('Amount differs from the saved bill')
    if request.user.profile.payments_frozen:
        score += 100
        reasons.append('Payments are frozen')
    if amount > request.user.profile.auto_pay_limit:
        score += 15
        reasons.append('Amount exceeds the automatic payment limit')
    duplicate = PaymentIntent.objects.filter(
        user=request.user, bill=bill, reference=bill.reference,
        payment_status__in=['PENDING', 'SUCCESS', 'SUBMITTED'],
    ).exists()
    if duplicate:
        score += 60
        reasons.append('Possible duplicate payment')
    score = min(score, 100)
    level = 'RED' if score >= 61 else ('YELLOW' if score >= 26 else 'GREEN')
    return score, level, reasons


@login_required
@require_POST
def create_intent(request, bill_id):
    bill = get_object_or_404(Bill, id=bill_id, user=request.user)
    try:
        amount = Decimal(request.POST.get('amount') or bill.amount)
    except InvalidOperation:
        messages.error(request, 'Enter a valid payment amount.')
        return redirect('dashboard')
    recipient = request.POST.get('recipient', '').strip() or bill.merchant_name
    phone = request.POST.get('phone', '').strip() or bill.merchant_phone
    score, level, reasons = assess_risk(request, bill, amount, recipient, phone)

    intent = PaymentIntent.objects.create(
        user=request.user, bill=bill,
        intent_id=f"AP-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
        amount=amount, recipient=recipient, phone=phone,
        reference=bill.reference,
        reason=f"{bill.category.title()} payment for {bill.name}",
        risk_score=score, risk_level=level, risk_reasons=reasons,
        authorization_status='BLOCKED' if level == 'RED' or request.user.profile.payments_frozen else ('CONFIRMATION_REQUIRED' if level == 'YELLOW' else 'AUTHORIZED'),
        expires_at=timezone.now() + timezone.timedelta(minutes=15),
    )
    audit(request.user, 'PAYMENT_INTENT_CREATED', intent, new=intent.authorization_status, reason='; '.join(reasons), risk=score, request=request)
    if intent.authorization_status == 'CONFIRMATION_REQUIRED':
        token = uuid.uuid4().hex
        otp = f"{uuid.uuid4().int % 1000000:06d}"
        Confirmation.objects.create(
            payment_intent=intent,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            otp_hash=make_password(otp),
            expires_at=timezone.now() + timezone.timedelta(minutes=15),
        )
    if intent.authorization_status == 'AUTHORIZED':
        messages.success(request, 'Payment Shield approved the intent.')
    elif level == 'RED' or request.user.profile.payments_frozen:
        messages.error(request, 'Payment Shield blocked the payment.')
    else:
        messages.info(request, 'Your confirmation is required.')
    return redirect('dashboard')


@login_required
@require_POST
def confirm_intent(request, intent_id):
    intent = get_object_or_404(PaymentIntent, id=intent_id, user=request.user)
    if intent.authorization_status != 'CONFIRMATION_REQUIRED':
        messages.error(request, 'This payment is not awaiting confirmation.')
        return redirect('dashboard')
    if timezone.now() >= intent.expires_at:
        intent.authorization_status = 'EXPIRED'
        intent.save(update_fields=['authorization_status'])
        audit(request.user, 'PAYMENT_INTENT_EXPIRED', intent, previous='CONFIRMATION_REQUIRED', new='EXPIRED', request=request)
        messages.error(request, 'The payment authorization expired.')
        return redirect('dashboard')
    intent.authorization_status = 'AUTHORIZED'
    intent.save(update_fields=['authorization_status'])
    audit(request.user, 'PAYMENT_CONFIRMED', intent, previous='CONFIRMATION_REQUIRED', new='AUTHORIZED', request=request)
    messages.success(request, 'Payment confirmed.')
    return redirect('dashboard')


@login_required
@require_POST
@transaction.atomic
def simulate_payment(request, intent_id):
    intent = get_object_or_404(PaymentIntent.objects.select_for_update(), id=intent_id, user=request.user)
    if intent.authorization_status != 'AUTHORIZED':
        messages.error(request, 'Payment is not authorized by Payment Shield.')
        return redirect('dashboard')
    if intent.payment_status in {'SUBMITTED', 'PENDING', 'SUCCESS'}:
        messages.info(request, 'This intent has already been submitted.')
        return redirect('dashboard')
    amount = int(intent.amount)
    balance = posted_balance(request.user)
    if request.user.profile.payments_frozen:
        intent.payment_status = 'BLOCKED'
        intent.save(update_fields=['payment_status'])
        audit(request.user, 'PAYMENT_HELD_FROZEN', intent, new='HELD_FROZEN', request=request)
        messages.error(request, 'Payments are frozen.')
        return redirect('dashboard')
    if balance['available'] < amount:
        intent.payment_status = 'FAILED'
        intent.risk_reasons = (intent.risk_reasons or []) + ['Insufficient wallet funds']
        intent.save(update_fields=['payment_status', 'risk_reasons'])
        audit(request.user, 'INSUFFICIENT_FUNDS', intent, new='FAILED', reason=f"Available KSh {balance['available']}", request=request)
        messages.error(request, f"Insufficient wallet balance: KSh {balance['available']}.")
        return redirect('dashboard')
    WalletHold.objects.get_or_create(
        user=request.user,
        payment_intent=intent,
        defaults={'amount': amount, 'active': True},
    )
    LedgerEntry.objects.create(
        user=request.user, type='HOLD', amount=amount,
        ref_type='PaymentIntent', ref_id=str(intent.id), status='POSTED',
    )
    payload = {'amount': str(intent.amount), 'phone': intent.phone, 'reference': intent.reference, 'intent_id': intent.intent_id}
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if settings.PAYHERO_LIVE_PAYMENTS:
        try:
            result = PayHeroClient().payout(
                amount=intent.amount,
                merchant={
                    'name': intent.recipient,
                    'payout_type': 'MOBILE',
                    'payout_account': intent.phone,
                },
                external_reference=intent.intent_id,
                narration=intent.reason,
            )
        except (RuntimeError, requests.RequestException) as exc:
            messages.error(request, str(exc))
            audit(request.user, 'PAYMENT_SUBMISSION_FAILED', intent, previous='AUTHORIZED', new='FAILED', reason=str(exc), request=request)
            intent.payment_status = 'FAILED'
            intent.save(update_fields=['payment_status'])
            return redirect('dashboard')
        provider_reference = (
            result.get('provider_reference')
            or result.get('transaction_reference')
            or result.get('reference')
            or result.get('checkoutRequestID')
            or f"PAYHERO-{intent.intent_id}"
        )
        checkout_reference = str(
            result.get('checkout_reference')
            or result.get('CheckoutRequestID')
            or result.get('checkout_request_id')
            or ''
        )
        PaymentAttempt.objects.create(
            payment_intent=intent, provider='payhero',
            provider_reference=str(provider_reference),
            checkout_reference=checkout_reference,
            amount=intent.amount, status='PENDING',
            request_payload_hash=payload_hash,
            response_payload=result,
        )
        audit(request.user, 'STK_PUSH_SUBMITTED', intent, previous='AUTHORIZED', new='PENDING', request=request)
    else:
        PaymentAttempt.objects.create(
            payment_intent=intent, provider='mock',
            provider_reference=f"MOCK-{intent.intent_id}",
            checkout_reference=f"CHK-{uuid.uuid4().hex[:8].upper()}",
            amount=intent.amount, status='PENDING',
            request_payload_hash=payload_hash,
            response_payload={'mode': 'sandbox', 'message': 'STK prompt simulated'},
        )
        audit(request.user, 'MOCK_STK_SUBMITTED', intent, previous='AUTHORIZED', new='PENDING', request=request)
    intent.payment_status = 'PENDING'
    intent.save(update_fields=['payment_status'])
    audit(request.user, 'MOCK_STK_SUBMITTED', intent, previous='AUTHORIZED', new='PENDING', request=request)
    messages.success(request, 'Mock STK push submitted.')
    return redirect('dashboard')


@csrf_exempt
@require_POST
@transaction.atomic
def mock_webhook(request):
    webhook_secret = getattr(settings, 'PAYHERO_CALLBACK_SECRET', '') or getattr(settings, 'PAYHERO_WEBHOOK_SECRET', '')
    if webhook_secret:
        supplied = (
            request.headers.get('X-PayHero-Signature')
            or request.headers.get('X-PayHero-Secret')
            or request.GET.get('s')
        )
        if not supplied or not hmac.compare_digest(supplied, webhook_secret):
            return JsonResponse({'detail': 'Invalid webhook authentication'}, status=401)
    try:
        if request.content_type == 'application/json':
            payload = json.loads(request.body or '{}')
        else:
            payload = request.POST.dict()
    except json.JSONDecodeError:
        return JsonResponse({'detail': 'Invalid JSON'}, status=400)
    response_body = payload.get('response') if isinstance(payload.get('response'), dict) else payload
    if isinstance(response_body, dict):
        payload = {**payload, **response_body}
    casefolded = {str(key).lower(): value for key, value in payload.items()}
    provider_reference = (
        payload.get('provider_reference')
        or payload.get('providerReference')
        or payload.get('transaction_reference')
        or casefolded.get('externalreference')
        or payload.get('external_reference')
        or payload.get('checkoutRequestID')
        or payload.get('CheckoutRequestID')
    )
    external_reference = payload.get('ExternalReference') or payload.get('external_reference') or casefolded.get('externalreference')
    if external_reference and str(external_reference).startswith('dep_'):
        deposit = Deposit.objects.select_for_update().filter(external_reference=external_reference).first()
        if not deposit:
            return JsonResponse({'received': True, 'ignored': 'unknown deposit'}, status=200)
        if deposit.status in {'SUCCESS','FAILED','CANCELLED','TIMEOUT'}:
            return JsonResponse({'received': True, 'duplicate': True})
        raw_status = str(payload.get('status') or payload.get('Status') or payload.get('ResultCode') or '').upper()
        deposit_status = 'SUCCESS' if raw_status in {'SUCCESS','SUCCESSFUL','0'} else 'FAILED'
        if settings.PAYHERO_LIVE_PAYMENTS and deposit_status == 'SUCCESS':
            verification = PayHeroClient().status(deposit.payhero_reference or external_reference)
            if verification['status'] != 'SUCCESS':
                deposit_status = 'FAILED'
                payload['statusVerification'] = verification
        deposit.status = deposit_status
        deposit.raw_callback = payload
        deposit.mpesa_receipt = str(payload.get('MpesaReceiptNumber') or payload.get('mpesa_receipt') or '')
        deposit.save(update_fields=['status','raw_callback','mpesa_receipt','updated_at'])
        if deposit_status == 'SUCCESS':
            LedgerEntry.objects.create(
                user=deposit.user, type='DEPOSIT', amount=deposit.amount,
                ref_type='Deposit', ref_id=str(deposit.id), status='POSTED',
            )
            audit(deposit.user, 'DEPOSIT_CALLBACK_SUCCESS', deposit, new='SUCCESS', request=request)
            Notification.objects.create(
                user=deposit.user, title='Wallet topped up',
                body=f"KSh {deposit.amount} was received.", channel='IN_APP', status='SENT',
            )
        else:
            audit(deposit.user, 'DEPOSIT_CALLBACK_FAILED', deposit, new='FAILED', reason=str(payload.get('ResultDesc') or payload.get('status') or ''), request=request)
        return JsonResponse({'received': True})
    provider_reference = (
        payload.get('provider_reference')
        or payload.get('providerReference')
        or payload.get('transaction_reference')
        or payload.get('checkoutRequestID')
    )
    intent_id = payload.get('intent_id') or payload.get('external_reference')
    raw_status = str(payload.get('status') or payload.get('Status') or payload.get('ResultCode') or '').upper()
    status = 'SUCCESS' if raw_status in {'SUCCESS', 'SUCCESSFUL', '0'} else ('FAILED' if raw_status else None)
    if not provider_reference or status not in {'SUCCESS', 'FAILED'}:
        return JsonResponse({'detail': 'Missing provider reference or valid status'}, status=400)
    attempt = PaymentAttempt.objects.select_for_update().select_related('payment_intent').filter(provider_reference=provider_reference).first()
    if not attempt:
        return JsonResponse({'detail': 'Unknown payment attempt'}, status=404)
    if settings.PAYHERO_LIVE_PAYMENTS and status == 'SUCCESS':
        verification = PayHeroClient().status(provider_reference)
        if verification['status'] != 'SUCCESS':
            status = 'FAILED'
            payload['statusVerification'] = verification
    if intent_id and intent_id != attempt.payment_intent.intent_id:
        return JsonResponse({'detail': 'Intent mismatch'}, status=400)
    if attempt.status in {'SUCCESS', 'FAILED'}:
        return JsonResponse({'received': True, 'duplicate': True})
    intent = attempt.payment_intent
    attempt.status = status
    attempt.response_payload = payload
    attempt.save(update_fields=['status', 'response_payload', 'updated_at'])
    intent.payment_status = status
    if status == 'SUCCESS':
        intent.executed_at = timezone.now()
        hold = getattr(intent, 'wallet_hold', None)
        if hold and hold.active:
            hold.active = False
            hold.save(update_fields=['active'])
            LedgerEntry.objects.create(
                user=intent.user, type='PAYMENT', amount=-int(intent.amount),
                ref_type='PaymentIntent', ref_id=str(intent.id), status='POSTED',
            )
            LedgerEntry.objects.create(
                user=intent.user, type='RELEASE', amount=int(intent.amount),
                ref_type='PaymentIntent', ref_id=str(intent.id), status='POSTED',
            )
    else:
        hold = getattr(intent, 'wallet_hold', None)
        if hold and hold.active:
            hold.active = False
            hold.save(update_fields=['active'])
            LedgerEntry.objects.create(
                user=intent.user, type='RELEASE', amount=int(intent.amount),
                ref_type='PaymentIntent', ref_id=str(intent.id), status='POSTED',
            )
    intent.save(update_fields=['payment_status', 'executed_at'])
    audit(intent.user, 'MOCK_PAYMENT_CALLBACK', intent, previous='PENDING', new=status, reason='Webhook result', actor='payhero', request=request)
    if status == 'SUCCESS' and not hasattr(intent, 'receipt'):
        receipt_number = f"AP-{timezone.now().strftime('%Y%m%d')}-{intent.id:04d}"
        receipt_dir = Path(settings.BASE_DIR) / 'receipts' / str(intent.user_id)
        receipt_dir.mkdir(parents=True, exist_ok=True)
        json_path = receipt_dir / f"{receipt_number}.json"
        json_path.write_text(json.dumps({
            'number': receipt_number,
            'payer': intent.user.username,
            'merchant': intent.recipient,
            'amount': str(intent.amount),
            'date': timezone.now().isoformat(),
            'providerReference': provider_reference,
            'riskLevel': intent.risk_level,
        }, indent=2))
        pdf_path = receipt_dir / f"{receipt_number}.pdf"
        pdf_content = (
            "%PDF-1.4\n"
            "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
            "3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >> endobj\n"
            "4 0 obj << /Length 55 >> stream\n"
            "BT /F1 18 Tf 72 720 Td (AutoPay Receipt) Tj ET\n"
            f"BT /F1 12 Tf 72 690 Td ({receipt_number}) Tj ET\n"
            "endstream endobj\n"
            "xref\n0 5\n0000000000 65535 f \n"
            "trailer << /Root 1 0 R /Size 5 >>\n"
            "%%EOF\n"
        )
        pdf_path.write_text(pdf_content)
        Receipt.objects.create(
            payment_intent=intent,
            number=receipt_number,
            json_path=str(json_path),
            pdf_path=str(pdf_path),
        )
        Notification.objects.create(
            user=intent.user,
            title='Payment successful',
            body=f"KSh {intent.amount} paid to {intent.recipient}.",
            channel='IN_APP',
            status='SENT',
        )
    return JsonResponse({'received': True})


def api_error(code, message, status=400):
    return JsonResponse({'error': {'code': code, 'message': message}}, status=status)


@login_required
def merchant_api(request):
    if request.method == 'GET':
        return JsonResponse({'merchants': [
            {
                'id': m.id, 'name': m.name, 'kind': m.kind,
                'payoutType': m.payout_type, 'payoutAccount': m.payout_account,
                'trusted': m.trusted, 'blacklisted': m.blacklisted,
            }
            for m in Merchant.objects.filter(user=request.user)
        ]})
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return api_error('INVALID_JSON', 'Request body must be valid JSON.')
    name = str(payload.get('name') or '').strip()
    account = str(payload.get('payoutAccount') or '').strip()
    if not name or not account:
        return api_error('INVALID_MERCHANT', 'Name and payoutAccount are required.')
    merchant, _ = Merchant.objects.get_or_create(
        user=request.user, name=name, payout_account=account,
        defaults={
            'kind': str(payload.get('kind') or 'OTHER').upper(),
            'payout_type': str(payload.get('payoutType') or 'MOBILE').upper(),
        },
    )
    audit(request.user, 'MERCHANT_CREATED', merchant, new='ACTIVE', request=request)
    return JsonResponse({'id': merchant.id, 'name': merchant.name}, status=201)


@login_required
@require_POST
def invoice_api(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return api_error('INVALID_JSON', 'Request body must be valid JSON.')
    text = str(payload.get('text') or '')
    fields = payload.get('fields') or {}
    merchant_name = str(fields.get('merchantName') or '')
    invoice_number = str(fields.get('invoiceNumber') or '')
    amount = int(fields.get('amount') or 0)
    due_date = fields.get('dueDate')
    account_ref = str(fields.get('accountRef') or '')
    if text:
        match_amount = re.search(r'(?i)k(?:es|sh)?\s*([\d,]+)', text)
        match_invoice = re.search(r'(?i)(?:invoice|ref(?:erence)?)[\s:#-]*([A-Z0-9/-]+)', text)
        if not merchant_name:
            merchant_name = str(payload.get('merchantName') or '')
        if not amount and match_amount:
            amount = int(match_amount.group(1).replace(',', ''))
        if not invoice_number and match_invoice:
            invoice_number = match_invoice.group(1)
    if not amount:
        return api_error('INVALID_INVOICE', 'Invoice amount is required.')
    merchant = Merchant.objects.filter(user=request.user, name__iexact=merchant_name).first() if merchant_name else None
    invoice = Invoice.objects.create(
        user=request.user,
        merchant=merchant,
        invoice_number=invoice_number,
        amount=amount,
        due_date=due_date or None,
        account_ref=account_ref,
        source='SMS' if text and 'Confirmed' in text else ('MANUAL' if not text else 'SMS'),
        raw_text=text,
        parsed_json={'merchantName': merchant_name, 'invoiceNumber': invoice_number, 'amount': amount, 'dueDate': due_date, 'accountRef': account_ref},
    )
    audit(request.user, 'INVOICE_CREATED', invoice, new='UNVERIFIED', request=request)
    return JsonResponse({'id': invoice.id, 'verified': False, 'merchantName': merchant_name, 'invoiceNumber': invoice_number, 'amount': amount})


@login_required
@require_POST
def verify_invoice_api(request, invoice_id):
    invoice = get_object_or_404(Invoice, id=invoice_id, user=request.user)
    flags = []
    if invoice.invoice_number:
        paid = PaymentIntent.objects.filter(
            user=request.user,
            reference=invoice.invoice_number,
            payment_status='SUCCESS',
        ).exists()
        active = PaymentIntent.objects.filter(
            user=request.user,
            reference=invoice.invoice_number,
            payment_status__in=['CREATED','SUBMITTED','PENDING'],
        ).exists()
        if paid or active:
            flags.append('DUPLICATE')
    if invoice.merchant:
        if not invoice.merchant.trusted:
            flags.append('NEW_MERCHANT')
        if invoice.merchant.payout_account and invoice.account_ref and invoice.merchant.payout_account != invoice.account_ref:
            flags.append('PAYOUT_DETAILS_CHANGED')
    invoice.verified = not flags
    invoice.verify_notes = '; '.join(flags) or 'No anomalies'
    invoice.save(update_fields=['verified', 'verify_notes'])
    audit(request.user, 'INVOICE_VERIFIED', invoice, new='VERIFIED' if invoice.verified else 'FLAGGED', reason=invoice.verify_notes, request=request)
    return JsonResponse({'ok': invoice.verified, 'flags': flags, 'suggestedAction': 'CANCEL_AND_CONFIRM' if flags else 'PROCEED'})


@login_required
def notifications_api(request):
    since = request.GET.get('since')
    queryset = Notification.objects.filter(user=request.user).order_by('-created_at')
    if since:
        parsed_since = parse_datetime(since)
        if parsed_since:
            queryset = queryset.filter(created_at__gt=parsed_since)
    return JsonResponse({'notifications': [
        {'id': n.id, 'title': n.title, 'body': n.body, 'channel': n.channel,
         'status': n.status, 'actions': n.actions, 'createdAt': n.created_at.isoformat()}
        for n in queryset[:100]
    ]})


@login_required
@require_POST
def notification_ack_api(request, notification_id):
    notification = get_object_or_404(Notification, id=notification_id, user=request.user)
    notification.status = 'ACKED'
    notification.acked_at = timezone.now()
    notification.save(update_fields=['status', 'acked_at'])
    return JsonResponse({'id': notification.id, 'status': 'ACKED'})


@login_required
def forecast_api(request):
    try:
        horizon = int(request.GET.get('days', 30))
    except ValueError:
        horizon = 30
    horizon = min(max(horizon, 1), 365)
    now = timezone.localdate()
    bills = Bill.objects.filter(user=request.user, active=True)
    timeline = []
    running = posted_balance(request.user)['available']
    for bill in bills:
        due = bill.due_date
        if due < now:
            due = now
        if due <= now + timedelta(days=horizon):
            running -= int(bill.amount)
            timeline.append({'date': due.isoformat(), 'bill': bill.name, 'amount': int(bill.amount)})
    timeline.sort(key=lambda item: item['date'])
    projected_end = running
    shortfall = max(0, -projected_end)
    snapshot = ForecastSnapshot.objects.create(
        user=request.user,
        horizon_days=horizon,
        projected_outflow=sum(int(bill.amount) for bill in bills),
        projected_balance=projected_end,
        shortfall=shortfall,
        timeline=timeline,
    )
    return JsonResponse({
        'horizonDays': horizon,
        'totalOut': snapshot.projected_outflow,
        'projectedEndBalance': projected_end,
        'shortfall': shortfall,
        'recommendedTopUp': shortfall,
        'timeline': timeline,
    })


@login_required
@require_POST
def subscription_audit_api(request):
    categories = {'SUBSCRIPTION','AIRTIME','DATA','STREAMING','OTHER_RECURRING','OTHER'}
    items = []
    for bill in Bill.objects.filter(user=request.user, active=True):
        category = bill.category
        if category in categories:
            items.append({
                'billId': bill.id,
                'title': bill.name,
                'amount': int(bill.amount),
                'action': 'PAUSE_OR_CANCEL',
            })
    return JsonResponse({'subscriptions': items})


@login_required
def audit_verify_api(request):
    events = list(AuditEvent.objects.filter(user=request.user).order_by('timestamp', 'id'))
    previous = ''
    broken = None
    for event in events:
        if event.prev_hash != previous:
            broken = event.id
            break
        previous = event.hash
    return JsonResponse({
        'valid': broken is None,
        'firstBrokenId': broken,
        'eventCount': len(events),
    })


@login_required
def household_api(request):
    if request.method == 'GET':
        household = request.user.profile.household
        if not household:
            return JsonResponse({'household': None})
        return JsonResponse({
            'household': {
                'id': household.id,
                'name': household.name,
                'approvalThreshold': household.approval_threshold,
                'members': [
                    {'id': m.user_id, 'role': m.role, 'spendLimit': m.spend_limit}
                    for m in household.members.select_related('user')
                ],
            }
        })
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return api_error('INVALID_JSON', 'Request body must be valid JSON.')
    name = str(payload.get('name') or '').strip()
    if not name:
        return api_error('INVALID_HOUSEHOLD', 'Name is required.')
    household = Household.objects.create(name=name, admin=request.user)
    HouseholdMember.objects.create(household=household, user=request.user, role='ADMIN')
    request.user.profile.household = household
    request.user.profile.save(update_fields=['household'])
    audit(request.user, 'HOUSEHOLD_CREATED', household, new='ACTIVE', request=request)
    return JsonResponse({'id': household.id, 'name': household.name}, status=201)


@login_required
@require_POST
def chat_api(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return api_error('INVALID_JSON', 'Request body must be valid JSON.')
    text = str(payload.get('text') or '').strip()
    lower = text.lower()
    intent = 'UNKNOWN'
    merchant = None
    amount = None
    if 'freeze' in lower:
        intent = 'FREEZE'
    elif 'forecast' in lower or 'due' in lower:
        intent = 'FORECAST'
    elif 'pay ' in lower:
        intent = 'PAY'
        amount_match = re.search(r'(?i)\b(?:kes|ksh)?\s*(\d[\d,]*)', text)
        if amount_match:
            amount = int(amount_match.group(1).replace(',', ''))
        for candidate in Merchant.objects.filter(user=request.user):
            if candidate.name.lower() in lower:
                merchant = candidate.name
                break
    elif 'add bill' in lower:
        intent = 'ADD_BILL'
    elif 'unfreeze' in lower:
        intent = 'UNFREEZE'
    if intent == 'FREEZE':
        request.user.profile.payments_frozen = True
        request.user.profile.save(update_fields=['payments_frozen'])
        audit(request.user, 'CHAT_FREEZE', request.user.profile, new='FROZEN', request=request)
        return JsonResponse({'intent': intent, 'reply': 'All payments are frozen.', 'confidence': 0.95})
    if intent == 'UNFREEZE':
        request.user.profile.payments_frozen = False
        request.user.profile.save(update_fields=['payments_frozen'])
        audit(request.user, 'CHAT_UNFREEZE', request.user.profile, new='UNFROZEN', request=request)
        return JsonResponse({'intent': intent, 'reply': 'Payments are unfrozen and will be re-evaluated.', 'confidence': 0.95})
    if intent == 'FORECAST':
        return JsonResponse({'intent': intent, 'reply': 'Use the forecast endpoint for upcoming obligations.', 'confidence': 0.9})
    if intent == 'PAY':
        if not merchant or not amount:
            return JsonResponse({'intent': intent, 'reply': 'Please confirm the merchant and amount.', 'confidence': 0.5})
        bill = Bill.objects.filter(user=request.user, merchant_name__iexact=merchant).first()
        if not bill:
            return JsonResponse({'intent': intent, 'reply': f'I could not find a bill for {merchant}.', 'confidence': 0.6})
        return JsonResponse({
            'intent': intent,
            'merchant': merchant,
            'amount': amount,
            'reply': f'I resolved {merchant} at KSh {amount}. Confirm this through the normal payment flow.',
            'confidence': 0.9,
        })
    return JsonResponse({'intent': 'UNKNOWN', 'reply': 'I could not understand that command. Try: pay 2000 for data to Safaricom.', 'confidence': 0.2})


@login_required
def receipts_api(request):
    receipts = Receipt.objects.filter(payment_intent__user=request.user).select_related('payment_intent')
    return JsonResponse({'receipts': [
        {'id': r.id, 'number': r.number, 'createdAt': r.created_at.isoformat()}
        for r in receipts
    ]})


@login_required
def receipt_download_api(request, receipt_id):
    receipt = get_object_or_404(Receipt, id=receipt_id, payment_intent__user=request.user)
    if request.GET.get('format') == 'pdf' and receipt.pdf_path:
        pdf_path = Path(receipt.pdf_path)
        if pdf_path.is_file():
            return FileResponse(pdf_path.open('rb'), content_type='application/pdf', as_attachment=True, filename=f"{receipt.number}.pdf")
    if not receipt.json_path:
        return api_error('RECEIPT_UNAVAILABLE', 'No receipt file is available.', 404)
    path = Path(receipt.json_path)
    if not path.is_file():
        return api_error('RECEIPT_UNAVAILABLE', 'No receipt file is available.', 404)
    return JsonResponse(json.loads(path.read_text()))


def health_api(request):
    return JsonResponse({'status': 'ok', 'demoMode': not settings.PAYHERO_LIVE_PAYMENTS})


@login_required
@require_POST
@transaction.atomic
def freeze_api(request):
    profile = request.user.profile
    profile.payments_frozen = True
    profile.frozen_at = timezone.now()
    profile.save(update_fields=['payments_frozen', 'frozen_at'])
    for intent in PaymentIntent.objects.filter(
        user=request.user,
        payment_status__in=['CREATED','SUBMITTED','PENDING'],
    ).select_for_update():
        intent.payment_status = 'BLOCKED'
        intent.save(update_fields=['payment_status'])
        hold = WalletHold.objects.filter(payment_intent=intent, active=True).first()
        if hold:
            hold.active = False
            hold.save(update_fields=['active'])
            LedgerEntry.objects.create(
                user=request.user, type='RELEASE', amount=hold.amount,
                ref_type='PaymentIntent', ref_id=str(intent.id), status='POSTED',
            )
    audit(request.user, 'PAYMENTS_FROZEN', profile, new='FROZEN', request=request)
    Notification.objects.create(
        user=request.user, title='Payments frozen',
        body='All pending payments have been stopped.', channel='IN_APP', status='SENT',
    )
    return JsonResponse({'frozen': True})


@login_required
@require_POST
@transaction.atomic
def unfreeze_api(request):
    profile = request.user.profile
    profile.payments_frozen = False
    profile.save(update_fields=['payments_frozen'])
    audit(request.user, 'PAYMENTS_UNFROZEN', profile, new='UNFROZEN', request=request)
    for intent in PaymentIntent.objects.filter(
        user=request.user,
        payment_status='BLOCKED',
    ).select_for_update():
        if timezone.now() - intent.created_at > timedelta(hours=24):
            intent.authorization_status = 'CONFIRMATION_REQUIRED'
            intent.payment_status = 'CREATED'
        else:
            intent.payment_status = 'CREATED'
        intent.save(update_fields=['authorization_status', 'payment_status'])
    return JsonResponse({'frozen': False})


@login_required
@require_POST
def merchant_trust_api(request, merchant_id):
    merchant = get_object_or_404(Merchant, id=merchant_id, user=request.user)
    merchant.trusted = True
    merchant.save(update_fields=['trusted'])
    audit(request.user, 'MERCHANT_TRUSTED', merchant, new='TRUSTED', request=request)
    return JsonResponse({'id': merchant.id, 'trusted': True})


@login_required
@require_POST
def merchant_blacklist_api(request, merchant_id):
    merchant = get_object_or_404(Merchant, id=merchant_id, user=request.user)
    merchant.blacklisted = True
    merchant.trusted = False
    merchant.save(update_fields=['blacklisted', 'trusted'])
    audit(request.user, 'MERCHANT_BLACKLISTED', merchant, new='BLACKLISTED', request=request)
    return JsonResponse({'id': merchant.id, 'blacklisted': True})


@login_required
@require_POST
@transaction.atomic
def cancel_payment_api(request, intent_id):
    intent = get_object_or_404(PaymentIntent.objects.select_for_update(), id=intent_id, user=request.user)
    if intent.payment_status in {'SUCCESS','FAILED','BLOCKED','CANCELLED'}:
        return JsonResponse({'status': intent.payment_status, 'idempotent': True})
    intent.payment_status = 'CANCELLED'
    intent.authorization_status = 'CANCELLED'
    intent.save(update_fields=['payment_status', 'authorization_status'])
    hold = WalletHold.objects.filter(payment_intent=intent, active=True).first()
    if hold:
        hold.active = False
        hold.save(update_fields=['active'])
        LedgerEntry.objects.create(
            user=request.user, type='RELEASE', amount=hold.amount,
            ref_type='PaymentIntent', ref_id=str(intent.id), status='POSTED',
        )
    audit(request.user, 'PAYMENT_CANCELLED', intent, new='CANCELLED', request=request)
    return JsonResponse({'status': 'CANCELLED'})


@login_required
@require_POST
def reverse_request_api(request, intent_id):
    intent = get_object_or_404(PaymentIntent, id=intent_id, user=request.user, payment_status='SUCCESS')
    if timezone.now() - intent.executed_at > timedelta(hours=24):
        return api_error('REVERSAL_WINDOW_CLOSED', 'The 24-hour reversal request window has closed.', 409)
    audit(request.user, 'REVERSAL_REQUESTED', intent, new='DISPUTE', reason='User requested merchant/provider reversal pack.', request=request)
    return JsonResponse({
        'status': 'DISPUTE',
        'message': 'A dispute pack is available. Automatic reversal is not guaranteed.',
        'receiptId': getattr(getattr(intent, 'receipt', None), 'id', None),
    })


@login_required
@require_POST
def pause_bill_api(request, bill_id):
    bill = get_object_or_404(Bill, id=bill_id, user=request.user)
    bill.active = False
    bill.save(update_fields=['active'])
    Reminder.objects.filter(bill=bill, status='PENDING').update(status='CANCELLED')
    PaymentIntent.objects.filter(bill=bill, payment_status='CREATED').update(payment_status='CANCELLED')
    audit(request.user, 'BILL_PAUSED', bill, new='INACTIVE', request=request)
    return JsonResponse({'id': bill.id, 'active': False})


@login_required
def bills_api(request):
    if request.method == 'GET':
        return JsonResponse({'bills': [
            {
                'id': b.id, 'title': b.name, 'amount': int(b.amount),
                'category': b.category, 'dueDate': b.due_date.isoformat(),
                'active': b.active, 'merchant': b.merchant_name,
                'frequency': b.frequency, 'autoExecute': b.auto_pay_enabled,
            }
            for b in Bill.objects.filter(user=request.user)
        ]})
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return api_error('INVALID_JSON', 'Request body must be valid JSON.')
    try:
        amount = int(payload['amount'])
        due_date = date_type.fromisoformat(payload['dueDate'])
    except (KeyError, TypeError, ValueError):
        return api_error('INVALID_BILL', 'amount and dueDate are required.')
    merchant_name = str(payload.get('merchant', payload.get('merchantName') or 'Unknown')).strip()
    merchant_phone = str(payload.get('merchantPhone') or '').strip()
    merchant, _ = Merchant.objects.get_or_create(
        user=request.user,
        name=merchant_name,
        payout_account=merchant_phone or 'PENDING',
        defaults={'kind': str(payload.get('category') or 'OTHER').upper(), 'payout_type': 'MOBILE'},
    )
    bill = Bill.objects.create(
        user=request.user,
        name=str(payload.get('title') or merchant_name),
        category=str(payload.get('category') or 'OTHER').upper(),
        merchant_name=merchant_name,
        merchant_phone=merchant_phone,
        reference=str(payload.get('reference') or f"BILL-{uuid.uuid4().hex[:8].upper()}"),
        amount=amount,
        due_date=due_date,
        frequency=str(payload.get('frequency') or 'MONTHLY').lower(),
        auto_pay_enabled=bool(payload.get('autoExecute', False)),
        day_of_month=due_date.day,
        next_due_at=timezone.make_aware(datetime.combine(due_date, time(8, 0))),
        reminder_offsets_min=payload.get('reminderOffsetsMin') or [4320, 60],
    )
    audit(request.user, 'BILL_CREATED', bill, new='ACTIVE', request=request)
    return JsonResponse({'id': bill.id, 'title': bill.name, 'nextDueAt': bill.next_due_at.isoformat()}, status=201)


@login_required
@require_POST
def suggest_bills_api(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = request.POST.dict()
    text = str(payload.get('text') or '')
    matches = re.findall(r'(?i)confirmed\.?\s*ksh?[\s]*([\d,]+)\s+sent to\s+(.+?)\s+on\s+(\d{1,2}/\d{1,2}/\d{2,4})', text)
    suggestions = []
    for amount, merchant, date_text in matches:
        suggestions.append({
            'merchant': merchant.strip(),
            'amount': int(amount.replace(',', '')),
            'date': date_text,
            'frequency': 'MONTHLY',
            'confidence': 0.65,
        })
    return JsonResponse({'suggestions': suggestions})


@login_required
def payments_api(request):
    payments = PaymentIntent.objects.filter(user=request.user).select_related('bill')[:100]
    return JsonResponse({'payments': [
        {
            'id': p.id,
            'externalReference': p.intent_id,
            'amount': int(p.amount),
            'merchant': p.recipient,
            'state': p.payment_status,
            'riskLevel': p.risk_level,
            'riskScore': p.risk_score,
            'reasons': p.risk_reasons,
        }
        for p in payments
    ]})


@login_required
@require_POST
@transaction.atomic
def simulate_demo_api(request):
    if not getattr(settings, 'DEMO_MODE', True):
        return api_error('DEMO_DISABLED', 'Demo simulation is disabled.', 403)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = request.POST.dict()
    merchant_name = str(payload.get('merchant') or 'Nairobi Hospital')
    amount = int(payload.get('amount') or 8000)
    bill = Bill.objects.filter(user=request.user, merchant_name__iexact=merchant_name).first()
    if not bill:
        bill = Bill.objects.create(
            user=request.user,
            name=merchant_name,
            category='HOSPITAL',
            merchant_name=merchant_name,
            merchant_phone='254700000000',
            reference=f"INV-{uuid.uuid4().hex[:8].upper()}",
            amount=amount,
            due_date=timezone.localdate(),
        )
    intent = PaymentIntent.objects.create(
        user=request.user,
        bill=bill,
        intent_id=f"AP-DEMO-{uuid.uuid4().hex[:8].upper()}",
        amount=amount,
        recipient=merchant_name,
        phone=bill.merchant_phone,
        reference=bill.reference,
        reason='Demo fraud simulation',
        risk_score=70,
        risk_level='RED',
        risk_reasons=['DUPLICATE', 'AMOUNT_SPIKE_50'],
        authorization_status='BLOCKED',
        expires_at=timezone.now() + timezone.timedelta(minutes=15),
    )
    audit(request.user, 'DEMO_FRAUD_SIMULATED', intent, new='BLOCKED', risk=70, request=request)
    return JsonResponse({'id': intent.id, 'level': 'RED', 'score': 70, 'state': 'BLOCKED', 'reasons': intent.risk_reasons})


@login_required
def confirm_api(request, token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    confirmation = Confirmation.objects.filter(token_hash=token_hash).select_related('payment_intent').first()
    if not confirmation or confirmation.payment_intent.user_id != request.user.id:
        return api_error('CONFIRMATION_NOT_FOUND', 'Confirmation link is invalid or expired.', 404)
    if request.method == 'GET':
        intent = confirmation.payment_intent
        return JsonResponse({
            'id': confirmation.id,
            'status': confirmation.status,
            'payment': {
                'amount': int(intent.amount),
                'merchant': intent.recipient,
                'riskLevel': intent.risk_level,
                'reasons': intent.risk_reasons,
            },
            'expiresAt': confirmation.expires_at.isoformat(),
        })
    return confirm_token_action(request, confirmation)


@require_POST
@login_required
@transaction.atomic
def confirm_token_action(request, confirmation):
    if confirmation.status != 'PENDING':
        return JsonResponse({'status': confirmation.status, 'idempotent': True})
    if timezone.now() >= confirmation.expires_at:
        confirmation.status = 'EXPIRED'
        confirmation.save(update_fields=['status'])
        confirmation.payment_intent.authorization_status = 'EXPIRED'
        confirmation.payment_intent.save(update_fields=['authorization_status'])
        return JsonResponse({'status': 'EXPIRED'})
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = request.POST.dict()
    action = str(payload.get('action') or 'reject').lower()
    if action == 'approve':
        otp = str(payload.get('otp') or '')
        if not otp or not check_password(otp, confirmation.otp_hash):
            confirmation.attempts += 1
            confirmation.save(update_fields=['attempts'])
            if confirmation.attempts >= 5:
                confirmation.status = 'EXPIRED'
                confirmation.save(update_fields=['status'])
            return api_error('INVALID_OTP', 'OTP is invalid.', 401)
        confirmation.status = 'APPROVED'
        confirmation.decided_by = request.user
        confirmation.decided_at = timezone.now()
        confirmation.save(update_fields=['status','decided_by','decided_at'])
        confirmation.payment_intent.authorization_status = 'AUTHORIZED'
        confirmation.payment_intent.save(update_fields=['authorization_status'])
        AgentFeedback.objects.create(
            payment_intent=confirmation.payment_intent,
            agent_decision=confirmation.payment_intent.risk_level,
            user_decision='CONFIRMED',
        )
        audit(request.user, 'PAYMENT_CONFIRMED', confirmation.payment_intent, new='AUTHORIZED', request=request)
        return JsonResponse({'status': 'APPROVED'})
    confirmation.status = 'REJECTED'
    confirmation.decided_by = request.user
    confirmation.decided_at = timezone.now()
    confirmation.save(update_fields=['status','decided_by','decided_at'])
    confirmation.payment_intent.authorization_status = 'CANCELLED'
    confirmation.payment_intent.payment_status = 'CANCELLED'
    confirmation.payment_intent.save(update_fields=['authorization_status','payment_status'])
    AgentFeedback.objects.create(
        payment_intent=confirmation.payment_intent,
        agent_decision=confirmation.payment_intent.risk_level,
        user_decision='CANCELLED',
    )
    audit(request.user, 'PAYMENT_REJECTED', confirmation.payment_intent, new='CANCELLED', request=request)
    return JsonResponse({'status': 'REJECTED'})


@login_required
def transactions_api(request):
    entries = LedgerEntry.objects.filter(user=request.user).order_by('-created_at')[:100]
    return JsonResponse({'transactions': [
        {
            'id': e.id,
            'type': e.type,
            'amount': e.amount,
            'status': e.status,
            'refType': e.ref_type,
            'refId': e.ref_id,
            'createdAt': e.created_at.isoformat(),
        }
        for e in entries
    ]})


@login_required
@require_POST
def fast_forward_api(request):
    if not getattr(settings, 'DEMO_MODE', True):
        return api_error('DEMO_DISABLED', 'Demo mode is disabled.', 403)
    generated = generate_bill_reminders()
    sent = send_due_reminders()
    return JsonResponse({'generated': generated, 'sent': sent, 'ok': True})


@login_required
def receipts_export_api(request):
    fmt = request.GET.get('format', 'csv').lower()
    receipts = Receipt.objects.filter(payment_intent__user=request.user).select_related('payment_intent')
    if fmt == 'zip':
        return api_error('ZIP_NOT_IMPLEMENTED', 'ZIP export requires an object-storage/archive build.', 501)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['number','payment','merchant','amount','date'])
    for receipt in receipts:
        writer.writerow([
            receipt.number,
            receipt.payment_intent.intent_id,
            receipt.payment_intent.recipient,
            int(receipt.payment_intent.amount),
            receipt.created_at.isoformat(),
        ])
    response = JsonResponse({'format': 'csv', 'csv': output.getvalue()})
    response['Content-Disposition'] = 'attachment; filename="autopay-receipts.csv"'
    return response


@login_required
@require_POST
def bills_import_api(request):
    upload = request.FILES.get('file')
    if not upload:
        try:
            payload = json.loads(request.body or '{}')
            csv_text = payload.get('csv', '')
        except json.JSONDecodeError:
            csv_text = ''
    else:
        csv_text = upload.read().decode('utf-8')
    if not csv_text:
        return api_error('INVALID_CSV', 'Upload a CSV file or send csv text.')
    created = 0
    reader = csv.DictReader(StringIO(csv_text))
    for row in reader:
        try:
            title = row.get('title') or row.get('name') or 'Imported bill'
            amount = int(row.get('amount') or 0)
            due_date = date_type.fromisoformat(row.get('dueDate') or row.get('due_date'))
        except (TypeError, ValueError):
            continue
        merchant_name = row.get('merchant') or title
        Bill.objects.create(
            user=request.user,
            name=title,
            category=(row.get('category') or 'SUPPLIER').upper(),
            merchant_name=merchant_name,
            merchant_phone=row.get('phone') or '',
            reference=row.get('reference') or f"BILL-{uuid.uuid4().hex[:8].upper()}",
            amount=amount,
            due_date=due_date,
            frequency=(row.get('frequency') or 'once').lower(),
        )
        created += 1
    audit(request.user, 'BILLS_IMPORTED', request.user.profile, new=str(created), request=request)
    return JsonResponse({'created': created})


@login_required
@require_POST
def negotiation_draft_api(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = request.POST.dict()
    provider = str(payload.get('provider') or 'Your provider')
    plan = str(payload.get('plan') or 'current plan')
    alternative = str(payload.get('alternative') or 'a cheaper provider plan')
    draft = (
        f"Hello {provider}, I am reviewing my {plan}. "
        f"I found {alternative} that better fits my usage. "
        "Could you match or improve this offer while keeping the same level of service?"
    )
    audit(request.user, 'NEGOTIATION_DRAFT_CREATED', request.user.profile, reason=draft, request=request)
    return JsonResponse({'draft': draft, 'autoSwitch': False})


@require_POST
@transaction.atomic
def auth_register_api(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return api_error('INVALID_JSON', 'Request body must be valid JSON.')
    username = str(payload.get('username') or payload.get('phone') or '').strip()
    password = str(payload.get('password') or '')
    if not username or len(password) < 8:
        return api_error('INVALID_CREDENTIALS', 'Username/phone and a password of at least 8 characters are required.')
    if get_user_model().objects.filter(username=username).exists():
        return api_error('USER_EXISTS', 'This account already exists.', 409)
    user = get_user_model().objects.create_user(
        username=username,
        email=str(payload.get('email') or ''),
        password=password,
    )
    user.profile.phone = str(payload.get('phone') or '')
    user.profile.save(update_fields=['phone'])
    token = signing.dumps({'user_id': user.id}, salt='autopay-api')
    return JsonResponse({'token': token, 'user': {'id': user.id, 'username': user.username}}, status=201)


@require_POST
def auth_login_api(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return api_error('INVALID_JSON', 'Request body must be valid JSON.')
    username = str(payload.get('username') or payload.get('phone') or '')
    password = str(payload.get('password') or '')
    user = django_authenticate(username=username, password=password)
    if not user:
        return api_error('INVALID_CREDENTIALS', 'Login credentials are invalid.', 401)
    token = signing.dumps({'user_id': user.id}, salt='autopay-api')
    return JsonResponse({'token': token, 'user': {'id': user.id, 'username': user.username}})


def bearer_user(request):
    authorization = request.headers.get('Authorization', '')
    if not authorization.startswith('Bearer '):
        return request.user if request.user.is_authenticated else None
    token = authorization.removeprefix('Bearer ').strip()
    try:
        payload = signing.loads(token, salt='autopay-api', max_age=60 * 60 * 24 * 7)
        return get_user_model().objects.filter(id=payload['user_id']).first()
    except (signing.BadSignature, KeyError, ValueError):
        return None


def me_api(request):
    user = bearer_user(request)
    if not user:
        return api_error('UNAUTHORIZED', 'Authentication is required.', 401)
    if request.method == 'PATCH':
        try:
            payload = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            return api_error('INVALID_JSON', 'Request body must be valid JSON.')
        profile = user.profile
        if 'threshold' in payload:
            profile.high_value_threshold = max(0, int(payload['threshold']))
        if 'highValueThreshold' in payload:
            profile.high_value_threshold = max(0, int(payload['highValueThreshold']))
        if 'quietHoursStart' in payload:
            profile.quiet_hours_start = payload['quietHoursStart'] or None
        if 'quietHoursEnd' in payload:
            profile.quiet_hours_end = payload['quietHoursEnd'] or None
        if 'phone' in payload:
            profile.phone = str(payload['phone'])
        profile.save()
    profile = user.profile
    return JsonResponse({
        'id': user.id,
        'name': user.get_full_name() or user.username,
        'phone': profile.phone,
        'email': user.email,
        'highValueThreshold': profile.high_value_threshold,
        'quietHoursStart': str(profile.quiet_hours_start) if profile.quiet_hours_start else None,
        'quietHoursEnd': str(profile.quiet_hours_end) if profile.quiet_hours_end else None,
        'frozen': profile.payments_frozen,
    })


def posted_balance(user):
    rows = LedgerEntry.objects.filter(user=user, status='POSTED')
    posted = sum(row.amount for row in rows)
    held = sum(row.amount for row in LedgerEntry.objects.filter(
        user=user, type='HOLD', status='POSTED',
    ))
    releases = sum(row.amount for row in LedgerEntry.objects.filter(
        user=user, type='RELEASE', status='POSTED',
    ))
    held = held - releases
    return {'posted': posted, 'held': max(held, 0), 'available': posted - max(held, 0)}


@login_required
@require_POST
@transaction.atomic
def wallet_deposit(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = request.POST.dict()
    try:
        amount = int(payload.get('amount', 0))
    except (TypeError, ValueError):
        return api_error('INVALID_AMOUNT', 'Amount must be an integer number of KES.')
    if amount < 10 or amount > 150000:
        return api_error('INVALID_AMOUNT', 'Deposit must be between KES 10 and KES 150,000.')
    phone = str(payload.get('phone') or request.user.profile.phone or '').strip()
    normalized = phone.replace(' ', '').replace('-', '')
    if normalized.startswith('0'):
        normalized = '254' + normalized[1:]
    elif normalized.startswith('+'):
        normalized = normalized[1:]
    if not re.fullmatch(r'254(?:7|1)\d{8}', normalized):
        return api_error('INVALID_PHONE', 'Use a Kenyan mobile number.')

    external_reference = f"dep_{uuid.uuid4()}"
    deposit = Deposit.objects.create(
        user=request.user, amount=amount, phone=normalized,
        external_reference=external_reference,
    )
    provider_reference = ''
    checkout_request_id = ''
    if settings.PAYHERO_LIVE_PAYMENTS:
        try:
            result = PayHeroClient().collect(
                amount=amount,
                phone=normalized,
                external_reference=external_reference,
                customer_name=request.user.get_full_name() or request.user.username,
            )
            provider_reference = result['providerReference']
            checkout_request_id = result.get('checkoutRequestId') or ''
            deposit.payhero_reference = provider_reference
            deposit.checkout_request_id = checkout_request_id
            deposit.save(update_fields=['payhero_reference', 'checkout_request_id'])
        except (RuntimeError, requests.RequestException) as exc:
            deposit.status = 'FAILED'
            deposit.save(update_fields=['status'])
            audit(request.user, 'DEPOSIT_FAILED', deposit, new='FAILED', reason=str(exc), request=request)
            return api_error('PROVIDER_ERROR', str(exc), 502)
    else:
        provider_reference = f"MOCK-{external_reference}"
        checkout_request_id = f"CHK-{uuid.uuid4().hex[:8].upper()}"
        deposit.payhero_reference = provider_reference
        deposit.checkout_request_id = checkout_request_id
        deposit.save(update_fields=['payhero_reference', 'checkout_request_id'])
    audit(request.user, 'DEPOSIT_CREATED', deposit, new='QUEUED', request=request)
    return JsonResponse({
        'depositId': deposit.id,
        'status': 'QUEUED',
        'message': 'Check your phone for the M-Pesa prompt',
        'providerReference': provider_reference,
    }, status=201)


@login_required
def deposit_status_api(request, deposit_id):
    deposit = get_object_or_404(Deposit, id=deposit_id, user=request.user)
    return JsonResponse({
        'id': deposit.id,
        'amount': deposit.amount,
        'status': deposit.status,
        'providerReference': deposit.payhero_reference,
        'mpesaReceipt': deposit.mpesa_receipt,
    })


@login_required
def wallet_api(request):
    if request.method == 'POST':
        return wallet_deposit(request)
    return JsonResponse(posted_balance(request.user))


deposit_api = wallet_deposit


@login_required
@require_POST
@transaction.atomic
def deposit_complete_api(request, deposit_id):
    deposit = get_object_or_404(Deposit, id=deposit_id, user=request.user)
    if deposit.status in {'SUCCESS', 'FAILED', 'CANCELLED', 'TIMEOUT'}:
        return JsonResponse({'status': deposit.status, 'idempotent': True})
    deposit.status = 'SUCCESS'
    deposit.mpesa_receipt = request.POST.get('receipt') or f"MOCK-{uuid.uuid4().hex[:8].upper()}"
    deposit.save(update_fields=['status', 'mpesa_receipt', 'updated_at'])
    LedgerEntry.objects.create(
        user=request.user, type='DEPOSIT', amount=deposit.amount,
        ref_type='Deposit', ref_id=str(deposit.id), status='POSTED',
    )
    audit(request.user, 'DEPOSIT_SUCCESS', deposit, new='SUCCESS', request=request)
    Notification.objects.create(
        user=request.user,
        title='Wallet topped up',
        body=f"KSh {deposit.amount} was received.",
        channel='IN_APP',
        status='SENT',
        actions=[{'label': 'View wallet', 'url': '/wallet'}],
    )
    return JsonResponse({'status': 'SUCCESS', 'balance': posted_balance(request.user)})
