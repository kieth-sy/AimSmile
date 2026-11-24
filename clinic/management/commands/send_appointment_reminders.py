import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from clinic.integrations import send_appointment_notifications
from clinic.models import Appointment, AppointmentStatus

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send appointment reminders for confirmed appointments about 24 hours away.'

    def handle(self, *args, **options):
        if not (
            settings.APPOINTMENT_SMS_NOTIFICATIONS
            or settings.APPOINTMENT_EMAIL_NOTIFICATIONS
        ):
            raise CommandError(
                'Enable appointment SMS or email notifications before sending reminders.'
            )

        now = timezone.now()
        window_start = now + timedelta(hours=23)
        window_end = now + timedelta(hours=25)
        local_start = timezone.localtime(window_start)
        local_end = timezone.localtime(window_end)
        candidates = Appointment.objects.filter(
            status=AppointmentStatus.CONFIRMED,
            reminder_sent_at__isnull=True,
            date__range=(local_start.date(), local_end.date()),
        ).select_related('patient', 'dentist', 'service')

        sent_count = 0
        errors = 0
        for appointment in candidates:
            appointment_at = timezone.make_aware(
                datetime.combine(appointment.date, appointment.time),
                timezone.get_current_timezone(),
            )
            if not window_start <= appointment_at < window_end:
                continue
            try:
                sent = send_appointment_notifications(appointment, reminder=True)
            except Exception:
                errors += 1
                logger.exception(
                    'Could not send reminder for appointment %s.',
                    appointment.pk,
                )
                continue
            if sent:
                appointment.reminder_sent_at = timezone.now()
                appointment.save(update_fields=['reminder_sent_at'])
                sent_count += 1

        self.stdout.write(self.style.SUCCESS(
            f'Sent {sent_count} appointment reminder(s).'
        ))
        if errors:
            raise CommandError(
                f'{errors} appointment reminder(s) failed; see the application logs.'
            )
