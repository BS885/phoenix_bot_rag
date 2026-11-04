import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

import bootstrap  # noqa: F401
from ingest import fetch

ROBOTS = mock.Mock(text="User-agent: *\nDisallow: /private/\n")


def run_cli(url: str):
    """Run `fetch --url <url>` with the network and the downloader mocked."""
    buf = io.StringIO()
    with mock.patch.object(fetch, "get_with_retry", return_value=ROBOTS), \
            mock.patch.object(fetch, "fetch_pages", return_value={
                "downloaded": ["u"], "skipped": [], "not_found": [], "failed": [], "blocked": [],
                "stopped": None}) as fp, \
            mock.patch.object(sys, "argv", ["fetch", "--url", url]), \
            redirect_stdout(buf):
        fetch.main()
    return buf.getvalue(), fp


class TestUrlMode(unittest.TestCase):
    def test_allowed_url_is_fetched_without_fragment(self):
        _, fp = run_cli("https://www.fnx.co.il/health-insurance/extra-covers/#faq")
        fp.assert_called_once()
        self.assertEqual(fp.call_args[0][0][0]["url"], "https://www.fnx.co.il/health-insurance/extra-covers/")

    def test_robots_disallowed_url_refused(self):
        out, fp = run_cli("https://www.fnx.co.il/private/x/")
        fp.assert_not_called()
        self.assertIn("refused", out)

    def test_filtered_url_refused(self):
        out, fp = run_cli("https://www.fnx.co.il/en/page/")
        fp.assert_not_called()
        self.assertIn("refused", out)


if __name__ == "__main__":
    unittest.main()
