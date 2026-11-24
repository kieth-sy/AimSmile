from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .engine import chatbot_response


class LocalChatbotResponseTests(TestCase):
    def test_greeting_uses_programmed_reply(self):
        self.assertIn('DentaBot', chatbot_response('Hi!'))

    def test_appointment_question_returns_booking_instructions(self):
        reply = chatbot_response('How can I book an appointment?')

        self.assertIn('Book Appointment', reply)

    def test_service_and_price_answers_do_not_invent_catalog_values(self):
        self.assertIn('Services page', chatbot_response('What services do you offer?'))
        self.assertIn('listed prices', chatbot_response('How much does treatment cost?'))

    def test_clinic_hours_are_not_invented(self):
        reply = chatbot_response('What are the clinic hours?')

        self.assertIn('Contact page', reply)
        self.assertNotIn('9:00 AM', reply)

    def test_symptom_reply_does_not_diagnose(self):
        reply = chatbot_response('I have severe tooth pain')

        self.assertIn('diagnose', reply)
        self.assertIn('urgent medical care', reply)

    def test_matching_words_are_not_detected_inside_other_words(self):
        reply = chatbot_response('This is a question about clinic services')

        self.assertIn('Services page', reply)


class LocalChatbotAPITests(TestCase):
    def setUp(self):
        cache.clear()

    def test_chat_page_does_not_require_an_api_key(self):
        response = self.client.get(reverse('aiassistant:chatbot'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Walang external AI API')
        self.assertNotContains(response, 'Online chat is not configured')

    @patch('aiassistant.views.engine.chatbot_response', return_value='Local reply')
    def test_api_responds_without_calling_an_external_provider(self, local_reply):
        response = self.client.post(
            reverse('aiassistant:chatbot_api'),
            {'message': 'Hello'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'reply': 'Local reply'})
        local_reply.assert_called_once_with('Hello')

    def test_api_rejects_empty_or_oversized_messages(self):
        empty_response = self.client.post(
            reverse('aiassistant:chatbot_api'),
            {'message': '   '},
        )
        long_response = self.client.post(
            reverse('aiassistant:chatbot_api'),
            {'message': 'x' * 2001},
        )

        self.assertEqual(empty_response.status_code, 400)
        self.assertEqual(long_response.status_code, 400)

    @patch('aiassistant.views.engine.chatbot_response', return_value='Local reply')
    def test_api_limits_messages_per_ip(self, local_reply):
        url = reverse('aiassistant:chatbot_api')
        responses = [
            self.client.post(url, {'message': 'Hello'})
            for _ in range(11)
        ]

        self.assertTrue(all(response.status_code == 200 for response in responses[:10]))
        self.assertEqual(responses[-1].status_code, 429)
        self.assertEqual(local_reply.call_count, 10)
