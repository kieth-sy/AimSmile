import json
import logging
import smtplib

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.mail import get_connection, send_mail
from django.core.mail.backends.console import EmailBackend as ConsoleEmailBackend
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from accounts.decorators import admin_required
from accounts.forms import StaffUserForm
from accounts.audit import log_activity
from accounts.models import ActivityLog, SignupDuplicateReport
from clinic import analytics
from clinic.models import (Appointment, Invoice, Prescription, Service,
                           TreatmentRecord)
from pages.forms import ContactReplyForm
from pages.models import ContactMessage

from .registry import RESOURCES, get_resource

User = get_user_model()
logger = logging.getLogger(__name__)


def _j(obj):
    return json.dumps(obj)


@admin_required
def dashboard(request):
    kpis = analytics.admin_kpis()
    context = {
        'kpis': kpis,
        'revenue_json': _j(analytics.revenue_by_month()),
        'status_json': _j(analytics.appointments_by_status()),
        'treatment_json': _j(analytics.treatment_distribution()),
        'growth_json': _j(analytics.patient_growth()),
        'appts_month_json': _j(analytics.appointments_per_month()),
        'recent_appointments': Appointment.objects.select_related(
            'patient', 'dentist')[:6],
        'recent_invoices': Invoice.objects.select_related('patient')[:6],
        'resources': RESOURCES.values(),
    }
    return render(request, 'manage_panel/dashboard.html', context)


@admin_required
def contact_message_list(request):
    contact_messages = ContactMessage.objects.all()
    return render(request, 'manage_panel/contact_messages.html', {
        'contact_messages': contact_messages,
        'pending_count': contact_messages.filter(
            status=ContactMessage.Status.PENDING).count(),
    })


@admin_required
def contact_message_detail(request, pk):
    contact_message = get_object_or_404(ContactMessage, pk=pk)
    return render(request, 'manage_panel/contact_message_detail.html', {
        'contact_message': contact_message,
        'form': ContactReplyForm(),
    })


@admin_required
@require_POST
def contact_message_reply(request, pk):
    contact_message = get_object_or_404(ContactMessage, pk=pk)
    form = ContactReplyForm(request.POST)
    if not form.is_valid():
        return render(request, 'manage_panel/contact_message_detail.html', {
            'contact_message': contact_message,
            'form': form,
        })

    connection = get_connection()
    if isinstance(connection, ConsoleEmailBackend):
        messages.error(
            request,
            'Email is not configured for delivery. Set up SMTP before sending replies.',
        )
        return redirect('manage_panel:contact_message_detail', pk=pk)

    subject = ' '.join(contact_message.subject.split())
    if not subject:
        subject = 'Your message to AimSmile Dental Clinic'
    if not subject.lower().startswith('re:'):
        subject = f'Re: {subject}'

    try:
        sent_count = send_mail(
            subject=subject,
            message=form.cleaned_data['reply'],
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[contact_message.email],
            fail_silently=False,
            connection=connection,
        )
    except (OSError, smtplib.SMTPException):
        logger.exception(
            'Failed to send contact-message reply for message %s',
            contact_message.pk,
        )
        messages.error(
            request,
            'The reply could not be sent. Check the email configuration and try again.',
        )
        return redirect('manage_panel:contact_message_detail', pk=pk)

    if sent_count != 1:
        messages.error(
            request,
            'The email service did not accept the reply. Please try again.',
        )
        return redirect('manage_panel:contact_message_detail', pk=pk)

    contact_message.reply = form.cleaned_data['reply']
    contact_message.status = ContactMessage.Status.REPLIED
    contact_message.replied_by = request.user
    contact_message.replied_at = timezone.now()
    contact_message.save(update_fields=[
        'reply', 'status', 'replied_by', 'replied_at',
    ])
    messages.success(request, f'Reply sent to {contact_message.email}.')
    return redirect('manage_panel:contact_message_detail', pk=pk)


# --------------------------------------------------------------------------
# Generic CRUD
# --------------------------------------------------------------------------
@admin_required
def resource_list(request, slug):
    resource = get_resource(slug)
    if not resource:
        raise Http404
    objects = resource.model.objects.all()
    search = request.GET.get('q', '')
    if search and resource.search_fields:
        q = Q()
        for f in resource.search_fields:
            q |= Q(**{f'{f}__icontains': search})
        objects = objects.filter(q)
    rows = []
    for obj in objects:
        rows.append({
            'obj': obj,
            'cells': [resource.value(obj, attr) for _, attr in resource.list_display],
        })
    return render(request, 'manage_panel/resource_list.html', {
        'resource': resource, 'rows': rows, 'search': search,
    })


@admin_required
def resource_create(request, slug):
    resource = get_resource(slug)
    if not resource:
        raise Http404
    FormClass = resource.form_class()
    if request.method == 'POST':
        form = FormClass(request.POST, request.FILES)
        if form.is_valid():
            obj = form.save()
            log_activity(
                request,
                ActivityLog.Action.RESOURCE_CREATED,
                f'{resource.label} created.',
                target=obj,
            )
            messages.success(request, f'{resource.label} created.')
            return redirect('manage_panel:resource_list', slug=slug)
    else:
        form = FormClass()
    return render(request, 'manage_panel/resource_form.html',
                  {'resource': resource, 'form': form,
                   'title': f'New {resource.label}'})


@admin_required
def resource_update(request, slug, pk):
    resource = get_resource(slug)
    if not resource:
        raise Http404
    obj = get_object_or_404(resource.model, pk=pk)
    FormClass = resource.form_class()
    if request.method == 'POST':
        form = FormClass(request.POST, request.FILES, instance=obj)
        if form.is_valid():
            obj = form.save()
            log_activity(
                request,
                ActivityLog.Action.RESOURCE_UPDATED,
                f'{resource.label} updated.',
                target=obj,
            )
            messages.success(request, f'{resource.label} updated.')
            return redirect('manage_panel:resource_list', slug=slug)
    else:
        form = FormClass(instance=obj)
    return render(request, 'manage_panel/resource_form.html',
                  {'resource': resource, 'form': form,
                   'title': f'Edit {resource.label}'})


@admin_required
def resource_delete(request, slug, pk):
    resource = get_resource(slug)
    if not resource:
        raise Http404
    obj = get_object_or_404(resource.model, pk=pk)
    if request.method == 'POST':
        obj.delete()
        log_activity(
            request,
            ActivityLog.Action.RESOURCE_DELETED,
            f'{resource.label} deleted.',
            target=obj,
        )
        messages.success(request, f'{resource.label} deleted.')
        return redirect('manage_panel:resource_list', slug=slug)
    return render(request, 'manage_panel/confirm_delete.html',
                  {'resource': resource, 'object': obj})


# --------------------------------------------------------------------------
# User management (dedicated, supports roles + password)
# --------------------------------------------------------------------------
@admin_required
def user_list(request):
    role = request.GET.get('role', '')
    search = request.GET.get('q', '')
    users = User.objects.select_related('identity_verification').order_by(
        '-date_joined'
    )
    pending_users = users.filter(signup_status='pending', is_active=False)
    duplicate_reports = SignupDuplicateReport.objects.filter(
        reviewed_at=None
    ).select_related('matched_user')
    if role:
        users = users.filter(role=role)
    if search:
        users = users.filter(Q(username__icontains=search) |
                             Q(first_name__icontains=search) |
                             Q(last_name__icontains=search) |
                             Q(email__icontains=search))
    return render(request, 'manage_panel/user_list.html', {
        'users': users, 'pending_users': pending_users,
        'duplicate_reports': duplicate_reports,
        'current_role': role, 'search': search,
    })


@admin_required
@require_POST
def pending_user_review(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    action = request.POST.get('action')
    if action not in ('approve', 'reject'):
        messages.error(request, 'Choose approve or reject.')
        return redirect('manage_panel:user_list')
    if action == 'approve':
        user_obj.is_active = True
        user_obj.signup_status = 'approved'
        user_obj.save(update_fields=['is_active', 'signup_status'])
        log_activity(
            request,
            ActivityLog.Action.USER_APPROVED,
            'User account approved.',
            target=user_obj,
            target_label=user_obj.get_username(),
        )
        messages.success(request, f'{user_obj.display_name} was approved and can now sign in.')
    else:
        user_obj.signup_status = 'rejected'
        user_obj.is_active = False
        user_obj.save(update_fields=['signup_status', 'is_active'])
        log_activity(
            request,
            ActivityLog.Action.USER_REJECTED,
            'User account rejected.',
            target=user_obj,
            target_label=user_obj.get_username(),
        )
        messages.warning(request, f'{user_obj.display_name} registration was rejected.')
    return redirect('manage_panel:user_list')


@admin_required
@require_POST
def duplicate_signup_review(request, pk):
    report = get_object_or_404(SignupDuplicateReport, pk=pk)
    report.reviewed_at = timezone.now()
    report.reviewed_by = request.user
    report.save(update_fields=['reviewed_at', 'reviewed_by'])
    log_activity(
        request,
        ActivityLog.Action.DUPLICATE_REVIEWED,
        'Duplicate-signup report reviewed.',
        target=report,
    )
    messages.success(request, 'Duplicate-signup report marked as reviewed.')
    return redirect('manage_panel:user_list')


@admin_required
def user_create(request):
    if request.method == 'POST':
        form = StaffUserForm(
            request.POST, request.FILES, require_email=True,
            require_password=True)
        if form.is_valid():
            user_obj = form.save()
            log_activity(
                request,
                ActivityLog.Action.USER_CREATED,
                'User account created.',
                target=user_obj,
                target_label=user_obj.get_username(),
            )
            messages.success(request, 'Account created successfully.')
            return redirect('manage_panel:user_list')
    else:
        form = StaffUserForm(require_email=True, require_password=True)
    return render(request, 'manage_panel/user_form.html',
                  {'form': form, 'title': 'Add User'})


@admin_required
def user_update(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        form = StaffUserForm(request.POST, request.FILES, instance=user_obj)
        if form.is_valid():
            user_obj = form.save()
            log_activity(
                request,
                ActivityLog.Action.USER_UPDATED,
                'User account updated.',
                target=user_obj,
                target_label=user_obj.get_username(),
            )
            messages.success(request, f'User {user_obj.username} updated.')
            return redirect('manage_panel:user_list')
    else:
        form = StaffUserForm(instance=user_obj)
    return render(request, 'manage_panel/user_form.html',
                  {'form': form, 'title': f'Edit {user_obj.username}'})


@admin_required
def user_delete(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    if user_obj == request.user:
        messages.error(request, "You can't delete your own account.")
        return redirect('manage_panel:user_list')
    if request.method == 'POST':
        username = user_obj.get_username()
        user_obj.delete()
        log_activity(
            request,
            ActivityLog.Action.USER_DELETED,
            'User account deleted.',
            target=user_obj,
            target_label=username,
        )
        messages.success(request, 'User deleted.')
        return redirect('manage_panel:user_list')
    return render(request, 'manage_panel/confirm_delete_user.html', {'object': user_obj})


@admin_required
def reports(request):
    """Extra admin analytics page."""
    from django.db.models import Count
    top_services = list(
        TreatmentRecord.objects.values('service__name')
        .annotate(count=Count('id')).order_by('-count')[:8]
    )
    top_services_basis = 'recorded treatments'
    if not top_services:
        top_services = list(
            Appointment.objects.filter(service__isnull=False)
            .values('service__name')
            .annotate(count=Count('id')).order_by('-count')[:8]
        )
        top_services_basis = 'appointment bookings'
    top_services_data = {
        'labels': [service['service__name'] or 'Other' for service in top_services],
        'data': [service['count'] for service in top_services],
    }
    context = {
        'revenue_json': _j(analytics.revenue_by_month()),
        'growth_json': _j(analytics.patient_growth()),
        'status_json': _j(analytics.appointments_by_status()),
        'treatment_json': _j(analytics.treatment_distribution()),
        'appts_month_json': _j(analytics.appointments_per_month()),
        'kpis': analytics.admin_kpis(),
        'top_services': top_services,
        'top_services_data': top_services_data,
        'top_services_basis': top_services_basis,
        'services': Service.objects.all(),
        'total_prescriptions': Prescription.objects.count(),
    }
    return render(request, 'manage_panel/reports.html', context)


@admin_required
def activity_log_list(request):
    logs = ActivityLog.objects.select_related('actor')
    selected_action = request.GET.get('action', '')
    action_values = {value for value, _ in ActivityLog.Action.choices}
    if selected_action in action_values:
        logs = logs.filter(action=selected_action)
    else:
        selected_action = ''

    search = request.GET.get('q', '').strip()[:100]
    if search:
        logs = logs.filter(
            Q(actor_username__icontains=search)
            | Q(summary__icontains=search)
            | Q(target_type__icontains=search)
            | Q(target_id__icontains=search)
            | Q(target_label__icontains=search)
            | Q(ip_address__icontains=search)
        )

    page_obj = Paginator(logs, 50).get_page(request.GET.get('page'))
    return render(request, 'manage_panel/activity_log_list.html', {
        'page_obj': page_obj,
        'search': search,
        'selected_action': selected_action,
        'action_choices': ActivityLog.Action.choices,
    })
