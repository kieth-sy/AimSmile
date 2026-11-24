from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver

from .audit import log_activity
from .models import ActivityLog


@receiver(user_logged_in, dispatch_uid='accounts.audit_user_logged_in')
def audit_user_logged_in(sender, request, user, **kwargs):
    log_activity(
        request,
        ActivityLog.Action.LOGIN_SUCCESS,
        'User signed in.',
        target=user,
        target_label=user.get_username(),
    )


@receiver(user_logged_out, dispatch_uid='accounts.audit_user_logged_out')
def audit_user_logged_out(sender, request, user, **kwargs):
    authenticated_user = user if user and user.is_authenticated else None
    log_activity(
        request,
        ActivityLog.Action.LOGOUT,
        'User signed out.',
        target=authenticated_user,
        target_label=authenticated_user.get_username() if authenticated_user else '',
    )


@receiver(user_login_failed, dispatch_uid='accounts.audit_user_login_failed')
def audit_user_login_failed(sender, credentials, request, **kwargs):
    log_activity(
        request,
        ActivityLog.Action.LOGIN_FAILED,
        'Failed sign-in attempt.',
    )
