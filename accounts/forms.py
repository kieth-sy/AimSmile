from copy import copy
import re

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.forms import (AuthenticationForm, SetPasswordForm,
                                       UserCreationForm)
from django.utils.html import format_html

from .models import Role

User = get_user_model()

INPUT = 'form-control-app'


class PhilippinesPhoneWidget(forms.TextInput):
    def render(self, name, value, attrs=None, renderer=None):
        return format_html(
            '<div class="d-flex align-items-center gap-2">'
            '<span class="badge-app badge-info" style="height:38px;display:inline-flex;align-items:center;font-size:.95rem;">+63</span>'
            '{}'
            '</div>',
            super().render(name, value, attrs, renderer),
        )


class BootstrapMixin:
    """Adds consistent CSS classes + placeholders to every field."""

    def _style(self):
        for name, field in self.fields.items():
            widget = field.widget
            css = INPUT
            if isinstance(widget, forms.CheckboxInput):
                css = 'form-check-input-app'
            elif isinstance(widget, forms.Select):
                css = INPUT + ' select-app'
            existing = widget.attrs.get('class', '')
            widget.attrs['class'] = (existing + ' ' + css).strip()
            if not widget.attrs.get('placeholder') and not isinstance(widget, (forms.Select, forms.CheckboxInput)):
                widget.attrs['placeholder'] = field.label or name.replace('_', ' ').title()


class SignUpForm(BootstrapMixin, UserCreationForm):
    first_name = forms.CharField(max_length=50)
    last_name = forms.CharField(max_length=50, required=False)
    email = forms.EmailField()
    date_of_birth = forms.DateField(
        required=True,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    address = forms.CharField(max_length=255, required=False)
    phone = forms.CharField(
        max_length=20,
        required=True,
        widget=PhilippinesPhoneWidget(attrs={
            'autocomplete': 'tel',
            'inputmode': 'numeric',
            'placeholder': '917 123 4567',
        })
    )
    class Meta:
        model = User
        fields = (
            'username', 'first_name', 'last_name', 'email', 'date_of_birth',
            'address', 'phone', 'password1', 'password2',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['phone'].required = True
        self.fields['phone'].initial = ''
        self.fields['phone'].widget.attrs['placeholder'] = '917 123 4567'
        self.fields['phone'].widget.attrs['inputmode'] = 'numeric'
        self.fields['phone'].widget.attrs['pattern'] = r'[0-9]{10}'
        self.fields['phone'].help_text = 'Philippines mobile number'
        autocomplete = {
            'username': 'username',
            'first_name': 'given-name',
            'last_name': 'family-name',
            'email': 'email',
        }
        for name, value in autocomplete.items():
            self.fields[name].widget.attrs['autocomplete'] = value
        self._style()

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        digits = re.sub(r'\D', '', phone)
        if digits.startswith('0'):
            digits = digits[1:]
        if not re.fullmatch(r'9\d{9}', digits):
            raise forms.ValidationError(
                'Enter a valid Philippines mobile number such as +63 917 123 4567.'
            )
        return '+63' + digits

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = Role.PATIENT  # public sign-ups are always patients
        user.email = self.cleaned_data['email']
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data.get('last_name', '')
        user.phone = self.cleaned_data.get('phone') or '0000000000'
        user.date_of_birth = self.cleaned_data['date_of_birth']
        user.address = self.cleaned_data.get('address', '')
        if commit:
            user.save()
        return user


class LoginForm(BootstrapMixin, AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
        self.fields['username'].widget.attrs['placeholder'] = 'Username'
        self.fields['password'].widget.attrs['placeholder'] = 'Password'
        self.fields['username'].widget.attrs['autocomplete'] = 'off'
        self.fields['username'].widget.attrs.pop('autofocus', None)
        self.fields['password'].widget.attrs['autocomplete'] = 'new-password'


class ProfileForm(BootstrapMixin, forms.ModelForm):
    date_of_birth = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'})
    )

    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'phone', 'gender',
                  'date_of_birth', 'address', 'bio', 'avatar')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class StaffUserForm(BootstrapMixin, forms.ModelForm):
    """Used by admins to create/edit any user (any role)."""
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(),
        help_text='Leave blank to keep the current password when editing.'
    )
    date_of_birth = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'})
    )

    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'phone',
                  'role', 'gender', 'date_of_birth', 'address', 'avatar',
                  'is_active')

    def __init__(self, *args, require_email=False, require_password=False,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = require_email
        self.fields['password'].required = require_password
        if require_password:
            self.fields['password'].help_text = (
                'Set the initial password for this account.')
        self._style()

    def save(self, commit=True):
        user = super().save(commit=False)
        pwd = self.cleaned_data.get('password')
        if pwd:
            user.set_password(pwd)
        if commit:
            user.save()
        return user

    def clean_password(self):
        password = self.cleaned_data.get('password')
        if password:
            user = copy(self.instance)
            for field in ('username', 'email', 'first_name', 'last_name'):
                if field in self.cleaned_data:
                    setattr(user, field, self.cleaned_data[field])
            validate_password(password, user)
        return password


class PasswordSetupForm(BootstrapMixin, SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class PhoneVerificationForm(BootstrapMixin, forms.Form):
    code = forms.RegexField(
        regex=r'^\d{6}$',
        max_length=6,
        min_length=6,
        label='SMS verification code',
        widget=forms.TextInput(attrs={
            'autocomplete': 'one-time-code',
            'inputmode': 'numeric',
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class TOTPCodeForm(BootstrapMixin, forms.Form):
    code = forms.RegexField(
        regex=r'^\d{6}$',
        max_length=6,
        min_length=6,
        label='Authenticator code',
        widget=forms.TextInput(attrs={
            'autocomplete': 'one-time-code',
            'inputmode': 'numeric',
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
