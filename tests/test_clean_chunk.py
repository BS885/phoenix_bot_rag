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

    def test_short_h3_merge_within_same_h2(self):
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>נושא</h2><h3>א</h3><p>קצר</p><h3>ב</h3><p>{LONG}</p>"))
        cs = chunk.chunk_page(d, "https://www.fnx.co.il/a/")
        self.assertEqual(len(cs), 1)
        self.assertIn("נושא > א", cs[0]["section_title"])

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
        big = "מילה " * 480
        d = clean.clean_html(page(f"<h1>עמוד</h1><h2>א</h2><p>{big}</p><h2>ב</h2><p>קצר קצר</p><h2>ג</h2><p>{big}ו</p>"))
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


if __name__ == "__main__":
    unittest.main()
