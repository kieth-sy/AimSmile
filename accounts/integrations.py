import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


logger = logging.getLogger(__name__)


class IntegrationError(Exception):
    """A provider is unavailable or returned an unusable response."""


def verify_recaptcha(token, remote_ip=None):
    if not settings.RECAPTCHA_ENABLED:
        return True
    if not settings.RECAPTCHA_SITE_KEY or not settings.RECAPTCHA_SECRET_KEY:
        raise IntegrationError('Google reCAPTCHA is enabled but not configured.')
    if not token:
        return False

    data = {
        'secret': settings.RECAPTCHA_SECRET_KEY,
        'response': token,
    }
    if remote_ip:
        data['remoteip'] = remote_ip
    request = urllib.request.Request(
        'https://www.google.com/recaptcha/api/siteverify',
        data=urllib.parse.urlencode(data).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning('reCAPTCHA verification request failed: %s', exc)
        raise IntegrationError('Google reCAPTCHA could not be reached.') from exc
    return result.get('success') is True


def send_sms(phone, message):
    if not all((
        settings.TWILIO_ACCOUNT_SID,
        settings.TWILIO_AUTH_TOKEN,
        settings.TWILIO_FROM_NUMBER,
    )):
        raise IntegrationError('Twilio SMS is not configured.')

    endpoint = (
        'https://api.twilio.com/2010-04-01/Accounts/'
        f'{settings.TWILIO_ACCOUNT_SID}/Messages.json'
    )
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode({
            'To': phone,
            'From': settings.TWILIO_FROM_NUMBER,
            'Body': message,
        }).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    credentials = base64.b64encode(
        f'{settings.TWILIO_ACCOUNT_SID}:{settings.TWILIO_AUTH_TOKEN}'.encode()
    ).decode()
    request.add_header('Authorization', f'Basic {credentials}')
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        logger.error('Twilio rejected an SMS request with HTTP %s.', exc.code)
        raise IntegrationError(
            f'Twilio rejected the SMS request (HTTP {exc.code}).'
        ) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning('Twilio SMS request failed: %s', exc)
        raise IntegrationError('Twilio SMS could not be reached.') from exc
    if not result.get('sid'):
        raise IntegrationError('Twilio did not confirm the SMS request.')


def _rekognition_client():
    if not settings.FACE_RECOGNITION_ENABLED:
        raise IntegrationError('Face duplicate detection is not enabled.')
    if not settings.AWS_REGION or not settings.AWS_REKOGNITION_COLLECTION_ID:
        raise IntegrationError('AWS Rekognition is enabled but not configured.')
    return boto3.client('rekognition', region_name=settings.AWS_REGION)


def _ensure_face_collection(client):
    try:
        client.create_collection(
            CollectionId=settings.AWS_REKOGNITION_COLLECTION_ID,
        )
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') != 'ResourceAlreadyExistsException':
            raise


def match_and_index_face(image_bytes):
    """Return (similarity, face_id); a non-None similarity means a match."""
    client = _rekognition_client()
    try:
        _ensure_face_collection(client)
        matches = client.search_faces_by_image(
            CollectionId=settings.AWS_REKOGNITION_COLLECTION_ID,
            Image={'Bytes': image_bytes},
            FaceMatchThreshold=95,
            MaxFaces=1,
        ).get('FaceMatches', [])
        if matches:
            return matches[0]['Similarity'], None

        indexed = client.index_faces(
            CollectionId=settings.AWS_REKOGNITION_COLLECTION_ID,
            Image={'Bytes': image_bytes},
            ExternalImageId=uuid.uuid4().hex,
            MaxFaces=1,
            QualityFilter='AUTO',
        ).get('FaceRecords', [])
        if not indexed:
            raise IntegrationError(
                'AWS Rekognition could not detect a clear face in the submitted photo.'
            )
        return None, indexed[0]['Face']['FaceId']
    except IntegrationError:
        raise
    except (BotoCoreError, ClientError) as exc:
        logger.error('AWS Rekognition request failed: %s', exc)
        raise IntegrationError('AWS Rekognition could not verify the submitted photo.') from exc


def delete_indexed_face(face_id):
    if not face_id:
        return
    client = _rekognition_client()
    try:
        client.delete_faces(
            CollectionId=settings.AWS_REKOGNITION_COLLECTION_ID,
            FaceIds=[face_id],
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error('AWS Rekognition face deletion failed: %s', exc)
        raise IntegrationError('The stored face record could not be deleted.') from exc


def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def encrypt_totp_secret(secret):
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    )
    return Fernet(key).encrypt(secret.encode()).decode()


def decrypt_totp_secret(encrypted_secret):
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    )
    try:
        return Fernet(key).decrypt(encrypted_secret.encode()).decode()
    except InvalidToken as exc:
        raise IntegrationError(
            'The authenticator key cannot be read. Check that the deployment SECRET_KEY has not changed.'
        ) from exc


def totp_code(secret, at_time=None):
    timestamp = time.time() if at_time is None else at_time
    key = base64.b32decode(secret + '=' * (-len(secret) % 8), casefold=True)
    counter = int(timestamp // 30)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f'{value % 1_000_000:06d}'


def verify_totp(encrypted_secret, code, at_time=None):
    if not code or len(code) != 6 or not code.isdigit():
        return False
    secret = decrypt_totp_secret(encrypted_secret)
    timestamp = time.time() if at_time is None else at_time
    return any(
        hmac.compare_digest(totp_code(secret, timestamp + offset * 30), code)
        for offset in (-1, 0, 1)
    )


def totp_provisioning_uri(username, secret):
    label = urllib.parse.quote(f'AimSmile:{username}')
    issuer = urllib.parse.quote('AimSmile')
    return (
        f'otpauth://totp/{label}?secret={secret}'
        f'&issuer={issuer}&algorithm=SHA1&digits=6&period=30'
    )


def create_xendit_invoice(invoice, amount, description=None):
    if not settings.XENDIT_ENABLED:
        raise IntegrationError('Xendit online payments are not enabled.')
    if not settings.XENDIT_API_KEY:
        raise IntegrationError('Xendit is enabled but XENDIT_API_KEY is missing.')

    payload = {
        'external_id': f'{invoice.number}-{uuid.uuid4().hex}',
        'amount': str(amount.quantize(Decimal('0.01'))),
        'payer_email': invoice.patient.email or 'patient@example.com',
        'description': description or f'Payment for {invoice.number}',
        'currency': 'PHP',
        'success_redirect_url': (
            settings.SITE_URL
            + f'/app/invoices/{invoice.pk}/'
        ),
        'failure_redirect_url': (
            settings.SITE_URL
            + f'/app/invoices/{invoice.pk}/'
        ),
    }
    request = urllib.request.Request(
        'https://api.xendit.co/v2/invoices',
        data=json.dumps(payload).encode(),
        headers={
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    credentials = base64.b64encode(
        f'{settings.XENDIT_API_KEY}:'.encode()
    ).decode()
    request.add_header('Authorization', f'Basic {credentials}')
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise IntegrationError(
            f'Xendit rejected the invoice request (HTTP {exc.code}).'
        ) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise IntegrationError('Xendit could not be reached.') from exc
    if not result.get('invoice_url') or not result.get('id'):
        raise IntegrationError('Xendit did not return a checkout URL.')
    return result
