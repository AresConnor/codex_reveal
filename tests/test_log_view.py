import unittest

from reveal.routing import display_item_number
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
