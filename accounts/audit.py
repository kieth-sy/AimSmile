import ipaddress

from .models import ActivityLog


def log_activity(request, action, summary, target=None, target_label=''):
    actor = getattr(request, 'user', None) if request is not None else None
    if actor is not None and not getattr(actor, 'is_authenticated', False):
        actor = None

    remote_addr = request.META.get('REMOTE_ADDR', '') if request is not None else ''
    try:
        ip_address = str(ipaddress.ip_address(remote_addr)) if remote_addr else None
    except ValueError:
        ip_address = None

    target_type = ''
    target_id = ''
    if target is not None:
        target_type = target._meta.verbose_name[:64]
        target_id = str(target.pk)[:64]

    return ActivityLog.objects.create(
        actor=actor,
        actor_username=actor.get_username()[:150] if actor else '',
        action=action,
        summary=summary[:255],
        target_type=target_type,
        target_id=target_id,
        target_label=target_label[:150],
        ip_address=ip_address,
    )
