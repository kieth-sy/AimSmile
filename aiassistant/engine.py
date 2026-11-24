"""Local rule-based helpers for symptom triage, no-show risk, and cost estimates."""
from datetime import date
import re


# ---------------------------------------------------------------------------
# 1. Symptom triage
# ---------------------------------------------------------------------------
SYMPTOM_RULES = [
    {
        'keywords': ['bleeding', 'gum', 'gums', 'swollen', 'swelling'],
        'condition': 'Gingivitis / Gum disease',
        'urgency': 'Medium',
        'service': 'Scaling & Cleaning',
        'advice': 'Rinse with warm salt water and book a cleaning. Avoid hard brushing.',
    },
    {
        'keywords': ['severe', 'unbearable', 'sharp pain', 'throbbing', 'abscess', 'pus'],
        'condition': 'Possible dental abscess / infection',
        'urgency': 'High',
        'service': 'Emergency Consultation',
        'advice': 'This may need urgent care. Please book an emergency appointment today.',
    },
    {
        'keywords': ['cavity', 'hole', 'decay', 'black spot', 'sensitive', 'sensitivity', 'cold', 'hot', 'sweet'],
        'condition': 'Tooth decay / sensitivity',
        'urgency': 'Medium',
        'service': 'Cavity Filling',
        'advice': 'Use a desensitising toothpaste and schedule a filling to stop further decay.',
    },
    {
        'keywords': ['wisdom', 'back tooth', 'jaw pain', 'erupting'],
        'condition': 'Wisdom tooth trouble',
        'urgency': 'Medium',
        'service': 'Tooth Extraction',
        'advice': 'An X-ray will confirm alignment. Extraction may be advised if impacted.',
    },
    {
        'keywords': ['broken', 'chipped', 'cracked', 'knocked', 'fell', 'trauma', 'accident'],
        'condition': 'Fractured / broken tooth',
        'urgency': 'High',
        'service': 'Emergency Consultation',
        'advice': 'Keep any fragments in milk and see a dentist quickly to save the tooth.',
    },
    {
        'keywords': ['yellow', 'stain', 'stained', 'white', 'whiten', 'brighten'],
        'condition': 'Cosmetic discolouration',
        'urgency': 'Low',
        'service': 'Teeth Whitening',
        'advice': 'A professional whitening session is safe and effective for stains.',
    },
    {
        'keywords': ['crooked', 'align', 'braces', 'gap', 'spacing'],
        'condition': 'Alignment / orthodontic concern',
        'urgency': 'Low',
        'service': 'Orthodontic Consultation',
        'advice': 'Braces or aligners can help. Book an orthodontic assessment.',
    },
    {
        'keywords': ['bad breath', 'smell', 'halitosis'],
        'condition': 'Halitosis (bad breath)',
        'urgency': 'Low',
        'service': 'Scaling & Cleaning',
        'advice': 'Often caused by plaque. A cleaning plus tongue hygiene usually helps.',
    },
]

URGENCY_SCORE = {'Low': 25, 'Medium': 60, 'High': 90}


def symptom_triage(text):
    text_l = (text or '').lower()
    matches = []
    for rule in SYMPTOM_RULES:
        hits = sum(1 for kw in rule['keywords'] if kw in text_l)
        if hits:
            matches.append((hits, rule))
    if not matches:
        return {
            'matched': False,
            'condition': 'General check-up recommended',
            'urgency': 'Low',
            'urgency_score': 20,
            'service': 'General Consultation',
            'advice': 'We could not pinpoint a specific issue. A routine check-up is the safest next step.',
            'alternatives': [],
        }
    matches.sort(key=lambda x: x[0], reverse=True)
    best = matches[0][1]
    alternatives = [m[1]['condition'] for m in matches[1:3]]
    return {
        'matched': True,
        'condition': best['condition'],
        'urgency': best['urgency'],
        'urgency_score': URGENCY_SCORE[best['urgency']],
        'service': best['service'],
        'advice': best['advice'],
        'alternatives': alternatives,
    }


def chatbot_response(message):
    """Return a free, local FAQ reply without sending chat to an external API."""
    text = re.sub(r'[^a-z0-9\s]', ' ', (message or '').lower())
    text = ' '.join(text.split())
    padded_text = f' {text} '

    def contains_any(*phrases):
        return any(f' {phrase} ' in padded_text for phrase in phrases)

    if not text:
        return 'Mag-type ng tanong tungkol sa appointments, services, presyo, o payments.'

    if contains_any(
        'toothache', 'tooth pain', 'pain', 'hurt', 'swelling', 'swollen',
        'bleeding', 'sensitive', 'sensitivity', 'masakit', 'sakit',
        'namamaga', 'dumudugo', 'dugo',
    ) or 'sakit ng ngipin' in text or 'masakit ang ngipin' in text:
        return (
            'Hindi ako makakapag-diagnose. Para sa sakit o sintomas, magpa-check '
            'sa dentista. Kung may hirap huminga o lumunok, hindi mapigil na '
            'pagdurugo, o matinding pamamaga, humingi agad ng urgent medical care.'
        )

    if contains_any(
        'appointment', 'book', 'booking', 'schedule', 'iskedyul',
        'magpa appointment', 'magpa schedule',
    ):
        return (
            'Para mag-book, mag-sign in o gumawa ng patient account, pagkatapos '
            'piliin ang Book Appointment. Kung kailangan ng tulong, kontakin ang clinic.'
        )

    if contains_any(
        'price', 'prices', 'cost', 'fee', 'how much', 'presyo', 'magkano',
    ):
        return (
            'Nag-iiba ang presyo depende sa serbisyo. Tingnan ang Services page '
            'para sa kasalukuyang listed prices, o kontakin ang clinic para sa estimate.'
        )

    if contains_any(
        'service', 'services', 'treatment', 'procedure', 'serbisyo',
        'ano ang serbisyo', 'ano services',
    ):
        return (
            'Tingnan ang Services page para sa kasalukuyang listahan ng serbisyo '
            'at mga presyong inilagay ng clinic.'
        )

    if contains_any(
        'hours', 'hour', 'open', 'opening', 'close', 'closing', 'location',
        'address', 'where', 'saan', 'nasaan', 'oras',
    ):
        return (
            'Para sa kumpirmadong clinic hours, lokasyon, o contact details, '
            'tingnan ang Contact page o direktang kontakin ang clinic.'
        )

    if contains_any(
        'invoice', 'bill', 'payment', 'pay', 'receipt', 'resibo',
        'bayad', 'billing',
    ):
        return (
            'Makikita ng signed-in patient ang bills sa My Bills. Ang payment '
            'request ay pending hanggang ma-review at makumpirma ng clinic staff.'
        )

    if contains_any('hello', 'hi', 'hey', 'kumusta', 'kamusta', 'good morning'):
        return (
            'Hi! Ako si DentaBot. Kaya kong tumulong sa pangkalahatang tanong '
            'tungkol sa appointments, services, presyo, at payments. Ano ang kailangan mo?'
        )

    if contains_any('thanks', 'thank you', 'salamat', 'bye', 'goodbye'):
        return 'Walang anuman! Para sa personal na payo, kumonsulta sa dentista.'

    return (
        'Local FAQ chatbot ako, kaya limitado ang mga sagot ko. Maaari akong '
        'tumulong sa appointments, services, presyo, at payments. Para sa iba '
        'pang tanong, kontakin ang clinic.'
    )


# ---------------------------------------------------------------------------
# 2. No-show risk predictor
# ---------------------------------------------------------------------------
def no_show_risk(appointment):
    """Heuristic 0-100 risk score for an appointment being a no-show."""
    risk = 20
    patient = appointment.patient
    history = patient.appointments.all()
    past_no_shows = history.filter(status='no_show').count()
    past_total = history.count()
    if past_total:
        risk += int((past_no_shows / past_total) * 40)
    # Longer lead time -> slightly higher risk
    lead = (appointment.date - date.today()).days
    if lead > 14:
        risk += 15
    elif lead > 7:
        risk += 8
    # Early morning slots are missed more often
    if appointment.time and appointment.time.hour < 10:
        risk += 10
    # Missing phone number -> hard to remind
    if not patient.phone or patient.phone.strip('0') == '':
        risk += 10
    return max(5, min(risk, 95))


def risk_band(score):
    if score >= 66:
        return ('High', 'danger')
    if score >= 40:
        return ('Medium', 'warning')
    return ('Low', 'success')


# ---------------------------------------------------------------------------
# 3. Cost estimator
# ---------------------------------------------------------------------------
COMPLEXITY_MULTIPLIER = {'simple': 1.0, 'moderate': 1.4, 'complex': 1.9}


def estimate_cost(base_price, complexity='simple', teeth=1, insurance=False):
    base = float(base_price or 0)
    mult = COMPLEXITY_MULTIPLIER.get(complexity, 1.0)
    subtotal = base * mult * max(1, int(teeth))
    tax = subtotal * 0.05
    total = subtotal + tax
    covered = total * 0.5 if insurance else 0
    return {
        'base': round(base, 2),
        'multiplier': mult,
        'teeth': int(teeth),
        'subtotal': round(subtotal, 2),
        'tax': round(tax, 2),
        'insurance_covered': round(covered, 2),
        'total': round(total - covered, 2),
        'low': round((total - covered) * 0.9, 2),
        'high': round((total - covered) * 1.15, 2),
    }
