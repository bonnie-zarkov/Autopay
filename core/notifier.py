from django.conf import settings

from .models import Notification


class InAppNotifier:
    def send(self, user, title, body, actions=None, channel='IN_APP'):
        notification = Notification.objects.create(
            user=user,
            title=title,
            body=body,
            channel=channel,
            status='SENT',
            actions=actions or [],
        )
        return {'ok': True, 'id': str(notification.id)}


class ConsoleSmsNotifier:
    def send(self, user, title, body, actions=None, channel='SMS'):
        if getattr(settings, 'SMS_PROVIDER', 'console') != 'console':
            return {'ok': False, 'id': None}
        print(f'[SMS {user.profile.phone or user.username}] {title}: {body}')
        return {'ok': True, 'id': f'console-{user.id}'}
