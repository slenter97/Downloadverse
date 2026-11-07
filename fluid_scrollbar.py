"""Scrollbar personalizzata in stile Windows 11.

Sottile e senza frecce, si ingrandisce quando il puntatore le passa sopra.
Viene caricata solo quando Tkinter è disponibile, per non rompere l'uso
headless della logica di download.
"""

import tkinter as tk

# Pillow e il renderer vengono caricati solo quando serve disegnare il thumb.
ImageTk = None
ui_render = None


class FluidScrollbar(tk.Canvas):
    """Scrollbar fluida: thumb arrotondato che si allarga al passaggio del mouse."""

    THICKNESS = 14
    IDLE_WIDTH = 4
    HOVER_WIDTH = 10
    MIN_LENGTH = 28
    COLOR_IDLE = "#4f4f4f"
    COLOR_HOVER = "#909090"

    def __init__(self, master, orient="vertical", command=None, bg=None, **kwargs):
        if orient in ("vertical", "v"):
            width, height = self.THICKNESS, 1
        else:
            width, height = 1, self.THICKNESS
        if bg is None:
            try:
                bg = master.cget("bg")
            except Exception:
                bg = "#1a1a1a"
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=0, **kwargs)
        self.orient = "vertical" if orient in ("vertical", "v") else "horizontal"
        self.command = command
        self._first = 0.0
        self._last = 1.0
        self._hover = False
        self._dragging = False
        self._thumb_width = self.IDLE_WIDTH
        self._anim_job = None
        self._drag_offset = 0.0
        self._thumb_cache = {}
        self.configure(cursor="arrow")
        self.bind("<Configure>", lambda _event: self._redraw())
        self.bind("<Map>", lambda _event: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    # Stessa interfaccia di ttk.Scrollbar.
    def set(self, first, last):
        try:
            first = float(first)
            last = float(last)
        except (TypeError, ValueError):
            return
        self._first = min(max(first, 0.0), 1.0)
        self._last = min(max(last, 0.0), 1.0)
        self._redraw()

    def _visible(self):
        return max(0.0, min(1.0, self._last - self._first))

    def _track_length(self):
        if self.orient == "vertical":
            return max(1, self.winfo_height() - 4)
        return max(1, self.winfo_width() - 4)

    def _geometry(self):
        pad = 2
        track = self._track_length()
        visible = self._visible()
        length = max(self.MIN_LENGTH, track * visible)
        length = min(length, track)
        offset = pad + self._first * track
        offset = min(max(offset, pad), pad + track - length)
        return pad, track, length, offset, visible

    def _redraw(self):
        self.delete("thumb")
        if not self.winfo_ismapped():
            return
        pad, track, length, offset, visible = self._geometry()
        if visible >= 0.999:
            return
        thickness = self._thumb_width
        hover = self._hover or self._dragging
        photo = self._thumb_photo(thickness, length, hover)
        if photo is not None:
            if self.orient == "vertical":
                x = (self.winfo_width() - thickness) / 2
                self.create_image(x, offset, anchor="nw", image=photo, tags="thumb")
            else:
                y = (self.winfo_height() - thickness) / 2
                self.create_image(offset, y, anchor="nw", image=photo, tags="thumb")
            return
        if self.orient == "vertical":
            x0 = (self.winfo_width() - thickness) / 2
            x1 = x0 + thickness
            y0, y1 = offset, offset + length
        else:
            y0 = (self.winfo_height() - thickness) / 2
            y1 = y0 + thickness
            x0, x1 = offset, offset + length
        color = self.COLOR_HOVER if hover else self.COLOR_IDLE
        radius = thickness / 2
        if self.orient == "vertical":
            self.create_oval(x0, y0, x1, y0 + thickness, fill=color, outline="", tags="thumb")
            self.create_oval(x0, y1 - thickness, x1, y1, fill=color, outline="", tags="thumb")
            self.create_rectangle(x0, y0 + radius, x1, y1 - radius,
                                  fill=color, outline="", tags="thumb")
        else:
            self.create_oval(x0, y0, x0 + thickness, y1, fill=color, outline="", tags="thumb")
            self.create_oval(x1 - thickness, y0, x1, y1, fill=color, outline="", tags="thumb")
            self.create_rectangle(x0 + radius, y0, x1 - radius, y1,
                                  fill=color, outline="", tags="thumb")

    def _thumb_photo(self, width, height, hover):
        global ImageTk, ui_render
        if ImageTk is None or ui_render is None:
            try:
                from PIL import ImageTk as _ImageTk

                import ui_render as _ui_render
            except Exception:
                return None
            ImageTk, ui_render = _ImageTk, _ui_render
        key = (int(round(width)), int(round(height)), bool(hover))
        photo = self._thumb_cache.get(key)
        if photo is None:
            if len(self._thumb_cache) > 60:
                self._thumb_cache.clear()
            photo = ImageTk.PhotoImage(
                ui_render.pill_thumb(key[0], key[1], hover))
            self._thumb_cache[key] = photo
        return photo

    def _animate_width(self, target):
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        if self._thumb_width == target:
            return
        step = 2 if target > self._thumb_width else -2

        def tick():
            self._anim_job = None
            value = self._thumb_width + step
            if (step > 0 and value >= target) or (step < 0 and value <= target):
                value = target
            self._thumb_width = value
            self._redraw()
            if value != target:
                self._anim_job = self.after(12, tick)

        self._anim_job = self.after(12, tick)

    def _on_enter(self, _event):
        self._hover = True
        self._animate_width(self.HOVER_WIDTH)

    def _on_leave(self, _event):
        self._hover = False
        if not self._dragging:
            self._animate_width(self.IDLE_WIDTH)

    def _pointer(self, event):
        return event.y if self.orient == "vertical" else event.x

    def _on_press(self, event):
        if self._visible() >= 0.999 or self.command is None:
            return
        pad, track, length, offset, visible = self._geometry()
        pos = self._pointer(event)
        if offset <= pos <= offset + length:
            self._drag_offset = (pos - offset) / track
        else:
            self._drag_offset = visible / 2
            self._scroll_to(pos)
        self._dragging = True
        self._redraw()

    def _scroll_to(self, pos):
        pad, track, length, offset, visible = self._geometry()
        first = (pos - pad) / track - self._drag_offset
        first = max(0.0, min(first, max(0.0, 1.0 - visible)))
        if abs(first - self._first) < 1e-6:
            return
        self._first = first
        self._last = first + visible
        try:
            self.command("moveto", first)
        except Exception:
            pass
        self._redraw()

    def _on_drag(self, event):
        if self._dragging:
            self._scroll_to(self._pointer(event))

    def _on_release(self, _event):
        self._dragging = False
        if not self._hover:
            self._animate_width(self.IDLE_WIDTH)
        self._redraw()
