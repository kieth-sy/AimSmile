from django.urls import path

from . import views

app_name = 'manage_panel'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('reports/', views.reports, name='reports'),
    path('activity-logs/', views.activity_log_list, name='activity_log_list'),
    path('contact-messages/', views.contact_message_list,
         name='contact_message_list'),
    path('contact-messages/<int:pk>/', views.contact_message_detail,
         name='contact_message_detail'),
    path('contact-messages/<int:pk>/reply/', views.contact_message_reply,
         name='contact_message_reply'),

    # Users
    path('users/', views.user_list, name='user_list'),
    path('users/new/', views.user_create, name='user_create'),
    path('users/<int:pk>/edit/', views.user_update, name='user_update'),
    path('users/<int:pk>/delete/', views.user_delete, name='user_delete'),
    path('users/<int:pk>/review/', views.pending_user_review,
         name='pending_user_review'),
    path('users/duplicates/<int:pk>/review/', views.duplicate_signup_review,
         name='duplicate_signup_review'),

    # Generic resources
    path('<slug:slug>/', views.resource_list, name='resource_list'),
    path('<slug:slug>/new/', views.resource_create, name='resource_create'),
    path('<slug:slug>/<int:pk>/edit/', views.resource_update, name='resource_update'),
    path('<slug:slug>/<int:pk>/delete/', views.resource_delete, name='resource_delete'),
]
