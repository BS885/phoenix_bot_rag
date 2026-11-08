import unittest

import config
from bot import retrieve as r

H = "https://www.fnx.co.il"
BODY = " ".join(f"מילה{i}" for i in range(30))


def hit(path, section, dist, body=BODY, page="עמוד", short=False, dup_urls="", n=0):
    return {"id": f"{path}{n}", "text": f"{page} > {section}\n{body}", "url": H + path, "page_title": page,
            "section_title": section, "anchor": "", "distance": dist, "short": short, "dup_urls": dup_urls}


class TestTopics(unittest.TestCase):
    def test_topic_of_urls(self):
        self.assertEqual(r.topic_of(H + "/health-insurance/extra-covers/"), "health-insurance")
        self.assertEqual(r.topic_of(H + "/claims-customer-service/claims-car-insurance/x/"), "car-insurance")
        self.assertEqual(r.topic_of(H + "/claims-customer-service/claims-appartment-insurance/x/"), "apartment-insurance")
        self.assertEqual(r.topic_of(H + "/customer-service/car-insurance/car_renewal/"), "car-insurance")
        self.assertEqual(r.topic_of(H + "/customer-service/app/"), "general")
        self.assertEqual(r.topic_of(H + "/claims-customer-service/"), "general")
        self.assertEqual(r.topic_of(H + "/claims-customer-service/claims-liability-insurance/y/"), "liability")

    def test_every_alias_target_has_a_name(self):
        for target in config.TOPIC_ALIASES.values():
            self.assertTrue(target in config.PRODUCT_NAMES or target in config.EXTRA_TOPIC_NAMES, target)

    def test_every_term_table_product_is_a_known_product(self):
        for slug in config.PRODUCT_TERMS:
            self.assertIn(slug, config.PRODUCT_NAMES)

    def test_detect_products(self):
        self.assertEqual(r.detect_products("איך מגישים תביעה אחרי תאונת דרכים בביטוח רכב?"), ["car-insurance"])
        self.assertIn("health-insurance", r.detect_products("יש לי ניתוח, מה אני צריך לעשות?"))
        self.assertEqual(r.detect_products("איך מגישים תביעה?"), [])
        self.assertIn("travel-insurance", r.detect_products('מה עושים בחו"ל?'))


class TestShort(unittest.TestCase):
    def test_short_without_number_or_link_is_dropped(self):
        self.assertTrue(r.is_very_short(hit("/a/", "שאלות נפוצות", 0.2, body="קצר מאוד כאן")))
        self.assertFalse(r.is_very_short(hit("/a/", "טלפון", 0.2, body="התקשרו 5432*")))
        self.assertFalse(r.is_very_short(hit("/a/", "קישור", 0.2, body="טופס (https://x.co.il/f)")))
        self.assertFalse(r.is_very_short(hit("/a/", "ארוך", 0.2)))

    def test_threshold_is_tunable(self):
        h = hit("/a/", "נושא", 0.2, body="אחת שתיים שלוש ארבע חמש שש שבע שמונה תשע עשר")   # 10 words
        self.assertFalse(r.is_very_short(h, min_words=10))
        self.assertTrue(r.is_very_short(h, min_words=11))

    def test_short_hits_are_counted_and_removed(self):
        out = r.process_hits("שאלה", [hit("/health-insurance/a/", "קצר", 0.1, body="קצר"),
                                      hit("/health-insurance/b/", "ארוך", 0.2)])
        self.assertEqual((out.dropped_short, [g.section_title for g in out.groups]), (1, ["ארוך"]))

    def test_never_ends_with_nothing(self):
        out = r.process_hits("שאלה", [hit("/health-insurance/a/", "נושא", 0.2, body="קצר")])
        self.assertEqual(len(out.groups), 1)


class TestGrouping(unittest.TestCase):
    def test_same_topic_section_and_text_are_grouped_with_all_sources(self):
        hits = [hit("/claims-customer-service/claims-health-insurance/a/", "מה קורה לאחר הגשת התביעה?", 0.205, page="א"),
                hit("/claims-customer-service/claims-health-insurance/b/", "מה קורה לאחר הגשת התביעה?", 0.208, page="ב",
                    dup_urls=H + "/claims-customer-service/claims-health-insurance/c/"),
                hit("/claims-customer-service/claims-car-insurance/d/", "מה קורה לאחר הגשת התביעה?", 0.210, page="ד")]
        out = r.process_hits("איך מקבלים תשובה?", hits)
        self.assertEqual([(g.topic, g.size) for g in out.groups], [("health-insurance", 2), ("car-insurance", 1)])
        self.assertEqual([s["url"][len(H):] for s in out.groups[0].sources],
                         ["/claims-customer-service/claims-health-insurance/a/",
                          "/claims-customer-service/claims-health-insurance/b/",
                          "/claims-customer-service/claims-health-insurance/c/"])
        self.assertEqual(out.groups[0].url[len(H):], "/claims-customer-service/claims-health-insurance/a/")  # the closest

    def test_same_title_but_different_content_is_not_hidden(self):
        other = " ".join(f"אחר{i}" for i in range(30))
        hits = [hit("/claims-customer-service/claims-health-insurance/a/", "איך מגישים תביעה?", 0.16),
                hit("/claims-customer-service/claims-health-insurance/b/", "איך מגישים תביעה?", 0.19, body=other)]
        out = r.process_hits("איך מגישים תביעה לבריאות?", hits)
        self.assertEqual(len(out.groups), 2)

    def test_max_groups(self):
        hits = [hit(f"/health-insurance/p{i}/", f"נושא {i}", 0.2 + i / 100) for i in range(9)]
        self.assertEqual(len(r.process_hits("בריאות", hits).groups), config.RETRIEVE_MAX_GROUPS)


class TestProductMatchAndClarify(unittest.TestCase):
    def two_products(self):
        return [hit("/claims-customer-service/claims-health-insurance/a/", "איך מגישים תביעה?", 0.200),
                hit("/claims-customer-service/claims-car-insurance/b/", "איך מגישים תביעה?", 0.215, body=BODY + " רכב")]

    def test_named_product_drops_other_products(self):
        out = r.process_hits("איך מגישים תביעה בביטוח רכב?", self.two_products())
        self.assertEqual([g.topic for g in out.groups], ["car-insurance"])
        self.assertEqual(out.dropped_other_product, 1)
        self.assertFalse(out.product_conflict)

    def test_named_product_with_no_match_keeps_everything_and_flags_a_miss(self):
        out = r.process_hits("איך מגישים תביעה בביטוח נסיעות?", self.two_products())
        self.assertTrue(out.product_miss)
        self.assertEqual(len(out.groups), 2)

    def test_conflict_when_strong_groups_are_from_different_products_and_none_is_named(self):
        out = r.process_hits("איך מגישים תביעה?", self.two_products())
        self.assertTrue(out.product_conflict)
        self.assertEqual(out.conflict_topics, ["health-insurance", "car-insurance"])

    def test_no_conflict_when_the_second_product_is_far(self):
        hits = self.two_products()
        hits[1]["distance"] = 0.20 + config.RETRIEVE_CLARIFY_DELTA + 0.01
        self.assertFalse(r.process_hits("איך מגישים תביעה?", hits).product_conflict)

    def test_no_conflict_for_weak_matches(self):
        hits = self.two_products()
        hits[0]["distance"], hits[1]["distance"] = 0.35, 0.36
        self.assertFalse(r.process_hits("איך מגישים תביעה?", hits).product_conflict)

    def test_general_pages_never_cause_a_conflict(self):
        hits = [hit("/customer-service/app/", "אפליקציה", 0.20), hit("/health-insurance/a/", "נושא", 0.21)]
        self.assertFalse(r.process_hits("שאלה כללית", hits).product_conflict)

    def test_debug_is_plain_data(self):
        d = r.process_hits("איך מגישים תביעה?", self.two_products()).debug()
        self.assertEqual(d["conflict_topics"], ["health-insurance", "car-insurance"])
        self.assertEqual(len(d["groups"]), 2)


if __name__ == "__main__":
    unittest.main()
