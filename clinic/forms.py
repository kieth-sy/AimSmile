from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.forms import inlineformset_factory
from django.utils import timezone

from .models import (Appointment, AppointmentStatus, InventoryItem, Invoice,
                     InvoiceItem, Payment, PaymentMethod, Prescription,
                     PrescriptionItem, Review, Service, ToothRecord,
                     TreatmentRecord)

User = get_user_model()

CTRL = 'form-control-app'


def _style(form):
    for _, field in form.fields.items():
        w = field.widget
        if isinstance(w, forms.CheckboxInput):
            w.attrs['class'] = (w.attrs.get('class', '') + ' form-check-input-app').strip()
        elif isinstance(w, forms.Select):
            w.attrs['class'] = (w.attrs.get('class', '') + f' {CTRL} select-app').strip()
        else:
            w.attrs['class'] = (w.attrs.get('class', '') + f' {CTRL}').strip()


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class AppointmentForm(StyledModelForm):
    class Meta:
        model = Appointment
        fields = ['patient', 'dentist', 'service', 'date', 'time', 'status', 'reason', 'notes']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'time': forms.TimeInput(attrs={'type': 'time'}),
            'reason': forms.TextInput(),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, patient_mode=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['patient'].queryset = User.objects.filter(role='patient')
        self.fields['dentist'].queryset = User.objects.filter(role='dentist')
        self.fields['service'].queryset = Service.objects.filter(is_active=True)
        if patient_mode:
            # patients book for themselves and cannot set status
            self.fields['dentist'].required = True
            self.fields.pop('patient')
            self.fields.pop('status')
            self.fields.pop('notes')


class PatientBookingForm(AppointmentForm):
    def __init__(self, *args, **kwargs):
        kwargs['patient_mode'] = True
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        dentist = cleaned_data.get('dentist')
        appointment_date = cleaned_data.get('date')
        appointment_time = cleaned_data.get('time')
        service = cleaned_data.get('service')
        if not all((dentist, appointment_date, appointment_time)):
            return cleaned_data

        start = appointment_time.hour * 60 + appointment_time.minute
        duration = service.duration_minutes if service else 30
        end = start + duration
        existing_appointments = Appointment.objects.filter(
            dentist=dentist,
            date=appointment_date,
            status__in=[
                AppointmentStatus.PENDING,
                AppointmentStatus.CONFIRMED,
            ],
        ).select_related('service')
        for existing in existing_appointments:
            existing_start = existing.time.hour * 60 + existing.time.minute
            existing_duration = (
                existing.service.duration_minutes if existing.service else 30
            )
            existing_end = existing_start + existing_duration
            if start < existing_end and end > existing_start:
                self.add_error(
                    'time',
                    'That time is unavailable for this dentist. Choose another time.',
                )
                break
        return cleaned_data


class ToothRecordForm(StyledModelForm):
    class Meta:
        model = ToothRecord
        fields = ['tooth_number', 'condition', 'note']


class TreatmentRecordForm(StyledModelForm):
    class Meta:
        model = TreatmentRecord
        fields = ['patient', 'dentist', 'service', 'tooth_number', 'description',
                  'cost', 'date', 'is_completed']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['patient'].queryset = User.objects.filter(role='patient')
        self.fields['dentist'].queryset = User.objects.filter(role='dentist')


class PrescriptionForm(StyledModelForm):
    class Meta:
        model = Prescription
        fields = ['patient', 'dentist', 'date', 'diagnosis', 'notes']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['patient'].queryset = User.objects.filter(role='patient')
        self.fields['dentist'].queryset = User.objects.filter(role='dentist')


PrescriptionItemFormSet = inlineformset_factory(
    Prescription, PrescriptionItem,
    fields=['medicine', 'dosage', 'frequency', 'duration', 'instructions'],
    extra=3, can_delete=True,
    widgets={
        'medicine': forms.TextInput(attrs={'class': CTRL, 'placeholder': 'Medicine'}),
        'dosage': forms.TextInput(attrs={'class': CTRL, 'placeholder': 'Dosage'}),
        'frequency': forms.TextInput(attrs={'class': CTRL, 'placeholder': 'Frequency'}),
        'duration': forms.TextInput(attrs={'class': CTRL, 'placeholder': 'Duration'}),
        'instructions': forms.TextInput(attrs={'class': CTRL, 'placeholder': 'Instructions'}),
    })


class InvoiceForm(StyledModelForm):
    class Meta:
        model = Invoice
        fields = ['patient', 'appointment', 'issued_date', 'due_date',
                  'tax_percent', 'discount', 'notes']
        widgets = {
            'issued_date': forms.DateInput(attrs={'type': 'date'}),
            'due_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['patient'].queryset = User.objects.filter(role='patient')
        self.fields['appointment'].required = False


InvoiceItemFormSet = inlineformset_factory(
    Invoice, InvoiceItem,
    fields=['description', 'quantity', 'unit_price'],
    extra=3, can_delete=True,
    widgets={
        'description': forms.TextInput(attrs={'class': CTRL, 'placeholder': 'Description'}),
        'quantity': forms.NumberInput(attrs={'class': CTRL}),
        'unit_price': forms.NumberInput(attrs={'class': CTRL}),
    })


class PaymentForm(StyledModelForm):
    amount = forms.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal('0.01'))

    class Meta:
        model = Payment
        fields = ['amount', 'method', 'paid_on', 'reference']
        widgets = {'paid_on': forms.DateInput(attrs={'type': 'date'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['paid_on'].initial = timezone.localdate()
        self.fields['paid_on'].required = False

    def clean_paid_on(self):
        return self.cleaned_data.get('paid_on') or timezone.localdate()


class PatientDownpaymentForm(forms.Form):
    method = forms.ChoiceField(choices=PaymentMethod.choices, label='Payment method')
    reference = forms.CharField(max_length=60, required=False, label='Payment reference')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['method'].choices = [
            choice for choice in PaymentMethod.choices
            if choice[0] != PaymentMethod.XENDIT
        ]
        _style(self)


class ReviewForm(StyledModelForm):
    class Meta:
        model = Review
        fields = ['dentist', 'rating', 'comment']
        widgets = {'comment': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['dentist'].queryset = User.objects.filter(role='dentist')
        self.fields['dentist'].required = False


class ServiceForm(StyledModelForm):
    class Meta:
        model = Service
        fields = ['name', 'description', 'price', 'duration_minutes', 'icon', 'image', 'is_active']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}


class InventoryItemForm(StyledModelForm):
    class Meta:
        model = InventoryItem
        fields = ['name', 'category', 'quantity', 'unit', 'reorder_level',
                  'unit_price', 'supplier', 'icon']
