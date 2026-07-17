import unittest

from reveal.routing import display_item_number
from reveal.widgets.log_view import format_history_token_line
from reveal.widgets.virtual_content import VirtualContentSource, is_large_content


class VirtualContentTests(unittest.TestCase):
    def test_display_line_mapping(self):
        src = VirtualContentSource(rows=["hello", "world"])
        src.set_width(80)
        self.assertEqual(src.display_line(0)[2], "hello")
        self.assertEqual(src.display_line(1)[2], "world")

    def test_large_threshold(self):
        small = "x\n" * 10
        self.assertFalse(is_large_content(small))
        big = "x\n" * 250
        self.assertTrue(is_large_content(big))

    def test_search_plain(self):
        src = VirtualContentSource(rows=["alpha", "beta alpha", "gamma"])
        src.set_width(80)
        matches = src.search("alpha")
        self.assertEqual(len(matches), 2)

    def test_search_invalid_regex_returns_empty(self):
        src = VirtualContentSource(rows=["abc"])
        src.set_width(80)
        self.assertEqual(src.search("(", regex=True), [])

    def test_item_numbers(self):
        self.assertEqual(display_item_number(0), 1)


class HistoryTokenLineTests(unittest.TestCase):
    def test_cached_rate_percent(self):
        line = format_history_token_line(
            {
                "input_tokens": 1000,
                "cached_input_tokens": 250,
                "output_tokens": 40,
            }
        )
        self.assertIn("tokens: in=1000", line)
        self.assertIn("cached=250", line)
        self.assertIn("out=40", line)
        self.assertIn("cached_rate=25.0%", line)

    def test_omit_rate_when_input_zero(self):
        line = format_history_token_line(
            {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 1,
            }
        )
        self.assertIn("tokens: in=0", line)
        self.assertNotIn("cached_rate=", line)

    def test_omit_rate_when_input_missing(self):
        line = format_history_token_line({"output_tokens": 3})
        self.assertIn("tokens: in=?", line)
        self.assertNotIn("cached_rate=", line)

