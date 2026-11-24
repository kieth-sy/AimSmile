import hashlib
import hmac
import io
import json
import re
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dateutil import parser as date_parser
from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError


class IdentityVerificationError(Exception):
    """An uploaded image cannot be used for identity verification."""


class IdentityVerificationSetupError(Exception):
    """A local OCR or face-recognition dependency is not configured."""


def image_digest(image_bytes):
    return hashlib.sha256(image_bytes).hexdigest()


def _read_image(image_bytes):
    if not image_bytes or len(image_bytes) > 5 * 1024 * 1024:
        raise IdentityVerificationError(
            'Upload a JPEG or PNG image that is 5 MB or smaller.'
        )
    try:
        image = Image.open(io.BytesIO(image_bytes))
        if image.format not in {'JPEG', 'PNG'}:
            raise IdentityVerificationError('Upload a JPEG or PNG image.')
        image = ImageOps.exif_transpose(image)
        if image.width < 300 or image.height < 180:
            raise IdentityVerificationError(
                'The image is too small to read. Upload a clearer photo.'
            )
        if image.width * image.height > 40_000_000:
            raise IdentityVerificationError(
                'The image dimensions are too large. Resize it and try again.'
            )
        return image.convert('RGB')
    except (UnidentifiedImageError, OSError) as exc:
        raise IdentityVerificationError(
            'The uploaded file is not a readable image. Please try another photo.'
        ) from exc


def _value_after_labels(lines, labels):
    label_pattern = '|'.join(labels)
    pattern = re.compile(
        rf'^\s*(?:{label_pattern})(?:\s*/\s*(?:{label_pattern}))?'
        rf'\s*(?:[:#-]\s*)?(.*)$',
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        value = match.group(1).strip(' :#-.')
        if value:
            return value
        if index + 1 < len(lines):
            next_line = lines[index + 1].strip(' :#-.')
            if next_line and not re.fullmatch(r'[\W_]+', next_line):
                return next_line
    return ''


def _parse_date(value, allow_future=False):
    if not value:
        return ''
    try:
        parsed = date_parser.parse(value, dayfirst=True, fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return ''
    if not allow_future and parsed > datetime.now().date():
        return ''
    return parsed.isoformat()


def _date_after_labels(lines, labels, allow_future=False):
    label_pattern = '|'.join(labels)
    pattern = re.compile(
        rf'\b(?:{label_pattern})\b\s*(?:[:#-]\s*)?(.*)$',
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        match = pattern.search(line)
        if not match:
            continue
        parsed = _parse_date(match.group(1), allow_future=allow_future)
        if parsed:
            return parsed
        if index + 1 < len(lines):
            parsed = _parse_date(lines[index + 1], allow_future=allow_future)
            if parsed:
                return parsed
    return ''


def detect_id_type(ocr_text):
    normalized_text = re.sub(r'\s+', ' ', ocr_text.upper())
    markers = {
        'ph_national_id': (
            r'\bPHILSYS\b',
            r'\bPHILIPPINE IDENTIFICATION (?:SYSTEM|CARD)\b',
            r'\bPAMBANSANG PAGKAKAKILANLAN\b',
        ),
        'drivers_license': (
            r"\bDRIVER(?:'S)? LICEN[CS]E\b",
            r'\bLAND TRANSPORTATION OFFICE\b',
        ),
        'passport': (
            r'\bPASSPORT\b',
            r'\bPASAPORTE\b',
        ),
        'umid': (
            r'\bUMID\b',
            r'\bUNIFIED MULTI[\s-]?PURPOSE ID\b',
        ),
    }
    matches = [
        id_type
        for id_type, patterns in markers.items()
        if any(re.search(pattern, normalized_text) for pattern in patterns)
    ]
    return matches[0] if len(matches) == 1 else None


def parse_id_text(ocr_text, id_type):
    lines = [re.sub(r'\s+', ' ', line).strip() for line in ocr_text.splitlines()]
    lines = [line for line in lines if line]

    given_names = _value_after_labels(
        lines,
        [
            r'GIVEN\s+NAMES?',
            r'FIRST\s+NAMES?',
            r'MGA\s+PANGALAN',
        ],
    )
    surname = _value_after_labels(
        lines,
        [r'SURNAME', r'LAST\s+NAME', r'APELYIDO'],
    )
    full_name = _value_after_labels(
        lines,
        [r'FULL\s+NAME', r'NAME', r'PANGALAN'],
    )
    if not full_name:
        full_name = ' '.join(part for part in (given_names, surname) if part)
    full_name = re.sub(r'\s+', ' ', full_name).strip(' :-')
    if not given_names and not surname and full_name:
        name_parts = full_name.rsplit(' ', 1)
        if len(name_parts) == 2:
            given_names, surname = name_parts
        else:
            given_names = full_name

    birth_date = _date_after_labels(
        lines,
        [
            r'DATE\s+OF\s+BIRTH',
            r'BIRTH\s+DATE',
            r'D\.?\s*O\.?\s*B\.?',
            r'PETSA\s+NG\s+KAPANGANAKAN',
        ],
    )
    expiration_date = _date_after_labels(
        lines,
        [r'EXPIRY\s+DATE', r'EXPIRATION\s+DATE', r'VALID\s+UNTIL'],
        allow_future=True,
    )
    address = _value_after_labels(
        lines,
        [r'ADDRESS', r'RESIDENCE', r'TIRAHAN'],
    )
    if address.strip().upper() in {
        'PHL',
        'PHILIPPINES',
        'PILIPINAS',
        'FILIPINO',
        'FILIPINA',
        'N/A',
        'UNKNOWN',
    }:
        address = ''

    id_labels = {
        'ph_national_id': [r'PCN', r'PHILSYS\s+(?:NO|NUMBER)', r'ID\s+(?:NO|NUMBER)'],
        'drivers_license': [
            r'DRIVER(?:\'S)?\s+LICENSE\s+(?:NO|NUMBER)',
            r'LICENSE\s+(?:NO|NUMBER)',
            r'ID\s+(?:NO|NUMBER)',
        ],
        'passport': [
            r'PASSPORT\s+(?:NO|NUMBER)',
            r'DOCUMENT\s+(?:NO|NUMBER)',
        ],
        'umid': [r'CRN', r'UMID\s+(?:NO|NUMBER)', r'ID\s+(?:NO|NUMBER)'],
    }
    id_number = _value_after_labels(lines, id_labels.get(id_type, []))
    if not id_number and id_type == 'ph_national_id':
        candidate_pattern = re.compile(
            r'(?<![A-Z0-9])(?:\d[\s.-]*){11,15}\d(?![A-Z0-9])',
            re.IGNORECASE,
        )
        for candidate in candidate_pattern.findall(' '.join(lines)):
            digits = re.sub(r'\D', '', candidate)
            if len(digits) in {12, 16}:
                id_number = candidate.strip(' :#.-')
                break

    missing_fields = [
        label for label, value in (
            ('Full name', full_name),
            ('Date of birth', birth_date),
            ('ID number', id_number),
        ) if not value
    ]
    return {
        'full_name': full_name,
        'given_names': given_names,
        'surname': surname,
        'birth_date': birth_date,
        'address': address,
        'id_number': id_number,
        'expiration_date': expiration_date,
        'missing_fields': missing_fields,
    }


def scan_id_image(image_bytes, id_type=''):
    image = _read_image(image_bytes)
    try:
        import pytesseract
    except ImportError as exc:
        raise IdentityVerificationSetupError(
            'Install the project requirements to enable local ID scanning.'
        ) from exc

    if settings.IDENTITY_TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = settings.IDENTITY_TESSERACT_CMD
    try:
        pytesseract.get_tesseract_version()
        grayscale = ImageOps.autocontrast(image.convert('L'))
        ocr_inputs = [(grayscale, '--psm 6'), (grayscale, '--psm 11')]
        if image.width * image.height <= 6_000_000:
            enlarged = ImageOps.autocontrast(
                grayscale.resize((image.width * 2, image.height * 2))
            )
            ocr_inputs.extend((
                (enlarged, '--psm 6'),
                (enlarged, '--psm 11'),
                (enlarged.point(lambda pixel: 255 if pixel > 165 else 0), '--psm 11'),
            ))
        else:
            ocr_inputs.append(
                (grayscale.point(lambda pixel: 255 if pixel > 165 else 0), '--psm 11')
            )
        ocr_texts = []
        detected_types = []
        for ocr_image, config in ocr_inputs:
            text = pytesseract.image_to_string(
                ocr_image,
                lang='eng',
                config=config,
            )
            ocr_texts.append(text)
            detected_type = detect_id_type(text)
            if detected_type:
                detected_types.append(detected_type)
            candidate_type = detected_type or id_type
            if candidate_type:
                candidate_fields = parse_id_text(text, candidate_type)
                if (
                    not candidate_fields['missing_fields']
                    and candidate_fields['given_names']
                    and candidate_fields['surname']
                ):
                    break
    except pytesseract.TesseractNotFoundError as exc:
        raise IdentityVerificationSetupError(
            'Tesseract OCR is not installed or IDENTITY_TESSERACT_CMD is incorrect.'
        ) from exc
    except pytesseract.TesseractError as exc:
        raise IdentityVerificationSetupError(
            'Tesseract OCR could not process the image. Check the English language data.'
        ) from exc

    type_counts = {
        candidate: detected_types.count(candidate)
        for candidate in set(detected_types)
    }
    if type_counts:
        highest_count = max(type_counts.values())
        detected_winners = [
            candidate for candidate, count in type_counts.items()
            if count == highest_count
        ]
        if len(detected_winners) == 1:
            id_type = detected_winners[0]
        elif id_type not in detected_winners:
            raise IdentityVerificationError(
                'The ID type could not be recognized confidently. Select the ID type manually and scan again.'
            )
    if id_type not in {
        'ph_national_id',
        'drivers_license',
        'passport',
        'umid',
    }:
        raise IdentityVerificationError(
            'The ID type could not be recognized. Select the ID type manually and scan again.'
        )

    results = [parse_id_text(text, id_type) for text in ocr_texts]
    fields = min(results, key=lambda result: len(result['missing_fields']))
    fields = fields.copy()
    for result in results:
        for field in (
            'full_name',
            'given_names',
            'surname',
            'birth_date',
            'address',
            'id_number',
            'expiration_date',
        ):
            if not fields[field] and result[field]:
                fields[field] = result[field]
    if fields['given_names'] or fields['surname']:
        fields['full_name'] = ' '.join(
            part for part in (fields['given_names'], fields['surname']) if part
        )
    fields['missing_fields'] = [
        label for label, value in (
            ('Full name', fields['full_name']),
            ('Date of birth', fields['birth_date']),
            ('ID number', fields['id_number']),
        ) if not value
    ]
    fields['id_type'] = id_type
    return fields


def _opencv_components():
    try:
        import cv2
        import numpy
    except ImportError as exc:
        raise IdentityVerificationSetupError(
            'OpenCV and NumPy are unavailable to this Python environment. Install '
            'the project requirements and confirm local security policy allows OpenCV.'
        ) from exc

    model_dir = Path(settings.IDENTITY_MODEL_DIR)
    detector_model = model_dir / 'face_detection_yunet_2023mar.onnx'
    recognizer_model = model_dir / 'face_recognition_sface_2021dec.onnx'
    if not detector_model.is_file() or not recognizer_model.is_file():
        raise IdentityVerificationSetupError(
            'Local face models are missing. Follow the Identity Verification setup '
            'in README.md and place both OpenCV Zoo models in '
            f'{model_dir}.'
        )

    try:
        detector = cv2.FaceDetectorYN.create(
            str(detector_model), '', (320, 320), 0.8, 0.3, 5000
        )
        recognizer = cv2.FaceRecognizerSF.create(str(recognizer_model), '')
    except (cv2.error, AttributeError) as exc:
        raise IdentityVerificationSetupError(
            'The installed OpenCV build cannot load the local face models.'
        ) from exc
    return cv2, numpy, detector, recognizer


def _decode_cv_image(image_bytes, cv2, numpy):
    validated = _read_image(image_bytes)
    buffer = io.BytesIO()
    validated.save(buffer, format='JPEG')
    image = cv2.imdecode(
        numpy.frombuffer(buffer.getvalue(), dtype=numpy.uint8),
        cv2.IMREAD_COLOR,
    )
    if image is None:
        raise IdentityVerificationError('The image could not be decoded.')
    return image


def _detect_one_face(image, detector):
    detector.setInputSize((image.shape[1], image.shape[0]))
    _, faces = detector.detect(image)
    if faces is None or len(faces) != 1:
        raise IdentityVerificationError(
            'Each image must show exactly one clear face. Retake the photo with '
            'good lighting and the full face visible.'
        )
    return faces[0]


def compare_face_to_id(id_image_bytes, selfie_image_bytes, pose_image_bytes):
    matched, similarity, _, _, _ = compare_face_to_id_and_templates(
        id_image_bytes,
        selfie_image_bytes,
        pose_image_bytes,
    )
    return matched, similarity


def compare_face_to_id_and_templates(
    id_image_bytes,
    selfie_image_bytes,
    pose_image_bytes,
    existing_face_templates=(),
):
    cv2, numpy, detector, recognizer = _opencv_components()
    id_image = _decode_cv_image(id_image_bytes, cv2, numpy)
    selfie_image = _decode_cv_image(selfie_image_bytes, cv2, numpy)
    id_face = _detect_one_face(id_image, detector)
    selfie_face = _detect_one_face(selfie_image, detector)

    for pose_image_bytes_item in pose_image_bytes:
        pose_image = _decode_cv_image(pose_image_bytes_item, cv2, numpy)
        _detect_one_face(pose_image, detector)

    id_aligned = recognizer.alignCrop(id_image, id_face)
    selfie_aligned = recognizer.alignCrop(selfie_image, selfie_face)
    id_features = numpy.asarray(
        recognizer.feature(id_aligned),
        dtype=numpy.float32,
    ).reshape(1, -1)
    selfie_features = numpy.asarray(
        recognizer.feature(selfie_aligned),
        dtype=numpy.float32,
    ).reshape(1, -1)
    similarity = float(recognizer.match(
        id_features,
        selfie_features,
        cv2.FaceRecognizerSF_FR_COSINE,
    ))
    face_template = numpy.ascontiguousarray(
        selfie_features,
        dtype=numpy.dtype('<f4'),
    ).tobytes()

    best_index = None
    best_similarity = -1.0
    for index, encrypted_template in enumerate(existing_face_templates):
        stored_bytes = decrypt_face_template(encrypted_template)
        if len(stored_bytes) % numpy.dtype('<f4').itemsize:
            raise IdentityVerificationSetupError(
                'A stored face template has an invalid format.'
            )
        stored_features = numpy.frombuffer(
            stored_bytes,
            dtype=numpy.dtype('<f4'),
        )
        if stored_features.size != selfie_features.size:
            raise IdentityVerificationSetupError(
                'A stored face template uses an incompatible model format.'
            )
        score = float(recognizer.match(
            selfie_features,
            stored_features.reshape(1, -1),
            cv2.FaceRecognizerSF_FR_COSINE,
        ))
        if score > best_similarity:
            best_index = index
            best_similarity = score

    return (
        similarity >= settings.IDENTITY_FACE_MATCH_THRESHOLD,
        similarity,
        face_template,
        best_index,
        best_similarity,
    )


def identity_id_number_digest(id_type, id_number):
    key = settings.IDENTITY_VERIFICATION_ENCRYPTION_KEY
    _identity_state_cipher()
    normalized_id = re.sub(r'[^A-Z0-9]', '', id_number.upper())
    if not normalized_id:
        raise IdentityVerificationError('The ID number could not be normalized.')
    message = f'identity-id-number-v1:{id_type}:{normalized_id}'.encode('utf-8')
    return hmac.new(key.encode('ascii'), message, hashlib.sha256).hexdigest()


def encrypt_face_template(template):
    if not template or len(template) % 4:
        raise IdentityVerificationError('The face template has an invalid format.')
    return _identity_state_cipher().encrypt(template)


def decrypt_face_template(encrypted_template):
    try:
        return _identity_state_cipher().decrypt(bytes(encrypted_template))
    except (InvalidToken, TypeError, ValueError) as exc:
        raise IdentityVerificationSetupError(
            'A stored face template cannot be decrypted. Check the identity encryption key.'
        ) from exc


def encrypt_identity_state(state):
    return _identity_state_cipher().encrypt(
        json.dumps(state).encode('utf-8')
    ).decode('ascii')


def _identity_state_cipher():
    key = settings.IDENTITY_VERIFICATION_ENCRYPTION_KEY
    if not key:
        raise IdentityVerificationSetupError(
            'Set IDENTITY_VERIFICATION_ENCRYPTION_KEY in the private .env file.'
        )
    try:
        return Fernet(key.encode('ascii'))
    except (ValueError, UnicodeEncodeError) as exc:
        raise IdentityVerificationSetupError(
            'IDENTITY_VERIFICATION_ENCRYPTION_KEY must be a valid Fernet key.'
        ) from exc


def decrypt_identity_state(encrypted_state):
    try:
        payload = _identity_state_cipher().decrypt(encrypted_state.encode('ascii'))
        return json.loads(payload.decode('utf-8'))
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise IdentityVerificationError(
            'Your verification session expired. Please scan your ID again.'
        ) from exc
