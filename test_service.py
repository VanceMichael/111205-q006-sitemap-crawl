import unittest
from decimal import Decimal
from service import canonical_url

class SitemapCrawlTests(unittest.TestCase):
    def test_removes_tracking_but_keeps_business_query(self):
        self.assertEqual(canonical_url('HTTPS://EXAMPLE.org/p?id=4&utm_source=x#top'), 'https://example.org/p?id=4')

    def test_empty_path(self):
        self.assertEqual(canonical_url('https://example.org'), 'https://example.org/')


if __name__ == '__main__':
    unittest.main()
