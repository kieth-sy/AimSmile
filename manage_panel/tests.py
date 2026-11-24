from datetime import date, time

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import ActivityLog, IdentityVerification, Role
from clinic.models import Appointment, Service, TreatmentRecord

User = get_user_model()


class AdminAccountCreationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='clinic-admin',
            password='admin-pass-928!',
            role='admin',
        )
        self.client.force_login(self.admin)

    def account_payload(self, username='new-patient'):
        return {
            'username': username,
            'first_name': 'New',
            'last_name': 'Patient',
            'email': f'{username}@example.com',
            'role': 'patient',
            'password': 'Strong-Clinic-Password-928!',
            'is_active': 'on',
        }

    def test_creation_sets_password_without_sending_email(self):
        response = self.client.post(
            reverse('manage_panel:user_create'),
            self.account_payload(),
        )

        self.assertRedirects(response, reverse('manage_panel:user_list'))
        user = User.objects.get(username='new-patient')
        self.assertTrue(user.check_password('Strong-Clinic-Password-928!'))
        self.assertEqual(mail.outbox, [])
        event = ActivityLog.objects.get(
            action=ActivityLog.Action.USER_CREATED,
            target_id=str(user.pk),
        )
        self.assertEqual(event.actor, self.admin)
        self.assertNotIn('Strong-Clinic-Password-928!', event.summary)

    def test_activity_log_page_is_available_to_admins(self):
        response = self.client.get(reverse('manage_panel:activity_log_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Activity Logs')

        filtered_response = self.client.get(
            reverse('manage_panel:activity_log_list'),
            {'q': 'not-an-ip', 'action': ActivityLog.Action.LOGIN_SUCCESS},
        )
        self.assertEqual(filtered_response.status_code, 200)

    def test_activity_log_page_is_forbidden_to_non_admins(self):
        patient = User.objects.create_user(
            username='audit-patient',
            password='patient-pass-928!',
            role='patient',
        )
        self.client.force_login(patient)

        response = self.client.get(reverse('manage_panel:activity_log_list'))

        self.assertEqual(response.status_code, 403)

    def test_account_creation_requires_an_email_address(self):
        payload = self.account_payload(username='no-email-user')
        payload['email'] = ''

        response = self.client.post(reverse('manage_panel:user_create'), payload)

        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)
        self.assertFalse(User.objects.filter(username='no-email-user').exists())
        self.assertEqual(mail.outbox, [])

    def test_account_creation_requires_a_valid_initial_password(self):
        payload = self.account_payload(username='weak-password-user')
        payload['password'] = 'short'

        response = self.client.post(
            reverse('manage_panel:user_create'),
            payload,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('password', response.context['form'].errors)
        self.assertFalse(User.objects.filter(username='weak-password-user').exists())
        self.assertEqual(mail.outbox, [])


class AdminKycReviewTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user(
            username='kyc-admin',
            password='admin-pass-928!',
            role=Role.ADMIN,
        )
        self.client.force_login(admin)

    def test_pending_accounts_show_identity_check_completion(self):
        verified_patient = User.objects.create_user(
            username='kyc-complete',
            password='patient-pass-928!',
            role=Role.PATIENT,
            signup_status='pending',
            is_active=False,
        )
        IdentityVerification.objects.create(
            user=verified_patient,
            id_type='passport',
            consented_at=timezone.now(),
            duplicate_check_consented_at=timezone.now(),
            id_number_digest='a' * 64,
            face_template=b'encrypted-template',
        )
        User.objects.create_user(
            username='kyc-incomplete',
            password='patient-pass-928!',
            role=Role.PATIENT,
            signup_status='pending',
            is_active=False,
        )

        response = self.client.get(reverse('manage_panel:user_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ID and face check complete')
        self.assertContains(response, 'Identity check missing')


class ReportsChartTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user(
            username='reports-admin',
            password='admin-pass-928!',
            role='admin',
        )
        self.client.force_login(admin)

    def test_reports_provide_top_service_pie_chart_data(self):
        patient = User.objects.create_user(
            username='reports-patient',
            password='patient-pass-928!',
            role='patient',
        )
        cleaning = Service.objects.create(name='Cleaning')
        filling = Service.objects.create(name='Filling')
        TreatmentRecord.objects.create(patient=patient, service=cleaning)
        TreatmentRecord.objects.create(patient=patient, service=cleaning)
        TreatmentRecord.objects.create(patient=patient, service=filling)

        response = self.client.get(reverse('manage_panel:reports'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['top_services_data'],
            {'labels': ['Cleaning', 'Filling'], 'data': [2, 1]},
        )
        self.assertEqual(response.context['top_services_basis'], 'recorded treatments')
        self.assertContains(response, 'topServicesChart')
        self.assertContains(response, 'treatChart')

    def test_top_services_use_appointments_when_no_treatments_exist(self):
        patient = User.objects.create_user(
            username='reports-patient',
            password='patient-pass-928!',
            role='patient',
        )
        cleaning = Service.objects.create(name='Cleaning')
        filling = Service.objects.create(name='Filling')
        Appointment.objects.create(
            patient=patient, service=cleaning, date=date(2026, 11, 15),
            time=time(10, 0),
        )
        Appointment.objects.create(
            patient=patient, service=cleaning, date=date(2026, 11, 16),
            time=time(11, 0),
        )
        Appointment.objects.create(
            patient=patient, service=filling, date=date(2026, 11, 17),
            time=time(12, 0),
        )

        response = self.client.get(reverse('manage_panel:reports'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['top_services_data'],
            {'labels': ['Cleaning', 'Filling'], 'data': [2, 1]},
        )
        self.assertEqual(response.context['top_services_basis'], 'appointment bookings')
        self.assertContains(response, 'topServicesChart')
        self.assertNotContains(response, 'treatChart')
        self.assertContains(response, 'No treatments recorded yet.')
