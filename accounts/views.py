import base64
import secrets
from datetime import datetime, timedelta
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode

from .forms import (
    LoginForm,
    PasswordSetupForm,
    PhoneVerificationForm,
    ProfileForm,
    SignUpForm,
    TOTPCodeForm,
)
from .integrations import (
    IntegrationError,
    decrypt_totp_secret,
    encrypt_totp_secret,
    new_totp_secret,
    send_sms,
    totp_provisioning_uri,
    verify_recaptcha,
    verify_totp,
)
from .identity_verification import (
    IdentityVerificationError,
    IdentityVerificationSetupError,
    compare_face_to_id_and_templates,
    decrypt_identity_state,
    encrypt_face_template,
    encrypt_identity_state,
    image_digest,
    identity_id_number_digest,
    scan_id_image,
)
from .audit import log_activity
from .models import (
    ActivityLog,
    GovernmentIDType,
    IdentityVerification,
    PhoneVerification,
    Role,
    SignupDuplicateReport,
    User,
)


IDENTITY_SESSION_TTL = timedelta(minutes=15)


def _clear_identity_session(request):
    for key in (
        'identity_scan_state',
        'identity_scan_token',
        'identity_proof_token',
        'identity_proof_created_at',
    ):
        request.session.pop(key, None)


def _identity_session_state(request):
    encrypted_state = request.session.get('identity_scan_state')
    if not encrypted_state:
        return None
    try:
        state = decrypt_identity_state(encrypted_state)
        created_at = datetime.fromisoformat(state['created_at'])
    except (
        IdentityVerificationError,
        IdentityVerificationSetupError,
        KeyError,
        TypeError,
        ValueError,
    ):
        _clear_identity_session(request)
        return None
    if timezone.now() - created_at > IDENTITY_SESSION_TTL:
        _clear_identity_session(request)
        return None
    return state


def _identity_form_initial(identity_state):
    fields = identity_state['fields']
    given_names = fields.get('given_names', '').strip()
    surname = fields.get('surname', '').strip()
    if given_names or surname:
        first_name, last_name = given_names, surname
    else:
        full_name = fields.get('full_name', '').strip()
        name_parts = full_name.rsplit(' ', 1)
        if len(name_parts) == 2:
            first_name, last_name = name_parts
        else:
            first_name, last_name = full_name, ''
    return {
        'first_name': first_name,
        'last_name': last_name,
        'date_of_birth': fields.get('birth_date', ''),
        'address': fields.get('address', ''),
    }


def _identity_signup_context(request, form=None):
    identity_state = _identity_session_state(request)
    proof_token = request.session.get('identity_proof_token', '')
    proof_created_at = request.session.get('identity_proof_created_at', '')
    verified = bool(
        identity_state
        and identity_state.get('flow') == 'signup'
        and proof_token
        and proof_created_at
    )
    if verified:
        try:
            proof_time = datetime.fromisoformat(proof_created_at)
            verified = timezone.now() - proof_time <= IDENTITY_SESSION_TTL
        except (TypeError, ValueError):
            verified = False
    if not verified:
        request.session.pop('identity_proof_token', None)
        request.session.pop('identity_proof_created_at', None)
        return {
            'identity_verified': False,
            'form': None,
            'supported_id_types': GovernmentIDType.choices,
        }
    return {
        'identity_verified': True,
        'identity_fields': identity_state['fields'],
        'identity_id_type': dict(GovernmentIDType.choices)[identity_state['id_type']],
        'identity_proof_token': proof_token,
        'form': form or SignUpForm(initial=_identity_form_initial(identity_state)),
        'recaptcha_enabled': settings.RECAPTCHA_ENABLED,
        'recaptcha_site_key': settings.RECAPTCHA_SITE_KEY,
        'supported_id_types': GovernmentIDType.choices,
    }


def signup_view(request):
    if request.user.is_authenticated:
        return redirect('accounts:redirect_dashboard')

    if request.method == 'POST':
        context = _identity_signup_context(request)
        submitted_proof = request.POST.get('identity_proof', '')
        if (
            not context['identity_verified']
            or not secrets.compare_digest(
                submitted_proof,
                context.get('identity_proof_token', ''),
            )
        ):
            messages.error(
                request,
                'Complete ID scanning and face matching before submitting your details.',
            )
            return redirect('accounts:signup')

        if request.POST.get('duplicate_check_consent') not in {'1', 'true', 'on'}:
            messages.error(
                request,
                'Consent to protected duplicate-patient checks is required to complete registration.',
            )
            return render(
                request,
                'accounts/identity_signup.html',
                _identity_signup_context(request, SignUpForm(request.POST)),
                status=400,
            )

        form = SignUpForm(request.POST)
        if form.is_valid():
            try:
                captcha_ok = verify_recaptcha(
                    request.POST.get('g-recaptcha-response', ''),
                    request.META.get('REMOTE_ADDR'),
                )
                if not captcha_ok:
                    form.add_error(None, 'Complete the reCAPTCHA check and try again.')
            except IntegrationError as exc:
                form.add_error(None, str(exc))
                return render(
                    request,
                    'accounts/identity_signup.html',
                    _identity_signup_context(request, form),
                    status=503,
                )

            if not form.errors:
                identity_state = _identity_session_state(request)
                try:
                    with transaction.atomic():
                        user = form.save(commit=False)
                        user.is_active = False
                        user.signup_status = 'pending'
                        user.save()
                        IdentityVerification.objects.create(
                            user=user,
                            id_type=identity_state['id_type'],
                            consented_at=datetime.fromisoformat(
                                identity_state['consented_at']
                            ),
                            duplicate_check_consented_at=datetime.fromisoformat(
                                identity_state['duplicate_check_consented_at']
                            ),
                            id_number_digest=identity_state['id_number_digest'],
                            face_template=encrypt_face_template(
                                base64.b64decode(identity_state['face_template'])
                            ),
                        )
                        matched_user_id = identity_state.get(
                            'face_duplicate_user_id'
                        )
                        matched_user = User.objects.filter(
                            pk=matched_user_id,
                            role=Role.PATIENT,
                        ).first() if matched_user_id else None
                        if matched_user:
                            SignupDuplicateReport.objects.create(
                                first_name=user.first_name,
                                last_name=user.last_name,
                                email=user.email,
                                phone=user.phone,
                                match_similarity=(
                                    identity_state['face_duplicate_similarity']
                                    * 100
                                ),
                                candidate_user=user,
                                matched_user=matched_user,
                            )
                except IntegrityError:
                    form.add_error(
                        None,
                        'Your account could not be created. Please check the details and try again.',
                    )
                else:
                    _clear_identity_session(request)
                    if matched_user:
                        messages.success(
                            request,
                            'Your registration was sent for clinic review because the face may match an existing patient.',
                        )
                    else:
                        messages.success(
                            request,
                            'Identity verification succeeded. Your account was submitted for clinic approval.',
                        )
                    return redirect('accounts:login')
        return render(
            request,
            'accounts/identity_signup.html',
            _identity_signup_context(request, form),
        )

    return render(
        request,
        'accounts/identity_signup.html',
        _identity_signup_context(request),
    )


@require_POST
def identity_scan_id(request):
    return _identity_scan_id(request, enrollment=False)


@require_POST
def identity_enrollment_scan_id(request):
    return _identity_scan_id(request, enrollment=True)


def _identity_scan_id(request, enrollment):
    if enrollment:
        if not request.user.is_authenticated:
            return JsonResponse(
                {'ok': False, 'message': 'Sign in to enroll identity for duplicate protection.'},
                status=401,
            )
        if not request.user.is_patient():
            return JsonResponse(
                {'ok': False, 'message': 'Only patient accounts can enroll identity data.'},
                status=403,
            )
    elif request.user.is_authenticated:
        return JsonResponse({'ok': False, 'message': 'You are already signed in.'}, status=400)

    id_type = request.POST.get('id_type', '')
    if id_type and id_type not in dict(GovernmentIDType.choices):
        return JsonResponse(
            {'ok': False, 'message': 'Select one of the supported government ID types.'},
            status=400,
        )
    if request.POST.get('biometric_consent') not in {'1', 'true', 'on'}:
        return JsonResponse(
            {'ok': False, 'message': 'Consent is required before local ID and face processing.'},
            status=400,
        )
    if request.POST.get('duplicate_check_consent') not in {'1', 'true', 'on'}:
        return JsonResponse(
            {'ok': False, 'message': 'Consent is required to store protected data for duplicate-patient checks.'},
            status=400,
        )

    _clear_identity_session(request)
    id_image = request.FILES.get('id_image')
    if not id_image or id_image.size > 5 * 1024 * 1024:
        return JsonResponse(
            {'ok': False, 'message': 'Choose a clear JPEG or PNG image of your ID that is 5 MB or smaller.'},
            status=400,
        )
    image_bytes = id_image.read()
    try:
        fields = scan_id_image(image_bytes, id_type)
    except IdentityVerificationSetupError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=503)
    except IdentityVerificationError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=422)

    id_type = fields.pop('id_type', '') or id_type
    if id_type not in dict(GovernmentIDType.choices):
        return JsonResponse(
            {'ok': False, 'message': 'The ID type could not be recognized. Select it manually and scan again.'},
            status=422,
        )

    if fields['missing_fields']:
        return JsonResponse({
            'ok': False,
            'message': (
                'The ID could not be read completely. Improve the lighting, show '
                'the full ID, and upload it again.'
            ),
            'missing_fields': fields['missing_fields'],
        }, status=422)

    try:
        id_digest = identity_id_number_digest(id_type, fields['id_number'])
    except IdentityVerificationSetupError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=503)
    except IdentityVerificationError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=422)

    existing_identities = IdentityVerification.objects.filter(
        id_number_digest=id_digest
    )
    if enrollment:
        existing_identities = existing_identities.exclude(user=request.user)
    if existing_identities.exists():
        return JsonResponse({
            'ok': False,
            'duplicate_id': True,
            'message': (
                'This ID may already be linked to a patient account. Sign in to your '
                'existing account or contact the clinic if you believe this is an error.'
            ),
        }, status=409)

    now = timezone.now()
    scan_token = secrets.token_urlsafe(32)
    state = {
        'flow': 'enrollment' if enrollment else 'signup',
        'enrollment_user_id': request.user.pk if enrollment else None,
        'id_type': id_type,
        'image_sha256': image_digest(image_bytes),
        'id_number_digest': id_digest,
        'fields': fields,
        'consented_at': now.isoformat(),
        'duplicate_check_consented_at': now.isoformat(),
        'created_at': now.isoformat(),
    }
    try:
        encrypted_state = encrypt_identity_state(state)
    except IdentityVerificationSetupError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=503)
    request.session['identity_scan_state'] = encrypted_state
    request.session['identity_scan_token'] = scan_token
    request.session.pop('identity_proof_token', None)
    request.session.pop('identity_proof_created_at', None)
    return JsonResponse({
        'ok': True,
        'scan_token': scan_token,
        'id_type': id_type,
        'id_type_label': dict(GovernmentIDType.choices)[id_type],
        'message': (
            f'{dict(GovernmentIDType.choices)[id_type]} detected. '
            'After face verification, review the extracted details and account fields.'
        ),
    })


@require_POST
def identity_match_face(request):
    return _identity_match_face(request, enrollment=False)


@require_POST
def identity_enrollment_match_face(request):
    return _identity_match_face(request, enrollment=True)


def _identity_match_face(request, enrollment):
    if enrollment:
        if not request.user.is_authenticated:
            return JsonResponse(
                {'ok': False, 'message': 'Sign in to enroll identity for duplicate protection.'},
                status=401,
            )
        if not request.user.is_patient():
            return JsonResponse(
                {'ok': False, 'message': 'Only patient accounts can enroll identity data.'},
                status=403,
            )
    elif request.user.is_authenticated:
        return JsonResponse({'ok': False, 'message': 'You are already signed in.'}, status=400)

    state = _identity_session_state(request)
    submitted_scan_token = request.POST.get('scan_token', '')
    current_scan_token = request.session.get('identity_scan_token', '')
    if (
        not state
        or state.get('flow') != ('enrollment' if enrollment else 'signup')
        or (
            enrollment
            and state.get('enrollment_user_id') != request.user.pk
        )
        or not current_scan_token
        or not secrets.compare_digest(submitted_scan_token, current_scan_token)
    ):
        return JsonResponse(
            {'ok': False, 'message': 'Your ID scan expired. Scan the ID again.'},
            status=409,
        )

    id_image = request.FILES.get('id_image')
    selfie_front = request.FILES.get('selfie_front')
    pose_names = ('selfie_left', 'selfie_right', 'selfie_closer', 'selfie_farther')
    pose_images = [request.FILES.get(name) for name in pose_names]
    uploads = [id_image, selfie_front, *pose_images]
    if any(not image or image.size > 5 * 1024 * 1024 for image in uploads):
        return JsonResponse(
            {'ok': False, 'message': 'Capture all five clear face images; each image must be 5 MB or smaller.'},
            status=400,
        )

    id_image_bytes = id_image.read()
    if not secrets.compare_digest(
        image_digest(id_image_bytes),
        state['image_sha256'],
    ):
        return JsonResponse(
            {'ok': False, 'message': 'The ID image changed. Scan the same ID again before face matching.'},
            status=409,
        )

    existing_records = IdentityVerification.objects.filter(
        user__role=Role.PATIENT,
        duplicate_check_consented_at__isnull=False,
        face_template__isnull=False,
    )
    if enrollment:
        existing_records = existing_records.exclude(user=request.user)
    existing_records = list(existing_records.select_related('user'))
    selfie_front_bytes = selfie_front.read()
    pose_image_bytes = [image.read() for image in pose_images]
    try:
        (
            matched,
            similarity,
            face_template,
            duplicate_index,
            duplicate_similarity,
        ) = compare_face_to_id_and_templates(
            id_image_bytes,
            selfie_front_bytes,
            pose_image_bytes,
            [record.face_template for record in existing_records],
        )
    except IdentityVerificationSetupError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=503)
    except IdentityVerificationError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=422)

    if not matched:
        request.session.pop('identity_proof_token', None)
        request.session.pop('identity_proof_created_at', None)
        return JsonResponse({
            'ok': False,
            'matched': False,
            'similarity': round(similarity * 100, 1),
            'message': 'The face did not meet the local match threshold. Retake the face photos or rescan the ID.',
        }, status=409)

    duplicate_record = None
    if (
        duplicate_index is not None
        and duplicate_similarity >= settings.IDENTITY_FACE_MATCH_THRESHOLD
    ):
        duplicate_record = existing_records[duplicate_index]

    state['face_template'] = base64.b64encode(face_template).decode('ascii')
    state['face_duplicate_user_id'] = (
        duplicate_record.user_id if duplicate_record else None
    )
    state['face_duplicate_similarity'] = (
        duplicate_similarity if duplicate_record else None
    )

    if enrollment:
        try:
            encrypted_template = encrypt_face_template(face_template)
            with transaction.atomic():
                identity_record, _ = IdentityVerification.objects.get_or_create(
                    user=request.user,
                    defaults={
                        'id_type': state['id_type'],
                        'consented_at': datetime.fromisoformat(
                            state['consented_at']
                        ),
                    },
                )
                identity_record.id_type = state['id_type']
                identity_record.consented_at = datetime.fromisoformat(
                    state['consented_at']
                )
                identity_record.duplicate_check_consented_at = datetime.fromisoformat(
                    state['duplicate_check_consented_at']
                )
                identity_record.id_number_digest = state['id_number_digest']
                identity_record.face_template = encrypted_template
                identity_record.verified_at = timezone.now()
                identity_record.save()
                if duplicate_record:
                    SignupDuplicateReport.objects.create(
                        first_name=request.user.first_name,
                        last_name=request.user.last_name,
                        email=request.user.email,
                        phone=request.user.phone,
                        match_similarity=duplicate_similarity * 100,
                        candidate_user=request.user,
                        matched_user=duplicate_record.user,
                    )
        except IntegrityError:
            return JsonResponse({
                'ok': False,
                'duplicate_id': True,
                'message': (
                    'This ID may already be linked to a patient account. Contact '
                    'the clinic if you believe this is an error.'
                ),
            }, status=409)
        except (IdentityVerificationError, IdentityVerificationSetupError) as exc:
            return JsonResponse({'ok': False, 'message': str(exc)}, status=503)

        _clear_identity_session(request)
        message = (
            'Identity enrollment is complete. A possible face duplicate was sent '
            'to the clinic for review.'
            if duplicate_record
            else 'Identity enrollment is complete for duplicate-patient checks.'
        )
        return JsonResponse({
            'ok': True,
            'enrolled': True,
            'possible_duplicate': bool(duplicate_record),
            'message': message,
        })

    try:
        request.session['identity_scan_state'] = encrypt_identity_state(state)
    except IdentityVerificationSetupError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=503)

    proof_token = secrets.token_urlsafe(32)
    request.session['identity_proof_token'] = proof_token
    request.session['identity_proof_created_at'] = timezone.now().isoformat()
    message = (
        'Face matched. A similar enrolled face was found, so your registration '
        'will be sent to the clinic for review.'
        if duplicate_record
        else 'Face matched. Review your personal information before final submission.'
    )
    return JsonResponse({
        'ok': True,
        'matched': True,
        'similarity': round(similarity * 100, 1),
        'proof_token': proof_token,
        'fields': state['fields'],
        'possible_duplicate': bool(duplicate_record),
        'message': message,
    })


def _send_phone_code(user):
    verification, _ = PhoneVerification.objects.get_or_create(
        user=user,
        defaults={
            'code_hash': '',
            'expires_at': timezone.now(),
        },
    )
    now = timezone.now()
    if verification.pk and verification.code_hash and (
        now - verification.last_sent_at
    ).total_seconds() < 60:
        raise IntegrationError('Wait one minute before requesting another SMS code.')
    code = f'{secrets.randbelow(1_000_000):06d}'
    verification.code_hash = make_password(code)
    verification.expires_at = now + timedelta(minutes=10)
    verification.last_sent_at = now
    verification.attempts = 0
    verification.save()
    send_sms(user.phone, f'Your AimSmile verification code is {code}. It expires in 10 minutes.')


class AppLoginView(LoginView):
    template_name = 'accounts/login.html'
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['recaptcha_enabled'] = settings.RECAPTCHA_ENABLED
        context['recaptcha_site_key'] = settings.RECAPTCHA_SITE_KEY
        return context

    def post(self, request, *args, **kwargs):
        if settings.RECAPTCHA_ENABLED:
            try:
                captcha_ok = verify_recaptcha(
                    request.POST.get('g-recaptcha-response', ''),
                    request.META.get('REMOTE_ADDR'),
                )
            except IntegrationError as exc:
                form = self.get_form()
                form.add_error(None, str(exc))
                return self.form_invalid(form)
            if not captcha_ok:
                form = self.get_form()
                form.add_error(None, 'Complete the reCAPTCHA check and try again.')
                return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        if settings.SMS_LOGIN_ENABLED:
            if not user.phone or user.phone == '0000000000':
                form = self.get_form()
                form.add_error(None, 'This account needs a verified phone number before SMS login.')
                return self.form_invalid(form)
            self.request.session.cycle_key()
            self.request.session['pending_sms_user_id'] = user.pk
            self.request.session['pending_sms_attempts'] = 0
            if user.totp_enabled:
                self.request.session['pending_2fa_user_id'] = user.pk
                self.request.session['pending_2fa_attempts'] = 0
            try:
                _send_phone_code(user)
            except IntegrationError as exc:
                form = self.get_form()
                form.add_error(None, str(exc))
                return self.form_invalid(form)
            messages.info(self.request, f'A verification code was sent to {user.phone}.')
            return redirect('accounts:sms_login')
        if user.totp_enabled:
            self.request.session.cycle_key()
            self.request.session['pending_2fa_user_id'] = user.pk
            self.request.session['pending_2fa_attempts'] = 0
            return redirect('accounts:two_factor_login')
        messages.success(self.request, f'Welcome back, {user.display_name}!')
        return super().form_valid(form)


def sms_login_view(request):
    user_id = request.session.get('pending_sms_user_id')
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if not user:
        request.session.pop('pending_sms_user_id', None)
        request.session.pop('pending_sms_attempts', None)
        return redirect('accounts:login')

    form = PhoneVerificationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        verification = PhoneVerification.objects.filter(user=user).first()
        attempts = request.session.get('pending_sms_attempts', 0)
        if not verification:
            form.add_error(None, 'Request a new SMS code before verifying.')
        elif verification.expires_at <= timezone.now():
            form.add_error(None, 'That code has expired. Request another SMS code.')
        elif attempts >= 5:
            log_activity(
                request,
                ActivityLog.Action.LOGIN_FAILED,
                'Failed SMS verification attempt.',
                target=user,
                target_label=user.get_username(),
            )
            form.add_error(None, 'Too many attempts. Sign in again to retry.')
        elif not check_password(form.cleaned_data['code'], verification.code_hash):
            attempts += 1
            verification.attempts = attempts
            verification.save(update_fields=['attempts'])
            request.session['pending_sms_attempts'] = attempts
            log_activity(
                request,
                ActivityLog.Action.LOGIN_FAILED,
                'Failed SMS verification attempt.',
                target=user,
                target_label=user.get_username(),
            )
            form.add_error('code', 'That SMS code is not valid.')
        else:
            verification.delete()
            user.phone_verified_at = timezone.now()
            user.save(update_fields=['phone_verified_at'])
            request.session.pop('pending_sms_user_id', None)
            request.session.pop('pending_sms_attempts', None)
            if user.totp_enabled:
                return redirect('accounts:two_factor_login')
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, f'Welcome back, {user.display_name}!')
            return redirect('accounts:redirect_dashboard')
    return render(request, 'accounts/sms_login.html', {'form': form, 'phone': user.phone})


@require_POST
def sms_login_resend_view(request):
    user_id = request.session.get('pending_sms_user_id')
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if not user:
        return redirect('accounts:login')
    try:
        _send_phone_code(user)
        messages.success(request, 'A new SMS verification code was sent.')
    except IntegrationError as exc:
        messages.error(request, str(exc))
    return redirect('accounts:sms_login')


def two_factor_login_view(request):
    user_id = request.session.get('pending_2fa_user_id')
    user = User.objects.filter(pk=user_id, is_active=True, totp_enabled=True).first()
    if not user:
        request.session.pop('pending_2fa_user_id', None)
        request.session.pop('pending_2fa_attempts', None)
        return redirect('accounts:login')
    form = TOTPCodeForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        attempts = request.session.get('pending_2fa_attempts', 0)
        try:
            valid = attempts < 5 and verify_totp(
                user.totp_secret,
                form.cleaned_data['code'],
            )
        except IntegrationError as exc:
            form.add_error(None, str(exc))
            valid = False
        if valid:
            request.session.pop('pending_2fa_user_id', None)
            request.session.pop('pending_2fa_attempts', None)
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, f'Welcome back, {user.display_name}!')
            return redirect('accounts:redirect_dashboard')
        attempts += 1
        request.session['pending_2fa_attempts'] = attempts
        log_activity(
            request,
            ActivityLog.Action.LOGIN_FAILED,
            'Failed authenticator verification attempt.',
            target=user,
            target_label=user.get_username(),
        )
        if attempts >= 5:
            request.session.pop('pending_2fa_user_id', None)
            request.session.pop('pending_2fa_attempts', None)
            form.add_error(None, 'Too many invalid codes. Sign in again to retry.')
        elif not form.errors:
            form.add_error('code', 'That authenticator code is not valid.')
    return render(request, 'accounts/two_factor_login.html', {'form': form})


@login_required
def two_factor_settings_view(request):
    user = request.user
    form = TOTPCodeForm(request.POST or None)
    provisioning_uri = ''
    secret = ''
    if user.totp_enabled:
        if request.method == 'POST' and form.is_valid():
            try:
                valid = verify_totp(user.totp_secret, form.cleaned_data['code'])
            except IntegrationError as exc:
                form.add_error(None, str(exc))
                valid = False
            if valid:
                user.totp_enabled = False
                user.totp_secret = ''
                user.save(update_fields=['totp_enabled', 'totp_secret'])
                messages.success(request, 'Two-factor authentication has been disabled.')
                return redirect('accounts:two_factor_settings')
            if not form.errors:
                form.add_error('code', 'That authenticator code is not valid.')
    else:
        if not user.totp_secret:
            secret = new_totp_secret()
            user.totp_secret = encrypt_totp_secret(secret)
            user.save(update_fields=['totp_secret'])
        else:
            secret = decrypt_totp_secret(user.totp_secret)
        provisioning_uri = totp_provisioning_uri(user.username, secret)
        if request.method == 'POST' and form.is_valid():
            if verify_totp(user.totp_secret, form.cleaned_data['code']):
                user.totp_enabled = True
                user.save(update_fields=['totp_enabled'])
                messages.success(request, 'Two-factor authentication is enabled.')
                return redirect('accounts:two_factor_settings')
            form.add_error('code', 'That authenticator code is not valid.')
    return render(request, 'accounts/two_factor_settings.html', {
        'form': form,
        'enabled': user.totp_enabled,
        'secret': secret,
        'provisioning_uri': provisioning_uri,
    })


def password_setup(request, uidb64, token):
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        return render(request, 'accounts/password_setup_invalid.html', status=400)

    form = PasswordSetupForm(
        user, request.POST if request.method == 'POST' else None)
    if form.is_valid():
        form.save()
        messages.success(request, 'Your password is set. You can now sign in.')
        return redirect('accounts:login')
    return render(request, 'accounts/password_setup.html', {'form': form})


@login_required
def redirect_dashboard(request):
    """Send each user to the dashboard that matches their role."""
    role = request.user.role
    if request.user.is_superuser or role == Role.ADMIN:
        return redirect('manage_panel:dashboard')
    if role == Role.DENTIST:
        return redirect('clinic:dentist_dashboard')
    if role == Role.RECEPTIONIST:
        return redirect('clinic:receptionist_dashboard')
    return redirect('clinic:patient_dashboard')


@login_required
def profile_view(request):
    if request.method == 'POST':
        form = ProfileForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Your profile has been updated.')
            return redirect('accounts:profile')
    else:
        form = ProfileForm(instance=request.user)
    identity_record = IdentityVerification.objects.filter(
        user=request.user
    ).first()
    identity_enrolled = bool(
        identity_record
        and identity_record.duplicate_check_consented_at
        and identity_record.id_number_digest
        and identity_record.face_template
    )
    return render(request, 'accounts/profile.html', {
        'form': form,
        'identity_enrolled': identity_enrolled,
    })


@login_required
def identity_enrollment_view(request):
    if not request.user.is_patient():
        return HttpResponseForbidden('Patient accounts only.')
    identity_record = IdentityVerification.objects.filter(
        user=request.user
    ).first()
    identity_enrolled = bool(
        identity_record
        and identity_record.duplicate_check_consented_at
        and identity_record.id_number_digest
        and identity_record.face_template
    )
    return render(request, 'accounts/identity_signup.html', {
        'identity_enrollment': True,
        'identity_enrolled': identity_enrolled,
        'identity_verified': False,
        'supported_id_types': GovernmentIDType.choices,
    })


@login_required
@require_POST
def identity_enrollment_delete(request):
    if not request.user.is_patient():
        return HttpResponseForbidden('Patient accounts only.')
    IdentityVerification.objects.filter(user=request.user).update(
        duplicate_check_consented_at=None,
        id_number_digest=None,
        face_template=None,
    )
    messages.success(
        request,
        'Your duplicate-check identity data was removed. You can enroll again from your profile.',
    )
    return redirect('accounts:profile')
