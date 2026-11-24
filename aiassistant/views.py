import hashlib

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from clinic.models import Service

from . import engine
MAX_CHAT_MESSAGE_LENGTH = 2000
CHAT_REQUESTS_PER_MINUTE = 10


def symptom_checker(request):
    result = None
    symptoms = ''
    if request.method == 'POST':
        symptoms = request.POST.get('symptoms', '')
        result = engine.symptom_triage(symptoms)
    return render(request, 'aiassistant/symptom_checker.html',
                  {'result': result, 'symptoms': symptoms})


def chatbot(request):
    return render(request, 'aiassistant/chatbot.html')


@require_POST
def chatbot_api(request):
    message = request.POST.get('message', '').strip()
    if not message or len(message) > MAX_CHAT_MESSAGE_LENGTH:
        return JsonResponse(
            {'error': 'Enter a message up to 2,000 characters.'},
            status=400,
        )

    if not _allow_chat_request(request):
        return JsonResponse(
            {'error': 'Too many messages. Please wait a minute and try again.'},
            status=429,
        )

    return JsonResponse({'reply': engine.chatbot_response(message)})


def _allow_chat_request(request):
    remote_addr = request.META.get('REMOTE_ADDR') or 'unknown'
    address_hash = hashlib.sha256(remote_addr.encode('utf-8')).hexdigest()
    cache_key = f'aiassistant:chat-rate:{address_hash}'
    if cache.add(cache_key, 1, timeout=60):
        return True
    try:
        count = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=60)
        return True
    return count <= CHAT_REQUESTS_PER_MINUTE


def cost_estimator(request):
    result = None
    services = Service.objects.filter(is_active=True)
    selected = None
    if request.method == 'POST':
        try:
            service = Service.objects.get(pk=request.POST.get('service'))
        except (Service.DoesNotExist, ValueError):
            service = None
        selected = service.id if service else None
        base = service.price if service else request.POST.get('base_price', 0)
        complexity = request.POST.get('complexity', 'simple')
        teeth = request.POST.get('teeth', 1)
        insurance = request.POST.get('insurance') == 'on'
        result = engine.estimate_cost(base, complexity, teeth, insurance)
        result['service'] = service
    return render(request, 'aiassistant/cost_estimator.html',
                  {'result': result, 'services': services, 'selected': selected})


@login_required
def hub(request):
    """Landing page linking all AI tools."""
    return render(request, 'aiassistant/hub.html')
