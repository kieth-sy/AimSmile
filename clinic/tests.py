import json
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from urllib.parse import parse_qs
from unittest.mock import PropertyMock, patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from cryptography.fernet import Fernet

from accounts.models import ActivityLog
from clinic import analytics
from clinic.integrations import (
    ClinicIntegrationError,
    decrypt_calendar_credentials,
    encrypt_calendar_credentials,
    get_clinic_google_calendar_embed_url,
    get_google_calendar_embed_url,
    remove_appointment_from_google_calendar,
    send_appointment_notifications,
    send_semaphore_sms,
    sync_appointment_to_clinic_google_calendar,
    sync_appointment_to_google_calendar,
)
from clinic.views import _configure_google_oauth_transport
from clinic.models import (Appointment, AppointmentStatus,
                           ClinicGoogleCalendarConnection, Invoice,
                           InvoiceItem, InvoiceStatus, Payment, PaymentMethod,
                           PaymentStatus, Service)
from clinic.forms import PatientDownpaymentForm

User = get_user_model()


class AnalyticsNewestFirstTests(TestCase):
    months = [(2025, 12)] + [(2026, month) for month in range(1, 12)]

    @patch('clinic.analytics.Payment')
    @patch('clinic.analytics._last_12_months', return_value=months)
    def test_revenue_starts_with_november_2026(self, _months, payment_model):
        payment_model.objects.filter.return_value.values_list.return_value = [
            (date(2026, 11, 1), Decimal('350')),
            (date(2025, 12, 1), Decimal('120')),
        ]

        result = analytics.revenue_by_month()

        self.assertEqual(result['labels'][0], 'Nov 26')
        self.assertEqual(result['labels'][-1], 'Dec 25')
        self.assertEqual(result['data'][0], 350.0)
        self.assertEqual(result['data'][-1], 120.0)

    @patch('clinic.analytics.User')
    @patch('clinic.analytics._last_12_months', return_value=months)
    def test_patient_growth_starts_with_november_2026(self, _months, user_model):
        user_model.objects.filter.return_value.values_list.return_value = [
            timezone.make_aware(datetime(2025, 12, 10)),
            timezone.make_aware(datetime(2026, 11, 10)),
        ]

        result = analytics.patient_growth()

        self.assertEqual(result['labels'][0], 'Nov 26')
        self.assertEqual(result['labels'][-1], 'Dec 25')
        self.assertEqual(result['data'][0], 2)
        self.assertEqual(result['data'][-1], 1)


class AppointmentCalendarTests(TestCase):
    def test_calendar_receives_appointments_with_service_duration(self):
        admin = User.objects.create_user(
            username='calendar-admin',
            password='calendar-pass-928!',
            role='admin',
        )
        patient = User.objects.create_user(
            username='calendar-patient',
            password='calendar-pass-928!',
            role='patient',
        )
        service = Service.objects.create(name='Cleaning', duration_minutes=45)
        appointment = Appointment.objects.create(
            patient=patient,
            service=service,
            date=date(2026, 11, 20),
            time=time(9, 30),
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('clinic:appointment_list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['appointments_data'],
            [{
                'id': appointment.id,
                'dentist_id': None,
                'name': patient.display_name,
                'phone': patient.phone or '0917 123 4567',
                'dentist': 'Unassigned',
                'service': 'Cleaning',
                'date_iso': '2026-11-20',
                'date_full': 'Friday, November 20, 2026',
                'time': '09:30 AM',
                'time_24': '09:30',
                'duration_minutes': 45,
                'status': 'Pending',
                'url': reverse('clinic:appointment_detail', args=(appointment.pk,)),
            }],
        )
        self.assertContains(response, 'class="nm-cal-table"')
        self.assertContains(response, 'id="appointmentCalendar"')

    def test_patient_calendar_shows_own_appointments_and_anonymous_busy_slots(self):
        patient = User.objects.create_user(
            username='calendar-patient-owner',
            password='calendar-pass-928!',
            first_name='Own',
            last_name='Patient',
            role='patient',
        )
        other_patient = User.objects.create_user(
            username='calendar-patient-other',
            password='calendar-pass-928!',
            first_name='Private',
            last_name='Patient',
            role='patient',
        )
        dentist = User.objects.create_user(
            username='calendar-availability-dentist',
            password='calendar-pass-928!',
            first_name='Ava',
            last_name='Dentist',
            role='dentist',
        )
        own_appointment = Appointment.objects.create(
            patient=patient,
            dentist=dentist,
            service=Service.objects.create(name='Patient Cleaning'),
            date=timezone.localdate(),
            time=time(9, 0),
        )
        Appointment.objects.create(
            patient=other_patient,
            dentist=dentist,
            service=Service.objects.create(name='Private Treatment'),
            date=timezone.localdate(),
            time=time(10, 0),
        )
        self.client.force_login(patient)

        response = self.client.get(reverse('clinic:appointment_list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item['id'] for item in response.context['appointments_data']],
            [own_appointment.pk],
        )
        self.assertEqual(
            response.context['busy_slots_data'],
            [{
                'dentist_id': dentist.pk,
                'date_iso': timezone.localdate().isoformat(),
                'date_full': timezone.localdate().strftime('%A, %B %d, %Y'),
                'time': '10:00 AM',
                'time_24': '10:00',
                'duration_minutes': 30,
            }],
        )
        self.assertContains(response, 'id="availabilityDentist"')
        self.assertContains(response, 'Dr. Ava Dentist')
        self.assertNotContains(response, 'Private Patient')
        self.assertNotContains(response, 'Private Treatment')

    def test_receptionist_calendar_includes_all_clinic_appointments(self):
        receptionist = User.objects.create_user(
            username='calendar-receptionist',
            password='calendar-pass-928!',
            role='receptionist',
        )
        patient = User.objects.create_user(
            username='calendar-receptionist-patient',
            password='calendar-pass-928!',
            role='patient',
        )
        Appointment.objects.create(
            patient=patient,
            date=timezone.localdate(),
            time=time(10, 0),
        )
        second_patient = User.objects.create_user(
            username='calendar-receptionist-patient-two',
            password='calendar-pass-928!',
            role='patient',
        )
        Appointment.objects.create(
            patient=second_patient,
            date=timezone.localdate(),
            time=time(11, 0),
        )
        self.client.force_login(receptionist)

        response = self.client.get(reverse('clinic:appointment_list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['appointments_data']), 2)
        self.assertEqual(response.context['busy_slots_data'], [])


class ReceptionistDashboardTests(TestCase):
    def setUp(self):
        self.receptionist = User.objects.create_user(
            username='front-desk',
            password='front-desk-pass-928!',
            role='receptionist',
        )
        self.patient = User.objects.create_user(
            username='front-desk-patient',
            password='patient-pass-928!',
            role='patient',
            first_name='Pending',
            last_name='Patient',
        )
        self.appointment = Appointment.objects.create(
            patient=self.patient,
            date=timezone.localdate(),
            time=time(9, 0),
        )
        self.client.force_login(self.receptionist)

    def test_dashboard_only_offers_confirmation_for_pending_appointments(self):
        confirmed_patient = User.objects.create_user(
            username='confirmed-patient',
            password='patient-pass-928!',
            role='patient',
            first_name='Confirmed',
            last_name='Patient',
        )
        Appointment.objects.create(
            patient=confirmed_patient,
            date=timezone.localdate(),
            time=time(10, 0),
            status='confirmed',
        )

        response = self.client.get(reverse('clinic:receptionist_dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_today'], 2)
        self.assertContains(
            response,
            'aria-label="Confirm appointment for Pending Patient"',
        )
        self.assertNotContains(
            response,
            'aria-label="Confirm appointment for Confirmed Patient"',
        )
        self.assertContains(response, 'method="post"')
        appointments = json.loads(response.context['appts_month_json'])
        self.assertEqual(len(appointments['labels']), 12)
        self.assertEqual(appointments['data'][-1], 2)
        self.assertContains(response, 'id="apptsChart"')

    def test_status_changes_require_post(self):
        status_url = reverse(
            'clinic:appointment_set_status',
            args=(self.appointment.pk, 'confirmed'),
        )

        response = self.client.get(status_url)
        self.assertEqual(response.status_code, 405)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, 'pending')

        response = self.client.post(status_url)

        self.assertRedirects(response, reverse('clinic:appointment_list'))
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, 'confirmed')
        event = ActivityLog.objects.get(
            action=ActivityLog.Action.APPOINTMENT_STATUS_CHANGED,
            target_id=str(self.appointment.pk),
        )
        self.assertEqual(event.actor, self.receptionist)
        self.assertIn('Confirmed', event.summary)


class PaymentRevenueTests(TestCase):
    def test_payment_without_date_updates_revenue(self):
        admin = User.objects.create_user(
            username='revenue-admin',
            password='revenue-pass-928!',
            role='admin',
        )
        patient = User.objects.create_user(
            username='revenue-patient',
            password='revenue-pass-928!',
            role='patient',
        )
        invoice = Invoice.objects.create(patient=patient, tax_percent=0)
        InvoiceItem.objects.create(
            invoice=invoice,
            description='Consultation',
            quantity=1,
            unit_price=Decimal('100.00'),
        )
        self.client.force_login(admin)

        response = self.client.post(
            reverse('clinic:invoice_add_payment', args=(invoice.pk,)),
            {'amount': '50.00', 'method': 'cash', 'reference': ''},
        )

        self.assertRedirects(
            response,
            reverse('clinic:invoice_detail', args=(invoice.pk,)),
        )
        payment = Payment.objects.get(invoice=invoice)
        self.assertEqual(payment.paid_on, timezone.localdate())

        dashboard = self.client.get(reverse('manage_panel:dashboard'))
        self.assertEqual(dashboard.context['kpis']['total_revenue'], Decimal('50.00'))
        revenue = json.loads(dashboard.context['revenue_json'])
        self.assertEqual(revenue['data'][0], 50.0)


class PatientDownpaymentTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='downpayment-admin',
            password='downpayment-pass-928!',
            role='admin',
        )
        self.patient = User.objects.create_user(
            username='downpayment-patient',
            password='downpayment-pass-928!',
            role='patient',
        )
        self.invoice = Invoice.objects.create(
            patient=self.patient,
            tax_percent=10,
        )
        InvoiceItem.objects.create(
            invoice=self.invoice,
            description='Dental service',
            quantity=1,
            unit_price=Decimal('100.00'),
        )

    def test_patient_submits_half_of_final_total_for_staff_verification(self):
        self.client.force_login(self.patient)
        invoice_page = self.client.get(
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )
        self.assertContains(invoice_page, '50% Downpayment')
        self.assertContains(invoice_page, 'Submit Manual Downpayment')
        self.assertNotContains(invoice_page, '<option value="xendit">')

        response = self.client.post(
            reverse('clinic:invoice_add_payment', args=(self.invoice.pk,)),
            {'method': 'cash', 'reference': 'Paid at front desk'},
        )

        self.assertRedirects(
            response,
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )
        payment = Payment.objects.get(invoice=self.invoice)
        self.assertEqual(payment.amount, Decimal('55.00'))
        self.assertEqual(payment.status, PaymentStatus.PENDING)
        self.assertEqual(payment.paid_on, timezone.localdate())
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.amount_paid, Decimal('0.00'))
        self.assertEqual(self.invoice.balance, Decimal('110.00'))
        self.assertEqual(self.invoice.status, InvoiceStatus.UNPAID)

        self.client.post(
            reverse('clinic:invoice_add_payment', args=(self.invoice.pk,)),
            {'method': 'cash', 'reference': 'Duplicate request'},
        )
        self.assertEqual(Payment.objects.filter(invoice=self.invoice).count(), 1)

        self.client.force_login(self.admin)
        dashboard_before = self.client.get(reverse('manage_panel:dashboard'))
        self.assertEqual(
            dashboard_before.context['kpis']['total_revenue'],
            Decimal('0'),
        )
        invoice_list = self.client.get(reverse('clinic:invoice_list'))
        self.assertContains(invoice_list, 'Payment to verify')
        invoice_page = self.client.get(
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )
        self.assertContains(invoice_page, 'Confirm payment')

        response = self.client.post(
            reverse('clinic:invoice_review_payment', args=(payment.pk,)),
            {'action': 'confirm'},
        )
        self.assertRedirects(
            response,
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )

        payment.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertEqual(payment.status, PaymentStatus.CONFIRMED)
        self.assertEqual(self.invoice.amount_paid, Decimal('55.00'))
        self.assertEqual(self.invoice.balance, Decimal('55.00'))
        self.assertEqual(self.invoice.status, InvoiceStatus.PARTIAL)
        dashboard_after = self.client.get(reverse('manage_panel:dashboard'))
        self.assertEqual(
            dashboard_after.context['kpis']['total_revenue'],
            Decimal('55.00'),
        )

    def test_confirmed_payment_reduces_required_downpayment(self):
        Payment.objects.create(
            invoice=self.invoice,
            amount=Decimal('20.00'),
            method='cash',
            paid_on=timezone.localdate(),
        )

        self.assertEqual(self.invoice.downpayment_due, Decimal('35.00'))

    def test_patient_manual_payment_methods_include_local_options_not_xendit(self):
        choices = dict(PatientDownpaymentForm().fields['method'].choices)

        self.assertIn(PaymentMethod.CASH, choices)
        self.assertIn(PaymentMethod.GCASH, choices)
        self.assertIn(PaymentMethod.MAYA, choices)
        self.assertIn(PaymentMethod.BANK_TRANSFER, choices)
        self.assertIn(PaymentMethod.CARD, choices)
        self.assertNotIn(PaymentMethod.XENDIT, choices)

    def test_staff_cannot_record_a_first_payment_below_downpayment(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse('clinic:invoice_add_payment', args=(self.invoice.pk,)),
            {'amount': '54.99', 'method': 'cash', 'reference': ''},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Payment.objects.filter(invoice=self.invoice).exists())
        self.assertContains(response, 'first payment must cover the required downpayment')

        response = self.client.post(
            reverse('clinic:invoice_add_payment', args=(self.invoice.pk,)),
            {'amount': '55.00', 'method': 'cash', 'reference': ''},
        )

        self.assertRedirects(
            response,
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )
        self.assertEqual(
            Payment.objects.get(invoice=self.invoice).amount,
            Decimal('55.00'),
        )

    def test_staff_can_record_smaller_partial_payments_after_downpayment(self):
        Payment.objects.create(
            invoice=self.invoice,
            amount=Decimal('55.00'),
            method=PaymentMethod.CASH,
            status=PaymentStatus.CONFIRMED,
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse('clinic:invoice_add_payment', args=(self.invoice.pk,)),
            {'amount': '10.00', 'method': 'cash', 'reference': 'Next visit'},
        )

        self.assertRedirects(
            response,
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.amount_paid, Decimal('65.00'))
        self.assertEqual(self.invoice.balance, Decimal('45.00'))

    @override_settings(XENDIT_CALLBACK_TOKEN='test-callback-token')
    def test_xendit_webhook_rejects_payment_below_first_downpayment(self):
        callback_url = reverse('clinic:xendit_webhook')
        payload = {
            'status': 'PAID',
            'external_id': f'{self.invoice.number}-checkout1',
            'amount': '40.00',
            'id': 'xendit-payment-1',
        }

        response = self.client.post(
            callback_url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_CALLBACK_TOKEN='test-callback-token',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Payment.objects.filter(invoice=self.invoice).exists())

    def test_patient_can_print_receipt_for_own_confirmed_payment(self):
        payment = Payment.objects.create(
            invoice=self.invoice,
            amount=Decimal('55.00'),
            method=PaymentMethod.GCASH,
            reference='GCASH-123',
            status=PaymentStatus.CONFIRMED,
        )
        self.client.force_login(self.patient)

        response = self.client.get(
            reverse('clinic:payment_receipt', args=(payment.pk,)),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'RCT-{payment.pk:06d}')
        self.assertContains(response, 'GCash')
        self.assertContains(response, 'GCASH-123')
        self.assertContains(response, 'Print Receipt')

    def test_patient_invoice_list_has_pay_action_for_outstanding_invoice(self):
        self.client.force_login(self.patient)

        response = self.client.get(reverse('clinic:invoice_list'))

        self.assertContains(response, '> Pay</a>')
        self.assertContains(
            response,
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )

    @override_settings(XENDIT_ENABLED=True)
    @patch('clinic.views.create_xendit_invoice')
    def test_patient_pays_downpayment_before_remaining_online_balance(
        self, create_checkout
    ):
        create_checkout.return_value = {
            'invoice_url': 'https://checkout.example.test/downpayment',
        }
        self.client.force_login(self.patient)
        checkout_url = reverse(
            'clinic:invoice_xendit_checkout',
            args=(self.invoice.pk,),
        )

        response = self.client.post(checkout_url)

        self.assertRedirects(
            response,
            'https://checkout.example.test/downpayment',
            fetch_redirect_response=False,
        )
        create_checkout.assert_called_once_with(
            self.invoice,
            Decimal('55.00'),
            f'Downpayment for {self.invoice.number}',
        )
        invoice_page = self.client.get(
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )
        self.assertContains(invoice_page, 'Pay 50% Downpayment Online')

        Payment.objects.create(
            invoice=self.invoice,
            amount=Decimal('55.00'),
            method='xendit',
            reference='confirmed-downpayment',
            status=PaymentStatus.CONFIRMED,
        )
        create_checkout.reset_mock()
        create_checkout.return_value = {
            'invoice_url': 'https://checkout.example.test/balance',
        }

        response = self.client.post(checkout_url)

        self.assertRedirects(
            response,
            'https://checkout.example.test/balance',
            fetch_redirect_response=False,
        )
        create_checkout.assert_called_once_with(
            self.invoice,
            Decimal('55.00'),
            f'Remaining balance for {self.invoice.number}',
        )
        invoice_page = self.client.get(
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )
        self.assertContains(invoice_page, 'Pay Remaining Balance Online')

    @override_settings(XENDIT_ENABLED=True)
    @patch('clinic.views.create_xendit_invoice')
    def test_pending_manual_payment_blocks_duplicate_online_checkout(
        self, create_checkout
    ):
        Payment.objects.create(
            invoice=self.invoice,
            amount=Decimal('55.00'),
            method='cash',
            status=PaymentStatus.PENDING,
        )
        self.client.force_login(self.patient)

        response = self.client.post(
            reverse('clinic:invoice_xendit_checkout', args=(self.invoice.pk,)),
        )

        self.assertRedirects(
            response,
            reverse('clinic:invoice_detail', args=(self.invoice.pk,)),
        )
        create_checkout.assert_not_called()

    def test_patient_cannot_submit_payment_for_another_patient_invoice(self):
        other_patient = User.objects.create_user(
            username='other-downpayment-patient',
            password='downpayment-pass-928!',
            role='patient',
        )
        other_invoice = Invoice.objects.create(patient=other_patient)
        self.client.force_login(self.patient)

        response = self.client.post(
            reverse('clinic:invoice_add_payment', args=(other_invoice.pk,)),
            {'method': 'cash', 'reference': ''},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Payment.objects.filter(invoice=other_invoice).exists())


class AppointmentIntegrationTests(TestCase):
    def setUp(self):
        self.patient = User.objects.create_user(
            username='integration-patient',
            password='integration-pass-928!',
            role='patient',
            email='patient@example.com',
            phone='+639171234567',
        )
        self.service = Service.objects.create(name='Cleaning')
        self.appointment = Appointment.objects.create(
            patient=self.patient,
            service=self.service,
            date=timezone.localdate(),
            time=time(9, 0),
        )

    @override_settings(
        APPOINTMENT_SMS_NOTIFICATIONS=False,
        APPOINTMENT_EMAIL_NOTIFICATIONS=True,
        DEFAULT_FROM_EMAIL='clinic@example.com',
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    )
    def test_appointment_email_is_sent_to_the_patient(self):
        sent = send_appointment_notifications(self.appointment)

        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['patient@example.com'])
        self.assertIn('pending confirmation', mail.outbox[0].body)

    @override_settings(
        APPOINTMENT_SMS_NOTIFICATIONS=False,
        APPOINTMENT_EMAIL_NOTIFICATIONS=False,
    )
    def test_notifications_are_not_reported_as_sent_when_disabled(self):
        self.assertFalse(send_appointment_notifications(self.appointment))
        self.assertEqual(mail.outbox, [])

    def test_new_patient_booking_is_queued_for_google_calendar_sync(self):
        dentist = User.objects.create_user(
            username='new-booking-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        self.client.force_login(self.patient)

        with patch('clinic.views._send_appointment_notice'):
            with patch('clinic.views._sync_appointment_to_calendar') as sync_calendar:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(
                        reverse('clinic:appointment_create'),
                        {
                            'dentist': dentist.pk,
                            'service': self.service.pk,
                            'date': (timezone.localdate() + timedelta(days=1)).isoformat(),
                            'time': '10:00',
                            'reason': 'Checkup',
                        },
                    )

        self.assertRedirects(response, reverse('clinic:appointment_list'))
        appointment = Appointment.objects.get(
            patient=self.patient,
            dentist=dentist,
            date=timezone.localdate() + timedelta(days=1),
        )
        self.assertEqual(appointment.status, AppointmentStatus.PENDING)
        sync_calendar.assert_called_once_with(appointment.pk)

    def test_patient_cannot_book_overlapping_dentist_appointment(self):
        dentist = User.objects.create_user(
            username='overlap-calendar-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        self.appointment.dentist = dentist
        self.appointment.save(update_fields=['dentist'])
        self.service.duration_minutes = 45
        self.service.save(update_fields=['duration_minutes'])
        self.client.force_login(self.patient)

        response = self.client.post(
            reverse('clinic:appointment_create'),
            {
                'dentist': dentist.pk,
                'service': self.service.pk,
                'date': self.appointment.date.isoformat(),
                'time': '09:30',
                'reason': 'Another cleaning',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('time', response.context['form'].errors)
        self.assertIn('unavailable', response.context['form'].errors['time'][0].lower())
        self.assertEqual(
            Appointment.objects.filter(dentist=dentist, date=self.appointment.date).count(),
            1,
        )

    def test_cancelling_pending_booking_removes_its_google_calendar_event(self):
        dentist = User.objects.create_user(
            username='cancel-calendar-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        self.appointment.dentist = dentist
        self.appointment.google_calendar_event_id = 'google-pending-appointment'
        self.appointment.save(update_fields=['dentist', 'google_calendar_event_id'])
        self.client.force_login(self.patient)

        with patch('clinic.views._remove_calendar_appointment') as remove_event:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.get(
                    reverse('clinic:appointment_cancel', args=(self.appointment.pk,))
                )

        self.assertRedirects(response, reverse('clinic:appointment_list'))
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, AppointmentStatus.CANCELLED)
        remove_event.assert_called_once_with(self.appointment.pk)

    def test_calendar_credentials_are_encrypted_before_storage(self):
        key = Fernet.generate_key().decode('ascii')
        credentials = '{"token": "private-token", "refresh_token": "private-refresh"}'

        with self.settings(GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY=key):
            encrypted = encrypt_calendar_credentials(credentials)
            self.assertNotIn('private-token', encrypted)
            self.assertEqual(decrypt_calendar_credentials(encrypted), credentials)

    @patch('clinic.integrations._google_calendar_service')
    def test_pending_appointment_is_inserted_into_connected_dentists_calendar(
        self, calendar_service
    ):
        dentist = User.objects.create_user(
            username='pending-calendar-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        self.appointment.dentist = dentist
        self.appointment.save(update_fields=['dentist'])
        event_operations = calendar_service.return_value.events.return_value
        event_operations.insert.return_value.execute.return_value = {
            'id': 'google-pending-appointment',
        }

        synced = sync_appointment_to_google_calendar(self.appointment)

        self.assertTrue(synced)
        event_operations.insert.assert_called_once()
        self.assertEqual(
            event_operations.insert.call_args.kwargs['calendarId'],
            'primary',
        )
        self.assertEqual(
            event_operations.insert.call_args.kwargs['body']['summary'],
            'Cleaning - integration-patient',
        )
        self.appointment.refresh_from_db()
        self.assertEqual(
            self.appointment.google_calendar_event_id,
            'google-pending-appointment',
        )

    @patch('clinic.integrations._clinic_google_calendar_service')
    def test_appointment_is_inserted_into_shared_clinic_calendar(
        self, calendar_service
    ):
        event_operations = calendar_service.return_value.events.return_value
        event_operations.insert.return_value.execute.return_value = {
            'id': 'shared-clinic-appointment',
        }

        synced = sync_appointment_to_clinic_google_calendar(self.appointment)

        self.assertTrue(synced)
        event_operations.insert.assert_called_once()
        self.assertEqual(
            event_operations.insert.call_args.kwargs['calendarId'],
            'primary',
        )
        event = event_operations.insert.call_args.kwargs['body']
        self.assertEqual(event['summary'], 'Cleaning - integration-patient')
        self.assertEqual(event['description'], 'AimSmile appointment')
        self.assertEqual(
            event['start']['dateTime'],
            f'{self.appointment.date.isoformat()}T'
            f'{self.appointment.time.strftime("%H:%M:%S")}+08:00',
        )
        expected_end = datetime.combine(
            self.appointment.date,
            self.appointment.time,
        ) + timedelta(minutes=self.service.duration_minutes)
        self.assertEqual(
            event['end']['dateTime'],
            f'{expected_end.date().isoformat()}T'
            f'{expected_end.time().strftime("%H:%M:%S")}+08:00',
        )
        self.appointment.refresh_from_db()
        self.assertEqual(
            self.appointment.clinic_google_calendar_event_id,
            'shared-clinic-appointment',
        )

    def test_booking_sync_updates_dentist_and_shared_clinic_calendars(self):
        dentist = User.objects.create_user(
            username='both-calendars-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        self.appointment.dentist = dentist
        self.appointment.save(update_fields=['dentist'])
        with patch(
            'clinic.integrations._google_calendar_service'
        ) as dentist_service, patch(
            'clinic.integrations._clinic_google_calendar_service'
        ) as clinic_service:
            dentist_service.return_value.events.return_value.insert.return_value.execute.return_value = {
                'id': 'dentist-appointment-event',
            }
            clinic_service.return_value.events.return_value.insert.return_value.execute.return_value = {
                'id': 'clinic-appointment-event',
            }
            synced = sync_appointment_to_google_calendar(self.appointment)

        self.assertTrue(synced)
        self.appointment.refresh_from_db()
        self.assertEqual(
            self.appointment.google_calendar_event_id,
            'dentist-appointment-event',
        )
        self.assertEqual(
            self.appointment.clinic_google_calendar_event_id,
            'clinic-appointment-event',
        )

    def test_cancellation_removes_events_from_both_connected_calendars(self):
        dentist = User.objects.create_user(
            username='remove-calendar-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        self.appointment.dentist = dentist
        self.appointment.google_calendar_event_id = 'dentist-event'
        self.appointment.clinic_google_calendar_event_id = 'clinic-event'
        self.appointment.save(update_fields=[
            'dentist',
            'google_calendar_event_id',
            'clinic_google_calendar_event_id',
        ])
        with patch(
            'clinic.integrations._google_calendar_service'
        ) as dentist_service, patch(
            'clinic.integrations._clinic_google_calendar_service'
        ) as clinic_service:
            removed = remove_appointment_from_google_calendar(self.appointment)

        self.assertTrue(removed)
        dentist_service.return_value.events.return_value.delete.assert_called_once_with(
            calendarId='primary',
            eventId='dentist-event',
        )
        clinic_service.return_value.events.return_value.delete.assert_called_once_with(
            calendarId='primary',
            eventId='clinic-event',
        )
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.google_calendar_event_id, '')
        self.assertEqual(self.appointment.clinic_google_calendar_event_id, '')

    @patch('clinic.integrations._google_calendar_service')
    def test_google_calendar_embed_url_uses_primary_calendar_and_manila_timezone(
        self, calendar_service
    ):
        dentist = User.objects.create_user(
            username='google-events-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        calendar_service.return_value.calendarList.return_value.get.return_value.execute.return_value = {
            'id': 'dentist@example.com',
        }

        embed_url = get_google_calendar_embed_url(dentist)

        self.assertEqual(
            parse_qs(embed_url.partition('?')[2]),
            {'src': ['dentist@example.com'], 'ctz': ['Asia/Manila'], 'mode': ['WEEK']},
        )
        calendar_service.return_value.calendarList.return_value.get.assert_called_once_with(
            calendarId='primary',
        )

    @patch('clinic.integrations._clinic_google_calendar_service')
    def test_shared_clinic_embed_url_uses_primary_calendar(self, calendar_service):
        calendar_service.return_value.calendarList.return_value.get.return_value.execute.return_value = {
            'id': 'clinic-calendar@example.com',
        }

        embed_url = get_clinic_google_calendar_embed_url()

        self.assertEqual(
            parse_qs(embed_url.partition('?')[2]),
            {
                'src': ['clinic-calendar@example.com'],
                'ctz': ['Asia/Manila'],
                'mode': ['WEEK'],
            },
        )

    @patch('clinic.views.get_google_calendar_embed_url')
    def test_appointment_page_embeds_connected_google_calendar(self, get_embed_url):
        dentist = User.objects.create_user(
            username='google-calendar-page-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        profile = dentist.doctor_profile
        profile.google_calendar_credentials = 'encrypted-calendar-token'
        profile.save(update_fields=['google_calendar_credentials'])
        get_embed_url.return_value = (
            'https://calendar.google.com/calendar/embed?src=dentist%40example.com'
        )
        self.client.force_login(dentist)

        response = self.client.get(reverse('clinic:appointment_list'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['google_calendar_connected'])
        self.assertEqual(
            response.context['google_calendar_embed_url'],
            get_embed_url.return_value,
        )
        self.assertContains(response, 'Open Google Calendar')
        self.assertContains(response, 'title="Google Calendar"')
        self.assertNotContains(response, 'id="appointmentCalendar"')

    @patch('clinic.views.get_clinic_google_calendar_embed_url')
    def test_admin_appointment_page_embeds_shared_clinic_calendar(self, get_embed_url):
        admin = User.objects.create_user(
            username='shared-calendar-admin',
            password='calendar-pass-928!',
            role='admin',
        )
        get_embed_url.return_value = (
            'https://calendar.google.com/calendar/embed?src=clinic%40example.com'
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('clinic:appointment_list'))

        self.assertContains(response, 'Connect Clinic Calendar')
        ClinicGoogleCalendarConnection.objects.create(
            pk=1,
            google_calendar_credentials='encrypted-clinic-token',
            connected_by=admin,
        )

        response = self.client.get(reverse('clinic:appointment_list'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['clinic_google_calendar_connected'])
        self.assertEqual(
            response.context['google_calendar_embed_url'],
            get_embed_url.return_value,
        )
        self.assertContains(response, 'Shared Clinic Google Calendar')
        self.assertContains(response, 'Open Clinic Google Calendar')
        self.assertContains(response, 'title="Shared Clinic Google Calendar"')
        self.assertNotContains(response, 'id="appointmentCalendar"')

    @patch(
        'clinic.views.get_google_calendar_embed_url',
        side_effect=ClinicIntegrationError('Calendar access is missing.'),
    )
    def test_appointment_page_falls_back_to_schedule_when_calendar_needs_reconnect(
        self, get_embed_url
    ):
        dentist = User.objects.create_user(
            username='google-calendar-reconnect-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        dentist.doctor_profile.google_calendar_credentials = 'encrypted-calendar-token'
        dentist.doctor_profile.save(update_fields=['google_calendar_credentials'])
        self.client.force_login(dentist)

        response = self.client.get(reverse('clinic:appointment_list'), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['google_calendar_embed_url'], '')
        self.assertContains(response, 'id="appointmentCalendar"')
        self.assertContains(response, 'Reconnect Google Calendar')
        self.assertContains(
            response,
            reverse('clinic:google_calendar_connect'),
        )
        get_embed_url.assert_called_once_with(dentist)

    def test_insecure_oauth_transport_is_allowed_only_for_local_debug(self):
        with patch.dict('os.environ', {'OAUTHLIB_INSECURE_TRANSPORT': '0'}):
            with self.settings(
                DEBUG=True,
                GOOGLE_CALENDAR_REDIRECT_URI=(
                    'http://127.0.0.1:8000/app/google-calendar/callback/'
                ),
            ):
                _configure_google_oauth_transport()
                self.assertEqual(
                    os.environ['OAUTHLIB_INSECURE_TRANSPORT'],
                    '1',
                )

        with patch.dict('os.environ', {'OAUTHLIB_INSECURE_TRANSPORT': '1'}):
            with self.settings(
                DEBUG=False,
                GOOGLE_CALENDAR_REDIRECT_URI=(
                    'https://clinic.example.com/app/google-calendar/callback/'
                ),
            ):
                _configure_google_oauth_transport()
                self.assertNotIn(
                    'OAUTHLIB_INSECURE_TRANSPORT',
                    os.environ,
                )

    @override_settings(
        SEMAPHORE_API_KEY='test-semaphore-key',
        SEMAPHORE_SENDER_NAME='AimSmile',
    )
    @patch('clinic.integrations.urlopen')
    def test_semaphore_receives_normalized_philippine_number(self, urlopen_mock):
        response = urlopen_mock.return_value.__enter__.return_value
        response.read.return_value = b'[{"message_id":"test-message"}]'

        send_semaphore_sms('+63 917 123 4567', 'Appointment reminder')

        request = urlopen_mock.call_args.args[0]
        payload = parse_qs(request.data.decode('utf-8'))
        self.assertEqual(payload['number'], ['639171234567'])
        self.assertEqual(payload['sendername'], ['AimSmile'])

    @override_settings(
        GOOGLE_CALENDAR_CLIENT_ID='test-client-id',
        GOOGLE_CALENDAR_CLIENT_SECRET='test-client-secret',
        GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode('ascii'),
    )
    def test_dentist_can_start_calendar_oauth_flow(self):
        dentist = User.objects.create_user(
            username='calendar-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        self.client.force_login(dentist)

        response = self.client.get(reverse('clinic:google_calendar_connect'))

        self.assertEqual(response.status_code, 302)
        self.assertIn('accounts.google.com', response['Location'])
        self.assertTrue(self.client.session.get('google_calendar_oauth_state'))
        self.assertTrue(
            self.client.session.get('google_calendar_oauth_code_verifier')
        )

    @override_settings(
        GOOGLE_CALENDAR_CLIENT_ID='test-client-id',
        GOOGLE_CALENDAR_CLIENT_SECRET='test-client-secret',
        GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode('ascii'),
    )
    def test_calendar_callback_reuses_pkce_verifier_from_session(self):
        from google_auth_oauthlib.flow import Flow

        dentist = User.objects.create_user(
            username='calendar-callback-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        self.client.force_login(dentist)
        session = self.client.session
        session['google_calendar_oauth_state'] = 'expected-state'
        session['google_calendar_oauth_code_verifier'] = 'original-pkce-verifier'
        session.save()

        with patch.object(Flow, 'fetch_token', autospec=True) as fetch_token:
            with patch.object(
                Flow,
                'credentials',
                new_callable=PropertyMock,
            ) as credentials:
                credentials.return_value.to_json.return_value = '{"token": "test"}'
                response = self.client.get(
                    reverse('clinic:google_calendar_callback'),
                    {
                        'state': 'expected-state',
                        'code': 'one-time-code',
                    },
                )

        self.assertEqual(response.status_code, 302)
        flow_used = fetch_token.call_args.args[0]
        self.assertEqual(flow_used.code_verifier, 'original-pkce-verifier')
        self.assertEqual(
            fetch_token.call_args.kwargs['authorization_response'].split('?')[0],
            'http://testserver/app/google-calendar/callback/',
        )
        dentist.refresh_from_db()
        self.assertTrue(dentist.doctor_profile.google_calendar_credentials)

    @override_settings(
        GOOGLE_CALENDAR_CLIENT_ID='test-client-id',
        GOOGLE_CALENDAR_CLIENT_SECRET='test-client-secret',
        GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode('ascii'),
    )
    @patch('clinic.views._sync_existing_appointments_to_clinic_calendar')
    def test_admin_calendar_callback_saves_shared_credentials_and_syncs_existing(
        self, sync_existing
    ):
        from google_auth_oauthlib.flow import Flow

        admin = User.objects.create_user(
            username='shared-calendar-oauth-admin',
            password='calendar-pass-928!',
            role='admin',
        )
        self.client.force_login(admin)
        session = self.client.session
        session['google_calendar_oauth_state'] = 'expected-admin-state'
        session['google_calendar_oauth_code_verifier'] = 'admin-pkce-verifier'
        session.save()

        with patch.object(Flow, 'fetch_token', autospec=True):
            with patch.object(
                Flow,
                'credentials',
                new_callable=PropertyMock,
            ) as credentials:
                credentials.return_value.to_json.return_value = '{"token": "clinic"}'
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.get(
                        reverse('clinic:google_calendar_callback'),
                        {
                            'state': 'expected-admin-state',
                            'code': 'admin-one-time-code',
                        },
                    )

        self.assertEqual(response.status_code, 302)
        connection = ClinicGoogleCalendarConnection.objects.get(pk=1)
        self.assertEqual(connection.connected_by, admin)
        self.assertEqual(
            decrypt_calendar_credentials(connection.google_calendar_credentials),
            '{"token": "clinic"}',
        )
        sync_existing.assert_called_once_with()

    @override_settings(
        GOOGLE_CALENDAR_CLIENT_ID='',
        GOOGLE_CALENDAR_CLIENT_SECRET='configured-secret',
        GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode('ascii'),
    )
    def test_calendar_setup_error_names_missing_setting_without_exposing_values(self):
        dentist = User.objects.create_user(
            username='calendar-unconfigured-dentist',
            password='calendar-pass-928!',
            role='dentist',
        )
        self.client.force_login(dentist)

        response = self.client.get(
            reverse('clinic:google_calendar_connect'),
            follow=True,
        )

        self.assertContains(
            response,
            'Google Calendar setup is missing: GOOGLE_CALENDAR_CLIENT_ID.',
        )
        self.assertNotContains(response, 'configured-secret')

    @override_settings(
        APPOINTMENT_SMS_NOTIFICATIONS=False,
        APPOINTMENT_EMAIL_NOTIFICATIONS=True,
    )
    @patch('clinic.management.commands.send_appointment_reminders.send_appointment_notifications')
    def test_reminder_command_sends_once_for_confirmed_appointment_in_window(
        self, send_notifications
    ):
        send_notifications.return_value = True
        future = timezone.localtime(timezone.now() + timedelta(hours=24))
        appointment = Appointment.objects.create(
            patient=self.patient,
            service=self.service,
            date=future.date(),
            time=future.time().replace(tzinfo=None, second=0, microsecond=0),
            status=AppointmentStatus.CONFIRMED,
        )

        call_command('send_appointment_reminders', stdout=StringIO())

        appointment.refresh_from_db()
        self.assertIsNotNone(appointment.reminder_sent_at)
        send_notifications.assert_called_once_with(appointment, reminder=True)
