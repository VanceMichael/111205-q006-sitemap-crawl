import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from fixture_server import site


class SiteTests(unittest.TestCase):
    def test_last_modified_and_etag_precedence(self):
        with site() as base:
            headers = {'If-Modified-Since': 'Wed, 11 Nov 2026 00:00:00 GMT'}
            with self.assertRaises(HTTPError) as failure:
                urlopen(Request(base + '/article', headers=headers), timeout=2)
            self.assertEqual(failure.exception.code, 304)
            failure.exception.close()
            headers['If-None-Match'] = '"different"'
            with urlopen(Request(base + '/article', headers=headers), timeout=2) as response:
                self.assertEqual(response.status, 200)

    def test_missing_and_temporary_failure_are_distinct(self):
        with site() as base:
            for path, status in [('/missing', 404), ('/temporary', 503)]:
                with self.assertRaises(HTTPError) as failure:
                    urlopen(base + path, timeout=2)
                self.assertEqual(failure.exception.code, status)
                if status == 503:
                    self.assertEqual(failure.exception.headers['Retry-After'], '5')
                failure.exception.close()
