import http.client
import json
import threading
import time
import unittest
from urllib.parse import urlsplit

from sanwufan.owner import OwnerControl


class OwnerControlTests(unittest.TestCase):
    def setUp(self):
        self.stopped = threading.Event()
        self.control = OwnerControl('http://127.0.0.1:8766', self.stopped.set, grace=.4)
        self.port = urlsplit(self.control.origin).port

    def tearDown(self):
        self.control.close()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body).encode() if body is not None else None, headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def session(self):
        status, headers, _ = self.request('/session', {'token': self.control.token}, {'Origin': self.control.origin})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0]

    def stream(self, cookie):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        connection.request('GET', '/events', headers={'Cookie': cookie})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.readline(), b'data: running\n')
        return connection, response

    def test_only_authenticated_local_management_can_stop_or_arm(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/events')[0], 403)
        self.assertEqual(self.request('/session', {'token': self.control.token})[0], 403)
        self.assertEqual(self.request('/session', {'token': 'wrong'}, {'Origin': self.control.origin})[0], 403)
        self.assertEqual(self.request('/session', {'token': '中文'}, {'Origin': self.control.origin})[0], 403)
        self.assertEqual(self.request('/stop', {}, {'Origin': self.control.origin})[0], 403)
        cookie = self.session()
        self.assertEqual(self.request('/stop', {}, {'Origin': 'https://a.tunnelmole.net', 'Cookie': cookie})[0], 403)
        self.assertEqual(self.request('/stop', {}, {'Origin': self.control.origin, 'Cookie': cookie, 'Host': 'evil.example'})[0], 403)
        self.assertFalse(self.stopped.wait(.6), 'Unvisited/headless launch must stay running')
        self.assertEqual(self.request('/stop', {}, {'Origin': self.control.origin, 'Cookie': cookie})[0], 200)
        self.assertTrue(self.stopped.wait(1))

    def test_last_page_close_refresh_and_multiple_pages(self):
        cookie = self.session()
        first = self.stream(cookie)
        second = self.stream(cookie)
        for obj in reversed(first):
            obj.close()
        self.assertFalse(self.stopped.wait(1.1), 'Another management page is still open')
        for obj in reversed(second):
            obj.close()
        # Refresh reconnects before the last stream's grace expires.
        replacement = self.stream(cookie)
        self.assertFalse(self.stopped.wait(1.1))
        for obj in reversed(replacement):
            obj.close()
        self.assertTrue(self.stopped.wait(3), 'The last management page closed')


if __name__ == '__main__':
    unittest.main()
