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
            self.assertTrue(slug in config.PRODUCT_NAMES or slug in config.EXTRA_TOPIC_NAMES, slug)

    def test_the_accident_insurance_pages_under_health_belong_to_personal_accident(self):
        H = "https://www.fnx.co.il"
        self.assertEqual(r.topic_of(H + "/health-insurance/personal-accidents/"), "personal-accident")
        self.assertEqual(r.topic_of(H + "/health-insurance/health_information/dangerous-hobby/"), "personal-accident")
        self.assertEqual(r.topic_of(H + "/claims-customer-service/claims-personal-accident-insurance/"), "personal-accident")
        self.assertEqual(r.topic_of(H + "/health-insurance/health_information/other/"), "health-insurance")
        self.assertEqual(r.detect_products("מה מכסה ביטוח תאונות אישיות?"), ["personal-accident"])

    def test_a_group_expands_a_chosen_product_and_health_is_not_in_it(self):
        self.assertEqual(r.search_topics(["personal-accident"]), ["personal-accident"])
        self.assertEqual(r.search_topics(["health-insurance"]), ["health-insurance"])
        self.assertEqual(r.search_topics(["car-insurance", "car-insurance"]), ["car-insurance"])

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


def part(n, words, section="מקטע", sid="s1", cid=None):
    body = " ".join(f"p{n}w{i}" for i in range(words))
    return {"id": cid or f"c{n}", "text": f"עמוד > {section}\n{body}", "chunk_no": n, "section_id": sid}


class TestFullContext(unittest.TestCase):
    def test_all_parts_in_page_order_without_repeating_the_header(self):
        parts = [part(2, 50), part(0, 50), part(1, 50)]
        text, words = r.section_context(parts[1], parts, 600)
        self.assertEqual(text.count("עמוד > מקטע"), 1)
        self.assertTrue(text.index("p0w0") < text.index("p1w0") < text.index("p2w0"))
        self.assertEqual(words, 3 + 50 + 2 * 50)   # the header (3 words) counts once, with the found chunk

    def test_cap_keeps_the_found_chunk_and_its_nearest_parts(self):
        parts = [part(n, 200) for n in range(5)]
        text, words = r.section_context(parts[2], parts, 600)
        self.assertIn("p2w0", text)
        self.assertLessEqual(words, 600 + 5)
        self.assertNotIn("p0w0", text)
        self.assertNotIn("p4w0", text)

    def test_found_chunk_is_kept_even_if_it_alone_exceeds_the_cap(self):
        parts = [part(0, 400), part(1, 400)]
        text, _ = r.section_context(parts[1], parts, 100)
        self.assertIn("p1w0", text)
        self.assertNotIn("p0w0", text)

    def test_total_cap_across_groups(self):
        groups = [r.Group(text="x > t\n" + " ".join(["w"] * 10), url=H + f"/a/{i}", anchor="",
                          page_title="x", section_title="t", topic="a", topic_name="a", distance=0.1 + i / 100,
                          short=False, chunk_id=f"g{i}0", section_id=f"sec{i}") for i in range(3)]
        sib = lambda sid: [part(n, 200, sid=sid, cid=f"g{sid[-1]}{n}") for n in range(3)]   # noqa: E731
        r.expand_context(groups, sib, max_section=600, max_total=900)
        self.assertLessEqual(groups[0].context_words, 600)
        self.assertLessEqual(sum(g.context_words for g in groups[:2]), 900 + 15)
        self.assertTrue(all(g.context for g in groups))         # nobody is left without context

    def test_no_section_id_means_just_the_found_chunk(self):
        g = r.Group(text="x > t\nאחת שתיים", url=H, anchor="", page_title="x", section_title="t", topic="a",
                    topic_name="a", distance=0.1, short=False)
        r.expand_context([g], lambda sid: [])
        self.assertEqual(g.context, "x > t\nאחת שתיים")

    def test_process_hits_carries_ids(self):
        h = hit("/health-insurance/a/", "נושא", 0.2)
        h["id"], h["section_id"] = "abc", "sec9"
        g = r.process_hits("שאלה", [h]).groups[0]
        self.assertEqual((g.chunk_id, g.section_id), ("abc", "sec9"))


def sec_hit(path, section, dist, sid, body_word, n, words=60, **kw):
    body = " ".join(f"{body_word}{i}" for i in range(words))
    h = hit(path, section, dist, body=body, n=n, **kw)
    h["id"], h["section_id"] = f"id{n}", sid
    return h


class TestSameSectionMergesOnce(unittest.TestCase):
    """Question 5 (places 2 and 3): two parts of one section came back as two groups, each with the full context."""

    def hits(self):
        p = "/claims-customer-service/claims-health-insurance/a/"
        return [sec_hit(p, "איך מגישים תביעה?", 0.191, "secA", "א", 0),
                sec_hit(p, "מה קורה לאחר הגשת התביעה?", 0.213, "secB", "ב", 1),
                sec_hit(p, "מה קורה לאחר הגשת התביעה?", 0.231, "secB", "ג", 2)]    # same section_id, different text

    def test_parts_of_one_section_become_one_group(self):
        out = r.process_hits("מחלה קשה, מה לצרף לתביעה?", self.hits())
        self.assertEqual([(g.section_id, g.size) for g in out.groups], [("secA", 1), ("secB", 2)])
        self.assertEqual(out.groups[1].distance, 0.213)          # the closest part represents the group

    def test_its_context_is_counted_once_in_the_total_cap(self):
        out = r.process_hits("מחלה קשה, מה לצרף לתביעה?", self.hits())
        parts = lambda sid: [part(n, 200, sid=sid, cid=f"{sid}{n}") for n in range(3)]    # noqa: E731
        calls = []

        def siblings(sid):
            calls.append(sid)
            return parts(sid)
        r.expand_context(out.groups, siblings, max_section=600, max_total=1000)
        self.assertEqual(calls, ["secA", "secB"])                  # one fetch per section, not per part
        self.assertLessEqual(out.groups[0].context_words, 600)
        self.assertLessEqual(out.groups[1].context_words, 1000 - out.groups[0].context_words + 5)
        self.assertGreater(out.groups[1].context_words, 0)


class TestLinkOnlyCap(unittest.TestCase):
    def link_hit(self, n, dist):
        h = hit(f"/health-insurance/l{n}/", f"קישור {n}", dist, body=f"הגשת תביעה (https://x.fnx.co.il/{n})", n=n)
        return h

    def test_is_link_only(self):
        self.assertTrue(r.is_link_only(self.link_hit(1, 0.2)))
        self.assertFalse(r.is_link_only(hit("/a/", "ארוך", 0.2)))                       # long
        self.assertFalse(r.is_link_only(hit("/a/", "טלפון", 0.2, body="התקשרו 5432*")))  # short but no link

    def test_at_most_two_link_only_groups_in_the_top_five(self):
        hits = [self.link_hit(i, 0.20 + i / 100) for i in range(4)] +                [hit(f"/health-insurance/p{i}/", f"נושא {i}", 0.30 + i / 100, n=10 + i) for i in range(3)]
        out = r.process_hits("שאלה בבריאות", hits)
        self.assertEqual(len(out.groups), config.RETRIEVE_MAX_GROUPS)
        self.assertEqual(sum(g.link_only for g in out.groups), config.RETRIEVE_MAX_LINK_ONLY)
        self.assertEqual(out.pushed_down_link_only, 2)
        self.assertEqual([g.link_only for g in out.groups[:2]], [True, True])           # the closest two stay, in order

    def test_cap_does_not_remove_link_only_when_there_is_nothing_else(self):
        out = r.process_hits("שאלה", [self.link_hit(i, 0.2 + i / 100) for i in range(4)])
        self.assertEqual(len(out.groups), 2)                                              # only the allowed two remain


class TestClaimsAreaBonus(unittest.TestCase):
    def hits(self):
        prod = sec_hit("/health-insurance/extra-covers/a/", "מה כולל", 0.200, "p1", "א", 0)
        prod["area"] = "product"
        claim = sec_hit("/claims-customer-service/claims-health-insurance/b/", "איך מגישים", 0.215, "c1", "ב", 1)
        claim["area"] = "claims"
        return [prod, claim]

    def order(self, q, **kw):
        return [g.section_title for g in r.process_hits(q, [dict(h) for h in self.hits()], **kw).groups]

    def test_claim_words(self):
        for q in ("איך מגישים תביעה?", "מה לצרף?", "איפה הטופס", "מה ההחזר על דיקור?", "אפשר להגיש בקשה?"):
            self.assertTrue(r.has_claim_word(q), q)
        for q in ("איך מצטרפים לקרן השתלמות?", "מה זה כיסוי אמבולטורי?", "מה שער הדולר היום?"):
            self.assertFalse(r.has_claim_word(q), q)

    def test_with_a_claim_word_the_claim_page_ranks_first_but_keeps_its_real_distance(self):
        out = r.process_hits("איך מגישים תביעה?", [dict(h) for h in self.hits()])
        self.assertEqual([g.section_title for g in out.groups], ["איך מגישים", "מה כולל"])
        self.assertEqual(out.groups[0].distance, 0.215)

    def test_without_a_claim_word_nothing_changes(self):
        self.assertEqual(self.order("איך מצטרפים לקרן השתלמות?"), ["מה כולל", "איך מגישים"])

    def test_bonus_can_be_switched_off_and_a_big_gap_is_not_overturned(self):
        self.assertEqual(self.order("איך מגישים תביעה?", claims_bonus=0.0), ["מה כולל", "איך מגישים"])
        far = self.hits()
        far[1]["distance"] = 0.30
        out = r.process_hits("איך מגישים תביעה?", [dict(h) for h in far])
        self.assertEqual(out.groups[0].section_title, "מה כולל")


if __name__ == "__main__":
    unittest.main()


class TestHardProductFilter(unittest.TestCase):
    def hits(self):
        def h(path, d, n):
            return {"id": f"i{n}", "text": f"עמוד > מקטע {n}\n" + "מילה " * 20, "url": "https://www.fnx.co.il" + path, "page_title": "עמוד",
                    "section_title": f"מקטע {n}", "anchor": "", "distance": d, "short": False, "dup_urls": "", "section_id": f"s{n}",
                    "collected_at": "2026-11-04T10:00:00+00:00", "area": "claims"}
        return [h("/claims-customer-service/claims-car-insurance/a/", 0.22, 1),
                h("/claims-customer-service/claims-personal-accident-insurance/", 0.28, 2),
                h("/claims-customer-service/claims-health-insurance/b/", 0.40, 3)]

    def test_hard_keeps_only_the_chosen_product(self):
        out = r.process_hits("שאלה", self.hits(), products=["personal-accident"], hard=True)
        self.assertEqual([g.topic for g in out.groups], ["personal-accident"])
        self.assertFalse(out.product_miss)

    def test_hard_with_nothing_close_enough_is_a_miss_with_no_groups_not_a_fallback_to_all(self):
        out = r.process_hits("שאלה", self.hits(), products=["health-insurance"], hard=True)   # health is at 0.40 > 0.30
        self.assertEqual(out.groups, [])
        self.assertTrue(out.product_miss)
        out = r.process_hits("שאלה", self.hits(), products=["travel-insurance"], hard=True)   # no chunk at all
        self.assertEqual((out.groups, out.product_miss), ([], True))

    def test_a_soft_product_still_falls_back_to_everything_and_flags_the_miss(self):
        out = r.process_hits("שאלה", self.hits(), products=["travel-insurance"])
        self.assertTrue(out.product_miss)
        self.assertGreaterEqual(len(out.groups), 3)

    def test_the_index_search_is_filtered_by_topic_and_an_old_index_falls_back(self):
        calls = []

        class Col:
            def __init__(self, topic_field):
                self.topic_field = topic_field

            def count(self):
                return 50

            def get(self, **kw):
                return {"metadatas": [{"topic": "x"} if self.topic_field else {"area": "claims"}]}

            def query(self, **kw):
                calls.append(kw.get("where"))
                return {"metadatas": [[]], "documents": [[]], "distances": [[]]}

        for field, expected in ((True, {"topic": {"$in": ["personal-accident"]}}), (False, None)):
            ret = r.Retriever(col=Col(field), embedder=object(), cache=object())
            with unittest.mock.patch.object(r.embed, "embed_query", return_value=[0.0]):
                out = ret.retrieve("מה מכסה ביטוח תאונות אישיות?", products=None, hard=True)
            self.assertEqual(calls[-1], expected)
        with unittest.mock.patch.object(r.embed, "embed_query", return_value=[0.0]):
            r.Retriever(col=Col(True), embedder=object(), cache=object()).retrieve("שאלה כללית", products=["car-insurance"], hard=False)
        self.assertIsNone(calls[-1])                                          # inherited: no filter in the search
