import json
import logging
import re
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)
CLINIC_TIME_ZONE = ZoneInfo('Asia/Manila')
GOOGLE_CALENDAR_SCOPES = (
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
)


class ClinicIntegrationError(Exception):
    pass


def _appointment_message(appointment, reminder=False):
    patient_name = appointment.patient.display_name
    service_name = appointment.service.name if appointment.service else 'Dental consultation'
    appointment_date = appointment.date.strftime('%B %d, %Y')
    appointment_time = appointment.time.strftime('%I:%M %p')
    if reminder:
        prefix = 'Reminder:'
    elif appointment.status == 'pending':
        prefix = 'Appointment request received (pending confirmation):'
    else:
        prefix = 'Appointment confirmed:'
    return (
        f'{prefix} {patient_name}, your {service_name} at AimSmile is '
        f'scheduled for {appointment_date} at {appointment_time}.'
    )


def send_semaphore_sms(phone_number, message):
    if not settings.SEMAPHORE_API_KEY:
        raise ClinicIntegrationError('Semaphore API key is not configured.')

    number = re.sub(r'\D', '', phone_number)
    if number.startswith('0'):
        number = '63' + number[1:]
    if not re.fullmatch(r'639\d{9}', number):
        raise ClinicIntegrationError('The patient phone number is not a valid PH mobile number.')

    fields = {
        'apikey': settings.SEMAPHORE_API_KEY,
        'number': number,
        'message': message,
    }
    if settings.SEMAPHORE_SENDER_NAME:
        fields['sendername'] = settings.SEMAPHORE_SENDER_NAME

    request = Request(
        'https://api.semaphore.co/api/v4/messages',
        data=urlencode(fields).encode('utf-8'),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=15) as response:
            response_body = response.read().decode('utf-8')
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ClinicIntegrationError('Semaphore SMS request failed.') from exc

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise ClinicIntegrationError('Semaphore returned an invalid response.') from exc
    if not isinstance(result, list) or not result or not result[0].get('message_id'):
        raise ClinicIntegrationError('Semaphore did not accept the SMS message.')


def send_appointment_notifications(appointment, reminder=False):
    message = _appointment_message(appointment, reminder=reminder)
    sent = False

    if settings.APPOINTMENT_SMS_NOTIFICATIONS:
        phone_number = getattr(appointment.patient, 'phone', '')
        if not phone_number:
            raise ClinicIntegrationError('The patient has no phone number for SMS.')
        send_semaphore_sms(phone_number, message)
        sent = True

    if settings.APPOINTMENT_EMAIL_NOTIFICATIONS:
        email = getattr(appointment.patient, 'email', '')
        if not email:
            raise ClinicIntegrationError('The patient has no email address for email.')
        send_mail(
            subject='Appointment reminder' if reminder else 'Appointment update',
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
        sent = True
    return sent


def _calendar_cipher():
    key = getattr(settings, 'GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY', '')
    if not key:
        raise ClinicIntegrationError(
            'Google Calendar token encryption key is not configured.'
        )
    try:
        return Fernet(key.encode('ascii'))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ClinicIntegrationError(
            'Google Calendar token encryption key is invalid.'
        ) from exc


def encrypt_calendar_credentials(credentials_json):
    return _calendar_cipher().encrypt(credentials_json.encode('utf-8')).decode('ascii')


def decrypt_calendar_credentials(encrypted_credentials):
    try:
        return _calendar_cipher().decrypt(
            encrypted_credentials.encode('ascii')
        ).decode('utf-8')
    except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
        raise ClinicIntegrationError(
            'Stored Google Calendar credentials cannot be decrypted.'
        ) from exc


def _calendar_service_from_credentials(encrypted_credentials, save_credentials):
    if not encrypted_credentials:
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ClinicIntegrationError(
            'Google Calendar packages are not installed.'
        ) from exc

    credentials = Credentials.from_authorized_user_info(
        json.loads(decrypt_calendar_credentials(encrypted_credentials)),
        scopes=GOOGLE_CALENDAR_SCOPES,
    )
    if credentials.expired and credentials.refresh_token:
        from google.auth.transport.requests import Request as GoogleAuthRequest

        credentials.refresh(GoogleAuthRequest())
        save_credentials(encrypt_calendar_credentials(credentials.to_json()))
    if not credentials.valid:
        raise ClinicIntegrationError(
            'Google Calendar authorization has expired; reconnect the calendar.'
        )
    return build('calendar', 'v3', credentials=credentials, cache_discovery=False)


def _google_calendar_service(dentist):
    if not dentist or not hasattr(dentist, 'doctor_profile'):
        return None
    profile = dentist.doctor_profile
    return _calendar_service_from_credentials(
        profile.google_calendar_credentials,
        lambda value: _save_doctor_calendar_credentials(profile, value),
    )


def _save_doctor_calendar_credentials(profile, credentials):
    profile.google_calendar_credentials = credentials
    profile.save(update_fields=['google_calendar_credentials'])


def _clinic_google_calendar_service():
    from .models import ClinicGoogleCalendarConnection

    connection = ClinicGoogleCalendarConnection.objects.filter(pk=1).first()
    if not connection:
        return None
    return _calendar_service_from_credentials(
        connection.google_calendar_credentials,
        lambda value: _save_clinic_calendar_credentials(connection, value),
    )


def _save_clinic_calendar_credentials(connection, credentials):
    connection.google_calendar_credentials = credentials
    connection.save(update_fields=['google_calendar_credentials', 'updated_at'])


def _appointment_google_calendar_event(appointment):
    start = timezone.make_aware(
        datetime.combine(appointment.date, appointment.time),
        CLINIC_TIME_ZONE,
    )
    duration = appointment.service.duration_minutes if appointment.service else 30
    end = start + timedelta(minutes=duration)
    event = {
        'summary': (
            f'{appointment.service.name if appointment.service else "Dental appointment"}'
            f' - {appointment.patient.display_name}'
        ),
        'description': 'AimSmile appointment',
        'start': {'dateTime': start.isoformat()},
        'end': {'dateTime': end.isoformat()},
    }
    return event


def _upsert_appointment_calendar_event(service, appointment, event, event_id_field):
    events = service.events()
    event_id = getattr(appointment, event_id_field)
    if event_id:
        updated = events.update(
            calendarId='primary',
            eventId=event_id,
            body=event,
        ).execute()
    else:
        updated = events.insert(calendarId='primary', body=event).execute()
    setattr(appointment, event_id_field, updated['id'])
    appointment.save(update_fields=[event_id_field])


def sync_appointment_to_clinic_google_calendar(appointment):
    service = _clinic_google_calendar_service()
    if service is None:
        return False
    _upsert_appointment_calendar_event(
        service,
        appointment,
        _appointment_google_calendar_event(appointment),
        'clinic_google_calendar_event_id',
    )
    return True


def sync_appointment_to_google_calendar(appointment):
    event = _appointment_google_calendar_event(appointment)
    synced = False
    failures = []

    if appointment.dentist:
        try:
            service = _google_calendar_service(appointment.dentist)
            if service is not None:
                _upsert_appointment_calendar_event(
                    service,
                    appointment,
                    event,
                    'google_calendar_event_id',
                )
                synced = True
        except Exception as exc:
            logger.exception(
                'Could not sync appointment %s to its dentist calendar.',
                appointment.pk,
            )
            failures.append(exc)

    try:
        service = _clinic_google_calendar_service()
        if service is not None:
            _upsert_appointment_calendar_event(
                service,
                appointment,
                event,
                'clinic_google_calendar_event_id',
            )
            synced = True
    except Exception as exc:
        logger.exception(
            'Could not sync appointment %s to the clinic calendar.',
            appointment.pk,
        )
        failures.append(exc)

    if failures:
        raise ClinicIntegrationError(
            'The appointment could not be synced to every connected Google Calendar.'
        ) from failures[0]
    return synced


def get_google_calendar_embed_url(dentist):
    service = _google_calendar_service(dentist)
    if service is None:
        return ''
    return _get_calendar_embed_url(service, f'dentist {dentist.pk}')


def get_clinic_google_calendar_embed_url():
    service = _clinic_google_calendar_service()
    if service is None:
        return ''
    return _get_calendar_embed_url(service, 'clinic')


def _get_calendar_embed_url(service, calendar_owner):
    try:
        calendar = service.calendarList().get(
            calendarId='primary',
        ).execute()
    except Exception as exc:
        logger.warning(
            'Google Calendar embed lookup failed for %s (%s).',
            calendar_owner,
            type(exc).__name__,
        )
        raise ClinicIntegrationError(
            'Google Calendar could not be embedded. Reconnect and allow calendar-list access.'
        ) from exc

    calendar_id = calendar.get('id')
    if not calendar_id:
        raise ClinicIntegrationError(
            'Google Calendar did not return a primary calendar ID.'
        )
    query = urlencode({
        'src': calendar_id,
        'ctz': 'Asia/Manila',
        'mode': 'WEEK',
    })
    return f'https://calendar.google.com/calendar/embed?{query}'


def remove_appointment_from_google_calendar(appointment):
    removed_fields = []
    failures = []
    calendar_targets = (
        (
            appointment.dentist,
            'google_calendar_event_id',
            _google_calendar_service,
        ),
        (
            None,
            'clinic_google_calendar_event_id',
            lambda _dentist: _clinic_google_calendar_service(),
        ),
    )
    for dentist, event_id_field, service_factory in calendar_targets:
        event_id = getattr(appointment, event_id_field)
        if not event_id:
            continue
        try:
            service = service_factory(dentist)
            if service is None:
                continue
            service.events().delete(
                calendarId='primary',
                eventId=event_id,
            ).execute()
        except Exception as exc:
            logger.exception(
                'Could not delete appointment %s from a Google Calendar.',
                appointment.pk,
            )
            failures.append(exc)
        else:
            setattr(appointment, event_id_field, '')
            removed_fields.append(event_id_field)

    if removed_fields:
        appointment.save(update_fields=removed_fields)
    if failures:
        raise ClinicIntegrationError(
            'The appointment could not be removed from every connected Google Calendar.'
        ) from failures[0]
    return bool(removed_fields)
