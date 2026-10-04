"""Mouse-wheel maths and the nested scrolling pages used by the desktop UI (app.py)."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk
from tkinter import ttk

import app


class WheelUnitsTests(unittest.TestCase):
    def test_windows_notches(self):
        self.assertEqual(app.wheel_units(SimpleNamespace(num="??", delta=-120), 3, "win32"), 3)
        self.assertEqual(app.wheel_units(SimpleNamespace(num="??", delta=120), 3, "win32"), -3)
        self.assertEqual(app.wheel_units(SimpleNamespace(num="??", delta=-240), 3, "win32"), 6)

    def test_partial_notch_still_moves(self):
        self.assertEqual(app.wheel_units(SimpleNamespace(num="??", delta=-30), 3, "win32"), 3)
        self.assertEqual(app.wheel_units(SimpleNamespace(num="??", delta=15), 3, "win32"), -3)

    def test_macos_small_deltas_are_distances(self):
        self.assertEqual(app.wheel_units(SimpleNamespace(num="??", delta=-1), 3, "aqua"), 1)
        self.assertEqual(app.wheel_units(SimpleNamespace(num="??", delta=4), 3, "aqua"), -4)

    def test_x11_buttons(self):
        self.assertEqual(app.wheel_units(SimpleNamespace(num=4, delta=0), 3, "x11"), -3)
        self.assertEqual(app.wheel_units(SimpleNamespace(num=5, delta=0), 3, "x11"), 3)

    def test_not_a_wheel_event(self):
        self.assertEqual(app.wheel_units(SimpleNamespace(num="??", delta=0), 3, "win32"), 0)


class ScrollableTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:  # no display (headless CI)
            self.skipTest(str(error))
        self.root.geometry("400x300+0+0")
        app.install_wheel_router(self.root)
        self.page = app.Scrollable(self.root, increment=10)
        self.page.outer.pack(fill="both", expand=True)
        self.inner_list = app.Scrollable(self.page.inner, increment=20, notch=1)
        self.inner_list.outer.pack(fill="x")
        self.inner_list.canvas.configure(height=100)
        rows = [ttk.Label(self.inner_list.inner, text=f"row {i}") for i in range(30)]
        for row in rows:
            row.pack(anchor="w")
        self.rows = rows
        ttk.Label(self.page.inner, text="below the list\n" * 40).pack()
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def wheel(self, widget, delta):
        """The wheel over `widget`, without depending on where the OS says the pointer is."""
        self.root.update_idletasks()
        event = SimpleNamespace(state=0, num="??", delta=delta, x_root=0, y_root=0)
        app.route_wheel(self.root, event, under=widget)
        self.root.update()

    def test_inner_frame_tracks_canvas_width_and_content_height(self):
        self.assertEqual(self.page.canvas.itemcget(self.page._item, "width"), str(self.page.canvas.winfo_width()))
        self.assertGreater(self.page.canvas.yview()[1] - self.page.canvas.yview()[0], 0)
        self.assertLess(self.page.canvas.yview()[1], 1.0)  # content is taller than the view: scrollable

    def test_wheel_over_a_child_row_scrolls_the_list_not_the_page(self):
        page_before, list_before = self.page.canvas.yview(), self.inner_list.canvas.yview()
        self.wheel(self.rows[1], -120)
        self.assertEqual(self.page.canvas.yview(), page_before)
        self.assertGreater(self.inner_list.canvas.yview()[0], list_before[0])

    def test_list_hands_off_to_the_page_at_its_end(self):
        self.inner_list.canvas.yview_moveto(1.0)
        self.root.update()
        before = self.page.canvas.yview()[0]
        visible = next(r for r in self.rows if r.winfo_rooty() + r.winfo_height() <= self.inner_list.canvas.winfo_rooty() + 100
                       and r.winfo_rooty() >= self.inner_list.canvas.winfo_rooty())
        self.wheel(visible, -120)  # down, but the list is already at the bottom
        self.assertGreater(self.page.canvas.yview()[0], before)

    def test_wheel_up_at_page_top_does_nothing(self):
        self.wheel(self.page.canvas, 120)
        self.assertEqual(self.page.canvas.yview()[0], 0.0)

    def test_wheel_over_text_that_scrolls_itself_is_left_alone(self):
        text = tk.Text(self.page.inner, height=3)
        text.insert("end", "\n".join(str(i) for i in range(100)))
        text.pack()
        self.root.update()
        before = self.page.canvas.yview()
        self.wheel(text, -120)
        self.assertEqual(self.page.canvas.yview(), before)


if __name__ == "__main__":
    unittest.main()
