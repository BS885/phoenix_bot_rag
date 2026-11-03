import unittest

import config
from ingest import urls

H = "https://www.fnx.co.il"


class TestFilter(unittest.TestCase):
    def test_allowed(self):
        self.assertTrue(urls.is_allowed_url(H + "/claims-customer-service/claims-health-insurance/"))

    def test_excluded_prefixes(self):
        for p in ("/en/x/", "/tags/a/", "/investor-relations/", "/career/job/", "/404/"):
            self.assertFalse(urls.is_allowed_url(H + p), p)

    def test_excluded_query(self):
        for q in ("?page=2", "?tag=a", "?domain=b", "?staff=c"):
            self.assertFalse(urls.is_allowed_url(H + "/health/" + q), q)

    def test_numeric_fund_page_and_old(self):
        self.assertFalse(urls.is_allowed_url(H + "/funds/12345"))
        self.assertFalse(urls.is_allowed_url(H + "/products/car-old/"))

    def test_other_host(self):
        self.assertFalse(urls.is_allowed_url("https://example.com/claims-customer-service/"))

    def test_priority_and_order(self):
        pages = [{"url": H + "/pension/a/"}, {"url": H + "/customer-service/b/"}, {"url": H + "/pension/a/"}]
        out = urls.select_urls(pages)
        self.assertEqual([p["url"] for p in out], [H + "/customer-service/b/", H + "/pension/a/"])
        self.assertEqual(out[0]["priority"], 1)


class TestParsing(unittest.TestCase):
    def test_sitemap_pages(self):
        xml = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<url><loc>https://www.fnx.co.il/a/</loc><lastmod>2026-01-02</lastmod></url>'
               '<url><loc>https://www.fnx.co.il/b/</loc></url></urlset>')
        pages, children = urls.parse_sitemap(xml)
        self.assertEqual(pages[0], {"url": H + "/a/", "lastmod": "2026-01-02"})
        self.assertIsNone(pages[1]["lastmod"])
        self.assertEqual(children, [])

    def test_sitemap_index(self):
        xml = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<sitemap><loc>https://www.fnx.co.il/s1.xml</loc></sitemap></sitemapindex>')
        pages, children = urls.parse_sitemap(xml)
        self.assertEqual((pages, children), ([], [H + "/s1.xml"]))

    def test_robots(self):
        txt = "User-agent: *\nDisallow: /private/\nSitemap: https://www.fnx.co.il/sitemap.xml\n"
        self.assertEqual(urls.sitemaps_from_robots(txt), [H + "/sitemap.xml"])
        rp = urls.make_robots(txt)
        out = urls.select_urls([{"url": H + "/private/x/"}, {"url": H + "/ok/"}], rp)
        self.assertEqual([p["url"] for p in out], [H + "/ok/"])


if __name__ == "__main__":
    unittest.main()
