"""
Django settings for dental_clinic project.

AimSmile - Dental Clinic Management System
"""

from pathlib import Path
import os

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env', override=True)

SECRET_KEY = 'django-insecure-&&nxld253nd+=u)z9(s5!uxjszbiq%i6!hh4sf**wx3-*!5&@-'

DEBUG = True

ALLOWED_HOSTS = ['*']


INSTALLED_APPS = [
    # NOTE: default Django admin is intentionally NOT installed — this project
    # ships its own custom management panel (see the `manage_panel` app).
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',

    # Local apps
    'accounts',
    'clinic',
    'pages',
    'aiassistant',
    'manage_panel',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'dental_clinic.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounts.context_processors.theme_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'dental_clinic.wsgi.application'


DB_ENGINE = os.getenv('DB_ENGINE', 'mysql').strip().lower()
if DB_ENGINE == 'sqlite':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
elif DB_ENGINE == 'mysql':
    import pymysql

    pymysql.install_as_MySQLdb()
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': os.getenv('DB_NAME', 'aimsmile'),
            'USER': os.getenv('DB_USER', 'root'),
            'PASSWORD': os.getenv('DB_PASSWORD', ''),
            'HOST': os.getenv('DB_HOST', '127.0.0.1'),
            'PORT': os.getenv('DB_PORT', '3306'),
            'OPTIONS': {
                'charset': 'utf8mb4',
            },
        }
    }
else:
    raise ImproperlyConfigured(
        "DB_ENGINE must be either 'mysql' or 'sqlite'."
    )


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 6}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
]


LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True


# Static files
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media files (user uploads)
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom user
AUTH_USER_MODEL = 'accounts.User'

# Auth redirects
LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'accounts:redirect_dashboard'
LOGOUT_REDIRECT_URL = 'pages:home'

MESSAGE_STORAGE = 'django.contrib.messages.storage.session.SessionStorage'

# Currency used across the app
CLINIC_CURRENCY = '₱'


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


# Email is console-only by default. Configure SMTP before enabling recovery in
# a deployed environment.
EMAIL_BACKEND = os.getenv(
    'EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend',
)
EMAIL_HOST = os.getenv('EMAIL_HOST', '')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_USE_SSL = env_bool('EMAIL_USE_SSL', False)
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'webmaster@localhost')
SITE_URL = os.getenv('SITE_URL', 'http://127.0.0.1:8000').rstrip('/')

# Provider integrations stay off until their credentials are explicitly set.
RECAPTCHA_ENABLED = env_bool('RECAPTCHA_ENABLED')
RECAPTCHA_SITE_KEY = os.getenv('RECAPTCHA_SITE_KEY', '')
RECAPTCHA_SECRET_KEY = os.getenv('RECAPTCHA_SECRET_KEY', '')

TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', '')
TWILIO_FROM_NUMBER = os.getenv('TWILIO_FROM_NUMBER', '')
SMS_LOGIN_ENABLED = env_bool(
    'SMS_LOGIN_ENABLED',
    bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER),
)
SEMAPHORE_API_KEY = os.getenv('SEMAPHORE_API_KEY', '')
SEMAPHORE_SENDER_NAME = os.getenv('SEMAPHORE_SENDER_NAME', '')
APPOINTMENT_SMS_NOTIFICATIONS = env_bool(
    'APPOINTMENT_SMS_NOTIFICATIONS',
    bool(SEMAPHORE_API_KEY),
)
APPOINTMENT_EMAIL_NOTIFICATIONS = env_bool('APPOINTMENT_EMAIL_NOTIFICATIONS')

GOOGLE_CALENDAR_CLIENT_ID = os.getenv('GOOGLE_CALENDAR_CLIENT_ID', '')
GOOGLE_CALENDAR_CLIENT_SECRET = os.getenv('GOOGLE_CALENDAR_CLIENT_SECRET', '')
GOOGLE_CALENDAR_REDIRECT_URI = os.getenv(
    'GOOGLE_CALENDAR_REDIRECT_URI',
    f'{SITE_URL}/app/google-calendar/callback/',
)
GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY = os.getenv(
    'GOOGLE_CALENDAR_TOKEN_ENCRYPTION_KEY',
    '',
)

FACE_RECOGNITION_ENABLED = env_bool('FACE_RECOGNITION_ENABLED')
AWS_REGION = os.getenv('AWS_REGION', '')
AWS_REKOGNITION_COLLECTION_ID = os.getenv(
    'AWS_REKOGNITION_COLLECTION_ID',
    'aimsmile-patient-signups',
)
IDENTITY_TESSERACT_CMD = os.getenv('IDENTITY_TESSERACT_CMD', '')
IDENTITY_VERIFICATION_ENCRYPTION_KEY = os.getenv(
    'IDENTITY_VERIFICATION_ENCRYPTION_KEY',
    '',
)
IDENTITY_MODEL_DIR = Path(os.getenv(
    'IDENTITY_MODEL_DIR',
    str(BASE_DIR / 'models' / 'identity'),
))
IDENTITY_FACE_MATCH_THRESHOLD = float(
    os.getenv('IDENTITY_FACE_MATCH_THRESHOLD', '0.363')
)

XENDIT_ENABLED = env_bool('XENDIT_ENABLED')
XENDIT_API_KEY = os.getenv('XENDIT_API_KEY', '')
XENDIT_CALLBACK_TOKEN = os.getenv('XENDIT_CALLBACK_TOKEN', '')

