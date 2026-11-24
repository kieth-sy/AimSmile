from django.shortcuts import redirect, render

from clinic.models import Review, Service

from .forms import ContactMessageForm


def home(request):
    return render(request, 'pages/home.html', {
        'services': Service.objects.filter(is_active=True)[:6],
        'reviews': Review.objects.filter(is_public=True).select_related('patient')[:6],
    })


def about(request):
    return render(request, 'pages/about.html')


def services(request):
    return render(request, 'pages/services.html', {
        'services': Service.objects.filter(is_active=True),
    })


def contact(request):
    if request.method == 'POST':
        form = ContactMessageForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('pages:contact_sent')
    else:
        form = ContactMessageForm()
    return render(request, 'pages/contact.html', {'form': form})


def contact_sent(request):
    return render(request, 'pages/contact.html', {
        'form': ContactMessageForm(),
        'sent': True,
    })
