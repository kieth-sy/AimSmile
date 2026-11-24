from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings
from django.utils import timezone


class Role(models.TextChoices):
    ADMIN = 'admin', 'Administrator'
    DENTIST = 'dentist', 'Dentist'
    RECEPTIONIST = 'receptionist', 'Receptionist'
    PATIENT = 'patient', 'Patient'


class SignupStatus(models.TextChoices):
    APPROVED = 'approved', 'Approved'
    PENDING = 'pending', 'Pending review'
    REJECTED = 'rejected', 'Rejected'


class Gender(models.TextChoices):
    MALE = 'male', 'Male'
    FEMALE = 'female', 'Female'
    OTHER = 'other', 'Other'


class GovernmentIDType(models.TextChoices):
    PHILIPPINE_NATIONAL_ID = 'ph_national_id', 'Philippine National ID'
    DRIVERS_LICENSE = 'drivers_license', "Driver's License"
    PASSPORT = 'passport', 'Passport'
    UMID = 'umid', 'UMID'


# Theme metadata used to give every role a distinct look & feel.
ROLE_THEMES = {
    Role.ADMIN: {
        'name': 'theme-admin',
        'label': 'Admin Console',
        'primary': '#4f46e5',
        'accent': '#a855f7',
        'gradient': 'linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%)',
        'icon': 'bi-shield-lock',
    },
    Role.DENTIST: {
        'name': 'theme-dentist',
        'label': 'Dentist Workspace',
        'primary': '#0d9488',
        'accent': '#22c55e',
        'gradient': 'linear-gradient(135deg, #0d9488 0%, #16a34a 100%)',
        'icon': 'bi-heart-pulse',
    },
    Role.RECEPTIONIST: {
        'name': 'theme-receptionist',
        'label': 'Front Desk',
        'primary': '#2563eb',
        'accent': '#06b6d4',
        'gradient': 'linear-gradient(135deg, #2563eb 0%, #06b6d4 100%)',
        'icon': 'bi-calendar2-week',
    },
    Role.PATIENT: {
        'name': 'theme-patient',
        'label': 'My Health',
        'primary': '#e11d48',
        'accent': '#f97316',
        'gradient': 'linear-gradient(135deg, #f43f5e 0%, #f97316 100%)',
        'icon': 'bi-emoji-smile',
    },
}


class User(AbstractUser):
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.PATIENT)
    phone = models.CharField(max_length=20, blank=True, default='0000000000')
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    address = models.CharField(max_length=255, blank=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    bio = models.TextField(blank=True)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    signup_status = models.CharField(
        max_length=10,
        choices=SignupStatus.choices,
        default=SignupStatus.APPROVED,
    )
    totp_secret = models.CharField(max_length=512, blank=True)
    totp_enabled = models.BooleanField(default=False)
    rekognition_face_id = models.CharField(max_length=128, blank=True)

    @property
    def theme(self):
        return ROLE_THEMES.get(self.role, ROLE_THEMES[Role.PATIENT])

    @property
    def initials(self):
        first = (self.first_name or self.username or '?')[:1]
        last = (self.last_name or '')[:1]
        return (first + last).upper()

    @property
    def display_name(self):
        full = self.get_full_name()
        return full if full else self.username

    def is_admin(self):
        return self.role == Role.ADMIN or self.is_superuser

    def is_dentist(self):
        return self.role == Role.DENTIST

    def is_receptionist(self):
        return self.role == Role.RECEPTIONIST

    def is_patient(self):
        return self.role == Role.PATIENT

    def __str__(self):
        return f"{self.display_name} ({self.get_role_display()})"


class PhoneVerification(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='phone_verification',
    )
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    last_sent_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)


class SignupDuplicateReport(models.Model):
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    match_similarity = models.FloatField()
    candidate_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='duplicate_signup_reports',
    )
    matched_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='matched_duplicate_signup_reports',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_duplicate_signups',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class IdentityVerification(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='identity_verification',
    )
    id_type = models.CharField(max_length=32, choices=GovernmentIDType.choices)
    consented_at = models.DateTimeField()
    duplicate_check_consented_at = models.DateTimeField(null=True, blank=True)
    id_number_digest = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
    )
    face_template = models.BinaryField(null=True, blank=True)
    verified_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-verified_at']

    def __str__(self):
        return f'{self.user} identity verified'


class ActivityLog(models.Model):
    class Action(models.TextChoices):
        LOGIN_SUCCESS = 'login_success', 'Successful sign-in'
        LOGIN_FAILED = 'login_failed', 'Failed sign-in'
        LOGOUT = 'logout', 'Sign-out'
        USER_CREATED = 'user_created', 'User created'
        USER_UPDATED = 'user_updated', 'User updated'
        USER_DELETED = 'user_deleted', 'User deleted'
        USER_APPROVED = 'user_approved', 'User approved'
        USER_REJECTED = 'user_rejected', 'User rejected'
        DUPLICATE_REVIEWED = 'duplicate_reviewed', 'Duplicate signup reviewed'
        RESOURCE_CREATED = 'resource_created', 'Clinic resource created'
        RESOURCE_UPDATED = 'resource_updated', 'Clinic resource updated'
        RESOURCE_DELETED = 'resource_deleted', 'Clinic resource deleted'
        APPOINTMENT_CREATED = 'appointment_created', 'Appointment created'
        APPOINTMENT_UPDATED = 'appointment_updated', 'Appointment updated'
        APPOINTMENT_STATUS_CHANGED = 'appointment_status_changed', 'Appointment status changed'
        APPOINTMENT_CANCELLED = 'appointment_cancelled', 'Appointment cancelled'
        APPOINTMENT_DELETED = 'appointment_deleted', 'Appointment deleted'
        INVOICE_CREATED = 'invoice_created', 'Invoice created'
        PAYMENT_SUBMITTED = 'payment_submitted', 'Payment submitted'
        PAYMENT_RECORDED = 'payment_recorded', 'Payment recorded'
        PAYMENT_REVIEWED = 'payment_reviewed', 'Payment reviewed'
        INVOICE_DELETED = 'invoice_deleted', 'Invoice deleted'

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activity_logs',
    )
    actor_username = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=40, choices=Action.choices)
    summary = models.CharField(max_length=255)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    target_label = models.CharField(max_length=150, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [
            models.Index(fields=['-created_at', 'action']),
        ]

    def __str__(self):
        return f'{self.get_action_display()} at {self.created_at:%Y-%m-%d %H:%M}'
