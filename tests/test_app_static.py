"""unsafe_allow_html may only carry the static CSS constant of app.py; no dynamic text may ever pass through it (stage 6.3)."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def calls_with_html_flag(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "unsafe_allow_html":
                    out.append((node, kw))
    return out


def module_constants(tree):
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            consts[node.targets[0].id] = node.value
    return consts


class TestUnsafeAllowHtml(unittest.TestCase):
    def test_there_is_exactly_one_use_and_it_is_st_markdown_with_a_static_constant(self):
        uses = calls_with_html_flag(TREE)
        self.assertEqual(len(uses), 1, "unsafe_allow_html must be used exactly once (the static CSS)")
        call, kw = uses[0]
        self.assertIsInstance(kw.value, ast.Constant)
        self.assertIs(kw.value.value, True)
        f = call.func
        self.assertTrue(isinstance(f, ast.Attribute) and f.attr == "markdown" and isinstance(f.value, ast.Name) and f.value.id == "st")
        self.assertEqual(len(call.args), 1)
        arg = call.args[0]
        self.assertIsInstance(arg, ast.Name, "the HTML argument must be a module-level constant, not an expression")
        value = module_constants(TREE)[arg.id]
        self.assertIsInstance(value, ast.Constant, "the constant must be a plain string literal: no f-string, no concatenation")
        self.assertIsInstance(value.value, str)

    def test_the_css_constant_is_only_css(self):
        arg = calls_with_html_flag(TREE)[0][0].args[0]
        css = module_constants(TREE)[arg.id].value
        self.assertTrue(css.strip().startswith("<style>") and css.strip().endswith("</style>"))
        lowered = css.lower()
        for bad in ("<script", "javascript:", "url(", "@import", "expression(", "<iframe", "<img", "onerror", "onload"):
            self.assertNotIn(bad, lowered, bad)
        self.assertEqual(lowered.count("<style>"), 1)

    def test_no_other_way_to_inject_html(self):
        for node in ast.walk(TREE):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, {"html", "iframe"}, f"st.{node.func.attr} must not be used")
        for forbidden in ("components.v1", "st.html(", "dangerouslySetInnerHTML", "innerHTML", "unsafe_allow_javascript"):
            self.assertNotIn(forbidden, SOURCE)

    def test_no_dynamic_text_goes_into_the_html_call(self):
        # the only st.markdown with an f-string / call / format as the first argument must NOT carry the html flag
        for node in ast.walk(TREE):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "markdown":
                flagged = any(kw.arg == "unsafe_allow_html" for kw in node.keywords)
                dynamic = node.args and not (isinstance(node.args[0], (ast.Constant, ast.Name)))
                self.assertFalse(flagged and dynamic)

    def test_other_modules_do_not_use_the_flag_either(self):
        for path in ROOT.glob("*.py"):
            if path.name == "app.py":
                continue
            self.assertNotIn("unsafe_allow_html", path.read_text(encoding="utf-8"), path.name)
        for path in (ROOT / "bot").glob("*.py"):
            self.assertNotIn("unsafe_allow_html", path.read_text(encoding="utf-8"), path.name)

    def test_app_py_keeps_the_privacy_and_missing_rules(self):
        self.assertIn("privacy.mask", SOURCE)
        self.assertNotIn(".missing", SOURCE)               # the model's missing text is never read by the UI


if __name__ == "__main__":
    unittest.main()
