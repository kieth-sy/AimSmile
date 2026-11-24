import io
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile

from .identity_verification import (
    detect_id_type,
    decrypt_face_template,
    encrypt_face_template,
    identity_id_number_digest,
    parse_id_text,
    scan_id_image,
)
from .models import ActivityLog, IdentityVerification, Role, SignupDuplicateReport
from .views import _identity_form_initial


TEST_ENCRYPTION_KEY = Fernet.generate_key().decode('ascii')


def image_upload(name):
    image = Image.new('RGB', (640, 480), 'white')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')


class LoginAutofillTests(TestCase):
    def test_login_form_disables_browser_autofill(self):
        response = self.client.get(reverse('accounts:login'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<form method="post" autocomplete="off">')
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, 'autocomplete="off"')
        self.assertContains(response, 'autocomplete="new-password"')
        self.assertNotContains(response, 'autofocus')


@override_settings(
    RECAPTCHA_ENABLED=False,
    SMS_LOGIN_ENABLED=False,
)
class AuthenticationActivityLogTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='audit-patient',
            password='Audit-Password-928!',
            role=Role.PATIENT,
        )

    def test_successful_login_and_logout_are_recorded(self):
        response = self.client.post(
            reverse('accounts:login'),
            {'username': self.user.username, 'password': 'Audit-Password-928!'},
        )

        self.assertEqual(response.status_code, 302)
        login_event = ActivityLog.objects.get(
            action=ActivityLog.Action.LOGIN_SUCCESS,
        )
        self.assertEqual(login_event.actor, self.user)
        self.assertEqual(login_event.actor_username, self.user.username)
        self.assertEqual(login_event.ip_address, '127.0.0.1')

        self.client.post(reverse('accounts:logout'))
        self.assertTrue(
            ActivityLog.objects.filter(
                action=ActivityLog.Action.LOGOUT,
                actor=self.user,
            ).exists()
        )

    def test_failed_login_is_recorded_without_credentials(self):
        password = 'Never-Store-This-Password!'
        response = self.client.post(
            reverse('accounts:login'),
            {'username': self.user.username, 'password': password},
        )

        self.assertEqual(response.status_code, 200)
        event = ActivityLog.objects.get(action=ActivityLog.Action.LOGIN_FAILED)
        self.assertIsNone(event.actor)
        self.assertNotIn(password, event.summary)

    def test_failed_two_factor_attempt_is_recorded_without_code(self):
        self.user.totp_enabled = True
        self.user.totp_secret = 'encrypted-secret'
        self.user.save(update_fields=['totp_enabled', 'totp_secret'])
        session = self.client.session
        session['pending_2fa_user_id'] = self.user.pk
        session['pending_2fa_attempts'] = 0
        session.save()

        with patch('accounts.views.verify_totp', return_value=False):
            response = self.client.post(
                reverse('accounts:two_factor_login'),
                {'code': '123456'},
            )

        self.assertEqual(response.status_code, 200)
        event = ActivityLog.objects.get(action=ActivityLog.Action.LOGIN_FAILED)
        self.assertEqual(event.target_id, str(self.user.pk))
        self.assertNotIn('123456', event.summary)


@override_settings(
    IDENTITY_VERIFICATION_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
    RECAPTCHA_ENABLED=False,
)
class IdentitySignupTests(TestCase):
    def test_scan_detects_id_type_without_manual_selection(self):
        extracted = {
            'full_name': 'Ariana Test',
            'birth_date': '2000-01-12',
            'address': 'Test Street, Manila',
            'id_number': 'P12345678',
            'expiration_date': '',
            'missing_fields': [],
            'id_type': 'passport',
        }
        with patch(
            'accounts.views.scan_id_image',
            return_value=extracted,
        ) as scanner:
            response = self.client.post(
                reverse('accounts:identity_scan_id'),
                {
                    'id_type': '',
                    'biometric_consent': 'true',
                    'duplicate_check_consent': 'true',
                    'id_image': image_upload('id.png'),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['id_type'], 'passport')
        self.assertEqual(response.json()['id_type_label'], 'Passport')
        self.assertNotIn('fields', response.json())
        self.assertNotIn('P12345678', response.content.decode())
        self.assertEqual(scanner.call_args.args[1], '')

    def test_registration_hides_personal_form_until_id_and_face_pass(self):
        response = self.client.get(reverse('accounts:signup'))
        self.assertContains(response, 'Philippine National ID')
        self.assertContains(response, '5-Step Guide')
        self.assertContains(response, 'Front View')
        self.assertContains(response, 'Turn Left')
        self.assertContains(response, 'Turn Right')
        self.assertContains(response, '30 cm')
        self.assertContains(response, '50–80 cm')
        self.assertContains(response, 'camera does not measure distance')
        self.assertContains(response, 'id="cameraGuide"')
        self.assertContains(response, 'id="cameraGuideFrame"')
        self.assertContains(response, 'id="cameraGuideDistance"')
        self.assertNotContains(response, 'name="username"')

        extracted = {
            'full_name': 'Ariana Test',
            'birth_date': '2000-01-12',
            'address': 'Test Street, Manila',
            'id_number': 'P12345678',
            'expiration_date': '2030-01-12',
            'missing_fields': [],
        }
        with patch('accounts.views.scan_id_image', return_value=extracted):
            scan_response = self.client.post(
                reverse('accounts:identity_scan_id'),
                {
                    'id_type': 'passport',
                    'biometric_consent': 'true',
                    'duplicate_check_consent': 'true',
                    'id_image': image_upload('id.png'),
                },
            )
        self.assertEqual(scan_response.status_code, 200)
        scan_data = scan_response.json()
        self.assertTrue(scan_data['ok'])
        self.assertNotIn('fields', scan_data)
        self.assertNotIn('P12345678', self.client.session['identity_scan_state'])

        response = self.client.get(reverse('accounts:signup'))
        self.assertNotContains(response, 'name="username"')

        face_uploads = {
            'scan_token': scan_data['scan_token'],
            'id_image': image_upload('id-again.png'),
            'selfie_front': image_upload('front.png'),
            'selfie_left': image_upload('left.png'),
            'selfie_right': image_upload('right.png'),
            'selfie_closer': image_upload('closer.png'),
            'selfie_farther': image_upload('farther.png'),
        }
        with patch(
            'accounts.views.compare_face_to_id_and_templates',
            return_value=(True, 0.8, b'\x01' * 512, None, -1.0),
        ):
            match_response = self.client.post(
                reverse('accounts:identity_match_face'),
                face_uploads,
            )
        self.assertEqual(match_response.status_code, 200)
        self.assertTrue(match_response.json()['matched'])

        response = self.client.get(reverse('accounts:signup'))
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'Ariana')
        self.assertContains(response, 'P12345678')

        response = self.client.post(
            reverse('accounts:signup'),
            {
                'identity_proof': match_response.json()['proof_token'],
                'duplicate_check_consent': 'true',
                'username': 'ariana-test',
                'first_name': 'Ariana',
                'last_name': 'Test',
                'email': 'ariana@example.com',
                'date_of_birth': '2000-01-12',
                'address': 'Test Street, Manila',
                'phone': '9171234567',
                'password1': 'SafePassword!123',
                'password2': 'SafePassword!123',
            },
        )
        self.assertRedirects(response, reverse('accounts:login'))
        user = get_user_model().objects.get(username='ariana-test')
        self.assertEqual(user.date_of_birth.isoformat(), '2000-01-12')
        self.assertEqual(user.address, 'Test Street, Manila')
        verification = IdentityVerification.objects.get(
            user=user,
            id_type='passport',
        )
        self.assertEqual(
            verification.id_number_digest,
            identity_id_number_digest('passport', 'P12345678'),
        )
        self.assertEqual(
            decrypt_face_template(verification.face_template),
            b'\x01' * 512,
        )
        self.assertFalse(hasattr(user, 'id_number'))

    def test_final_registration_cannot_skip_verification(self):
        response = self.client.post(
            reverse('accounts:signup'),
            {'username': 'unverified'},
        )
        self.assertRedirects(response, reverse('accounts:signup'))
        self.assertFalse(get_user_model().objects.filter(username='unverified').exists())

    def test_scan_requires_biometric_consent(self):
        with patch('accounts.views.scan_id_image') as scanner:
            response = self.client.post(
                reverse('accounts:identity_scan_id'),
                {
                    'id_type': 'passport',
                    'id_image': image_upload('id.png'),
                },
            )
        self.assertEqual(response.status_code, 400)
        scanner.assert_not_called()

    def test_scan_requires_duplicate_check_consent(self):
        with patch('accounts.views.scan_id_image') as scanner:
            response = self.client.post(
                reverse('accounts:identity_scan_id'),
                {
                    'id_type': 'passport',
                    'biometric_consent': 'true',
                    'id_image': image_upload('id.png'),
                },
            )
        self.assertEqual(response.status_code, 400)
        scanner.assert_not_called()

    def test_incomplete_scan_reports_missing_fields_without_returning_pii(self):
        extracted = {
            'full_name': 'Synthetic Patient',
            'birth_date': '2000-01-12',
            'address': 'Synthetic Street',
            'id_number': '1234-5678-9012-3456',
            'expiration_date': '',
            'missing_fields': ['Date of birth'],
        }
        with patch('accounts.views.scan_id_image', return_value=extracted):
            response = self.client.post(
                reverse('accounts:identity_scan_id'),
                {
                    'id_type': 'ph_national_id',
                    'biometric_consent': 'true',
                    'duplicate_check_consent': 'true',
                    'id_image': image_upload('synthetic-id.png'),
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['missing_fields'], ['Date of birth'])
        self.assertNotIn('fields', response.json())
        self.assertNotIn('Synthetic Patient', response.content.decode())
        self.assertNotIn('1234-5678-9012-3456', response.content.decode())

    def test_face_mismatch_does_not_unlock_personal_information(self):
        extracted = {
            'full_name': 'Ariana Test',
            'birth_date': '2000-01-12',
            'address': '',
            'id_number': 'P12345678',
            'expiration_date': '',
            'missing_fields': [],
        }
        with patch('accounts.views.scan_id_image', return_value=extracted):
            scan_response = self.client.post(
                reverse('accounts:identity_scan_id'),
                {
                    'id_type': 'passport',
                    'biometric_consent': 'true',
                    'duplicate_check_consent': 'true',
                    'id_image': image_upload('id.png'),
                },
            )
        uploads = {
            'scan_token': scan_response.json()['scan_token'],
            'id_image': image_upload('id-again.png'),
            'selfie_front': image_upload('front.png'),
            'selfie_left': image_upload('left.png'),
            'selfie_right': image_upload('right.png'),
            'selfie_closer': image_upload('closer.png'),
            'selfie_farther': image_upload('farther.png'),
        }
        with patch(
            'accounts.views.compare_face_to_id_and_templates',
            return_value=(False, 0.21, b'\x01' * 512, None, -1.0),
        ):
            response = self.client.post(
                reverse('accounts:identity_match_face'),
                uploads,
            )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()['matched'])
        self.assertNotIn('identity_proof_token', self.client.session)
        response = self.client.get(reverse('accounts:signup'))
        self.assertNotContains(response, 'name="username"')

    def test_existing_id_fingerprint_blocks_duplicate_signup(self):
        user_model = get_user_model()
        existing_patient = user_model.objects.create_user(
            username='existing-patient',
            password='SafePassword!123',
            role=Role.PATIENT,
        )
        IdentityVerification.objects.create(
            user=existing_patient,
            id_type='passport',
            consented_at=timezone.now(),
            id_number_digest=identity_id_number_digest(
                'passport',
                'P12345678',
            ),
        )
        extracted = {
            'full_name': 'Ariana Test',
            'birth_date': '2000-01-12',
            'address': '',
            'id_number': 'P-1234 5678',
            'expiration_date': '',
            'missing_fields': [],
        }
        with patch('accounts.views.scan_id_image', return_value=extracted):
            response = self.client.post(
                reverse('accounts:identity_scan_id'),
                {
                    'id_type': 'passport',
                    'biometric_consent': 'true',
                    'duplicate_check_consent': 'true',
                    'id_image': image_upload('duplicate-id.png'),
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()['duplicate_id'])

    def test_face_only_match_creates_review_report(self):
        user_model = get_user_model()
        existing_patient = user_model.objects.create_user(
            username='existing-face',
            password='SafePassword!123',
            role=Role.PATIENT,
            first_name='Existing',
            last_name='Patient',
            email='existing@example.com',
        )
        IdentityVerification.objects.create(
            user=existing_patient,
            id_type='passport',
            consented_at=timezone.now(),
            duplicate_check_consented_at=timezone.now(),
            id_number_digest=identity_id_number_digest('passport', 'OLD123'),
            face_template=encrypt_face_template(b'\x02' * 512),
        )
        extracted = {
            'full_name': 'New Patient',
            'birth_date': '2000-01-12',
            'address': 'Test Street',
            'id_number': 'NEW123456',
            'expiration_date': '',
            'missing_fields': [],
        }
        with patch('accounts.views.scan_id_image', return_value=extracted):
            scan_response = self.client.post(
                reverse('accounts:identity_scan_id'),
                {
                    'id_type': 'passport',
                    'biometric_consent': 'true',
                    'duplicate_check_consent': 'true',
                    'id_image': image_upload('new-id.png'),
                },
            )
        face_uploads = {
            'scan_token': scan_response.json()['scan_token'],
            'id_image': image_upload('new-id-again.png'),
            'selfie_front': image_upload('front.png'),
            'selfie_left': image_upload('left.png'),
            'selfie_right': image_upload('right.png'),
            'selfie_closer': image_upload('closer.png'),
            'selfie_farther': image_upload('farther.png'),
        }
        with patch(
            'accounts.views.compare_face_to_id_and_templates',
            return_value=(True, 0.8, b'\x03' * 512, 0, 0.9),
        ):
            match_response = self.client.post(
                reverse('accounts:identity_match_face'),
                face_uploads,
            )
        self.assertTrue(match_response.json()['possible_duplicate'])
        response = self.client.post(
            reverse('accounts:signup'),
            {
                'identity_proof': match_response.json()['proof_token'],
                'duplicate_check_consent': 'true',
                'username': 'new-patient',
                'first_name': 'New',
                'last_name': 'Patient',
                'email': 'new@example.com',
                'date_of_birth': '2000-01-12',
                'address': 'Test Street',
                'phone': '9171234567',
                'password1': 'SafePassword!123',
                'password2': 'SafePassword!123',
            },
        )
        self.assertRedirects(response, reverse('accounts:login'))
        report = SignupDuplicateReport.objects.get()
        self.assertEqual(report.candidate_user.username, 'new-patient')
        self.assertEqual(report.matched_user, existing_patient)
        self.assertEqual(report.match_similarity, 90.0)
        self.assertFalse(report.candidate_user.is_active)

    def test_existing_patient_can_enroll_and_remove_duplicate_data(self):
        user = get_user_model().objects.create_user(
            username='legacy-patient',
            password='SafePassword!123',
            role=Role.PATIENT,
        )
        verification = IdentityVerification.objects.create(
            user=user,
            id_type='passport',
            consented_at=timezone.now(),
        )
        self.client.force_login(user)
        self.assertContains(
            self.client.get(reverse('accounts:identity_enrollment')),
            'Identity enrollment',
        )
        extracted = {
            'full_name': 'Legacy Patient',
            'birth_date': '1990-01-12',
            'address': '',
            'id_number': 'LEGACY123',
            'expiration_date': '',
            'missing_fields': [],
        }
        with patch('accounts.views.scan_id_image', return_value=extracted):
            scan_response = self.client.post(
                reverse('accounts:identity_enrollment_scan_id'),
                {
                    'id_type': 'passport',
                    'biometric_consent': 'true',
                    'duplicate_check_consent': 'true',
                    'id_image': image_upload('legacy-id.png'),
                },
            )
        uploads = {
            'scan_token': scan_response.json()['scan_token'],
            'id_image': image_upload('legacy-id-again.png'),
            'selfie_front': image_upload('front.png'),
            'selfie_left': image_upload('left.png'),
            'selfie_right': image_upload('right.png'),
            'selfie_closer': image_upload('closer.png'),
            'selfie_farther': image_upload('farther.png'),
        }
        with patch(
            'accounts.views.compare_face_to_id_and_templates',
            return_value=(True, 0.8, b'\x04' * 512, None, -1.0),
        ):
            response = self.client.post(
                reverse('accounts:identity_enrollment_match_face'),
                uploads,
            )
        self.assertTrue(response.json()['enrolled'])
        verification.refresh_from_db()
        self.assertEqual(
            verification.id_number_digest,
            identity_id_number_digest('passport', 'LEGACY123'),
        )
        self.assertEqual(
            decrypt_face_template(verification.face_template),
            b'\x04' * 512,
        )
        response = self.client.post(
            reverse('accounts:identity_enrollment_delete')
        )
        self.assertRedirects(response, reverse('accounts:profile'))
        verification.refresh_from_db()
        self.assertIsNone(verification.id_number_digest)
        self.assertIsNone(verification.face_template)


class OCRParsingTests(TestCase):
    def test_surname_only_is_prefilled_as_last_name_and_invalid_address_is_omitted(self):
        fields = parse_id_text(
            'LAST NAME: ESPIRITU\n'
            'ADDRESS: PHL\n',
            'passport',
        )
        initial = _identity_form_initial({'fields': fields})

        self.assertEqual(initial['first_name'], '')
        self.assertEqual(initial['last_name'], 'ESPIRITU')
        self.assertEqual(initial['address'], '')

    def test_scanner_detects_id_type_and_extracts_fields_automatically(self):
        ocr_text = (
            'REPUBLIC OF THE PHILIPPINES\n'
            'PASSPORT\n'
            'FULL NAME: Ariana Test\n'
            'DATE OF BIRTH: 12 JAN 2000\n'
            'PASSPORT NO: P12345678\n'
        )
        with (
            patch('pytesseract.get_tesseract_version'),
            patch('pytesseract.image_to_string', return_value=ocr_text),
        ):
            fields = scan_id_image(image_upload('id.png').read())

        self.assertEqual(fields['id_type'], 'passport')
        self.assertEqual(fields['full_name'], 'Ariana Test')
        self.assertEqual(fields['birth_date'], '2000-01-12')
        self.assertEqual(fields['id_number'], 'P12345678')
        self.assertEqual(fields['missing_fields'], [])

    def test_scanner_combines_name_parts_read_across_ocr_passes(self):
        partial_text = (
            'PASSPORT\nLAST NAME: ESPIRITU\n'
            'DATE OF BIRTH: 22 MAR 2006\nPASSPORT NO: P12345678\n'
        )
        complete_text = (
            'PASSPORT\nGIVEN NAMES: SAMPLE USER\nLAST NAME: ESPIRITU\n'
            'DATE OF BIRTH: 22 MAR 2006\nPASSPORT NO: P12345678\n'
        )
        with (
            patch('pytesseract.get_tesseract_version'),
            patch(
                'pytesseract.image_to_string',
                side_effect=[partial_text, complete_text],
            ) as ocr,
        ):
            fields = scan_id_image(image_upload('id.png').read())

        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(fields['full_name'], 'SAMPLE USER ESPIRITU')
        self.assertEqual(fields['given_names'], 'SAMPLE USER')
        self.assertEqual(fields['surname'], 'ESPIRITU')

    def test_detects_supported_government_id_types(self):
        examples = (
            ('PHILSYS Philippine Identification System', 'ph_national_id'),
            ('LAND TRANSPORTATION OFFICE Driver License', 'drivers_license'),
            ('Republic of the Philippines Passport', 'passport'),
            ('UMID Unified Multi-Purpose ID', 'umid'),
        )

        for text, expected in examples:
            with self.subTest(id_type=expected):
                self.assertEqual(detect_id_type(text), expected)

    def test_does_not_guess_unknown_or_conflicting_id_types(self):
        self.assertIsNone(detect_id_type('Republic of the Philippines'))
        self.assertIsNone(detect_id_type('Passport UMID'))

    def test_extracts_labeled_passport_information(self):
        fields = parse_id_text(
            'FULL NAME: Ariana Test\n'
            'DATE OF BIRTH: 12 JAN 2000\n'
            'ADDRESS: Test Street, Manila\n'
            'PASSPORT NO: P12345678\n'
            'EXPIRY DATE: 12 JAN 2030\n',
            'passport',
        )
        self.assertEqual(fields['full_name'], 'Ariana Test')
        self.assertEqual(fields['birth_date'], '2000-01-12')
        self.assertEqual(fields['id_number'], 'P12345678')
        self.assertEqual(fields['expiration_date'], '2030-01-12')
        self.assertEqual(fields['missing_fields'], [])

    def test_incomplete_ocr_reports_required_fields(self):
        fields = parse_id_text('FULL NAME: Ariana Test\n', 'passport')
        self.assertEqual(fields['missing_fields'], ['Date of birth', 'ID number'])

    def test_extracts_philippine_national_id_with_bilingual_birth_label(self):
        fields = parse_id_text(
            'Apelyido/Last Name\n'
            'Sample\n'
            'Mga Pangalan/Given Names\n'
            'Patient\n'
            'Petsa ng Kapanganakan/Date of Birth: 22 MAR 2006\n'
            'PCN 1234-5678-9012-3456\n',
            'ph_national_id',
        )
        self.assertEqual(fields['full_name'], 'Patient Sample')
        self.assertEqual(fields['given_names'], 'Patient')
        self.assertEqual(fields['surname'], 'Sample')
        self.assertEqual(fields['birth_date'], '2006-03-22')
        self.assertEqual(fields['id_number'], '1234-5678-9012-3456')
        self.assertEqual(fields['missing_fields'], [])
