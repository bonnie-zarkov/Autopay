"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from core import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.dashboard, name='dashboard'),
    path('login/', views.login_view, name='login'),
    path('signup/', views.signup_view, name='signup'),
    path('logout/', views.logout_view, name='logout'),
    path('bills/new/', views.create_bill, name='create_bill'),
    path('intents/new/<int:bill_id>/', views.create_intent, name='create_intent'),
    path('intents/<int:intent_id>/confirm/', views.confirm_intent, name='confirm_intent'),
    path('intents/<int:intent_id>/simulate/', views.simulate_payment, name='simulate_payment'),
    path('freeze/', views.toggle_freeze, name='toggle_freeze'),
    path('reminders/', views.reminders, name='reminders'),
    path('api/payments/webhooks/payhero/', views.mock_webhook, name='payhero_webhook'),
    path('api/webhooks/payhero/', views.mock_webhook, name='payhero_webhook_api'),
    path('webhooks/payhero', views.mock_webhook, name='payhero_webhook_public'),
    path('api/wallet/', views.wallet_api, name='wallet_api'),
    path('api/wallet/deposit/', views.deposit_api, name='deposit_api'),
    path('api/wallet/deposit/<int:deposit_id>/', views.deposit_status_api, name='deposit_status_api'),
    path('api/wallet/deposit/<int:deposit_id>/complete/', views.deposit_complete_api, name='deposit_complete_api'),
    path('api/merchants/', views.merchant_api, name='merchant_api'),
    path('api/invoices/', views.invoice_api, name='invoice_api'),
    path('api/invoices/<int:invoice_id>/verify/', views.verify_invoice_api, name='verify_invoice_api'),
    path('api/notifications/', views.notifications_api, name='notifications_api'),
    path('api/notifications/<int:notification_id>/ack/', views.notification_ack_api, name='notification_ack_api'),
    path('api/forecast/', views.forecast_api, name='forecast_api'),
    path('api/audit/subscriptions/', views.subscription_audit_api, name='subscription_audit_api'),
    path('api/audit/verify/', views.audit_verify_api, name='audit_verify_api'),
    path('api/household/', views.household_api, name='household_api'),
    path('api/chat/', views.chat_api, name='chat_api'),
    path('api/receipts/<int:receipt_id>/download/', views.receipt_download_api, name='receipt_download_api'),
    path('api/receipts/', views.receipts_api, name='receipts_api'),
    path('api/health/', views.health_api, name='health_api'),
    path('api/freeze/', views.freeze_api, name='freeze_api'),
    path('api/unfreeze/', views.unfreeze_api, name='unfreeze_api'),
    path('api/merchants/<int:merchant_id>/trust/', views.merchant_trust_api, name='merchant_trust_api'),
    path('api/merchants/<int:merchant_id>/blacklist/', views.merchant_blacklist_api, name='merchant_blacklist_api'),
    path('api/payments/<int:intent_id>/cancel/', views.cancel_payment_api, name='cancel_payment_api'),
    path('api/payments/<int:intent_id>/reverse-request/', views.reverse_request_api, name='reverse_request_api'),
    path('api/bills/<int:bill_id>/pause/', views.pause_bill_api, name='pause_bill_api'),
    path('api/bills/', views.bills_api, name='bills_api'),
    path('api/bills/suggest/', views.suggest_bills_api, name='suggest_bills_api'),
    path('api/payments/', views.payments_api, name='payments_api'),
    path('api/payments/simulate/', views.simulate_demo_api, name='simulate_demo_api'),
    path('api/confirm/<str:token>/', views.confirm_api, name='confirm_api'),
    path('api/wallet/transactions/', views.transactions_api, name='transactions_api'),
    path('api/demo/fast-forward/', views.fast_forward_api, name='fast_forward_api'),
    path('api/receipts/export/', views.receipts_export_api, name='receipts_export_api'),
    path('api/bills/import/', views.bills_import_api, name='bills_import_api'),
    path('api/negotiation/draft/', views.negotiation_draft_api, name='negotiation_draft_api'),
    path('api/auth/register/', views.auth_register_api, name='auth_register_api'),
    path('api/auth/login/', views.auth_login_api, name='auth_login_api'),
    path('api/me/', views.me_api, name='me_api'),
]
