from django.contrib import admin
from .models import Bill, TrustedPayee, PaymentIntent, PaymentAttempt, AuditEvent, Reminder

admin.site.register(Bill)
admin.site.register(TrustedPayee)
admin.site.register(PaymentIntent)
admin.site.register(PaymentAttempt)
admin.site.register(AuditEvent)
admin.site.register(Reminder)

# Register your models here.
