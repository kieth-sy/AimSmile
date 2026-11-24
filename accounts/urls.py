from django.contrib.auth import views as auth_views
from django.contrib.auth.views import LogoutView
from django.urls import path, reverse_lazy

from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.AppLoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('signup/', views.signup_view, name='signup'),
    path('identity/scan-id/', views.identity_scan_id, name='identity_scan_id'),
    path('identity/match-face/', views.identity_match_face, name='identity_match_face'),
    path('identity/enroll/', views.identity_enrollment_view,
         name='identity_enrollment'),
    path('identity/enroll/scan-id/', views.identity_enrollment_scan_id,
         name='identity_enrollment_scan_id'),
    path('identity/enroll/match-face/', views.identity_enrollment_match_face,
         name='identity_enrollment_match_face'),
    path('identity/enroll/remove/', views.identity_enrollment_delete,
         name='identity_enrollment_delete'),
    path('two-factor/login/', views.two_factor_login_view, name='two_factor_login'),
    path('sms/login/', views.sms_login_view, name='sms_login'),
    path('sms/login/resend/', views.sms_login_resend_view, name='sms_login_resend'),
    path('two-factor/settings/', views.two_factor_settings_view,
         name='two_factor_settings'),
    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            template_name='accounts/password_reset_form.html',
            email_template_name='accounts/password_reset_email.html',
            success_url=reverse_lazy('accounts:password_reset_done'),
        ),
        name='password_reset',
    ),
    path(
        'password-reset/done/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='accounts/password_reset_done.html',
        ),
        name='password_reset_done',
    ),
    path(
        'password-reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='accounts/password_reset_confirm.html',
            success_url=reverse_lazy('accounts:password_reset_complete'),
        ),
        name='password_reset_confirm',
    ),
    path(
        'password-reset/complete/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='accounts/password_reset_complete.html',
        ),
        name='password_reset_complete',
    ),
    path('password-setup/<uidb64>/<token>/', views.password_setup,
         name='password_setup'),
    path('profile/', views.profile_view, name='profile'),
    path('go/', views.redirect_dashboard, name='redirect_dashboard'),
]
