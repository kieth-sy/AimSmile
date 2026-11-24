from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import ContactMessage

User = get_user_model()


class ContactMessageTests(TestCase):
    def test_public_contact_form_saves_message(self):
        response = self.client.post(reverse('pages:contact'), {
            'name': 'Jamie Patient',
            'email': 'jamie@example.com',
            'subject': 'Appointment question',
            'message': 'Can I book a consultation?',
        })

        self.assertRedirects(response, reverse('pages:contact_sent'))
        self.assertTrue(ContactMessage.objects.filter(
            name='Jamie Patient',
            email='jamie@example.com',
            subject='Appointment question',
            message='Can I book a consultation?',
            status=ContactMessage.Status.PENDING,
        ).exists())
        self.assertContains(self.client.get(reverse('pages:contact_sent')), 'message has been sent')

    def test_public_contact_form_rejects_invalid_email(self):
        response = self.client.post(reverse('pages:contact'), {
            'name': 'Jamie Patient',
            'email': 'not-an-email',
            'message': 'Can I book a consultation?',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_dentist_cannot_open_admin_contact_inbox(self):
        dentist = User.objects.create_user(
            username='contact-dentist',
            password='strong-test-password',
            role='dentist',
        )
        self.client.force_login(dentist)

        response = self.client.get(reverse('manage_panel:contact_message_list'))

        self.assertEqual(response.status_code, 403)


class ContactMessageReplyTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='contact-admin',
            password='strong-test-password',
            role='admin',
        )
        self.client.force_login(self.admin)
        self.contact_message = ContactMessage.objects.create(
            name='Jamie Patient',
            email='jamie@example.com',
            subject='Appointment question',
            message='Can I book a consultation?',
        )

    def test_admin_inbox_has_topbar_message_button(self):
        response = self.client.get(reverse('manage_panel:contact_message_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-label="Contact messages"')
        self.assertContains(response, 'href="/manage/contact-messages/"')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_admin_reply_is_emailed_to_patient_and_saved(self):
        response = self.client.post(
            reverse('manage_panel:contact_message_reply',
                    args=[self.contact_message.pk]),
            {'reply': 'Yes, please call us to schedule your consultation.'},
        )

        self.assertRedirects(
            response,
            reverse('manage_panel:contact_message_detail',
                    args=[self.contact_message.pk]),
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['jamie@example.com'])
        self.assertEqual(mail.outbox[0].subject, 'Re: Appointment question')
        self.assertEqual(
            mail.outbox[0].body,
            'Yes, please call us to schedule your consultation.',
        )
        self.contact_message.refresh_from_db()
        self.assertEqual(self.contact_message.status, ContactMessage.Status.REPLIED)
        self.assertEqual(
            self.contact_message.reply,
            'Yes, please call us to schedule your consultation.',
        )
        self.assertEqual(self.contact_message.replied_by, self.admin)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend')
    def test_console_email_backend_does_not_mark_reply_as_sent(self):
        response = self.client.post(
            reverse('manage_panel:contact_message_reply',
                    args=[self.contact_message.pk]),
            {'reply': 'Please call us to schedule.'},
        )

        self.assertRedirects(
            response,
            reverse('manage_panel:contact_message_detail',
                    args=[self.contact_message.pk]),
        )
        self.contact_message.refresh_from_db()
        self.assertEqual(self.contact_message.status, ContactMessage.Status.PENDING)
        self.assertEqual(self.contact_message.reply, '')
