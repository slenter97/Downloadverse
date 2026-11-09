#!/usr/bin/env python3
# Dipendenze opzionali per siti protetti (fallback automatico alle librerie base):
#   python -m pip install curl_cffi
#   python -m pip install cloudscraper
#   python -m pip install browser_cookie3  # lettura best-effort cookie browser
"""Gestore di download con coda, priorità e interfaccia Tkinter.

Dipendenze: solo libreria standard di Python. La logica di trasferimento è
indipendente dalla GUI e comunica gli aggiornamenti tramite queue.Queue.
"""

APP_VERSION = "1.0.1"

import email.message
import base64
import hashlib
import importlib
import json
import logging
import math
import locale
import os
import queue
import re
import shutil
import subprocess
import stat
import sys
import tempfile
import threading
import time
import zlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import html as html_module
from html.parser import HTMLParser
from http.cookies import SimpleCookie


class _LazyModule:
    """Modulo caricato alla prima operazione, per ridurre la RAM all'avvio."""

    def __init__(self, name):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_module", None)

    def _load(self):
        module = object.__getattribute__(self, "_module")
        if module is None:
            module = importlib.import_module(object.__getattribute__(self, "_name"))
            object.__setattr__(self, "_module", module)
        return module

    def __getattr__(self, item):
        return getattr(self._load(), item)


# requests (e urllib3) pesano diversi MB e servono solo quando si usa la rete.
requests = _LazyModule("requests")

# I moduli pesanti o opzionali vengono caricati solo al primo utilizzo:
# all'avvio restano fuori dalla memoria finché non servono davvero.
pystray = Image = ImageDraw = None
ui_render = None
ImageTk = None
curl_requests = None
cloudscraper = None
browser_cookie3 = None
_CryptoAES = None

_OPTIONAL_MODULES = {}


def _load_optional(name):
    """Importa un modulo solo al primo utilizzo e ne conserva il risultato."""
    if name in _OPTIONAL_MODULES:
        return _OPTIONAL_MODULES[name]
    try:
        module = importlib.import_module(name)
    except Exception:
        module = None
    _OPTIONAL_MODULES[name] = module
    return module


def _load_curl_cffi():
    global curl_requests
    if curl_requests is None:
        curl_requests = _load_optional("curl_cffi.requests")
    return curl_requests


def _load_cloudscraper():
    global cloudscraper
    if cloudscraper is None:
        cloudscraper = _load_optional("cloudscraper")
    return cloudscraper


def _load_browser_cookie3():
    global browser_cookie3
    if browser_cookie3 is None:
        browser_cookie3 = _load_optional("browser_cookie3")
    return browser_cookie3


def _load_crypto():
    global _CryptoAES
    if _CryptoAES is None:
        _CryptoAES = _load_optional("Crypto.Cipher.AES")
    return _CryptoAES
# Tkinter viene importato soltanto per creare la finestra: la logica della
# coda resta importabile e testabile anche su server/headless senza Tcl/Tk.
tk = filedialog = messagebox = ttk = None
FluidScrollbar = None


def _load_tkinter():
    global tk, filedialog, messagebox, ttk, FluidScrollbar
    if tk is None:
        import tkinter as _tk
        from tkinter import filedialog as _filedialog, messagebox as _messagebox, ttk as _ttk
        tk, filedialog, messagebox, ttk = _tk, _filedialog, _messagebox, _ttk
        from fluid_scrollbar import FluidScrollbar as _FluidScrollbar
        FluidScrollbar = _FluidScrollbar


def _center_window(window, parent=None):
    """Center a laid-out Tk window on its parent, falling back to the screen."""
    window.update_idletasks()
    width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    # Preserve an explicit WM size (for dialogs with a designed fixed layout).
    geometry = window.geometry()
    match = re.match(r"^(\d+)x(\d+)(?:[+-]\d+){2}$", geometry)
    if match and int(match.group(1)) > 1 and int(match.group(2)) > 1:
        width, height = int(match.group(1)), int(match.group(2))

    screen_width, screen_height = window.winfo_screenwidth(), window.winfo_screenheight()
    try:
        if parent is not None and parent.winfo_exists():
            parent.update_idletasks()
            parent_width, parent_height = parent.winfo_width(), parent.winfo_height()
            if parent_width > 1 and parent_height > 1:
                x = parent.winfo_rootx() + (parent_width - width) // 2
                y = parent.winfo_rooty() + (parent_height - height) // 2
            else:
                x, y = (screen_width - width) // 2, (screen_height - height) // 2
        else:
            x, y = (screen_width - width) // 2, (screen_height - height) // 2
    except Exception:
        x, y = (screen_width - width) // 2, (screen_height - height) // 2
    window.geometry(f"{width}x{height}+{x}+{y}")


# Font family is resolved against the active Tk display at startup.  The bundled
# Roboto files are registered privately on Windows (never installed system-wide).
APP_FONT_FAMILY = "Roboto"
APP_FONT_CANDIDATES = (
    "Google Sans Text", "Google Sans", "Roboto", "Segoe UI Variable Text",
    "Segoe UI", "Arial",
)


def _executable_dir():
    """Cartella dell'eseguibile (o dello script in esecuzione da sorgente)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _app_resource_path(*parts):
    base = getattr(sys, "_MEIPASS", _executable_dir())
    candidate = os.path.join(base, *parts)
    if os.path.exists(candidate):
        return candidate
    # Esecuzione dal sorgente: le risorse stanno nella cartella assets.
    fallback = os.path.join(base, "assets", *parts)
    return fallback if os.path.exists(fallback) else candidate


STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_RUN_NAME = "DOWNLOADVERSE"


def _startup_shortcut_paths():
    """Percorsi delle vecchie scorciatoie di avvio automatico."""
    paths = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(os.path.join(appdata, "Microsoft", "Windows",
                                  "Start Menu", "Programs", "Startup",
                                  "DOWNLOADVERSE.lnk"))
    programdata = os.environ.get("ProgramData")
    if programdata:
        paths.append(os.path.join(programdata, "Microsoft", "Windows",
                                  "Start Menu", "Programs", "StartUp",
                                  "DOWNLOADVERSE.lnk"))
    return paths


def is_startup_enabled():
    """True se DOWNLOADVERSE è impostato per l'avvio con Windows."""
    if os.name != "nt":
        return False
    try:
        import winreg
    except Exception:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, STARTUP_RUN_NAME)
            if value:
                return True
    except OSError:
        pass
    return any(os.path.isfile(path) for path in _startup_shortcut_paths())


def set_startup_enabled(enabled):
    """Attiva o disattiva l'avvio automatico per l'utente corrente."""
    if os.name != "nt":
        raise OSError("L'avvio automatico è supportato solo su Windows.")
    import winreg
    if enabled:
        exe_path = os.path.abspath(sys.executable)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, STARTUP_RUN_NAME, 0, winreg.REG_SZ,
                              f'"{exe_path}"')
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, STARTUP_RUN_NAME)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    # Rimuove anche eventuali scorciatoie lasciate dalle versioni precedenti.
    for path in _startup_shortcut_paths():
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def _apply_window_icon(root):
    """Applica l'icona DOWNLOADVERSE a finestra, taskbar e dialoghi."""
    ico_path = _app_resource_path("downloadverse.ico")
    try:
        if os.path.isfile(ico_path):
            root.iconbitmap(default=ico_path)
    except Exception:
        logging.exception("Impossibile applicare l'icona ICO")
    try:
        png_path = _app_resource_path("downloadverse.png")
        if os.path.isfile(png_path):
            image = tk.PhotoImage(file=png_path)
            root.iconphoto(True, image)
            # Mantiene vivo il PhotoImage per tutta la vita della finestra.
            root._downloadverse_icon = image
    except Exception:
        logging.exception("Impossibile applicare l'icona PNG")


def _register_bundled_fonts():
    if os.name != "nt":
        return
    try:
        import ctypes
        add_font = ctypes.windll.gdi32.AddFontResourceExW
        add_font.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
        add_font.restype = ctypes.c_int
        for name in ("Roboto-Regular.ttf", "Roboto-Medium.ttf"):
            path = _app_resource_path("fonts", name)
            if os.path.isfile(path):
                add_font(path, 0x10, None)  # FR_PRIVATE
    except Exception:
        # Bundled registration is a best-effort enhancement; use installed fonts.
        pass


def _choose_app_font(root):
    global APP_FONT_FAMILY
    _register_bundled_fonts()
    try:
        from tkinter import font as tkfont
        available = {family.casefold(): family for family in tkfont.families(root)}
        APP_FONT_FAMILY = next(
            (available[name.casefold()] for name in APP_FONT_CANDIDATES
             if name.casefold() in available),
            "TkDefaultFont",
        )
        for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
                     "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
                     "TkIconFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name, root=root).configure(family=APP_FONT_FAMILY)
            except (tk.TclError, KeyError):
                pass
    except Exception:
        APP_FONT_FAMILY = "Roboto"
    return APP_FONT_FAMILY


class PopupMenu:
    """Dark, roomy Toplevel menu with native-like popup behavior and API."""
    BG = "#171717"
    FG = "#e6e6e6"
    HOVER = "#333333"
    DISABLED = "#777777"
    ACCENT = "#e10600"
    MIN_WIDTH = 260
    ITEM_HEIGHT = 42

    def __init__(self, parent, tearoff=0, bg=None, fg=None,
                 activebackground=None, activeforeground=None, font=None, **_kwargs):
        _load_tkinter()
        self.parent = parent
        self.bg = bg or self.BG
        self.fg = fg or self.FG
        self.hover_bg = activebackground or self.HOVER
        self.hover_fg = activeforeground or "#ffffff"
        self.font = font or (APP_FONT_FAMILY, 13)
        self.entries = []
        self.window = None
        self.frame = None
        self._bindings = []
        self._submenu_after = None
        self._open = False

    def add_command(self, label, command=None, state="normal", **_kwargs):
        self.entries.append({"type": "command", "label": label, "command": command, "state": state})
        self._render()

    def add_radiobutton(self, label, command=None, variable=None, value=None, state="normal", **_kwargs):
        self.entries.append({"type": "radio", "label": label, "command": command,
                             "variable": variable, "value": value, "state": state})
        self._render()

    def add_cascade(self, label, menu, state="normal", **_kwargs):
        self.entries.append({"type": "cascade", "label": label, "menu": menu, "state": state})
        self._render()

    def add_separator(self):
        self.entries.append({"type": "separator", "label": "", "state": "normal"})
        self._render()

    def entryconfigure(self, index, **options):
        entry = self.entries[index]
        entry.update(options)
        self._render()

    entryconfig = entryconfigure

    def _render(self):
        if not self.window or not self.window.winfo_exists():
            return
        for child in self.frame.winfo_children():
            child.destroy()
        for index, entry in enumerate(self.entries):
            if entry["type"] == "separator":
                bar = tk.Frame(self.frame, bg="#343434", height=1)
                bar.pack(fill="x", padx=12, pady=5)
                continue
            state = entry.get("state", "normal")
            enabled = state != "disabled"
            row = tk.Frame(self.frame, bg=self.bg, height=self.ITEM_HEIGHT,
                           cursor="hand2" if enabled else "arrow")
            row.pack(fill="x")
            kind = entry["type"]
            marker = ""
            if kind == "radio":
                try:
                    marker = "●" if entry["variable"].get() == entry.get("value") else "○"
                except Exception:
                    marker = "○"
            elif kind == "cascade":
                marker = "›"
            color = self.fg if enabled else self.DISABLED
            leading = tk.Label(row, text=marker, width=3, bg=self.bg, fg=color,
                               font=(APP_FONT_FAMILY, 12), anchor="center")
            leading.pack(side="left", padx=(5, 0))
            label = tk.Label(row, text=entry.get("label", ""), bg=self.bg, fg=color,
                             font=self.font, anchor="w")
            label.pack(side="left", fill="both", expand=True, padx=(4, 14))
            # Measure children before disabling propagation.  Previously the
            # propagation lock ran while the row still had its default 1px
            # requested width, collapsing the complete popup to a thin line.
            row.update_idletasks()
            row.configure(width=max(self.MIN_WIDTH, row.winfo_reqwidth()),
                          height=self.ITEM_HEIGHT)
            row.pack_propagate(False)
            entry["_row"] = row
            for widget in (row, leading, label):
                widget.bind("<Enter>", lambda event, i=index: self._hover(i, True), add="+")
                widget.bind("<Leave>", lambda event, i=index: self._hover(i, False), add="+")
                widget.bind("<ButtonRelease-1>", lambda event, i=index: self._activate(i), add="+")
        if self._open and self.window and self.window.winfo_exists():
            try:
                if self.window.state() != "withdrawn":
                    self._fit_window(self.window)
            except tk.TclError:
                pass

    def _fit_window(self, window, x=None, y=None):
        """Size popup to its laid-out content, with a readable minimum width."""
        window.update_idletasks()
        width = max(self.MIN_WIDTH, window.winfo_reqwidth())
        height = max(1, window.winfo_reqheight())
        if x is None:
            x = window.winfo_rootx()
        if y is None:
            y = window.winfo_rooty()
        sw, sh = self.parent.winfo_screenwidth(), self.parent.winfo_screenheight()
        x = max(0, min(int(x), max(0, sw - width)))
        y = max(0, min(int(y), max(0, sh - height)))
        window.geometry(f"{width}x{height}+{x}+{y}")
        return width, height

    def _hover(self, index, active):
        entry = self.entries[index]
        if entry["type"] == "cascade" and entry.get("state") != "disabled":
            self._schedule_submenu(index)
        try:
            row = entry.get("_row")
            if row is None:
                return
            color = self.hover_bg if active and entry.get("state") != "disabled" else self.bg
            fg = self.hover_fg if active and entry.get("state") != "disabled" else (
                self.fg if entry.get("state") != "disabled" else self.DISABLED)
            for widget in row.winfo_children():
                widget.configure(bg=color, fg=fg)
            row.configure(bg=color)
        except (tk.TclError, IndexError):
            pass
        if not active and entry["type"] != "cascade":
            self._schedule_close_submenus()

    def _schedule_submenu(self, index):
        entry = self.entries[index]
        submenu = entry.get("menu")
        if not submenu:
            return
        if self._submenu_after:
            try: self.parent.after_cancel(self._submenu_after)
            except tk.TclError: pass
        self._submenu_after = self.parent.after(90, lambda: self._show_submenu(index))

    def _show_submenu(self, index):
        self._submenu_after = None
        try:
            row = self.entries[index].get("_row")
            if row is None:
                return
            submenu = self.entries[index]["menu"]
            if submenu._open and submenu.window and submenu.window.winfo_exists():
                submenu.window.lift()
                return
            x = self.window.winfo_rootx() + self.window.winfo_width() - 3
            y = row.winfo_rooty()
            submenu.window = tk.Toplevel(self.parent)
            submenu.window.withdraw()
            submenu.window.overrideredirect(True)
            submenu.window.configure(bg=submenu.bg, highlightthickness=1, highlightbackground="#414141")
            submenu.frame = tk.Frame(submenu.window, bg=submenu.bg, padx=3, pady=5)
            submenu.frame.pack(fill="both", expand=True)
            submenu._open = True
            submenu._render()
            submenu.window.update_idletasks()
            width = max(submenu.MIN_WIDTH, submenu.window.winfo_reqwidth())
            if x + width > self.parent.winfo_screenwidth():
                x = max(0, self.window.winfo_rootx() - width + 3)
            submenu._fit_window(submenu.window, x, y)
            submenu.window.deiconify()
            submenu.window.lift()
            submenu.window.focus_force()
            submenu.window.bind("<FocusOut>", self._focus_out, add="+")
        except (tk.TclError, IndexError):
            pass

    def _schedule_close_submenus(self):
        if self._submenu_after:
            try: self.parent.after_cancel(self._submenu_after)
            except tk.TclError: pass
            self._submenu_after = None
        for entry in self.entries:
            if entry["type"] == "cascade" and entry["menu"]._open:
                self.parent.after(220, entry["menu"].close)

    def _activate(self, index):
        entry = self.entries[index]
        if entry.get("state") == "disabled":
            return "break"
        if entry["type"] == "cascade":
            self._show_submenu(index)
            return "break"
        if entry["type"] == "radio" and entry.get("variable") is not None:
            try:
                entry["variable"].set(entry.get("value"))
            except Exception:
                pass
        command = entry.get("command")
        self.close()
        if command:
            command()
        return "break"

    def _all_open_menus(self):
        result = [self] if self._open else []
        for entry in self.entries:
            if entry["type"] == "cascade":
                result.extend(entry["menu"]._all_open_menus())
        return result

    def _outside_click(self, event):
        x, y = getattr(event, "x_root", -99999), getattr(event, "y_root", -99999)
        inside = False
        for menu in self._all_open_menus():
            try:
                w = menu.window
                inside |= (w.winfo_rootx() <= x < w.winfo_rootx() + w.winfo_width() and
                           w.winfo_rooty() <= y < w.winfo_rooty() + w.winfo_height())
            except tk.TclError:
                pass
        if not inside:
            self.close()

    def tk_popup(self, x, y, nested=False):
        _load_tkinter()
        if self.window and self.window.winfo_exists():
            self.window.destroy()
        self.window = tk.Toplevel(self.parent)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg=self.bg, highlightthickness=1, highlightbackground="#414141")
        self.frame = tk.Frame(self.window, bg=self.bg, padx=3, pady=5)
        self.frame.pack(fill="both", expand=True)
        self._open = True
        self._render()
        self._fit_window(self.window, x, y)
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        if not nested:
            for seq, callback in (("<ButtonPress>", self._outside_click), ("<Escape>", lambda _e: self.close())):
                try:
                    fid = self.parent.bind_all(seq, callback, add="+")
                    self._bindings.append((seq, fid))
                except tk.TclError:
                    pass
        self.window.bind("<FocusOut>", self._focus_out, add="+")
        return "break"

    def _focus_out(self, _event=None):
        try:
            self.parent.after(80, lambda: self.close() if self._open and not self._focus_is_menu() else None)
        except tk.TclError:
            pass

    def _focus_is_menu(self):
        try:
            focus = self.parent.focus_get()
            return any(focus is not None and str(focus).startswith(str(m.window)) for m in self._all_open_menus())
        except tk.TclError:
            return False

    def close(self):
        if not self._open:
            return
        self._open = False
        for entry in self.entries:
            if entry["type"] == "cascade":
                entry["menu"].close()
        for seq, fid in self._bindings:
            try:
                self.parent._unbind(("bind", "all", seq), fid)
            except (tk.TclError, AttributeError):
                pass
        self._bindings.clear()
        try:
            if self.window and self.window.winfo_exists():
                self.window.destroy()
        except tk.TclError:
            pass

    def grab_release(self):
        # Compatibility with tk.Menu callers; popup is a Toplevel without a grab.
        return None


PRIORITIES = {"Alta": 0, "Normale": 1, "Bassa": 2}
CHUNK_SIZE = 64 * 1024
PROGRESS_SEGMENTS = 10
MAGNET_METADATA_TIMEOUT = 60

lt = None
LIBTORRENT_IMPORT_ERROR = ""


def _load_libtorrent():
    """Carica libtorrent solo quando si aggiunge o si gestisce un torrent."""
    global lt, LIBTORRENT_IMPORT_ERROR
    if lt is not None:
        return lt
    if LIBTORRENT_IMPORT_ERROR:
        return None
    try:
        import libtorrent as _libtorrent
    except Exception as exc:
        LIBTORRENT_IMPORT_ERROR = str(exc)
        return None
    lt = _libtorrent
    return lt

# UI translations (Italian is the source language; all displayed strings use these catalogs).

def _user_data_dir():
    """Cartella dati scrivibile, indipendente dalla directory del programma."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "GDownloader")


def _is_writable_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".gdownloader_write_test")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _data_dir():
    """Preferisce la cartella dell'eseguibile (modalità portabile), altrimenti AppData."""
    executable_dir = _executable_dir()
    if _is_writable_dir(executable_dir):
        return executable_dir
    return _user_data_dir()


DATA_DIR = _data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
LANGUAGE_CONFIG_PATH = os.path.join(DATA_DIR, "downloader_config.json")
SESSION_PATH = os.path.join(DATA_DIR, "session.json")
COOKIE_CONFIG_PATH = os.path.join(DATA_DIR, "browser_cookies.json")


def _migrate_legacy_data(target_dir):
    """Copia i dati delle versioni precedenti nella cartella dati attiva."""
    sources = [_user_data_dir()]
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            sources.append(os.path.join(local_appdata, "DOWNLOADVERSE"))
            sources.append(os.path.join(local_appdata, "Programs", "DOWNLOADVERSE"))
    target = os.path.abspath(target_dir)
    for legacy_dir in sources:
        if os.path.abspath(legacy_dir) == target:
            continue
        for name in ("session.json", "downloader_config.json", "browser_cookies.json"):
            source = os.path.join(legacy_dir, name)
            destination = os.path.join(target_dir, name)
            if os.path.isfile(source) and not os.path.exists(destination):
                try:
                    shutil.copy2(source, destination)
                except OSError:
                    logging.info("Impossibile migrare %s nella cartella dati", name)


_migrate_legacy_data(DATA_DIR)


UI_STRINGS = {
 "  /  GESTORE DOWNLOAD": ("  /  DOWNLOAD MANAGER", "  /  DOWNLOAD-VERWALTUNG"),
 "TRASFERIMENTI  •  HTTP / HTTPS / MAGNET": ("TRANSFERS  •  HTTP / HTTPS / MAGNET", "ÜBERTRAGUNGEN  •  HTTP / HTTPS / MAGNET"),
 "NUOVO DOWNLOAD": ("NEW DOWNLOAD", "NEUER DOWNLOAD"), "＋  Aggiungi": ("＋  Add", "＋  Hinzufügen"),
 "DESTINAZIONE": ("DESTINATION", "ZIELORDNER"), "Scegli cartella…": ("Choose folder…", "Ordner wählen…"),
 "▶  Avvia coda": ("▶  Start queue", "▶  Warteschlange starten"), "RIMUOVI": ("REMOVE", "ENTFERNEN"),
 "Sei sicuro di rimuovere i file in download?": ("Are you sure you want to remove the downloading files?", "Möchten Sie die heruntergeladenen Dateien wirklich entfernen?"),
 "NOME FILE": ("FILE NAME", "DATEINAME"), "PROGRESSO": ("PROGRESS", "FORTSCHRITT"), "%": ("%", "%"), "DIMENSIONE": ("SIZE", "GRÖSSE"), "VELOCITÀ": ("SPEED", "GESCHWINDIGKEIT"), "PEER": ("PEERS", "PEERS"), "PRIORITÀ": ("PRIORITY", "PRIORITÄT"), "STATO": ("STATUS", "STATUS"),
 "Taglia": ("Cut", "Ausschneiden"), "Copia": ("Copy", "Kopieren"), "Incolla": ("Paste", "Einfügen"), "Seleziona tutto": ("Select all", "Alles auswählen"),
 "Informazioni file…": ("File information…", "Dateiinformationen…"), "Priorità": ("Priority", "Priorität"), "Alta": ("High", "Hoch"), "Normale": ("Normal", "Normal"), "Bassa": ("Low", "Niedrig"), "Scarica ora": ("Download now", "Jetzt herunterladen"), "Pausa/Riprendi": ("Pause/Resume", "Pause/Fortsetzen"), "Pausa / riprendi gruppo": ("Pause / resume group", "Gruppe pausieren / fortsetzen"), "Annulla": ("Cancel", "Abbrechen"), "Rimuovi dalla lista": ("Remove from list", "Aus Liste entfernen"), "Apri cartella": ("Open folder", "Ordner öffnen"),
 "Doppio clic su una riga per avviare subito, fuori dai limiti della coda.": ("Double-click a row to start immediately, outside the queue limit.", "Doppelklick auf eine Zeile startet sofort, unabhängig vom Warteschlangenlimit."), "FILE TEMPORANEI  ·  .part": ("TEMPORARY FILES  ·  .part", "TEMPORÄRE DATEIEN  ·  .part"),
 "Impostazioni": ("Settings", "Einstellungen"), "Download simultanei": ("Simultaneous downloads", "Gleichzeitige Downloads"), "Numero di download simultanei (1–10)": ("Number of simultaneous downloads (1–10)", "Anzahl gleichzeitiger Downloads (1–10)"), "Applica": ("Apply", "Anwenden"), "Lingua": ("Language", "Sprache"), "Predefinito di sistema": ("System default", "Systemstandard"), "English": ("English", "English"), "Deutsch": ("German", "Deutsch"), "Italiano": ("Italian", "Italienisch"),
 "In coda": ("Queued", "In Warteschlange"), "In avvio": ("Starting", "Wird gestartet"), "In corso": ("Downloading", "Wird heruntergeladen"), "In pausa": ("Paused", "Pausiert"), "Annullato": ("Cancelled", "Abgebrochen"), "Completato": ("Completed", "Abgeschlossen"), "Errore": ("Error", "Fehler"), "Annullamento…": ("Cancelling…", "Wird abgebrochen…"),
 "GESTORE DOWNLOAD": ("DOWNLOAD MANAGER", "DOWNLOAD-VERWALTUNG"), "TRASFERIMENTI": ("TRANSFERS", "ÜBERTRAGUNGEN"), "Incolla automaticamente": ("Paste automatically", "Automatisch einfügen"),
 "Inserisci un URL HTTP o HTTPS valido.": ("Enter a valid HTTP or HTTPS URL.", "Geben Sie eine gültige HTTP- oder HTTPS-URL ein."), "Il torrent contiene un percorso file non sicuro.": ("The torrent contains an unsafe file path.", "Der Torrent enthält einen unsicheren Dateipfad."), "Percorso torrent non sicuro.": ("Unsafe torrent path.", "Unsicherer Torrent-Pfad."), "La sessione torrent è già stata chiusa.": ("The torrent session has already been closed.", "Die Torrent-Sitzung wurde bereits geschlossen."), "Il torrent si è arrestato prima del completamento del file.": ("The torrent stopped before the file completed.", "Der Torrent wurde vor Abschluss der Datei angehalten."), "Sovrascrittura rifiutata": ("Overwrite declined", "Überschreiben abgelehnt"), "Download annullato": ("Download cancelled", "Download abgebrochen"), "Ricerca metadati torrent…": ("Searching torrent metadata…", "Torrent-Metadaten werden gesucht…"), "Ricerca metadati (timeout 60 s)…": ("Searching metadata (60 s timeout)…", "Metadaten werden gesucht (60 s Zeitlimit)…"), "Libtorrent non installato": ("Libtorrent not installed", "Libtorrent nicht installiert"), "Installazione libtorrent in corso…": ("Installing libtorrent…", "Libtorrent wird installiert…"), "In attesa": ("Waiting", "Wartend"), "Risoluzione link…": ("Resolving link…", "Link wird aufgelöst…"), "Connessione…": ("Connecting…", "Verbindung wird hergestellt…"),
 "Dominio non supportato": ("Unsupported domain", "Nicht unterstützte Domain"), "Scegli datanodes.to o fuckingfast.co.": ("Choose datanodes.to or fuckingfast.co.", "Wählen Sie datanodes.to oder fuckingfast.co."),
 "Libtorrent non disponibile": ("Libtorrent unavailable", "Libtorrent nicht verfügbar"), "libtorrent non è installato. Vuoi installarlo ora?": ("libtorrent is not installed. Install it now?", "libtorrent ist nicht installiert. Jetzt installieren?"), "Installazione non riuscita": ("Installation failed", "Installation fehlgeschlagen"), "Non è stato possibile installare libtorrent. Consulta il crash log.": ("Could not install libtorrent. See the crash log.", "libtorrent konnte nicht installiert werden. Siehe Crash-Log."), "Magnet non valido": ("Invalid magnet", "Ungültiger Magnet-Link"), "Inserisci un magnet con hash BTIH valido.": ("Enter a magnet link with a valid BTIH hash.", "Geben Sie einen Magnet-Link mit gültigem BTIH-Hash ein."),
 "Download attivo": ("Download active", "Download aktiv"), "Questo file è già in download.": ("This file is already downloading.", "Diese Datei wird bereits heruntergeladen."), "Annulla il trasferimento prima di rimuoverlo.": ("Cancel the transfer before removing it.", "Brechen Sie die Übertragung ab, bevor Sie sie entfernen."), "Impossibile aprire la cartella": ("Could not open folder", "Ordner konnte nicht geöffnet werden"), "Informazioni file": ("File information", "Dateiinformationen"), "DETTAGLI TRASFERIMENTO": ("TRANSFER DETAILS", "ÜBERTRAGUNGSDETAILS"), "Nome": ("Name", "Name"), "Dimensione": ("Size", "Größe"), "Tipo MIME": ("MIME type", "MIME-Typ"), "Percorso": ("Path", "Pfad"), "Stato": ("Status", "Status"), "Byte scaricati": ("Bytes downloaded", "Heruntergeladene Bytes"), "Peer": ("Peers", "Peers"), "Info hash": ("Info hash", "Info-Hash"), "Chiudi": ("Close", "Schließen"), "Release GitHub": ("GitHub release", "GitHub-Release"), "La release non contiene asset scaricabili.": ("The release contains no downloadable assets.", "Das Release enthält keine herunterladbaren Assets."), "Errore release GitHub": ("GitHub release error", "GitHub-Release-Fehler"), "Conferma sovrascrittura": ("Confirm overwrite", "Überschreiben bestätigen"), "Il file esiste già:\n{0}\n\nVuoi sovrascriverlo?": ("The file already exists:\n{0}\n\nOverwrite it?", "Die Datei existiert bereits:\n{0}\n\nÜberschreiben?"),
}
UI_STRINGS.update({
 "NUOVO DOWNLOAD": ("NEW DOWNLOAD", "NEUER DOWNLOAD"), "Aggiungi": ("Add", "Hinzufügen"), "RIMUOVI": ("REMOVE", "ENTFERNEN"),
 "Asset release GitHub": ("GitHub release assets", "GitHub-Release-Assets"), "SELEZIONA I FILE DELLA RELEASE": ("SELECT RELEASE FILES", "RELEASE-DATEIEN AUSWÄHLEN"), " elementi disponibili": (" items available", " verfügbare Elemente"), "Dimensione n/d": ("Size n/a", "Größe n. v."), "Sorgente": ("Source", "Quelle"), "Seleziona tutto": ("Select all", "Alles auswählen"), "Aggiungi selezionati": ("Add selected", "Auswahl hinzufügen"),
 "URL non valido": ("Invalid URL", "Ungültige URL"), "Release GitHub": ("GitHub release", "GitHub-Release"), "La release non contiene asset scaricabili.": ("The release contains no downloadable assets.", "Das Release enthält keine herunterladbaren Assets."), "Errore release GitHub": ("GitHub release error", "GitHub-Release-Fehler"), "SELEZIONA I FILE DELLA RELEASE": ("SELECT RELEASE FILES", "RELEASE-DATEIEN AUSWÄHLEN"), "Conferma sovrascrittura": ("Confirm overwrite", "Überschreiben bestätigen"),
 "Il link punta a una pagina web, non a un file": ("The link points to a web page, not a file", "Der Link verweist auf eine Webseite, nicht auf eine Datei"), "Inserisci un magnet valido.": ("Enter a valid magnet link.", "Geben Sie einen gültigen Magnet-Link ein."), "Magnet non valido: è richiesto un hash BTIH (xt=urn:btih:…).": ("Invalid magnet: a BTIH hash (xt=urn:btih:…) is required.", "Ungültiger Magnet-Link: ein BTIH-Hash (xt=urn:btih:…) ist erforderlich."), "Il torrent contiene un percorso file non sicuro.": ("The torrent contains an unsafe file path.", "Der Torrent enthält einen unsicheren Dateipfad."), "Ricerca metadati annullata": ("Metadata lookup cancelled", "Metadatensuche abgebrochen"), "Timeout: metadati torrent non ricevuti entro 60 secondi. Verifica rete, tracker o peer.": ("Timeout: torrent metadata was not received within 60 seconds. Check network, trackers, or peers.", "Zeitüberschreitung: Torrent-Metadaten wurden innerhalb von 60 Sekunden nicht empfangen. Netzwerk, Tracker oder Peers prüfen."),
 "Libtorrent not installed": ("Libtorrent not installed", "Libtorrent nicht installiert"), "Installazione libtorrent annullata.": ("Libtorrent installation cancelled.", "Libtorrent-Installation abgebrochen."), "Impossibile mostrare il prompt di installazione libtorrent": ("Could not display the libtorrent installation prompt", "Installationsabfrage für libtorrent konnte nicht angezeigt werden"), "pip terminato con codice ": ("pip exited with code ", "pip wurde mit Code beendet: "), "Limite richieste GitHub raggiunto. Riprova più tardi.": ("GitHub API rate limit reached. Try again later.", "GitHub-API-Ratenlimit erreicht. Versuchen Sie es später erneut."), "Release GitHub non trovata (404). Verifica repository e tag.": ("GitHub release not found (404). Check repository and tag.", "GitHub-Release nicht gefunden (404). Repository und Tag prüfen."),
 "GESTORE DOWNLOAD": ("DOWNLOAD MANAGER", "DOWNLOAD-VERWALTUNG"), "TRASFERIMENTI  •  HTTP / HTTPS / MAGNET": ("TRANSFERS  •  HTTP / HTTPS / MAGNET", "ÜBERTRAGUNGEN  •  HTTP / HTTPS / MAGNET"), "NUOVO DOWNLOAD": ("NEW DOWNLOAD", "NEUER DOWNLOAD"), "PROGRESSO": ("PROGRESS", "FORTSCHRITT"), "PRIORITÀ": ("PRIORITY", "PRIORITÄT"), "VELOCITÀ": ("SPEED", "GESCHWINDIGKEIT"), "DIMENSIONE": ("SIZE", "GRÖSSE"), "NOME FILE": ("FILE NAME", "DATEINAME"), "STATO": ("STATUS", "STATUS"), "DESTINAZIONE": ("DESTINATION", "ZIELORDNER"),
})
UI_STRINGS.update({
 "NOME FILE": ("FILE NAME", "DATEINAME"), "PROGRESSO": ("PROGRESS", "FORTSCHRITT"), "DIMENSIONE": ("SIZE", "GRÖSSE"), "VELOCITÀ": ("SPEED", "GESCHWINDIGKEIT"), "PRIORITÀ": ("PRIORITY", "PRIORITÄT"), "STATO": ("STATUS", "STATUS"), "DESTINAZIONE": ("DESTINATION", "ZIELORDNER"),
 "SELEZIONA I FILE DELLA RELEASE": ("SELECT RELEASE FILES", "RELEASE-DATEIEN AUSWÄHLEN"), " elementi disponibili": (" items available", " verfügbare Elemente"), "Dimensione n/d": ("Size n/a", "Größe n. v."), "Sorgente": ("Source", "Quelle"), "Seleziona tutto": ("Select all", "Alles auswählen"), "Aggiungi selezionati": ("Add selected", "Auswahl hinzufügen"), "Conferma sovrascrittura": ("Confirm overwrite", "Überschreiben bestätigen"),
 "URL non valido": ("Invalid URL", "Ungültige URL"), "Errore API GitHub HTTP ": ("GitHub API HTTP error ", "GitHub-API-HTTP-Fehler "), "Impossibile recuperare la release GitHub: ": ("Could not fetch GitHub release: ", "GitHub-Release konnte nicht abgerufen werden: "), "HEAD: ": ("HEAD: ", "HEAD: "), "Asset release GitHub": ("GitHub release assets", "GitHub-Release-Assets"), "SELEZIONA I FILE DELLA RELEASE": ("SELECT RELEASE FILES", "RELEASE-DATEIEN AUSWÄHLEN"), "La release non contiene asset scaricabili.": ("The release contains no downloadable assets.", "Das Release enthält keine herunterladbaren Assets."), "Release GitHub": ("GitHub release", "GitHub-Release"), "Errore release GitHub": ("GitHub release error", "GitHub-Release-Fehler"), "Risoluzione link…": ("Resolving link…", "Link wird aufgelöst…"), "Connessione…": ("Connecting…", "Verbindung wird hergestellt…"), "In coda": ("Queued", "In Warteschlange"), "In avvio": ("Starting", "Wird gestartet"), "In corso": ("Downloading", "Wird heruntergeladen"), "In pausa": ("Paused", "Pausiert"), "Annullato": ("Cancelled", "Abgebrochen"), "Completato": ("Completed", "Abgeschlossen"), "Errore": ("Error", "Fehler"), "Annullamento…": ("Cancelling…", "Wird abgebrochen…"),
 "Dominio non supportato": ("Unsupported domain", "Nicht unterstützte Domain"), "Scegli datanodes.to o fuckingfast.co.": ("Choose datanodes.to or fuckingfast.co.", "Wählen Sie datanodes.to oder fuckingfast.co."),
 "Libtorrent non disponibile": ("Libtorrent unavailable", "Libtorrent nicht verfügbar"), "libtorrent non è installato. Vuoi installarlo ora?": ("libtorrent is not installed. Install it now?", "libtorrent ist nicht installiert. Jetzt installieren?"), "Installazione non riuscita": ("Installation failed", "Installation fehlgeschlagen"), "Non è stato possibile installare libtorrent. Consulta il crash log.": ("Could not install libtorrent. See the crash log.", "libtorrent konnte nicht installiert werden. Siehe Crash-Log."), "Magnet non valido": ("Invalid magnet", "Ungültiger Magnet-Link"), "Inserisci un magnet con hash BTIH valido.": ("Enter a magnet link with a valid BTIH hash.", "Geben Sie einen Magnet-Link mit gültigem BTIH-Hash ein."), "Download attivo": ("Download active", "Download aktiv"), "Questo file è già in download.": ("This file is already downloading.", "Diese Datei wird bereits heruntergeladen."), "Annulla il trasferimento prima di rimuoverlo.": ("Cancel the transfer before removing it.", "Brechen Sie die Übertragung ab, bevor Sie sie entfernen."), "Impossibile aprire la cartella": ("Could not open folder", "Ordner konnte nicht geöffnet werden"), "Informazioni file": ("File information", "Dateiinformationen"), "DETTAGLI TRASFERIMENTO": ("TRANSFER DETAILS", "ÜBERTRAGUNGSDETAILS"), "Nome": ("Name", "Name"), "Dimensione": ("Size", "Größe"), "Tipo MIME": ("MIME type", "MIME-Typ"), "Percorso": ("Path", "Pfad"), "Stato": ("Status", "Status"), "Byte scaricati": ("Bytes downloaded", "Heruntergeladene Bytes"), "Peer": ("Peers", "Peers"), "Info hash": ("Info hash", "Info-Hash"), "Chiudi": ("Close", "Schließen"),
})
UI_STRINGS.update({
 "TRASFERIMENTI  •  HTTP / HTTPS / MAGNET": ("TRANSFERS  •  HTTP / HTTPS / MAGNET", "ÜBERTRAGUNGEN  •  HTTP / HTTPS / MAGNET"), "NUOVO DOWNLOAD": ("NEW DOWNLOAD", "NEUER DOWNLOAD"), "＋  Aggiungi": ("＋  Add", "＋  Hinzufügen"), "▶  Avvia coda": ("▶  Start queue", "▶  Warteschlange starten"), "Scegli cartella…": ("Choose folder…", "Ordner wählen…"), "RIMUOVI": ("REMOVE", "ENTFERNEN"), "Doppio clic su una riga per avviare subito, fuori dai limiti della coda.": ("Double-click a row to start immediately, bypassing the queue limit.", "Doppelklick startet sofort und umgeht das Warteschlangenlimit."), "FILE TEMPORANEI  ·  .part": ("TEMPORARY FILES  ·  .part", "TEMPORÄRE DATEIEN  ·  .part"),
 "SELEZIONA I FILE DELLA RELEASE": ("SELECT RELEASE FILES", "RELEASE-DATEIEN AUSWÄHLEN"), "Aggiungi selezionati": ("Add selected", "Auswahl hinzufügen"), " elementi disponibili": (" items available", " verfügbare Elemente"), "Dimensione n/d": ("Size n/a", "Größe n. v."), "Sorgente": ("Source", "Quelle"), "Scarica ora": ("Download now", "Jetzt herunterladen"), "Pausa/Riprendi": ("Pause/Resume", "Pause/Fortsetzen"), "Rimuovi dalla lista": ("Remove from list", "Aus Liste entfernen"), "Apri cartella": ("Open folder", "Ordner öffnen"), "Informazioni file…": ("File information…", "Dateiinformationen…"), "Annulla": ("Cancel", "Abbrechen"), "Priorità": ("Priority", "Priorität"), "Alta": ("High", "Hoch"), "Normale": ("Normal", "Normal"), "Bassa": ("Low", "Niedrig"), " peer": (" peer", " Peer"),
 "Installazione libtorrent in corso…": ("Installing libtorrent…", "Libtorrent wird installiert…"), "Installazione libtorrent annullata.": ("Libtorrent installation cancelled.", "Libtorrent-Installation abgebrochen."), "pip terminato con codice ": ("pip exited with code ", "pip wurde mit Code beendet: "), "Limite richieste GitHub raggiunto. Riprova più tardi.": ("GitHub API rate limit reached. Try again later.", "GitHub-API-Ratenlimit erreicht. Versuchen Sie es später erneut."), "Release GitHub non trovata (404). Verifica repository e tag.": ("GitHub release not found (404). Check repository and tag.", "GitHub-Release nicht gefunden (404). Repository und Tag prüfen."), "Impossibile mostrare il prompt di installazione libtorrent": ("Could not show the libtorrent installation prompt", "Installationsdialog für libtorrent konnte nicht angezeigt werden"), "Impossibile installare libtorrent.": ("Could not install libtorrent.", "libtorrent konnte nicht installiert werden."),
 "URL non valido": ("Invalid URL", "Ungültige URL"), "Inserisci un URL HTTP o HTTPS valido.": ("Enter a valid HTTP or HTTPS URL.", "Geben Sie eine gültige HTTP- oder HTTPS-URL ein."), "Conferma sovrascrittura": ("Confirm overwrite", "Überschreiben bestätigen"), "Il file esiste già:\n": ("The file already exists:\n", "Die Datei existiert bereits:\n"), "\n\nVuoi sovrascriverlo?": ("\n\nOverwrite it?", "\n\nÜberschreiben?"),
})
UI_STRINGS.update({
 "libtorrent non è installato. Vuoi installarlo ora?": ("libtorrent is not installed. Install it now?", "libtorrent ist nicht installiert. Jetzt installieren?"), "Inserisci un magnet con hash BTIH valido.": ("Enter a magnet link with a valid BTIH hash.", "Geben Sie einen Magnet-Link mit gültigem BTIH-Hash ein."), "Questo file è già in download.": ("This file is already downloading.", "Diese Datei wird bereits heruntergeladen."), "Annulla il trasferimento prima di rimuoverlo.": ("Cancel the transfer before removing it.", "Brechen Sie die Übertragung ab, bevor Sie sie entfernen."), "La release non contiene asset scaricabili.": ("The release contains no downloadable assets.", "Das Release enthält keine herunterladbaren Assets."), "Non è stato possibile installare libtorrent. Consulta il crash log.": ("Could not install libtorrent. See the crash log.", "libtorrent konnte nicht installiert werden. Siehe Crash-Log."),
})
UI_STRINGS.update({
 "Seleziona i file del torrent": ("Select torrent files", "Torrent-Dateien auswählen"), "Recupero metadati torrent…": ("Retrieving torrent metadata…", "Torrent-Metadaten werden abgerufen…"),
 "Torrent…": ("Torrent…", "Torrent…"), "Apri file torrent": ("Open torrent files", "Torrent-Dateien öffnen"), "Seleziona un file .torrent valido.": ("Choose a valid .torrent file.", "Wählen Sie eine gültige .torrent-Datei aus."), "Seleziona": ("Select", "Auswählen"),
 "Deseleziona": ("Deselect", "Auswahl aufheben"), "Priorità": ("Priority", "Priorität"),
 "Pausa": ("Pause", "Pausieren"), "Riprendi": ("Resume", "Fortsetzen"),
 "Rimuovi": ("Remove", "Entfernen"), "Avvia ora": ("Start now", "Jetzt starten"),
 "Seleziona i file del torrent": ("Select torrent files", "Torrent-Dateien auswählen"),
 "Recupero metadati…": ("Retrieving metadata…", "Metadaten werden abgerufen…"),
 "Conferma": ("Confirm", "Bestätigen"), "Totale selezionato:": ("Selected total:", "Ausgewählte Gesamtgröße:"),
 "Recupero metadati torrent…": ("Retrieving torrent metadata…", "Torrent-Metadaten werden abgerufen…"),
 "Priorità applicata": ("Priority applied", "Priorität angewendet"),
})
UI_STRINGS.update({
 "Español": ("Spanish", "Spanisch"), "Français": ("French", "Französisch"), "Download": ("Downloads", "Downloads"),
 "Seleziona la lingua dell'interfaccia": ("Select the interface language", "Wählen Sie die Sprache der Benutzeroberfläche"),
 "Limiti di velocità: 0 = illimitata": ("Speed limits: 0 = unlimited", "Geschwindigkeitsbegrenzung: 0 = unbegrenzt"),
 "Velocità massima download": ("Maximum download speed", "Maximale Downloadgeschwindigkeit"),
 "Velocità massima upload": ("Maximum upload speed", "Maximale Uploadgeschwindigkeit"),
 "Numero massimo peer": ("Maximum number of peers", "Maximale Anzahl an Peers"),
 "Valore non valido nelle impostazioni.": ("Invalid settings value.", "Ungültiger Einstellungswert."),
})
UI_STRINGS.update({
 "Cookie host protetti": ("Protected host cookies", "Cookies geschützter Hosts"),
 "Cookie host protetti: incolla l'header Cookie del browser per datanodes.to o fuckingfast.co.":
     ("Protected host cookies: paste the browser Cookie header for datanodes.to or fuckingfast.co.",
      "Cookies geschützter Hosts: Fügen Sie den Cookie-Header des Browsers für datanodes.to oder fuckingfast.co ein."),
 "Salva": ("Save", "Speichern"),
 "Cookie salvato": ("Cookie saved", "Cookie gespeichert"),
 "Impossibile salvare il cookie": ("Could not save the cookie", "Cookie konnte nicht gespeichert werden"),
 "Salvato in": ("Saved to", "Gespeichert in"),
 "Svuota coda": ("Clear queue", "Warteschlange leeren"),
})
UI_STRINGS.update({
 "Importa lista link": ("Import links", "Links importieren"),
 "Importazione": ("Import", "Import"),
 " elementi": (" items", " Elemente"),
 "Importa lista link: incolla un paste PrivateBin e/o un elenco di link.":
     ("Import links: paste a PrivateBin paste and/or a list of links.",
      "Links importieren: Fügen Sie ein PrivateBin-Paste und/oder eine Linkliste ein."),
 "Link paste (PrivateBin)": ("Paste link (PrivateBin)", "Paste-Link (PrivateBin)"),
 "Link manuali (uno per riga)": ("Manual links (one per line)", "Manuelle Links (eine pro Zeile)"),
 "Importa": ("Import", "Importieren"),
 "Lettura paste…": ("Reading paste…", "Paste wird gelesen…"),
 "Importazione…": ("Importing…", "Import wird ausgeführt…"),
 "Importazione completata": ("Import completed", "Import abgeschlossen"),
 " link aggiunti alla coda.": (" links added to the queue.", " Links zur Warteschlange hinzugefügt."),
 "Nessun link trovato.": ("No links found.", "Keine Links gefunden."),
 "Tutti i link sono già presenti nella coda.":
     ("All links are already in the queue.", "Alle Links sind bereits in der Warteschlange."),
 "Il paste non contiene link.": ("The paste contains no links.", "Das Paste enthält keine Links."),
 "Errore importazione": ("Import error", "Importfehler"),
 "Inserisci almeno un link o un paste da importare.":
     ("Enter at least one link or a paste to import.",
      "Geben Sie mindestens einen Link oder ein Paste zum Importieren ein."),
 "I file verranno aggiunti alla cartella di destinazione corrente.":
     ("Files will be added to the current destination folder.",
      "Die Dateien werden dem aktuellen Zielordner hinzugefügt."),
})
UI_STRINGS.update({
 "Cerca file…": ("Search files…", "Dateien suchen…"),
 "Cerca cartella…": ("Search folder…", "Ordner suchen…"),
 "Cartelle": ("Folders", "Ordner"),
 "Apri": ("Open", "Öffnen"),
 "Scegli cartella": ("Choose folder", "Ordner wählen"),
 "Seleziona cartella": ("Select folder", "Ordner auswählen"),
 "Data modifica": ("Date modified", "Änderungsdatum"),
 "Tipo": ("Type", "Typ"),
 "Cartella": ("Folder", "Ordner"),
 "File torrent": ("Torrent file", "Torrent-Datei"),
 "Inserisci un Link, un magnet o trascina un file .torrent sotto":
     ("Enter a link, a magnet, or drag a .torrent file below",
      "Link oder Magnet einfügen oder eine .torrent-Datei unten hineinziehen"),
 "Trascina qui un file Torrent": ("Drag a Torrent file here",
                                  "Ziehen Sie eine Torrent-Datei hierher"),
 "Informazioni": ("Information", "Informationen"),
 "Versione software": ("Software version", "Softwareversion"),
 "Autore": ("Author", "Autor"),
 "Controlla aggiornamenti": ("Check for updates", "Nach Updates suchen"),
 "Aggiornamento": ("Update", "Update"),
 "Controllo aggiornamenti…": ("Checking for updates…", "Suche nach Updates…"),
 "Nessun aggiornamento disponibile.": ("No updates available.", "Keine Updates verfügbar."),
 "Nuova versione disponibile: {0}":
     ("New version available: {0}", "Neue Version verfügbar: {0}"),
 "Aggiornamento annullato.": ("Update cancelled.", "Update abgebrochen."),
 "Nuova versione {0} disponibile. Scaricare e installare ora?\nL'applicazione verrà chiusa.":
     ("Version {0} is available. Download and install now?\nThe application will close.",
      "Version {0} ist verfügbar. Jetzt herunterladen und installieren?\nDie Anwendung wird geschlossen."),
 "Download aggiornamento…": ("Downloading update…", "Update wird heruntergeladen…"),
 "Aggiornamento scaricato. Avvio del setup…":
     ("Update downloaded. Starting setup…", "Update heruntergeladen. Setup wird gestartet…"),
 "Impossibile controllare gli aggiornamenti.":
     ("Could not check for updates.", "Updates konnten nicht geprüft werden."),
 "Errore download aggiornamento.":
     ("Update download error.", "Fehler beim Herunterladen des Updates."),
 "Aggiornamento non supportato su questo sistema.":
     ("Updates are not supported on this system.",
      "Updates werden auf diesem System nicht unterstützt."),
 "Uscita": ("Exit", "Beenden"),
 "Chiudere completamente o ridurre a icona?":
     ("Close completely or minimize to tray?",
      "Vollständig schließen oder in den Infobereich minimieren?"),
 "Non chiedermelo più": ("Don't ask me again", "Nicht mehr fragen"),
 "Riduci": ("Minimize", "Minimieren"),
 "Reimposta richiesta di chiusura":
     ("Reset close prompt", "Schließen-Abfrage zurücksetzen"),
 "Richiesta di chiusura ripristinata.":
     ("Close prompt restored.", "Schließen-Abfrage wiederhergestellt."),
})
LANGUAGES = {"system": "Predefinito di sistema", "en": "English", "de": "Deutsch", "it": "Italiano", "es": "Español", "fr": "Français"}
def detect_system_language(locale_module=None):
    loc = locale_module or locale
    if os.name == "nt" and locale_module is None:
        try:
            import ctypes
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            primary = langid & 0x3ff
            return {0x10: "it", 0x07: "de", 0x09: "en", 0x0a: "es", 0x0c: "fr"}.get(primary, "en")
        except Exception:
            pass
    candidates = []
    for getter_name in ("getlocale", "getdefaultlocale"):
        try:
            value = getattr(loc, getter_name)()
            if isinstance(value, tuple):
                value = value[0]
            if value:
                candidates.append(str(value))
        except Exception:
            pass
    language_names = {
        "italian": "it", "german": "de", "english": "en",
        "spanish": "es", "french": "fr",
    }
    for value in candidates:
        normalized = value.lower().replace("-", "_")
        prefix = normalized.split("_", 1)[0]
        if prefix in ("it", "de", "en", "es", "fr"):
            return prefix
        if prefix in language_names:
            return language_names[prefix]
    return "en"
def _read_app_config(path=LANGUAGE_CONFIG_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}

def _write_app_config(updates, path=LANGUAGE_CONFIG_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    config = _read_app_config(path)
    config.update(updates)
    # Scrittura atomica: un'interruzione di corrente non deve lasciare il
    # file di configurazione pieno di byte nulli o troncato.
    temp = path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.flush()
        os.replace(temp, path)
    except OSError:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise

def load_language_config(path=LANGUAGE_CONFIG_PATH):
    value = _read_app_config(path).get("language", "system")
    return value if value in LANGUAGES else "system"

def save_language_config(language, path=LANGUAGE_CONFIG_PATH):
    if language not in LANGUAGES: raise ValueError("Unsupported language")
    _write_app_config({"language": language}, path)

def load_max_concurrent_config(path=LANGUAGE_CONFIG_PATH):
    try:
        value = int(_read_app_config(path).get("max_concurrent", 2))
    except (TypeError, ValueError):
        value = 2
    return min(10, max(1, value))

def save_max_concurrent_config(value, path=LANGUAGE_CONFIG_PATH):
    value = int(value)
    if not 1 <= value <= 10:
        raise ValueError("max_concurrent must be between 1 and 10")
    _write_app_config({"max_concurrent": value}, path)

CLOSE_ACTIONS = ("ask", "reduce", "close")

def load_close_action(path=LANGUAGE_CONFIG_PATH):
    value = _read_app_config(path).get("close_action", "ask")
    return value if value in CLOSE_ACTIONS else "ask"

def save_close_action(action, path=LANGUAGE_CONFIG_PATH):
    if action not in CLOSE_ACTIONS:
        raise ValueError("Invalid close action")
    _write_app_config({"close_action": action}, path)

RATE_UNITS = {"KB/s": 1024, "MB/s": 1024 ** 2, "GB/s": 1024 ** 3}


def rate_to_bytes_per_second(value, unit):
    """Converte il valore mostrato nelle impostazioni in byte al secondo."""
    if unit not in RATE_UNITS:
        raise ValueError("Unsupported rate unit")
    try:
        amount = Decimal(str(value).strip() or "0")
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid rate") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("Rate must be a finite non-negative number")
    return int((amount * RATE_UNITS[unit]).to_integral_value(rounding=ROUND_HALF_UP))


def format_rate_limit(bytes_per_second):
    """Restituisce il valore in MB/s, l'unica unità usata dall'interfaccia."""
    value = max(0, int(bytes_per_second))
    if value == 0:
        return "0", "MB/s"
    amount = (Decimal(value) / RATE_UNITS["MB/s"]).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP)
    return format(amount.normalize(), "f"), "MB/s"


def load_download_limits_config(path=LANGUAGE_CONFIG_PATH):
    config = _read_app_config(path)
    result = {}
    for key, default, maximum in (("download_rate_limit", 0, 10**12), ("upload_rate_limit", 0, 10**12), ("connections_limit", 200, 1000)):
        try: value = int(config.get(key, default))
        except (TypeError, ValueError): value = default
        result[key] = min(maximum, max(0 if key != "connections_limit" else 1, value))
    return result

def save_download_limits_config(download_rate_limit, upload_rate_limit, connections_limit, path=LANGUAGE_CONFIG_PATH):
    values = {"download_rate_limit": int(download_rate_limit), "upload_rate_limit": int(upload_rate_limit), "connections_limit": int(connections_limit)}
    if any(v < 0 for k, v in values.items() if k != "connections_limit") or not 1 <= values["connections_limit"] <= 1000:
        raise ValueError("Invalid download limits")
    _write_app_config(values, path)


def _normalize_torrent_search_api(row):
    if not isinstance(row, dict):
        return None
    name = str(row.get("name") or "").strip()[:80]
    url = str(row.get("url") or "").strip()
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url.replace("{query}", "test"))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return {"name": name or parsed.netloc, "url": url}


def load_torrent_search_apis(path=LANGUAGE_CONFIG_PATH):
    config = _read_app_config(path)
    rows = config.get("torrent_search_apis")
    result = []
    if isinstance(rows, list):
        for row in rows:
            api = _normalize_torrent_search_api(row)
            if api is not None:
                result.append(api)
    if not result:
        legacy = config.get("torrent_search_api")
        if isinstance(legacy, str) and legacy.strip():
            api = _normalize_torrent_search_api(
                {"name": "API personalizzata", "url": legacy})
            if api is not None:
                result.append(api)
    return result


def save_torrent_search_apis(rows, path=LANGUAGE_CONFIG_PATH):
    cleaned = []
    for row in rows or ():
        api = _normalize_torrent_search_api(row)
        if api is not None:
            cleaned.append(api)
    _write_app_config({"torrent_search_apis": cleaned,
                       "torrent_search_api": ""}, path)


def load_torrent_search_api(path=LANGUAGE_CONFIG_PATH):
    apis = load_torrent_search_apis(path)
    return apis[0]["url"] if apis else ""


def save_torrent_search_api(url, path=LANGUAGE_CONFIG_PATH):
    value = (url or "").strip()
    if value:
        api = _normalize_torrent_search_api(
            {"name": "API personalizzata", "url": value})
        if api is None:
            raise ValueError("Invalid torrent search API URL")
        save_torrent_search_apis([api], path)
    else:
        save_torrent_search_apis([], path)

class SharedDownloadThrottle:
    """Byte-rate limiter shared by all active HTTP transfer threads."""
    def __init__(self): self._lock = threading.Lock(); self._rate = 0; self._next = 0.0
    def set_rate(self, bytes_per_second):
        with self._lock:
            self._rate = max(0, int(bytes_per_second))
            self._next = time.monotonic()
    def acquire(self, byte_count):
        with self._lock:
            rate = self._rate
            if rate <= 0: return
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + max(0, byte_count) / rate
        delay = start - now
        if delay > 0: time.sleep(delay)

HTTP_DOWNLOAD_THROTTLE = SharedDownloadThrottle()

# Catalogos completos ES/FR: phrase-level mappings; fallback preserves technical tokens.
_ES_TEXT = {
"  /  GESTORE DOWNLOAD":"  /  GESTOR DE DESCARGAS","TRASFERIMENTI  •  HTTP / HTTPS / MAGNET":"TRANSFERENCIAS  •  HTTP / HTTPS / MAGNET","NUOVO DOWNLOAD":"NUEVA DESCARGA","＋  Aggiungi":"＋  Añadir","DESTINAZIONE":"DESTINO","Scegli cartella…":"Elegir carpeta…","▶  Avvia coda":"▶  Iniciar cola","RIMUOVI":"ELIMINAR","Sei sicuro di rimuovere i file in download?":"¿Seguro que quieres eliminar los archivos en descarga?","NOME FILE":"NOMBRE DEL ARCHIVO","PROGRESSO":"PROGRESO","DIMENSIONE":"TAMAÑO","VELOCITÀ":"VELOCIDAD","PEER":"PARES","PRIORITÀ":"PRIORIDAD","STATO":"ESTADO","Taglia":"Cortar","Copia":"Copiar","Incolla":"Pegar","Seleziona tutto":"Seleccionar todo","Informazioni file…":"Información del archivo…","Priorità":"Prioridad","Alta":"Alta","Normale":"Normal","Bassa":"Baja","Scarica ora":"Descargar ahora","Pausa/Riprendi":"Pausar/Reanudar","Pausa / riprendi gruppo":"Pausar / reanudar grupo","Annulla":"Cancelar","Rimuovi dalla lista":"Eliminar de la lista","Apri cartella":"Abrir carpeta","Doppio clic su una riga per avviare subito, fuori dai limiti della coda.":"Haz doble clic en una fila para iniciar inmediatamente, sin respetar el límite de la cola.","FILE TEMPORANEI  ·  .part":"ARCHIVOS TEMPORALES  ·  .part","Impostazioni":"Configuración","Download simultanei":"Descargas simultáneas","Numero di download simultanei (1–10)":"Número de descargas simultáneas (1–10)","Applica":"Aplicar","Lingua":"Idioma","Predefinito di sistema":"Predeterminado del sistema","English":"Inglés","Deutsch":"Alemán","Italiano":"Italiano","Español":"Español","Français":"Francés","Tipo MIME":"Tipo MIME","Info hash":"Hash de información","Torrent…":"Torrent…","Español":"Español","In coda":"En cola","In avvio":"In inicio","In corso":"Descargando","In pausa":"En pausa","Annullato":"Cancelado","Completato":"Completado","Errore":"Error","Annullamento…":"Cancelando…","GESTORE DOWNLOAD":"GESTOR DE DESCARGAS","TRASFERIMENTI":"TRANSFERENCIAS","Incolla automaticamente":"Pegar automáticamente",
"Inserisci un URL HTTP o HTTPS valido.":"Introduce una URL HTTP o HTTPS válida.","Il torrent contiene un percorso file non sicuro.":"El torrent contiene una ruta de archivo no segura.","Percorso torrent non sicuro.":"Ruta de torrent no segura.","La sessione torrent è già stata chiusa.":"La sesión torrent ya se ha cerrado.","Il torrent si è arrestato prima del completamento del file.":"El torrent se detuvo antes de completar el archivo.","Sovrascrittura rifiutata":"Sobrescritura rechazada","Download annullato":"Descarga cancelada","Ricerca metadati torrent…":"Buscando metadatos del torrent…","Ricerca metadati (timeout 60 s)…":"Buscando metadatos (límite de 60 s)…","Libtorrent non installato":"Libtorrent no está instalado","Installazione libtorrent in corso…":"Instalando libtorrent…","In attesa":"En espera","Risoluzione link…":"Resolviendo enlace…","Connessione…":"Conectando…","Dominio non supportato":"Dominio no compatible","Scegli datanodes.to o fuckingfast.co.":"Elige datanodes.to o fuckingfast.co.","Libtorrent non disponibile":"Libtorrent no disponible","libtorrent non è installato. Vuoi installarlo ora?":"libtorrent no está instalado. ¿Quieres instalarlo ahora?","Installazione non riuscita":"La instalación ha fallado","Non è stato possibile installare libtorrent. Consulta il crash log.":"No se pudo instalar libtorrent. Consulta el registro de errores.","Magnet non valido":"Magnet no válido","Inserisci un magnet con hash BTIH valido.":"Introduce un magnet con un hash BTIH válido.","Download attivo":"Descarga activa","Questo file è già in download.":"Este archivo ya se está descargando.","Annulla il trasferimento prima di rimuoverlo.":"Cancela la transferencia antes de eliminarla.","Impossibile aprire la cartella":"No se pudo abrir la carpeta","Informazioni file":"Información del archivo","DETTAGLI TRASFERIMENTO":"DETALLES DE LA TRANSFERENCIA","Nome":"Nombre","Dimensione":"Tamaño","Tipo MIME":"Tipo MIME","Percorso":"Ruta","Stato":"Estado","Byte scaricati":"Bytes descargados","Peer":"Pares","Info hash":"Hash de información","Chiudi":"Cerrar","Release GitHub":"Versión de GitHub","La release non contiene asset scaricabili.":"La versión no contiene archivos descargables.","Errore release GitHub":"Error de versión de GitHub","Conferma sovrascrittura":"Confirmar sobrescritura","Il file esiste già:\n{0}\n\nVuoi sovrascriverlo?":"El archivo ya existe:\n{0}\n\n¿Quieres sobrescribirlo?","Aggiungi":"Añadir","Asset release GitHub":"Archivos de la versión de GitHub","SELEZIONA I FILE DELLA RELEASE":"SELECCIONA LOS ARCHIVOS DE LA VERSIÓN"," elementi disponibili":" elementos disponibles","Dimensione n/d":"Tamaño n/d","Sorgente":"Origen","Aggiungi selezionati":"Añadir seleccionados","URL non valido":"URL no válida","Il link punta a una pagina web, non a un file":"El enlace apunta a una página web, no a un archivo","Inserisci un magnet valido.":"Introduce un magnet válido.","Magnet non valido: è richiesto un hash BTIH (xt=urn:btih:…).":"Magnet no válido: se requiere un hash BTIH (xt=urn:btih:…).","Ricerca metadati annullata":"Búsqueda de metadatos cancelada","Timeout: metadati torrent non ricevuti entro 60 secondi. Verifica rete, tracker o peer.":"Tiempo agotado: no se recibieron los metadatos del torrent en 60 segundos. Comprueba la red, los trackers o los pares.","Libtorrent not installed":"Libtorrent no está instalado","Installazione libtorrent annullata.":"Instalación de libtorrent cancelada.","Impossibile mostrare il prompt di installazione libtorrent":"No se pudo mostrar el aviso de instalación de libtorrent","pip terminato con codice ":"pip finalizó con el código ","Limite richieste GitHub raggiunto. Riprova più tardi.":"Se alcanzó el límite de solicitudes de GitHub. Inténtalo más tarde.","Release GitHub non trovata (404). Verifica repository e tag.":"No se encontró la versión de GitHub (404). Comprueba el repositorio y la etiqueta.","Errore API GitHub HTTP ":"Error HTTP de la API de GitHub ","Impossibile recuperare la release GitHub: ":"No se pudo obtener la versión de GitHub: ","HEAD: ":"HEAD: "," peer":" pares","Impossibile installare libtorrent.":"No se pudo instalar libtorrent.","Il file esiste già:\n":"El archivo ya existe:\n","\n\nVuoi sovrascriverlo?":"\n\n¿Quieres sobrescribirlo?","Seleziona i file del torrent":"Selecciona los archivos del torrent","Recupero metadati torrent…":"Obteniendo metadatos del torrent…","Torrent…":"Torrent…","Apri file torrent":"Abrir archivos torrent","Seleziona un file .torrent valido.":"Selecciona un archivo .torrent válido.","Seleziona":"Seleccionar","Deseleziona":"Deseleccionar","Pausa":"Pausar","Riprendi":"Reanudar","Rimuovi":"Eliminar","Avvia ora":"Iniciar ahora","Recupero metadati…":"Obteniendo metadatos…","Conferma":"Confirmar","Totale selezionato:":"Total seleccionado:","Priorità applicata":"Prioridad aplicada","Español":"Español","Français":"Francés","Download":"Descargas","Seleziona la lingua dell'interfaccia":"Selecciona el idioma de la interfaz","Limiti di velocità: 0 = illimitata":"Límites de velocidad: 0 = ilimitada","Velocità massima download":"Velocidad máxima de descarga","Velocità massima upload":"Velocidad máxima de subida","Numero massimo peer":"Número máximo de pares","Valore non valido nelle impostazioni.":"Valor de configuración no válido."
}
_FR_TEXT = {
"  /  GESTORE DOWNLOAD":"  /  GESTIONNAIRE DE TÉLÉCHARGEMENT","TRASFERIMENTI  •  HTTP / HTTPS / MAGNET":"TRANSFERTS  •  HTTP / HTTPS / MAGNET","NUOVO DOWNLOAD":"NOUVEAU TÉLÉCHARGEMENT","＋  Aggiungi":"＋  Ajouter","DESTINAZIONE":"DESTINATION","Scegli cartella…":"Choisir un dossier…","▶  Avvia coda":"▶  Démarrer la file","RIMUOVI":"SUPPRIMER","Sei sicuro di rimuovere i file in download?":"Voulez-vous vraiment supprimer les fichiers en cours de téléchargement ?","NOME FILE":"NOM DU FICHIER","PROGRESSO":"PROGRESSION","DIMENSIONE":"TAILLE","VELOCITÀ":"VITESSE","PEER":"PAIRS","PRIORITÀ":"PRIORITÉ","STATO":"ÉTAT","Taglia":"Couper","Copia":"Copier","Incolla":"Coller","Seleziona tutto":"Tout sélectionner","Informazioni file…":"Informations sur le fichier…","Priorità":"Priorité","Alta":"Élevée","Normale":"Normale","Bassa":"Basse","Info hash":"Empreinte info","Torrent…":"Torrent…","Français":"Français","Scarica ora":"Télécharger maintenant","Pausa/Riprendi":"Pause/Reprendre","Pausa / riprendi gruppo":"Mettre en pause / reprendre le groupe","Annulla":"Annuler","Rimuovi dalla lista":"Retirer de la liste","Apri cartella":"Ouvrir le dossier","Doppio clic su una riga per avviare subito, fuori dai limiti della coda.":"Double-cliquez sur une ligne pour démarrer immédiatement, sans tenir compte de la limite de la file.","FILE TEMPORANEI  ·  .part":"FICHIERS TEMPORAIRES  ·  .part","Impostazioni":"Paramètres","Download simultanei":"Téléchargements simultanés","Numero di download simultanei (1–10)":"Nombre de téléchargements simultanés (1–10)","Applica":"Appliquer","Lingua":"Langue","Predefinito di sistema":"Par défaut du système","English":"Anglais","Deutsch":"Allemand","Italiano":"Italien","Español":"Espagnol","Français":"Français","In coda":"En attente","In avvio":"Démarrage","In corso":"En cours","In pausa":"En pause","Annullato":"Annulé","Completato":"Terminé","Errore":"Erreur","Annullamento…":"Annulation…","GESTORE DOWNLOAD":"GESTIONNAIRE DE TÉLÉCHARGEMENT","TRASFERIMENTI":"TRANSFERTS","Incolla automaticamente":"Coller automatiquement",
"Inserisci un URL HTTP o HTTPS valido.":"Saisissez une URL HTTP ou HTTPS valide.","Il torrent contiene un percorso file non sicuro.":"Le torrent contient un chemin de fichier non sécurisé.","Percorso torrent non sicuro.":"Chemin torrent non sécurisé.","La sessione torrent è già stata chiusa.":"La session torrent est déjà fermée.","Il torrent si è arrestato prima del completamento del file.":"Le torrent s'est arrêté avant la fin du fichier.","Sovrascrittura rifiutata":"Écrasement refusé","Download annullato":"Téléchargement annulé","Ricerca metadati torrent…":"Recherche des métadonnées du torrent…","Ricerca metadati (timeout 60 s)…":"Recherche des métadonnées (délai de 60 s)…","Libtorrent non installato":"Libtorrent non installé","Installazione libtorrent in corso…":"Installation de libtorrent…","In attesa":"En attente","Risoluzione link…":"Résolution du lien…","Connessione…":"Connexion…","Dominio non supportato":"Domaine non pris en charge","Scegli datanodes.to o fuckingfast.co.":"Choisissez datanodes.to ou fuckingfast.co.","Libtorrent non disponibile":"Libtorrent indisponible","libtorrent non è installato. Vuoi installarlo ora?":"libtorrent n'est pas installé. Voulez-vous l'installer maintenant ?","Installazione non riuscita":"Échec de l'installation","Non è stato possibile installare libtorrent. Consulta il crash log.":"Impossible d'installer libtorrent. Consultez le journal d'erreurs.","Magnet non valido":"Magnet invalide","Inserisci un magnet con hash BTIH valido.":"Saisissez un lien magnet avec un hash BTIH valide.","Download attivo":"Téléchargement actif","Questo file è già in download.":"Ce fichier est déjà en cours de téléchargement.","Annulla il trasferimento prima di rimuoverlo.":"Annulez le transfert avant de le supprimer.","Impossibile aprire la cartella":"Impossible d'ouvrir le dossier","Informazioni file":"Informations sur le fichier","DETTAGLI TRASFERIMENTO":"DÉTAILS DU TRANSFERT","Nome":"Nom","Dimensione":"Taille","Tipo MIME":"Type MIME","Percorso":"Chemin","Stato":"État","Byte scaricati":"Octets téléchargés","Peer":"Pairs","Info hash":"Empreinte d’information","Chiudi":"Fermer","Release GitHub":"Version GitHub","La release non contiene asset scaricabili.":"La version ne contient aucun fichier téléchargeable.","Errore release GitHub":"Erreur de version GitHub","Conferma sovrascrittura":"Confirmer l'écrasement","Il file esiste già:\n{0}\n\nVuoi sovrascriverlo?":"Le fichier existe déjà :\n{0}\n\nVoulez-vous l'écraser ?","Aggiungi":"Ajouter","Asset release GitHub":"Fichiers de la version GitHub","SELEZIONA I FILE DELLA RELEASE":"SÉLECTIONNER LES FICHIERS DE LA VERSION"," elementi disponibili":" éléments disponibles","Dimensione n/d":"Taille n/d","Sorgente":"Source","Aggiungi selezionati":"Ajouter la sélection","URL non valido":"URL invalide","Il link punta a una pagina web, non a un file":"Le lien pointe vers une page Web, pas vers un fichier","Inserisci un magnet valido.":"Saisissez un lien magnet valide.","Magnet non valido: è richiesto un hash BTIH (xt=urn:btih:…).":"Magnet invalide : un hash BTIH (xt=urn:btih:…) est requis.","Ricerca metadati annullata":"Recherche des métadonnées annulée","Timeout: metadati torrent non ricevuti entro 60 secondi. Verifica rete, tracker o peer.":"Délai dépassé : les métadonnées torrent n'ont pas été reçues en 60 secondes. Vérifiez le réseau, les trackers ou les pairs.","Libtorrent not installed":"Libtorrent non installé","Installazione libtorrent annullata.":"Installation de libtorrent annulée.","Impossibile mostrare il prompt di installazione libtorrent":"Impossible d'afficher l'invite d'installation de libtorrent","pip terminato con codice ":"pip s'est terminé avec le code ","Limite richieste GitHub raggiunto. Riprova più tardi.":"Limite de requêtes GitHub atteint. Réessayez plus tard.","Release GitHub non trovata (404). Verifica repository e tag.":"Version GitHub introuvable (404). Vérifiez le dépôt et le tag.","Errore API GitHub HTTP ":"Erreur HTTP de l'API GitHub ","Impossibile recuperare la release GitHub: ":"Impossible de récupérer la version GitHub : ","HEAD: ":"HEAD : "," peer":" pairs","Impossibile installare libtorrent.":"Impossible d'installer libtorrent.","Il file esiste già:\n":"Le fichier existe déjà :\n","\n\nVuoi sovrascriverlo?":"\n\nVoulez-vous l'écraser ?","Seleziona i file del torrent":"Sélectionner les fichiers torrent","Recupero metadati torrent…":"Récupération des métadonnées torrent…","Torrent…":"Torrent…","Apri file torrent":"Ouvrir des fichiers torrent","Seleziona un file .torrent valido.":"Sélectionnez un fichier .torrent valide.","Seleziona":"Sélectionner","Deseleziona":"Désélectionner","Pausa":"Pause","Riprendi":"Reprendre","Rimuovi":"Supprimer","Avvia ora":"Démarrer maintenant","Recupero metadati…":"Récupération des métadonnées…","Conferma":"Confirmer","Totale selezionato:":"Total sélectionné :","Priorità applicata":"Priorité appliquée","Español":"Espagnol","Français":"Français","Download":"Téléchargements","Seleziona la lingua dell'interfaccia":"Sélectionnez la langue de l'interface","Limiti di velocità: 0 = illimitata":"Limites de vitesse : 0 = illimitée","Velocità massima download":"Vitesse maximale de téléchargement","Velocità massima upload":"Vitesse maximale d'envoi","Numero massimo peer":"Nombre maximal de pairs","Valore non valido nelle impostazioni.":"Valeur de paramètre invalide."
}
ES_STRINGS = {key: _ES_TEXT.get(key, key) for key in UI_STRINGS}
FR_STRINGS = {key: _FR_TEXT.get(key, key) for key in UI_STRINGS}
ES_STRINGS.update({
 "Cookie host protetti": "Cookies de hosts protegidos",
 "Cookie host protetti: incolla l'header Cookie del browser per datanodes.to o fuckingfast.co.":
     "Cookies de hosts protegidos: pega el encabezado Cookie del navegador para datanodes.to o fuckingfast.co.",
 "Salva": "Guardar", "Cookie salvato": "Cookie guardada",
 "Impossibile salvare il cookie": "No se pudo guardar la cookie",
 "Salvato in": "Guardado en", "Svuota coda": "Vaciar cola",
 "Importa lista link": "Importar lista de enlaces",
 "Importazione": "Importación",
 " elementi": " elementos",
 "Importa lista link: incolla un paste PrivateBin e/o un elenco di link.":
     "Importar lista de enlaces: pega un paste de PrivateBin y/o una lista de enlaces.",
 "Link paste (PrivateBin)": "Enlace del paste (PrivateBin)",
 "Link manuali (uno per riga)": "Enlaces manuales (uno por línea)",
 "Importa": "Importar", "Lettura paste…": "Leyendo paste…",
 "Importazione…": "Importando…",
 "Importazione completata": "Importación completada",
 " link aggiunti alla coda.": " enlaces añadidos a la cola.",
 "Nessun link trovato.": "No se encontraron enlaces.",
 "Tutti i link sono già presenti nella coda.":
     "Todos los enlaces ya están en la cola.",
 "Il paste non contiene link.": "El paste no contiene enlaces.",
 "Errore importazione": "Error de importación",
 "Inserisci almeno un link o un paste da importare.":
     "Introduce al menos un enlace o un paste para importar.",
 "I file verranno aggiunti alla cartella di destinazione corrente.":
     "Los archivos se añadirán a la carpeta de destino actual.",
 "Cerca file…": "Buscar archivos…", "Cerca cartella…": "Buscar carpeta…",
 "Cartelle": "Carpetas",
 "Apri": "Abrir", "Scegli cartella": "Elegir carpeta",
 "Seleziona cartella": "Seleccionar carpeta",
 "Data modifica": "Fecha de modificación",
 "Tipo": "Tipo", "Cartella": "Carpeta", "File torrent": "Archivo torrent",
 "Inserisci un Link, un magnet o trascina un file .torrent sotto":
     "Introduce un enlace, un magnet o arrastra un archivo .torrent abajo",
 "Trascina qui un file Torrent": "Arrastra aquí un archivo Torrent",
 "Informazioni": "Información", "Versione software": "Versión del software",
 "Autore": "Autor", "Controlla aggiornamenti": "Buscar actualizaciones",
 "Aggiornamento": "Actualización",
 "Controllo aggiornamenti…": "Buscando actualizaciones…",
 "Nessun aggiornamento disponibile.": "No hay actualizaciones disponibles.",
 "Nuova versione disponibile: {0}": "Nueva versión disponible: {0}",
 "Aggiornamento annullato.": "Actualización cancelada.",
 "Nuova versione {0} disponibile. Scaricare e installare ora?\nL'applicazione verrà chiusa.":
     "La versión {0} está disponible. ¿Descargar e instalar ahora?\nLa aplicación se cerrará.",
 "Download aggiornamento…": "Descargando actualización…",
 "Aggiornamento scaricato. Avvio del setup…":
     "Actualización descargada. Iniciando el instalador…",
 "Impossibile controllare gli aggiornamenti.":
     "No se pudieron buscar actualizaciones.",
 "Errore download aggiornamento.":
     "Error al descargar la actualización.",
 "Aggiornamento non supportato su questo sistema.":
     "Las actualizaciones no son compatibles con este sistema.",
 "Uscita": "Salir",
 "Chiudere completamente o ridurre a icona?":
     "¿Cerrar completamente o minimizar a la bandeja?",
 "Non chiedermelo più": "No volver a preguntar",
 "Riduci": "Minimizar",
 "Reimposta richiesta di chiusura":
     "Restablecer pregunta de cierre",
 "Richiesta di chiusura ripristinata.":
     "Pregunta de cierre restablecida.",
})
FR_STRINGS.update({
 "Cookie host protetti": "Cookies des hôtes protégés",
 "Cookie host protetti: incolla l'header Cookie del browser per datanodes.to o fuckingfast.co.":
     "Cookies des hôtes protégés : collez l'en-tête Cookie du navigateur pour datanodes.to ou fuckingfast.co.",
 "Salva": "Enregistrer", "Cookie salvato": "Cookie enregistré",
 "Impossibile salvare il cookie": "Impossible d'enregistrer le cookie",
 "Salvato in": "Enregistré dans", "Svuota coda": "Vider la file",
 "Importa lista link": "Importer une liste de liens",
 "Importazione": "Importation",
 " elementi": " éléments",
 "Importa lista link: incolla un paste PrivateBin e/o un elenco di link.":
     "Importer une liste de liens : collez un paste PrivateBin et/ou une liste de liens.",
 "Link paste (PrivateBin)": "Lien du paste (PrivateBin)",
 "Link manuali (uno per riga)": "Liens manuels (un par ligne)",
 "Importa": "Importer", "Lettura paste…": "Lecture du paste…",
 "Importazione…": "Importation…",
 "Importazione completata": "Importation terminée",
 " link aggiunti alla coda.": " liens ajoutés à la file.",
 "Nessun link trovato.": "Aucun lien trouvé.",
 "Tutti i link sono già presenti nella coda.":
     "Tous les liens sont déjà dans la file.",
 "Il paste non contiene link.": "Le paste ne contient aucun lien.",
 "Errore importazione": "Erreur d'importation",
 "Inserisci almeno un link o un paste da importare.":
     "Saisissez au moins un lien ou un paste à importer.",
 "I file verranno aggiunti alla cartella di destinazione corrente.":
     "Les fichiers seront ajoutés au dossier de destination actuel.",
 "Cerca file…": "Rechercher des fichiers…", "Cerca cartella…": "Rechercher un dossier…",
 "Cartelle": "Dossiers",
 "Apri": "Ouvrir", "Scegli cartella": "Choisir un dossier",
 "Seleziona cartella": "Sélectionner un dossier",
 "Data modifica": "Date de modification",
 "Tipo": "Type", "Cartella": "Dossier", "File torrent": "Fichier torrent",
 "Inserisci un Link, un magnet o trascina un file .torrent sotto":
     "Saisissez un lien, un magnet ou faites glisser un fichier .torrent ci-dessous",
 "Trascina qui un file Torrent": "Faites glisser un fichier Torrent ici",
 "Informazioni": "Informations", "Versione software": "Version du logiciel",
 "Autore": "Auteur", "Controlla aggiornamenti": "Rechercher des mises à jour",
 "Aggiornamento": "Mise à jour",
 "Controllo aggiornamenti…": "Recherche de mises à jour…",
 "Nessun aggiornamento disponibile.": "Aucune mise à jour disponible.",
 "Nuova versione disponibile: {0}": "Nouvelle version disponible : {0}",
 "Aggiornamento annullato.": "Mise à jour annulée.",
 "Nuova versione {0} disponibile. Scaricare e installare ora?\nL'applicazione verrà chiusa.":
     "La version {0} est disponible. Télécharger et installer maintenant ?\nL'application va se fermer.",
 "Download aggiornamento…": "Téléchargement de la mise à jour…",
 "Aggiornamento scaricato. Avvio del setup…":
     "Mise à jour téléchargée. Démarrage de l'installation…",
 "Impossibile controllare gli aggiornamenti.":
     "Impossible de rechercher les mises à jour.",
 "Errore download aggiornamento.":
     "Erreur lors du téléchargement de la mise à jour.",
 "Aggiornamento non supportato su questo sistema.":
     "Les mises à jour ne sont pas prises en charge sur ce système.",
 "Uscita": "Quitter",
 "Chiudere completamente o ridurre a icona?":
     "Fermer complètement ou réduire dans la zone de notification ?",
 "Non chiedermelo più": "Ne plus me demander",
 "Riduci": "Réduire",
 "Reimposta richiesta di chiusura":
     "Réinitialiser la demande de fermeture",
 "Richiesta di chiusura ripristinata.":
     "Demande de fermeture réinitialisée.",
})

UI_STRINGS["slenter97 con DeepSeek"] = ("slenter97 with DeepSeek",
                                        "slenter97 mit DeepSeek")
ES_STRINGS["slenter97 con DeepSeek"] = "slenter97 con DeepSeek"
FR_STRINGS["slenter97 con DeepSeek"] = "slenter97 avec DeepSeek"

UI_STRINGS["Avvio automatico"] = ("Automatic startup", "Automatischer Start")
ES_STRINGS["Avvio automatico"] = "Inicio automático"
FR_STRINGS["Avvio automatico"] = "Démarrage automatique"

UI_STRINGS["Avvia DOWNLOADVERSE all'avvio di Windows"] = (
    "Start DOWNLOADVERSE with Windows",
    "DOWNLOADVERSE beim Start von Windows starten")
ES_STRINGS["Avvia DOWNLOADVERSE all'avvio di Windows"] = (
    "Iniciar DOWNLOADVERSE con Windows")
FR_STRINGS["Avvia DOWNLOADVERSE all'avvio di Windows"] = (
    "Démarrer DOWNLOADVERSE avec Windows")

UI_STRINGS["Cerca torrent"] = ("Search torrents", "Torrents suchen")
ES_STRINGS["Cerca torrent"] = "Buscar torrents"
FR_STRINGS["Cerca torrent"] = "Rechercher des torrents"

UI_STRINGS["CERCA TORRENT"] = ("SEARCH TORRENTS", "TORRENTS SUCHEN")
ES_STRINGS["CERCA TORRENT"] = "BUSCAR TORRENTS"
FR_STRINGS["CERCA TORRENT"] = "RECHERCHER DES TORRENTS"

UI_STRINGS["Cerca"] = ("Search", "Suchen")
ES_STRINGS["Cerca"] = "Buscar"
FR_STRINGS["Cerca"] = "Rechercher"

UI_STRINGS["Inserisci il nome di un torrent e premi Cerca."] = (
    "Enter a torrent name and press Search.",
    "Geben Sie einen Torrent-Namen ein und klicken Sie auf Suchen.")
ES_STRINGS["Inserisci il nome di un torrent e premi Cerca."] = (
    "Introduce el nombre de un torrent y pulsa Buscar.")
FR_STRINGS["Inserisci il nome di un torrent e premi Cerca."] = (
    "Saisissez le nom d'un torrent et appuyez sur Rechercher.")

UI_STRINGS["Ricerca in corso…"] = ("Searching…", "Suche läuft…")
ES_STRINGS["Ricerca in corso…"] = "Buscando…"
FR_STRINGS["Ricerca in corso…"] = "Recherche…"

UI_STRINGS["{0} risultati trovati."] = (
    "{0} results found.", "{0} Ergebnisse gefunden.")
ES_STRINGS["{0} risultati trovati."] = "{0} resultados encontrados."
FR_STRINGS["{0} risultati trovati."] = "{0} résultats trouvés."

UI_STRINGS["Nessun risultato trovato."] = (
    "No results found.", "Keine Ergebnisse gefunden.")
ES_STRINGS["Nessun risultato trovato."] = "No se encontraron resultados."
FR_STRINGS["Nessun risultato trovato."] = "Aucun résultat trouvé."

UI_STRINGS["Aggiungi alla coda"] = ("Add to queue",
                                    "Zur Warteschlange hinzufügen")
ES_STRINGS["Aggiungi alla coda"] = "Añadir a la cola"
FR_STRINGS["Aggiungi alla coda"] = "Ajouter à la file"

UI_STRINGS["Aggiunto alla coda."] = ("Added to queue.",
                                     "Zur Warteschlange hinzugefügt.")
ES_STRINGS["Aggiunto alla coda."] = "Añadido a la cola."
FR_STRINGS["Aggiunto alla coda."] = "Ajouté à la file."

UI_STRINGS["Seleziona un risultato da aggiungere."] = (
    "Select a result to add.", "Wählen Sie ein Ergebnis zum Hinzufügen.")
ES_STRINGS["Seleziona un risultato da aggiungere."] = (
    "Selecciona un resultado para añadir.")
FR_STRINGS["Seleziona un risultato da aggiungere."] = (
    "Sélectionnez un résultat à ajouter.")

UI_STRINGS["SEED"] = ("SEEDS", "SEEDS")
ES_STRINGS["SEED"] = "SEEDS"
FR_STRINGS["SEED"] = "SEEDS"

UI_STRINGS["FONTE"] = ("SOURCE", "QUELLE")
ES_STRINGS["FONTE"] = "FUENTE"
FR_STRINGS["FONTE"] = "SOURCE"

UI_STRINGS["NOME"] = ("NAME", "NAME")
ES_STRINGS["NOME"] = "NOMBRE"
FR_STRINGS["NOME"] = "NOM"

UI_STRINGS["Attenzione: il torrent contiene la stagione {found}, non la stagione {expected}."] = (
    "Warning: this torrent contains season {found}, not season {expected}.",
    "Achtung: Dieser Torrent enthält Staffel {found}, nicht Staffel {expected}.")
ES_STRINGS["Attenzione: il torrent contiene la stagione {found}, non la stagione {expected}."] = (
    "Atención: el torrent contiene la temporada {found}, no la temporada {expected}.")
FR_STRINGS["Attenzione: il torrent contiene la stagione {found}, non la stagione {expected}."] = (
    "Attention : ce torrent contient la saison {found}, pas la saison {expected}.")

UI_STRINGS["FONTI DI RICERCA"] = ("SEARCH SOURCES", "SUCHQUELLEN")
ES_STRINGS["FONTI DI RICERCA"] = "FUENTES DE BÚSQUEDA"
FR_STRINGS["FONTI DI RICERCA"] = "SOURCES DE RECHERCHE"

UI_STRINGS["La ricerca torrent usa solo fonti legali: Internet Archive (pubblico dominio, Creative Commons, software libero)."] = (
    "Torrent search uses legal sources only: Internet Archive (public domain, "
    "Creative Commons, free software).",
    "Die Torrent-Suche verwendet nur legale Quellen: Internet Archive "
    "(Gemeinfreiheit, Creative Commons, freie Software).")
ES_STRINGS["La ricerca torrent usa solo fonti legali: Internet Archive (pubblico dominio, Creative Commons, software libero)."] = (
    "La búsqueda de torrents usa solo fuentes legales: Internet Archive "
    "(dominio público, Creative Commons, software libre).")
FR_STRINGS["La ricerca torrent usa solo fonti legali: Internet Archive (pubblico dominio, Creative Commons, software libero)."] = (
    "La recherche de torrents n'utilise que des sources légales : Internet Archive "
    "(domaine public, Creative Commons, logiciels libres).")

UI_STRINGS["API di ricerca personalizzata (facoltativa)"] = (
    "Custom search API (optional)", "Benutzerdefinierte Such-API (optional)")
ES_STRINGS["API di ricerca personalizzata (facoltativa)"] = (
    "API de búsqueda personalizada (opcional)")
FR_STRINGS["API di ricerca personalizzata (facoltativa)"] = (
    "API de recherche personnalisée (facultative)")

UI_STRINGS["URL dell'API con {query} al posto del testo cercato. La risposta deve essere JSON: elenco di risultati con name, size, seeders, leechers e magnet oppure torrent_url."] = (
    "API URL with {query} in place of the search text. The response must be JSON: "
    "a list of results with name, size, seeders, leechers and magnet or torrent_url.",
    "API-URL mit {query} anstelle des Suchtexts. Die Antwort muss JSON sein: eine "
    "Liste von Ergebnissen mit name, size, seeders, leechers und magnet oder torrent_url.")
ES_STRINGS["URL dell'API con {query} al posto del testo cercato. La risposta deve essere JSON: elenco di risultati con name, size, seeders, leechers e magnet oppure torrent_url."] = (
    "URL de la API con {query} en lugar del texto buscado. La respuesta debe ser JSON: "
    "una lista de resultados con name, size, seeders, leechers y magnet o torrent_url.")
FR_STRINGS["URL dell'API con {query} al posto del testo cercato. La risposta deve essere JSON: elenco di risultati con name, size, seeders, leechers e magnet oppure torrent_url."] = (
    "URL de l'API avec {query} à la place du texte recherché. La réponse doit être "
    "du JSON : une liste de résultats avec name, size, seeders, leechers et magnet ou torrent_url.")

UI_STRINGS["Lascia vuoto per usare solo Internet Archive."] = (
    "Leave empty to use Internet Archive only.",
    "Leer lassen, um nur Internet Archive zu verwenden.")
ES_STRINGS["Lascia vuoto per usare solo Internet Archive."] = (
    "Déjalo vacío para usar solo Internet Archive.")
FR_STRINGS["Lascia vuoto per usare solo Internet Archive."] = (
    "Laissez vide pour utiliser uniquement Internet Archive.")

UI_STRINGS["Il programma permette di default solo ricerche su fonti legali, eventuali usi non consoni sono un problema dell'utente."] = (
    "By default the program only searches legal sources; any inappropriate use "
    "is the user's responsibility.",
    "Standardmäßig durchsucht das Programm nur legale Quellen; eine unsachgemäße "
    "Nutzung liegt in der Verantwortung des Benutzers.")
ES_STRINGS["Il programma permette di default solo ricerche su fonti legali, eventuali usi non consoni sono un problema dell'utente."] = (
    "Por defecto, el programa solo busca en fuentes legales; cualquier uso "
    "inadecuado es responsabilidad del usuario.")
FR_STRINGS["Il programma permette di default solo ricerche su fonti legali, eventuali usi non consoni sono un problema dell'utente."] = (
    "Par défaut, le programme ne recherche que des sources légales ; tout usage "
    "inapproprié relève de la responsabilité de l'utilisateur.")

UI_STRINGS["Fonti legali: Internet Archive (pubblico dominio, Creative Commons, software libero) e le API configurate nelle impostazioni Torrent."] = (
    "Legal sources: Internet Archive (public domain, Creative Commons, free software) "
    "and the APIs configured in the Torrent settings.",
    "Legale Quellen: Internet Archive (Gemeinfreiheit, Creative Commons, freie Software) "
    "und die in den Torrent-Einstellungen konfigurierten APIs.")
ES_STRINGS["Fonti legali: Internet Archive (pubblico dominio, Creative Commons, software libero) e le API configurate nelle impostazioni Torrent."] = (
    "Fuentes legales: Internet Archive (dominio público, Creative Commons, software libre) "
    "y las API configuradas en los ajustes de Torrent.")
FR_STRINGS["Fonti legali: Internet Archive (pubblico dominio, Creative Commons, software libero) e le API configurate nelle impostazioni Torrent."] = (
    "Sources légales : Internet Archive (domaine public, Creative Commons, logiciels libres) "
    "et les API configurées dans les paramètres Torrent.")

UI_STRINGS["Download del file torrent…"] = (
    "Downloading torrent file…", "Torrent-Datei wird heruntergeladen…")
ES_STRINGS["Download del file torrent…"] = "Descargando archivo torrent…"
FR_STRINGS["Download del file torrent…"] = "Téléchargement du fichier torrent…"

UI_STRINGS["Impossibile scaricare il file torrent."] = (
    "Could not download the torrent file.", "Torrent-Datei konnte nicht heruntergeladen werden.")
ES_STRINGS["Impossibile scaricare il file torrent."] = (
    "No se pudo descargar el archivo torrent.")
FR_STRINGS["Impossibile scaricare il file torrent."] = (
    "Impossible de télécharger le fichier torrent.")

UI_STRINGS["Risultato non valido."] = (
    "Invalid result.", "Ungültiges Ergebnis.")
ES_STRINGS["Risultato non valido."] = "Resultado no válido."
FR_STRINGS["Risultato non valido."] = "Résultat non valide."

UI_STRINGS["API personalizzata"] = (
    "Custom API", "Benutzerdefinierte API")
ES_STRINGS["API personalizzata"] = "API personalizada"
FR_STRINGS["API personalizzata"] = "API personnalisée"

UI_STRINGS["Aggiungi un'API di ricerca"] = (
    "Add a search API", "Such-API hinzufügen")
ES_STRINGS["Aggiungi un'API di ricerca"] = "Añadir una API de búsqueda"
FR_STRINGS["Aggiungi un'API di ricerca"] = "Ajouter une API de recherche"

UI_STRINGS["Cerca e aggiungi"] = (
    "Search and add", "Suchen und hinzufügen")
ES_STRINGS["Cerca e aggiungi"] = "Buscar y añadir"
FR_STRINGS["Cerca e aggiungi"] = "Rechercher et ajouter"

UI_STRINGS["API"] = ("API", "API")
ES_STRINGS["API"] = "API"
FR_STRINGS["API"] = "API"

UI_STRINGS["Integrata"] = ("Built-in", "Integriert")
ES_STRINGS["Integrata"] = "Integrada"
FR_STRINGS["Integrata"] = "Intégrée"

UI_STRINGS["Scrivi il nome o l'URL dell'API."] = (
    "Enter the API name or URL.", "Geben Sie den API-Namen oder die URL ein.")
ES_STRINGS["Scrivi il nome o l'URL dell'API."] = (
    "Escribe el nombre o la URL de la API.")
FR_STRINGS["Scrivi il nome o l'URL dell'API."] = (
    "Saisissez le nom ou l'URL de l'API.")

UI_STRINGS["Internet Archive è già integrato."] = (
    "Internet Archive is already built in.", "Internet Archive ist bereits integriert.")
ES_STRINGS["Internet Archive è già integrato."] = (
    "Internet Archive ya está integrado.")
FR_STRINGS["Internet Archive è già integrato."] = (
    "Internet Archive est déjà intégré.")

UI_STRINGS["Servizio non riconosciuto. Inserisci il dominio o l'URL completo dell'API."] = (
    "Unrecognised service. Enter the domain or the full API URL.",
    "Dienst nicht erkannt. Geben Sie die Domain oder die vollständige API-URL ein.")
ES_STRINGS["Servizio non riconosciuto. Inserisci il dominio o l'URL completo dell'API."] = (
    "Servicio no reconocido. Introduce el dominio o la URL completa de la API.")
FR_STRINGS["Servizio non riconosciuto. Inserisci il dominio o l'URL completo dell'API."] = (
    "Service non reconnu. Saisissez le domaine ou l'URL complète de l'API.")

UI_STRINGS["API già aggiunta."] = (
    "API already added.", "API bereits hinzugefügt.")
ES_STRINGS["API già aggiunta."] = "API ya añadida."
FR_STRINGS["API già aggiunta."] = "API déjà ajoutée."

UI_STRINGS["Impossibile salvare l'API."] = (
    "Could not save the API.", "API konnte nicht gespeichert werden.")
ES_STRINGS["Impossibile salvare l'API."] = "No se pudo guardar la API."
FR_STRINGS["Impossibile salvare l'API."] = "Impossible d'enregistrer l'API."

UI_STRINGS["API aggiunta."] = ("API added.", "API hinzugefügt.")
ES_STRINGS["API aggiunta."] = "API añadida."
FR_STRINGS["API aggiunta."] = "API ajoutée."

UI_STRINGS["Ricerca API…"] = ("Searching for API…", "API wird gesucht…")
ES_STRINGS["Ricerca API…"] = "Buscando API…"
FR_STRINGS["Ricerca API…"] = "Recherche de l'API…"

UI_STRINGS["API non trovata per questo servizio."] = (
    "No API found for this service.", "Keine API für diesen Dienst gefunden.")
ES_STRINGS["API non trovata per questo servizio."] = (
    "No se encontró ninguna API para este servicio.")
FR_STRINGS["API non trovata per questo servizio."] = (
    "Aucune API trouvée pour ce service.")

UI_STRINGS["Seleziona un'API da rimuovere."] = (
    "Select an API to remove.", "Wählen Sie eine zu entfernende API aus.")
ES_STRINGS["Seleziona un'API da rimuovere."] = (
    "Selecciona una API para eliminar.")
FR_STRINGS["Seleziona un'API da rimuovere."] = (
    "Sélectionnez une API à supprimer.")

UI_STRINGS["La fonte integrata non può essere rimossa."] = (
    "The built-in source cannot be removed.",
    "Die integrierte Quelle kann nicht entfernt werden.")
ES_STRINGS["La fonte integrata non può essere rimossa."] = (
    "La fuente integrada no se puede eliminar.")
FR_STRINGS["La fonte integrata non può essere rimossa."] = (
    "La source intégrée ne peut pas être supprimée.")

UI_STRINGS["API rimossa."] = ("API removed.", "API entfernt.")
ES_STRINGS["API rimossa."] = "API eliminada."
FR_STRINGS["API rimossa."] = "API supprimée."

UI_STRINGS["Scrivi il nome di un servizio noto (es. Internet Archive) oppure il dominio/URL completo dell'API. Se l'API non viene trovata, incolla l'URL con {query} o [QUERY] al posto del testo cercato."] = (
    "Enter the name of a known service (e.g. Internet Archive) or the domain/full "
    "API URL. If the API is not found, paste the URL with {query} or [QUERY] in place "
    "of the search text.",
    "Geben Sie den Namen eines bekannten Dienstes (z. B. Internet Archive) oder "
    "die Domain/vollständige API-URL ein. Wenn die API nicht gefunden wird, fügen "
    "Sie die URL mit {query} oder [QUERY] anstelle des Suchtexts ein.")
ES_STRINGS["Scrivi il nome di un servizio noto (es. Internet Archive) oppure il dominio/URL completo dell'API. Se l'API non viene trovata, incolla l'URL con {query} o [QUERY] al posto del testo cercato."] = (
    "Escribe el nombre de un servicio conocido (p. ej. Internet Archive) o el "
    "dominio/URL completo de la API. Si no se encuentra la API, pega la URL con "
    "{query} o [QUERY] en lugar del texto buscado.")
FR_STRINGS["Scrivi il nome di un servizio noto (es. Internet Archive) oppure il dominio/URL completo dell'API. Se l'API non viene trovata, incolla l'URL con {query} o [QUERY] al posto del testo cercato."] = (
    "Saisissez le nom d'un service connu (par ex. Internet Archive) ou le "
    "domaine/l'URL complète de l'API. Si l'API n'est pas trouvée, collez l'URL "
    "avec {query} ou [QUERY] à la place du texte recherché.")

UI_STRINGS["Verifica"] = ("Check", "Prüfen")
ES_STRINGS["Verifica"] = "Verificar"
FR_STRINGS["Verifica"] = "Vérifier"

UI_STRINGS["Non verificata"] = ("Not checked", "Nicht geprüft")
ES_STRINGS["Non verificata"] = "No verificada"
FR_STRINGS["Non verificata"] = "Non vérifiée"

UI_STRINGS["Verifica…"] = ("Checking…", "Prüfung…")
ES_STRINGS["Verifica…"] = "Verificando…"
FR_STRINGS["Verifica…"] = "Vérification…"

UI_STRINGS["Verifica in corso…"] = ("Check in progress…", "Prüfung läuft…")
ES_STRINGS["Verifica in corso…"] = "Verificación en curso…"
FR_STRINGS["Verifica in corso…"] = "Vérification en cours…"

UI_STRINGS["Funzionante"] = ("Working", "Funktionsfähig")
ES_STRINGS["Funzionante"] = "Funcionando"
FR_STRINGS["Funzionante"] = "Fonctionne"

UI_STRINGS["Non funzionante"] = ("Not working", "Nicht funktionsfähig")
ES_STRINGS["Non funzionante"] = "No funciona"
FR_STRINGS["Non funzionante"] = "Ne fonctionne pas"

UI_STRINGS["URL mancante."] = ("Missing URL.", "URL fehlt.")
ES_STRINGS["URL mancante."] = "Falta la URL."
FR_STRINGS["URL mancante."] = "URL manquante."

UI_STRINGS["La risposta non è JSON."] = (
    "The response is not JSON.", "Die Antwort ist kein JSON.")
ES_STRINGS["La risposta non è JSON."] = "La respuesta no es JSON."
FR_STRINGS["La risposta non è JSON."] = "La réponse n'est pas du JSON."

UI_STRINGS["JSON non valido."] = ("Invalid JSON.", "Ungültiges JSON.")
ES_STRINGS["JSON non valido."] = "JSON no válido."
FR_STRINGS["JSON non valido."] = "JSON invalide."

UI_STRINGS["Formato JSON non riconosciuto."] = (
    "Unrecognised JSON format.", "JSON-Format nicht erkannt.")
ES_STRINGS["Formato JSON non riconosciuto."] = (
    "Formato JSON no reconocido.")
FR_STRINGS["Formato JSON non riconosciuto."] = (
    "Format JSON non reconnu.")

UI_STRINGS["Nessun risultato nel formato atteso."] = (
    "No results in the expected format.",
    "Keine Ergebnisse im erwarteten Format.")
ES_STRINGS["Nessun risultato nel formato atteso."] = (
    "Ningún resultado en el formato esperado.")
FR_STRINGS["Nessun risultato nel formato atteso."] = (
    "Aucun résultat dans le format attendu.")

UI_STRINGS["Verifica non riuscita."] = (
    "Check failed.", "Prüfung fehlgeschlagen.")
ES_STRINGS["Verifica non riuscita."] = "Verificación fallida."
FR_STRINGS["Verifica non riuscita."] = "Vérification échouée."

UI_STRINGS["Risposta HTTP non valida."] = (
    "Invalid HTTP response.", "Ungültige HTTP-Antwort.")
ES_STRINGS["Risposta HTTP non valida."] = "Respuesta HTTP no válida."
FR_STRINGS["Risposta HTTP non valida."] = "Réponse HTTP invalide."

UI_STRINGS["Errore di rete."] = ("Network error.", "Netzwerkfehler.")
ES_STRINGS["Errore di rete."] = "Error de red."
FR_STRINGS["Errore di rete."] = "Erreur réseau."

UI_STRINGS["Errore di rete:"] = ("Network error:", "Netzwerkfehler:")
ES_STRINGS["Errore di rete:"] = "Error de red:"
FR_STRINGS["Errore di rete:"] = "Erreur réseau :"

UI_STRINGS["Seleziona un'API da verificare."] = (
    "Select an API to check.", "Wählen Sie eine zu prüfende API aus.")
ES_STRINGS["Seleziona un'API da verificare."] = (
    "Selecciona una API para verificar.")
FR_STRINGS["Seleziona un'API da verificare."] = (
    "Sélectionnez une API à vérifier.")

UI_STRINGS["Internet Archive è sempre attivo."] = (
    "Internet Archive is always active.",
    "Internet Archive ist immer aktiv.")
ES_STRINGS["Internet Archive è sempre attivo."] = (
    "Internet Archive siempre está activo.")
FR_STRINGS["Internet Archive è sempre attivo."] = (
    "Internet Archive est toujours actif.")

UI_STRINGS["Questo è un sito, non un endpoint API."] = (
    "This is a website, not an API endpoint.",
    "Dies ist eine Website, kein API-Endpunkt.")
ES_STRINGS["Questo è un sito, non un endpoint API."] = (
    "Este es un sitio web, no un endpoint de API.")
FR_STRINGS["Questo è un sito, non un endpoint API."] = (
    "Ceci est un site web, pas un endpoint d'API.")

UI_STRINGS["Segnaposto {0} non supportato: usa {query} o [QUERY]."] = (
    "Placeholder {0} is not supported: use {query} or [QUERY].",
    "Platzhalter {0} wird nicht unterstützt: verwenden Sie {query} oder [QUERY].")
ES_STRINGS["Segnaposto {0} non supportato: usa {query} o [QUERY]."] = (
    "Marcador {0} no compatible: usa {query} o [QUERY].")
FR_STRINGS["Segnaposto {0} non supportato: usa {query} o [QUERY]."] = (
    "Paramètre {0} non pris en charge : utilisez {query} ou [QUERY].")


def translate_ui(text, language):
    if language == "it": return text
    pair = UI_STRINGS.get(text)
    return (pair[0 if language == "en" else 1] if language in ("en", "de") else ES_STRINGS.get(text, text) if language == "es" else FR_STRINGS.get(text, text)) if pair else text

GITHUB_RELEASE_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s]+)/releases"
    r"(?:/tag/([^/?#\s]+)|/latest)?/?(?:[?#].*)?$", re.IGNORECASE
)


def parse_github_release_url(url):
    """Restituisce owner, repository, tag ed endpoint API per URL release GitHub."""
    match = GITHUB_RELEASE_RE.fullmatch((url or "").strip())
    if not match:
        return None
    owner, repo, tag = match.groups()
    repo = repo.removesuffix(".git")
    if not owner or not repo:
        return None
    if tag:
        endpoint = (f"https://api.github.com/repos/{owner}/{repo}/releases/tags/"
                    f"{urllib.parse.quote(urllib.parse.unquote(tag), safe='')}")
    else:
        endpoint = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    return {"owner": owner, "repo": repo, "tag": tag, "endpoint": endpoint}


def parse_github_release_assets(release, owner, repo):
    """Normalizza gli asset API e aggiunge gli archivi sorgente disponibili."""
    if not isinstance(release, dict):
        raise ValueError("Risposta GitHub non valida.")
    rows = []
    assets = release.get("assets", [])
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict) or not asset.get("browser_download_url"):
                continue
            asset_name = sanitize_filename(str(asset.get("name") or "download"), "download")
            rows.append({
                "name": asset_name,
                "url": asset["browser_download_url"],
                "size": asset.get("size"),
                "download_count": asset.get("download_count", 0),
                "kind": "asset",
            })
    tag = sanitize_filename(str(release.get("tag_name") or "latest"), "latest")
    safe_repo = sanitize_filename(os.path.basename(repo.rstrip("/")) or "source", "source")
    for ext, key in (("zip", "zipball_url"), ("tar.gz", "tarball_url")):
        url = release.get(key)
        if url:
            rows.append({
                "name": f"{safe_repo}-{tag}.{ext}", "url": url,
                "size": None, "download_count": None, "kind": "source",
            })
    return rows


def parse_magnet_uri(url):
    """Valida e normalizza un magnet BitTorrent con un info-hash btih."""
    value = (url or "").strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme.lower() != "magnet":
            return None
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        xt_values = query.get("xt", [])
        for xt in xt_values:
            if not xt.lower().startswith("urn:btih:"):
                continue
            info_hash = xt[9:]
            if re.fullmatch(r"[0-9a-fA-F]{40}", info_hash) or re.fullmatch(r"[A-Z2-7a-z2-7]{32}", info_hash):
                return {"url": value, "info_hash": info_hash, "name": (query.get("dn") or [""])[0]}
    except (ValueError, AttributeError):
        pass
    return None


def progress_bar_text(percent, segments=PROGRESS_SEGMENTS):
    """Rappresentazione testuale del progresso, sicura anche senza Tkinter."""
    segments = max(1, int(segments))
    percent = max(0, min(100, int(percent)))
    filled = min(segments, (percent * segments) // 100)
    return "█" * filled + "░" * (segments - filled)


_SEASON_PATTERNS = (
    re.compile(r"\bS(\d{1,2})[ ._-]?E\d{1,3}\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})x(\d{2,3})\b", re.IGNORECASE),
    re.compile(r"\bS(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\b(?:Season|Stagione|Staffel|Temporada|Saison)\s*(\d{1,2})\b",
               re.IGNORECASE),
)


def torrent_season_numbers(text):
    """Stagioni riconosciute nei nomi dei file o nel titolo del torrent."""
    seasons = set()
    for pattern in _SEASON_PATTERNS:
        for match in pattern.finditer(text or ""):
            for group in match.groups():
                if group:
                    try:
                        seasons.add(int(group))
                    except (TypeError, ValueError):
                        pass
                    break
    return seasons


def aggregate_torrent_progress(tasks):
    """Progresso ponderato in byte dei file appartenenti a un torrent."""
    known = [task for task in tasks if task.total is not None and task.total >= 0]
    total = sum(int(task.total) for task in known)
    downloaded = sum(min(int(task.total), max(0, int(task.downloaded))) for task in known)
    if total > 0:
        percent = min(100, int(downloaded * 100 / total))
    else:
        percent = 100 if known and all(task.state == "Completato" for task in known) else 0
    return downloaded, total, percent


def human_size(value):
    if value is None or value < 0:
        return "—"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


UPDATE_REPO = "slenter97/Downloadverse"
UPDATE_RELEASES_URL = f"https://github.com/{UPDATE_REPO}/releases"
PROJECT_GITHUB_URL = f"https://github.com/{UPDATE_REPO}"
UPDATE_RELEASES_API = f"https://api.github.com/repos/{UPDATE_REPO}/releases"
_UPDATE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
}


class UpdateError(Exception):
    """Errore leggibile durante il controllo o il download dell'aggiornamento."""


def _version_tuple(value):
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:4]]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def _github_update_headers(accept="application/vnd.github+json"):
    headers = dict(_UPDATE_HEADERS)
    headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _find_latest_update():
    """Cerca nelle release GitHub l'asset .msi con la versione più alta."""
    try:
        response = requests.get(UPDATE_RELEASES_API,
                                headers=_github_update_headers(),
                                params={"per_page": 100}, timeout=30)
        response.raise_for_status()
        releases = response.json()
    except Exception as exc:
        raise UpdateError("Release GitHub non raggiungibili.") from exc
    if not isinstance(releases, list):
        raise UpdateError("Risposta GitHub non valida.")
    current = _version_tuple(APP_VERSION)
    latest = None
    for release in releases:
        if not isinstance(release, dict):
            continue
        tag = str(release.get("tag_name") or release.get("name") or "")
        for asset in release.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            if not name.lower().endswith(".msi"):
                continue
            match = (re.search(r"(\d+(?:\.\d+){1,3})", name)
                     or re.search(r"(\d+(?:\.\d+){1,3})", tag))
            if not match:
                continue
            version = match.group(1)
            parsed = _version_tuple(version)
            if parsed <= current:
                continue
            if latest is None or parsed > latest["version_tuple"]:
                latest = {
                    "id": asset.get("id"),
                    "name": name,
                    "version": version,
                    "version_tuple": parsed,
                    "url": asset.get("browser_download_url") or "",
                    "size": asset.get("size"),
                }
    return latest


def _download_update_msi(url, filename, asset_id=None):
    """Scarica l'asset .msi della release GitHub e ne verifica la firma."""
    target_dir = tempfile.mkdtemp(prefix="downloadverse_update_")
    safe_name = sanitize_filename(filename or "DOWNLOADVERSE-update.msi",
                                  "DOWNLOADVERSE-update.msi")
    if not safe_name.lower().endswith(".msi"):
        safe_name += ".msi"
    target = os.path.join(target_dir, safe_name)
    candidates = []
    if url:
        candidates.append(url)
    if asset_id is not None:
        candidates.append(
            f"https://api.github.com/repos/{UPDATE_REPO}/releases/assets/{asset_id}")
    last_error = None
    for candidate in candidates:
        try:
            response = requests.get(candidate,
                                    headers=_github_update_headers(
                                        "application/octet-stream"),
                                    timeout=60, stream=True, allow_redirects=True)
            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if "text/html" in content_type or "application/json" in content_type:
                response.close()
                raise UpdateError("Il link GitHub non punta a un file MSI.")
            with open(target, "wb") as handle:
                for chunk in response.iter_content(chunk_size=262144):
                    if chunk:
                        handle.write(chunk)
            response.close()
            if os.path.getsize(target) < 8:
                raise UpdateError("Il file scaricato è vuoto.")
            with open(target, "rb") as handle:
                magic = handle.read(8)
            if magic != b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1":
                raise UpdateError("Il file scaricato non è un MSI valido.")
            return target
        except Exception as exc:
            last_error = exc
            try:
                os.remove(target)
            except OSError:
                pass
    raise UpdateError("Errore download aggiornamento.") from last_error

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name, default="download"):
    """Rende un nome sicuro per file system Windows e POSIX."""
    value = (name or "").replace("\\", "/")
    value = os.path.basename(value)
    value = _INVALID_FILENAME_CHARS.sub("_", value).strip(" .")
    if not value or value in (".", ".."):
        return default
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        value = "_" + value
    return value or default


def filename_from_headers(url, headers):
    """Ricava un nome sicuro da Content-Disposition o dall'URL."""
    name = None
    disposition = headers.get("Content-Disposition", "")
    try:
        msg = email.message.Message()
        msg["content-disposition"] = disposition
        name = msg.get_filename()
    except (TypeError, ValueError):
        pass
    if name:
        name = urllib.parse.unquote(name)
    if not name:
        path = urllib.parse.urlsplit(url).path
        name = urllib.parse.unquote(os.path.basename(path.rstrip("/")))
    # Non permettere percorsi o caratteri non validi forniti dal server/dall'URL.
    return sanitize_filename(name, "download")


PROTECTED_HOSTS = ("datanodes.to", "fuckingfast.co")
CLOUDFLARE_HELP = (
    "Bloccato da Cloudflare. Apri il link nel browser e completa il controllo; "
    "Verifica che il link sia accessibile dal browser e riprova più tardi."
)


def load_browser_auth(path=None):
    """Legge in modo tollerante i cookie salvati localmente."""
    try:
        with open(path or COOKIE_CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_browser_auth(host, cookie, user_agent="", path=None):
    """Salva Cookie/UA per un host protetto (file locale, permessi privati)."""
    host = (host or "").strip().lower().split(":", 1)[0].strip().strip(".")
    if (not host or len(host) > 253
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
                                r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", host)):
        raise ValueError(
            "Inserisci un dominio valido, ad esempio kenotrontv.ru.")
    cookie = (cookie or "").strip()
    if not cookie:
        raise ValueError("La stringa Cookie non può essere vuota.")
    target = path or COOKIE_CONFIG_PATH
    data = load_browser_auth(target)
    data[host] = {"cookie": cookie, "user_agent": (user_agent or "").strip()}
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def _auth_for_host(host, config):
    host = (host or "").lower().rstrip(".")
    for domain, auth in config.items():
        domain = str(domain).lower().lstrip(".").rstrip(".")
        if (host == domain or host.endswith("." + domain)) and isinstance(auth, dict):
            return auth
    return {}


def _put_cookie_header(session, host, cookie_header):
    """Importa Cookie in un cookie jar con scope host per evitare fughe a terzi."""
    parsed = SimpleCookie()
    try:
        parsed.load(cookie_header or "")
    except Exception:
        parsed = SimpleCookie()
    pairs = [(name, morsel.value) for name, morsel in parsed.items()]
    if not pairs:
        for part in (cookie_header or "").split(";"):
            if "=" in part:
                name, value = part.strip().split("=", 1)
                if name.strip():
                    pairs.append((name.strip(), value.strip()))
    for name, value in pairs:
        try:
            session.cookies.set(name, value, domain=host, path="/")
        except Exception:
            # Non usare un header Cookie globale: potrebbe propagarsi a host terzi
            # durante i redirect. In caso di cookie jar incompatibile si continua senza.
            continue


def make_http_session(url, config_path=None, backend=None):
    """Crea un client HTTP: curl_cffi/Chrome, cloudscraper o requests standard."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    choices = [backend] if backend else ["curl", "cloudscraper", "requests"]
    session = None
    for choice in choices:
        try:
            if choice == "curl":
                curl = _load_curl_cffi()
                if curl is None:
                    continue
                session = curl.Session(impersonate="chrome")
            elif choice == "cloudscraper":
                scraper = _load_cloudscraper()
                if scraper is None:
                    continue
                session = scraper.create_scraper()
            elif choice == "requests":
                session = requests.Session()
            else:
                continue
            break
        except Exception:
            session = None
    if session is None:
        session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA,
                            "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8"})
    auth = _auth_for_host(host, load_browser_auth(config_path))
    if auth.get("user_agent"):
        session.headers["User-Agent"] = str(auth["user_agent"])
    if auth.get("cookie"):
        _put_cookie_header(session, host, str(auth["cookie"]))
    elif any(host == d or host.endswith("." + d) for d in PROTECTED_HOSTS):
        # Best effort soltanto: profili/browser non disponibili non bloccano il download.
        cookies_module = _load_browser_cookie3()
        if cookies_module is not None:
            try:
                jar = cookies_module.chrome(domain_name=host)
                session.cookies.update(jar)
            except Exception:
                pass
    return session


def _request_with_fallback(session, method, url, config_path=None, **kwargs):
    """Prova il client attivo e passa agli altri trasporti in caso di challenge/errore."""
    first_error = None
    response = None
    probe_body = not bool(kwargs.get("stream", False))
    try:
        response = getattr(session, method.lower())(url, **kwargs)
        if not _looks_like_cloudflare(response, probe_body=probe_body):
            return response, session
    except Exception as exc:
        first_error = exc
    used = {type(session).__module__.split(".", 1)[0]}
    for backend in ("cloudscraper", "requests"):
        if backend == "cloudscraper" and _load_cloudscraper() is None:
            continue
        if backend == "requests" and "requests" in used:
            continue
        alternate = make_http_session(url, config_path, backend=backend)
        try:
            alternate.headers.update(session.headers)
            alternate.cookies.update(session.cookies)
        except Exception:
            pass
        try:
            candidate = getattr(alternate, method.lower())(url, **kwargs)
            if not _looks_like_cloudflare(candidate, probe_body=probe_body):
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                try:
                    session.close()
                except Exception:
                    pass
                return candidate, alternate
            candidate.close()
        except Exception as exc:
            first_error = first_error or exc
    if response is not None:
        # Nessun trasporto alternativo disponibile: il chiamante gestisce la
        # risposta (challenge Cloudflare o contenuto) senza riceverla chiusa.
        return response, session
    if first_error is not None:
        raise first_error
    raise LinkResolutionError(CLOUDFLARE_HELP)


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
DIRECT_LINK_RE = re.compile(r'https?://[^\s"\'<>\\]+', re.IGNORECASE)


class LinkResolutionError(Exception):
    """Errore leggibile relativo alla risoluzione di link-hosting."""


class _InputParser(HTMLParser):
    """Legge input e metadati del form necessari nei passaggi di hosting."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inputs = {}
        self.forms = []
        self._form = None

    def handle_starttag(self, tag, attrs):
        item = dict(attrs)
        if tag.lower() == "form":
            self._form = {"action": item.get("action", ""),
                          "method": item.get("method", "get").lower(), "inputs": {}}
            self.forms.append(self._form)
        elif tag.lower() == "input":
            name = item.get("name")
            if name:
                value = item.get("value", "")
                self.inputs[name] = value
                if self._form is not None:
                    self._form["inputs"][name] = value

    def handle_endtag(self, tag):
        if tag.lower() == "form":
            self._form = None


_DOWNLOAD_EXTENSIONS = frozenset({
    "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "exe", "msi",
    "dmg", "pkg", "deb", "rpm", "apk", "bin", "img", "pdf", "mp4",
    "mkv", "avi", "mp3", "flac", "txt", "csv", "torrent", "tgz", "zst",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
    "json", "xml", "7zip", "webm", "mov", "wav", "ogg", "m4a", "epub",
})


class _DownloadAnchorParser(HTMLParser):
    """Estrae esclusivamente link <a>, associando il testo visibile all'href."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self._anchor = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self._anchor = [href, []]

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._anchor is not None:
            self.anchors.append((self._anchor[0], " ".join(self._anchor[1])))
            self._anchor = None

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.anchors.append((href, ""))


def extract_download_links(page, base_url):
    """Restituisce href di anchor che paiono file scaricabili, in URL assoluti deduplicati."""
    parser = _DownloadAnchorParser()
    try:
        parser.feed(page or "")
    except Exception:
        pass
    found, seen = [], set()
    for href, label in parser.anchors:
        href = html_module.unescape((href or "").strip())
        if not href or href.lower().startswith(("javascript:", "data:", "mailto:", "#")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            continue
        path = urllib.parse.unquote(parts.path).lower()
        ext = os.path.splitext(path.rstrip("/"))[1].lstrip(".")
        if ext not in _DOWNLOAD_EXTENSIONS and "download" not in path and "download" not in label.lower():
            continue
        absolute = urllib.parse.urlunsplit(parts)
        key = urllib.parse.urldefrag(absolute)[0]
        if key not in seen:
            seen.add(key)
            found.append(absolute)
    return found


def _is_direct_file_url(url):
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower()
    return os.path.splitext(path.rstrip("/"))[1].lstrip(".") in _DOWNLOAD_EXTENSIONS


def _filename_from_url(url):
    path = urllib.parse.urlsplit(url).path
    return filename_from_headers(url, {}) if os.path.basename(path.rstrip("/")) else "download"


def _parse_page_forms(page):
    parser = _InputParser()
    try:
        parser.feed(page or "")
    except Exception:
        pass
    return parser


def _looks_like_cloudflare(response, probe_body=True):
    """Rileva una challenge Cloudflare senza consumare download in streaming."""
    headers = getattr(response, "headers", {})
    server = str(headers.get("Server", "")).lower()
    status = getattr(response, "status_code", 0)
    mitigated = str(headers.get("cf-mitigated", "")).lower()
    if "challenge" in mitigated:
        return True
    if status in (403, 503) and "cloudflare" in server:
        return True
    if not probe_body:
        return False
    body = (getattr(response, "text", "") or "")[:200000].lower()
    return ("cf-chl-" in body or "just a moment..." in body or
            "checking your browser" in body or "cloudflare ray id" in body)


def _normalize_page_text(text):
    text = html_module.unescape(text or "")
    text = re.sub(r"\\u002[fF]", "/", text)
    text = re.sub(r"\\u003[aA]", ":", text)
    return re.sub(r"\\+/", "/", text)


_FILE_URL_RE = re.compile(r"\.(?:7z|avi|bz2|csv|exe|gz|iso|mkv|mp3|mp4|pdf|rar|tar|torrent|txt|zip)(?:$|[?#])", re.I)


def _extract_page_urls(page, base_url, source_url=None):
    """Estrae URL da HTML/JS includendo URL relativi ed escaped."""
    text = _normalize_page_text(page)
    candidates = []
    patterns = [
        r"(?is)(?:href|src|action)\s*=\s*[\"']([^\"']+)",
        r"(?is)(?:window\s*\.\s*open|open)\s*\(\s*[\"']([^\"']+)",
        r"(?is)(?:url\s*[:=]|location\s*(?:\.\s*href)?\s*=)\s*[\"']([^\"']+)",
        r"(?is)[\"']((?:https?:)?//[^\"'\s<>]+|/[^\"'\s<>]+)[\"']",
        r"(?i)https?://[^\s\"'<>]+",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidates.append(match.group(1) if match.lastindex else match.group(0))
    import base64
    for encoded in re.findall(r"(?i)(?:atob\s*\(\s*[\"']|base64[,\s]+)([A-Za-z0-9+/=_-]{24,})", text):
        try:
            raw = encoded.replace('-', '+').replace('_', '/')
            decoded = base64.b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8', 'ignore')
            candidates.extend(re.findall(r"(?:https?://[^\s\"'<>]+|/[^\s\"'<>]+)", decoded))
        except Exception:
            pass
    normalized, seen = [], set()
    source_url = source_url or base_url
    for candidate in candidates:
        candidate = html_module.unescape(candidate.strip()).strip("\\\"' ").rstrip("),;]}")
        if not candidate or candidate.lower().startswith(("javascript:", "data:")):
            continue
        if candidate.startswith('//'):
            candidate = urllib.parse.urlsplit(base_url).scheme + ':' + candidate
        candidate = urllib.parse.urljoin(base_url, candidate)
        parts = urllib.parse.urlsplit(candidate)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            continue
        candidate = urllib.parse.urlunsplit(parts)
        if candidate.rstrip('/') == source_url.rstrip('/') or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _response_direct_url(response, base_url):
    location = response.headers.get('Location')
    if location:
        return urllib.parse.urljoin(response.url or base_url, location)
    try:
        data = response.json()
        if isinstance(data, dict):
            for key in ('url', 'download', 'link', 'href'):
                if data.get(key):
                    return urllib.parse.urljoin(response.url or base_url, str(data[key]))
    except (ValueError, TypeError, requests.exceptions.JSONDecodeError):
        pass
    candidates = _extract_page_urls(getattr(response, 'text', '') or '', response.url or base_url)
    for candidate in candidates:
        if '/dl/' in urllib.parse.urlsplit(candidate).path.lower():
            return candidate
    for candidate in candidates:
        if _FILE_URL_RE.search(urllib.parse.urlsplit(candidate).path):
            return candidate
    base_host = (urllib.parse.urlsplit(base_url).hostname or '').lower()
    for candidate in candidates:
        if (urllib.parse.urlsplit(candidate).hostname or '').lower() != base_host:
            return candidate
    return None


def _save_resolution_debug(host, page, trace, reason):
    safe_host = re.sub(r'[^a-zA-Z0-9.-]', '_', host or 'host')
    filename = f'debug_page_{safe_host}.html'
    debug_dir = os.path.join(DATA_DIR, "debug")
    path = os.path.join(debug_dir, filename)
    saved_path = path
    try:
        os.makedirs(debug_dir, exist_ok=True)
        with open(path, 'w', encoding='utf-8', errors='replace') as handle:
            handle.write(page or '')
    except OSError:
        logging.exception('Impossibile salvare pagina debug %s', path)
        saved_path = path + ' (errore di scrittura)'
    logging.error('Estrazione link fallita: %s; passaggi=%s; primi 500 caratteri pagina=%r',
                  reason, json.dumps(trace, ensure_ascii=False), (page or '')[:500])
    return saved_path


def _debug_link_error(host, page, trace, reason):
    filename = _save_resolution_debug(host, page, trace, reason)
    return LinkResolutionError(f'{reason}. Pagina salvata in {filename}.')


def _page_countdown(page):
    text = _normalize_page_text(page)
    for pattern in (r'(?:wait|waiting|countdown|timer)[^0-9]{0,30}(\d{1,3})\s*(?:seconds?|secs?|s)?',
                    r'(\d{1,3})\s*seconds?\s*(?:remaining|left)?'):
        match = re.search(pattern, text, re.I)
        if match:
            return min(60, int(match.group(1)))
    return 0


def _response_is_file(response):
    ctype = str(response.headers.get('Content-Type', '')).lower()
    disposition = str(response.headers.get('Content-Disposition', '')).lower()
    return 'attachment' in disposition or (ctype and not any(x in ctype for x in ('html', 'json', 'text/')))


def _fuckingfast_htmx_direct(page, page_url, session, timeout, trace):
    """Esegue il POST HTMX del pulsante DOWNLOAD e legge il link dall'header."""
    endpoint = None
    match = re.search(r'(?is)hx-post\s*=\s*["\']([^"\']+)["\']', page or '')
    if match:
        endpoint = html_module.unescape(match.group(1)).strip()
    if not endpoint:
        # Fallback: il pulsante usa sempre /f/<id>/go.
        parts = [urllib.parse.unquote(part) for part in
                 urllib.parse.urlsplit(page_url).path.split('/') if part]
        if parts:
            endpoint = f'/f/{parts[0]}/go'
    if not endpoint:
        return None, session
    endpoint = urllib.parse.urljoin(page_url, endpoint)
    parsed = urllib.parse.urlsplit(page_url)
    headers = {
        'HX-Request': 'true',
        'HX-Current-URL': page_url,
        'Referer': page_url,
        'Accept': '*/*',
    }
    if parsed.scheme and parsed.netloc:
        headers['Origin'] = f'{parsed.scheme}://{parsed.netloc}'
    posted, session = _request_with_fallback(
        session, 'POST', endpoint, headers=headers, timeout=timeout,
        allow_redirects=False)
    trace.append({'method': 'POST', 'url': endpoint,
                  'status': getattr(posted, 'status_code', None),
                  'response_url': getattr(posted, 'url', None), 'step': 'htmx'})
    if _looks_like_cloudflare(posted):
        raise LinkResolutionError(CLOUDFLARE_HELP)
    for header in ('HX-Redirect', 'HX-Location', 'Location'):
        target = posted.headers.get(header)
        if not target:
            continue
        target = html_module.unescape(str(target)).strip()
        if target.startswith('{'):
            try:
                data = json.loads(target)
                target = str(data.get('path') or data.get('url') or '')
            except (ValueError, TypeError):
                target = ''
        if target:
            return urllib.parse.urljoin(posted.url or endpoint, target), session
    if posted.status_code >= 400:
        posted.raise_for_status()
    return _response_direct_url(posted, endpoint), session


def resolve_host_url(url, session=None, timeout=25):
    """Risolvi datanodes.to e fuckingfast.co; ritorna URL, nome eventuale e sessione."""
    session = session or make_http_session(url)
    host = (urllib.parse.urlsplit(url).hostname or '').lower()
    trace = []
    if host == 'fuckingfast.co' or host.endswith('.fuckingfast.co'):
        response, session = _request_with_fallback(session, 'GET', url, headers={'Referer': url}, timeout=timeout)
        trace.append({'method': 'GET', 'url': url, 'status': getattr(response, 'status_code', None),
                      'response_url': getattr(response, 'url', None)})
        if _looks_like_cloudflare(response):
            raise LinkResolutionError(CLOUDFLARE_HELP)
        response.raise_for_status()
        page = response.text or ''
        candidates = _extract_page_urls(page, response.url or url, source_url=url)
        direct = next((x for x in candidates if '/dl/' in urllib.parse.urlsplit(x).path.lower()), None)
        direct = direct or next((x for x in candidates if _FILE_URL_RE.search(urllib.parse.urlsplit(x).path)), None)
        if not direct:
            # La pagina usa un pulsante HTMX: il link non è nel HTML ma arriva
            # dall'header HX-Redirect della POST /f/<id>/go.
            direct, session = _fuckingfast_htmx_direct(
                page, response.url or url, session, timeout, trace)
        if not direct:
            raise _debug_link_error(host, page, trace, 'Link diretto non trovato nella pagina fuckingfast.co')
        title_match = re.search(r'<title[^>]*>(.*?)</title>', page, re.I | re.S)
        title = html_module.unescape(re.sub(r'<[^>]+>', '', title_match.group(1))).strip() if title_match else ''
        fragment = urllib.parse.unquote(urllib.parse.urlsplit(url).fragment).strip()
        name = fragment or title or None
        if name:
            name = sanitize_filename(name, '') or None
        return direct, name, session

    if host == 'datanodes.to' or host.endswith('.datanodes.to'):
        response, session = _request_with_fallback(session, 'GET', url, headers={'Referer': url}, timeout=timeout)
        trace.append({'method': 'GET', 'url': url, 'status': getattr(response, 'status_code', None),
                      'response_url': getattr(response, 'url', None)})
        if _looks_like_cloudflare(response):
            raise LinkResolutionError(CLOUDFLARE_HELP)
        response.raise_for_status()
        page = response.text or ''
        parts = [urllib.parse.unquote(x) for x in urllib.parse.urlsplit(url).path.split('/') if x]
        if not parts:
            raise _debug_link_error(host, page, trace, 'URL datanodes.to privo di ID')
        file_id = parts[0]
        current, current_page = response, page
        endpoint, last_page = urllib.parse.urljoin(response.url or url, '/download'), page
        for step in range(3):
            page_urls = _extract_page_urls(current_page, current.url or url)
            page_direct = next((x for x in page_urls if '/dl/' in urllib.parse.urlsplit(x).path.lower()), None)
            page_direct = page_direct or next((x for x in page_urls if _FILE_URL_RE.search(urllib.parse.urlsplit(x).path)), None)
            if page_direct:
                return page_direct, None, session
            parsed = _parse_page_forms(current_page)
            form = dict(parsed.inputs)
            if not form:
                # Redirect HTTP nella risposta di un passaggio successivo.
                location = current.headers.get('Location') if step else None
                if location:
                    next_url = urllib.parse.urljoin(current.url or endpoint, location)
                    followed, session = _request_with_fallback(session, 'GET', next_url,
                        headers={'Referer': current.url or url}, timeout=timeout, allow_redirects=False)
                    trace.append({'method': 'GET', 'url': next_url, 'status': getattr(followed, 'status_code', None),
                                  'response_url': getattr(followed, 'url', None), 'step': step + 1})
                    if _looks_like_cloudflare(followed):
                        raise LinkResolutionError(CLOUDFLARE_HELP)
                    if _response_is_file(followed):
                        return followed.url or next_url, None, session
                    current, current_page = followed, followed.text or ''
                    continue
                raise _debug_link_error(host, current_page, trace, 'Modulo o link diretto non trovato nella pagina datanodes.to')
            if step == 0:
                form.update({'op': 'download2', 'id': file_id, 'rand': form.get('rand', ''),
                             'referer': form.get('referer', ''), 'method_free': 'Free Download >>',
                             'method_premium': '', 'dl': '1'})
            active = parsed.forms[0] if parsed.forms else {}
            if active.get('action'):
                endpoint = urllib.parse.urljoin(current.url or url, active['action'])
            elif step:
                endpoint = urllib.parse.urljoin(current.url or url, '/download')
            delay = _page_countdown(current_page)
            if delay:
                time.sleep(delay)
            posted, session = _request_with_fallback(session, 'POST', endpoint, data=form,
                headers={'Referer': current.url or url}, timeout=timeout, allow_redirects=False)
            trace.append({'method': 'POST', 'url': endpoint, 'status': getattr(posted, 'status_code', None),
                          'response_url': getattr(posted, 'url', None), 'step': step + 1})
            if _looks_like_cloudflare(posted):
                raise LinkResolutionError(CLOUDFLARE_HELP)
            location = posted.headers.get('Location')
            if location:
                target = urllib.parse.urljoin(posted.url or endpoint, location)
                target_host = (urllib.parse.urlsplit(target).hostname or '').lower()
                original_host = (urllib.parse.urlsplit(url).hostname or '').lower()
                if target_host != original_host or '/dl/' in urllib.parse.urlsplit(target).path.lower():
                    return target, None, session
                followed, session = _request_with_fallback(session, 'GET', target,
                    headers={'Referer': posted.url or endpoint}, timeout=timeout, allow_redirects=False)
                trace.append({'method': 'GET', 'url': target, 'status': getattr(followed, 'status_code', None),
                              'response_url': getattr(followed, 'url', None), 'step': step + 1})
                if _looks_like_cloudflare(followed):
                    raise LinkResolutionError(CLOUDFLARE_HELP)
                if _response_is_file(followed):
                    return followed.url or target, None, session
                current, current_page = followed, followed.text or ''
                continue
            direct = _response_direct_url(posted, endpoint)
            if direct:
                return direct, None, session
            if _response_is_file(posted):
                return posted.url or endpoint, None, session
            if posted.status_code >= 400:
                posted.raise_for_status()
            current, current_page, last_page = posted, posted.text or '', posted.text or ''
        raise _debug_link_error(host, last_page or current_page, trace,
                                'Il server datanodes.to non ha restituito un link diretto entro 3 passaggi')
    return url, None, session


_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: index for index, char in enumerate(_BASE58_ALPHABET)}


class PrivateBinError(Exception):
    """Errore leggibile durante la lettura di un paste PrivateBin."""


# Payload PrivateBin minimale usato da --selftest per verificare AES-GCM,
# PBKDF2 e decompressione anche nell'eseguibile compilato.
_SELFTEST_PRIVATEBIN_KEY = "9gqdEiobx9BSwxzqoUQz54La6cv2FJS5QZStSV5kZ8PJ"
_SELFTEST_PRIVATEBIN_PAYLOAD = (
    '{"status":0,"adata":[["MDEyMzQ1Njc4OWFiY2RlZg==","c2FsdDEyMzQ=",100000,256,128,'
    '"aes","gcm","zlib"],"plaintext",0,0],"ct":"ogz9FlV7wFN0Nx61G5OkT1+k9443Vdc4NoLJNKq0xq'
    'C1u1XKPt01RF1oiKNftJeY0qIWRNUHECcxgHVXCKE=","v":2}'
)


def _base58_decode(value):
    """Decodifica una stringa base58 (formato chiavi PrivateBin)."""
    number = 0
    for char in value or "":
        index = _BASE58_INDEX.get(char)
        if index is None:
            raise PrivateBinError("La chiave del paste contiene caratteri non validi.")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading + body


MEGA_HOSTS = ("mega.nz", "mega.co.nz")
MEGA_API_URL = "https://g.api.mega.co.nz/cs?id=0&ak=0"
_MEGA_API_ERRORS = {
    -1: "Errore interno di MEGA.",
    -2: "Richiesta MEGA non valida.",
    -3: "Quota di trasferimento MEGA superata. Riprova più tardi.",
    -4: "Quota di archiviazione MEGA superata.",
    -6: "Troppe richieste a MEGA. Riprova tra qualche minuto.",
    -9: "File MEGA non trovato o link non valido.",
    -11: "La chiave del link MEGA non è valida.",
    -16: "Account MEGA bloccato.",
    -17: "Quota di trasferimento MEGA esaurita.",
}


def is_mega_host(url):
    try:
        host = (urllib.parse.urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return any(host == mega or host.endswith("." + mega) for mega in MEGA_HOSTS)


def parse_mega_file_url(url):
    """Estrae handle e chiave da un link MEGA di un singolo file."""
    try:
        split = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return None
    if split.scheme not in ("http", "https"):
        return None
    host = (split.hostname or "").lower()
    if not any(host == mega or host.endswith("." + mega) for mega in MEGA_HOSTS):
        return None
    path = (split.path or "").lower()
    if "/folder/" in path:
        return None
    fragment = urllib.parse.unquote(split.fragment or "").strip()
    parts = [part for part in split.path.split("/") if part]
    handle = ""
    key_text = ""
    if len(parts) >= 2 and parts[0].lower() in ("file", "embed"):
        handle = parts[1]
        key_text = fragment
    elif fragment.startswith("!"):
        bits = fragment[1:].split("!")
        handle = bits[0]
        key_text = bits[1] if len(bits) > 1 else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", handle or ""):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{22,}", key_text or ""):
        return None
    try:
        key = base64.urlsafe_b64decode(key_text + "=" * (-len(key_text) % 4))
    except (ValueError, TypeError):
        return None
    if len(key) < 32:
        return None
    return {"handle": handle, "key": key[:32]}


def _mega_api_request(payload, timeout=30):
    session = make_http_session(MEGA_API_URL)
    try:
        response = session.post(MEGA_API_URL, json=payload, timeout=timeout,
                                headers={"Content-Type": "application/json"})
        response.raise_for_status()
        return response.json()
    finally:
        try:
            session.close()
        except Exception:
            pass


def resolve_mega_file(url, timeout=30):
    """Metadati e URL del blob cifrato di un file MEGA pubblico."""
    parsed = parse_mega_file_url(url)
    if not parsed:
        raise LinkResolutionError(
            "Link MEGA non valido o cartella MEGA non supportata.")
    if _load_crypto() is None:
        raise LinkResolutionError(
            "pycryptodome non è installato: impossibile scaricare da MEGA.")
    data = _mega_api_request(
        [{"a": "g", "g": 1, "p": parsed["handle"], "ssl": 2}], timeout)
    if not isinstance(data, list) or not data:
        raise LinkResolutionError("Risposta MEGA non valida.")
    entry = data[0]
    if not isinstance(entry, dict):
        code = entry if isinstance(entry, int) else -1
        raise LinkResolutionError(
            _MEGA_API_ERRORS.get(code, f"Errore MEGA ({code})."))
    if not entry.get("g"):
        raise LinkResolutionError("MEGA non ha restituito un link di download.")
    key = parsed["key"]
    name = ""
    attributes = entry.get("at") or ""
    if attributes:
        try:
            attr_data = base64.urlsafe_b64decode(
                attributes + "=" * (-len(attributes) % 4))
            attr_key = bytes(a ^ b for a, b in zip(key[:16], key[16:32]))
            decrypted = _CryptoAES.new(
                attr_key, _CryptoAES.MODE_CBC, iv=b"\x00" * 16).decrypt(attr_data)
            decrypted = decrypted.rstrip(b"\x00")
            if decrypted.startswith(b"MEGA"):
                name = json.loads(
                    decrypted[4:].decode("utf-8", "replace")).get("n", "") or ""
        except Exception:
            name = ""
    return {
        "url": entry["g"],
        "size": int(entry.get("s") or 0),
        "name": name,
        "key": key,
    }


def decrypt_mega_file(path, key, chunk_size=4 * 1024 * 1024):
    """Decritta in place un blob MEGA (AES-128-CTR, chiave derivata dal link)."""
    if _load_crypto() is None:
        raise LinkResolutionError(
            "pycryptodome non è installato: impossibile decrittare il file MEGA.")
    content_key = bytes(a ^ b for a, b in zip(key[:16], key[16:32]))
    nonce = key[16:24]
    cipher = _CryptoAES.new(content_key, _CryptoAES.MODE_CTR, nonce=nonce)
    offset = 0
    with open(path, "r+b") as handle:
        while True:
            handle.seek(offset)
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            handle.seek(offset)
            handle.write(cipher.decrypt(chunk))
            offset += len(chunk)
    return offset


def looks_like_privatebin_url(url):
    """Riconosce un link PrivateBin senza tentare di scaricarlo."""
    try:
        split = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return False
    if split.scheme not in ("http", "https") or not split.netloc:
        return False
    host = (split.hostname or "").lower()
    if any(host == mega or host.endswith("." + mega) for mega in MEGA_HOSTS):
        return False
    if any(host == protected or host.endswith("." + protected)
           for protected in PROTECTED_HOSTS):
        return False
    fragment_name = urllib.parse.unquote(split.fragment).strip()
    extension = os.path.splitext(fragment_name)[1].lstrip(".").lower()
    if extension in _DOWNLOAD_EXTENSIONS:
        # Il frammento è il nome del file (es. fuckingfast.co/...#film.part01.rar).
        return False
    fragment = urllib.parse.unquote(split.fragment)
    return bool(fragment) and ("paste" in host or "pasteid" in split.query.lower()
                               or len(fragment) >= 30)


def _privatebin_api_url(url):
    """Restituisce URL API PrivateBin e chiave (frammento) del paste."""
    split = urllib.parse.urlsplit((url or "").strip())
    if split.scheme not in ("http", "https") or not split.netloc:
        raise PrivateBinError("Inserisci un link PrivateBin HTTP o HTTPS valido.")
    paste_id = ""
    for part in split.query.split("&"):
        if not part:
            continue
        key, _sep, value = part.partition("=")
        if key.lower() == "pasteid" and value:
            paste_id = urllib.parse.unquote(value)
            break
        if not value and not paste_id:
            paste_id = urllib.parse.unquote(key)
    if not paste_id:
        path_parts = [part for part in split.path.split("/") if part]
        if path_parts:
            paste_id = urllib.parse.unquote(path_parts[-1])
    if not paste_id:
        raise PrivateBinError("Il link non contiene un ID paste.")
    api_url = f"{split.scheme}://{split.netloc}{split.path or '/'}?pasteid={urllib.parse.quote(paste_id, safe='')}"
    fragment = urllib.parse.unquote(split.fragment).strip().split("&", 1)[0]
    return api_url, fragment


def _decrypt_privatebin_payload(payload, key_text):
    """Decifra il payload JSON PrivateBin 1.x (AES-GCM + PBKDF2)."""
    if _load_crypto() is None:
        raise PrivateBinError(
            "pycryptodome non è installato: impossibile decifrare il paste PrivateBin.")
    try:
        data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    except (TypeError, ValueError) as exc:
        raise PrivateBinError("La risposta del paste non è un JSON valido.") from exc
    if not isinstance(data, dict):
        raise PrivateBinError("La risposta del paste non è valida.")
    status = data.get("status", 0)
    if status not in (0, "0", None):
        message = data.get("message") or data.get("error") or f"stato {status}"
        raise PrivateBinError(f"Il server del paste ha restituito un errore: {message}")
    adata = data.get("adata")
    ciphertext_b64 = data.get("ct")
    if not isinstance(adata, list) or not adata or not ciphertext_b64:
        raise PrivateBinError("Il paste è vuoto o non contiene dati cifrati.")
    spec = adata[0]
    if not isinstance(spec, list) or len(spec) < 8:
        raise PrivateBinError("Formato di cifratura del paste non riconosciuto.")
    if str(spec[5]).lower() != "aes" or str(spec[6]).lower() != "gcm":
        raise PrivateBinError("Il paste usa una cifratura non supportata.")
    key = _base58_decode(key_text)
    if not key:
        raise PrivateBinError("La chiave del paste non è valida.")
    key = key.rjust(int(spec[3]) // 8 or 32, b"\x00")
    try:
        salt = base64.b64decode(spec[1])
        nonce = base64.b64decode(spec[0])
        ciphertext = base64.b64decode(ciphertext_b64)
    except (ValueError, TypeError) as exc:
        raise PrivateBinError("I dati cifrati del paste sono danneggiati.") from exc
    tag_length = int(spec[4]) // 8
    if tag_length <= 0 or len(ciphertext) <= tag_length:
        raise PrivateBinError("I dati cifrati del paste sono danneggiati.")
    additional_data = json.dumps(
        adata, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    derived = hashlib.pbkdf2_hmac(
        "sha256", key, salt, int(spec[2]), dklen=int(spec[3]) // 8 or 32)
    cipher = _CryptoAES.new(derived, _CryptoAES.MODE_GCM,
                            nonce=nonce, mac_len=tag_length)
    cipher.update(additional_data)
    try:
        plain = cipher.decrypt_and_verify(
            ciphertext[:-tag_length], ciphertext[-tag_length:])
    except (ValueError, KeyError) as exc:
        raise PrivateBinError(
            "Impossibile decifrare il paste: chiave errata o contenuto danneggiato.") from exc
    mode = str(spec[7]).lower()
    if mode in ("zlib", "rawdeflate"):
        try:
            plain = zlib.decompress(plain, -15)
        except zlib.error:
            try:
                plain = zlib.decompress(plain)
            except zlib.error as exc:
                raise PrivateBinError(
                    "Il contenuto del paste non può essere decompresso.") from exc
    text = plain.decode("utf-8", errors="replace")
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        decoded = None
    if isinstance(decoded, dict):
        for field in ("paste", "text", "data", "content"):
            value = decoded.get(field)
            if isinstance(value, str):
                return value
    return text


def _fetch_privatebin_paste(url, session=None, timeout=25):
    """Scarica e decifra un paste PrivateBin; restituisce il testo in chiaro."""
    api_url, key_text = _privatebin_api_url(url)
    if not key_text:
        raise PrivateBinError(
            "Il link non contiene la chiave di decifratura (la parte dopo #).")
    session = session or make_http_session(api_url)
    response, session = _request_with_fallback(
        session, "GET", api_url,
        headers={"X-Requested-With": "JSONHttpRequest",
                 "Accept": "application/json"},
        timeout=timeout)
    if _looks_like_cloudflare(response):
        raise PrivateBinError(CLOUDFLARE_HELP)
    response.raise_for_status()
    return _decrypt_privatebin_payload(response.text or "", key_text)


_URL_IN_TEXT_RE = re.compile(r"""(?i)https?://[^\s<>"'`\\]+""")


def extract_urls_from_text(text):
    """Estrae gli URL da testo semplice o markdown, conservando l'ordine."""
    if not text:
        return []
    text = html_module.unescape(str(text))
    urls = []
    seen = set()
    for match in _URL_IN_TEXT_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}\u00bb\"'")
        if not url:
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _suggested_filename(url):
    """Preferisce il nome contenuto nel frammento (#nomefile.rar) quando presente."""
    split = urllib.parse.urlsplit(url)
    fragment = urllib.parse.unquote(split.fragment).strip()
    if fragment:
        name = sanitize_filename(fragment, "")
        extension = os.path.splitext(name)[1].lstrip(".").lower()
        if name and extension in _DOWNLOAD_EXTENSIONS:
            return name
    return _filename_from_url(url)


def fetch_urls_from_source(url, session=None, timeout=25):
    """Estrae i link da un paste PrivateBin oppure da una pagina/testo remoto."""
    split = urllib.parse.urlsplit((url or "").strip())
    if split.scheme not in ("http", "https") or not split.netloc:
        raise PrivateBinError("Inserisci un link HTTP o HTTPS valido.")
    if parse_mega_file_url(url):
        return [url]
    host = (split.hostname or "").lower()
    if any(host == protected or host.endswith("." + protected)
           for protected in PROTECTED_HOSTS):
        return [url]
    if looks_like_privatebin_url(url):
        # PrivateBin mette la chiave di decifratura nel frammento.
        text = _fetch_privatebin_paste(url, session, timeout)
        urls = extract_urls_from_text(text)
        if not urls:
            raise PrivateBinError("Il paste non contiene link.")
        return urls
    session = session or make_http_session(url)
    response, session = _request_with_fallback(
        session, "GET", url, headers={"Referer": url}, timeout=timeout)
    if _looks_like_cloudflare(response):
        raise PrivateBinError(CLOUDFLARE_HELP)
    if response.status_code in (401, 403):
        raise PrivateBinError(
            "Il sito ha negato l'accesso (HTTP %d). Serve un account attivo/accesso VIP "
            "oppure i cookie di sessione: apri il sito nel browser, copia l'header Cookie "
            "e incollalo in «Cookie host protetti»." % response.status_code)
    response.raise_for_status()
    body = response.text or ""
    content_type = str(response.headers.get("Content-Type", "")).lower()
    if "html" in content_type:
        urls = extract_download_links(body, response.url or url)
    else:
        urls = extract_urls_from_text(body)
    if not urls:
        raise PrivateBinError("Il paste non contiene link.")
    return urls


TORRENT_SEARCH_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
)


def _torrent_magnet(info_hash, name):
    """Costruisce un magnet con tracker comuni a partire dall'hash."""
    trackers = "".join(
        "&tr=" + urllib.parse.quote(tracker, safe="")
        for tracker in TORRENT_SEARCH_TRACKERS)
    return (f"magnet:?xt=urn:btih:{str(info_hash).lower()}"
            f"&dn={urllib.parse.quote(name or '', safe='')}{trackers}")


TORRENT_SEARCH_API_CATALOG = (
    {
        "name": "Internet Archive",
        "aliases": ("internet archive", "archive.org", "librivox"),
        "builtin": True,
        "url": "",
    },
)


def resolve_torrent_search_api(value):
    """Riconosce un nome noto o un URL/dominio inserito dall'utente."""
    text = (value or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    for entry in TORRENT_SEARCH_API_CATALOG:
        if (lowered == entry["name"].casefold()
                or any(alias in lowered for alias in entry["aliases"])):
            return {"name": entry["name"], "url": entry["url"],
                    "builtin": entry["builtin"]}
    if "://" in text or "{query}" in text:
        api = _normalize_torrent_search_api(
            {"name": urllib.parse.urlsplit(
                text.replace("{query}", "test")).netloc, "url": text})
        if api is not None:
            return {"name": api["name"], "url": api["url"], "builtin": False}
    return None


def looks_like_domain(value):
    return bool(re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}",
        (value or "").strip().casefold()))


def probe_torrent_search_api(value):
    """Cerca un endpoint JSON comune per un dominio indicato dall'utente."""
    base = (value or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    templates = (
        base + "/api/search?q={query}",
        base + "/api/torrents?search={query}",
        base + "/api/v1/search?q={query}",
        base + "/search?q={query}&format=json",
        base + "/search.json?q={query}",
        base + "/api/search?query={query}",
    )
    session = make_http_session(base + "/")
    try:
        for template in templates:
            try:
                response = session.get(template.replace("{query}", "test"),
                                       timeout=8, allow_redirects=True)
                if response.status_code != 200:
                    continue
                content_type = response.headers.get("Content-Type", "").lower()
                body = (response.text or "").lstrip()
                if "json" not in content_type and not body.startswith(("{", "[")):
                    continue
                payload = response.json()
                rows = payload
                recognised = False
                if isinstance(payload, dict):
                    for key in ("results", "torrents", "data", "items", "hits"):
                        value = payload.get(key)
                        if isinstance(value, list):
                            rows, recognised = value, True
                            break
                        if (isinstance(value, dict)
                                and isinstance(value.get("hits"), list)):
                            rows, recognised = value["hits"], True
                            break
                if not recognised and not isinstance(payload, list):
                    continue
                if not isinstance(rows, list):
                    continue
                for row in rows[:5]:
                    if not isinstance(row, dict):
                        continue
                    has_name = any(key in row for key in
                                   ("name", "title", "torrent_name"))
                    has_target = any(key in row for key in (
                        "magnet", "magnet_uri", "magnet_link", "infohash",
                        "info_hash", "hash", "torrent_url", "torrent",
                        "url", "link"))
                    if has_name and has_target:
                        return template
            except Exception:
                continue
    finally:
        try:
            session.close()
        except Exception:
            pass
    return ""


def _torrent_search_int(value, default=0):
    try:
        if isinstance(value, bool):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _torrent_search_first(row, names):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _torrent_search_internet_archive(query, session, limit=30):
    """Risultati da Internet Archive: pubblico dominio, CC e software libero.

    Internet Archive è una fonte legale e non bloccata in Italia; i torrent
    pubblicati riguardano opere libere o comunque ospitate dalla piattaforma.
    """
    safe_query = (query or "").replace("\\", " ").replace('"', " ").strip()
    params = {
        "fl[]": ["identifier", "title", "item_size", "downloads"],
        "rows": max(1, int(limit)),
        "page": 1,
        "output": "json",
    }
    queries = [f'format:("Archive BitTorrent") AND title:("{safe_query}")']
    if safe_query:
        queries.append(f'format:("Archive BitTorrent") AND ({safe_query})')
    docs = []
    for search_query in queries:
        response = session.get(
            "https://archive.org/advancedsearch.php",
            params={**params, "q": search_query}, timeout=25)
        response.raise_for_status()
        payload = response.json()
        docs = ((payload or {}).get("response") or {}).get("docs") or []
        if docs:
            break
    results = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        identifier = str(doc.get("identifier") or "").strip()
        if not identifier:
            continue
        name = html_module.unescape(
            str(doc.get("title") or identifier)).strip()
        torrent_name = f"{identifier}_archive.torrent"
        torrent_url = ("https://archive.org/download/"
                       f"{urllib.parse.quote(identifier, safe='')}/"
                       f"{urllib.parse.quote(torrent_name, safe='')}")
        results.append({
            "infohash": "",
            "name": name or identifier,
            "size": _torrent_search_int(doc.get("item_size"), 0),
            "seeders": None,
            "leechers": None,
            "popularity": _torrent_search_int(doc.get("downloads"), 0),
            "source": "Internet Archive",
            "magnet": None,
            "torrent_url": torrent_url,
            "identifier": identifier,
        })
    return results[:limit]


def _torrent_search_custom(api_url, query, session, limit=30,
                           source_name="API personalizzata"):
    """Risultati dall'API JSON scelta dall'utente nelle impostazioni."""
    api_url = (api_url or "").strip()
    if not api_url:
        return []
    response = session.get(_torrent_search_api_url(api_url, query), timeout=25)
    response.raise_for_status()
    payload = response.json()
    rows = payload
    if isinstance(payload, dict):
        rows = []
        for key in ("results", "torrents", "data", "items", "hits"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
            if isinstance(value, dict) and isinstance(value.get("hits"), list):
                rows = value["hits"]
                break
    results = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = str(_torrent_search_first(
            row, ("name", "title", "torrent_name")) or "").strip()
        size = _torrent_search_int(_torrent_search_first(
            row, ("size", "size_bytes", "length", "total_size", "bytes")), 0)
        seeders = _torrent_search_int(
            _torrent_search_first(row, ("seeders", "seeds", "seed")), None)
        leechers = _torrent_search_int(
            _torrent_search_first(row, ("leechers", "peers", "peer")), None)
        magnet = _torrent_search_first(
            row, ("magnet", "magnet_uri", "magnet_link"))
        infohash = str(_torrent_search_first(
            row, ("infohash", "info_hash", "hash")) or "").strip()
        torrent_url = _torrent_search_first(
            row, ("torrent_url", "torrent"))
        if not torrent_url:
            candidate = _torrent_search_first(row, ("url", "link"))
            if (isinstance(candidate, str)
                    and candidate.lower().startswith(("http://", "https://"))
                    and ".torrent" in candidate.lower()):
                torrent_url = candidate
        if isinstance(magnet, str) and magnet.lower().startswith("magnet:?"):
            parsed = parse_magnet_uri(magnet)
            if not parsed:
                continue
            infohash = parsed.get("info_hash") or infohash
            if not name:
                name = parsed.get("name") or ""
        elif re.fullmatch(r"[0-9A-Fa-f]{40}|[A-Z2-7a-z2-7]{32}", infohash):
            magnet = _torrent_magnet(infohash, name)
        elif isinstance(torrent_url, str) and torrent_url.lower().startswith(
                ("http://", "https://")):
            magnet = None
        else:
            continue
        if not name:
            name = infohash or "Torrent"
        results.append({
            "infohash": (infohash or "").lower(),
            "name": html_module.unescape(name)[:300],
            "size": max(0, size or 0),
            "seeders": seeders,
            "leechers": leechers,
            "popularity": 0,
            "source": source_name,
            "magnet": magnet,
            "torrent_url": (torrent_url
                            if isinstance(torrent_url, str)
                            and torrent_url.lower().startswith(("http://", "https://"))
                            else None),
            "identifier": "",
        })
    return results[:limit]


_QUERY_PLACEHOLDER = re.compile(r"\[QUERY\]", re.IGNORECASE)


def _torrent_search_api_url(api_url, query):
    encoded = urllib.parse.quote_plus(query)
    if "{query}" in api_url:
        return api_url.replace("{query}", encoded)
    if _QUERY_PLACEHOLDER.search(api_url):
        return _QUERY_PLACEHOLDER.sub(lambda _match: encoded, api_url)
    return api_url + ("&" if "?" in api_url else "?") + "q=" + encoded


def test_torrent_search_api(api_url, session=None):
    """Verifica che l'API risponda con JSON nel formato supportato.

    Restituisce ``{"ok": bool, "results": int, "error": codice, "detail": testo}``.
    """
    api_url = (api_url or "").strip()
    if not api_url:
        return {"ok": False, "results": 0, "error": "missing", "detail": ""}
    placeholder = None
    for match in re.finditer(r"\[[A-Za-z0-9_]+\]", api_url):
        if match.group(0).casefold() != "[query]":
            placeholder = match
            break
    if placeholder is not None:
        return {"ok": False, "results": 0, "error": "placeholder",
                "detail": placeholder.group(0)}
    try:
        parsed = urllib.parse.urlsplit(api_url.replace("{query}", "test"))
        if parsed.path in ("", "/") and not parsed.query:
            return {"ok": False, "results": 0, "error": "homepage",
                    "detail": ""}
    except ValueError:
        pass
    own_session = session is None
    session = session or make_http_session(api_url)
    try:
        response = session.get(_torrent_search_api_url(api_url, "test"),
                               timeout=20, allow_redirects=True)
        if response.status_code != 200:
            return {"ok": False, "results": 0, "error": "http",
                    "detail": str(response.status_code)}
        content_type = response.headers.get("Content-Type", "").lower()
        body = (response.text or "").lstrip()
        if "json" not in content_type and not body.startswith(("{", "[")):
            return {"ok": False, "results": 0, "error": "not_json", "detail": ""}
        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "results": 0, "error": "invalid_json",
                    "detail": ""}
        rows = payload
        recognised = isinstance(payload, list)
        if isinstance(payload, dict):
            for key in ("results", "torrents", "data", "items", "hits"):
                value = payload.get(key)
                if isinstance(value, list):
                    rows, recognised = value, True
                    break
                if (isinstance(value, dict)
                        and isinstance(value.get("hits"), list)):
                    rows, recognised = value["hits"], True
                    break
        if not recognised or not isinstance(rows, list):
            return {"ok": False, "results": 0, "error": "format", "detail": ""}
        valid_rows = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            has_name = any(key in row for key in
                           ("name", "title", "torrent_name"))
            has_target = any(key in row for key in (
                "magnet", "magnet_uri", "magnet_link", "infohash",
                "info_hash", "hash", "torrent_url", "torrent", "url", "link"))
            if has_name and has_target:
                valid_rows += 1
        if rows and not valid_rows:
            return {"ok": False, "results": 0, "error": "empty", "detail": ""}
        return {"ok": True, "results": valid_rows, "error": "", "detail": ""}
    except Exception as exc:
        return {"ok": False, "results": 0, "error": "network",
                "detail": str(exc) or exc.__class__.__name__}
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def search_public_torrents(query, limit=60, custom_apis=None):
    """Cerca su fonti legali e sulle API scelte dall'utente."""
    query = (query or "").strip()
    if not query:
        return []
    if custom_apis is None:
        custom_apis = load_torrent_search_apis()
    session = make_http_session("https://archive.org/")
    merged = {}
    try:
        providers = [("Internet Archive", _torrent_search_internet_archive)]
        for api in custom_apis or ():
            url = str(api.get("url") or "").strip()
            name = str(api.get("name") or "API personalizzata").strip()
            if not url:
                continue
            providers.append((
                name,
                lambda q, s, n, u=url, src=name:
                    _torrent_search_custom(u, q, s, n, source_name=src)))
        for label, provider in providers:
            try:
                rows = provider(query, session, 30)
            except Exception as exc:
                logging.info("Ricerca torrent non riuscita su %s: %s", label, exc)
                continue
            for row in rows:
                key = (row.get("infohash") or row.get("torrent_url")
                       or f"{row.get('name')}|{row.get('size')}")
                current = merged.get(key)
                if current is None or (
                        _torrent_search_int(row.get("seeders"), 0)
                        > _torrent_search_int(current.get("seeders"), 0)):
                    merged[key] = row
    finally:
        try:
            session.close()
        except Exception:
            pass
    ordered = sorted(
        merged.values(),
        key=lambda item: (
            _torrent_search_int(item.get("seeders"), 0),
            _torrent_search_int(item.get("leechers"), 0),
            _torrent_search_int(item.get("popularity"), 0)),
        reverse=True)
    return ordered[:limit]


class ImportGroup:
    """Gruppo espandibile dei file aggiunti da un'unica importazione."""
    def __init__(self, iid, name):
        self.iid = iid
        self.name = name


class DownloadTask:
    """Stato di un singolo elemento della coda."""
    def __init__(self, ident, url, order, filename=None):
        self.id = ident
        self.url = url
        self.resolved_url = None
        self.session = None
        self.import_group_iid = None
        self.resolve_event = threading.Event()
        self.resolve_started = False
        self.overwrite_event = threading.Event()
        self.overwrite_allowed = False
        self.overwrite_path = None
        self.order = order
        self.preferred_name = filename
        self.name = filename or filename_from_headers(url, {})
        self.priority = "Normale"
        self.state = "In coda"
        self.downloaded = 0
        self.total = None
        self.speed = 0.0
        self.mime = "—"
        self.path = ""
        self.error = ""
        self.cancel_event = threading.Event()
        # Stato esplicito per il ciclo asincrono dei magnet; distinto dagli
        # altri eventi di arresto usati da download e pausa.
        self.cancelled = False
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.running = False
        self.bypass = False
        self.limit_paused = False
        self.manual_resume_pending = False
        self.start_sequence = 0
        self.response = None
        self._last_ui = 0.0
        self.torrent_job = None
        self.torrent_index = None
        self.mega_info = None
        self.torrent_placeholder = False
        self.metadata_running = False
        self.peers = 0
        self.persist_on_close = False
        # Validatori HTTP usati con If-Range per riprendere un .part solo se il
        # file remoto non è cambiato nel frattempo.
        self.etag = ""
        self.last_modified = ""

    @property
    def percent(self):
        if self.total and self.total > 0:
            return min(100, int(self.downloaded * 100 / self.total))
        return 0


class TorrentJob:
    """Sessione condivisa e metadati di un singolo torrent."""
    def __init__(self, session, handle, magnet_url):
        self.session = session
        self.handle = handle
        self.magnet_url = magnet_url
        self.files = []
        self.files_by_index = {}
        self.preexisting = {}
        self.tasks = {}
        self.info = None
        self.selected_indices = set()
        self._release_lock = threading.Lock()
        self._removed = False
        self.peer_count = 0
        self.download_rate = 0.0
        self.upload_rate = 0.0
        self.group_iid = None
        self.selection_pending = False
        self.removed_tasks = []


class DownloadManager:
    """Thread worker e schedulatore, senza dipendenze dalla GUI."""
    def __init__(self, events, destination, max_concurrent=2):
        self.events = events
        self.destination = destination
        self.max_concurrent = max(1, int(max_concurrent))
        self.download_rate_limit = 0
        self.upload_rate_limit = 0
        self.connections_limit = 200
        self.tasks = []
        self._condition = threading.Condition()
        self._next_id = 1
        self._next_order = 1
        self._next_start_sequence = 1
        self._queue_enabled = False
        self._reserved = set()
        self._active = set()
        self._closed = False
        self._magnet_pending = set()
        self._torrent_jobs = set()
        self._torrent_session = None
        self._torrent_session_lock = threading.Lock()
        threading.Thread(target=self._scheduler, name="download-scheduler", daemon=True).start()

    def _notify(self, task):
        self.events.put(("update", task.id))

    def set_download_limits(self, download_rate_limit, upload_rate_limit, connections_limit):
        self.download_rate_limit = max(0, int(download_rate_limit))
        self.upload_rate_limit = max(0, int(upload_rate_limit))
        self.connections_limit = min(1000, max(1, int(connections_limit)))
        HTTP_DOWNLOAD_THROTTLE.set_rate(self.download_rate_limit)
        with self._condition:
            jobs = list(self._torrent_jobs)
        # Keep to libtorrent's session settings: invalid/deprecated keys can
        # make apply_settings reject the whole dictionary, including rate caps.
        settings = {"download_rate_limit": self.download_rate_limit,
                    "upload_rate_limit": self.upload_rate_limit,
                    "connections_limit": self.connections_limit}
        with self._torrent_session_lock:
            session = self._torrent_session
            if session is not None:
                try:
                    if hasattr(session, "apply_settings"):
                        session.apply_settings(settings)
                    else:
                        session.set_settings(settings)
                except Exception:
                    logging.exception("Impossibile aggiornare i limiti torrent")
        for job in jobs:
            try:
                job.handle.set_max_connections(self.connections_limit)
            except Exception:
                pass

    def _configure_new_torrent_session(self, session):
        settings = {"download_rate_limit": max(0, int(self.download_rate_limit)),
                    "upload_rate_limit": max(0, int(self.upload_rate_limit)),
                    "connections_limit": min(1000, max(1, int(self.connections_limit)))}
        try:
            if hasattr(session, "apply_settings"): session.apply_settings(settings)
            else: session.set_settings(settings)
        except Exception: logging.exception("Impossibile configurare i limiti torrent")

    def _get_torrent_session(self):
        """Restituisce l'unica sessione libtorrent condivisa da tutti i torrent."""
        with self._torrent_session_lock:
            if self._closed:
                raise RuntimeError("La sessione torrent è già stata chiusa.")
            if self._torrent_session is not None:
                return self._torrent_session
            libtorrent = _load_libtorrent()
            if libtorrent is None:
                raise RuntimeError("libtorrent non è disponibile.")
            session = libtorrent.session()
            self._configure_new_torrent_session(session)
            try:
                session.listen_on(6881, 6891)
            except Exception:
                logging.info("Porta torrent non disponibile; proseguo con porte casuali")
            try:
                session.start_dht()
                session.add_dht_router("router.bittorrent.com", 6881)
            except Exception:
                logging.info("Configurazione DHT non disponibile; proseguo con tracker/peer magnet")
            self._torrent_session = session
            return session

    def add(self, url, filename=None):
        url = url.strip()
        if parse_magnet_uri(url):
            return self.add_magnet(url)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Inserisci un URL HTTP o HTTPS valido.")
        with self._condition:
            task = DownloadTask(self._next_id, url, self._next_order, filename)
            self._next_id += 1
            self._next_order += 1
            self.tasks.append(task)
            self._condition.notify_all()
        self._notify(task)
        # La risoluzione avviene quando lo scheduler assegna uno slot al task:
        # importare molti link non deve creare decine di resolver in parallelo.
        return task

    def add_magnet(self, url, restore_selection=None, restore_children=None):
        parsed = parse_magnet_uri(url)
        if not parsed:
            raise ValueError("Magnet non valido: è richiesto un hash BTIH (xt=urn:btih:…).")
        with self._condition:
            existing = next((t for t in self.tasks if t.url == url and
                             (t.torrent_placeholder or t.torrent_job is not None)), None)
            if existing:
                return existing
            task = DownloadTask(self._next_id, url, self._next_order,
                                parsed.get("name") or "Ricerca metadati torrent…")
            task.torrent_placeholder = True
            task.state = "Ricerca metadati (timeout 60 s)…"
            task.resolve_started = True
            if restore_selection is not None:
                task._restore_torrent_selection = list(restore_selection)
                task._restore_torrent_children = dict(restore_children or {})
            self._next_id += 1
            self._next_order += 1
            self.tasks.append(task)
            self._magnet_pending.add(url)
        self._notify(task)
        task.metadata_running = True
        threading.Thread(target=self._resolve_magnet, args=(task,),
                         name=f"magnet-metadata-{task.id}", daemon=True).start()
        return task

    def add_torrent_file(self, path, restore_selection=None, restore_children=None):
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path) or not path.lower().endswith(".torrent"):
            raise ValueError("Seleziona un file .torrent valido.")
        with self._condition:
            existing = next(
                (t for t in self.tasks if t.torrent_placeholder
                 and os.path.abspath(t.url) == path), None)
            if existing is not None:
                return existing
            task = DownloadTask(self._next_id, path, self._next_order, os.path.basename(path))
            task.torrent_placeholder = True
            task.state = "Recupero metadati torrent…"
            task.resolve_started = True
            if restore_selection is not None:
                task._restore_torrent_selection = list(restore_selection)
                task._restore_torrent_children = dict(restore_children or {})
            self._next_id += 1
            self._next_order += 1
            self.tasks.append(task)
        self._notify(task)
        task.metadata_running = True
        threading.Thread(target=self._resolve_torrent_file, args=(task, path),
                         name=f"torrent-file-{task.id}", daemon=True).start()
        return task

    def retry_torrent_file(self, placeholder):
        path = placeholder.url
        with self._condition:
            if placeholder not in self.tasks or placeholder.cancel_event.is_set():
                return False
            placeholder.state = "Recupero metadati torrent…"
            placeholder.error = ""
            placeholder.cancel_event.clear()
        self._notify(placeholder)
        threading.Thread(target=self._resolve_torrent_file, args=(placeholder, path),
                         name=f"torrent-file-{placeholder.id}", daemon=True).start()
        return True

    def _torrent_files(self, info):
        storage = info.files()
        pad_flag = getattr(getattr(lt, "file_storage", None), "flag_pad_file", 0)
        files = []
        for index in range(storage.num_files()):
            path = storage.file_path(index)
            size = storage.file_size(index)
            safe_path = os.path.normpath(path).replace("\\", os.sep).replace("/", os.sep)
            if os.path.isabs(safe_path) or safe_path == ".." or safe_path.startswith(".." + os.sep):
                raise ValueError("Il torrent contiene un percorso file non sicuro.")
            try:
                is_pad = bool(storage.file_flags(index) & pad_flag)
            except Exception:
                is_pad = False
            files.append({"index": index, "path": safe_path,
                          "size": int(size), "pad": is_pad})
        return files

    def _torrent_output_path(self, item):
        """Percorso finale di un file torrent, o None se esce dalla cartella."""
        try:
            root = os.path.abspath(self.destination)
            path = os.path.abspath(os.path.join(root, item["path"]))
            if os.path.commonpath((root, path)) != root:
                return None
            return path
        except (KeyError, TypeError, ValueError, OSError):
            return None

    def _resolve_torrent_file(self, placeholder, path):
        job = None
        try:
            if placeholder.cancel_event.is_set() or self._closed:
                return
            if _load_libtorrent() is None:
                placeholder.state = "Libtorrent non installato"
                self.events.put(("libtorrent_missing", placeholder.id))
                return
            info = lt.torrent_info(path)
            session = self._get_torrent_session()
            params = {"ti": info, "save_path": self.destination,
                      "file_priorities": [0] * info.files().num_files()}
            try:
                handle = session.add_torrent(params)
                try: handle.set_max_connections(self.connections_limit)
                except Exception: pass
            except (TypeError, AttributeError):
                params_obj = lt.add_torrent_params()
                params_obj.ti = info
                params_obj.save_path = self.destination
                try: params_obj.file_priorities = [0] * info.files().num_files()
                except Exception: pass
                handle = session.add_torrent(params_obj)
                try: handle.set_max_connections(self.connections_limit)
                except Exception: pass
            job = TorrentJob(session, handle, path)
            try: handle.pause()
            except Exception: pass
            with self._condition:
                self._torrent_jobs.add(job)
            self._request_torrent_selection(placeholder, job, info)
        except Exception as exc:
            placeholder.state, placeholder.error = "Errore", str(exc)
            logging.exception("Impossibile leggere il torrent %s", path)
        finally:
            placeholder.metadata_running = False
            with self._condition:
                self._condition.notify_all()
            if job is not None and not job.selection_pending and not job.tasks:
                self._release_torrent_job(job)
            self._notify(placeholder)

    def _request_torrent_selection(self, placeholder, job, info):
        """Accoda la selezione solo se il placeholder è tuttora attivo."""
        files = self._torrent_files(info)
        # Verifica, preparazione del job e accodamento sono atomici rispetto
        # alla cancellazione: il worker non può toccare handle già rimosso.
        with self._condition:
            if (placeholder not in self.tasks or placeholder.cancelled
                    or placeholder.cancel_event.is_set() or placeholder.state == "Annullato"
                    or self._closed or job._removed or job.handle is None):
                job.selection_pending = False
                return False
            if job.selection_pending:
                return False
            job.files, job.info, job.selection_pending = files, info, True
            job.files_by_index = {item["index"]: item for item in files}
            try:
                job.handle.pause()
            except Exception:
                pass
            try:
                job.handle.prioritize_files([0] * len(files))
            except Exception:
                logging.exception("Impossibile inizializzare le priorità torrent")
            placeholder.state = "Seleziona i file del torrent"
            restored = getattr(placeholder, "_restore_torrent_selection", None)
            if restored is not None:
                # Selezione salvata in precedenza: evita di riproporre il dialogo.
                self.confirm_torrent_selection(placeholder, job, restored)
            else:
                self.events.put(("torrent_selection", placeholder.id, job))
        return True

    def confirm_torrent_selection(self, placeholder, job, selected_indices):
        selected = []
        for value in selected_indices or ():
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            item = job.files_by_index.get(index)
            if item is None or item.get("pad"):
                continue
            if index not in selected:
                selected.append(index)
        selected.sort()
        # Fotografa i file già presenti prima di qualsiasi trasferimento: serve
        # a distinguere i file dell'utente dall'allocazione fatta da libtorrent.
        job.preexisting = {}
        for index in selected:
            path = self._torrent_output_path(job.files_by_index[index])
            if path and os.path.isfile(path):
                try:
                    job.preexisting[index] = os.path.getsize(path)
                except OSError:
                    job.preexisting[index] = -1
        children = []
        with self._condition:
            if (placeholder not in self.tasks or placeholder.cancelled
                    or placeholder.cancel_event.is_set() or job._removed):
                return False
            # Le priorità restano a zero e il torrent resta in pausa: i file
            # non vengono creati né scaricati finché il relativo task non parte.
            try:
                job.handle.prioritize_files([0] * len(job.files))
                job.handle.pause()
            except Exception:
                logging.exception("Impossibile preparare la selezione torrent")
            self.tasks.remove(placeholder)
            job.selection_pending = False
            job.selected_indices = set(selected)
            for index in selected:
                item = job.files_by_index[index]
                child = DownloadTask(self._next_id, placeholder.url, self._next_order,
                                     filename=item["path"])
                self._next_id += 1
                self._next_order += 1
                child.total, child.mime = item["size"], "BitTorrent"
                child.torrent_job, child.torrent_index = job, index
                self.tasks.append(child)
                children.append(child)
                job.tasks[index] = child
            self._magnet_pending.discard(placeholder.url)
            self._condition.notify_all()
        self.events.put(("torrent_ready", placeholder.id, children, job))
        if not children:
            with self._condition:
                self._torrent_jobs.discard(job)
            self._release_torrent_job(job)
        return True

    def cancel_torrent_selection(self, placeholder, job):
        """Invalida il placeholder e chiude la sessione torrent una sola volta."""
        with self._condition:
            placeholder.cancelled = True
            placeholder.cancel_event.set()
            placeholder.state = "Annullato"
            placeholder.error = ""
            if placeholder in self.tasks:
                self.tasks.remove(placeholder)
            job.selection_pending = False
            self._magnet_pending.discard(placeholder.url)
            self._torrent_jobs.discard(job)
            self._condition.notify_all()
        self._notify(placeholder)
        self._release_torrent_job(job)

    def retry_magnet(self, placeholder):
        """Riprende la risoluzione metadati di un magnet sospeso."""
        with self._condition:
            if (placeholder not in self.tasks or placeholder.cancelled
                    or placeholder.cancel_event.is_set()):
                return False
            placeholder.state = "Ricerca metadati (timeout 60 s)…"
            placeholder.error = ""
            self._magnet_pending.add(placeholder.url)
        self._notify(placeholder)
        threading.Thread(target=self._resolve_magnet, args=(placeholder,),
                         name=f"magnet-metadata-{placeholder.id}", daemon=True).start()
        return True

    def _resolve_magnet(self, placeholder):
        job = None
        try:
            if placeholder.cancelled or placeholder.cancel_event.is_set() or self._closed:
                return
            if _load_libtorrent() is None:
                placeholder.state = "Libtorrent non installato"
                self.events.put(("libtorrent_missing", placeholder.id))
                return
            session = self._get_torrent_session()
            params = lt.parse_magnet_uri(placeholder.url)
            params.save_path = self.destination
            # Serializza add_torrent e registrazione del job con cancel(): se
            # l'annullamento vince la gara non si crea un handle/sessione torrent;
            # se vince il worker, cancel() trova e rimuove il job registrato.
            with self._condition:
                if (placeholder not in self.tasks or placeholder.cancelled
                        or placeholder.cancel_event.is_set() or self._closed):
                    return
                handle = session.add_torrent(params)
                try: handle.set_max_connections(self.connections_limit)
                except Exception: pass
                job = TorrentJob(session, handle, placeholder.url)
                self._torrent_jobs.add(job)
            deadline = time.monotonic() + MAGNET_METADATA_TIMEOUT
            last_update = 0.0
            while time.monotonic() < deadline:
                if placeholder.cancelled or placeholder.cancel_event.is_set() or self._closed:
                    raise InterruptedError("Ricerca metadati annullata")
                if handle.has_metadata():
                    # Il polling può restituire metadati contemporaneamente a
                    # cancel(); ricontrolla prima di leggere/inoltrare l'evento.
                    if placeholder.cancelled or placeholder.cancel_event.is_set():
                        return
                    info = handle.get_torrent_info() if hasattr(handle, "get_torrent_info") else handle.torrent_file()
                    if placeholder.cancelled or placeholder.cancel_event.is_set():
                        return
                    self._request_torrent_selection(placeholder, job, info)
                    return
                now = time.monotonic()
                if now - last_update >= 2:
                    placeholder.state = f"Ricerca metadati… {max(0, int(deadline-now))} s"
                    self._notify(placeholder)
                    last_update = now
                try:
                    session.wait_for_alert(100)
                except Exception:
                    placeholder.cancel_event.wait(0.1)
            raise TimeoutError("Timeout: metadati torrent non ricevuti entro 60 secondi. Verifica rete, tracker o peer.")
        except InterruptedError as exc:
            placeholder.state, placeholder.error = "Annullato", str(exc)
        except Exception as exc:
            if placeholder.cancel_event.is_set():
                placeholder.state, placeholder.error = "Annullato", ""
            else:
                placeholder.state = "Errore"
                placeholder.error = str(exc)
                logging.exception("Risoluzione magnet fallita: %s", placeholder.url)
        finally:
            if (placeholder.state not in ("Errore", "Annullato", "Libtorrent non installato",
                                          "Installazione libtorrent in corso…") and job is None):
                placeholder.state = "Errore"
            if job is not None and not job.tasks and not job.selection_pending:
                self._release_torrent_job(job)
            with self._condition:
                self._magnet_pending.discard(placeholder.url)
                placeholder.metadata_running = False
                self._condition.notify_all()
            self._notify(placeholder)

    @staticmethod
    def _release_torrent_job(job):
        """Rimuove il torrent dalla sessione e lascia liberi gli handle dei file."""
        if job is None:
            return False
        with job._release_lock:
            if job._removed:
                return False
            job._removed = True
            session, handle = job.session, job.handle
            # Svuota prima i riferimenti condivisi; la rimozione della sessione
            # è ciò che chiude i file aperti da libtorrent.
            job.handle = None
            job.session = None
        if handle is not None:
            try:
                handle.pause()
            except Exception:
                pass
        if session is not None and handle is not None:
            try:
                session.remove_torrent(handle)
            except Exception:
                logging.exception("Impossibile rimuovere il torrent dalla sessione")
        # Windows può lasciare il bit read-only sui file completati da torrent.
        # Correggilo solo dopo la rimozione del torrent, quando gli handle sono liberi.
        for task in job.tasks.values():
            if task.state == "Completato" and task.path and os.path.isfile(task.path):
                try:
                    mode = os.stat(task.path).st_mode
                    os.chmod(task.path, mode | stat.S_IWRITE)
                except OSError:
                    logging.exception("Impossibile rendere scrivibile il file torrent %s", task.path)
            elif task.state == "Annullato" and task.path and os.path.isfile(task.path):
                try:
                    os.chmod(task.path, os.stat(task.path).st_mode | stat.S_IWRITE)
                    os.remove(task.path)
                except OSError:
                    logging.exception("Impossibile rimuovere il parziale torrent %s", task.path)
        return True

    def _maybe_release_torrent_job(self, job):
        if job is None:
            return
        discarded = []
        with self._condition:
            # Le righe torrent ancora in coda contano anche se non sono state
            # ancora aggiunte a selected_indices dal worker: non chiudere la
            # sessione mentre esistono file del gruppo ancora gestiti.
            remaining = list(job.tasks.values())
            terminal = bool(remaining) and all(
                t.state in ("Completato", "Annullato", "Errore") and not t.running
                for t in remaining)
            should_release = self._closed or terminal
            if should_release:
                self._torrent_jobs.discard(job)
                # Le righe mai avviate non possono più essere scaricate dopo che
                # la sessione è rimossa; renderlo esplicito invece di lasciarle
                # in coda con un handle ormai chiuso.
                for task in job.tasks.values():
                    if task.torrent_index not in job.selected_indices and task.state == "In coda":
                        task.state = "Annullato"
                        task.error = "Sessione torrent chiusa dopo i file selezionati."
                        discarded.append(task)
        if should_release:
            self._release_torrent_job(job)
            for task in discarded:
                self._notify(task)

    def resolve(self, task):
        with self._condition:
            if task.resolve_started or task.resolve_event.is_set():
                return
            task.resolve_started = True
            task.state = "Risoluzione link…"
        self._notify(task)
        threading.Thread(target=self._resolve_task, args=(task,),
                         name=f"resolver-{task.id}", daemon=True).start()

    def _resolve_task(self, task):
        try:
            mega_info = None
            if parse_mega_file_url(task.url):
                mega_info = resolve_mega_file(task.url)
                direct = mega_info["url"]
                session = make_http_session(direct)
                name = mega_info.get("name") or ""
                task.mega_info = mega_info
            else:
                direct, name, session = resolve_host_url(task.url, task.session)
            task.resolved_url = direct
            task.session = session
            if mega_info is not None:
                if mega_info.get("size"):
                    task.total = mega_info["size"]
                if name and not task.preferred_name:
                    task.name = sanitize_filename(name, task.name)
            elif name and not task.preferred_name:
                task.name = filename_from_headers(name, {})
            if task.state == "Risoluzione link…":
                task.state = "Connessione…" if task.running else "In coda"
        except Exception as exc:
            task.error = str(exc)
            task.state = "Errore"
            logging.exception("Risoluzione link fallita per %s", task.url)
        finally:
            task.resolve_event.set()
            self._notify(task)
            with self._condition:
                self._condition.notify_all()

    def start_queue(self):
        with self._condition:
            self._queue_enabled = True
            for task in self.tasks:
                if task.torrent_placeholder:
                    continue
                if task.torrent_job is not None and task.state == "In coda":
                    task.torrent_job.selected_indices.add(task.torrent_index)
                if task.state in ("Annullato", "Errore") and not task.running:
                    task.state = "In coda"
                    task.error = ""
                    task.downloaded, task.total, task.speed = 0, None, 0.0
                    task.cancel_event.clear()
                    task.pause_event.set()
                    if task.torrent_job is None:
                        task.resolved_url = None
                        task.session = None
                        task.resolve_started = False
                        task.resolve_event.clear()
            self._condition.notify_all()

    def _pause_torrent_if_all_inactive(self, job):
        if job is None or job.handle is None:
            return
        active = [job.tasks[i] for i in job.selected_indices
                  if i in job.tasks and job.tasks[i].running]
        if active and all(not item.pause_event.is_set() for item in active):
            try:
                flags = getattr(getattr(lt, "torrent_flags", None), "auto_managed", None)
                if flags is not None and hasattr(job.handle, "unset_flags"):
                    job.handle.unset_flags(flags)
                job.handle.pause()
            except Exception:
                logging.exception("Impossibile mettere in pausa il torrent")

    def _resume_torrent_task(self, task):
        job = task.torrent_job
        if job is None or job.handle is None:
            return
        try:
            flags = getattr(getattr(lt, "torrent_flags", None), "auto_managed", None)
            if flags is not None and hasattr(job.handle, "set_flags"):
                job.handle.set_flags(flags)
            job.handle.resume()
            self._set_torrent_file_priority(task, self._torrent_priority(task))
        except Exception:
            logging.exception("Impossibile riprendere il torrent")

    def set_limit(self, value):
        changed = []
        with self._condition:
            self.max_concurrent = max(1, int(value))
            active = [t for t in self.tasks if t.running and t.id in self._active]
            excess = max(0, len(active) - self.max_concurrent)
            # Riduci prima i trasferimenti avviati più recentemente.
            for task in sorted(active, key=lambda t: t.start_sequence, reverse=True)[:excess]:
                task.limit_paused = True
                task.manual_resume_pending = False
                task.pause_event.clear()
                task.state, task.speed = "In coda", 0.0
                self._active.discard(task.id)
                if task.torrent_job is not None:
                    self._set_torrent_file_priority(task, 0)
                    self._pause_torrent_if_all_inactive(task.torrent_job)
                changed.append(task)
            self._condition.notify_all()
        for task in changed:
            self._notify(task)

    def start_many(self, tasks):
        """Porta i task selezionati in testa alla coda rispettando il limite."""
        with self._condition:
            pending = [t for t in tasks
                       if not t.running and not t.torrent_placeholder
                       and t.state != "Completato"]
            if not pending:
                return
            queued_orders = [t.order for t in self.tasks
                             if not t.running and t.state == "In coda"
                             and t not in pending]
            base = min(queued_orders, default=self._next_order)
            for offset, task in enumerate(pending):
                if task.state in ("Annullato", "Errore"):
                    task.error = ""
                    task.downloaded, task.total, task.speed = 0, None, 0.0
                    task.cancel_event.clear()
                    if task.torrent_job is None:
                        task.resolved_url = None
                        task.session = None
                        task.resolve_started = False
                        task.resolve_event.clear()
                task.pause_event.set()
                task.state = "In coda"
                task.limit_paused = False
                task.manual_resume_pending = False
                task.order = base - len(pending) + offset
            self._queue_enabled = True
            self._condition.notify_all()
        for task in pending:
            self._notify(task)

    def set_priority(self, task, priority):
        with self._condition:
            task.priority = priority
            if task.torrent_job is not None and task.torrent_index is not None and task.running:
                self._set_torrent_file_priority(task, self._torrent_priority(task))
            self._condition.notify_all()
        self._notify(task)

    def start_now(self, task):
        with self._condition:
            if task.running:
                return False
            if task.state == "Errore" and task.torrent_job is None:
                task.resolved_url = None
                task.session = None
                task.resolve_started = False
                task.resolve_event.clear()
            task.cancel_event.clear()
            task.pause_event.set()
            task.state, task.error = "In avvio", ""
            task.downloaded, task.total, task.speed = 0, None, 0.0
            task.running = True
            task.bypass = True
            task.limit_paused = False
            task.manual_resume_pending = False
            task.start_sequence = self._next_start_sequence
            self._next_start_sequence += 1
            if task.torrent_job is not None:
                task.torrent_job.selected_indices.add(task.torrent_index)
            self._active.add(task.id)
            threading.Thread(target=self._download, args=(task,),
                             name=f"download-now-{task.id}", daemon=True).start()
        self._notify(task)
        return True

    def toggle_pause_group(self, tasks):
        """Mette in pausa o riprende tutti i task non terminali del gruppo."""
        tasks = [task for task in tasks
                 if task.state not in ("Completato", "Annullato", "Errore")]
        if not tasks:
            return
        should_pause = any(
            (task.running and task.pause_event.is_set()) or
            (not task.running and task.state == "In coda")
            for task in tasks
        )
        changed = []
        with self._condition:
            for task in tasks:
                if task.running:
                    continue
                if should_pause and task.state == "In coda":
                    task.pause_event.clear()
                    task.state, task.speed = "In pausa", 0.0
                    changed.append(task)
                elif not should_pause and task.state == "In pausa":
                    task.pause_event.set()
                    task.state = "In coda"
                    changed.append(task)
            self._condition.notify_all()
        for task in tasks:
            if task.running:
                if (should_pause and task.pause_event.is_set()) or (not should_pause and not task.pause_event.is_set()):
                    self.toggle_pause(task)
        for task in changed:
            self._notify(task)

    def toggle_pause(self, task):
        changed = False
        with self._condition:
            if not task.running or task.state in ("Completato", "Annullato", "Errore"):
                return
            if task.pause_event.is_set():
                task.pause_event.clear()
                task.limit_paused = False
                task.manual_resume_pending = False
                task.state, task.speed = "In pausa", 0.0
                self._active.discard(task.id)
                if task.torrent_job is not None:
                    self._set_torrent_file_priority(task, 0)
                    self._pause_torrent_if_all_inactive(task.torrent_job)
                changed = True
            elif task.limit_paused:
                # Un'azione utente trasforma la pausa automatica in pausa manuale.
                task.limit_paused = False
                task.manual_resume_pending = False
                task.state = "In pausa"
                changed = True
            elif task.id not in self._active:
                # La ripresa esplicita attende uno slot, non oltrepassa il limite.
                task.manual_resume_pending = True
                task.state = "In coda"
                changed = True
            else:
                task.pause_event.set()
                task.state = "In corso"
                if task.torrent_job is not None:
                    self._resume_torrent_task(task)
                changed = True
            self._condition.notify_all()
        if changed:
            self._notify(task)


    def cancel(self, task):
        jobs_to_release = []
        with self._condition:
            if task.torrent_placeholder and parse_magnet_uri(task.url):
                # Magnet metadata lookup has no torrent_job attached until the
                # worker has created its libtorrent handle. Signal the worker,
                # remove the placeholder immediately, and release any job that
                # has already been registered for this magnet.
                task.cancelled = True
                task.cancel_event.set()
                task.state, task.speed, task.error = "Annullato", 0.0, ""
                if task in self.tasks:
                    self.tasks.remove(task)
                self._magnet_pending.discard(task.url)
                jobs_to_release = [job for job in self._torrent_jobs
                                   if job.magnet_url == task.url and not job.tasks]
                for job in jobs_to_release:
                    job.selection_pending = False
                    self._torrent_jobs.discard(job)
                self._condition.notify_all()
                job = None
            else:
                if task.running:
                    task.cancel_event.set()
                    task.pause_event.set()
                    task.state, task.speed = "Annullato", 0.0
                    if task.torrent_job is not None:
                        task.torrent_job.selected_indices.add(task.torrent_index)
                elif task.state not in ("Completato", "Annullato"):
                    task.cancel_event.set()
                    task.state = "Annullato"
                    task.error = ""
                    if task.torrent_job is not None:
                        task.torrent_job.selected_indices.add(task.torrent_index)
                job = task.torrent_job
        self._notify(task)
        for pending_job in jobs_to_release:
            self._release_torrent_job(pending_job)
        if job is not None:
            threading.Thread(target=self._maybe_release_torrent_job, args=(job,),
                             name=f"torrent-cancel-cleanup-{task.id}", daemon=True).start()

    def remove(self, task):
        """Rimuove una riga; i worker attivi vengono annullati senza bloccare la GUI."""
        response = None
        job = None
        with self._condition:
            if task not in self.tasks:
                return False
            if task.torrent_placeholder:
                task.cancelled = True
                task.cancel_event.set()
                self._magnet_pending.discard(task.url)
            job = task.torrent_job
            if task.running or task.state not in ("Completato", "Annullato", "Errore"):
                task.cancel_event.set()
                task.pause_event.set()
                task.state, task.speed = "Annullato", 0.0
                response = task.response
            if job is not None and task.torrent_index is not None:
                job.removed_tasks.append(task)
                try:
                    self._set_torrent_file_priority(task, 0)
                except Exception:
                    pass
                job.selected_indices.discard(task.torrent_index)
                job.tasks.pop(task.torrent_index, None)
            self.tasks.remove(task)
            self._condition.notify_all()
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if job is not None:
            if task.torrent_index is not None and not job.tasks:
                threading.Thread(target=self._cleanup_last_removed_torrent_task,
                                 args=(task, job), name=f"torrent-remove-cleanup-{task.id}",
                                 daemon=True).start()
            else:
                threading.Thread(target=self._cleanup_removed_torrent_file,
                                 args=(task, job), name=f"torrent-file-remove-{task.id}",
                                 daemon=True).start()
        return True

    def _cleanup_removed_torrent_file(self, task, job):
        while task.running:
            time.sleep(0.05)
        if task.state == "Annullato" and task.path and os.path.isfile(task.path):
            try:
                os.chmod(task.path, os.stat(task.path).st_mode | stat.S_IWRITE)
                os.remove(task.path)
            except OSError:
                logging.exception("Impossibile rimuovere il parziale torrent %s", task.path)
        self._maybe_release_torrent_job(job)

    def _cleanup_last_removed_torrent_task(self, task, job):
        # Aspetta tutti i worker già rimossi dal gruppo, poi chiude la sessione
        # soltanto quando non resta alcun file selezionato nel torrent.
        while any(removed.running for removed in job.removed_tasks):
            time.sleep(0.05)
        job.tasks.update({removed.torrent_index: removed for removed in job.removed_tasks
                          if removed.torrent_index is not None})
        with self._condition:
            self._torrent_jobs.discard(job)
        self._release_torrent_job(job)

    def remove_all(self):
        """Annulla in modo asincrono ogni trasferimento e svuota la coda."""
        with self._condition:
            removed = list(self.tasks)
            jobs = set(self._torrent_jobs)
            jobs.update(t.torrent_job for t in removed if t.torrent_job is not None)
            for task in removed:
                if task.state != "Completato":
                    task.cancel_event.set()
                    task.pause_event.set()
                    task.state, task.speed = "Annullato", 0.0
                task.limit_paused = False
                task.manual_resume_pending = False
                if task.torrent_placeholder:
                    self._magnet_pending.discard(task.url)
            self.tasks.clear()
            self._active.clear()
            self._condition.notify_all()
        if removed or jobs:
            threading.Thread(target=self._cleanup_removed_all, args=(removed, jobs),
                             name="remove-all-cleanup", daemon=True).start()
        return removed

    def _cleanup_removed_all(self, tasks, jobs):
        # Chiudere le risposte HTTP sblocca iter_content; handle torrent/sessioni
        # vengono rilasciati solo dopo l'uscita dei worker.
        for task in tasks:
            response = task.response
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
        while any(task.running or task.metadata_running for task in tasks):
            time.sleep(0.05)
        for job in jobs:
            self._release_torrent_job(job)

    def _scheduler(self):
        while True:
            with self._condition:
                if self._closed:
                    return
                if len(self._active) < self.max_concurrent:
                    resumable = [t for t in self.tasks if t.running and
                                 not t.pause_event.is_set() and
                                 (t.limit_paused or t.manual_resume_pending)]
                    if resumable:
                        resumable.sort(key=lambda t: t.start_sequence)
                        task = resumable[0]
                        task.limit_paused = False
                        task.manual_resume_pending = False
                        task.pause_event.set()
                        task.state = "In corso"
                        self._active.add(task.id)
                        if task.torrent_job is not None:
                            self._resume_torrent_task(task)
                        self._notify(task)
                        continue
                    if self._queue_enabled:
                        waiting = [t for t in self.tasks if not t.running and t.state == "In coda"]
                        if waiting:
                            waiting.sort(key=lambda t: (PRIORITIES.get(t.priority, 1), t.order))
                            task = waiting[0]
                            task.running = True
                            task.bypass = False
                            task.limit_paused = False
                            task.manual_resume_pending = False
                            task.start_sequence = self._next_start_sequence
                            self._next_start_sequence += 1
                            if task.torrent_job is not None:
                                task.torrent_job.selected_indices.add(task.torrent_index)
                            self._active.add(task.id)
                            threading.Thread(target=self._download, args=(task,),
                                             name=f"download-{task.id}", daemon=True).start()
                            self._notify(task)
                            continue
                self._condition.wait(timeout=0.4)

    def _unique_path(self, name):
        with self._condition:
            os.makedirs(self.destination, exist_ok=True)
            name = sanitize_filename(name)
            stem, ext = os.path.splitext(name)
            stem = stem or name
            candidate = os.path.join(self.destination, name)
            index = 1
            while os.path.exists(candidate) or candidate in self._reserved:
                candidate = os.path.join(self.destination, f"{stem} ({index}){ext}")
                index += 1
            self._reserved.add(candidate)
            return candidate

    def _download(self, task):
        if task.torrent_job is not None:
            self._download_torrent(task)
            return
        final_path = part_path = response = None
        try:
            if not task.resolve_started:
                self.resolve(task)
            task.state = "Risoluzione link…" if not task.resolve_event.is_set() else "Connessione…"
            self._notify(task)
            while not task.resolve_event.wait(0.2):
                if task.cancel_event.is_set():
                    raise InterruptedError("Download annullato")
            if task.cancel_event.is_set():
                raise InterruptedError("Download annullato")
            if task.state == "Errore":
                raise LinkResolutionError(task.error or "Impossibile risolvere il link")
            resolved_url = task.resolved_url or task.url
            session = task.session or make_http_session(resolved_url)
            task.session = session
            candidate_path = task.path or os.path.join(self.destination, sanitize_filename(task.name))
            part_path = candidate_path + ".part"
            resume_bytes = 0
            if getattr(task, "mega_info", None) and os.path.isfile(part_path + ".decrypting"):
                # Decrittazione interrotta in precedenza: il .part non è affidabile.
                for leftover in (part_path, part_path + ".decrypting"):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
            if getattr(task, "persist_on_close", False) and os.path.isfile(part_path):
                resume_bytes = os.path.getsize(part_path)
            previous_total = task.total if resume_bytes else None
            overwrite_checked = False
            for attempt in range(2):
                headers = {"User-Agent": session.headers.get("User-Agent", BROWSER_UA)}
                if resolved_url != task.url:
                    headers["Referer"] = task.url
                if resume_bytes > 0:
                    headers["Range"] = f"bytes={resume_bytes}-"
                    validator = task.etag or task.last_modified
                    if validator:
                        headers["If-Range"] = validator
                if task.cancel_event.is_set():
                    raise InterruptedError("Download annullato")
                response, session = _request_with_fallback(
                    session, "GET", resolved_url, headers=headers, timeout=30, stream=True,
                    allow_redirects=True)
                task.session = session
                task.response = response
                if task.cancel_event.is_set():
                    raise InterruptedError("Download annullato")
                if _looks_like_cloudflare(response, probe_body=False):
                    response.close()
                    raise LinkResolutionError(CLOUDFLARE_HELP)
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                disposition_header = response.headers.get("Content-Disposition", "")
                disposition_name = filename_from_headers(resolved_url, response.headers) if disposition_header else None
                url_name = filename_from_headers(resolved_url, {})
                non_html_name = disposition_name or task.preferred_name or task.name or url_name
                filename_ext = os.path.splitext(non_html_name)[1].lower()
                is_html_filename = filename_ext in (".html", ".htm", ".xhtml")
                if content_type in ("text/html", "application/xhtml+xml") and not is_html_filename:
                    response.close()
                    raise LinkResolutionError("Il link punta a una pagina web, non a un file")
                if getattr(task, "mega_info", None) and task.mega_info.get("name"):
                    task.name = sanitize_filename(task.mega_info["name"], task.name)
                else:
                    task.name = disposition_name or task.preferred_name or task.name or url_name
                if not task.path and not task.overwrite_allowed:
                    candidate_path = os.path.join(self.destination, sanitize_filename(task.name))
                    part_path = candidate_path + ".part"
                if os.path.exists(candidate_path) and not resume_bytes and not overwrite_checked:
                    overwrite_checked = True
                    task.overwrite_path = candidate_path
                    task.overwrite_event.clear()
                    self.events.put(("overwrite_prompt", task.id, candidate_path))
                    while not task.overwrite_event.wait(0.2):
                        if task.cancel_event.is_set():
                            raise InterruptedError("Download annullato")
                    if not task.overwrite_allowed:
                        response.close()
                        task.state = "Annullato"
                        task.error = "Sovrascrittura rifiutata"
                        return
                try:
                    response_length = int(response.headers.get("Content-Length"))
                except (TypeError, ValueError):
                    response_length = None
                if resume_bytes:
                    content_range = response.headers.get("Content-Range", "")
                    resumed = (response.status_code == 206 and
                               content_range.startswith(f"bytes {resume_bytes}-"))
                    if not resumed:
                        # Server ignora Range: riscrive da zero per non corrompere il file.
                        resume_bytes = 0
                        task.total = response_length
                    else:
                        new_total = (response_length + resume_bytes
                                     if response_length is not None else None)
                        if (previous_total is not None and new_total is not None
                                and previous_total != new_total):
                            # Il file remoto è cambiato: il .part non è più valido.
                            response.close()
                            try:
                                os.remove(part_path)
                            except OSError:
                                pass
                            resume_bytes = 0
                            task.total = None
                            task.etag = ""
                            task.last_modified = ""
                            if attempt == 0:
                                continue
                            raise LinkResolutionError(
                                "Il file remoto è cambiato durante la ripresa.")
                        task.total = new_total
                else:
                    task.total = response_length
                break
            task.etag = response.headers.get("ETag", "") or ""
            task.last_modified = response.headers.get("Last-Modified", "") or ""
            task.downloaded = resume_bytes
            task.mime = content_type or "—"
            if task.overwrite_allowed and task.overwrite_path:
                final_path = task.overwrite_path
            elif resume_bytes:
                final_path = candidate_path
            else:
                final_path = self._unique_path(task.name)
            part_path = final_path + ".part"
            task.path = final_path
            task.state = "In corso"
            self._notify(task)
            started = time.monotonic()
            sample_time = started
            sample_bytes = 0
            with open(part_path, "ab" if resume_bytes else "wb") as output:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    while not task.pause_event.wait(timeout=0.2):
                        if task.cancel_event.is_set():
                            raise InterruptedError("Download annullato")
                    if task.cancel_event.is_set():
                        raise InterruptedError("Download annullato")
                    if not chunk:
                        continue
                    HTTP_DOWNLOAD_THROTTLE.acquire(len(chunk))
                    output.write(chunk)
                    task.downloaded += len(chunk)
                    sample_bytes += len(chunk)
                    now = time.monotonic()
                    if now - sample_time >= 0.5:
                        task.speed = sample_bytes / max(now - sample_time, 0.001)
                        sample_time, sample_bytes = now, 0
                    if now - task._last_ui >= 0.12:
                        task._last_ui = now
                        self._notify(task)
            response.close()
            if task.cancel_event.is_set():
                raise InterruptedError("Download annullato")
            if getattr(task, "mega_info", None):
                task.state = "In corso"
                self._notify(task)
                marker = part_path + ".decrypting"
                try:
                    with open(marker, "w", encoding="utf-8"):
                        pass
                    decrypt_mega_file(part_path, task.mega_info["key"])
                finally:
                    try:
                        os.remove(marker)
                    except OSError:
                        pass
            if not (task.overwrite_allowed and task.overwrite_path):
                with self._condition:
                    self._reserved.discard(final_path)
                    final_path = self._unique_path(os.path.basename(final_path))
            if task.overwrite_allowed and task.overwrite_path:
                os.replace(part_path, final_path)
            else:
                os.rename(part_path, final_path)
            task.path = final_path
            task.state = "Completato"
            task.speed = 0.0
            if task.total is None:
                task.total = task.downloaded
        except InterruptedError:
            task.state = "Annullato"
            task.speed = 0.0
            if part_path and os.path.exists(part_path) and not task.persist_on_close:
                try:
                    os.remove(part_path)
                except OSError:
                    pass
        except Exception as exc:
            if task.cancel_event.is_set():
                task.state, task.error = "Annullato", ""
            else:
                task.state = "Errore"
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (401, 403):
                    task.error = (
                        "Accesso negato dal sito (HTTP %d): serve un account attivo/VIP "
                        "oppure i cookie di sessione (menu «Cookie host protetti»)."
                        % status)
                else:
                    task.error = str(getattr(exc, "reason", exc))
            task.speed = 0.0
            if part_path and os.path.exists(part_path) and not task.persist_on_close:
                try:
                    os.remove(part_path)
                except OSError:
                    pass
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            task.response = None
            # Le sessioni curl_cffi/requests contengono pool e thread: chiuderle
            # evita accumuli di risorse quando si scaricano molte parti.
            session = getattr(task, "session", None)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
                task.session = None
            if final_path:
                with self._condition:
                    self._reserved.discard(final_path)
            with self._condition:
                task.running = False
                task.limit_paused = False
                task.manual_resume_pending = False
                task.bypass = False
                self._active.discard(task.id)
                self._condition.notify_all()
            self._notify(task)

    @staticmethod
    def _torrent_priority(task):
        return {"Alta": 7, "Normale": 4, "Bassa": 1}.get(task.priority, 4)

    @staticmethod
    def _set_torrent_file_priority(task, priority):
        try:
            job = task.torrent_job
            handle = job.handle if job is not None else None
            index = task.torrent_index
            if handle is None or index is None:
                return
            if hasattr(handle, "file_priority"):
                handle.file_priority(int(index), int(priority))
            else:
                values = list(handle.file_priorities())
                if 0 <= int(index) < len(values):
                    values[int(index)] = int(priority)
                    handle.prioritize_files(values)
        except Exception:
            logging.exception("Impossibile aggiornare priorità file torrent %s", task.name)

    def _download_torrent(self, task):
        job = task.torrent_job
        index = task.torrent_index
        if job is None or job._removed or job.handle is None:
            task.state, task.error = "Errore", "La sessione torrent è già stata chiusa."
            task.running = False
            self._notify(task)
            with self._condition:
                self._active.discard(task.id)
                self._condition.notify_all()
            return
        item = job.files_by_index.get(index)
        if item is None:
            item = job.files[index] if 0 <= index < len(job.files) else None
        if item is None:
            task.state, task.error = "Errore", "File torrent non valido."
            task.running = False
            self._notify(task)
            with self._condition:
                self._active.discard(task.id)
                self._condition.notify_all()
            return
        final_path = self._torrent_output_path(item)
        if final_path is None:
            task.state, task.error = "Errore", "Percorso torrent non sicuro."
            task.running = False
            self._notify(task)
            with self._condition:
                self._active.discard(task.id)
                self._condition.notify_all()
            self._maybe_release_torrent_job(job)
            return
        try:
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            # Chiede conferma solo per file realmente presenti prima della
            # selezione, con dimensione diversa e non già usati da libtorrent.
            preexisting_size = getattr(job, "preexisting", {}).get(index, 0)
            expected_size = int(item.get("size") or 0)
            if preexisting_size and preexisting_size != expected_size:
                try:
                    progress_values = job.handle.file_progress()
                    own_bytes = (int(progress_values[index])
                                 if index < len(progress_values) else 0)
                except Exception:
                    own_bytes = 0
                if own_bytes <= 0:
                    task.overwrite_path = final_path
                    task.overwrite_event.clear()
                    self.events.put(("overwrite_prompt", task.id, final_path))
                    while not task.overwrite_event.wait(0.2):
                        if task.cancel_event.is_set():
                            raise InterruptedError("Download annullato")
                    if not task.overwrite_allowed:
                        self._set_torrent_file_priority(task, 0)
                        job.selected_indices.discard(index)
                        task.state, task.error = "Annullato", "Sovrascrittura rifiutata"
                        return
            task.path = final_path
            task.downloaded = 0
            task.total = item["size"]
            task.state = "In corso" if task.pause_event.is_set() else (
                "In coda" if task.limit_paused else "In pausa")
            task.error = ""
            if task.pause_event.is_set():
                self._resume_torrent_task(task)
            else:
                self._set_torrent_file_priority(task, 0)
            self._notify(task)
            sample_at = time.monotonic()
            sample_bytes = 0
            last_notify = 0.0
            while not task.cancel_event.is_set():
                if not task.pause_event.is_set():
                    task.speed = 0.0
                    task.state = "In pausa" if not task.limit_paused else "In coda"
                    if task.cancel_event.wait(0.1): break
                    continue
                if task.state in ("In pausa", "In coda"):
                    task.state = "In corso"
                if task.cancel_event.wait(0.1): break
                try:
                    progress = job.handle.file_progress()
                    status = job.handle.status()
                    downloaded = int(progress[index]) if index < len(progress) else 0
                    task.downloaded = min(task.total or downloaded, downloaded)
                    task.peers = int(getattr(status, "num_peers", 0))
                    job.peer_count = task.peers
                    # Le rate libtorrent sono aggregate per torrent.
                    job.download_rate = max(0.0, float(getattr(status, "download_rate", 0) or 0))
                    job.upload_rate = max(0.0, float(getattr(status, "upload_rate", 0) or 0))
                    now = time.monotonic()
                    elapsed = now - sample_at
                    if elapsed >= 1:
                        task.speed = max(0, task.downloaded - sample_bytes) / elapsed
                        sample_at, sample_bytes = now, task.downloaded
                    if task.total == 0 or (task.total is not None and task.downloaded >= task.total and os.path.exists(final_path)):
                        task.downloaded = task.total or 0
                        task.state = "Completato"
                        task.speed = 0.0
                        break
                    if now - last_notify >= 0.2:
                        self._notify(task)
                        last_notify = now
                except Exception as exc:
                    raise RuntimeError(f"Errore aggiornando il torrent: {exc}") from exc
            if task.cancel_event.is_set():
                self._set_torrent_file_priority(task, 0)
                task.state, task.speed = "Annullato", 0.0
            elif task.state != "Completato":
                task.state = "Errore"
                task.error = "Il torrent si è arrestato prima del completamento del file."
        except InterruptedError:
            task.state, task.speed = "Annullato", 0.0
        except Exception as exc:
            task.state, task.error, task.speed = "Errore", str(exc), 0.0
            logging.exception("Download torrent fallito per %s", task.name)
        finally:
            with self._condition:
                task.running = False
                task.limit_paused = False
                task.manual_resume_pending = False
                self._active.discard(task.id)
                if not any(other is not task and other.running for other in job.tasks.values()):
                    job.download_rate = 0.0
                    job.upload_rate = 0.0
                self._condition.notify_all()
            self._notify(task)
            threading.Thread(target=self._maybe_release_torrent_job, args=(job,),
                             name=f"torrent-finish-cleanup-{task.id}", daemon=True).start()

    def head_info(self, task):
        def inspect():
            info = {}
            try:
                session = make_http_session(task.url)
                response = session.head(task.url, timeout=12, allow_redirects=True)
                response.raise_for_status()
                try:
                    info["size"] = int(response.headers.get("Content-Length"))
                except (TypeError, ValueError):
                    info["size"] = None
                content_type = response.headers.get("Content-Type", "")
                info["mime"] = content_type.split(";", 1)[0] if content_type else "—"
                info["name"] = filename_from_headers(task.url, response.headers)
                response.close()
            except Exception as exc:
                info["error"] = str(getattr(exc, "reason", exc))
            self.events.put(("head", task.id, info))
        threading.Thread(target=inspect, name=f"head-{task.id}", daemon=True).start()

    def close(self):
        with self._condition:
            self._closed = True
            for task in self.tasks:
                if task.running:
                    task.cancel_event.set()
                    task.pause_event.set()
                elif task.torrent_job is not None and task.state not in ("Completato", "Annullato", "Errore"):
                    task.cancel_event.set()
            jobs = set(self._torrent_jobs)
            jobs.update(t.torrent_job for t in self.tasks if t.torrent_job is not None)
            self._condition.notify_all()
        # Rimuovere ogni sessione anche se alcuni worker sono ancora in uscita:
        # così libtorrent chiude subito i file prima che la GUI termini.
        with self._torrent_session_lock:
            self._torrent_session = None
        for job in jobs:
            threading.Thread(target=self._release_torrent_job, args=(job,),
                             name="torrent-close-cleanup", daemon=True).start()


def applica_titlebar_scura(finestra, colore):
    """Applica i colori scuri alla cornice DWM quando disponibile (Windows)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        def colore_ref(value):
            value = value.lstrip("#")
            rosso, verde, blu = (int(value[i:i + 2], 16) for i in (0, 2, 4))
            return (blu << 16) | (verde << 8) | rosso

        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_int
        dwmapi.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

        child_hwnd = ctypes.c_void_p(finestra.winfo_id())
        hwnd = user32.GetAncestor(child_hwnd, 2)  # GA_ROOT
        if not hwnd:
            hwnd = user32.GetParent(child_hwnd)
        if not hwnd:
            hwnd = child_hwnd

        def set_attribute(attribute, value):
            data = ctypes.c_int(value)  # DWORD/COLORREF a 32 bit
            try:
                dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)
                )
            except Exception:
                # Un attributo può non essere supportato dalla versione di Windows.
                pass

        set_attribute(20, ctypes.c_int(1).value)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        set_attribute(19, ctypes.c_int(1).value)  # compatibilità con versioni precedenti
        set_attribute(35, colore_ref(colore))  # DWMWA_CAPTION_COLOR (COLORREF)
        set_attribute(34, colore_ref(colore))  # DWMWA_BORDER_COLOR (COLORREF)
        set_attribute(36, colore_ref("#FFFFFF"))  # DWMWA_TEXT_COLOR (COLORREF)

        # Forza la ricalcolazione della cornice dopo aver aggiornato gli attributi DWM.
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                            0x0002 | 0x0001 | 0x0004 | 0x0020)
    except Exception:
        # DWM non è disponibile su tutte le versioni/configurazioni Windows.
        pass


class DownloaderApp:
    """Finestra principale e integrazione thread-safe con DownloadManager."""
    BG = "#000000"
    PANEL = "#0d0d0d"
    ROW = "#1a1a1a"
    LINE = "#292929"
    TEXT = "#e6e6e6"
    MUTED = "#a8a8a8"
    ACCENT = "#e10600"
    ACCENT_HOVER = "#ff1a1a"
    SELECT_BG = "#d4d4d4"
    SELECT_FG = "#101010"

    def __init__(self, root):
        _load_tkinter()
        self.root = root
        _choose_app_font(root)
        _apply_window_icon(root)
        self.language_choice = load_language_config()
        self.language = detect_system_language() if self.language_choice == "system" else self.language_choice
        root.title("DOWNLOADVERSE")
        root.geometry("1120x700")
        root.minsize(820, 520)
        root.configure(bg=self.BG)
        self.events = queue.Queue()
        default_destination = os.path.join(os.path.expanduser("~"), "Downloads")
        saved_destination = _read_app_config().get("download_directory", default_destination)
        if not isinstance(saved_destination, str) or not saved_destination.strip():
            saved_destination = default_destination
        self.destination = tk.StringVar(value=os.path.abspath(os.path.expanduser(saved_destination)))
        self.url_var = tk.StringVar()
        self.max_concurrent = load_max_concurrent_config()
        self.close_action = load_close_action()
        self.limit_var = tk.IntVar(value=self.max_concurrent)
        self.tasks_by_id = {}
        self.rows = {}
        self.import_groups = {}
        self._import_group_counter = 0
        self.info_windows = {}
        self._github_pending = set()
        self._github_cache = {}
        self._github_input_urls = {}
        self._github_dialogs = {}
        self._libtorrent_install_lock = threading.Lock()
        self._libtorrent_install_active = False
        self._libtorrent_prompt_scheduled = False
        self._libtorrent_waiting = {}
        self._page_pending = set()
        self._page_scanned = set()
        self._cancelled_torrent_selections = set()
        self._torrent_api_checks = {}
        self._torrent_api_checking = set()
        # Sopprime solo i reinserimenti automatici ripetuti dello stesso clipboard.
        self._suppressed_clipboard_magnet = None
        self._auto_paste_last_value = None
        self._clipboard_paste_pending = False
        self._auto_import_running = False
        self._auto_import_url = ""
        self._url_placeholder_active = False
        self._drop_hwnd = None
        self._drop_wndproc = None
        self._drop_old_wndproc = None
        self._update_check_running = False
        self.manager = DownloadManager(self.events, self.destination.get(), self.max_concurrent)
        self._tray_icon = None
        self._tray_thread = None
        self._closing = False
        self._restoring_torrent_groups = {}
        self._restore_pending_states = []
        limits = load_download_limits_config()
        self.manager.set_download_limits(limits["download_rate_limit"], limits["upload_rate_limit"], limits["connections_limit"])
        self._style()
        self._build()
        self._apply_language()
        _center_window(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._applica_titlebar_finestra(self.root)
        self.root.after(250, self._enable_torrent_drop)
        self.root.after(120, self._poll)
        self._restore_session()
        # Controllo aggiornamenti silenzioso all'avvio: avvisa solo se esiste
        # davvero una nuova versione.
        self.root.after(3000, lambda: self._check_for_updates(silent=True))

    def _restore_session(self):
        """Ripristina una sessione precedente senza duplicare elementi."""
        try:
            with open(SESSION_PATH, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            logging.exception("Impossibile leggere la sessione salvata")
            return
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return
        self._restore_pending_states = []
        saved_destination = data.get("destination")
        if isinstance(saved_destination, str) and saved_destination.strip():
            saved_destination = os.path.abspath(os.path.expanduser(saved_destination))
            self.destination.set(saved_destination)
            self.manager.destination = saved_destination
            try:
                _write_app_config({"download_directory": saved_destination})
            except OSError:
                logging.exception("Impossibile ripristinare la cartella di download")
        torrent_restore = {}
        for saved in data["items"]:
            if isinstance(saved, dict) and saved.get("torrent_index") is not None:
                group = torrent_restore.setdefault(saved.get("url"), {"indices": [], "children": {}})
                try:
                    index = int(saved["torrent_index"])
                    group["indices"].append(index)
                    group["children"][index] = saved
                except (TypeError, ValueError):
                    pass
        saved_import_groups = data.get("import_groups")
        if not isinstance(saved_import_groups, dict):
            saved_import_groups = {}
        for group_id in saved_import_groups:
            if isinstance(group_id, str):
                match = re.fullmatch(r"import-group-(\d+)", group_id)
                if match:
                    self._import_group_counter = max(
                        self._import_group_counter, int(match.group(1)))
        restored_magnets = {}
        for item in data["items"]:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            self._restore_pending_states.append(item)
            if item.get("torrent_index") is not None:
                url = item["url"]
                if url not in restored_magnets:
                    group = torrent_restore.get(url, {"indices": [], "children": {}})
                    try:
                        if parse_magnet_uri(url):
                            placeholder = self.manager.add_magnet(url, group["indices"], group["children"])
                        else:
                            placeholder = self.manager.add_torrent_file(url, group["indices"], group["children"])
                    except Exception:
                        logging.exception("Impossibile ripristinare il torrent %s", url)
                        continue
                    restored_magnets[url] = placeholder
                    self.tasks_by_id[placeholder.id] = placeholder
                    self._render(placeholder)
                continue
            url = str(item["url"])
            if item.get("torrent_placeholder") and url.lower().endswith(".torrent"):
                try:
                    task = self.manager.add_torrent_file(url)
                except Exception:
                    logging.exception("Impossibile ripristinare file torrent %s", url)
                    continue
            elif item.get("torrent_placeholder") and parse_magnet_uri(url):
                try:
                    task = self.manager.add_magnet(url)
                except Exception:
                    logging.exception("Impossibile ripristinare magnet")
                    continue
            else:
                try:
                    task = self.manager.add(url, filename=item.get("preferred_name") or item.get("name"))
                except Exception:
                    logging.exception("Impossibile ripristinare download %s", url)
                    continue
            self._apply_restored_task(task, item)
            group_id = item.get("import_group")
            if isinstance(group_id, str) and group_id:
                info = saved_import_groups.get(group_id)
                if not isinstance(info, dict):
                    info = {}
                if group_id not in self.import_groups:
                    self._ensure_import_group(
                        group_id, str(info.get("name") or ""),
                        bool(info.get("open")))
                task.import_group_iid = group_id
            self.tasks_by_id[task.id] = task
            self._render(task)
        self._migrate_import_groups()
        # Gli elementi il cui torrent è in metadata lookup mantengono la selezione per file.
        if any(bool(i.get("was_running")) and i.get("torrent_index") is None
               and not i.get("torrent_placeholder") for i in self._restore_pending_states):
            self.manager.start_queue()

    @staticmethod
    def _apply_restored_task(task, item):
        task.name = str(item.get("name") or task.name)
        task.preferred_name = item.get("preferred_name") or task.preferred_name
        task.path = str(item.get("path") or "")
        task.total = item.get("total") if isinstance(item.get("total"), (int, float)) else None
        try:
            task.downloaded = max(0, int(item.get("downloaded", 0) or 0))
        except (TypeError, ValueError):
            task.downloaded = 0
        task.priority = item.get("priority") if item.get("priority") in PRIORITIES else "Normale"
        task.etag = str(item.get("etag") or "")
        task.last_modified = str(item.get("last_modified") or "")
        task.persist_on_close = True
        state = str(item.get("state") or "In coda")
        if state not in ("In pausa", "In coda", "In corso", "Completato", "Errore", "Annullato"):
            state = "In coda"
        task._restore_was_running = bool(item.get("was_running"))
        task.state = ("In coda" if task._restore_was_running and state == "In corso"
                      else ("In pausa" if state == "In corso" else state))
        task.download_dir = str(item.get("folder") or (os.path.dirname(task.path) if task.path else ""))
        task._restore_downloaded = task.downloaded
        if state == "Completato":
            task.state = "Completato"
            task.downloaded = task.total if task.total is not None else task.downloaded
        elif state == "Errore":
            task.error = str(item.get("error") or "")

    def _restore_torrent_children(self, children, parent=None):
        should_start = False
        for task in children:
            placeholder = None
            if parent is not None and hasattr(parent, "_restore_torrent_children"):
                placeholder = parent._restore_torrent_children.get(task.torrent_index)
            if placeholder:
                self._apply_restored_task(task, placeholder)
                task.running = False
                task.pause_event.set()
                if task.state == "Completato":
                    task.downloaded = task.total if task.total is not None else task.downloaded
                elif task._restore_was_running:
                    task.state = "In coda"
                    should_start = True
                elif task.state == "In pausa":
                    task.pause_event.clear()
                self.tasks_by_id[task.id] = task
                self._render(task)
        if should_start:
            self.root.after(200, self.manager.start_queue)

    def _save_session(self):
        items = []
        for task in list(self.manager.tasks):
            state = task.state
            running = bool(task.running and state != "In pausa")
            task.persist_on_close = True
            if running and state not in ("Completato", "Errore", "Annullato"):
                state = "In corso"
            if task.torrent_job is not None:
                selected = sorted(task.torrent_job.selected_indices)
                index = task.torrent_index
                torrent_selected = True
            else:
                selected = []
                index = None
                torrent_selected = bool(task.torrent_placeholder and parse_magnet_uri(task.url))
            items.append({
                "url": task.url, "name": task.name, "preferred_name": task.preferred_name,
                "path": task.path, "folder": (os.path.dirname(task.path) if task.path else self.destination.get()),
                "total": task.total, "downloaded": task.downloaded,
                "progress": task.percent, "state": state, "error": task.error,
                "priority": task.priority, "was_running": running,
                "etag": task.etag, "last_modified": task.last_modified,
                "torrent_placeholder": bool(task.torrent_placeholder),
                "torrent_index": index, "torrent_selected": torrent_selected,
                "selected_indices": selected,
                "import_group": getattr(task, "import_group_iid", None),
            })
        import_groups = {}
        for iid, group in self.import_groups.items():
            if not self.tree.exists(iid):
                continue
            try:
                is_open = bool(int(self.tree.item(iid, "open") or 0))
            except (tk.TclError, TypeError, ValueError):
                is_open = False
            import_groups[iid] = {"name": group.name, "open": is_open}
        payload = {"version": 1, "destination": self.destination.get(),
                   "items": items, "import_groups": import_groups}
        try:
            os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
            temp = SESSION_PATH + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, SESSION_PATH)
        except OSError:
            logging.exception("Impossibile salvare la sessione")

    def _ask_exit_action(self):
        """Return reduce, close, or None from the centered exit dialog."""
        window = tk.Toplevel(self.root)
        window.title(translate_ui("Uscita", self.language))
        window.configure(bg=self.PANEL)
        window.transient(self.root)
        result = {"choice": None}

        body = tk.Frame(window, bg=self.PANEL, padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=translate_ui("Chiudere completamente o ridurre a icona?",
                                         self.language),
                 bg=self.PANEL, fg=self.TEXT,
                 font=(APP_FONT_FAMILY, 10)).pack(pady=(0, 12))
        remember_var = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text=translate_ui("Non chiedermelo più", self.language),
                       variable=remember_var, bg=self.PANEL, fg=self.TEXT,
                       selectcolor=self.BG, activebackground=self.PANEL,
                       activeforeground=self.TEXT,
                       font=(APP_FONT_FAMILY, 9)).pack(pady=(0, 14))
        buttons = tk.Frame(body, bg=self.PANEL)
        buttons.pack(fill="x")

        def choose(value):
            if value in ("reduce", "close") and remember_var.get():
                self.close_action = value
                try:
                    save_close_action(value)
                except OSError:
                    logging.exception("Impossibile salvare la scelta di chiusura")
            result["choice"] = value
            window.destroy()

        ttk.Button(buttons, text=translate_ui("Riduci", self.language),
                   command=lambda: choose("reduce")).pack(side="left")
        ttk.Button(buttons, text=translate_ui("Chiudi", self.language),
                   style="Accent.TButton",
                   command=lambda: choose("close")).pack(side="left", padx=8)
        ttk.Button(buttons, text=translate_ui("Annulla", self.language),
                   command=lambda: choose(None)).pack(side="right")
        window.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        _center_window(window, self.root)
        window.grab_set()
        self.root.wait_window(window)
        return result["choice"]

    def _close(self):
        if self._closing:
            return
        if self.close_action == "close":
            self._close_completely()
            return
        if self.close_action == "reduce":
            self._minimize_to_tray()
            return
        choice = self._ask_exit_action()
        if choice is None:
            return
        if choice == "reduce":
            self._minimize_to_tray()
            return
        self._close_completely()

    def _minimize_to_tray(self):
        global pystray, Image, ImageDraw
        pystray = _load_optional("pystray")
        Image = _load_optional("PIL.Image")
        ImageDraw = _load_optional("PIL.ImageDraw")
        try:
            if pystray is None or Image is None or ImageDraw is None:
                raise RuntimeError("pystray/Pillow non disponibili")
            try:
                image = Image.open(_app_resource_path("downloadverse.png")).convert("RGBA")
                image = image.resize((64, 64), Image.Resampling.LANCZOS)
            except Exception:
                image = Image.new("RGBA", (64, 64), (10, 14, 35, 255))
                draw = ImageDraw.Draw(image)
                draw.polygon([(32, 54), (10, 30), (24, 30), (24, 10),
                              (40, 10), (40, 30), (54, 30)],
                             fill=(110, 225, 255, 255))
            menu = pystray.Menu(
                pystray.MenuItem("Ripristina finestra", lambda icon, item: self.root.after(0, self._restore_from_tray), default=True),
                pystray.MenuItem("Esci", lambda icon, item: self.root.after(0, self._exit_from_tray)),
            )
            self._tray_icon = pystray.Icon("DOWNLOADVERSE", image, "DOWNLOADVERSE", menu)
            self.root.withdraw()
            self._tray_thread = threading.Thread(target=self._tray_icon.run, name="system-tray", daemon=True)
            self._tray_thread.start()
        except Exception:
            logging.exception("Impossibile avviare l'icona system tray")
            self.root.deiconify()
            messagebox.showwarning("System tray non disponibile", "Non è stato possibile avviare la system tray. La finestra è stata ripristinata.", parent=self.root)

    def _restore_from_tray(self):
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
        except Exception:
            pass
        self._tray_icon = None
        self.root.deiconify()
        self.root.lift()

    def _exit_from_tray(self):
        self._closing = False
        self._close_completely()

    def _close_completely(self):
        if self._closing:
            return
        self._closing = True
        self._save_session()
        self.manager.close()
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
        except Exception:
            pass
        self._disable_torrent_drop()
        self.root.destroy()

    def _applica_titlebar_finestra(self, finestra):
        try:
            finestra.update_idletasks()
            applica_titlebar_scura(finestra, self.BG)
            finestra.after(300, lambda w=finestra: applica_titlebar_scura(w, self.BG))
            if not getattr(finestra, "_gdownloader_titlebar_map_bound", False):
                finestra.bind(
                    "<Map>",
                    lambda _event, w=finestra: applica_titlebar_scura(w, self.BG),
                    add="+",
                )
                finestra._gdownloader_titlebar_map_bound = True
            finestra.after(50, lambda w=finestra: self._riapplica_titlebar(w))
        except Exception:
            pass

    def _riapplica_titlebar(self, finestra):
        try:
            if finestra.winfo_exists():
                applica_titlebar_scura(finestra, self.BG)
        except Exception:
            pass

    def _make_group_toggle_image(self, expanded):
        """Piccola icona quadrata +/− per espandere i gruppi importati."""
        size = 16
        image = tk.PhotoImage(width=size, height=size)
        image.put("#1a1a1a", to=(0, 0, size, size))
        border = "#3a3a3a"
        image.put(border, to=(0, 0, size, 1))
        image.put(border, to=(0, size - 1, size, size))
        image.put(border, to=(0, 0, 1, size))
        image.put(border, to=(size - 1, 0, size, size))
        sign = self.ACCENT
        image.put(sign, to=(3, 7, size - 3, 9))
        if not expanded:
            image.put(sign, to=(7, 3, 9, size - 3))
        return image

    def _group_image(self, is_open):
        return getattr(self, "_group_open_image" if is_open
                       else "_group_closed_image", "")

    def _add_dropped_paths(self, paths):
        added = 0
        for path in paths:
            if (isinstance(path, str) and path.lower().endswith(".torrent")
                    and os.path.isfile(path)):
                if self._add_torrent_file(path):
                    added += 1
        return added

    def _enable_torrent_drop(self):
        """Abilita il trascinamento di file .torrent sulla lista download."""
        if os.name != "nt" or getattr(self, "_drop_hwnd", None):
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
            if not hwnd:
                hwnd = self.root.winfo_id()
            shell32.DragAcceptFiles.argtypes = [ctypes.c_void_p, wintypes.BOOL]
            shell32.DragAcceptFiles.restype = None
            shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                               ctypes.c_wchar_p, ctypes.c_uint]
            shell32.DragQueryFileW.restype = ctypes.c_uint
            shell32.DragQueryPoint.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(wintypes.POINT)]
            shell32.DragQueryPoint.restype = wintypes.BOOL
            shell32.DragFinish.argtypes = [ctypes.c_void_p]
            shell32.DragFinish.restype = None
            user32.ClientToScreen.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.POINT)]
            user32.ClientToScreen.restype = wintypes.BOOL
            user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                 ctypes.c_void_p]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_uint, ctypes.c_size_t,
                                               ctypes.c_ssize_t]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            self._drop_hwnd = int(hwnd)
            self._drop_user32 = user32
            self._drop_shell32 = shell32
            self._drop_old_wndproc = user32.GetWindowLongPtrW(hwnd, -4)
            wndproc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_size_t, ctypes.c_ssize_t)

            def wndproc(hwnd, msg, wparam, lparam):
                if msg == 0x0233:  # WM_DROPFILES
                    try:
                        self._handle_torrent_drop(wparam)
                    except Exception:
                        logging.exception("Errore durante il drag&drop torrent")
                    return 0
                return user32.CallWindowProcW(self._drop_old_wndproc, hwnd, msg,
                                              wparam, lparam)

            self._drop_wndproc = wndproc_type(wndproc)
            shell32.DragAcceptFiles(hwnd, True)
            user32.SetWindowLongPtrW(
                hwnd, -4, ctypes.cast(self._drop_wndproc, ctypes.c_void_p))
            logging.info("Drag&drop torrent abilitato")
        except Exception:
            logging.exception("Impossibile abilitare il drag&drop torrent")

    def _disable_torrent_drop(self):
        if os.name != "nt" or not getattr(self, "_drop_hwnd", None):
            return
        try:
            import ctypes
            self._drop_user32.SetWindowLongPtrW(
                ctypes.c_void_p(self._drop_hwnd), -4, self._drop_old_wndproc)
            self._drop_shell32.DragAcceptFiles(
                ctypes.c_void_p(self._drop_hwnd), False)
        except Exception:
            pass
        self._drop_hwnd = None
        self._drop_wndproc = None

    def _handle_torrent_drop(self, hdrop):
        try:
            import ctypes
            from ctypes import wintypes
            shell32 = self._drop_shell32
            user32 = self._drop_user32
            point = wintypes.POINT()
            shell32.DragQueryPoint(ctypes.c_void_p(hdrop), ctypes.byref(point))
            user32.ClientToScreen(ctypes.c_void_p(self._drop_hwnd),
                                  ctypes.byref(point))
            tree_x = self.tree.winfo_rootx()
            tree_y = self.tree.winfo_rooty()
            right = tree_x + self.tree.winfo_width()
            bottom = tree_y + self.tree.winfo_height()
            scrollbar = getattr(self, "scrollbar", None)
            if scrollbar is not None:
                right = max(right, scrollbar.winfo_rootx()
                            + scrollbar.winfo_width())
            if not (tree_x <= point.x <= right and tree_y <= point.y <= bottom):
                return
            count = shell32.DragQueryFileW(
                ctypes.c_void_p(hdrop), 0xFFFFFFFF, None, 0)
            paths = []
            for index in range(count):
                length = shell32.DragQueryFileW(
                    ctypes.c_void_p(hdrop), index, None, 0)
                buffer = ctypes.create_unicode_buffer(length + 1)
                shell32.DragQueryFileW(ctypes.c_void_p(hdrop), index, buffer,
                                       length + 1)
                if buffer.value:
                    paths.append(buffer.value)
            self._add_dropped_paths(paths)
        finally:
            try:
                self._drop_shell32.DragFinish(ctypes.c_void_p(hdrop))
            except Exception:
                pass

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=self.BG, foreground=self.TEXT,
                        fieldbackground=self.ROW, bordercolor=self.LINE,
                        troughcolor=self.ROW, font=(APP_FONT_FAMILY, 10))
        style.configure("Treeview", background=self.ROW, fieldbackground=self.ROW,
                        foreground=self.TEXT, rowheight=35, borderwidth=0, relief="flat",
                        font=(APP_FONT_FAMILY, 10))
        style.configure("Treeview.Heading", background="#161616", foreground="#d0d0d0",
                        font=(APP_FONT_FAMILY, 9, "bold"), relief="flat", padding=(9, 10))
        style.map("Treeview.Heading",
                  background=[("pressed", "#303030"), ("active", "#222222"),
                              ("!disabled", "#161616")],
                  foreground=[("pressed", "#ffffff"), ("active", "#ffffff"),
                              ("!disabled", "#d0d0d0")])
        style.map("Treeview", background=[("selected", self.SELECT_BG)],
                  foreground=[("selected", self.SELECT_FG)])
        # Layout senza il triangolino nativo: il gruppo usa una sola icona
        # + / − personalizzata, coerente con il resto dell'interfaccia.
        try:
            style.layout("NoArrow.Treeview", style.layout("Treeview"))
            item_layout = style.layout("Treeview.Item")
            no_arrow_layout = []
            for element, options in item_layout:
                if element == "Treeitem.padding":
                    options = dict(options)
                    options["children"] = [
                        child for child in options.get("children", [])
                        if child[0] != "Treeitem.indicator"]
                no_arrow_layout.append((element, options))
            style.layout("NoArrow.Treeview.Item", no_arrow_layout)
            style.configure("NoArrow.Treeview", background=self.ROW,
                            fieldbackground=self.ROW, foreground=self.TEXT,
                            rowheight=35, borderwidth=0, relief="flat",
                            font=(APP_FONT_FAMILY, 10))
            style.map("NoArrow.Treeview",
                      background=[("selected", self.SELECT_BG)],
                      foreground=[("selected", self.SELECT_FG)])
            style.configure("NoArrow.Treeview.Heading", background="#161616",
                            foreground="#d0d0d0", font=(APP_FONT_FAMILY, 9, "bold"),
                            relief="flat", padding=(9, 10))
            style.map("NoArrow.Treeview.Heading",
                      background=[("pressed", "#303030"), ("active", "#222222"),
                                  ("!disabled", "#161616")],
                      foreground=[("pressed", "#ffffff"), ("active", "#ffffff"),
                                  ("!disabled", "#d0d0d0")])
        except tk.TclError:
            logging.exception("Impossibile creare lo stile Treeview senza frecce")
        style.configure("TButton", background="#242424", foreground=self.TEXT,
                        font=(APP_FONT_FAMILY, 10, "bold"), padding=(12, 9),
                        borderwidth=0, focusthickness=0)
        style.map("TButton", background=[("active", "#333333"), ("pressed", "#111111")],
                  foreground=[("disabled", "#777777")])
        style.configure("Accent.TButton", background=self.ACCENT, foreground="#ffffff",
                        font=(APP_FONT_FAMILY, 10, "bold"), padding=(12, 9), borderwidth=0)
        style.map("Accent.TButton", background=[("disabled", "#3a1a1a"),
                                                 ("active", self.ACCENT_HOVER),
                                                 ("pressed", "#a90000")],
                  foreground=[("disabled", "#777777"), ("active", "#ffffff")])
        style.configure("TEntry", padding=10, fieldbackground="#171717", foreground=self.TEXT,
                        insertcolor="#ffffff", bordercolor="#383838", lightcolor="#383838",
                        darkcolor="#383838")
        style.map("TEntry", fieldbackground=[("focus", "#202020")],
                  bordercolor=[("focus", self.ACCENT)])
        style.configure("Placeholder.TEntry", padding=10, fieldbackground="#171717",
                        foreground=self.MUTED, insertcolor="#ffffff",
                        bordercolor="#383838", lightcolor="#383838", darkcolor="#383838")
        style.map("Placeholder.TEntry", fieldbackground=[("focus", "#202020")],
                  bordercolor=[("focus", self.ACCENT)])
        style.configure("TSpinbox", padding=6, fieldbackground="#171717", foreground=self.TEXT,
                        arrowcolor=self.TEXT, bordercolor="#383838", insertcolor="#ffffff")
        style.map("TSpinbox", fieldbackground=[("focus", "#202020")],
                  bordercolor=[("focus", self.ACCENT)], arrowcolor=[("active", self.ACCENT_HOVER)])
        # Controls in Settings/Download share the same dark field, red outline,
        # typography and compact height, independently of the app-wide styles.
        settings_field = {
            "padding": (8, 6), "fieldbackground": self.ROW,
            "foreground": "#ffffff", "font": (APP_FONT_FAMILY, 10),
            "bordercolor": self.ACCENT, "lightcolor": self.ACCENT,
            "darkcolor": self.ACCENT, "borderwidth": 1, "relief": "solid",
            "insertcolor": "#ffffff",
        }
        style.configure("Settings.TEntry", **settings_field)
        style.map("Settings.TEntry", fieldbackground=[("focus", "#202020")],
                  bordercolor=[("focus", self.ACCENT)])
        style.configure("Settings.TSpinbox", **settings_field, arrowcolor="#ffffff")
        style.map("Settings.TSpinbox", fieldbackground=[("focus", "#202020")],
                  bordercolor=[("focus", self.ACCENT)], arrowcolor=[("active", self.ACCENT_HOVER)])
        style.configure("Settings.TCombobox", **settings_field, background=self.ROW,
                        arrowcolor="#ffffff")
        style.map("Settings.TCombobox", fieldbackground=[("readonly", self.ROW), ("focus", "#202020")],
                  foreground=[("readonly", "#ffffff")], background=[("active", "#242424")],
                  bordercolor=[("focus", self.ACCENT)])
        style.configure("TScrollbar", background="#3a3a3a", troughcolor="#0a0a0a",
                        bordercolor="#0a0a0a", arrowcolor="#ffffff", width=14,
                        lightcolor="#3a3a3a", darkcolor="#3a3a3a")
        style.map("TScrollbar", background=[("active", "#5a5a5a")])
        style.configure("Horizontal.TProgressbar", background=self.ACCENT,
                        troughcolor="#292929", bordercolor="#292929", lightcolor=self.ACCENT,
                        darkcolor=self.ACCENT)
        # Set defaults for Tk's native dialogs where Tk honors option database colors.
        self.root.option_add("*font", (APP_FONT_FAMILY, 10))
        self.root.option_add("*Dialog*font", (APP_FONT_FAMILY, 10))
        self.root.option_add("*Menu.font", (APP_FONT_FAMILY, 13))
        self.root.option_add("*Dialog.msg.background", self.PANEL)
        self.root.option_add("*Dialog.msg.foreground", self.TEXT)
        self.root.option_add("*Dialog.background", self.PANEL)
        self.root.option_add("*Dialog.foreground", self.TEXT)
        self.root.option_add("*Menu.background", self.PANEL)
        self.root.option_add("*Menu.foreground", self.TEXT)
        self.root.option_add("*Menu.activeBackground", self.ACCENT)
        self.root.option_add("*Menu.activeForeground", "#ffffff")

    def _build(self):
        header = tk.Frame(self.root, bg=self.BG, padx=26, pady=18)
        header.pack(fill="x")
        self.brand_label = tk.Label(header, text="DOWNLOADVERSE", bg=self.BG, fg="#ffffff",
                 font=(APP_FONT_FAMILY, 22, "bold")); self.brand_label.pack(side="left")
        self.side_menu_toggle = ttk.Button(header, text="☰", command=self._toggle_side_menu)
        self.side_menu_toggle.pack(side="right", padx=(8, 0))
        # Collapsible edge drawer: settings and the existing utility actions are
        # grouped here without removing their original toolbar functionality.
        self.side_menu = tk.Frame(self.root, bg=self.PANEL, padx=8, pady=8,
                                  highlightbackground=self.LINE, highlightthickness=1)
        self.side_menu_items = []
        for label, action in (("Impostazioni", self._open_settings),
                              ("Cookie host protetti", self._open_cookie_dialog)):
            button = ttk.Button(self.side_menu, text=translate_ui(label, self.language),
                                command=lambda callback=action: self._run_side_menu_action(callback))
            button.pack(fill="x", pady=2)
            self.side_menu_items.append((button, label))
        self.side_menu.place_forget()

        panel = tk.Frame(self.root, bg=self.PANEL, padx=18, pady=16,
                         highlightthickness=0)
        panel.pack(fill="x", padx=26, pady=(0, 14))
        tk.Label(panel, text="NUOVO DOWNLOAD", bg=self.PANEL, fg=self.ACCENT,
                 font=(APP_FONT_FAMILY, 9, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 9))
        self.url_entry = ttk.Entry(panel, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.url_entry.bind("<Return>", lambda _e: self._add_from_button())
        # L'incolla automatico da focus/click popola soltanto la barra; non avvia azioni.
        self.url_entry.bind("<Button-1>", self._on_url_click)
        self.url_entry.bind("<FocusIn>", self._on_url_focus_in)
        self.url_entry.bind("<FocusOut>", self._on_url_focus_out)
        self.url_entry.bind("<<Paste>>", self._paste_event)
        self._set_url_placeholder()
        self.url_menu = PopupMenu(self.root, font=(APP_FONT_FAMILY, 13))
        self.url_menu.add_command(label="Taglia", command=lambda: self.url_entry.event_generate("<<Cut>>"))
        self.url_menu.add_command(label="Copia", command=lambda: self.url_entry.event_generate("<<Copy>>"))
        self.url_menu.add_command(label="Incolla", command=self._paste_from_clipboard)
        self.url_menu.add_separator()
        self.url_menu.add_command(label="Seleziona tutto", command=lambda: self.url_entry.selection_range(0, "end"))
        self.url_entry.bind("<Button-3>", self._url_context)
        self.url_entry.bind("<Button-2>", self._url_context)
        self.url_entry.bind("<Control-Button-1>", self._url_context)
        self.add_button = ttk.Button(panel, text="＋  Aggiungi", style="Accent.TButton",
                                     command=self._add_from_button)
        self.add_button.grid(row=1, column=1, padx=(0, 6))
        panel.columnconfigure(0, weight=1)
        toolbar = tk.Frame(self.root, bg=self.BG, padx=26)
        toolbar.pack(fill="x", pady=(1, 10))
        queue_controls = tk.Frame(toolbar, bg=self.BG)
        queue_controls.pack(anchor="w")
        ttk.Button(queue_controls, text="▶  Avvia coda", style="Accent.TButton", command=self._start_queue).pack(side="left", padx=8)
        self.pause_button = tk.Button(
            queue_controls, text="⏸", command=self._pause_cancel,
            font=("Segoe UI Symbol", 14), width=3, relief="flat", bd=0,
            bg=self.PANEL, fg=self.ACCENT, activebackground="#242424",
            activeforeground=self.ACCENT_HOVER, disabledforeground=self.MUTED,
            cursor="hand2", takefocus=True)
        self.pause_button.pack(side="left", padx=8)
        ttk.Button(queue_controls, text=translate_ui("Rimuovi", self.language), command=self._remove_selected).pack(side="left", padx=8)
        ttk.Button(queue_controls, text=translate_ui("Apri file torrent", self.language), command=self._choose_torrent_files).pack(side="left", padx=8)
        ttk.Button(queue_controls, text=translate_ui("Cerca torrent", self.language), command=self._open_torrent_search).pack(side="left", padx=8)
        self.selection_bar = tk.Frame(self.root, bg=self.PANEL, padx=26, pady=7)
        ttk.Button(self.selection_bar, text=translate_ui("Seleziona tutti", self.language), command=self._select_all_rows).pack(side="left")
        ttk.Button(self.selection_bar, text=translate_ui("Deseleziona", self.language), command=self._deselect_rows).pack(side="left", padx=6)
        ttk.Button(self.selection_bar, text=translate_ui("Priorità", self.language), command=self._apply_bulk_priority).pack(side="left", padx=(10, 4))
        self.bulk_priority_var = tk.StringVar(value="Normale")
        self.bulk_priority_option = ttk.OptionMenu(self.selection_bar, self.bulk_priority_var, "Normale", "Alta", "Normale", "Bassa")
        self.bulk_priority_option.pack(side="left")
        self.bulk_priority_menu = self.bulk_priority_option["menu"]
        ttk.Button(self.selection_bar, text=translate_ui("Pausa", self.language), command=lambda: self._bulk_pause(True)).pack(side="left", padx=(8, 3))
        ttk.Button(self.selection_bar, text=translate_ui("Riprendi", self.language), command=lambda: self._bulk_pause(False)).pack(side="left", padx=3)
        ttk.Button(self.selection_bar, text=translate_ui("Rimuovi", self.language), command=self._remove_selected).pack(side="left", padx=3)
        ttk.Button(self.selection_bar, text=translate_ui("Avvia ora", self.language), style="Accent.TButton", command=self._bulk_start_now).pack(side="left", padx=3)

        table_wrap = tk.Frame(self.root, bg=self.BG, padx=26)
        table_wrap.pack(fill="both", expand=True, pady=(0, 14))
        self._group_closed_image = self._make_group_toggle_image(False)
        self._group_open_image = self._make_group_toggle_image(True)
        columns = ("progress", "percent", "size", "speed", "upload", "peer", "priority", "state")
        # Extended retains native cursor, drag, Shift-click, and Ctrl-click behavior.
        self.tree = ttk.Treeview(table_wrap, columns=columns, show="tree headings",
                                 selectmode="extended", style="NoArrow.Treeview")
        self.tree.heading("#0", text="NOME FILE", anchor="w")
        self.tree.heading("progress", text="PROGRESSO", anchor="w")
        self.tree.heading("percent", text="%")
        self.tree.heading("size", text="DIMENSIONE")
        self.tree.heading("speed", text="VELOCITÀ")
        self.tree.heading("upload", text="UPLOAD")
        self.tree.heading("peer", text="PEER")
        self.tree.heading("priority", text="PRIORITÀ")
        self.tree.heading("state", text="STATO", anchor="w")
        self.tree.column("#0", width=280, minwidth=170, stretch=True, anchor="w")
        self.tree.column("progress", width=175, minwidth=110, stretch=True, anchor="center")
        self.tree.column("percent", width=62, minwidth=54, stretch=False, anchor="center")
        self.tree.column("size", width=105, minwidth=85, stretch=False, anchor="e")
        self.tree.column("speed", width=110, minwidth=85, stretch=False, anchor="e")
        self.tree.column("upload", width=110, minwidth=85, stretch=False, anchor="e")
        self.tree.column("peer", width=72, minwidth=60, stretch=False, anchor="center")
        self.tree.column("priority", width=90, minwidth=75, stretch=False, anchor="center")
        self.tree.column("state", width=255, minwidth=150, stretch=True, anchor="w")
        self.tree["displaycolumns"] = columns
        scrollbar = FluidScrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self._tree_scrolled)
        # La scrollbar va impacchettata prima della Treeview: altrimenti la
        # Treeview con expand=True occupa tutto lo spazio e la nasconde.
        scrollbar.pack(side="right", fill="y", padx=(6, 0))
        self.tree.pack(side="left", fill="both", expand=True)
        self.scrollbar = scrollbar

        # Barra di avanzamento grafica sovrapposta alla colonna "progress".
        self._overlay_phase = 0.0
        self._overlay_clock = time.monotonic()
        self._overlay_after = None
        self._progress_photo_cache = {}
        self.progress_overlay = tk.Canvas(self.tree, bg=self.ROW, bd=0,
                                          highlightthickness=0, takefocus=0)
        self.progress_overlay.bind("<Button-1>", self._overlay_click)
        self.progress_overlay.bind("<Double-1>", self._overlay_double_click)
        self.progress_overlay.bind("<Button-3>", self._overlay_context)
        self.progress_overlay.bind("<Button-2>", self._overlay_context)
        self.progress_overlay.bind("<MouseWheel>", self._overlay_wheel)
        self.progress_overlay.bind("<Button-4>", self._overlay_wheel)
        self.progress_overlay.bind("<Button-5>", self._overlay_wheel)
        self.tree.bind("<Configure>",
                       lambda _event: self._schedule_progress_overlay(), add="+")
        self.tree.bind("<Configure>",
                       lambda event: self._fit_tree_columns(event), add="+")
        # Il trascinamento delle intestazioni ridimensiona le colonne senza
        # generare un <Configure>: la barra sovrapposta va riallineata subito.
        for sequence in ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.tree.bind(sequence,
                           lambda _event: self._schedule_progress_overlay(), add="+")
        self.root.after(120, self._progress_overlay_tick)

        self.empty_state = tk.Frame(table_wrap, bg=self.ROW)
        self.empty_plus = tk.Label(self.empty_state, text="+", bg=self.ROW,
                                   fg=self.ACCENT, font=(APP_FONT_FAMILY, 84, "bold"))
        self.empty_plus.pack()
        self.empty_label = tk.Label(
            self.empty_state,
            text=translate_ui("Trascina qui un file Torrent", self.language),
            bg=self.ROW, fg=self.TEXT, font=(APP_FONT_FAMILY, 16, "bold"))
        self.empty_label.pack(pady=(0, 18))
        for widget in (self.empty_state, self.empty_plus, self.empty_label):
            widget.bind("<Button-1>", lambda _event: self.url_entry.focus_set())
        self._update_empty_state()

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection)
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<Button-1>", self._tree_click, add="+")
        self.tree.bind("<Button-3>", self._context)
        self.tree.bind("<Button-2>", self._context)
        # Su Windows la rotellina arriva al widget che ha il focus: senza questi
        # binding la lista non scorre mentre il cursore è nella barra URL.
        self.tree.bind("<MouseWheel>", self._on_tree_mousewheel, add="+")
        self.tree.bind("<Button-4>", self._on_tree_mousewheel, add="+")
        self.tree.bind("<Button-5>", self._on_tree_mousewheel, add="+")
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open_close, add="+")
        self.tree.bind("<<TreeviewClose>>", self._on_tree_open_close, add="+")
        self.root.bind("<MouseWheel>", self._on_root_mousewheel, add="+")
        self.root.bind("<Button-4>", self._on_root_mousewheel, add="+")
        self.root.bind("<Button-5>", self._on_root_mousewheel, add="+")
        # Leave Ctrl+left-click to Treeview so extended multi-selection remains available.
        self.context_menu = PopupMenu(self.root, bg="#171717", fg=self.TEXT,
                                      activebackground="#333333", activeforeground="#ffffff",
                                      font=(APP_FONT_FAMILY, 13))
        self.context_menu.add_command(label="Informazioni file…", command=self._show_info)
        priority_menu = PopupMenu(self.root, bg="#171717", fg=self.TEXT,
                                  activebackground="#333333", activeforeground="#ffffff",
                                  font=(APP_FONT_FAMILY, 13))
        self.priority_menu = priority_menu
        self.priority_var = tk.StringVar(value="Normale")
        for priority in ("Alta", "Normale", "Bassa"):
            priority_menu.add_radiobutton(label=priority, variable=self.priority_var,
                value=priority, command=lambda p=priority: self._set_priority(p))
        self.context_menu.add_cascade(label="Priorità", menu=priority_menu)
        self.context_menu.add_command(label="Scarica ora", command=self._download_now)
        self.context_menu.add_command(label="Pausa/Riprendi", command=self._pause_cancel)
        self.context_menu.add_command(label="Annulla", command=self._cancel)
        self.context_menu.add_command(label="Rimuovi dalla lista", command=self._remove)
        self.context_menu.add_command(label="Apri cartella", command=self._open_folder)
        footer = tk.Frame(self.root, bg=self.BG, padx=26)
        footer.pack(fill="x", pady=(0, 16))
        tk.Label(footer, text=f"Version {APP_VERSION}",
                 bg=self.BG, fg="#929292", font=(APP_FONT_FAMILY, 9)).pack(side="left")
        github_link = tk.Label(footer, text="GitHub", bg=self.BG,
                               fg=self.ACCENT, cursor="hand2",
                               font=(APP_FONT_FAMILY, 9, "underline"))
        github_link.pack(side="left", padx=(10, 0))
        github_link.bind("<Button-1>", lambda _event: self._open_github_page())
        github_link.bind("<Enter>", lambda _event: github_link.configure(
            fg=self.ACCENT_HOVER))
        github_link.bind("<Leave>", lambda _event: github_link.configure(
            fg=self.ACCENT))

    @staticmethod
    def _open_github_page():
        try:
            webbrowser.open(PROJECT_GITHUB_URL)
        except Exception:
            logging.exception("Impossibile aprire la pagina GitHub del progetto")

    def _toggle_side_menu(self):
        try:
            if self.side_menu.winfo_manager():
                self.side_menu.place_forget()
            else:
                self.side_menu.place(relx=1.0, x=-18, y=72, anchor="ne", width=270)
                self.side_menu.lift()
        except tk.TclError:
            pass

    def _close_side_menu(self):
        try:
            self.side_menu.place_forget()
        except tk.TclError:
            pass

    def _run_side_menu_action(self, callback):
        self._close_side_menu()
        callback()

    def _open_settings(self):
        if getattr(self, "settings_window", None) is not None:
            try:
                if self.settings_window.winfo_exists(): self.settings_window.lift(); return
            except tk.TclError: pass
        window = tk.Toplevel(self.root); self.settings_window = window
        window.title(translate_ui("Impostazioni", self.language)); window.geometry("780x620"); window.minsize(680, 560)
        window.configure(bg=self.BG)
        shell = tk.Frame(window, bg=self.BG, padx=18, pady=18); shell.pack(fill="both", expand=True)
        left = tk.Frame(shell, bg=self.PANEL, width=205, padx=8, pady=8); left.pack(side="left", fill="y"); left.pack_propagate(False)
        self.settings_list = tk.Listbox(left, bg=self.PANEL, fg=self.TEXT, selectbackground=self.ACCENT,
            selectforeground="#ffffff", relief="flat", highlightthickness=0, activestyle="none", font=(APP_FONT_FAMILY, 10))
        self.settings_list.pack(fill="both", expand=True)
        self.settings_categories = ["Download", "Torrent", "Informazioni", "Lingua"]
        self.settings_current_category = "Download"
        for category in self.settings_categories: self.settings_list.insert("end", translate_ui(category, self.language))
        self.settings_list.selection_set(self.settings_categories.index(self.settings_current_category))
        right = tk.Frame(shell, bg=self.BG, padx=24, pady=12); right.pack(side="left", fill="both", expand=True)
        self.settings_content = right
        self.settings_vars = {}
        self.settings_list.bind("<<ListboxSelect>>", self._show_settings_category)
        self._show_settings_category()
        _center_window(window, self.root)
        self._applica_titlebar_finestra(window)
        window.protocol("WM_DELETE_WINDOW", window.destroy)

    def _show_settings_category(self, _event=None):
        if not hasattr(self, "settings_content"): return
        sel = self.settings_list.curselection()
        if sel and 0 <= sel[0] < len(self.settings_categories):
            self.settings_current_category = self.settings_categories[sel[0]]
        category = getattr(self, "settings_current_category", "Download")
        if category not in self.settings_categories:
            category = self.settings_current_category = self.settings_categories[0]
        # Rebuild only the right-hand panel; never change the left-hand selection.
        for child in self.settings_content.winfo_children(): child.destroy()
        self.settings_title_label = tk.Label(self.settings_content, text=translate_ui(category, self.language), bg=self.BG, fg=self.ACCENT, font=(APP_FONT_FAMILY, 14, "bold"))
        self.settings_title_label.pack(anchor="w", pady=(0, 18))
        if category == "Lingua":
            tk.Label(self.settings_content, text=translate_ui("Seleziona la lingua dell'interfaccia", self.language), bg=self.BG, fg=self.TEXT, font=(APP_FONT_FAMILY, 10)).pack(anchor="w", pady=(0, 8))
            self.settings_vars["language"] = tk.StringVar(value=self.language_choice)
            options = [("system", "Predefinito di sistema"), ("en", "English"), ("de", "Deutsch"), ("it", "Italiano"), ("es", "Español"), ("fr", "Français")]
            for code, label in options:
                ttk.Radiobutton(self.settings_content, text=translate_ui(label, self.language), variable=self.settings_vars["language"], value=code).pack(anchor="w", pady=2)
        elif category == "Torrent":
            tk.Label(self.settings_content,
                     text=translate_ui("FONTI DI RICERCA", self.language),
                     bg=self.BG, fg=self.TEXT,
                     font=(APP_FONT_FAMILY, 10)).pack(anchor="w", pady=(0, 6))
            tk.Label(self.settings_content,
                     text=translate_ui(
                         "La ricerca torrent usa solo fonti legali: Internet Archive "
                         "(pubblico dominio, Creative Commons, software libero).",
                         self.language), bg=self.BG, fg=self.MUTED,
                     wraplength=470, justify="left", anchor="w",
                     font=(APP_FONT_FAMILY, 9)).pack(anchor="w", pady=(0, 10))
            tk.Label(self.settings_content,
                     text=translate_ui("Aggiungi un'API di ricerca", self.language),
                     bg=self.BG, fg=self.TEXT,
                     font=(APP_FONT_FAMILY, 10)).pack(anchor="w", pady=(0, 4))
            add_row = tk.Frame(self.settings_content, bg=self.BG)
            add_row.pack(fill="x", pady=(0, 4))
            self.settings_vars["torrent_api_input"] = tk.StringVar()
            ttk.Entry(add_row,
                      textvariable=self.settings_vars["torrent_api_input"],
                      style="Settings.TEntry").pack(side="left", fill="x",
                                                   expand=True)
            ttk.Button(add_row, text=translate_ui("Cerca e aggiungi",
                                                  self.language),
                       command=self._add_torrent_search_api).pack(
                           side="left", padx=(8, 0))
            self.torrent_api_status = tk.StringVar(value="")
            tk.Label(self.settings_content, textvariable=self.torrent_api_status,
                     bg=self.BG, fg=self.MUTED, wraplength=470, justify="left",
                     anchor="w", font=(APP_FONT_FAMILY, 9)).pack(
                         fill="x", pady=(0, 4))
            table_wrap = tk.Frame(self.settings_content, bg=self.ROW,
                                  highlightbackground=self.LINE,
                                  highlightthickness=1)
            table_wrap.pack(fill="both", expand=True, pady=(0, 6))
            self.torrent_api_table = ttk.Treeview(
                table_wrap, columns=("name", "url", "status"), show="headings",
                height=4, style="NoArrow.Treeview")
            self.torrent_api_table.heading(
                "name", text=translate_ui("NOME", self.language), anchor="w")
            self.torrent_api_table.heading(
                "url", text=translate_ui("API", self.language), anchor="w")
            self.torrent_api_table.heading(
                "status", text=translate_ui("STATO", self.language), anchor="w")
            self.torrent_api_table.column("name", width=120, minwidth=90,
                                          anchor="w", stretch=False)
            self.torrent_api_table.column("url", width=240, minwidth=150,
                                          anchor="w", stretch=True)
            self.torrent_api_table.column("status", width=100, minwidth=80,
                                          anchor="w", stretch=False)
            self.torrent_api_table.tag_configure("ok", foreground="#39d353")
            self.torrent_api_table.tag_configure("error",
                                                 foreground="#ff5a3c")
            self.torrent_api_table.tag_configure("pending",
                                                 foreground=self.MUTED)
            api_scrollbar = FluidScrollbar(
                table_wrap, orient="vertical",
                command=self.torrent_api_table.yview)
            self.torrent_api_table.configure(yscrollcommand=api_scrollbar.set)
            api_scrollbar.pack(side="right", fill="y")
            self.torrent_api_table.pack(side="left", fill="both", expand=True)
            api_buttons = tk.Frame(self.settings_content, bg=self.BG)
            api_buttons.pack(fill="x", pady=(0, 6))
            ttk.Button(api_buttons, text=translate_ui("Rimuovi", self.language),
                       command=self._remove_torrent_search_api).pack(side="left")
            ttk.Button(api_buttons, text=translate_ui("Verifica", self.language),
                       command=self._verify_torrent_search_api).pack(
                           side="left", padx=(6, 0))
            tk.Label(self.settings_content,
                     text=translate_ui(
                         "Scrivi il nome di un servizio noto (es. Internet Archive) "
                         "oppure il dominio/URL completo dell'API. Se l'API non viene "
                         "trovata, incolla l'URL con {query} o [QUERY] al posto del "
                         "testo cercato.", self.language), bg=self.BG, fg=self.MUTED,
                     wraplength=470, justify="left", anchor="w",
                     font=(APP_FONT_FAMILY, 9)).pack(anchor="w")
            self._refresh_torrent_api_table()
            self._auto_check_torrent_search_apis()
        elif category == "Informazioni":
            info_grid = tk.Frame(self.settings_content, bg=self.BG)
            info_grid.pack(anchor="w", pady=(0, 16))
            for row_index, (label, value) in enumerate((
                    ("Versione software", APP_VERSION),
                    ("Autore", translate_ui("slenter97 con DeepSeek",
                                            self.language)))):
                tk.Label(info_grid, text=translate_ui(label, self.language),
                         bg=self.BG, fg=self.MUTED, width=20, anchor="w",
                         font=(APP_FONT_FAMILY, 10)).grid(
                             row=row_index, column=0, sticky="w", pady=4)
                tk.Label(info_grid, text=value, bg=self.BG, fg=self.TEXT,
                         anchor="w", font=(APP_FONT_FAMILY, 10, "bold")).grid(
                             row=row_index, column=1, sticky="w", pady=4)
            self.startup_var = tk.BooleanVar(value=is_startup_enabled())

            def toggle_startup():
                try:
                    set_startup_enabled(self.startup_var.get())
                except OSError as exc:
                    self.startup_var.set(not self.startup_var.get())
                    messagebox.showerror(
                        translate_ui("Avvio automatico", self.language),
                        str(exc), parent=self.settings_window or self.root)
                    return
                self.startup_var.set(is_startup_enabled())

            tk.Checkbutton(
                self.settings_content,
                text=translate_ui("Avvia DOWNLOADVERSE all'avvio di Windows",
                                  self.language),
                variable=self.startup_var, command=toggle_startup,
                bg=self.BG, fg=self.TEXT, activebackground=self.BG,
                activeforeground=self.TEXT, selectcolor=self.PANEL,
                font=(APP_FONT_FAMILY, 10)).pack(anchor="w", pady=(0, 14))
            self.update_status_var = tk.StringVar(value="")
            self.update_check_button = ttk.Button(
                self.settings_content,
                text=translate_ui("Controlla aggiornamenti", self.language),
                style="Accent.TButton", command=self._check_for_updates)
            self.update_check_button.pack(anchor="w", pady=(4, 10))
            tk.Label(self.settings_content, textvariable=self.update_status_var,
                     bg=self.BG, fg=self.MUTED, wraplength=500, justify="left",
                     anchor="w", font=(APP_FONT_FAMILY, 9)).pack(anchor="w")
            ttk.Button(self.settings_content,
                       text=translate_ui("Reimposta richiesta di chiusura",
                                         self.language),
                       command=self._reset_close_action).pack(anchor="w", pady=(14, 0))
        else:
            tk.Label(self.settings_content, text=translate_ui("DESTINAZIONE", self.language), bg=self.BG, fg=self.TEXT, font=(APP_FONT_FAMILY, 10)).pack(anchor="w", pady=(0, 8))
            folder_row = tk.Frame(self.settings_content, bg=self.BG)
            folder_row.pack(fill="x", anchor="w", pady=(0, 12))
            self.settings_vars["download_directory"] = tk.StringVar(value=self.destination.get())
            ttk.Entry(folder_row, textvariable=self.settings_vars["download_directory"], style="Settings.TEntry").pack(side="left", fill="x", expand=True)
            ttk.Button(folder_row, text=translate_ui("Scegli cartella…", self.language), command=self._choose_settings_folder).pack(side="left", padx=(8, 0))

            tk.Label(self.settings_content, text=translate_ui("Download simultanei", self.language),
                     bg=self.BG, fg=self.TEXT, font=(APP_FONT_FAMILY, 10), anchor="w").pack(anchor="w", pady=(0, 6))
            concurrent_row = tk.Frame(self.settings_content, bg=self.BG)
            concurrent_row.pack(anchor="w", pady=(0, 14))
            self.settings_vars["concurrent"] = tk.IntVar(value=self.manager.max_concurrent)
            stepper = tk.Frame(concurrent_row, bg=self.BG)
            stepper.pack(side="left")

            def adjust_concurrent(delta, variable=self.settings_vars["concurrent"]):
                try:
                    value = int(variable.get())
                except (ValueError, tk.TclError):
                    value = self.manager.max_concurrent
                variable.set(min(10, max(1, value + delta)))

            ttk.Entry(stepper, width=4, textvariable=self.settings_vars["concurrent"],
                      style="Settings.TEntry", justify="center").pack(side="left")
            arrow_box = tk.Frame(stepper, bg=self.BG)
            arrow_box.pack(side="left", padx=(4, 0))
            arrow_style = dict(bg="#242424", fg=self.TEXT, activebackground=self.ACCENT,
                               activeforeground="#ffffff", relief="flat", bd=0,
                               font=("Segoe UI Symbol", 8, "bold"), width=2, height=1,
                               padx=0, pady=0, cursor="hand2", highlightthickness=0,
                               takefocus=True)
            tk.Button(arrow_box, text="▲", command=lambda: adjust_concurrent(1),
                      **arrow_style).pack(side="top", fill="x")
            tk.Button(arrow_box, text="▼", command=lambda: adjust_concurrent(-1),
                      **arrow_style).pack(side="top", fill="x", pady=(1, 0))
            tk.Label(concurrent_row, text=translate_ui("Numero di download simultanei (1–10)", self.language),
                     bg=self.BG, fg=self.MUTED, font=(APP_FONT_FAMILY, 9)).pack(side="left", padx=(12, 0))

            limits = load_download_limits_config()
            tk.Label(self.settings_content, text=translate_ui("Limiti di velocità: 0 = illimitata", self.language) + "  (MB/s)", bg=self.BG, fg=self.TEXT, font=(APP_FONT_FAMILY, 10)).pack(anchor="w", pady=(0, 8))
            down_value, _down_unit = format_rate_limit(limits["download_rate_limit"])
            up_value, _up_unit = format_rate_limit(limits["upload_rate_limit"])
            self.settings_vars["download_rate"] = tk.StringVar(value=down_value)
            self.settings_vars["upload_rate"] = tk.StringVar(value=up_value)
            for label, key in (("Velocità massima download", "download"), ("Velocità massima upload", "upload")):
                row=tk.Frame(self.settings_content,bg=self.BG); row.pack(anchor="w",pady=4)
                tk.Label(row,text=translate_ui(label,self.language),bg=self.BG,fg=self.TEXT,width=28,anchor="w").pack(side="left")
                ttk.Entry(row,width=10,textvariable=self.settings_vars[key+"_rate"],style="Settings.TEntry").pack(side="left")
                tk.Label(row, text="MB/s", bg=self.BG, fg=self.MUTED,
                         font=(APP_FONT_FAMILY, 10), width=6, anchor="w").pack(side="left", padx=(6, 0))
            row=tk.Frame(self.settings_content,bg=self.BG); row.pack(anchor="w",pady=8)
            tk.Label(row,text=translate_ui("Numero massimo peer",self.language),bg=self.BG,fg=self.TEXT,width=28,anchor="w").pack(side="left")
            self.settings_vars["connections"] = tk.IntVar(value=limits["connections_limit"])
            ttk.Spinbox(row,from_=1,to=1000,width=8,textvariable=self.settings_vars["connections"],
                        style="Settings.TSpinbox").pack(side="left")
        if category not in ("Informazioni", "Torrent"):
            ttk.Button(self.settings_content, text=translate_ui("Applica", self.language), command=self._apply_settings).pack(anchor="w", pady=(16, 0))

    def _set_torrent_api_status(self, text):
        variable = getattr(self, "torrent_api_status", None)
        if variable is not None:
            try:
                variable.set(text)
            except tk.TclError:
                pass

    def _refresh_torrent_api_table(self):
        tree = getattr(self, "torrent_api_table", None)
        if tree is None:
            return
        try:
            for iid in tree.get_children():
                tree.delete(iid)
            self._torrent_api_rows = {}
            tree.insert("", "end", iid="builtin-internet-archive",
                        values=("Internet Archive",
                                translate_ui("Integrata", self.language), "OK"),
                        tags=("ok",))
            for api in load_torrent_search_apis():
                url = api["url"]
                if url in self._torrent_api_checking:
                    status, tag = "Verifica…", "pending"
                else:
                    check = self._torrent_api_checks.get(url) or {}
                    state = check.get("state")
                    if state == "ok":
                        status, tag = "OK", "ok"
                    elif state == "error":
                        status, tag = "Errore", "error"
                    else:
                        status, tag = "Non verificata", "pending"
                iid = tree.insert(
                    "", "end",
                    values=(api["name"], url,
                            translate_ui(status, self.language)),
                    tags=(tag,))
                self._torrent_api_rows[iid] = api
        except tk.TclError:
            pass

    def _auto_check_torrent_search_apis(self):
        now = time.time()
        for api in load_torrent_search_apis():
            check = self._torrent_api_checks.get(api["url"])
            if check and now - check.get("at", 0) < 300:
                continue
            self._start_torrent_api_check(api)

    def _start_torrent_api_check(self, api):
        url = str(api.get("url") or "").strip()
        if not url or url in self._torrent_api_checking:
            return
        self._torrent_api_checking.add(url)
        self._torrent_api_checks[url] = {"state": "pending", "detail": "",
                                         "at": time.time()}
        self._refresh_torrent_api_table()
        threading.Thread(target=self._torrent_api_check_worker, args=(api,),
                         name="torrent-api-check", daemon=True).start()

    def _torrent_api_check_worker(self, api):
        result = test_torrent_search_api(api.get("url"))
        self.events.put(("torrent_api_check",
                         str(api.get("name") or api.get("url") or ""),
                         str(api.get("url") or ""), result))

    def _torrent_api_error_text(self, result):
        code = result.get("error") or ""
        detail = str(result.get("detail") or "").strip()
        if code == "http":
            text = f"HTTP {detail}." if detail else translate_ui(
                "Risposta HTTP non valida.", self.language)
            return text
        if code == "network":
            prefix = translate_ui("Errore di rete:", self.language)
            return f"{prefix} {detail}" if detail else translate_ui(
                "Errore di rete.", self.language)
        text = {
            "missing": "URL mancante.",
            "not_json": "La risposta non è JSON.",
            "invalid_json": "JSON non valido.",
            "format": "Formato JSON non riconosciuto.",
            "empty": "Nessun risultato nel formato atteso.",
            "homepage": "Questo è un sito, non un endpoint API.",
        }.get(code, "Verifica non riuscita.")
        if code == "placeholder":
            return translate_ui(
                "Segnaposto {0} non supportato: usa {query} o [QUERY].",
                self.language).replace("{0}", detail)
        return translate_ui(text, self.language)

    def _finish_torrent_api_check(self, name, url, result):
        self._torrent_api_checking.discard(url)
        ok = bool(result.get("ok"))
        self._torrent_api_checks[url] = {
            "state": "ok" if ok else "error",
            "detail": str(result.get("detail") or ""),
            "error": str(result.get("error") or ""),
            "at": time.time(),
        }
        try:
            window = getattr(self, "settings_window", None)
            alive = bool(window is not None and window.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive or getattr(self, "settings_current_category", "") != "Torrent":
            return
        self._refresh_torrent_api_table()
        if ok:
            message = f"{name}: {translate_ui('Funzionante', self.language)}"
        else:
            message = (f"{name}: {translate_ui('Non funzionante', self.language)}"
                       f" — {self._torrent_api_error_text(result)}")
        self._set_torrent_api_status(message)

    def _verify_torrent_search_api(self):
        tree = getattr(self, "torrent_api_table", None)
        if tree is None:
            return
        selection = tree.selection()
        if not selection:
            self._set_torrent_api_status(translate_ui(
                "Seleziona un'API da verificare.", self.language))
            return
        tags = tree.item(selection[0], "tags") or ()
        if selection[0] == "builtin-internet-archive":
            self._set_torrent_api_status(translate_ui(
                "Internet Archive è sempre attivo.", self.language))
            return
        api = getattr(self, "_torrent_api_rows", {}).get(selection[0])
        if api is None:
            return
        self._torrent_api_checks.pop(api["url"], None)
        self._start_torrent_api_check(api)
        self._set_torrent_api_status(translate_ui("Verifica in corso…",
                                                  self.language))

    def _add_torrent_search_api(self):
        variable = self.settings_vars.get("torrent_api_input")
        text = variable.get().strip() if variable is not None else ""
        if not text:
            self._set_torrent_api_status(translate_ui(
                "Scrivi il nome o l'URL dell'API.", self.language))
            return
        resolved = resolve_torrent_search_api(text)
        if resolved is not None:
            if resolved["builtin"]:
                self._set_torrent_api_status(translate_ui(
                    "Internet Archive è già integrato.", self.language))
                return
            self._store_torrent_search_api(resolved["name"], resolved["url"])
            return
        if looks_like_domain(text):
            self._start_torrent_api_probe(text)
            return
        self._set_torrent_api_status(translate_ui(
            "Servizio non riconosciuto. Inserisci il dominio o l'URL completo dell'API.",
            self.language))

    def _store_torrent_search_api(self, name, url):
        apis = load_torrent_search_apis()
        normalized = (url or "").strip()
        if any(api["url"].strip().casefold() == normalized.casefold()
               for api in apis):
            self._set_torrent_api_status(translate_ui(
                "API già aggiunta.", self.language))
            return
        apis.append({"name": (name or normalized).strip(), "url": normalized})
        try:
            save_torrent_search_apis(apis)
        except (OSError, ValueError):
            logging.exception("Impossibile salvare l'API di ricerca torrent")
            self._set_torrent_api_status(translate_ui(
                "Impossibile salvare l'API.", self.language))
            return
        if variable := self.settings_vars.get("torrent_api_input"):
            try:
                variable.set("")
            except tk.TclError:
                pass
        self._refresh_torrent_api_table()
        self._set_torrent_api_status(translate_ui("API aggiunta.",
                                                  self.language))
        for api in load_torrent_search_apis():
            if api["url"].strip().casefold() == normalized.casefold():
                self._start_torrent_api_check(api)
                break

    def _start_torrent_api_probe(self, value):
        if getattr(self, "_torrent_api_probe_running", False):
            return
        self._torrent_api_probe_running = True
        self._set_torrent_api_status(translate_ui("Ricerca API…",
                                                  self.language))
        threading.Thread(target=self._torrent_api_probe_worker,
                         args=(value,), name="torrent-api-probe",
                         daemon=True).start()

    def _torrent_api_probe_worker(self, value):
        url = ""
        error = ""
        try:
            url = probe_torrent_search_api(value)
            if not url:
                error = translate_ui("API non trovata per questo servizio.",
                                     self.language)
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
        self.events.put(("torrent_api_probe", value, url, error))

    def _finish_torrent_api_probe(self, value, url, error):
        self._torrent_api_probe_running = False
        try:
            window = getattr(self, "settings_window", None)
            alive = bool(window is not None and window.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive or getattr(self, "settings_current_category", "") != "Torrent":
            return
        if url:
            try:
                host = urllib.parse.urlsplit(
                    url.replace("{query}", "test")).netloc
            except ValueError:
                host = ""
            self._store_torrent_search_api(host or value, url)
        else:
            self._set_torrent_api_status(
                error or translate_ui("API non trovata per questo servizio.",
                                      self.language))

    def _remove_torrent_search_api(self):
        tree = getattr(self, "torrent_api_table", None)
        if tree is None:
            return
        selection = tree.selection()
        if not selection:
            self._set_torrent_api_status(translate_ui(
                "Seleziona un'API da rimuovere.", self.language))
            return
        tags = tree.item(selection[0], "tags") or ()
        if selection[0] == "builtin-internet-archive":
            self._set_torrent_api_status(translate_ui(
                "La fonte integrata non può essere rimossa.", self.language))
            return
        api = getattr(self, "_torrent_api_rows", {}).get(selection[0])
        if api is None:
            return
        apis = [row for row in load_torrent_search_apis()
                if row["url"].strip().casefold()
                != api["url"].strip().casefold()]
        try:
            save_torrent_search_apis(apis)
        except (OSError, ValueError):
            logging.exception("Impossibile rimuovere l'API di ricerca torrent")
            self._set_torrent_api_status(translate_ui(
                "Impossibile salvare l'API.", self.language))
            return
        self._refresh_torrent_api_table()
        self._set_torrent_api_status(translate_ui("API rimossa.",
                                                  self.language))
        self._torrent_api_checks.pop(api["url"], None)
        self._torrent_api_checking.discard(api["url"])

    def _apply_settings(self):
        category = getattr(self, "settings_current_category", "Download")
        try:
            if category == "Lingua":
                self._change_language(self.settings_vars["language"].get())
                return
            if category == "Torrent":
                return
            if category == "Download":
                raw_folder = self.settings_vars["download_directory"].get().strip()
                if not raw_folder:
                    raise ValueError("Cartella di download vuota")
                folder = os.path.abspath(os.path.expanduser(raw_folder))
                os.makedirs(folder, exist_ok=True)
                self.destination.set(folder)
                self.manager.destination = folder
                _write_app_config({"download_directory": folder})

                def parse_rate(key):
                    return rate_to_bytes_per_second(
                        self.settings_vars[key + "_rate"].get(), "MB/s")

                down = parse_rate("download")
                up = parse_rate("upload")
                peers = int(self.settings_vars["connections"].get())
                save_download_limits_config(down, up, peers)
                self.manager.set_download_limits(down, up, peers)
                self._save_settings_limit(self.settings_vars["concurrent"])
        except (ValueError, tk.TclError, OSError):
            logging.exception("Impossibile applicare le impostazioni")
            messagebox.showerror(translate_ui("Errore", self.language), translate_ui("Valore non valido nelle impostazioni.", self.language), parent=self.settings_window)

    def _save_settings_limit(self, variable):
        try:
            value = min(10, max(1, int(variable.get()))); variable.set(value)
            save_max_concurrent_config(value); self.max_concurrent = value; self.limit_var.set(value); self.manager.set_limit(value)
        except (ValueError, tk.TclError, OSError):
            logging.exception("Impossibile salvare il numero di download simultanei")
            variable.set(self.manager.max_concurrent)

    def _set_update_status(self, text):
        variable = getattr(self, "update_status_var", None)
        if variable is not None:
            try:
                variable.set(text)
            except tk.TclError:
                pass

    def _set_update_button_enabled(self, enabled):
        button = getattr(self, "update_check_button", None)
        if button is not None:
            try:
                button.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    def _check_for_updates(self, silent=False):
        if getattr(self, "_update_check_running", False):
            if not silent:
                self._update_check_silent = False
            return
        self._update_check_running = True
        self._update_check_silent = bool(silent)
        self._set_update_button_enabled(False)
        if not silent:
            self._set_update_status(
                translate_ui("Controllo aggiornamenti…", self.language))
        threading.Thread(target=self._fetch_update_info, name="update-check",
                         daemon=True).start()

    def _fetch_update_info(self):
        info = None
        error = ""
        try:
            info = _find_latest_update()
        except Exception as exc:
            logging.exception("Controllo aggiornamenti fallito")
            error = str(exc) or exc.__class__.__name__
        self.events.put(("update_check_result", info, error))

    def _finish_update_check(self, info, error):
        silent = bool(getattr(self, "_update_check_silent", False))
        self._update_check_silent = False
        self._update_check_running = False
        self._set_update_button_enabled(True)
        if error:
            message = translate_ui("Impossibile controllare gli aggiornamenti.",
                                   self.language)
            if silent:
                logging.info("Controllo aggiornamenti all'avvio non riuscito: %s",
                             error)
                return
            self._set_update_status(message)
            messagebox.showerror(translate_ui("Aggiornamento", self.language),
                                 f"{message}\n{error}", parent=self.root)
            return
        if not info:
            if not silent:
                self._set_update_status(
                    translate_ui("Nessun aggiornamento disponibile.",
                                 self.language))
            return
        self._set_update_status(
            translate_ui("Nuova versione disponibile: {0}", self.language).format(
                info["version"]))
        prompt = translate_ui(
            "Nuova versione {0} disponibile. Scaricare e installare ora?\n"
            "L'applicazione verrà chiusa.", self.language).format(info["version"])
        if messagebox.askyesno(translate_ui("Aggiornamento", self.language),
                               prompt, parent=self.root):
            self._start_update_download(info)
        else:
            self._set_update_status(
                translate_ui("Aggiornamento annullato.", self.language))

    def _start_update_download(self, info):
        self._pending_update_version = info.get("version", "")
        self._set_update_button_enabled(False)
        self._set_update_status(
            translate_ui("Download aggiornamento…", self.language))
        threading.Thread(target=self._download_update, args=(info,),
                         name="update-download", daemon=True).start()

    def _download_update(self, info):
        path = None
        error = ""
        try:
            path = _download_update_msi(info.get("url", ""),
                                        info.get("name", ""),
                                        info.get("id"))
        except Exception as exc:
            logging.exception("Download aggiornamento fallito")
            error = str(exc) or exc.__class__.__name__
        self.events.put(("update_download_result", path, error))

    def _finish_update_download(self, path, error):
        self._set_update_button_enabled(True)
        if error:
            message = translate_ui("Errore download aggiornamento.", self.language)
            self._set_update_status(message)
            messagebox.showerror(translate_ui("Aggiornamento", self.language),
                                 f"{message}\n{error}", parent=self.root)
            return
        self._set_update_status(
            translate_ui("Aggiornamento scaricato. Avvio del setup…",
                         self.language))
        self.root.after(300, lambda: self._start_update_installer(path))

    def _start_update_installer(self, msi_path):
        expected_version = getattr(self, "_pending_update_version", "")
        if os.name != "nt":
            messagebox.showinfo(
                translate_ui("Aggiornamento", self.language),
                translate_ui("Aggiornamento non supportato su questo sistema.",
                             self.language),
                parent=self.root)
            return
        if not msi_path or not os.path.isfile(msi_path):
            messagebox.showerror(translate_ui("Aggiornamento", self.language),
                                 translate_ui("Errore download aggiornamento.",
                                              self.language),
                                 parent=self.root)
            return
        try:
            if getattr(sys, "frozen", False):
                exe_path = os.path.abspath(sys.executable)
                app_args = ""
            else:
                exe_path = os.path.abspath(sys.executable)
                app_args = f'"{os.path.abspath(__file__)}"'
            script_path = os.path.join(
                tempfile.gettempdir(), f"downloadverse_update_{os.getpid()}.ps1")
            script = r'''
param([int]$ParentPid, [string]$MsiPath, [string]$ExePath, [string]$AppArgs, [string]$ExpectedVersion)
$logPath = Join-Path $env:TEMP 'downloadverse_update.log'
Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
function Write-Log([string]$Message) {
    try { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" | Out-File -FilePath $logPath -Append -Encoding utf8 } catch { }
}
Write-Log "helper avviato (parent=$ParentPid, msi=$MsiPath)"
foreach ($pyiVar in @('_PYI_APPLICATION_HOME_DIR', '_PYI_ARCHIVE_FILE', '_PYI_PARENT_PROCESS_LEVEL', '_MEIPASS2')) {
    Remove-Item -LiteralPath ("Env:" + $pyiVar) -ErrorAction SilentlyContinue
}
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) { Start-Sleep -Milliseconds 400 }
Write-Log "programma chiuso, avvio installazione"
$msiArgs = '/i "' + $MsiPath + '" /passive /norestart'
try {
    $proc = Start-Process -FilePath 'msiexec.exe' -ArgumentList $msiArgs -Wait -Verb RunAs -PassThru
    Write-Log ("installazione completata, codice " + $proc.ExitCode)
} catch {
    Write-Log ("elevazione non riuscita: " + $_.Exception.Message)
    try {
        $proc = Start-Process -FilePath 'msiexec.exe' -ArgumentList $msiArgs -Wait -PassThru
        Write-Log ("installazione senza elevazione, codice " + $proc.ExitCode)
    } catch { Write-Log ("installazione fallita: " + $_.Exception.Message) }
}
Start-Sleep -Seconds 2
$candidates = @()
if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles 'DOWNLOADVERSE\DOWNLOADVERSE.exe') }
if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'DOWNLOADVERSE\DOWNLOADVERSE.exe') }
if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\DOWNLOADVERSE\DOWNLOADVERSE.exe') }
if ($ExePath) { $candidates += $ExePath }
$existing = @($candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
$target = $null
if ($ExpectedVersion) {
    $target = $existing | Where-Object {
        try {
            $installedVersion = (Get-Item -LiteralPath $_).VersionInfo.FileVersion
            $installedVersion -and $installedVersion.StartsWith($ExpectedVersion)
        } catch { $false }
    } | Select-Object -First 1
}
if (-not $target) { $target = $existing | Select-Object -First 1 }
if (-not $target) { $target = $ExePath }
if ($target -and (Test-Path -LiteralPath $target)) {
    if ($AppArgs) { Start-Process -FilePath $target -ArgumentList $AppArgs } else { Start-Process -FilePath $target }
} else {
    Start-Process -FilePath 'DOWNLOADVERSE'
}
Start-Sleep -Seconds 1
try { Remove-Item -LiteralPath (Split-Path -Parent $MsiPath) -Recurse -Force -ErrorAction SilentlyContinue } catch { }
try { Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue } catch { }
'''.strip()
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(script)
            creationflags = 0
            if os.name == "nt":
                # CREATE_NO_WINDOW: DETACHED_PROCESS farebbe uscire PowerShell
                # subito senza eseguire lo script.
                creationflags = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP",
                                           0x00000200))
            # PyInstaller passa la cartella di estrazione ai figli tramite
            # queste variabili: se il nuovo processo le eredita, riusa la
            # vecchia _MEI (già cancellata) e non trova python313.dll.
            helper_env = os.environ.copy()
            for pyi_var in ("_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE",
                            "_PYI_PARENT_PROCESS_LEVEL", "_MEIPASS2"):
                helper_env.pop(pyi_var, None)
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-WindowStyle", "Hidden", "-File", script_path,
                 str(os.getpid()), msi_path, exe_path, app_args,
                 expected_version],
                creationflags=creationflags, close_fds=True, env=helper_env)
            messagebox.showinfo(
                translate_ui("Aggiornamento", self.language),
                translate_ui(
                    "Il programma verrà chiuso per completare l'aggiornamento.\n"
                    "Conferma la richiesta di autorizzazione di Windows per "
                    "installare la nuova versione.",
                    self.language),
                parent=self.root)
        except Exception:
            logging.exception("Impossibile avviare il setup di aggiornamento")
            messagebox.showerror(translate_ui("Aggiornamento", self.language),
                                 translate_ui("Errore download aggiornamento.",
                                              self.language),
                                 parent=self.root)
            return
        self._closing = True
        try:
            self._save_session()
        except Exception:
            logging.exception("Impossibile salvare la sessione prima dell'aggiornamento")
        self.manager.close()
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
        except Exception:
            pass
        self._disable_torrent_drop()
        self.root.destroy()

    def _reset_close_action(self):
        self.close_action = "ask"
        try:
            save_close_action("ask")
        except OSError:
            logging.exception("Impossibile ripristinare la richiesta di chiusura")
        self._set_update_status(
            translate_ui("Richiesta di chiusura ripristinata.", self.language))

    def _change_language(self, choice):
        self.language_choice = choice
        self.language = detect_system_language() if choice == "system" else choice
        try: save_language_config(choice)
        except OSError: logging.exception("Impossibile salvare la preferenza lingua")
        self._apply_language()
    def _apply_language(self):
        self.root.title("DOWNLOADVERSE")
        if getattr(self, "_url_placeholder_active", False):
            self._set_url_placeholder()
        if hasattr(self, "empty_label"):
            self.empty_label.configure(
                text=translate_ui("Trascina qui un file Torrent", self.language))
        for widget in self.root.winfo_children(): self._translate_widget_tree(widget)
        if hasattr(self, "bulk_priority_menu"):
            labels = [translate_ui(name, self.language) for name in ("Alta", "Normale", "Bassa")]
            for index, label in enumerate(labels):
                try: self.bulk_priority_menu.entryconfigure(index, label=label)
                except tk.TclError: pass
        if hasattr(self, "side_menu_items"):
            for button, key in self.side_menu_items:
                button.configure(text=translate_ui(key, self.language))
        if hasattr(self, "tree"):
            for col, text, anchor in (
                    ("#0", "NOME FILE", "w"),
                    ("progress", "PROGRESSO", "w"),
                    ("percent", "%", "center"),
                    ("size", "DIMENSIONE", "center"),
                    ("speed", "VELOCITÀ", "center"),
                    ("upload", "UPLOAD", "center"),
                    ("peer", "PEER", "center"),
                    ("priority", "PRIORITÀ", "center"),
                    ("state", "STATO", "w")):
                self.tree.heading(col, text=translate_ui(text, self.language),
                                  anchor=anchor)
        if getattr(self, "settings_window", None) is not None:
            try:
                if self.settings_window.winfo_exists():
                    self.settings_window.title(translate_ui("Impostazioni", self.language))
                    if hasattr(self, "settings_list"):
                        category = getattr(self, "settings_current_category", "Download")
                        index = self.settings_categories.index(category) if category in self.settings_categories else 0
                        self.settings_list.delete(0, "end")
                        for name in self.settings_categories:
                            self.settings_list.insert("end", translate_ui(name, self.language))
                        self.settings_list.selection_clear(0, "end")
                        self.settings_list.selection_set(index)
                        self.settings_list.activate(index)
                        if hasattr(self, "settings_content"): self._show_settings_category()
            except tk.TclError: pass
        if hasattr(self, "url_menu"):
            for i, text in enumerate(("Taglia", "Copia", "Incolla", None, "Seleziona tutto")):
                if text is not None: self.url_menu.entryconfigure(i, label=translate_ui(text, self.language))
        if hasattr(self, "context_menu"):
            entries = ((0,"Informazioni file…"),(1,"Priorità"),(2,"Scarica ora"),(3,"Pausa/Riprendi"),(4,"Annulla"),(5,"Rimuovi dalla lista"),(6,"Apri cartella"))
            for i, text in entries: self.context_menu.entryconfigure(i, label=translate_ui(text, self.language))
            selection = self.tree.selection() if hasattr(self, "tree") else ()
            if selection and self.tree.item(selection[0], "tags") == ("torrent_group",):
                self.context_menu.entryconfigure(3, label=translate_ui("Pausa / riprendi gruppo", self.language))
        if hasattr(self, "priority_menu"):
            for i, key in enumerate(("Alta", "Normale", "Bassa")): self.priority_menu.entryconfigure(i, label=translate_ui(key,self.language))
        for task in self.tasks_by_id.values(): self._render(task)
    def _translate_widget_tree(self, widget):
        try:
            text = widget.cget("text")
            source = text
            if source not in UI_STRINGS:
                for key, (english, german) in UI_STRINGS.items():
                    if source == english or source == german or source == ES_STRINGS.get(key) or source == FR_STRINGS.get(key):
                        source = key
                        break
            if source in UI_STRINGS: widget.configure(text=translate_ui(source, self.language))
        except Exception: pass
        for child in widget.winfo_children(): self._translate_widget_tree(child)

    def _url_placeholder_text(self):
        return translate_ui(
            "Inserisci un Link, un magnet o trascina un file .torrent sotto",
            self.language)

    def _set_url_placeholder(self):
        self._url_placeholder_active = True
        self.url_var.set(self._url_placeholder_text())
        try:
            self.url_entry.configure(style="Placeholder.TEntry")
        except tk.TclError:
            pass

    def _clear_url_placeholder(self):
        if not getattr(self, "_url_placeholder_active", False):
            return
        self._url_placeholder_active = False
        self.url_var.set("")
        try:
            self.url_entry.configure(style="TEntry")
        except tk.TclError:
            pass

    def _on_url_focus_in(self, _event=None):
        self._clear_url_placeholder()
        self._clipboard_on_entry(_event)

    def _on_url_focus_out(self, _event=None):
        if not self.url_var.get().strip():
            self._set_url_placeholder()

    def _on_url_click(self, event=None):
        self._clear_url_placeholder()
        self._clipboard_on_entry(event)

    def _url_context(self, event):
        self._clear_url_placeholder()
        try:
            self.url_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.url_menu.grab_release()
        return "break"

    def _clipboard_on_entry(self, _event=None):
        # FocusIn e Button-1 possono arrivare insieme: pianifica un solo controllo.
        if not self._clipboard_paste_pending:
            self._clipboard_paste_pending = True
            self.root.after_idle(self._paste_clipboard)

    def _paste_clipboard(self):
        self._clipboard_paste_pending = False
        # Non sovrascrivere testo già digitato/incollato e non ripetere un
        # auto-paste già consumato quando la barra è stata svuotata dopo l'aggiunta.
        if (self.url_var.get().strip()
                and not getattr(self, "_url_placeholder_active", False)):
            return
        try:
            value = self.root.clipboard_get()
        except Exception:
            return
        value = value.strip()
        if (not value or value == self._auto_paste_last_value
                or (value == self._suppressed_clipboard_magnet and parse_magnet_uri(value))):
            return
        self._set_url_entry_text(value)
        self._auto_paste_last_value = value

    def _set_url_entry_text(self, value):
        self._url_placeholder_active = False
        try:
            self.url_entry.configure(style="TEntry")
        except tk.TclError:
            pass
        self.url_var.set(value)
        self.url_entry.selection_range(0, "end")
        self.url_entry.icursor("end")

    def _paste_event(self, _event=None):
        # Gestisce Ctrl+V usando lo stesso percorso del menu contestuale e blocca
        # il binding classico di Tk, senza callback differiti di aggiunta.
        self._paste_from_clipboard()
        return "break"

    def _paste_from_clipboard(self):
        try:
            value = self.root.clipboard_get()
        except Exception:
            return
        self._set_url_entry_text(value)
        self._auto_paste_last_value = value.strip() or self._auto_paste_last_value

    def _handle_libtorrent_missing(self, task_id):
        task = self.tasks_by_id.get(task_id)
        if not task or task.cancelled or task.cancel_event.is_set() or task.state == "Annullato":
            return
        self._libtorrent_waiting[task_id] = task
        if _load_libtorrent() is not None:
            self._resume_waiting_magnets()
            return
        with self._libtorrent_install_lock:
            if self._libtorrent_install_active:
                task.state = "Installazione libtorrent in corso…"
                self._render(task)
                return
            if self._libtorrent_prompt_scheduled:
                return
            self._libtorrent_prompt_scheduled = True
        # Accumula gli eventi magnet già in coda: un solo dialog per il gruppo.
        self.root.after_idle(self._ask_libtorrent_install)

    def _ask_libtorrent_install(self):
        with self._libtorrent_install_lock:
            self._libtorrent_prompt_scheduled = False
            if self._libtorrent_install_active or not self._libtorrent_waiting:
                return
            self._libtorrent_install_active = True
        try:
            install = messagebox.askyesno(
                translate_ui("Libtorrent non disponibile", self.language),
                translate_ui("libtorrent non è installato. Vuoi installarlo ora?", self.language),
                parent=self.root)
        except Exception as exc:
            logging.exception("Impossibile mostrare il prompt di installazione libtorrent")
            self._finish_libtorrent_install_error(str(exc))
            return
        if not install:
            for pending in self._libtorrent_waiting.values():
                pending.cancel_event.set()
                pending.state = "Annullato"
                pending.error = "Installazione libtorrent annullata."
                self._render(pending)
            self._libtorrent_waiting.clear()
            with self._libtorrent_install_lock:
                self._libtorrent_install_active = False
            return
        for pending in self._libtorrent_waiting.values():
            pending.state = "Installazione libtorrent in corso…"
            self._render(pending)
        threading.Thread(target=self._install_libtorrent,
                         name="libtorrent-installer", daemon=True).start()

    def _install_libtorrent(self):
        if getattr(sys, "frozen", False):
            self.events.put(("libtorrent_install_result", False, None,
                             "libtorrent non è incluso in questa build portabile."))
            return
        try:
            kwargs = {"capture_output": True, "text": True, "check": False}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "libtorrent"], **kwargs)
            if result.returncode != 0:
                details = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
                raise RuntimeError(f"pip terminato con codice {result.returncode}.\n{details}")
            importlib.invalidate_caches()
            module = importlib.import_module("libtorrent")
            self.events.put(("libtorrent_install_result", True, module, ""))
        except Exception as exc:
            logging.exception("Installazione/import di libtorrent fallita")
            self.events.put(("libtorrent_install_result", False, None, str(exc)))

    def _resume_waiting_magnets(self):
        global lt, LIBTORRENT_IMPORT_ERROR
        if lt is None:
            try:
                importlib.invalidate_caches()
                lt = importlib.import_module("libtorrent")
                LIBTORRENT_IMPORT_ERROR = ""
            except Exception as exc:
                logging.exception("Reimport di libtorrent fallito dopo l'installazione")
                self._finish_libtorrent_install_error(str(exc))
                return
        waiting = list(self._libtorrent_waiting.values())
        self._libtorrent_waiting.clear()
        with self._libtorrent_install_lock:
            self._libtorrent_install_active = False
        for task in waiting:
            if (task in self.manager.tasks and not task.cancelled
                    and not task.cancel_event.is_set() and task.state != "Annullato"):
                if task.url.lower().endswith(".torrent"):
                    self.manager.retry_torrent_file(task)
                else:
                    self.manager.retry_magnet(task)

    def _finish_libtorrent_install_error(self, detail):
        for pending in self._libtorrent_waiting.values():
            pending.state = "Errore"
            pending.error = "Impossibile installare libtorrent."
            self._render(pending)
        self._libtorrent_waiting.clear()
        with self._libtorrent_install_lock:
            self._libtorrent_install_active = False
        logging.error("Dettagli installazione libtorrent: %s", detail)
        messagebox.showerror(translate_ui("Installazione non riuscita", self.language),
                             "Non è stato possibile installare libtorrent. Consulta il crash log.",
                             parent=self.root)

    def _add_magnet(self, url):
        if not parse_magnet_uri(url):
            messagebox.showwarning(translate_ui("Magnet non valido", self.language), translate_ui("Inserisci un magnet con hash BTIH valido.", self.language), parent=self.root)
            return
        try:
            task = self.manager.add_magnet(url)
        except ValueError as exc:
            messagebox.showwarning(translate_ui("Magnet non valido", self.language), str(exc), parent=self.root)
            return
        if task:
            # Una aggiunta esplicita del magnet prevale sulla soppressione impostata
            # quando l'utente ne aveva annullato una precedente.
            self._suppressed_clipboard_magnet = None
            self.tasks_by_id[task.id] = task
            self._render(task)
            self.url_var.set("")

    def _request_page_links(self, url):
        """Scarica ed esamina una pagina generica in background, senza toccare Tk dal worker."""
        key = urllib.parse.urldefrag(url.strip())[0]
        if key in self._page_pending or key in self._page_scanned:
            return
        self._page_pending.add(key)
        threading.Thread(target=self._fetch_page_links, args=(key,),
                         name="page-link-extractor", daemon=True).start()

    def _fetch_page_links(self, url):
        try:
            with requests.get(url, headers={"User-Agent": BROWSER_UA,
                                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                              timeout=(10, 25), stream=True, allow_redirects=True) as response:
                response.raise_for_status()
                content_type = str(response.headers.get("Content-Type", "")).lower()
                final_url = response.url or url
                if "html" in content_type or not content_type or "text/plain" in content_type:
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        if not chunk:
                            continue
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= 2 * 1024 * 1024:
                            break
                    encoding = response.encoding or "utf-8"
                    body = b"".join(chunks)[:2 * 1024 * 1024].decode(encoding, errors="replace")
                else:
                    body = ""
            if body and ("html" in content_type or re.search(r"<\s*(?:!doctype\s+html|html|a\b)", body, re.I)):
                links = extract_download_links(body, final_url)
            else:
                links = []
            self.events.put(("page_links_result", url, links))
        except Exception as exc:
            logging.info("Estrazione link pagina fallita per %s: %s", url, exc)
            self.events.put(("page_links_result", url, []))

    def _add_page_link_results(self, source_url, links):
        """Aggiunge URL trovati oppure la pagina stessa, evitando duplicati in lista."""
        self._page_pending.discard(urllib.parse.urldefrag(source_url)[0])
        self._page_scanned.add(urllib.parse.urldefrag(source_url)[0])
        candidates = links or [source_url]
        present = {task.url for task in self.manager.tasks}
        added = 0
        for link in candidates:
            if link in present:
                continue
            try:
                task = self.manager.add(link, filename=_filename_from_url(link))
            except ValueError:
                continue
            self.tasks_by_id[task.id] = task
            self._render(task)
            present.add(link)
            added += 1
        current_url = urllib.parse.urldefrag(self.url_var.get().strip())[0]
        if added and current_url == source_url.strip():
            self.url_var.set("")

    def _start_auto_import(self, url):
        """Legge un paste PrivateBin in background senza aprire finestre."""
        if getattr(self, "_auto_import_running", False):
            return
        self._auto_import_running = True
        self._auto_import_url = url
        self.url_var.set("")
        button = getattr(self, "add_button", None)
        if button is not None:
            button.configure(text=translate_ui("Importazione…", self.language),
                             state="disabled")
        threading.Thread(target=self._load_auto_import, args=(url,),
                         name="auto-paste-import", daemon=True).start()

    def _load_auto_import(self, url):
        urls = []
        error = ""
        try:
            urls = fetch_urls_from_source(url)
        except Exception as exc:
            logging.exception("Importazione automatica del paste fallita")
            error = str(exc) or exc.__class__.__name__
        self.events.put(("auto_import_result", urls, error))

    def _finish_auto_import(self, urls, error):
        self._auto_import_running = False
        button = getattr(self, "add_button", None)
        if button is not None:
            button.configure(text=translate_ui("＋  Aggiungi", self.language),
                             state="normal")
        if error:
            saved_url = getattr(self, "_auto_import_url", "")
            if saved_url:
                self.url_var.set(saved_url)
            messagebox.showerror(translate_ui("Errore importazione", self.language),
                                 error, parent=self.root)
            return
        added = self._enqueue_imported_urls(urls)
        if added:
            self._reveal_imported_tasks(added)
        self.url_entry.focus_set()

    def _open_link_import(self):
        """Finestra per importare un paste PrivateBin o un elenco di link."""
        if getattr(self, "link_import_window", None) is not None:
            try:
                if self.link_import_window.winfo_exists():
                    self.link_import_window.lift()
                    self.link_import_window.focus_force()
                    return
            except tk.TclError:
                pass
        window = tk.Toplevel(self.root)
        self.link_import_window = window
        window.title(translate_ui("Importa lista link", self.language))
        window.configure(bg=self.PANEL)
        window.geometry("760x620")
        window.minsize(620, 480)
        window.transient(self.root)
        body = tk.Frame(window, bg=self.PANEL, padx=20, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=translate_ui("Importa lista link", self.language),
                 bg=self.PANEL, fg=self.ACCENT,
                 font=(APP_FONT_FAMILY, 12, "bold")).pack(anchor="w", pady=(0, 4))
        tk.Label(body, text=translate_ui(
                     "Importa lista link: incolla un paste PrivateBin e/o un elenco di link.",
                     self.language),
                 bg=self.PANEL, fg=self.MUTED, wraplength=700, justify="left",
                 font=(APP_FONT_FAMILY, 9)).pack(anchor="w", pady=(0, 12))

        tk.Label(body, text=translate_ui("Link paste (PrivateBin)", self.language),
                 bg=self.PANEL, fg=self.TEXT,
                 font=(APP_FONT_FAMILY, 10), anchor="w").pack(anchor="w", pady=(0, 4))
        prefill = ""
        current = self.url_var.get().strip()
        try:
            split = urllib.parse.urlsplit(current)
            host = (split.hostname or "").lower()
            fragment = urllib.parse.unquote(split.fragment)
            if (split.scheme in ("http", "https")
                    and ("paste" in host or "pasteid" in split.query.lower()
                         or len(fragment) >= 20)):
                prefill = current
        except ValueError:
            pass
        paste_var = tk.StringVar(value=prefill)
        paste_entry = ttk.Entry(body, textvariable=paste_var, style="Settings.TEntry")
        paste_entry.pack(fill="x", pady=(0, 12))

        tk.Label(body, text=translate_ui("Link manuali (uno per riga)", self.language),
                 bg=self.PANEL, fg=self.TEXT,
                 font=(APP_FONT_FAMILY, 10), anchor="w").pack(anchor="w", pady=(0, 4))
        text_wrap = tk.Frame(body, bg=self.PANEL)
        text_wrap.pack(fill="both", expand=True)
        link_text = tk.Text(text_wrap, height=12, wrap="none", undo=True,
                            bg=self.BG, fg=self.TEXT, insertbackground=self.TEXT,
                            relief="flat", bd=6, highlightthickness=1,
                            highlightbackground=self.LINE, highlightcolor=self.ACCENT,
                            font=(APP_FONT_FAMILY, 10))
        text_scroll = FluidScrollbar(text_wrap, orient="vertical", command=link_text.yview)
        link_text.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side="right", fill="y")
        link_text.pack(side="left", fill="both", expand=True)

        def scroll_link_text(event):
            widget = getattr(event, "widget", None)
            if self._widget_is_within(widget, link_text):
                return None
            try:
                under_pointer = window.winfo_containing(event.x_root, event.y_root)
            except (tk.TclError, AttributeError):
                return None
            if (self._widget_is_within(under_pointer, link_text)
                    or under_pointer is text_scroll):
                try:
                    link_text.yview_scroll(self._wheel_units(event), "units")
                except tk.TclError:
                    pass
                return "break"
            return None

        window.bind("<MouseWheel>", scroll_link_text, add="+")
        window.bind("<Button-4>", scroll_link_text, add="+")
        window.bind("<Button-5>", scroll_link_text, add="+")

        tk.Label(body, text=f"{translate_ui('DESTINAZIONE', self.language)}: {self.destination.get()}",
                 bg=self.PANEL, fg=self.MUTED, anchor="w",
                 font=(APP_FONT_FAMILY, 9)).pack(anchor="w", pady=(8, 2))
        tk.Label(body, text=translate_ui(
                     "I file verranno aggiunti alla cartella di destinazione corrente.",
                     self.language),
                 bg=self.PANEL, fg=self.MUTED, anchor="w",
                 font=(APP_FONT_FAMILY, 9)).pack(anchor="w", pady=(0, 8))
        status_var = tk.StringVar(value="")
        status_label = tk.Label(body, textvariable=status_var, bg=self.PANEL,
                                fg=self.ACCENT, anchor="w",
                                font=(APP_FONT_FAMILY, 9))
        status_label.pack(fill="x")

        buttons = tk.Frame(body, bg=self.PANEL)
        buttons.pack(fill="x", pady=(12, 0))
        import_button = ttk.Button(buttons, text=translate_ui("Importa", self.language),
                                   style="Accent.TButton")
        import_button.pack(side="right", padx=(8, 0))
        self.link_import_button = import_button
        self.link_import_status = status_var
        ttk.Button(buttons, text=translate_ui("Chiudi", self.language),
                   command=lambda: self._close_link_import(window)).pack(side="right")

        def do_import():
            paste_url = paste_var.get().strip()
            manual_text = link_text.get("1.0", "end").strip()
            if not paste_url and not manual_text:
                messagebox.showwarning(
                    translate_ui("Errore importazione", self.language),
                    translate_ui("Inserisci almeno un link o un paste da importare.", self.language),
                    parent=window)
                return
            import_button.state(["disabled"])
            status_var.set(translate_ui(
                "Lettura paste…" if paste_url else "Importazione…", self.language))
            threading.Thread(target=self._load_import_sources,
                             args=(paste_url, manual_text), daemon=True).start()

        import_button.configure(command=do_import)
        paste_entry.bind("<Return>", lambda _event: do_import())
        window.bind("<Escape>", lambda _event: self._close_link_import(window))
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_link_import(window))
        _center_window(window, self.root)
        self._applica_titlebar_finestra(window)
        window.grab_set()
        paste_entry.focus_set()

    def _load_import_sources(self, paste_url, manual_text):
        """Legge paste e link manuali in un thread, senza toccare Tk."""
        urls = []
        error = ""
        try:
            if paste_url:
                urls.extend(fetch_urls_from_source(paste_url))
            if manual_text:
                urls.extend(extract_urls_from_text(manual_text))
        except Exception as exc:
            logging.exception("Importazione lista link fallita")
            error = str(exc) or exc.__class__.__name__
        self.events.put(("paste_import_result", urls, error))

    def _import_group_name(self, tasks):
        """Ricava un nome comune dai file importati (es. titolo del repack)."""
        names = []
        for task in tasks:
            name = os.path.splitext(os.path.basename(task.name or ""))[0]
            name = re.sub(r"(?i)[\s._-]*(?:part|cd|disk)[\s._-]*\d+$", "", name)
            name = re.sub(r"(?i)[\s._-]*(?:fitgirl|dodi)-repacks(?:\.site)?[\s._-]*",
                          "_", name)
            if name:
                names.append(name)
        prefix = os.path.commonprefix(names) if names else ""
        prefix = re.sub(r"[\s._-]+$", "", prefix).strip(" ._-")
        if len(prefix) < 4:
            candidate = names[0] if names else ""
            prefix = candidate.strip(" ._-")
        if not prefix:
            prefix = translate_ui("Importazione", self.language)
        return prefix[:72]

    def _import_group_text(self, group, is_open, tasks):
        label = group.name
        if tasks:
            label = (f"{label} ({len(tasks)}"
                     f"{translate_ui(' elementi', self.language)})")
        return label

    def _ensure_import_group(self, iid, name="", is_open=False):
        group = self.import_groups.get(iid)
        if group is not None and self.tree.exists(iid):
            return group
        group = ImportGroup(iid, name or translate_ui("Importazione", self.language))
        self.import_groups[iid] = group
        self.tree.insert("", "end", iid=iid,
                         text=self._import_group_text(group, is_open, []),
                         image=self._group_image(is_open),
                         values=("", "", "", "", "", "", "", ""),
                         tags=("import_group",), open=is_open)
        return group

    def _new_import_group(self, tasks):
        self._import_group_counter += 1
        iid = f"import-group-{self._import_group_counter}"
        group = self._ensure_import_group(iid, self._import_group_name(tasks))
        for task in tasks:
            task.import_group_iid = iid
        return group

    def _import_group_task_ids(self, iid):
        ids = []
        for child in self.tree.get_children(iid):
            try:
                ids.append(int(self.tree.item(child, "tags")[0]))
            except (IndexError, TypeError, ValueError):
                continue
        return ids

    def _import_group_tasks(self, group):
        return [self.tasks_by_id[ident]
                for ident in self._import_group_task_ids(group.iid)
                if ident in self.tasks_by_id]

    def _render_import_group(self, group):
        if group is None or not self.tree.exists(group.iid):
            return
        tasks = self._import_group_tasks(group)
        if not tasks:
            try:
                self.tree.delete(group.iid)
            except tk.TclError:
                pass
            self.import_groups.pop(group.iid, None)
            return
        try:
            is_open = bool(int(self.tree.item(group.iid, "open") or 0))
        except (tk.TclError, TypeError, ValueError):
            is_open = False
        _downloaded, total, percent = aggregate_torrent_progress(tasks)
        speed = sum(task.speed for task in tasks
                    if getattr(task, "speed", 0) and task.speed > 0)
        states = {task.state for task in tasks}
        if states and states <= {"Completato"}:
            state = "Completato"
        elif states & {"In corso", "In avvio", "Connessione…"}:
            state = "In corso"
        elif states and states <= {"In pausa"}:
            state = "In pausa"
        elif "Errore" in states:
            state = "Errore"
        elif "Annullato" in states:
            state = "Annullato"
        else:
            state = "In coda"
        values = ("",
                  f"{percent}%" if total > 0 else "—",
                  human_size(total) if total > 0 else "—",
                  f"{human_size(speed)}/s" if speed > 0 else "—",
                  "—", "", "", translate_ui(state, self.language))
        try:
            self.tree.item(group.iid,
                           text=self._import_group_text(group, is_open, tasks),
                           image=self._group_image(is_open),
                           values=values, tags=("import_group",))
        except tk.TclError:
            pass

    def _refresh_import_groups(self):
        for iid, group in list(self.import_groups.items()):
            if self.tree.exists(iid):
                self._render_import_group(group)

    def _on_tree_open_close(self, _event=None):
        self._refresh_import_groups()
        for iid in self.tree.get_children(""):
            if self.tree.item(iid, "tags") == ("torrent_group",):
                job = next((t.torrent_job for t in self.tasks_by_id.values()
                            if t.torrent_job and t.torrent_job.group_iid == iid), None)
                if job:
                    self._render_torrent_group(job)

    def _toggle_import_group(self, iid):
        group = self.import_groups.get(iid)
        if group is None:
            return
        try:
            is_open = bool(int(self.tree.item(iid, "open") or 0))
            self.tree.item(iid, open=not is_open)
            self._render_import_group(group)
        except (tk.TclError, TypeError, ValueError):
            pass

    def _toggle_torrent_group(self, iid):
        try:
            is_open = bool(int(self.tree.item(iid, "open") or 0))
            self.tree.item(iid, open=not is_open)
        except (tk.TclError, TypeError, ValueError):
            return
        job = next((t.torrent_job for t in self.tasks_by_id.values()
                    if t.torrent_job and t.torrent_job.group_iid == iid), None)
        if job:
            self._render_torrent_group(job)

    def _migrate_import_groups(self):
        """Raggruppa i download multi-parte ripristinati da sessioni precedenti."""
        part_re = re.compile(
            r"(?i)(?:^|[._\-\s])(?:part|cd|disk|dvd)[._\-\s]*\d+(?:[._\-\s]|$)")
        run = []

        def flush():
            if len(run) < 2:
                run.clear()
                return
            tasks = []
            for row in run:
                try:
                    ident = int(self.tree.item(row, "tags")[0])
                except (IndexError, TypeError, ValueError):
                    continue
                task = self.tasks_by_id.get(ident)
                if task is not None:
                    tasks.append(task)
            if len(tasks) < 2 or not any(part_re.search(task.name or "")
                                         for task in tasks):
                run.clear()
                return
            raw_names = [os.path.splitext(os.path.basename(task.name or ""))[0]
                         for task in tasks]
            if len(os.path.commonprefix(raw_names).strip(" ._-")) < 4:
                run.clear()
                return
            name = self._import_group_name(tasks)
            if not name:
                run.clear()
                return
            self._import_group_counter += 1
            group_iid = f"import-group-{self._import_group_counter}"
            group = ImportGroup(group_iid, name)
            self.import_groups[group_iid] = group
            try:
                index = self.tree.index(run[0])
            except tk.TclError:
                index = "end"
            self.tree.insert("", index, iid=group_iid,
                             text=self._import_group_text(group, False, []),
                             image=self._group_image(False),
                             values=("", "", "", "", "", "", "", ""),
                             tags=("import_group",), open=False)
            for row in run:
                try:
                    ident = int(self.tree.item(row, "tags")[0])
                except (IndexError, TypeError, ValueError):
                    continue
                task = self.tasks_by_id.get(ident)
                if task is None:
                    continue
                task.import_group_iid = group_iid
                try:
                    self.tree.move(row, group_iid, "end")
                except tk.TclError:
                    pass
            self._render_import_group(group)
            run.clear()

        for row in list(self.tree.get_children("")):
            tags = self.tree.item(row, "tags")
            ident = None
            if len(tags) == 1:
                try:
                    ident = int(tags[0])
                except (TypeError, ValueError):
                    ident = None
            task = self.tasks_by_id.get(ident) if ident is not None else None
            if (task is None or getattr(task, "import_group_iid", None)
                    or task.torrent_job is not None):
                flush()
                continue
            run.append(row)
        flush()

    def _enqueue_imported_urls(self, urls):
        """Accoda gli URL importati evitando duplicati, nella destinazione corrente."""
        present = {task.url for task in self.manager.tasks}
        added = []
        for link in urls:
            if not link or link in present:
                continue
            try:
                task = self.manager.add(link, filename=_suggested_filename(link))
            except ValueError:
                continue
            self.tasks_by_id[task.id] = task
            present.add(link)
            added.append(task)
        group = self._new_import_group(added) if len(added) > 1 else None
        for task in added:
            self._render(task)
        if group is not None:
            self._render_import_group(group)
        return added

    def _reveal_imported_tasks(self, tasks):
        """Porta in vista il gruppo importato (o la singola riga)."""
        if not tasks:
            return
        group_iid = getattr(tasks[0], "import_group_iid", None)
        try:
            if group_iid and group_iid in self.import_groups \
                    and self.tree.exists(group_iid):
                self.tree.selection_set(group_iid)
                self.tree.focus(group_iid)
                self.tree.see(group_iid)
                self._render_import_group(self.import_groups[group_iid])
                return
            iids = [str(task.id) for task in tasks
                    if self.tree.exists(str(task.id))]
            if not iids:
                return
            self.tree.selection_set(*iids)
            self.tree.focus(iids[0])
            self.tree.see(iids[0])
        except tk.TclError:
            pass

    def _close_link_import(self, window=None):
        window = window or getattr(self, "link_import_window", None)
        if window is not None:
            try:
                window.grab_release()
                window.destroy()
            except tk.TclError:
                pass
        self.link_import_window = None
        self.link_import_button = None
        self.link_import_status = None

    def _finish_link_import(self, urls, error):
        """Gestisce sulla GUI il risultato dell'importazione."""
        window = getattr(self, "link_import_window", None)
        try:
            if window is not None and not window.winfo_exists():
                window = None
        except tk.TclError:
            window = None
        button = getattr(self, "link_import_button", None)
        status_var = getattr(self, "link_import_status", None)
        if button is not None:
            try:
                button.state(["!disabled"])
            except tk.TclError:
                pass
        if status_var is not None:
            status_var.set("")
        if error:
            if window is not None:
                try:
                    window.grab_set()
                except tk.TclError:
                    pass
            messagebox.showerror(translate_ui("Errore importazione", self.language),
                                 error, parent=window or self.root)
            return
        added_tasks = self._enqueue_imported_urls(urls)
        if not added_tasks:
            message = ("Tutti i link sono già presenti nella coda." if urls
                       else "Nessun link trovato.")
            messagebox.showinfo(translate_ui("Importazione completata", self.language),
                                translate_ui(message, self.language),
                                parent=window or self.root)
            return
        self._close_link_import(window)
        self._reveal_imported_tasks(added_tasks)
        messagebox.showinfo(
            translate_ui("Importazione completata", self.language),
            f"{len(added_tasks)}{translate_ui(' link aggiunti alla coda.', self.language)}",
            parent=self.root)

    def _choose_settings_folder(self):
        current = self.settings_vars.get("download_directory")
        initial = current.get() if current is not None else self.destination.get()

        def apply_folder(path):
            if current is not None:
                current.set(path)
            settings = getattr(self, "settings_window", None)
            if settings is not None:
                try:
                    if settings.winfo_exists():
                        settings.deiconify()
                        settings.lift()
                except tk.TclError:
                    pass

        self._choose_torrent_files(folder_mode=True, initial_path=initial,
                                   on_folder_selected=apply_folder,
                                   parent=self.settings_window)

    def _open_cookie_dialog(self):
        """Permette di salvare Cookie e User-Agent per gli host protetti."""
        window = tk.Toplevel(self.root)
        window.title(translate_ui("Cookie host protetti", self.language))
        window.configure(bg=self.PANEL)
        window.transient(self.root)
        window.resizable(False, False)
        body = tk.Frame(window, bg=self.PANEL, padx=20, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=translate_ui("Cookie host protetti", self.language),
                 bg=self.PANEL, fg=self.ACCENT,
                 font=(APP_FONT_FAMILY, 11, "bold")).pack(anchor="w", pady=(0, 6))
        tk.Label(body, text=translate_ui(
                     "Cookie host protetti: incolla l'header Cookie del browser per qualsiasi sito che richiede il login (es. datanodes.to, kenotrontv.ru).",
                     self.language),
                 bg=self.PANEL, fg=self.MUTED, wraplength=560, justify="left",
                 font=(APP_FONT_FAMILY, 9)).pack(anchor="w", pady=(0, 14))

        form = tk.Frame(body, bg=self.PANEL)
        form.pack(fill="x")
        host_var = tk.StringVar(value=PROTECTED_HOSTS[0])
        cookie_var = tk.StringVar()
        user_agent_var = tk.StringVar()
        tk.Label(form, text="Host", bg=self.PANEL, fg=self.TEXT,
                 font=(APP_FONT_FAMILY, 10), anchor="w").grid(row=0, column=0, sticky="w", pady=5)
        host_box = ttk.Combobox(form, textvariable=host_var, values=PROTECTED_HOSTS,
                                state="normal", width=34, style="Settings.TCombobox")
        host_box.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=5)
        tk.Label(form, text="Cookie", bg=self.PANEL, fg=self.TEXT,
                 font=(APP_FONT_FAMILY, 10), anchor="w").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=cookie_var, width=58,
                  style="Settings.TEntry").grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=5)
        tk.Label(form, text="User-Agent", bg=self.PANEL, fg=self.TEXT,
                 font=(APP_FONT_FAMILY, 10), anchor="w").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=user_agent_var, width=58,
                  style="Settings.TEntry").grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=5)
        form.columnconfigure(1, weight=1)

        def load_current(_event=None):
            auth = load_browser_auth().get(host_var.get(), {})
            if isinstance(auth, dict):
                cookie_var.set(str(auth.get("cookie") or ""))
                user_agent_var.set(str(auth.get("user_agent") or ""))

        host_box.bind("<<ComboboxSelected>>", load_current)
        host_box.bind("<Return>", load_current)
        load_current()

        buttons = tk.Frame(body, bg=self.PANEL)
        buttons.pack(fill="x", pady=(16, 0))

        def save():
            try:
                target = save_browser_auth(host_var.get(), cookie_var.get(), user_agent_var.get())
            except (ValueError, OSError) as exc:
                messagebox.showerror(translate_ui("Impossibile salvare il cookie", self.language),
                                     str(exc), parent=window)
                return
            messagebox.showinfo(translate_ui("Cookie salvato", self.language),
                                f"{translate_ui('Salvato in', self.language)} {target}",
                                parent=window)
            window.destroy()

        ttk.Button(buttons, text=translate_ui("Salva", self.language),
                   style="Accent.TButton", command=save).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text=translate_ui("Chiudi", self.language),
                   command=window.destroy).pack(side="right")
        _center_window(window, self.root)
        self._applica_titlebar_finestra(window)
        window.grab_set()

    def _open_torrent_search(self):
        existing = getattr(self, "torrent_search_window", None)
        if existing is not None:
            try:
                existing.deiconify()
                existing.lift()
                return
            except tk.TclError:
                self.torrent_search_window = None
        window = tk.Toplevel(self.root)
        window.title(translate_ui("Cerca torrent", self.language))
        window.configure(bg=self.PANEL)
        window.geometry("940x620")
        window.minsize(720, 500)
        window.transient(self.root)
        self.torrent_search_window = window
        self.torrent_search_running = False
        self.torrent_search_rows = {}

        body = tk.Frame(window, bg=self.PANEL, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=translate_ui("CERCA TORRENT", self.language),
                 bg=self.PANEL, fg=self.ACCENT,
                 font=(APP_FONT_FAMILY, 11, "bold")).pack(anchor="w")
        search_row = tk.Frame(body, bg=self.PANEL)
        search_row.pack(fill="x", pady=(10, 6))
        self.torrent_search_var = tk.StringVar()
        entry = ttk.Entry(search_row, textvariable=self.torrent_search_var,
                          style="Settings.TEntry")
        entry.pack(side="left", fill="x", expand=True)
        self.torrent_search_button = ttk.Button(
            search_row, text=translate_ui("Cerca", self.language),
            style="Accent.TButton", command=self._start_torrent_search)
        self.torrent_search_button.pack(side="left", padx=(8, 0))
        self.torrent_search_status = tk.StringVar(value=translate_ui(
            "Inserisci il nome di un torrent e premi Cerca.", self.language))
        tk.Label(body, textvariable=self.torrent_search_status, bg=self.PANEL,
                 fg=self.MUTED, font=(APP_FONT_FAMILY, 9),
                 anchor="w").pack(fill="x")
        tk.Label(body, text=translate_ui(
            "Il programma permette di default solo ricerche su fonti legali, "
            "eventuali usi non consoni sono un problema dell'utente.",
            self.language), bg=self.PANEL, fg=self.MUTED,
            font=(APP_FONT_FAMILY, 8), wraplength=860,
            justify="left", anchor="w").pack(fill="x", pady=(2, 0))

        table_wrap = tk.Frame(body, bg=self.PANEL)
        table_wrap.pack(fill="both", expand=True, pady=(10, 10))
        columns = ("name", "size", "seeders", "leechers", "source")
        self.torrent_search_results = ttk.Treeview(
            table_wrap, columns=columns, show="headings", height=8,
            style="NoArrow.Treeview")
        headings = (
            ("name", "NOME", 420, "w"),
            ("size", "DIMENSIONE", 110, "e"),
            ("seeders", "SEED", 70, "center"),
            ("leechers", "PEER", 70, "center"),
            ("source", "FONTE", 140, "w"),
        )
        for column, label, width, anchor in headings:
            self.torrent_search_results.heading(
                column, text=translate_ui(label, self.language), anchor=anchor)
            self.torrent_search_results.column(
                column, width=width, minwidth=60, stretch=(column == "name"),
                anchor=anchor)
        scrollbar = FluidScrollbar(table_wrap, orient="vertical",
                                   command=self.torrent_search_results.yview)
        self.torrent_search_results.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.torrent_search_results.pack(side="left", fill="both", expand=True)
        self.torrent_search_results.bind(
            "<Double-1>", lambda _event: self._add_torrent_search_selection())

        buttons = tk.Frame(body, bg=self.PANEL)
        buttons.pack(fill="x")
        ttk.Button(buttons, text=translate_ui("Chiudi", self.language),
                   command=self._close_torrent_search).pack(side="right")
        ttk.Button(buttons, text=translate_ui("Aggiungi alla coda", self.language),
                   style="Accent.TButton",
                   command=self._add_torrent_search_selection).pack(
                       side="right", padx=(0, 8))

        entry.bind("<Return>", lambda _event: self._start_torrent_search())
        window.protocol("WM_DELETE_WINDOW", self._close_torrent_search)
        _center_window(window, self.root)
        self._applica_titlebar_finestra(window)
        entry.focus_set()

    def _start_torrent_search(self):
        if getattr(self, "torrent_search_running", False):
            return
        variable = getattr(self, "torrent_search_var", None)
        query = variable.get().strip() if variable is not None else ""
        if not query:
            return
        self.torrent_search_running = True
        button = getattr(self, "torrent_search_button", None)
        if button is not None:
            try:
                button.state(["disabled"])
            except tk.TclError:
                pass
        status = getattr(self, "torrent_search_status", None)
        if status is not None:
            status.set(translate_ui("Ricerca in corso…", self.language))
        threading.Thread(target=self._torrent_search_worker, args=(query,),
                         name="torrent-search", daemon=True).start()

    def _torrent_search_worker(self, query):
        results = []
        error = ""
        try:
            results = search_public_torrents(query)
        except Exception as exc:
            logging.exception("Ricerca torrent fallita")
            error = str(exc) or exc.__class__.__name__
        self.events.put(("torrent_search_result", results, error))

    def _finish_torrent_search(self, results, error):
        self.torrent_search_running = False
        button = getattr(self, "torrent_search_button", None)
        if button is not None:
            try:
                button.state(["!disabled"])
            except tk.TclError:
                button = None
        status = getattr(self, "torrent_search_status", None)
        tree = getattr(self, "torrent_search_results", None)
        if status is None or tree is None:
            return
        if error:
            status.set(error)
            return
        for iid in tree.get_children():
            tree.delete(iid)
        self.torrent_search_rows = {}
        for row in results:
            seeders = row.get("seeders")
            leechers = row.get("leechers")
            iid = tree.insert("", "end", values=(
                row["name"], human_size(row["size"]),
                "—" if seeders is None else seeders,
                "—" if leechers is None else leechers,
                translate_ui(row["source"], self.language)))
            self.torrent_search_rows[iid] = row
        if results:
            status.set(translate_ui("{0} risultati trovati.",
                                    self.language).format(len(results)))
        else:
            status.set(translate_ui("Nessun risultato trovato.", self.language))

    def _add_torrent_search_selection(self):
        tree = getattr(self, "torrent_search_results", None)
        if tree is None:
            return
        selection = tree.selection()
        row = (getattr(self, "torrent_search_rows", {}).get(selection[0])
               if selection else None)
        status = getattr(self, "torrent_search_status", None)
        if not row:
            if status is not None:
                status.set(translate_ui(
                    "Seleziona un risultato da aggiungere.", self.language))
            return
        magnet = row.get("magnet")
        if magnet:
            self._add_magnet(magnet)
            if status is not None:
                status.set(translate_ui("Aggiunto alla coda.", self.language))
            return
        if row.get("torrent_url"):
            if status is not None:
                status.set(translate_ui("Download del file torrent…",
                                        self.language))
            threading.Thread(target=self._download_search_torrent, args=(row,),
                             name="search-torrent-file", daemon=True).start()
            return
        if status is not None:
            status.set(translate_ui("Risultato non valido.", self.language))

    def _download_search_torrent(self, row):
        path = ""
        error = ""
        try:
            url = row.get("torrent_url")
            session = make_http_session(url)
            try:
                response = session.get(url, timeout=30, allow_redirects=True)
                response.raise_for_status()
                data = response.content
            finally:
                try:
                    session.close()
                except Exception:
                    pass
            if not data or len(data) > 8 * 1024 * 1024 or data[:1] != b"d":
                raise ValueError("File torrent non valido.")
            folder = os.path.join(DATA_DIR, "torrents")
            os.makedirs(folder, exist_ok=True)
            base = str(row.get("identifier") or row.get("name") or "torrent")
            filename = sanitize_filename(base)[:120] or "torrent"
            if not filename.lower().endswith(".torrent"):
                filename += ".torrent"
            path = os.path.join(folder, filename)
            temp = path + ".tmp"
            with open(temp, "wb") as handle:
                handle.write(data)
            os.replace(temp, path)
        except Exception as exc:
            path = ""
            error = str(exc) or exc.__class__.__name__
            logging.exception("Impossibile scaricare il file torrent dalla ricerca")
        self.events.put(("torrent_file_ready", path,
                         str(row.get("name") or ""), error))

    def _close_torrent_search(self):
        window = getattr(self, "torrent_search_window", None)
        self.torrent_search_window = None
        self.torrent_search_button = None
        self.torrent_search_results = None
        self.torrent_search_status = None
        self.torrent_search_rows = {}
        self.torrent_search_var = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def _choose_torrent_files(self, folder_mode=False, initial_path=None,
                              on_folder_selected=None, parent=None):
        _load_tkinter()
        import string
        import uuid

        browser_title = translate_ui(
            "Scegli cartella" if folder_mode else "Apri file torrent", self.language)
        parent = parent or self.root
        window = tk.Toplevel(parent)
        window.title(browser_title)
        window.geometry("980x620")
        window.minsize(760, 480)
        window.configure(bg=self.PANEL)
        window.transient(parent)

        profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        known_folders = {
            "Download": ("{374DE290-123F-4565-9164-39C4925E467B}",
                         ("Downloads", "Download", "Scaricati", "Téléchargements", "Heruntergeladen")),
            "Immagini": ("{33E28130-4E1E-4676-835A-98395C3BC3BB}",
                         ("Pictures", "Immagini", "Images", "Bilder")),
            "Documenti": ("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
                           ("Documents", "Documenti", "Mes documents", "Dokumente")),
            "Desktop": ("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
                        ("Desktop", "Scrivania", "Bureau", "Schreibtisch")),
        }
        known_paths = {}
        if sys.platform == "win32":
            import ctypes

            class _GUID(ctypes.Structure):
                _fields_ = (("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                            ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8))

            shell32 = ctypes.windll.shell32
            ole32 = ctypes.windll.ole32
            shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), ctypes.c_uint32,
                                                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree.restype = None
            for label, (guid_text, _names) in known_folders.items():
                raw = uuid.UUID(guid_text.strip("{}")).bytes_le
                guid = _GUID.from_buffer_copy(raw)
                result = ctypes.c_wchar_p()
                try:
                    if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                                    ctypes.byref(result)) == 0 and result.value:
                        known_paths[label] = result.value
                except (AttributeError, OSError):
                    pass
                finally:
                    if result:
                        ole32.CoTaskMemFree(ctypes.cast(result, ctypes.c_void_p))

        shortcuts = []
        for label, (_guid, candidates) in known_folders.items():
            path = known_paths.get(label)
            if not path:
                path = next((os.path.join(profile, name) for name in candidates
                             if os.path.isdir(os.path.join(profile, name))),
                            os.path.join(profile, candidates[0]))
            shortcuts.append((label, os.path.normpath(path)))

        volumes = set()
        if os.name == "nt":
            try:
                volumes.update(os.listdrives())
            except (AttributeError, OSError):
                pass
            try:
                import ctypes
                mask = ctypes.windll.kernel32.GetLogicalDrives()
                volumes.update(f"{chr(65 + index)}:\\" for index in range(26)
                               if mask & (1 << index))
            except (AttributeError, OSError):
                pass
            # Keep the sidebar useful even if a Windows drive-enumeration API
            # is unavailable: include the profile drive and any mounted letters.
            profile_drive, _ = os.path.splitdrive(profile)
            if profile_drive and os.path.isdir(profile_drive + os.sep):
                volumes.add(profile_drive + os.sep)
            volumes.update(f"{letter}:\\" for letter in string.ascii_uppercase
                           if os.path.isdir(f"{letter}:\\"))
        else:
            volumes.add(os.path.abspath(os.sep))
            try:
                volumes.update(os.path.join(os.sep, name) for name in os.listdir(os.sep)
                               if os.path.ismount(os.path.join(os.sep, name)))
            except OSError:
                pass
        volumes = sorted((os.path.normpath(path) for path in volumes if path), key=str.casefold)

        body = tk.Frame(window, bg=self.PANEL, padx=14, pady=12)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=browser_title, bg=self.PANEL,
                 fg=self.ACCENT, font=(APP_FONT_FAMILY, 11, "bold")).pack(anchor="w", pady=(0, 10))
        buttons = tk.Frame(body, bg=self.PANEL)
        buttons.pack(side="bottom", fill="x", pady=(10, 0))
        browser = tk.Frame(body, bg=self.PANEL)
        browser.pack(fill="both", expand=True)
        sidebar = tk.Frame(browser, bg=self.ROW, highlightbackground=self.LINE, highlightthickness=1)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        tk.Label(sidebar, text=translate_ui("Cartelle", self.language), bg=self.ROW, fg=self.MUTED,
                 font=(APP_FONT_FAMILY, 9, "bold")).pack(anchor="w", padx=8, pady=(8, 4))
        tree = ttk.Treeview(sidebar, show="tree", selectmode="browse", height=20)
        tree.pack(fill="both", expand=True, padx=5, pady=(0, 6))
        tree.column("#0", width=205, minwidth=175, stretch=True)

        right = tk.Frame(browser, bg=self.PANEL)
        right.pack(side="left", fill="both", expand=True)

        header = tk.Frame(right, bg=self.PANEL)
        header.pack(fill="x", pady=(0, 6))
        search_frame = tk.Frame(header, bg=self.ROW, highlightbackground=self.LINE,
                                highlightthickness=1)
        search_frame.pack(side="right", padx=(10, 0))
        tk.Label(search_frame, text="🔍", bg=self.ROW, fg=self.MUTED,
                 font=(APP_FONT_FAMILY, 9)).pack(side="left", padx=(7, 2))
        search_entry = tk.Entry(search_frame, width=22, bg=self.ROW, fg=self.MUTED,
                                insertbackground=self.TEXT, relief="flat", bd=0,
                                font=(APP_FONT_FAMILY, 9), highlightthickness=0)
        search_entry.pack(side="left", padx=(0, 7), pady=5)
        search_placeholder = translate_ui(
            "Cerca cartella…" if folder_mode else "Cerca file…", self.language)
        search_active = [False]

        def set_search_placeholder():
            if not search_entry.get().strip():
                search_active[0] = False
                search_entry.delete(0, "end")
                search_entry.insert(0, search_placeholder)
                search_entry.configure(fg=self.MUTED)

        def on_search_focus_in(_event=None):
            if not search_active[0]:
                search_active[0] = True
                if search_entry.get() == search_placeholder:
                    search_entry.delete(0, "end")
                search_entry.configure(fg=self.TEXT)

        def on_search_focus_out(_event=None):
            if not search_entry.get().strip():
                set_search_placeholder()

        def search_text():
            if not search_active[0]:
                return ""
            value = search_entry.get().strip()
            return "" if value == search_placeholder else value

        search_entry.bind("<FocusIn>", on_search_focus_in)
        search_entry.bind("<FocusOut>", on_search_focus_out)
        search_entry.bind("<KeyRelease>", lambda _event: refresh_listing())
        search_entry.bind("<<Paste>>", lambda _event: search_entry.after(10, refresh_listing))
        set_search_placeholder()

        current_path = tk.StringVar(value="")
        nav = tk.Frame(header, bg=self.PANEL)
        nav.pack(side="left", fill="x", expand=True)
        back_button = ttk.Button(nav, text="←", width=3,
                                 command=lambda: go_back())
        back_button.pack(side="left", padx=(0, 6))
        tk.Label(nav, textvariable=current_path, bg=self.PANEL, fg=self.MUTED,
                 font=(APP_FONT_FAMILY, 9), anchor="w").pack(side="left", fill="x", expand=True)
        history = []

        def update_back_button():
            back_button.configure(state="normal" if history else "disabled")

        update_back_button()

        listing = tk.Frame(right, bg=self.ROW, highlightbackground=self.LINE,
                           highlightthickness=1)
        listing.pack(fill="both", expand=True)
        listing.grid_rowconfigure(0, weight=1)
        listing.grid_columnconfigure(0, weight=1)

        browser_style = ttk.Style(window)
        browser_style.configure("Browser.Treeview", background=self.ROW,
                                fieldbackground=self.ROW, foreground=self.TEXT,
                                rowheight=26, borderwidth=0, relief="flat",
                                font=(APP_FONT_FAMILY, 9))
        browser_style.map("Browser.Treeview",
                          background=[("selected", self.SELECT_BG)],
                          foreground=[("selected", self.SELECT_FG)])
        browser_style.configure("Browser.Treeview.Heading", background="#161616",
                                foreground="#d0d0d0", font=(APP_FONT_FAMILY, 9, "bold"),
                                relief="flat", padding=(6, 6))
        browser_style.map("Browser.Treeview.Heading",
                          background=[("pressed", "#303030"), ("active", "#222222"),
                                      ("!disabled", "#161616")],
                          foreground=[("pressed", "#ffffff"), ("active", "#ffffff"),
                                      ("!disabled", "#d0d0d0")])

        columns = ("name", "modified", "type", "size")
        files = ttk.Treeview(listing, columns=columns, show="headings",
                             selectmode="extended", style="Browser.Treeview")
        files.heading("name", text=translate_ui("Nome", self.language), anchor="w")
        files.heading("modified", text=translate_ui("Data modifica", self.language), anchor="w")
        files.heading("type", text=translate_ui("Tipo", self.language), anchor="w")
        files.heading("size", text=translate_ui("Dimensione", self.language), anchor="e")
        files.column("name", width=300, minwidth=180, stretch=True, anchor="w")
        files.column("modified", width=150, minwidth=120, stretch=False, anchor="w")
        files.column("type", width=110, minwidth=90, stretch=False, anchor="w")
        files.column("size", width=90, minwidth=70, stretch=False, anchor="e")
        files.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        file_scroll = FluidScrollbar(listing, orient="vertical", command=files.yview)
        files.configure(yscrollcommand=file_scroll.set)
        file_scroll.grid(row=0, column=1, sticky="ns", pady=4)
        file_hscroll = FluidScrollbar(listing, orient="horizontal", command=files.xview)
        files.configure(xscrollcommand=file_hscroll.set)
        file_hscroll.grid(row=1, column=0, sticky="ew", padx=(4, 0), pady=(0, 4))

        folder_entries = []
        row_entries = {}
        open_button = None
        tree_paths = {}
        tree_loaded = set()

        def format_modified(stat_result):
            try:
                return time.strftime("%d/%m/%Y %H:%M",
                                     time.localtime(stat_result.st_mtime))
            except (OSError, ValueError, OverflowError):
                return "—"

        def update_open_button(*_event):
            if open_button is None:
                return
            if folder_mode:
                open_button.configure(state="normal")
                return
            selected = [iid for iid in files.selection() if iid in row_entries]
            has_torrent = any(not row_entries[iid]["is_dir"] for iid in selected)
            open_button.configure(state="normal" if has_torrent else "disabled")

        def refresh_listing(*_event):
            query = search_text().casefold()
            for iid in files.get_children(""):
                files.delete(iid)
            row_entries.clear()
            for entry in folder_entries:
                if query and query not in entry["name"].casefold():
                    continue
                iid = str(len(row_entries))
                row_entries[iid] = entry
                files.insert("", "end", iid=iid, values=(
                    ("📁  " if entry["is_dir"] else "📄  ") + entry["name"],
                    entry["modified"],
                    translate_ui(entry["type"], self.language),
                    entry["size"]))
            update_open_button()

        def add_tree_node(parent, label, path):
            node = tree.insert(parent, "end", text=label, values=(path or "",))
            tree_paths[node] = path
            if path and os.path.isdir(path):
                tree.insert(node, "end", text="…", values=("",))
            return node

        for label, path in shortcuts:
            add_tree_node("", label, path)
        for path in volumes:
            add_tree_node("", path, path)

        def populate_tree(node):
            path = tree_paths.get(node)
            if not path or node in tree_loaded:
                return
            tree_loaded.add(node)
            children = tree.get_children(node)
            if len(children) == 1 and not tree_paths.get(children[0]):
                tree.delete(children[0])
            try:
                folders = sorted((entry for entry in os.scandir(path)
                                  if entry.is_dir(follow_symlinks=False)),
                                 key=lambda entry: entry.name.casefold())
            except OSError:
                folders = []
            for entry in folders:
                add_tree_node(node, entry.name, entry.path)

        def show_folder(path, remember=True):
            path = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
            if not os.path.isdir(path):
                return
            previous = current_path.get()
            if (remember and previous
                    and os.path.normcase(previous) != os.path.normcase(path)):
                history.append(previous)
            current_path.set(path)
            try:
                items = list(os.scandir(path))
            except OSError:
                items = []
            folder_entries.clear()
            directories = []
            torrents = []
            for item in items:
                try:
                    if item.is_dir(follow_symlinks=False):
                        directories.append(item)
                    elif (not folder_mode and item.is_file(follow_symlinks=False)
                          and item.name.lower().endswith(".torrent")):
                        torrents.append(item)
                except OSError:
                    continue
            for item in (sorted(directories, key=lambda entry: entry.name.casefold())
                         + sorted(torrents, key=lambda entry: entry.name.casefold())):
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                    stat = item.stat(follow_symlinks=False)
                except OSError:
                    is_dir = False
                    stat = None
                folder_entries.append({
                    "name": item.name,
                    "path": item.path,
                    "is_dir": is_dir,
                    "modified": format_modified(stat) if stat is not None else "—",
                    "type": "Cartella" if is_dir else "File torrent",
                    "size": ("" if is_dir or stat is None
                             else human_size(stat.st_size)),
                })
            refresh_listing()
            update_back_button()

        def go_back():
            if not history:
                return
            target = history.pop()
            show_folder(target, remember=False)

        def on_tree_open(_event=None):
            node = tree.focus()
            populate_tree(node)

        def on_tree_select(_event=None):
            selection = tree.selection()
            if selection:
                path = tree_paths.get(selection[0])
                if path:
                    show_folder(path)

        def on_file_double_click(event=None):
            if event is not None:
                iid = files.identify_row(event.y)
                if not iid:
                    return
            else:
                selection = files.selection()
                iid = selection[0] if selection else None
            entry = row_entries.get(iid)
            if entry is None:
                return
            if entry["is_dir"]:
                show_folder(entry["path"])
            elif not folder_mode and self._add_torrent_file(entry["path"]):
                close_dialog()

        def close_dialog():
            try:
                window.grab_release()
                window.destroy()
            except tk.TclError:
                pass

        def open_selected():
            selected = [row_entries[iid]["path"] for iid in files.selection()
                        if iid in row_entries and not row_entries[iid]["is_dir"]]
            opened = False
            for path in selected:
                opened = self._add_torrent_file(path) or opened
            if opened:
                close_dialog()

        def select_folder():
            selected = [row_entries[iid]["path"] for iid in files.selection()
                        if iid in row_entries and row_entries[iid]["is_dir"]]
            target = selected[0] if selected else current_path.get()
            if target and os.path.isdir(target):
                if on_folder_selected is not None:
                    on_folder_selected(target)
                close_dialog()

        tree.bind("<<TreeviewOpen>>", on_tree_open)
        tree.bind("<<TreeviewSelect>>", on_tree_select)
        files.bind("<Double-Button-1>", on_file_double_click)
        files.bind("<<TreeviewSelect>>", lambda _event: update_open_button())
        files.bind("<Return>", lambda _event: select_folder() if folder_mode else open_selected())

        ttk.Button(buttons, text=translate_ui("Annulla", self.language),
                   command=close_dialog).pack(side="right")
        if folder_mode:
            open_button = ttk.Button(buttons, text=translate_ui("Seleziona cartella", self.language),
                                     style="Accent.TButton", command=select_folder)
        else:
            open_button = ttk.Button(buttons, text=translate_ui("Apri", self.language),
                                     style="Accent.TButton", command=open_selected,
                                     state="disabled")
        open_button.pack(side="right", padx=(0, 8))
        if not folder_mode:
            tk.Label(buttons, text="*.torrent", bg=self.PANEL, fg=self.MUTED,
                     font=(APP_FONT_FAMILY, 9)).pack(side="right", padx=(0, 10))
        window.protocol("WM_DELETE_WINDOW", close_dialog)
        start_path = initial_path if initial_path and os.path.isdir(initial_path) else None
        if not start_path and shortcuts:
            start_path = shortcuts[0][1]
        if start_path:
            show_folder(start_path)
        if shortcuts and not folder_mode:
            tree.selection_set(tree.get_children("")[0])
        update_open_button()
        _center_window(window, parent)
        self._applica_titlebar_finestra(window)
        window.lift()
        window.grab_set()

    # Backward-compatible internal alias for existing call sites.
    def _choose_torrent_file(self):
        self._choose_torrent_files()

    def _add_torrent_file(self, path):
        try:
            task = self.manager.add_torrent_file(path)
        except ValueError as exc:
            messagebox.showwarning(translate_ui("URL non valido", self.language), str(exc), parent=self.root)
            return False
        self.tasks_by_id[task.id] = task
        self._render(task)
        return True

    def _add_from_button(self):
        url = ("" if getattr(self, "_url_placeholder_active", False)
               else self.url_var.get().strip())
        if not url:
            return
        embedded = extract_urls_from_text(url)
        if len(embedded) > 1:
            self.url_var.set("")
            added = self._enqueue_imported_urls(embedded)
            if added:
                self._reveal_imported_tasks(added)
            self.url_entry.focus_set()
            return
        if len(embedded) == 1:
            url = embedded[0]
        if looks_like_privatebin_url(url):
            self._start_auto_import(url)
            return
        local_path = urllib.parse.unquote(url[7:]) if url.lower().startswith("file://") else url
        if local_path.lower().endswith(".torrent") and os.path.isfile(os.path.expanduser(local_path)):
            if self._add_torrent_file(os.path.expanduser(local_path)):
                self.url_var.set("")
            return
        if parse_magnet_uri(url) or url.lower().startswith("magnet:"):
            self._add_magnet(url)
            return
        if parse_github_release_url(url):
            self._request_github_release(url)
            return
        hostname = (urllib.parse.urlsplit(url).hostname or "").lower()
        protected = any(hostname == host or hostname.endswith("." + host)
                        for host in PROTECTED_HOSTS)
        parsed = urllib.parse.urlsplit(url)
        is_web_url = parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
        if is_mega_host(url) and not parse_mega_file_url(url):
            messagebox.showwarning(
                translate_ui("Link MEGA", self.language),
                translate_ui(
                    "Inserisci un link MEGA di un singolo file: le cartelle non sono supportate.",
                    self.language),
                parent=self.root)
            return
        if (is_web_url and not _is_direct_file_url(url) and not protected
                and not is_mega_host(url)):
            self._request_page_links(url)
            return
        try:
            task = self.manager.add(url)
        except ValueError as exc:
            messagebox.showwarning(translate_ui("URL non valido", self.language), str(exc), parent=self.root)
            return
        self.tasks_by_id[task.id] = task
        self.url_var.set("")
        self.url_entry.focus_set()
        self._render(task)

    def _request_github_release(self, url):
        parsed = parse_github_release_url(url)
        if not parsed:
            return
        endpoint = parsed["endpoint"]
        self._github_input_urls[endpoint] = url.strip()
        if endpoint in self._github_pending:
            return
        if endpoint in self._github_cache:
            self._show_release_assets(endpoint, self._github_cache[endpoint])
            return
        self._github_pending.add(endpoint)
        threading.Thread(target=self._fetch_github_release, args=(parsed,),
                         name="github-release", daemon=True).start()

    def _fetch_github_release(self, parsed):
        endpoint = parsed["endpoint"]
        try:
            headers = {"User-Agent": "DOWNLOADVERSE/1.0", "Accept": "application/vnd.github+json"}
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(endpoint, headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                release = json.loads(response.read().decode("utf-8"))
            rows = parse_github_release_assets(release, parsed["owner"], parsed["repo"])
            self.events.put(("github_result", endpoint, rows))
        except urllib.error.HTTPError as exc:
            logging.exception("GitHub API error for %s", endpoint)
            remaining = exc.headers.get("X-RateLimit-Remaining", "") if exc.headers else ""
            if exc.code == 429 or (exc.code == 403 and remaining == "0"):
                message = "Limite richieste GitHub raggiunto. Riprova più tardi."
            elif exc.code == 404:
                message = "Release GitHub non trovata (404). Verifica repository e tag."
            else:
                message = f"Errore API GitHub HTTP {exc.code}: {exc.reason}"
            self.events.put(("github_error", endpoint, message))
        except Exception as exc:
            logging.exception("Impossibile recuperare release GitHub %s", endpoint)
            self.events.put(("github_error", endpoint,
                             f"Impossibile recuperare la release GitHub: {exc}"))

    def _show_release_assets(self, endpoint, rows):
        existing = self._github_dialogs.get(endpoint)
        if existing:
            try:
                existing.lift()
                return
            except tk.TclError:
                self._github_dialogs.pop(endpoint, None)
        window = tk.Toplevel(self.root)
        window.title(translate_ui("Asset release GitHub", self.language))
        window.configure(bg=self.PANEL)
        window.transient(self.root)
        window.geometry("760x490")
        window.minsize(590, 330)
        self._github_dialogs[endpoint] = window
        body = tk.Frame(window, bg=self.PANEL, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=translate_ui("SELEZIONA I FILE DELLA RELEASE", self.language), bg=self.PANEL,
                 fg=self.ACCENT, font=(APP_FONT_FAMILY, 11, "bold")).pack(anchor="w")
        tk.Label(body, text=f"{len(rows)}{translate_ui(' elementi disponibili', self.language)}", bg=self.PANEL,
                 fg=self.MUTED, font=(APP_FONT_FAMILY, 9)).pack(anchor="w", pady=(3, 10))
        listing = tk.Frame(body, bg=self.ROW, highlightbackground=self.LINE, highlightthickness=1)
        listing.pack(fill="both", expand=True)
        canvas = tk.Canvas(listing, bg=self.ROW, highlightthickness=0)
        scrollbar = FluidScrollbar(listing, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=self.ROW)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        inner_window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(inner_window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        vars_ = []
        for index, asset in enumerate(rows):
            selected = tk.BooleanVar(value=False)
            vars_.append(selected)
            row = tk.Frame(inner, bg=self.ROW, padx=9, pady=5)
            row.grid(row=index, column=0, sticky="ew")
            inner.columnconfigure(0, weight=1)
            cb = tk.Checkbutton(row, variable=selected, bg=self.ROW, activebackground=self.ROW,
                                selectcolor=self.PANEL, fg=self.TEXT, activeforeground=self.TEXT,
                                relief="flat", highlightthickness=0)
            cb.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 8))
            tk.Label(row, text=asset["name"], bg=self.ROW, fg=self.TEXT,
                     font=(APP_FONT_FAMILY, 9, "bold"), anchor="w").grid(row=0, column=1, sticky="w")
            size_text = human_size(asset.get("size")) if asset.get("size") is not None else "Dimensione n/d"
            count = asset.get("download_count")
            count_text = f"{count:,} download".replace(",", ".") if count is not None else "Sorgente"
            tk.Label(row, text=f"{size_text}  ·  {count_text}", bg=self.ROW, fg=self.MUTED,
                     font=(APP_FONT_FAMILY, 8), anchor="w").grid(row=1, column=1, sticky="w")
            row.columnconfigure(1, weight=1)
        buttons = tk.Frame(body, bg=self.PANEL)
        buttons.pack(fill="x", pady=(12, 0))
        def select_all():
            for var in vars_:
                var.set(True)
        def add_selected():
            added = 0
            for asset, var in zip(rows, vars_):
                if var.get():
                    try:
                        task = self.manager.add(asset["url"], filename=asset["name"])
                    except ValueError as exc:
                        messagebox.showwarning(translate_ui("URL non valido", self.language), str(exc), parent=window)
                        continue
                    self.tasks_by_id[task.id] = task
                    self._render(task)
                    added += 1
            if added:
                submitted_url = self._github_input_urls.pop(endpoint, "")
                if submitted_url and self.url_var.get().strip() == submitted_url:
                    self.url_var.set("")
                self._close_release_dialog(endpoint, window)
        ttk.Button(buttons, text="Seleziona tutto", command=select_all).pack(side="left")
        ttk.Button(buttons, text="Annulla", command=lambda: self._close_release_dialog(endpoint, window)).pack(side="right")
        ttk.Button(buttons, text="Aggiungi selezionati", style="Accent.TButton",
                   command=add_selected).pack(side="right", padx=(0, 8))
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_release_dialog(endpoint, window))
        _center_window(window, self.root)
        self._applica_titlebar_finestra(window)
        window.grab_set()

    def _close_release_dialog(self, endpoint, window):
        self._github_dialogs.pop(endpoint, None)
        try:
            window.grab_release()
            window.destroy()
        except tk.TclError:
            pass

    def _show_torrent_selection(self, placeholder_id, job):
        placeholder = self.tasks_by_id.get(placeholder_id)
        if (placeholder_id in self._cancelled_torrent_selections or placeholder is None
                or placeholder.cancelled or placeholder.cancel_event.is_set()
                or placeholder.state == "Annullato" or placeholder not in self.manager.tasks
                or job._removed):
            self._cancelled_torrent_selections.add(placeholder_id)
            pending = placeholder or type("Pending", (), {
                "cancel_event": threading.Event(), "url": job.magnet_url,
                "id": placeholder_id})()
            self.manager.cancel_torrent_selection(pending, job)
            self.tasks_by_id.pop(placeholder_id, None)
            self.rows.pop(placeholder_id, None)
            try:
                if self.tree.exists(str(placeholder_id)):
                    self.tree.delete(str(placeholder_id))
            except tk.TclError:
                pass
            return
        window = tk.Toplevel(self.root)
        window.title(translate_ui("Seleziona i file del torrent", self.language))
        window.geometry("700x500")
        window.configure(bg=self.PANEL)
        window.transient(self.root)
        window.minsize(540, 330)
        body = tk.Frame(window, bg=self.PANEL, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=translate_ui("Seleziona i file del torrent", self.language), bg=self.PANEL,
                 fg=self.ACCENT, font=(APP_FONT_FAMILY, 11, "bold")).pack(anchor="w")
        torrent_name = ""
        try:
            torrent_name = job.info.name() if job.info is not None else ""
        except Exception:
            torrent_name = ""
        visible_files = [item for item in job.files if not item.get("pad")]
        visible_total = sum(item["size"] for item in visible_files)
        subtitle_parts = [part for part in (
            torrent_name, f"{len(visible_files)} file", human_size(visible_total)) if part]
        tk.Label(body, text=" · ".join(subtitle_parts), bg=self.PANEL,
                 fg=self.MUTED, font=(APP_FONT_FAMILY, 9), wraplength=620,
                 justify="left", anchor="w").pack(fill="x", pady=(3, 4))
        expected_seasons = torrent_season_numbers(torrent_name) if torrent_name else set()
        found_seasons = set()
        for item in visible_files:
            found_seasons.update(
                torrent_season_numbers(os.path.basename(item["path"])))
        if expected_seasons and found_seasons and not (expected_seasons & found_seasons):
            warning = translate_ui(
                "Attenzione: il torrent contiene la stagione {found}, non la stagione {expected}.",
                self.language).format(
                    found=", ".join(str(value) for value in sorted(found_seasons)),
                    expected=", ".join(str(value) for value in sorted(expected_seasons)))
            tk.Label(body, text=warning, bg=self.PANEL, fg=self.ACCENT,
                     font=(APP_FONT_FAMILY, 9, "bold"), wraplength=620,
                     justify="left", anchor="w").pack(fill="x", pady=(0, 8))
        else:
            tk.Frame(body, bg=self.PANEL, height=4).pack()
        listing = tk.Frame(body, bg=self.ROW, highlightbackground=self.LINE, highlightthickness=1)
        listing.pack(fill="both", expand=True)
        canvas = tk.Canvas(listing, bg=self.ROW, highlightthickness=0)
        scrollbar = FluidScrollbar(listing, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=self.ROW)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        inner_window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(inner_window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        vars_ = []
        for row_index, item in enumerate(visible_files):
            var = tk.BooleanVar(value=True)
            vars_.append((item, var))
            row = tk.Frame(inner, bg=self.ROW, padx=9, pady=5)
            row.grid(row=row_index, column=0, sticky="ew")
            inner.columnconfigure(0, weight=1)
            tk.Checkbutton(row, variable=var, bg=self.ROW, activebackground=self.ROW,
                           selectcolor=self.PANEL, fg=self.TEXT, activeforeground=self.TEXT,
                           relief="flat", highlightthickness=0,
                           command=lambda: update_total()).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 8))
            tk.Label(row, text=os.path.basename(item["path"]), bg=self.ROW, fg=self.TEXT,
                     font=(APP_FONT_FAMILY, 9, "bold"), anchor="w").grid(row=0, column=1, sticky="w")
            tk.Label(row, text=f"{human_size(item['size'])}  ·  {item['path']}",
                     bg=self.ROW, fg=self.MUTED,
                     font=(APP_FONT_FAMILY, 8), anchor="w").grid(row=1, column=1, sticky="w")
            row.columnconfigure(1, weight=1)
        total_label = tk.Label(body, text="", bg=self.PANEL, fg=self.MUTED, font=(APP_FONT_FAMILY, 9))
        total_label.pack(anchor="w", pady=(8, 0))
        def update_total():
            total = sum(item["size"] for item, var in vars_ if var.get())
            total_label.configure(text=f"{translate_ui('Totale selezionato:', self.language)} {human_size(total)}")
        def select_all():
            for _item, var in vars_: var.set(True)
            update_total()
        def deselect_all():
            for _item, var in vars_: var.set(False)
            update_total()
        def confirm():
            selected = sorted({item["index"] for item, var in vars_ if var.get()})
            self._close_torrent_selection(window)
            self.manager.confirm_torrent_selection(placeholder, job, selected)
        def cancel():
            self._cancel_torrent_selection_dialog(placeholder, job, window)
        buttons = tk.Frame(body, bg=self.PANEL)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text=translate_ui("Seleziona tutti", self.language), command=select_all).pack(side="left")
        ttk.Button(buttons, text=translate_ui("Deseleziona", self.language), command=deselect_all).pack(side="left", padx=6)
        ttk.Button(buttons, text=translate_ui("Annulla", self.language), command=cancel).pack(side="right")
        ttk.Button(buttons, text=translate_ui("Conferma", self.language), style="Accent.TButton", command=confirm).pack(side="right", padx=(0, 8))
        update_total()
        window.protocol("WM_DELETE_WINDOW", cancel)
        _center_window(window, self.root)
        self._applica_titlebar_finestra(window)
        window.grab_set()

    def _cancel_torrent_selection_dialog(self, placeholder, job, window=None):
        """Chiude e invalida la selezione prima di rimuovere placeholder e torrent."""
        self._cancelled_torrent_selections.add(placeholder.id)
        self._suppressed_clipboard_magnet = placeholder.url
        placeholder.cancelled = True
        if window is not None:
            self._close_torrent_selection(window)
        self.manager.cancel_torrent_selection(placeholder, job)
        self.url_var.set("")
        self.tasks_by_id.pop(placeholder.id, None)
        self.rows.pop(placeholder.id, None)
        try:
            if self.tree.exists(str(placeholder.id)):
                self.tree.delete(str(placeholder.id))
        except tk.TclError:
            pass

    @staticmethod
    def _close_torrent_selection(window):
        try:
            window.grab_release()
            window.destroy()
        except tk.TclError:
            pass

    def _start_queue(self):
        self._limit_changed()
        self.manager.start_queue()

    def _limit_changed(self):
        try:
            value = int(self.limit_var.get())
            value = min(10, max(1, value))
            self.manager.set_limit(value)
            self.max_concurrent = value
            save_max_concurrent_config(value)
        except (ValueError, tk.TclError, OSError):
            self.limit_var.set(self.manager.max_concurrent)

    def _selected(self):
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            ident = int(self.tree.item(selection[0], "tags")[0])
        except (IndexError, TypeError, ValueError):
            return None
        return self.tasks_by_id.get(ident)

    def _on_tree_selection(self, _event=None):
        self._refresh_pause_button()
        self._refresh_selection_bar()

    def _pause_action_for_selection(self):
        """Return the action implied by the selected task or torrent group."""
        selection = self.tree.selection()
        if not selection:
            return "pause"
        iid = selection[0]
        if self.tree.item(iid, "tags") in (("torrent_group",), ("import_group",)):
            tasks = [self.tasks_by_id[ident]
                     for ident in self._import_group_task_ids(iid)
                     if ident in self.tasks_by_id]
        else:
            task = self._selected()
            tasks = [task] if task is not None else []
        tasks = [task for task in tasks
                 if task.state not in ("Completato", "Annullato", "Errore")]
        should_pause = any(
            (task.running and task.pause_event.is_set()) or
            (not task.running and task.state == "In coda")
            for task in tasks)
        return "pause" if should_pause or not tasks else "resume"

    def _refresh_pause_button(self):
        button = getattr(self, "pause_button", None)
        if button is None:
            return
        action = self._pause_action_for_selection()
        button.configure(text="⏸" if action == "pause" else "▶",
                         font=("Segoe UI Symbol", 14),
                         command=self._pause_cancel)

    def _refresh_selection_bar(self):
        try:
            selection = self.tree.selection()
        except tk.TclError:
            selection = ()
        show = False
        if selection:
            # La barra azioni multiple appare solo con una selezione multipla:
            # la semplice riga evidenziata è già la selezione del file.
            show = len(selection) > 1
        if show:
            if not self.selection_bar.winfo_manager():
                self.selection_bar.pack(fill="x", before=self.tree.master, pady=(0, 5))
        else:
            self.selection_bar.pack_forget()

    def _tree_click(self, event):
        """Espande/chiude un gruppo cliccando l'icona +/−."""
        if self.tree.identify_column(event.x) != "#0":
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        tags = self.tree.item(iid, "tags")
        if tags not in (("import_group",), ("torrent_group",)):
            return None
        try:
            element = self.tree.identify_element(event.x, event.y)
        except tk.TclError:
            element = ""
        if element not in ("image", "Treeitem.image"):
            return None
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        if tags == ("import_group",):
            self._toggle_import_group(iid)
        else:
            self._toggle_torrent_group(iid)
        return "break"

    def _select_all_rows(self):
        iids = []
        def collect(parent=""):
            for iid in self.tree.get_children(parent):
                iids.append(iid)
                collect(iid)
        collect()
        if iids:
            self.tree.selection_set(*iids)
        self._refresh_selection_bar()

    def _deselect_rows(self):
        self.tree.selection_remove(self.tree.selection())
        self._refresh_selection_bar()

    def _checked_tasks(self):
        ids = set()
        for iid in self.tree.selection():
            if not self.tree.exists(iid):
                continue
            tags = self.tree.item(iid, "tags")
            if tags in (("torrent_group",), ("import_group",)):
                ids.update(self._import_group_task_ids(iid))
            else:
                try:
                    ids.add(int(tags[0]))
                except (IndexError, TypeError, ValueError):
                    pass
        return [self.tasks_by_id[i] for i in ids if i in self.tasks_by_id]

    def _apply_bulk_priority(self):
        for task in self._checked_tasks(): self.manager.set_priority(task, self.bulk_priority_var.get())

    def _bulk_pause(self, pause):
        tasks = self._checked_tasks()
        if pause:
            tasks = [task for task in tasks if task.running or task.state == "In coda"]
        else:
            tasks = [task for task in tasks if task.state == "In pausa"]
        self.manager.toggle_pause_group(tasks)

    def _bulk_remove(self):
        # Compatibility alias: all removal controls share selection semantics.
        self._remove_selected()

    def _selected_removal_tasks(self):
        """Restituisce i task evidenziati, espandendo le righe di gruppo."""
        return self._checked_tasks()

    def _remove_selected(self):
        tasks = self._selected_removal_tasks()
        if not tasks:
            # Non svuotare la coda per errore: l'azione "Svuota coda" è separata.
            return
        if not self._confirm_removal(tasks):
            return
        for task in tasks:
            self._remove_task_row(task)
        self._remove_empty_torrent_groups()
        self._refresh_import_groups()
        self.tree.selection_remove(self.tree.selection())
        self._refresh_selection_bar()
        self._update_empty_state()

    def _confirm_removal(self, tasks):
        active_states = ("In avvio", "In corso", "Risoluzione link…", "Connessione…",
                         "Recupero metadati torrent…", "Seleziona i file del torrent")
        has_unfinished_active = any(
            task.state != "Completato" and
            (task.running or task.state == "In pausa" or task.state in active_states)
            for task in tasks
        )
        return not has_unfinished_active or messagebox.askyesno(
            translate_ui("Rimuovi", self.language),
            translate_ui("Sei sicuro di rimuovere i file in download?", self.language),
            parent=self.root)

    def _remove_task_row(self, task):
        if not self.manager.remove(task):
            return False
        self.tasks_by_id.pop(task.id, None)
        self.rows.pop(task.id, None)
        iid = str(task.id)
        if self.tree.exists(iid):
            self.tree.delete(iid)
        self._update_empty_state()
        return True

    def _bulk_start_now(self):
        tasks = self._checked_tasks()
        if tasks:
            self.manager.start_many(tasks)

    def _double_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if self.tree.item(row, "tags") in (("import_group",), ("torrent_group",)):
            self.tree.selection_set(row)
            self.tree.focus(row)
            return "break"
        self.tree.selection_set(row)
        self._download_now()

    def _context(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            task = self._selected()
            is_group = self.tree.item(row, "tags") in (("torrent_group",), ("import_group",))
            if task or is_group:
                if task:
                    self.priority_var.set(task.priority)
                self.context_menu.entryconfigure(3, label=translate_ui(
                    "Pausa / riprendi gruppo" if is_group else "Pausa/Riprendi", self.language))
                try:
                    self.context_menu.tk_popup(event.x_root, event.y_root)
                finally:
                    self.context_menu.grab_release()
        return "break"

    def _download_now(self):
        task = self._selected()
        if task and task.torrent_placeholder:
            return
        if task and not self.manager.start_now(task):
            messagebox.showinfo(translate_ui("Download attivo", self.language), translate_ui("Questo file è già in download.", self.language), parent=self.root)

    def _pause_cancel(self):
        selection = self.tree.selection()
        if selection and self.tree.item(selection[0], "tags") in (
                ("torrent_group",), ("import_group",)):
            group_tasks = [self.tasks_by_id[ident]
                           for ident in self._import_group_task_ids(selection[0])
                           if ident in self.tasks_by_id]
            self.manager.toggle_pause_group(group_tasks)
            self._refresh_pause_button()
            return
        task = self._selected()
        if not task:
            return
        if task.torrent_placeholder and parse_magnet_uri(task.url):
            self._cancel()
            self._refresh_pause_button()
            return
        if task.running:
            self.manager.toggle_pause(task)
        elif task.state in ("In coda", "In pausa"):
            self.manager.toggle_pause_group([task])
        elif task.state not in ("Completato", "Annullato"):
            self.manager.cancel(task)
        self._refresh_pause_button()

    def _cancel(self):
        task = self._selected()
        if task:
            if task.torrent_placeholder and parse_magnet_uri(task.url):
                self._cancelled_torrent_selections.add(task.id)
                self._suppressed_clipboard_magnet = task.url
                self.manager.cancel(task)
                self.tasks_by_id.pop(task.id, None)
                self.rows.pop(task.id, None)
                try:
                    if self.tree.exists(str(task.id)):
                        self.tree.delete(str(task.id))
                except tk.TclError:
                    pass
                self._update_empty_state()
            else:
                self.manager.cancel(task)

    def _set_priority(self, priority):
        task = self._selected()
        if task:
            self.manager.set_priority(task, priority)

    def _remove_all(self):
        tasks = list(self.manager.tasks)
        if not self._confirm_removal(tasks):
            return
        removed = self.manager.remove_all()
        removed_ids = {task.id for task in removed}
        for ident in removed_ids:
            self.tasks_by_id.pop(ident, None)
            self.rows.pop(ident, None)
            if self.tree.exists(str(ident)):
                self.tree.delete(str(ident))
        self._remove_empty_torrent_groups()
        self._refresh_import_groups()
        self.tree.selection_remove(self.tree.selection())
        self._refresh_selection_bar()
        self._update_empty_state()

    def _remove_empty_torrent_groups(self):
        for iid in self.tree.get_children(""):
            tags = self.tree.item(iid, "tags")
            if tags in (("torrent_group",), ("import_group",)) \
                    and not self.tree.get_children(iid):
                self.tree.delete(iid)
                self.import_groups.pop(iid, None)

    def _remove(self):
        # Voce menu contestuale: rimuove soltanto la riga (o le righe già selezionate).
        self._remove_selected()

    def _open_folder(self):
        task = self._selected()
        path = os.path.dirname(task.path) if task and task.path else self.destination.get()
        os.makedirs(path, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror(translate_ui("Impossibile aprire la cartella", self.language), str(exc), parent=self.root)

    def _show_info(self):
        task = self._selected()
        if not task:
            return
        window = tk.Toplevel(self.root)
        window.title(translate_ui("Informazioni file", self.language))
        window.configure(bg=self.PANEL)
        window.transient(self.root)
        window.resizable(False, False)
        body = tk.Frame(window, bg=self.PANEL, padx=22, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=translate_ui("DETTAGLI TRASFERIMENTO", self.language), bg=self.PANEL, fg=self.ACCENT,
                 font=(APP_FONT_FAMILY, 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 13))
        fields = ("URL", "Nome", "Dimensione", "Tipo MIME", "Percorso", "Stato", "Byte scaricati", "Peer", "Info hash")
        labels = {}
        for index, name in enumerate(fields, 1):
            tk.Label(body, text=translate_ui(name, self.language).upper(), bg=self.PANEL, fg=self.MUTED,
                     font=(APP_FONT_FAMILY, 8, "bold")).grid(row=index, column=0, sticky="nw", padx=(0, 16), pady=5)
            val = tk.Label(body, text="—", bg=self.PANEL, fg=self.TEXT, justify="left", anchor="w",
                           wraplength=460, font=(APP_FONT_FAMILY, 9))
            val.grid(row=index, column=1, sticky="w", pady=5)
            labels[name] = val
        self.info_windows[task.id] = labels
        self._update_info(task)
        window.protocol("WM_DELETE_WINDOW", lambda i=task.id, w=window: self._close_info(i, w))
        ttk.Button(body, text="Chiudi", command=lambda i=task.id, w=window: self._close_info(i, w)).grid(
            row=len(fields) + 1, column=1, sticky="e", pady=(12, 0))
        _center_window(window, self.root)
        self._applica_titlebar_finestra(window)
        if task.torrent_job is None and not task.torrent_placeholder:
            self.manager.head_info(task)

    def _close_info(self, ident, window):
        self.info_windows.pop(ident, None)
        window.destroy()

    def _update_info(self, task):
        labels = self.info_windows.get(task.id)
        if not labels:
            return
        labels["URL"].configure(text=task.url)
        labels["Nome"].configure(text=task.name)
        labels["Dimensione"].configure(text=human_size(task.total))
        labels["Tipo MIME"].configure(text=task.mime)
        labels["Percorso"].configure(text=task.path or self.destination.get())
        labels["Stato"].configure(text=translate_ui(task.state, self.language) + (f" — {translate_ui(task.error, self.language)}" if task.error else ""))
        labels["Byte scaricati"].configure(text=f"{task.downloaded:,}".replace(",", "."))
        labels["Peer"].configure(text=str(task.peers) if task.torrent_job is not None else "—")
        magnet = parse_magnet_uri(task.url)
        labels["Info hash"].configure(text=magnet["info_hash"] if magnet else "—")

    def _render(self, task):
        size = human_size(task.total)
        speed = f"{human_size(task.speed)}/s" if task.speed > 0 else "—"
        if task.state == "In pausa": speed = "0 B/s"
        peer_value = ""
        if task.torrent_job is not None and len(task.torrent_job.tasks) <= 1:
            peer_value = str(task.torrent_job.peer_count)
        upload_speed = "—"
        if task.torrent_job is not None and not task.torrent_job.group_iid:
            rate = task.torrent_job.upload_rate
            upload_speed = f"{human_size(rate)}/s" if rate > 0 else "0 B/s"
        values = ("", f"{task.percent}%" if task.total is not None else "—",
                  size, speed, upload_speed, peer_value, translate_ui(task.priority, self.language),
                  (translate_ui(task.error, self.language) if task.state == "Errore" and task.error else translate_ui(task.state, self.language)))
        iid = str(task.id)
        if self.tree.exists(iid):
            self.tree.item(iid, text=task.name, values=values, tags=(task.id,))
        else:
            if task.torrent_job is not None and task.torrent_job.group_iid:
                parent = task.torrent_job.group_iid
            elif (getattr(task, "import_group_iid", None)
                  and self.tree.exists(task.import_group_iid)):
                parent = task.import_group_iid
            else:
                parent = ""
            self.tree.insert(parent, "end", iid=iid, text=task.name, values=values, tags=(task.id,))
        if task.torrent_job is not None and task.torrent_job.group_iid:
            self._render_torrent_group(task.torrent_job)
        elif getattr(task, "import_group_iid", None):
            group = self.import_groups.get(task.import_group_iid)
            if group is not None:
                self._render_import_group(group)
        self._update_info(task)
        self._refresh_selection_bar()
        self._refresh_pause_button()
        self._update_empty_state()
        self._schedule_progress_overlay()

    def _render_torrent_group(self, job):
        group_iid = job.group_iid
        if not group_iid or not self.tree.exists(group_iid):
            return
        children = list(job.tasks.values())
        downloaded, total, percent = aggregate_torrent_progress(children)
        percent_text = f"{percent}%" if children else "—"
        states = {task.state for task in children}
        if states and states <= {"Completato"}:
            state = "Completato"
        elif "In corso" in states or "In avvio" in states or "Connessione…" in states:
            state = "In corso"
        elif states and states <= {"In pausa"}:
            state = "In pausa"
        elif "Errore" in states:
            state = "Errore"
        elif "Annullato" in states:
            state = "Annullato"
        else:
            state = "In coda"
        group_name = self.tree.item(group_iid, "text")
        try:
            is_open = bool(int(self.tree.item(group_iid, "open") or 0))
        except (tk.TclError, TypeError, ValueError):
            is_open = False
        download_rate = f"{human_size(job.download_rate)}/s" if job.download_rate > 0 else "0 B/s"
        upload_rate = f"{human_size(job.upload_rate)}/s" if job.upload_rate > 0 else "0 B/s"
        self.tree.item(group_iid, text=group_name,
                       image=self._group_image(is_open),
                       values=(
            "", percent_text, human_size(total) if total > 0 else "—", download_rate, upload_rate,
            str(job.peer_count), "", translate_ui(state, self.language)))
        self._schedule_progress_overlay()

    def _tree_scrolled(self, first, last):
        self.scrollbar.set(first, last)
        self._schedule_progress_overlay()

    # ------------------------------------------------------------------
    # Barra di avanzamento grafica (colonna PROGRESSO)
    # ------------------------------------------------------------------
    def _fit_tree_columns(self, _event=None):
        """Adatta le colonne allo spazio disponibile, mantenendo visibile STATO."""
        tree = self.tree
        try:
            available = tree.winfo_width() - 4
        except tk.TclError:
            return
        if available < 120:
            return
        desired = {
            "#0": 280, "progress": 175, "percent": 62, "size": 105,
            "speed": 110, "upload": 110, "peer": 72, "priority": 90,
            "state": 255,
        }
        minimum = {
            "#0": 140, "progress": 105, "percent": 58, "size": 98,
            "speed": 96, "upload": 90, "peer": 60, "priority": 86,
            "state": 140,
        }
        order = ["#0", "progress", "percent", "size", "speed", "upload",
                 "peer", "priority", "state"]
        optional = ["upload", "peer", "speed", "priority", "percent", "size"]
        visible = list(order)
        for column in optional:
            if sum(minimum[name] for name in visible) <= available:
                break
            if column in visible:
                visible.remove(column)
        if not visible:
            return
        min_total = sum(minimum[name] for name in visible)
        desired_total = sum(desired[name] for name in visible)
        widths = {}
        if available >= desired_total:
            extra = available - desired_total
            for name in visible:
                widths[name] = desired[name]
            for name, weight in (("#0", 0.45), ("progress", 0.25), ("state", 0.30)):
                if name in widths:
                    widths[name] += int(extra * weight)
        elif available >= min_total:
            extra = available - min_total
            spread = max(1, desired_total - min_total)
            for name in visible:
                share = (desired[name] - minimum[name]) / spread
                widths[name] = minimum[name] + int(extra * share)
        else:
            for name in visible:
                widths[name] = minimum[name]
        used = sum(widths[name] for name in visible)
        widths[visible[-1]] += available - used
        display = tuple(name for name in visible if name != "#0")
        try:
            if tuple(tree["displaycolumns"]) != display:
                tree["displaycolumns"] = display
            for name in visible:
                tree.column(name, width=max(1, widths[name]), stretch=False)
        except tk.TclError:
            return
        self._schedule_progress_overlay()

    def _tree_rows_top(self):
        """Y dell'inizio della prima riga visibile del Treeview."""
        try:
            height = self.tree.winfo_height()
        except tk.TclError:
            return 42
        for y in range(0, min(height, 140)):
            try:
                if self.tree.identify_row(y):
                    bbox = self.tree.bbox(self.tree.identify_row(y))
                    if bbox and bbox[1] >= 0:
                        return bbox[1]
                    return max(0, y)
            except tk.TclError:
                break
        return 42

    def _schedule_progress_overlay(self):
        overlay = getattr(self, "progress_overlay", None)
        if overlay is None or self._overlay_after is not None:
            return
        try:
            self._overlay_after = self.root.after_idle(self._draw_progress_overlay_safe)
        except tk.TclError:
            self._overlay_after = None

    def _draw_progress_overlay_safe(self):
        self._overlay_after = None
        try:
            self._draw_progress_overlay()
        except tk.TclError:
            pass
        except Exception:
            logging.exception("Errore durante il disegno della barra di avanzamento")

    def _progress_overlay_tick(self):
        try:
            animated = self._draw_progress_overlay()
        except tk.TclError:
            return
        except Exception:
            logging.exception("Errore durante l'animazione della barra di avanzamento")
            animated = False
        try:
            self.root.after(60 if animated else 250, self._progress_overlay_tick)
        except tk.TclError:
            pass

    @staticmethod
    def _blend_hex(first, second, ratio):
        ratio = max(0.0, min(1.0, ratio))
        start = tuple(int(first[index:index + 2], 16) for index in (1, 3, 5))
        end = tuple(int(second[index:index + 2], 16) for index in (1, 3, 5))
        return "#%02x%02x%02x" % tuple(
            int(start[index] + (end[index] - start[index]) * ratio)
            for index in range(3))

    @staticmethod
    def _rounded_rect(canvas, x0, y0, x1, y1, radius, fill):
        width = x1 - x0
        height = y1 - y0
        if width <= 0 or height <= 0:
            return
        radius = max(1.0, min(radius, width / 2, height / 2))
        center = (y0 + y1) / 2
        canvas.create_oval(x0, center - radius, x0 + 2 * radius, center + radius,
                           fill=fill, outline="")
        canvas.create_oval(x1 - 2 * radius, center - radius, x1, center + radius,
                           fill=fill, outline="")
        canvas.create_rectangle(x0 + radius, center - radius, x1 - radius,
                                center + radius, fill=fill, outline="")

    def _draw_progress_bar(self, canvas, iid, width, height, y, selected):
        tree = self.tree
        canvas.create_rectangle(0, y, width, y + height,
                                fill=self.SELECT_BG if selected else self.ROW,
                                outline="")
        try:
            percent_text = tree.set(iid, "percent")
            state_text = tree.set(iid, "state")
        except tk.TclError:
            return False
        percent = None
        if isinstance(percent_text, str) and percent_text.endswith("%"):
            try:
                percent = max(0.0, min(100.0, float(percent_text[:-1])))
            except ValueError:
                percent = None
        pad_x = 7
        x0 = pad_x
        x1 = width - pad_x
        if x1 - x0 < 10:
            return False
        bar_height = min(13.0, max(8.0, height - 18))
        y0 = y + (height - bar_height) / 2
        if percent is None or percent <= 0:
            mode = "idle"
        elif percent >= 100:
            mode = "completed"
        elif "pausa" in state_text.lower():
            mode = "paused"
        elif "In corso" in state_text:
            mode = "downloading"
        else:
            mode = "waiting"
        downloading = mode == "downloading"
        renderer = _load_optional("ui_render")
        image_tk = _load_optional("PIL.ImageTk")
        row_background = self.SELECT_BG if selected else self.ROW
        if renderer is not None and image_tk is not None:
            bucket = 0
            if downloading:
                bucket = int(((self._overlay_phase % (2 * math.pi))
                              / (2 * math.pi)) * 16)
            key = (int(round(x1 - x0)), int(round(bar_height)),
                   int(percent or 0), mode, bucket, row_background)
            photo = self._progress_photo_cache.get(key)
            if photo is None:
                if len(self._progress_photo_cache) > 320:
                    self._progress_photo_cache.clear()
                rendered = renderer.progress_bar(
                    x1 - x0, bar_height, percent or 0, mode, self._overlay_phase,
                    background=row_background)
                photo = image_tk.PhotoImage(rendered)
                self._progress_photo_cache[key] = photo
            canvas.create_image(x0, y0, anchor="nw", image=photo)
            return downloading
        y1 = y0 + bar_height
        radius = bar_height / 2
        self._rounded_rect(canvas, x0, y0, x1, y1, radius, "#d9d9d9")
        if percent is None or percent <= 0:
            return False
        inner_width = x1 - x0
        fill_width = max(bar_height, inner_width * percent / 100.0)
        fill_width = min(fill_width, inner_width)
        paused = "pausa" in state_text.lower()
        if percent >= 100:
            color = "#c81616"
        elif paused:
            color = "#8f1a1a"
        elif downloading:
            pulse = 0.5 + 0.5 * math.sin(self._overlay_phase * 1.15)
            color = self._blend_hex("#8e0000", "#ff2a1a", 0.35 + 0.65 * pulse)
        else:
            color = "#c01818"
        self._rounded_rect(canvas, x0, y0, x0 + fill_width, y1,
                           min(radius, fill_width / 2), color)
        if downloading and fill_width > 34:
            band_width = 26.0
            span = fill_width - band_width
            position = math.sin(self._overlay_phase * 0.85) * 0.5 + 0.5
            start = x0 + span * position
            shine = self._blend_hex(color, "#ffb0a8", 0.55)
            self._rounded_rect(canvas, start, y0 + 1.5, start + band_width,
                               y1 - 1.5, (bar_height - 3) / 2, shine)
        return downloading

    def _draw_progress_overlay(self):
        tree = self.tree
        canvas = getattr(self, "progress_overlay", None)
        if canvas is None:
            return False
        try:
            if not tree.winfo_ismapped() or not tree.winfo_viewable():
                canvas.place_forget()
                return False
        except tk.TclError:
            return False
        top = self._tree_rows_top()
        y = top + 1
        height = tree.winfo_height()
        rows = []
        while y < height and len(rows) < 300:
            try:
                iid = tree.identify_row(y)
                if not iid:
                    break
                bbox = tree.bbox(iid, "progress")
            except tk.TclError:
                break
            if bbox:
                rows.append((iid, bbox))
                next_y = bbox[1] + max(1, bbox[3]) + 1
                if next_y <= y:
                    break
                y = next_y
            else:
                y += 35
        if not rows:
            canvas.place_forget()
            return False
        column_x, _column_y, column_width, _column_h = rows[0][1]
        if column_width <= 4:
            canvas.place_forget()
            return False
        last_bottom = max(bbox[1] + bbox[3] for _iid, bbox in rows)
        canvas_height = max(1, min(height, last_bottom) - top)
        current = (canvas.winfo_x(), canvas.winfo_y(),
                   canvas.winfo_width(), canvas.winfo_height())
        if current != (column_x, top, column_width, canvas_height):
            canvas.place(in_=tree, x=column_x, y=top,
                         width=column_width, height=canvas_height)
        canvas.delete("all")
        selected = set(tree.selection())
        now = time.monotonic()
        elapsed = max(0.0, now - getattr(self, "_overlay_clock", now))
        self._overlay_clock = now
        self._overlay_phase = (self._overlay_phase + elapsed * 3.0) % (2 * math.pi)
        animated = False
        for iid, _bbox in rows:
            cell_x, cell_y, cell_w, cell_h = _bbox
            if self._draw_progress_bar(canvas, iid, cell_w, cell_h,
                                       cell_y - top, iid in selected):
                animated = True
        return animated

    def _overlay_tree_y(self, event):
        try:
            return event.y + self.progress_overlay.winfo_y()
        except tk.TclError:
            return event.y

    def _overlay_click(self, event):
        iid = self.tree.identify_row(self._overlay_tree_y(event))
        if not iid:
            return "break"
        if event.state & 0x0004:  # Ctrl: aggiunge/rimuove dalla selezione
            if iid in self.tree.selection():
                self.tree.selection_remove(iid)
            else:
                self.tree.selection_add(iid)
        else:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        self._refresh_selection_bar()
        return "break"

    def _overlay_double_click(self, event):
        iid = self.tree.identify_row(self._overlay_tree_y(event))
        if not iid:
            return "break"
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        if self.tree.item(iid, "tags") in (("import_group",), ("torrent_group",)):
            return "break"
        self._download_now()
        return "break"

    def _overlay_context(self, event):
        iid = self.tree.identify_row(self._overlay_tree_y(event))
        if iid:
            self.tree.selection_set(iid)
            task = self._selected()
            is_group = self.tree.item(iid, "tags") in (("torrent_group",), ("import_group",))
            if task or is_group:
                if task:
                    self.priority_var.set(task.priority)
                self.context_menu.entryconfigure(3, label=translate_ui(
                    "Pausa / riprendi gruppo" if is_group else "Pausa/Riprendi",
                    self.language))
                try:
                    self.context_menu.tk_popup(event.x_root, event.y_root)
                finally:
                    self.context_menu.grab_release()
        return "break"

    def _overlay_wheel(self, event):
        self.tree.yview_scroll(self._wheel_units(event), "units")
        return "break"

    def _update_empty_state(self):
        state = getattr(self, "empty_state", None)
        if state is None:
            return
        try:
            if self.tree.get_children(""):
                state.place_forget()
                # Forza il ridisegno dell'area prima occupata dallo stato vuoto.
                self.tree.update_idletasks()
                self._schedule_progress_overlay()
            else:
                state.place(in_=self.tree, relx=0.5, rely=0.55,
                            anchor="center")
                state.lift()
        except tk.TclError:
            pass

    @staticmethod
    def _widget_is_within(widget, ancestor):
        while widget is not None:
            if widget is ancestor:
                return True
            try:
                widget = widget.master
            except AttributeError:
                return False
        return False

    @staticmethod
    def _wheel_units(event):
        delta = getattr(event, "delta", 0) or 0
        if delta:
            if abs(delta) >= 120:
                units = int(-delta / 120)
                return units or (-1 if delta > 0 else 1)
            return -1 if delta > 0 else 1
        return -1 if getattr(event, "num", 0) == 4 else 1

    def _scroll_with_event(self, target, event):
        try:
            target.yview_scroll(self._wheel_units(event), "units")
        except tk.TclError:
            pass
        return "break"

    def _on_tree_mousewheel(self, event):
        return self._scroll_with_event(self.tree, event)

    def _on_root_mousewheel(self, event):
        # Se il puntatore è sulla lista, scorre la lista anche quando il focus
        # è sulla barra URL o su un altro campo. Il Treeview gestisce già da
        # solo gli eventi che riceve direttamente.
        widget = getattr(event, "widget", None)
        if self._widget_is_within(widget, self.tree):
            return None
        try:
            under_pointer = self.root.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, AttributeError):
            return None
        if (self._widget_is_within(under_pointer, self.tree)
                or under_pointer is getattr(self, "scrollbar", None)):
            return self._scroll_with_event(self.tree, event)
        return None

    def _poll(self):
        try:
            update_ids = {}
            processed = 0
            # Non svuotare un backlog enorme in un solo tick: la finestra deve
            # restare reattiva anche con molti download attivi.
            while processed < 250:
                processed += 1
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                if event[0] == "update":
                    update_ids[event[1]] = None
                    continue
                if event[0] == "libtorrent_missing":
                    self._handle_libtorrent_missing(event[1])
                elif event[0] == "libtorrent_install_result":
                    _kind, success, module, detail = event
                    if success:
                        global lt, LIBTORRENT_IMPORT_ERROR
                        lt = module
                        LIBTORRENT_IMPORT_ERROR = ""
                        self._resume_waiting_magnets()
                    else:
                        self._finish_libtorrent_install_error(detail)
                elif event[0] == "torrent_selection":
                    self._show_torrent_selection(event[1], event[2])
                elif event[0] == "torrent_ready":
                    _kind, placeholder_id, children, _job = event
                    old = self.tasks_by_id.pop(placeholder_id, None)
                    self.rows.pop(placeholder_id, None)
                    # Un evento ready già in coda non deve ripopolare la GUI
                    # dopo un annullamento del placeholder.
                    if (placeholder_id in self._cancelled_torrent_selections
                            or old is None or old.cancelled or old.cancel_event.is_set()):
                        continue
                    if self.tree.exists(str(placeholder_id)):
                        self.tree.delete(str(placeholder_id))
                    if len(children) > 1:
                        job = _job
                        group_iid = f"torrent-group-{placeholder_id}"
                        name = ""
                        try:
                            name = job.info.name()
                        except Exception:
                            pass
                        if not name:
                            name = os.path.basename(children[0].name.split(os.sep, 1)[0]) or children[0].name
                        job.group_iid = group_iid
                        self.tree.insert("", "end", iid=group_iid, text=name,
                                         image=self._group_image(True), open=True,
                                         values=("", "", "", "0 B/s", "0 B/s", str(job.peer_count), "", ""),
                                         tags=("torrent_group",))
                    if getattr(old, "_restore_torrent_children", None) is not None:
                        self._restore_torrent_children(children, old)
                    for task in children:
                        self.tasks_by_id.setdefault(task.id, task)
                        self._render(task)
                elif event[0] == "github_result":
                    _kind, endpoint, rows = event
                    self._github_pending.discard(endpoint)
                    self._github_cache[endpoint] = rows
                    if rows:
                        self._show_release_assets(endpoint, rows)
                    else:
                        messagebox.showinfo(translate_ui("Release GitHub", self.language), translate_ui("La release non contiene asset scaricabili.", self.language), parent=self.root)
                elif event[0] == "github_error":
                    _kind, endpoint, message = event
                    self._github_pending.discard(endpoint)
                    self._github_input_urls.pop(endpoint, None)
                    messagebox.showerror(translate_ui("Errore release GitHub", self.language), message, parent=self.root)
                elif event[0] == "page_links_result":
                    _kind, source_url, links = event
                    self._add_page_link_results(source_url, links)
                elif event[0] == "paste_import_result":
                    _kind, urls, error = event
                    self._finish_link_import(urls, error)
                elif event[0] == "auto_import_result":
                    _kind, urls, error = event
                    self._finish_auto_import(urls, error)
                elif event[0] == "torrent_search_result":
                    _kind, search_results, search_error = event
                    self._finish_torrent_search(search_results, search_error)
                elif event[0] == "torrent_api_probe":
                    _kind, probe_value, probe_url, probe_error = event
                    self._finish_torrent_api_probe(probe_value, probe_url,
                                                   probe_error)
                elif event[0] == "torrent_api_check":
                    _kind, check_name, check_url, check_result = event
                    self._finish_torrent_api_check(check_name, check_url,
                                                   check_result)
                elif event[0] == "torrent_file_ready":
                    _kind, path, name, error = event
                    status = getattr(self, "torrent_search_status", None)
                    if error or not path:
                        if status is not None:
                            status.set(error or translate_ui(
                                "Impossibile scaricare il file torrent.",
                                self.language))
                    else:
                        try:
                            task = self.manager.add_torrent_file(path)
                        except ValueError as exc:
                            if status is not None:
                                status.set(str(exc))
                        else:
                            self._suppressed_clipboard_magnet = None
                            self.tasks_by_id[task.id] = task
                            self._render(task)
                            self.url_var.set("")
                            if status is not None:
                                status.set(translate_ui(
                                    "Aggiunto alla coda.", self.language))
                elif event[0] == "update_check_result":
                    _kind, info, error = event
                    self._finish_update_check(info, error)
                elif event[0] == "update_download_result":
                    _kind, path, error = event
                    self._finish_update_download(path, error)
                elif event[0] == "overwrite_prompt":
                    task = self.tasks_by_id.get(event[1])
                    if task:
                        task.overwrite_allowed = messagebox.askyesno(
                            translate_ui("Conferma sovrascrittura", self.language),
                            translate_ui("Il file esiste già:\n", self.language) + f"{os.path.basename(event[2])}\n\n" + translate_ui("\n\nVuoi sovrascriverlo?", self.language).replace("\n\n", ""),
                            parent=self.root)
                        task.overwrite_event.set()
                elif event[0] == "head":
                    task = self.tasks_by_id.get(event[1])
                    if task:
                        info = event[2]
                        if info.get("size") is not None:
                            task.total = info["size"]
                        if info.get("mime"):
                            task.mime = info["mime"]
                        if info.get("name") and task.state in ("In coda", "Annullato", "Errore"):
                            task.name = info["name"]
                        if info.get("error"):
                            task.error = f"HEAD: {info['error']}"
                        self._render(task)
            for ident in update_ids:
                task = self.tasks_by_id.get(ident)
                if task:
                    self._render(task)
        except queue.Empty:
            pass
        except Exception:
            # Un errore in un handler non deve interrompere il polling futuro.
            logging.exception("Errore durante la gestione degli eventi della GUI")
        finally:
            self.root.after(120, self._poll)


def _install_crash_logging(root=None):
    """Append uncaught Python and Tk callback exceptions to downloader_crash.log."""
    log_path = os.path.join(DATA_DIR, "downloader_crash.log")
    logging.basicConfig(filename=log_path, level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    logging.info("DOWNLOADVERSE startup; Python %s", sys.version.replace("\n", " "))

    def log_exception(exc_type, exc_value, exc_tb):
        logging.critical("Uncaught main-thread exception",
                         exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = log_exception
    if root is not None:
        root.report_callback_exception = log_exception


def _run_selftest():
    """Verifica rapida della build: import nativi e inizializzazione Tk."""
    import importlib
    _load_tkinter()
    for module_name in ("requests", "curl_cffi", "libtorrent", "pystray", "PIL.Image",
                        "Crypto.Cipher.AES"):
        importlib.import_module(module_name)
    plain = _decrypt_privatebin_payload(_SELFTEST_PRIVATEBIN_PAYLOAD,
                                        _SELFTEST_PRIVATEBIN_KEY)
    if "selftest.rar" not in plain:
        raise RuntimeError("Selftest PrivateBin fallito")
    for icon_name in ("downloadverse.png", "downloadverse.ico"):
        if not os.path.isfile(_app_resource_path(icon_name)):
            raise RuntimeError(f"Risorsa icona mancante: {icon_name}")
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    root.destroy()
    return 0


def main():
    _install_crash_logging()
    if ("--selftest" in sys.argv
            or os.environ.get("DOWNLOADVERSE_SELFTEST") == "1"
            or os.environ.get("GDOWNLOADER_SELFTEST") == "1"):
        try:
            return _run_selftest()
        except Exception:
            logging.exception("Selftest fallito")
            return 1
    try:
        _load_tkinter()
    except ImportError as exc:
        raise SystemExit(f"Tkinter/Tcl non disponibile: {exc}")
    root = tk.Tk()
    root.report_callback_exception = sys.excepthook
    DownloaderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
