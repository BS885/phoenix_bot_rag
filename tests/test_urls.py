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


class TestFetchable(unittest.TestCase):
    ROBOTS = "User-agent: *\nDisallow: /private/\n"

    def test_fragment_removed(self):
        self.assertEqual(urls.normalize_url(H + "/health-insurance/extra-covers/#faq"), H + "/health-insurance/extra-covers/")

    def test_allowed_page_passes(self):
        ok, reason = urls.fetchable(H + "/health-insurance/", urls.make_robots(self.ROBOTS))
        self.assertEqual((ok, reason), (True, ""))

    def test_robots_disallow_blocks(self):
        ok, reason = urls.fetchable(H + "/private/x/", urls.make_robots(self.ROBOTS))
        self.assertFalse(ok)
        self.assertIn("robots", reason)

    def test_filter_blocks_before_robots(self):
        ok, reason = urls.fetchable(H + "/en/page/", urls.make_robots(self.ROBOTS))
        self.assertFalse(ok)
        self.assertIn("filter", reason)


class TestOutOfScope(unittest.TestCase):
    def test_class_actions_and_terms_of_use_are_excluded(self):
        for path in ("/claims-customer-service/class-actions/credit-claim-1/",
                     "/claims-customer-service/class-actions/", "/customer-service/terms-of-use/privacy-policy/"):
            self.assertFalse(urls.is_allowed_url(H + path), path)

    def test_neighbouring_claims_and_service_pages_stay_in_scope(self):
        for path in ("/claims-customer-service/claims-health-insurance/", "/customer-service/car-insurance/",
                     "/claims-customer-service/", "/customer-service/"):
            self.assertTrue(urls.is_allowed_url(H + path), path)

    def test_excluded_pages_never_reach_the_crawl_list(self):
        pages = [{"url": H + "/claims-customer-service/class-actions/x/"}, {"url": H + "/customer-service/a/"}]
        self.assertEqual([p["url"] for p in urls.select_urls(pages)], [H + "/customer-service/a/"])

    def test_spf_iframe_pages_are_blocked_by_robots_txt(self):
        robots = urls.make_robots("User-agent: *\nDisallow: /SPF\n")
        ok, reason = urls.fetchable(H + "/SPF/iframe_health-insurance.aspx", robots)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
