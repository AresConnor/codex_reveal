import unittest

from textual.app import App, ComposeResult
from textual.strip import Strip

from reveal.widgets.virtual_content import VirtualContentSource, VirtualContentView


class VirtualContentApp(App):
    def __init__(self, text: str):
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        source = VirtualContentSource.from_text(self._text)
        yield VirtualContentView(source, id="vc")


class VirtualContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_line_returns_strip(self):
        text = "\n".join(
            f"{i}.1  response.output_item.added  ts=1784220813.248 seq={i} log_id=22450934"
            for i in range(1, 40)
        )
        app = VirtualContentApp(text)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            view = app.query_one("#vc", VirtualContentView)
            line = view.render_line(0)
            self.assertIsInstance(line, Strip)
            # Regression: Textual styles cache requires Strip.adjust_cell_length.
            adjusted = line.adjust_cell_length(84)
            self.assertIsInstance(adjusted, Strip)
            self.assertEqual(adjusted.cell_length, 84)

            blank = view.render_line(10_000)
            self.assertIsInstance(blank, Strip)
            self.assertEqual(blank.adjust_cell_length(84).cell_length, 84)

    async def test_render_line_with_search_highlight(self):
        app = VirtualContentApp("alpha beta gamma\nbeta again")
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            view = app.query_one("#vc", VirtualContentView)
            count, err = view.set_search("beta")
            self.assertEqual(err, "")
            self.assertEqual(count, 2)
            line = view.render_line(0)
            self.assertIsInstance(line, Strip)
            self.assertEqual(line.adjust_cell_length(60).cell_length, 60)
