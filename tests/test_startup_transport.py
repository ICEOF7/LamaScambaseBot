import unittest
from unittest.mock import patch
import requests
import test_bot


class StartupTransport(unittest.TestCase):
    setUp = test_bot.AppTest.setUp
    tearDown = test_bot.AppTest.tearDown

    def test_transport_retry_recovers_before_polling(self):
        with patch.object(self.app.bot.raw,'remove_webhook',side_effect=[requests.ConnectionError('dns'),requests.Timeout('timeout'),True]) as request, patch.object(self.app.time,'sleep'):
            self.assertTrue(self.app.remove_webhook_with_retry())
            self.assertEqual(request.call_count,3)

    def test_transport_retries_are_bounded(self):
        with patch.object(self.app.bot.raw,'remove_webhook',side_effect=requests.ConnectionError('dns')) as request, patch.object(self.app.time,'sleep'):
            with self.assertRaises(requests.ConnectionError):self.app.remove_webhook_with_retry()
            self.assertEqual(request.call_count,3)

    def test_api_errors_do_not_retry(self):
        with patch.object(self.app.bot.raw,'remove_webhook',side_effect=RuntimeError('API refused')) as request:
            with self.assertRaises(RuntimeError):self.app.remove_webhook_with_retry()
            self.assertEqual(request.call_count,1)
