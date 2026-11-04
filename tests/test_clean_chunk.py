import unittest

import config
from ingest import chunk, clean

LONG = "מילה " * 80  # 80 words, > MIN_CHUNK_WORDS


def page(body: str) -> str:
    return f"<html><head><title>t</title></head><body><nav>תפריט</nav><main>{body}</main><footer>f</footer></body></html>"


class TestClean(unittest.TestCase):
    def test_removes_noise_and_keeps_structure(self):
        d = clean.clean_html(page(f"<h1>כותרת</h1><script>x()</script><h2>מקטע</h2><p>{LONG}</p><ul><li>א</li><li>ב</li></ul>"))
        self.assertEqual(d["title"], "כותרת")
        self.assertEqual([b["type"] for b in d["blocks"]], ["h1", "h2", "p", "li", "li"])
        self.assertNotIn("תפריט", " ".join(b["text"] for b in d["blocks"]))

    def test_nested_list_keeps_parent_text_once(self):
        d = clean.clean_html(page("<h1>x</h1><ul><li>הורה<ul><li>ילד</li></ul></li></ul>"))
        texts = [b["text"] for b in d["blocks"] if b["type"] == "li"]
        self.assertEqual(texts, ["הורה", "ילד"])

    def test_short_page_is_empty(self):
        self.assertTrue(clean.clean_html(page("<h1>x</h1><p>קצר</p>"))["empty"])

    def test_zero_width_removed(self):
        d = clean.clean_html(page(f"<h1>x</h1><p>a​b {LONG}</p>"))
        self.assertTrue(d["blocks"][1]["text"].startswith("ab "))

    def test_strip_repeated_when_enabled_keeps_headings(self):
        docs = [clean.clean_html(page(f"<h1>p{i}</h1><h2>קבוע</h2><p>{LONG}{i}</p><p>באנר קבוע</p>")) for i in range(6)]
        config.STRIP_REPEATED_BLOCKS = True
        try:
            out = clean.strip_repeated(docs)
        finally:
            config.STRIP_REPEATED_BLOCKS = False
        for d in out:
            texts = [b["text"] for b in d["blocks"]]
            self.assertNotIn("באנר קבוע", texts)
            self.assertIn("קבוע", texts)  # the h2 heading survives

    def test_strip_repeated_off_by_default(self):
        docs = [clean.clean_html(page(f"<h1>p{i}</h1><p>{LONG}{i}</p><p>באנר קבוע</p>")) for i in range(6)]
        out = clean.strip_repeated(docs)
        self.assertIn("באנר קבוע", [b["text"] for b in out[0]["blocks"]])


def texts(html: str) -> list[str]:
    return [b["text"] for b in clean.clean_html(page(html))["blocks"]]


class TestLinks(unittest.TestCase):
    def test_standalone_link_label_and_address(self):
        t = texts('<h1>x</h1><section><a href="https://forms.fnx.co.il/f?x=1">טופס הגשת תביעה</a></section>')
        self.assertIn("טופס הגשת תביעה (https://forms.fnx.co.il/f?x=1)", t)

    def test_relative_link_made_absolute(self):
        t = texts('<h1>x</h1><div><a href="/ituran/doctors/">איתור רופאים</a></div>')
        self.assertIn("איתור רופאים (https://www.fnx.co.il/ituran/doctors/)", t)

    def test_standalone_tel_and_mailto_links(self):
        t = texts('<h1>x</h1><div><a href="tel:*3455">התקשרו אלינו</a></div><div><a href="mailto:a@fnx.co.il">כתבו לנו</a></div>')
        self.assertIn("התקשרו אלינו (tel:*3455)", t)
        self.assertIn("כתבו לנו (mailto:a@fnx.co.il)", t)

    def test_anchor_and_script_links_skipped(self):
        t = texts('<h1>x</h1><div><a href="#tab-2">לשונית</a></div><div><a href="javascript:void(0)">פתח</a></div><div><a href="#">סרטון</a></div>')
        self.assertEqual(t, ["x"])

    def test_inline_link_not_duplicated_as_block(self):
        t = texts('<h1>x</h1><p>ראו את <a href="https://a.co.il/z">הטופס</a> המצורף</p>')
        self.assertEqual(t, ["x", "ראו את הטופס המצורף"])

    def test_inline_tel_link_gets_number_when_label_hides_it(self):
        t = texts('<h1>x</h1><p>לשירות <a href="tel:*3455">חייגו עכשיו</a> בכל שעה</p>')
        self.assertIn("לשירות חייגו עכשיו (tel:*3455) בכל שעה", t)

    def test_inline_tel_link_unchanged_when_number_shown(self):
        t = texts('<h1>x</h1><p>טלפון <a href="tel:03-7336946">03-7336946</a></p>')
        self.assertIn("טלפון 03-7336946", t)


class TestNoiseAndLooseText(unittest.TestCase):
    def test_breadcrumbs_removed_nav_or_not(self):
        t = texts('<h1>x</h1><nav class="breadcrumbs"><ul><li><a href="/">דף הבית</a></li></ul></nav>'
                  '<div class="breadcrumbs"><a href="/">עמוד אב</a></div><p>תוכן אמיתי</p>')
        self.assertEqual(t, ["x", "תוכן אמיתי"])

    def test_carousel_indicator_dropped_but_promo_text_kept(self):
        t = texts('<h1>x</h1><div><span>שקופית מספר 1 מתוך 3</span><span>הטבה למצטרפים חדשים עד 31.10.26</span>'
                  '<span>שקופית מספר 2 מתוך 3</span></div>')
        self.assertEqual(t, ["x", "הטבה למצטרפים חדשים עד 31.10.26"])

    def test_loose_inline_fragments_joined_into_one_block(self):
        t = texts('<h1>x</h1><div>הטבה <b>מיוחדת</b> לחברים</div>')
        self.assertEqual(t, ["x", "הטבה מיוחדת לחברים"])

    def test_digits_only_loose_text_ignored(self):
        self.assertEqual(texts('<h1>x</h1><div><span>1</span><span>2</span></div>'), ["x"])


class TestHeadings(unittest.TestCase):
    def test_h1_h2_inside_header_kept(self):
        d = clean.clean_html(page('<header><h1>כותרת</h1></header><header><h2>מקטע</h2></header><p>טקסט</p>'))
        self.assertEqual([b["type"] for b in d["blocks"]], ["h1", "h2", "p"])

    def test_sticky_nav_heading_removed_by_design(self):
        t = texts('<h1>x</h1><nav class="sticky-nav"><header><h2>איך נוכל לעזור לך?</h2></header>'
                  '<ul><li><a href="#a">למצוא רופא</a></li></ul></nav><h2>תוכן</h2>')
        self.assertEqual(t, ["x", "תוכן"])

    def test_heading_inside_list_item_kept_in_item_text(self):
        t = texts('<h1>x</h1><ul><li><h2>ניתוחים בישראל</h2><p>תיאור הכרטיס</p></li></ul>')
        self.assertEqual(t, ["x", "ניתוחים בישראל תיאור הכרטיס"])


class TestChunk(unittest.TestCase):
    def test_split_by_headings_with_title_prefix(self):
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>א</h2><p>{LONG}</p><h2>ב</h2><p>{LONG}</p>"))
        cs = chunk.chunk_page(d, "https://www.fnx.co.il/claims-customer-service/x/")
        self.assertEqual(len(cs), 2)
        self.assertTrue(cs[0]["text"].startswith("עמוד > א\n"))
        self.assertEqual(cs[0]["area"], "claims")

    def test_short_sections_of_different_topics_do_not_merge(self):
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>א</h2><p>קצר</p><h2>ב</h2><p>{LONG}</p>"))
        cs = chunk.chunk_page(d, "https://www.fnx.co.il/a/")
        self.assertEqual(len(cs), 2)
        self.assertEqual([c["section_title"] for c in cs], ["א", "ב"])

    def test_sibling_h3_never_merged(self):
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>שאלות</h2><h3>שאלה א</h3><p>תשובה קצרה</p>"
                                  f"<h3>שאלה ב</h3><p>תשובה קצרה אחרת</p>"))
        cs = chunk.chunk_page(d, "https://www.fnx.co.il/a/")
        self.assertEqual([c["section_title"] for c in cs], ["שאלות > שאלה א", "שאלות > שאלה ב"])
        self.assertTrue(all(c["short"] for c in cs))

    def test_short_flag_false_for_long_chunk(self):
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>א</h2><p>{LONG}</p>"))
        self.assertFalse(chunk.chunk_page(d, "https://www.fnx.co.il/a/")[0]["short"])

    def test_long_section_splits_by_paragraph_not_inside_list(self):
        para = "מילה " * 300
        item = "פריט " * 300
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>א</h2><p>{para}</p><ul><li>{item}</li><li>עוד</li></ul>"))
        cs = chunk.chunk_page(d, "https://www.fnx.co.il/a/")
        self.assertEqual(len(cs), 2)
        self.assertIn("- עוד", cs[1]["text"])

    def test_oversized_paragraph_cut_between_sentences_with_overlap(self):
        sentences = [f"משפט מספר {i} " + "מילה " * 98 + "." for i in range(12)]  # ~100 words each
        d = clean.clean_html(page("<h1>עמוד</h1><h2>א</h2><p>" + " ".join(sentences) + "</p>"))
        cs = chunk.chunk_page(d, "https://www.fnx.co.il/a/")
        self.assertGreater(len(cs), 1)
        for c in cs:
            self.assertLessEqual(len(c["text"].split()), config.MAX_CHUNK_WORDS + 10)
        # last sentence of chunk 1 opens chunk 2 (overlap)
        last = cs[0]["text"].split("\n")[-1].split(". ")[-1][:20]
        self.assertIn(last.strip(" ."), cs[1]["text"])

    def test_merge_never_exceeds_max(self):
        big = "מילה " * 200
        mid = "אמצע " * 40
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>א</h2><p>{big}</p><p>{big}ו</p><p>{mid}</p><p>קצר קצר</p>"
                                  f"<h2>ב</h2><p>קצר קצר</p><h2>ג</h2><p>{big}ז</p>"))
        for c in chunk.chunk_page(d, "https://www.fnx.co.il/a/"):
            self.assertLessEqual(len(c["text"].split()), config.MAX_CHUNK_WORDS + 10)

    def test_boilerplate_section_dropped(self):
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>א</h2><p>{LONG}</p><h2>כל הדרכים לדבר איתנו</h2><p>{LONG}</p>"))
        cs = chunk.chunk_page(d, "https://www.fnx.co.il/a/")
        self.assertEqual(len(cs), 1)
        self.assertNotIn("כל הדרכים", cs[0]["text"])

    def test_ids_stable(self):
        d = clean.clean_html(page(f"<h1>עמוד</h1><p>{LONG}</p>"))
        u = "https://www.fnx.co.il/a/"
        self.assertEqual(chunk.chunk_page(d, u)[0]["id"], chunk.chunk_page(d, u)[0]["id"])


TABLE = ("<table><tr><th>שם</th><th>תשואה</th><th>נכסים</th></tr>"
         "<tr><th>קרן א מידע נוסף על המסלול</th><td>תשואה 4.1%</td><td>100</td></tr>"
         "<tr><th>קרן ב מידע נוסף על המסלול</th><td>תשואה 5.2%</td><td>200</td></tr></table>")


class TestTables(unittest.TestCase):
    def test_header_row_not_emitted_and_unlabeled_cells_get_column_names(self):
        rows = [b["text"] for b in clean.clean_html(page("<h1>x</h1>" + TABLE))["blocks"] if b["type"] == "row"]
        self.assertEqual(len(rows), 2)
        # the row-header cell (name) is left as is; the other cells get their column names
        self.assertEqual(rows[0], "קרן א מידע נוסף על המסלול | תשואה 4.1% | נכסים 100")

    def test_table_without_header_row_untouched(self):
        d = clean.clean_html(page("<h1>x</h1><table><tr><td>א</td><td>1</td></tr></table>"))
        self.assertEqual([b["text"] for b in d["blocks"] if b["type"] == "row"], ["א | 1"])

    def test_one_chunk_per_row_with_name_in_title(self):
        d = clean.clean_html(page("<h1>מסלולים</h1><h2>נתונים לתאריך 30.09</h2>" + TABLE + "<p>הערה כללית אחרי הטבלה</p>"))
        cs = chunk.chunk_page(d, "https://www.fnx.co.il/a/")
        titles = [c["section_title"] for c in cs]
        self.assertIn("נתונים לתאריך 30.09 > קרן א", titles)
        self.assertIn("נתונים לתאריך 30.09 > קרן ב", titles)
        row_a = next(c for c in cs if c["section_title"].endswith("קרן א"))
        self.assertIn("תשואה 4.1%", row_a["text"])
        self.assertNotIn("4.1%", next(c for c in cs if c["section_title"].endswith("קרן ב"))["text"])
        self.assertNotIn("הערה כללית", row_a["text"])  # text after the table is not glued to the last row
        self.assertTrue(any("הערה כללית" in c["text"] for c in cs))

    def test_teaser_section_dropped(self):
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>א</h2><p>{LONG}</p><h2>אולי יעניין אותך</h2><h3>מאמר</h3><p>{LONG}</p>"))
        cs = chunk.chunk_page(d, "https://www.fnx.co.il/a/")
        self.assertEqual(len(cs), 1)
        self.assertNotIn("מאמר", cs[0]["text"])


class TestLinkOnlySections(unittest.TestCase):
    def build(self, body: str):
        return chunk.chunk_page(clean.clean_html(page(body)), "https://www.fnx.co.il/a/")

    def test_section_of_only_links_dropped(self):
        cs = self.build(f'<h1>עמוד</h1><h2>תוכן</h2><p>{LONG}</p>'
                        '<h2>קישורים</h2><div><a href="/x/">קישור א</a></div><div><a href="/y/">קישור ב</a></div>')
        self.assertEqual([c["section_title"] for c in cs], ["תוכן"])

    def test_section_with_a_sentence_and_links_kept(self):
        cs = self.build('<h1>עמוד</h1><h2>קישורים</h2><p>לטפסים המלאים אפשר להיכנס מכאן.</p><div><a href="/x/">טופס</a></div>')
        self.assertEqual(len(cs), 1)
        self.assertIn("טופס (https://www.fnx.co.il/x/)", cs[0]["text"])

    def test_rule_can_be_switched_off(self):
        config.DROP_LINK_ONLY_SECTIONS = False
        try:
            cs = self.build('<h1>עמוד</h1><h2>קישורים</h2><div><a href="/x/">קישור</a></div>')
        finally:
            config.DROP_LINK_ONLY_SECTIONS = True
        self.assertEqual(len(cs), 1)


class TestSectionIdAndAnchor(unittest.TestCase):
    URL = "https://www.fnx.co.il/a/"

    def chunks(self, body: str, meta=None):
        return chunk.chunk_page(clean.clean_html(page(body)), self.URL, meta)

    def test_section_ids_unique_and_stable(self):
        body = f"<h1>עמוד</h1><h2>א</h2><p>{LONG}</p><h2>ב</h2><p>{LONG}</p>"
        a, b = self.chunks(body), self.chunks(body)
        self.assertEqual([c["section_id"] for c in a], [c["section_id"] for c in b])
        self.assertEqual(len({c["section_id"] for c in a}), len(a))

    def test_duplicate_titles_get_different_section_ids(self):
        cs = self.chunks(f"<h1>עמוד</h1><h2>זהה</h2><p>{LONG}</p><h2>זהה</h2><p>{LONG}x</p>")
        self.assertEqual(len({c["section_id"] for c in cs}), 2)

    def test_anchor_from_wrapper_id_and_inherited_by_h3(self):
        cs = self.chunks(f'<h1>עמוד</h1><section id="faq"><h2>שאלות</h2><p>{LONG}</p></section>'
                         f'<h2>אחר</h2><p>{LONG}y</p>')
        by_title = {c["section_title"]: c for c in cs}
        self.assertEqual(by_title["שאלות"]["anchor"], "faq")
        self.assertEqual(by_title["אחר"]["anchor"], "")

    def test_anchor_is_empty_string_never_none(self):
        cs = self.chunks(f"<h1>עמוד</h1><h2>א</h2><p>{LONG}</p>")
        self.assertEqual(cs[0]["anchor"], "")

    def test_merged_chunk_keeps_first_section_id(self):
        cs = self.chunks(f"<h1>עמוד</h1><p>פתיחה קצרה</p><h2>א</h2><p>{LONG}</p>")
        self.assertEqual(len(cs), 1)
        intro_id = chunk.hashlib.sha1(f"{self.URL}|עמוד|0".encode("utf-8")).hexdigest()[:12]
        self.assertEqual(cs[0]["section_id"], intro_id)

    def test_no_none_in_metadata(self):
        for meta in (None, {}, {"collected_at": None, "lastmod": None}, {"collected_at": "2026-01-01", "lastmod": "x"}):
            for c in self.chunks(f"<h1>עמוד</h1><h2>א</h2><p>{LONG}</p>", meta):
                for key, value in c.items():
                    self.assertIsNotNone(value, key)
                    self.assertIsInstance(value, (str, int, float, bool), key)


class TestStage2Fixes(unittest.TestCase):
    CLAIMS = "https://www.fnx.co.il/claims-customer-service/x/"
    OTHER = "https://www.fnx.co.il/health-insurance/x/"

    def chunks(self, body: str, url: str):
        return chunk.chunk_page(clean.clean_html(page(body)), url)

    LINKS = '<h1>עמוד</h1><h2>טפסים</h2><div><a href="/f/">טופס א</a></div><div><a href="/g/">טופס ב</a></div>'

    def test_link_only_section_kept_in_claims_area_dropped_elsewhere(self):
        kept = self.chunks(self.LINKS, self.CLAIMS)
        self.assertEqual(len(kept), 1)
        self.assertIn("טופס א (https://www.fnx.co.il/f/)", kept[0]["text"])
        self.assertEqual(self.chunks(self.LINKS, self.OTHER), [])

    def test_exact_duplicate_inside_a_section_removed_but_not_across_sections(self):
        body = (f"<h1>עמוד</h1><h2>א</h2><ul><li>שלב אחד</li><li>שלב שניים</li></ul><p>{LONG}</p>"
                f"<ul><li>שלב אחד</li><li>שלב שניים</li></ul>"
                f"<h2>ב</h2><ul><li>שלב אחד</li></ul><p>{LONG}ב</p>")
        cs = {c["section_title"]: c for c in self.chunks(body, self.OTHER)}
        self.assertEqual(cs["א"]["text"].count("שלב אחד"), 1)
        self.assertEqual(cs["ב"]["text"].count("שלב אחד"), 1)

    def test_cta_label_line_removed(self):
        cs = self.chunks(f"<h1>עמוד</h1><h2>א</h2><p>{LONG}</p><p>הגשת תביעה</p>", self.OTHER)
        self.assertNotIn("\nהגשת תביעה", cs[0]["text"])

    def test_link_with_same_label_as_cta_keeps_its_address(self):
        cs = self.chunks(f'<h1>עמוד</h1><h2>א</h2><p>{LONG}</p><p>הגשת תביעה</p><div><a href="/c/">הגשת תביעה</a></div>', self.OTHER)
        self.assertIn("הגשת תביעה (https://www.fnx.co.il/c/)", cs[0]["text"])

    def test_href_kept_for_links_inside_list_items_only(self):
        d = clean.clean_html(page('<h1>x</h1><ul><li>מלאו את <a href="/form/">הטופס</a> ושלחו</li></ul>'
                                  '<p>ראו את <a href="/form/">הטופס</a> ושלחו</p>'))
        texts_ = [b["text"] for b in d["blocks"]]
        self.assertIn("מלאו את הטופס (https://www.fnx.co.il/form/) ושלחו", texts_)
        self.assertIn("ראו את הטופס ושלחו", texts_)

    def test_li_anchor_link_without_real_target_unchanged(self):
        d = clean.clean_html(page('<h1>x</h1><ul><li>ראו <a href="#faq">למטה</a> או <a href="javascript:void(0)">כאן</a></li></ul>'))
        self.assertIn("ראו למטה או כאן", [b["text"] for b in d["blocks"]])

    def test_intro_title_not_doubled(self):
        cs = self.chunks(f"<h1>עמוד</h1><p>פתיחה קצרה</p><h2>איך מגישים</h2><p>{LONG}</p>", self.OTHER)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["section_title"], "איך מגישים")
        self.assertTrue(cs[0]["text"].startswith("עמוד > איך מגישים\n"))
        self.assertNotIn("עמוד > עמוד", cs[0]["text"])

    def test_max_chunk_words_is_250_and_parts_share_section_id(self):
        self.assertEqual(config.MAX_CHUNK_WORDS, 250)
        paras = "".join(f"<p>{('פסקה%d ' % i) * 120}</p>" for i in range(5))  # 5 paragraphs of 120 words
        cs = self.chunks(f"<h1>עמוד</h1><h2>ארוך</h2>{paras}", self.OTHER)
        self.assertGreaterEqual(len(cs), 3)
        for c in cs:
            self.assertLessEqual(len(c["text"].split()), config.MAX_CHUNK_WORDS + 10)
        self.assertEqual(len({c["section_id"] for c in cs}), 1)
        self.assertEqual(len({c["id"] for c in cs}), len(cs))


if __name__ == "__main__":
    unittest.main()
