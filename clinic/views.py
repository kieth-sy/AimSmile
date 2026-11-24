import json
import hmac
import logging
import os
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.conf import settings

from accounts.audit import log_activity
from accounts.decorators import role_required, staff_required
from accounts.models import ActivityLog
from accounts.integrations import IntegrationError, create_xendit_invoice
from aiassistant.engine import no_show_risk, risk_band

from . import analytics
from .integrations import (
    ClinicIntegrationError,
    GOOGLE_CALENDAR_SCOPES,
    encrypt_calendar_credentials,
    get_clinic_google_calendar_embed_url,
    get_google_calendar_embed_url,
    remove_appointment_from_google_calendar,
    send_appointment_notifications,
    sync_appointment_to_clinic_google_calendar,
    sync_appointment_to_google_calendar,
)
from .forms import (AppointmentForm, InventoryItemForm, InvoiceForm,
                    InvoiceItemFormSet, PatientBookingForm,
                    PatientDownpaymentForm, PaymentForm, PrescriptionForm,
                    PrescriptionItemFormSet, ReviewForm, TreatmentRecordForm)
from .models import (DOWNPAYMENT_PERCENT, Appointment, AppointmentStatus,
                     ClinicGoogleCalendarConnection, InventoryItem, Invoice,
                     Payment, PaymentStatus, Prescription, ToothRecord,
                     TreatmentRecord)

User = get_user_model()
logger = logging.getLogger(__name__)


def _j(obj):
    return json.dumps(obj)


def _send_appointment_notice(appointment_id, reminder=False):
    try:
        appointment = Appointment.objects.select_related(
            'patient', 'dentist', 'service'
        ).get(pk=appointment_id)
        send_appointment_notifications(appointment, reminder=reminder)
    except Exception:
        logger.exception(
            'Could not send appointment notice for appointment %s.',
            appointment_id,
        )


def _sync_appointment_to_calendar(appointment_id):
    try:
        appointment = Appointment.objects.select_related(
            'patient', 'dentist', 'service'
        ).get(pk=appointment_id)
        sync_appointment_to_google_calendar(appointment)
    except Exception:
        logger.exception(
            'Could not sync appointment %s to Google Calendar.',
            appointment_id,
        )


def _sync_appointment_to_clinic_calendar(appointment_id):
    try:
        appointment = Appointment.objects.select_related(
            'patient', 'dentist', 'service'
        ).get(pk=appointment_id)
        sync_appointment_to_clinic_google_calendar(appointment)
    except Exception:
        logger.exception(
            'Could not sync appointment %s to the clinic Google Calendar.',
            appointment_id,
        )


def _sync_existing_appointments_to_clinic_calendar():
    appointment_ids = Appointment.objects.exclude(
        status=AppointmentStatus.CANCELLED,
    ).values_list('pk', flat=True)
    for appointment_id in appointment_ids.iterator():
        _sync_appointment_to_clinic_calendar(appointment_id)


def _remove_calendar_appointment(appointment_id):
    try:
        appointment = Appointment.objects.select_related('dentist').get(
            pk=appointment_id
        )
        remove_appointment_from_google_calendar(appointment)
    except Exception:
        logger.exception(
            'Could not remove appointment %s from Google Calendar.',
            appointment_id,
        )


def _google_oauth_failure_reason(exc):
    allowed_codes = {
        'access_denied',
        'invalid_client',
        'invalid_grant',
        'invalid_request',
        'redirect_uri_mismatch',
        'unauthorized_client',
    }
    provider_code = getattr(exc, 'error', '')
    response = getattr(exc, 'response', None)
    if not isinstance(provider_code, str) or provider_code not in allowed_codes:
        provider_code = ''
    if not provider_code and response is not None:
        try:
            response_payload = response.json()
        except (AttributeError, ValueError):
            response_payload = {}
        if isinstance(response_payload, dict):
            provider_code = response_payload.get('error', '')
    if not isinstance(provider_code, str) or provider_code not in allowed_codes:
        provider_code = ''

    reason = type(exc).__name__
    if response is not None and getattr(response, 'status_code', None):
        reason += f' HTTP {response.status_code}'
    if provider_code:
        reason += f' ({provider_code})'
    return reason


def _configure_google_oauth_transport():
    redirect_uri = urlsplit(settings.GOOGLE_CALENDAR_REDIRECT_URI)
    is_local_http = (
        settings.DEBUG
        and redirect_uri.scheme == 'http'
        and redirect_uri.hostname in {'localhost', '127.0.0.1', '::1'}
    )
    if is_local_http:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    else:
        os.environ.pop('OAUTHLIB_INSECURE_TRANSPORT', None)


# ===========================================================================
# DASHBOARDS
# ===========================================================================
@role_required('dentist')
def dentist_dashboard(request):
    me = request.user
    today = timezone.localdate()
    my_appts = Appointment.objects.filter(dentist=me)
    context = {
        'today_appointments': my_appts.filter(date=today).order_by('time'),
        'upcoming': my_appts.filter(date__gte=today).exclude(
            status__in=['completed', 'cancelled']).order_by('date', 'time')[:6],
        'patients_count': my_appts.values('patient').distinct().count(),
        'treatments_count': TreatmentRecord.objects.filter(dentist=me).count(),
        'completed_count': my_appts.filter(status='completed').count(),
        'pending_count': my_appts.filter(status='pending').count(),
        'recent_treatments': TreatmentRecord.objects.filter(dentist=me)[:6],
        'appts_month_json': _j(analytics.appointments_per_month()),
        'treatment_dist_json': _j(analytics.treatment_distribution()),
        'status_json': _j(analytics.appointments_by_status()),
    }
    return render(request, 'clinic/dashboard_dentist.html', context)


@role_required('receptionist')
def receptionist_dashboard(request):
    today = timezone.localdate()
    appts = Appointment.objects.select_related('patient', 'dentist', 'service')
    context = {
        'today_appointments': appts.filter(date=today).order_by('time')[:8],
        'pending': appts.filter(status='pending').order_by('date', 'time')[:8],
        'total_today': appts.filter(date=today).count(),
        'total_appointments': appts.count(),
        'pending_count': appts.filter(status='pending').count(),
        'confirmed_count': appts.filter(status='confirmed').count(),
        'patients_count': User.objects.filter(role='patient').count(),
        'unpaid_invoices': Invoice.objects.exclude(status='paid').count(),
        'revenue_json': _j(analytics.revenue_by_month()),
        'status_json': _j(analytics.appointments_by_status()),
        'appts_month_json': _j(analytics.appointments_per_month()),
    }
    return render(request, 'clinic/dashboard_receptionist.html', context)


@role_required('patient')
def patient_dashboard(request):
    me = request.user
    today = timezone.localdate()
    appts = Appointment.objects.filter(patient=me)
    invoices = Invoice.objects.filter(patient=me)
    outstanding = sum((inv.balance for inv in invoices), 0)
    tooth_records = {t.tooth_number: t for t in ToothRecord.objects.filter(patient=me)}
    context = {
        'upcoming': appts.filter(date__gte=today).exclude(
            status__in=['completed', 'cancelled']).order_by('date', 'time'),
        'past': appts.filter(Q(date__lt=today) | Q(status__in=['completed', 'cancelled']))[:6],
        'appointments_count': appts.count(),
        'treatments_count': TreatmentRecord.objects.filter(patient=me).count(),
        'prescriptions_count': Prescription.objects.filter(patient=me).count(),
        'outstanding': outstanding,
        'invoices': invoices[:5],
        'tooth_records': tooth_records,
        'tooth_range_upper': range(1, 17),
        'tooth_range_lower': range(17, 33),
        'recent_treatments': TreatmentRecord.objects.filter(patient=me)[:5],
        'prescriptions': Prescription.objects.filter(patient=me)[:5],
    }
    return render(request, 'clinic/dashboard_patient.html', context)


# ===========================================================================
# APPOINTMENTS
# ===========================================================================
@login_required
def appointment_list(request):
    user = request.user
    qs = Appointment.objects.select_related('patient', 'dentist', 'service')
    if user.is_patient():
        qs = qs.filter(patient=user)
    elif user.is_dentist():
        qs = qs.filter(dentist=user)
    status = request.GET.get('status')
    if status:
        qs = qs.filter(status=status)
    search = request.GET.get('q')
    if search:
        qs = qs.filter(Q(patient__first_name__icontains=search) |
                       Q(patient__last_name__icontains=search) |
                       Q(reason__icontains=search))

    first_appt = qs.first()
    appts_list = []
    for a in qs:
        appts_list.append({
            'id': a.id,
            'dentist_id': a.dentist_id,
            'name': a.patient.display_name if a.patient else 'Unknown',
            'phone': getattr(a.patient, 'phone', '') or '0917 123 4567',
            'dentist': f"Dr. {a.dentist.display_name}" if a.dentist else "Unassigned",
            'service': a.service.name if a.service else (a.reason or "Consultation"),
            'date_iso': a.date.isoformat() if a.date else '',
            'date_full': a.date.strftime('%A, %B %d, %Y') if a.date else '',
            'time': a.time.strftime('%I:%M %p') if a.time else '',
            'time_24': a.time.strftime('%H:%M') if a.time else '',
            'duration_minutes': a.service.duration_minutes if a.service else 30,
            'status': a.get_status_display(),
            'url': reverse('clinic:appointment_detail', args=(a.pk,)),
        })

    busy_slots = []
    dentists = User.objects.none()
    if user.is_patient():
        dentists = User.objects.filter(role='dentist').order_by(
            'first_name',
            'last_name',
            'username',
        )
        other_bookings = Appointment.objects.filter(
            date__gte=timezone.localdate(),
            status__in=[AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED],
            dentist__isnull=False,
        ).exclude(
            patient=user,
        ).select_related(
            'service',
        ).order_by(
            'date',
            'time',
        )
        for booking in other_bookings:
            busy_slots.append({
                'dentist_id': booking.dentist_id,
                'date_iso': booking.date.isoformat(),
                'date_full': booking.date.strftime('%A, %B %d, %Y'),
                'time': booking.time.strftime('%I:%M %p'),
                'time_24': booking.time.strftime('%H:%M'),
                'duration_minutes': (
                    booking.service.duration_minutes if booking.service else 30
                ),
            })

    google_calendar_connected = False
    clinic_google_calendar_connected = False
    google_calendar_embed_url = ''
    if user.is_dentist():
        doctor_profile = getattr(user, 'doctor_profile', None)
        google_calendar_connected = bool(
            doctor_profile and doctor_profile.google_calendar_credentials
        )
        if google_calendar_connected:
            try:
                google_calendar_embed_url = get_google_calendar_embed_url(user)
            except ClinicIntegrationError as exc:
                logger.warning(
                    'Could not embed Google Calendar for user %s (%s).',
                    user.pk,
                    type(exc).__name__,
                )
                messages.warning(
                    request,
                    'Reconnect Google Calendar to grant the calendar access needed to display it here.',
                )
    elif user.is_admin():
        clinic_connection = ClinicGoogleCalendarConnection.objects.filter(
            pk=1,
        ).first()
        clinic_google_calendar_connected = bool(
            clinic_connection and clinic_connection.google_calendar_credentials
        )
        if clinic_google_calendar_connected:
            try:
                google_calendar_embed_url = get_clinic_google_calendar_embed_url()
            except ClinicIntegrationError as exc:
                logger.warning(
                    'Could not embed the clinic Google Calendar for user %s (%s).',
                    user.pk,
                    type(exc).__name__,
                )
                messages.warning(
                    request,
                    'Reconnect the shared clinic Google Calendar to display it here.',
                )

    return render(request, 'clinic/appointment_list.html', {
        'appointments': qs,
        'appointments_count': qs.count(),
        'first_appointment': first_appt,
        'appointments_data': appts_list,
        'busy_slots_data': busy_slots,
        'dentists': dentists,
        'calendar_today': timezone.localdate().isoformat(),
        'statuses': AppointmentStatus.choices,
        'current_status': status,
        'search': search or '',
        'google_calendar_connected': google_calendar_connected,
        'clinic_google_calendar_connected': clinic_google_calendar_connected,
        'google_calendar_embed_url': google_calendar_embed_url,
    })


@login_required
def appointment_create(request):
    user = request.user
    patient_mode = user.is_patient()
    FormClass = PatientBookingForm if patient_mode else AppointmentForm
    if request.method == 'POST':
        form = FormClass(request.POST)
        if form.is_valid():
            appt = form.save(commit=False)
            if patient_mode:
                appt.patient = user
                appt.status = AppointmentStatus.PENDING
            appt.save()
            appt.no_show_risk = no_show_risk(appt)
            appt.save(update_fields=['no_show_risk'])
            log_activity(
                request,
                ActivityLog.Action.APPOINTMENT_CREATED,
                'Appointment created.',
                target=appt,
            )
            transaction.on_commit(
                lambda appointment_id=appt.pk: _send_appointment_notice(appointment_id)
            )
            transaction.on_commit(
                lambda appointment_id=appt.pk: _sync_appointment_to_calendar(
                    appointment_id
                )
            )
            messages.success(request, 'Appointment booked successfully!')
            return redirect('clinic:appointment_list')
    else:
        form = FormClass()
    return render(request, 'clinic/form_page.html',
                  {'form': form, 'title': 'Book Appointment'})


@staff_required
def appointment_update(request, pk):
    appt = get_object_or_404(Appointment, pk=pk)
    if request.method == 'POST':
        form = AppointmentForm(request.POST, instance=appt)
        if form.is_valid():
            previous_status = appt.status
            appt = form.save()
            appt.no_show_risk = no_show_risk(appt)
            appt.save(update_fields=['no_show_risk'])
            log_activity(
                request,
                ActivityLog.Action.APPOINTMENT_UPDATED,
                'Appointment updated.',
                target=appt,
            )
            if appt.status == AppointmentStatus.CANCELLED:
                transaction.on_commit(
                    lambda appointment_id=appt.pk: _remove_calendar_appointment(
                        appointment_id
                    )
                )
            else:
                transaction.on_commit(
                    lambda appointment_id=appt.pk: _sync_appointment_to_calendar(
                        appointment_id
                    )
                )
            if (
                appt.status == AppointmentStatus.CONFIRMED
                and previous_status != AppointmentStatus.CONFIRMED
            ):
                transaction.on_commit(
                    lambda appointment_id=appt.pk: _send_appointment_notice(
                        appointment_id
                    )
                )
            messages.success(request, 'Appointment updated.')
            return redirect('clinic:appointment_list')
    else:
        form = AppointmentForm(instance=appt)
    return render(request, 'clinic/form_page.html',
                  {'form': form, 'title': f'Edit Appointment #{appt.id}'})


@login_required
def appointment_detail(request, pk):
    appt = get_object_or_404(
        Appointment.objects.select_related('patient', 'dentist', 'service'), pk=pk)
    user = request.user
    if user.is_patient() and appt.patient_id != user.id:
        raise PermissionDenied
    if user.is_dentist() and appt.dentist_id != user.id:
        raise PermissionDenied
    band, color = risk_band(appt.no_show_risk)
    return render(request, 'clinic/appointment_detail.html',
                  {'appt': appt, 'risk_band': band, 'risk_color': color})


@staff_required
@require_POST
def appointment_set_status(request, pk, status):
    appt = get_object_or_404(Appointment, pk=pk)
    if status in dict(AppointmentStatus.choices):
        previous_status = appt.status
        appt.status = status
        appt.save(update_fields=['status'])
        if status != previous_status:
            log_activity(
                request,
                ActivityLog.Action.APPOINTMENT_STATUS_CHANGED,
                f'Appointment status changed to {appt.get_status_display()}.',
                target=appt,
            )
        if status == AppointmentStatus.CONFIRMED:
            transaction.on_commit(
                lambda appointment_id=appt.pk: _sync_appointment_to_calendar(
                    appointment_id
                )
            )
            if previous_status != AppointmentStatus.CONFIRMED:
                transaction.on_commit(
                    lambda appointment_id=appt.pk: _send_appointment_notice(
                        appointment_id
                    )
                )
        elif status == AppointmentStatus.CANCELLED:
            transaction.on_commit(
                lambda appointment_id=appt.pk: _remove_calendar_appointment(
                    appointment_id
                )
            )
        messages.success(request, f'Appointment marked as {appt.get_status_display()}.')
    return redirect(request.META.get('HTTP_REFERER', 'clinic:appointment_list'))


@login_required
def appointment_cancel(request, pk):
    appt = get_object_or_404(Appointment, pk=pk)
    user = request.user
    if user.is_patient() and appt.patient_id != user.id:
        raise PermissionDenied
    was_cancelled = appt.status == AppointmentStatus.CANCELLED
    appt.status = AppointmentStatus.CANCELLED
    appt.save(update_fields=['status'])
    if not was_cancelled:
        log_activity(
            request,
            ActivityLog.Action.APPOINTMENT_CANCELLED,
            'Appointment cancelled.',
            target=appt,
        )
    if appt.google_calendar_event_id or appt.clinic_google_calendar_event_id:
        transaction.on_commit(
            lambda appointment_id=appt.pk: _remove_calendar_appointment(appointment_id)
        )
    messages.info(request, 'Appointment cancelled.')
    return redirect(request.META.get('HTTP_REFERER', 'clinic:appointment_list'))


@role_required('dentist', 'admin')
def google_calendar_connect(request):
    required_settings = {
        'GOOGLE_CALENDAR_CLIENT_ID': settings.GOOGLE_CALENDAR_CLIENT_ID,
        'GOOGLE_CALENDAR_CLIENT_SECRET': settings.GOOGLE_CALENDAR_CLIENT_SECRET,
        'GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY': (
            settings.GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY
        ),
    }
    missing_settings = [
        name for name, value in required_settings.items() if not value
    ]
    if missing_settings:
        messages.error(
            request,
            'Google Calendar setup is missing: '
            + ', '.join(missing_settings)
            + '.',
        )
        return redirect('accounts:redirect_dashboard')

    try:
        _configure_google_oauth_transport()
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                'web': {
                    'client_id': settings.GOOGLE_CALENDAR_CLIENT_ID,
                    'client_secret': settings.GOOGLE_CALENDAR_CLIENT_SECRET,
                    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                    'token_uri': 'https://oauth2.googleapis.com/token',
                    'redirect_uris': [settings.GOOGLE_CALENDAR_REDIRECT_URI],
                }
            },
            scopes=GOOGLE_CALENDAR_SCOPES,
            redirect_uri=settings.GOOGLE_CALENDAR_REDIRECT_URI,
        )
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent',
        )
    except ImportError:
        logger.exception('Google Calendar OAuth package is not installed.')
        messages.error(request, 'Google Calendar integration dependencies are missing.')
        return redirect('accounts:redirect_dashboard')

    request.session['google_calendar_oauth_state'] = state
    request.session['google_calendar_oauth_code_verifier'] = flow.code_verifier
    return redirect(authorization_url)


@role_required('dentist', 'admin')
def google_calendar_callback(request):
    expected_state = request.session.pop('google_calendar_oauth_state', '')
    code_verifier = request.session.pop(
        'google_calendar_oauth_code_verifier',
        '',
    )
    if (
        not expected_state
        or request.GET.get('state') != expected_state
        or not code_verifier
    ):
        messages.error(request, 'Google Calendar authorization could not be verified.')
        return redirect('accounts:redirect_dashboard')
    if request.GET.get('error'):
        messages.error(request, 'Google Calendar connection was not approved.')
        return redirect('accounts:redirect_dashboard')

    try:
        _configure_google_oauth_transport()
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                'web': {
                    'client_id': settings.GOOGLE_CALENDAR_CLIENT_ID,
                    'client_secret': settings.GOOGLE_CALENDAR_CLIENT_SECRET,
                    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                    'token_uri': 'https://oauth2.googleapis.com/token',
                    'redirect_uris': [settings.GOOGLE_CALENDAR_REDIRECT_URI],
                }
            },
            scopes=GOOGLE_CALENDAR_SCOPES,
            state=expected_state,
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
            redirect_uri=settings.GOOGLE_CALENDAR_REDIRECT_URI,
        )
        flow.fetch_token(authorization_response=request.build_absolute_uri())
        encrypted_credentials = encrypt_calendar_credentials(
            flow.credentials.to_json()
        )
        if request.user.is_dentist():
            profile = request.user.doctor_profile
            profile.google_calendar_credentials = encrypted_credentials
            profile.save(update_fields=['google_calendar_credentials'])
        else:
            connection, _ = ClinicGoogleCalendarConnection.objects.get_or_create(
                pk=1,
            )
            connection.google_calendar_credentials = encrypted_credentials
            connection.connected_by = request.user
            connection.save(update_fields=[
                'google_calendar_credentials',
                'connected_by',
                'updated_at',
            ])
            transaction.on_commit(_sync_existing_appointments_to_clinic_calendar)
    except Exception as exc:
        reason = _google_oauth_failure_reason(exc)
        logger.error(
            'Google Calendar OAuth callback failed for user %s (%s).',
            request.user.pk,
            reason,
        )
        messages.error(
            request,
            'Google Calendar authorization failed ('
            + reason
            + '). Check the OAuth client settings and try again.',
        )
        return redirect('accounts:redirect_dashboard')

    if request.user.is_admin():
        messages.success(
            request,
            'Shared clinic Google Calendar connected. Existing appointments are syncing.',
        )
    else:
        messages.success(request, 'Google Calendar connected successfully.')
    return redirect('accounts:redirect_dashboard')


@staff_required
def appointment_delete(request, pk):
    appt = get_object_or_404(Appointment, pk=pk)
    if request.method == 'POST':
        appt.delete()
        log_activity(
            request,
            ActivityLog.Action.APPOINTMENT_DELETED,
            'Appointment deleted.',
            target=appt,
        )
        messages.success(request, 'Appointment deleted.')
        return redirect('clinic:appointment_list')
    return render(request, 'clinic/confirm_delete.html',
                  {'object': appt, 'title': 'Delete Appointment',
                   'cancel_url': 'clinic:appointment_list'})


# ===========================================================================
# PATIENTS
# ===========================================================================
@staff_required
def patient_list(request):
    search = request.GET.get('q', '')
    patients = User.objects.filter(role='patient').select_related('patient_profile')
    if search:
        patients = patients.filter(Q(first_name__icontains=search) |
                                   Q(last_name__icontains=search) |
                                   Q(email__icontains=search))
    return render(request, 'clinic/patient_list.html',
                  {'patients': patients, 'search': search})


@staff_required
def patient_detail(request, pk):
    patient = get_object_or_404(User, pk=pk, role='patient')
    tooth_records = {t.tooth_number: t for t in ToothRecord.objects.filter(patient=patient)}
    return render(request, 'clinic/patient_detail.html', {
        'patient': patient,
        'appointments': Appointment.objects.filter(patient=patient)[:8],
        'treatments': TreatmentRecord.objects.filter(patient=patient)[:8],
        'prescriptions': Prescription.objects.filter(patient=patient)[:8],
        'invoices': Invoice.objects.filter(patient=patient)[:8],
        'tooth_records': tooth_records,
        'tooth_range_upper': range(1, 17),
        'tooth_range_lower': range(17, 33),
        'conditions': ToothRecord._meta.get_field('condition').choices,
    })


@staff_required
def tooth_update(request, pk):
    """Add/update a tooth record for a patient (dentist/staff)."""
    patient = get_object_or_404(User, pk=pk, role='patient')
    if request.method == 'POST':
        number = request.POST.get('tooth_number')
        condition = request.POST.get('condition')
        note = request.POST.get('note', '')
        if number and condition:
            ToothRecord.objects.update_or_create(
                patient=patient, tooth_number=number,
                defaults={'condition': condition, 'note': note})
            messages.success(request, f'Tooth {number} updated.')
    return redirect('clinic:patient_detail', pk=patient.pk)


# ===========================================================================
# TREATMENTS
# ===========================================================================
@login_required
def treatment_list(request):
    user = request.user
    qs = TreatmentRecord.objects.select_related('patient', 'dentist', 'service')
    if user.is_patient():
        qs = qs.filter(patient=user)
    elif user.is_dentist():
        qs = qs.filter(dentist=user)
    return render(request, 'clinic/treatment_list.html', {'treatments': qs})


@role_required('dentist', 'admin', 'receptionist')
def treatment_create(request):
    if request.method == 'POST':
        form = TreatmentRecordForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Treatment record added.')
            return redirect('clinic:treatment_list')
    else:
        initial = {}
        if request.user.is_dentist():
            initial['dentist'] = request.user
        form = TreatmentRecordForm(initial=initial)
    return render(request, 'clinic/form_page.html',
                  {'form': form, 'title': 'New Treatment Record'})


@role_required('dentist', 'admin', 'receptionist')
def treatment_update(request, pk):
    treatment = get_object_or_404(TreatmentRecord, pk=pk)
    if request.method == 'POST':
        form = TreatmentRecordForm(request.POST, instance=treatment)
        if form.is_valid():
            form.save()
            messages.success(request, 'Treatment updated.')
            return redirect('clinic:treatment_list')
    else:
        form = TreatmentRecordForm(instance=treatment)
    return render(request, 'clinic/form_page.html',
                  {'form': form, 'title': 'Edit Treatment'})


@role_required('dentist', 'admin')
def treatment_delete(request, pk):
    treatment = get_object_or_404(TreatmentRecord, pk=pk)
    if request.method == 'POST':
        treatment.delete()
        messages.success(request, 'Treatment deleted.')
        return redirect('clinic:treatment_list')
    return render(request, 'clinic/confirm_delete.html',
                  {'object': treatment, 'title': 'Delete Treatment',
                   'cancel_url': 'clinic:treatment_list'})


# ===========================================================================
# PRESCRIPTIONS
# ===========================================================================
@login_required
def prescription_list(request):
    user = request.user
    qs = Prescription.objects.select_related('patient', 'dentist')
    if user.is_patient():
        qs = qs.filter(patient=user)
    elif user.is_dentist():
        qs = qs.filter(dentist=user)
    return render(request, 'clinic/prescription_list.html', {'prescriptions': qs})


@role_required('dentist', 'admin')
def prescription_create(request):
    if request.method == 'POST':
        form = PrescriptionForm(request.POST)
        formset = PrescriptionItemFormSet(request.POST, prefix='items')
        if form.is_valid() and formset.is_valid():
            rx = form.save()
            formset.instance = rx
            formset.save()
            messages.success(request, 'Prescription created.')
            return redirect('clinic:prescription_detail', pk=rx.pk)
    else:
        initial = {'dentist': request.user} if request.user.is_dentist() else {}
        form = PrescriptionForm(initial=initial)
        formset = PrescriptionItemFormSet(prefix='items')
    return render(request, 'clinic/prescription_form.html',
                  {'form': form, 'formset': formset, 'title': 'New Prescription'})


@login_required
def prescription_detail(request, pk):
    rx = get_object_or_404(Prescription.objects.select_related('patient', 'dentist'), pk=pk)
    if request.user.is_patient() and rx.patient_id != request.user.id:
        raise PermissionDenied
    return render(request, 'clinic/prescription_detail.html', {'rx': rx})


@role_required('dentist', 'admin')
def prescription_delete(request, pk):
    rx = get_object_or_404(Prescription, pk=pk)
    if request.method == 'POST':
        rx.delete()
        messages.success(request, 'Prescription deleted.')
        return redirect('clinic:prescription_list')
    return render(request, 'clinic/confirm_delete.html',
                  {'object': rx, 'title': 'Delete Prescription',
                   'cancel_url': 'clinic:prescription_list'})


# ===========================================================================
# INVOICES & PAYMENTS
# ===========================================================================
@login_required
def invoice_list(request):
    user = request.user
    qs = Invoice.objects.select_related('patient').prefetch_related('payments')
    if user.is_patient():
        qs = qs.filter(patient=user)
    return render(request, 'clinic/invoice_list.html', {'invoices': qs})


@role_required('receptionist', 'admin')
def invoice_create(request):
    if request.method == 'POST':
        form = InvoiceForm(request.POST)
        formset = InvoiceItemFormSet(request.POST, prefix='items')
        if form.is_valid() and formset.is_valid():
            invoice = form.save()
            formset.instance = invoice
            formset.save()
            invoice.recalculate_status()
            log_activity(
                request,
                ActivityLog.Action.INVOICE_CREATED,
                'Invoice created.',
                target=invoice,
            )
            messages.success(request, f'Invoice {invoice.number} created.')
            return redirect('clinic:invoice_detail', pk=invoice.pk)
    else:
        form = InvoiceForm()
        formset = InvoiceItemFormSet(prefix='items')
    return render(request, 'clinic/invoice_form.html',
                  {'form': form, 'formset': formset, 'title': 'New Invoice'})


@login_required
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('patient'), pk=pk)
    if request.user.is_patient() and invoice.patient_id != request.user.id:
        raise PermissionDenied
    return render(request, 'clinic/invoice_detail.html',
                  _invoice_detail_context(invoice))


@login_required
def payment_receipt(request, pk):
    payment = get_object_or_404(
        Payment.objects.select_related('invoice', 'invoice__patient'),
        pk=pk,
        status=PaymentStatus.CONFIRMED,
    )
    if request.user.is_patient():
        if payment.invoice.patient_id != request.user.id:
            raise PermissionDenied
    elif not (request.user.is_admin() or request.user.is_receptionist()):
        raise PermissionDenied

    return render(request, 'clinic/payment_receipt.html', {
        'payment': payment,
        'invoice': payment.invoice,
        'receipt_number': f'RCT-{payment.pk:06d}',
    })


def _invoice_detail_context(invoice, payment_form=None, downpayment_form=None):
    if payment_form is None:
        payment_form = PaymentForm(
            initial={'amount': invoice.downpayment_due or invoice.balance}
        )
    if downpayment_form is None:
        downpayment_form = PatientDownpaymentForm()
    return {
        'invoice': invoice,
        'xendit_enabled': settings.XENDIT_ENABLED,
        'payment_form': payment_form,
        'downpayment_form': downpayment_form,
        'downpayment_amount': invoice.downpayment_due,
        'downpayment_percent': DOWNPAYMENT_PERCENT,
        'first_payment_required': (
            invoice.downpayment_due if invoice.amount_paid <= 0 else Decimal('0.00')
        ),
        'pending_downpayment': invoice.payments.filter(
            status=PaymentStatus.PENDING
        ).first(),
    }


@login_required
@require_POST
def invoice_add_payment(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    if request.user.is_patient():
        if invoice.patient_id != request.user.id:
            raise PermissionDenied
        form = PatientDownpaymentForm(request.POST)
        if not form.is_valid():
            messages.error(request, 'Downpayment request was not submitted. Check the highlighted fields.')
            return render(request, 'clinic/invoice_detail.html',
                          _invoice_detail_context(invoice, downpayment_form=form))

        with transaction.atomic():
            invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
            pending_exists = invoice.payments.filter(
                status=PaymentStatus.PENDING
            ).exists()
            amount = invoice.downpayment_due
            if pending_exists:
                messages.info(request, 'A downpayment request is already awaiting verification.')
            elif amount <= 0:
                messages.info(request, 'The required downpayment has already been met.')
            else:
                payment = Payment.objects.create(
                    invoice=invoice,
                    amount=amount,
                    method=form.cleaned_data['method'],
                    paid_on=timezone.localdate(),
                    reference=form.cleaned_data['reference'],
                    status=PaymentStatus.PENDING,
                )
                log_activity(
                    request,
                    ActivityLog.Action.PAYMENT_SUBMITTED,
                    'Payment request submitted.',
                    target=payment,
                )
                messages.success(
                    request,
                    'Downpayment request submitted. Clinic staff must verify it before the balance is updated.',
                )
        return redirect('clinic:invoice_detail', pk=invoice.pk)

    if not (request.user.is_superuser or request.user.role in ('admin', 'receptionist')):
        raise PermissionDenied

    form = PaymentForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Payment was not recorded. Correct the highlighted fields.')
        return render(request, 'clinic/invoice_detail.html',
                      _invoice_detail_context(invoice, payment_form=form))

    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        amount = form.cleaned_data['amount']
        if amount > invoice.balance:
            form.add_error('amount', 'Payment cannot exceed the remaining invoice balance.')
        elif invoice.amount_paid <= 0 and amount < invoice.downpayment_due:
            form.add_error(
                'amount',
                f'The first payment must cover the required downpayment of {invoice.downpayment_due}.',
            )
        elif invoice.payments.filter(status=PaymentStatus.PENDING).exists():
            form.add_error(
                None,
                'Review the pending payment request before recording another payment.',
            )
        else:
            payment = form.save(commit=False)
            payment.invoice = invoice
            payment.status = PaymentStatus.CONFIRMED
            payment.save()
            invoice.recalculate_status()
            log_activity(
                request,
                ActivityLog.Action.PAYMENT_RECORDED,
                'Payment recorded by clinic staff.',
                target=payment,
            )
            messages.success(request, 'Payment recorded.')

    if form.errors:
        messages.error(request, 'Payment was not recorded. Correct the highlighted fields.')
        return render(request, 'clinic/invoice_detail.html',
                      _invoice_detail_context(invoice, payment_form=form))
    return redirect('clinic:invoice_detail', pk=invoice.pk)


@login_required
@require_POST
def invoice_create_xendit_checkout(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    if request.user.is_patient() and invoice.patient_id != request.user.id:
        raise PermissionDenied
    if not (request.user.is_patient() or request.user.is_receptionist() or request.user.is_admin()):
        raise PermissionDenied

    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if request.user.is_patient():
            if invoice.payments.filter(status=PaymentStatus.PENDING).exists():
                messages.info(
                    request,
                    'A payment request is awaiting clinic verification. Please wait before starting another checkout.',
                )
                return redirect('clinic:invoice_detail', pk=invoice.pk)
            amount = invoice.downpayment_due or invoice.balance
            description = (
                f'Downpayment for {invoice.number}'
                if invoice.downpayment_due > 0
                else f'Remaining balance for {invoice.number}'
            )
        else:
            amount = invoice.balance
            description = f'Payment for {invoice.number}'

    if amount <= 0:
        messages.info(request, 'There is no remaining balance to pay.')
        return redirect('clinic:invoice_detail', pk=invoice.pk)
    try:
        checkout = create_xendit_invoice(invoice, amount, description)
    except IntegrationError as exc:
        messages.error(request, str(exc))
        return redirect('clinic:invoice_detail', pk=invoice.pk)
    return redirect(checkout['invoice_url'])


@csrf_exempt
@require_POST
def xendit_payment_webhook(request):
    callback_token = request.headers.get('x-callback-token', '')
    if not settings.XENDIT_CALLBACK_TOKEN:
        return JsonResponse({'detail': 'Callback token is not configured.'}, status=503)
    if not hmac.compare_digest(callback_token, settings.XENDIT_CALLBACK_TOKEN):
        return JsonResponse({'detail': 'Invalid callback token.'}, status=401)
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'detail': 'Invalid JSON.'}, status=400)

    if payload.get('status') not in ('PAID', 'SETTLED'):
        return JsonResponse({'received': True})
    external_id = str(payload.get('external_id', ''))
    invoice_number = external_id.rsplit('-', 1)[0]
    if not invoice_number.startswith('INV-'):
        invoice_number = external_id
    invoice = Invoice.objects.filter(number=invoice_number).first()
    if not invoice:
        return JsonResponse({'detail': 'Invoice not found.'}, status=404)

    amount = payload.get('paid_amount') or payload.get('amount')
    try:
        amount = Decimal(str(amount))
    except (TypeError, InvalidOperation):
        return JsonResponse({'detail': 'Invalid payment amount.'}, status=400)
    if amount <= 0:
        return JsonResponse({'detail': 'Invalid payment amount.'}, status=400)

    reference = str(payload.get('id') or external_id)
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.amount_paid <= 0 and amount < invoice.downpayment_due:
            return JsonResponse(
                {'detail': 'The first payment must cover the required downpayment.'},
                status=400,
            )
        if not invoice.payments.filter(reference=reference).exists():
            remaining = invoice.balance
            confirmed_amount = min(amount, remaining)
            if confirmed_amount > 0:
                Payment.objects.create(
                    invoice=invoice,
                    amount=confirmed_amount,
                    method='xendit',
                    paid_on=timezone.localdate(),
                    reference=reference,
                    status=PaymentStatus.CONFIRMED,
                )
                invoice.recalculate_status()
    return JsonResponse({'received': True})


@role_required('receptionist', 'admin')
@require_POST
def invoice_review_payment(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    action = request.POST.get('action')
    if action not in ('confirm', 'reject'):
        messages.error(request, 'Choose whether to confirm or reject this payment request.')
        return redirect('clinic:invoice_detail', pk=payment.invoice_id)

    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=payment.invoice_id)
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != PaymentStatus.PENDING:
            messages.info(request, 'This payment request has already been reviewed.')
        elif action == 'confirm' and payment.amount > invoice.balance:
            messages.error(
                request,
                'This request exceeds the remaining balance. Reject it and ask the patient to submit a new request.',
            )
        elif action == 'confirm':
            payment.status = PaymentStatus.CONFIRMED
            payment.save(update_fields=['status'])
            invoice.recalculate_status()
            log_activity(
                request,
                ActivityLog.Action.PAYMENT_REVIEWED,
                'Payment request confirmed.',
                target=payment,
            )
            messages.success(request, 'Payment verified and applied to the invoice.')
        else:
            payment.status = PaymentStatus.REJECTED
            payment.save(update_fields=['status'])
            log_activity(
                request,
                ActivityLog.Action.PAYMENT_REVIEWED,
                'Payment request rejected.',
                target=payment,
            )
            messages.success(request, 'Payment request rejected.')

    return redirect('clinic:invoice_detail', pk=invoice.pk)


@role_required('receptionist', 'admin')
def invoice_delete(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    if request.method == 'POST':
        invoice.delete()
        log_activity(
            request,
            ActivityLog.Action.INVOICE_DELETED,
            'Invoice deleted.',
            target=invoice,
        )
        messages.success(request, 'Invoice deleted.')
        return redirect('clinic:invoice_list')
    return render(request, 'clinic/confirm_delete.html',
                  {'object': invoice, 'title': 'Delete Invoice',
                   'cancel_url': 'clinic:invoice_list'})


# ===========================================================================
# INVENTORY
# ===========================================================================
@staff_required
def inventory_list(request):
    items = InventoryItem.objects.all()
    return render(request, 'clinic/inventory_list.html', {
        'items': items,
        'low_count': sum(1 for i in items if i.is_low),
        'total_value': items.aggregate(v=Sum('unit_price'))['v'] or 0,
    })


@staff_required
def inventory_create(request):
    if request.method == 'POST':
        form = InventoryItemForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Inventory item added.')
            return redirect('clinic:inventory_list')
    else:
        form = InventoryItemForm()
    return render(request, 'clinic/form_page.html',
                  {'form': form, 'title': 'Add Inventory Item'})


@staff_required
def inventory_update(request, pk):
    item = get_object_or_404(InventoryItem, pk=pk)
    if request.method == 'POST':
        form = InventoryItemForm(request.POST, instance=item)
        if form.is_valid():
            form.save()
            messages.success(request, 'Inventory item updated.')
            return redirect('clinic:inventory_list')
    else:
        form = InventoryItemForm(instance=item)
    return render(request, 'clinic/form_page.html',
                  {'form': form, 'title': 'Edit Inventory Item'})


@staff_required
def inventory_delete(request, pk):
    item = get_object_or_404(InventoryItem, pk=pk)
    if request.method == 'POST':
        item.delete()
        messages.success(request, 'Item deleted.')
        return redirect('clinic:inventory_list')
    return render(request, 'clinic/confirm_delete.html',
                  {'object': item, 'title': 'Delete Item',
                   'cancel_url': 'clinic:inventory_list'})


# ===========================================================================
# REVIEWS
# ===========================================================================
@role_required('patient')
def review_create(request):
    if request.method == 'POST':
        form = ReviewForm(request.POST)
        if form.is_valid():
            review = form.save(commit=False)
            review.patient = request.user
            review.save()
            messages.success(request, 'Thanks for your feedback!')
            return redirect('clinic:patient_dashboard')
    else:
        form = ReviewForm()
    return render(request, 'clinic/form_page.html',
                  {'form': form, 'title': 'Leave a Review'})
