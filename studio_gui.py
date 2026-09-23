"""Desktop front end for Genre MIDI Studio; standard-library dependencies only."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from genre_midi import __version__
from genre_midi.model import BAR, PPQ, Config, Song, ROLES
from genre_midi.note_automation import NOTE_MAX, build_note_lanes, preview_note_lane
from genre_midi.registry import get_profile, profile_fingerprint
from genre_midi.service import available_genres, create_song, export_song, inspect_sound_set, load_recipe


BG = "#0c131b"
PANEL = "#131e29"
FIELD = "#1c2a38"
LINE = "#2a3c4c"
TEXT = "#e5edf4"
MUTED = "#94a8ba"
ACCENT = "#38d5af"
CYAN = "#54d9ef"
AMBER = "#f2bd66"
SECTION_COLORS = {
    "intro": "#366475", "build": "#9d7443", "drop": "#268d7f",
    "breakdown": "#65588f", "outro": "#48617a",
}
ARRANGEMENTS = {
    "Full song · 128 bars": "full",
    "Compact song · 64 bars": "compact",
    "Drop sketch · 16 bars": "drop",
}
KICK_MODES = {"Pitched · follows the key": "pitched", "Fixed · sampler trigger": "fixed"}
KEYS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
LANE_ROLES = ROLES
NOTE_DENSITIES = {
    "Fine · 120 notes/beat · 8 ticks": 120,
    "Very fine · 240 notes/beat · 4 ticks": 240,
    "Extra fine · 480 notes/beat · 2 ticks": 480,
    "Maximum · 960 notes/beat · 1 tick": 960,
}
PREVIEW_VIEWS = ("Musical notes", "Tiny-note automation")


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str):
        self.widget, self.text = widget, text
        self.window = None
        self.timer = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _event=None):
        self.timer = self.widget.after(450, self.show)

    def show(self):
        self.timer = None
        if self.window is not None:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(
            f"+{self.widget.winfo_rootx() + 10}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 5}"
        )
        tk.Label(self.window, text=self.text, wraplength=310, justify="left", padx=12,
                 pady=9, bg=FIELD, fg=TEXT, relief="solid", borderwidth=1,
                 font=("Segoe UI", 9)).pack()

    def hide(self, _event=None):
        if self.timer is not None:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


class StudioApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Genre MIDI Studio")
        self.root.configure(bg=BG)
        screen_h = root.winfo_screenheight()
        screen_w = root.winfo_screenwidth()
        self.root.geometry(f"{min(1180, screen_w - 50)}x{min(880, screen_h - 90)}")
        self.root.minsize(960, 690)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.profiles = available_genres()
        if not self.profiles:
            raise ValueError("No genre profiles are installed.")
        self._events: queue.Queue = queue.Queue()
        self._busy = False
        self._closing = False
        self._applying = False
        self._refreshing_preview = False
        self._dirty = True
        self._controls: list[tuple[tk.Widget, str]] = []
        self._song: Song | None = None
        self._preview_config: str | None = None
        self._custom_sections = None
        self._sound_set: str | None = None
        self._lane_roles: dict[str, str] = {}
        self._selected_section = 0
        self._export_folder: Path | None = None
        self._style_names: dict[str, str] = {}
        self._note_lanes: list[dict] = []
        self._note_lane_names: dict[str, dict] = {}
        self._arrangement_names = dict(ARRANGEMENTS)
        self._genre_names = {profile["name"]: profile["id"] for profile in self.profiles}
        self._make_style()
        self._make_vars()
        self._make_ui()
        default = Config()
        if default.genre not in self._genre_names.values():
            first_profile = self.profiles[0]
            default.genre = first_profile["id"]
            default.style = next(iter(first_profile["styles"]))
            default.bpm = first_profile["bpm"]["default"]
            default.scale = first_profile["scales"][0]
        self._apply_config(default)
        for variable in self._config_vars:
            variable.trace_add("write", self._mark_dirty)
        self.automation_var.trace_add("write", self._automation_changed)
        self.note_density_var.trace_add("write", self._note_density_changed)
        self.preview_view_var.trace_add("write", self._preview_view_changed)
        self.automation_lane_var.trace_add("write", lambda *_args: self._draw_piano())
        self.preview_bar_var.trace_add("write", lambda *_args: self._draw_piano())
        self.root.after(80, self._poll)

    def _make_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"), foreground=TEXT)
        style.configure("Subtitle.TLabel", foreground=ACCENT, font=("Segoe UI", 10))
        style.configure("Heading.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("TButton", background=FIELD, foreground=TEXT, bordercolor=LINE,
                        focusthickness=0, padding=(12, 7), font=("Segoe UI", 9))
        style.map("TButton", background=[("active", "#2a4051"), ("disabled", PANEL)],
                  foreground=[("disabled", "#627586")])
        style.configure("Accent.TButton", background=ACCENT, foreground=BG, font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#72e4c8"), ("disabled", "#20483f")],
                  foreground=[("disabled", "#628d80")])
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT,
                        bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=6)
        style.configure("TCombobox", fieldbackground=FIELD, foreground=TEXT, arrowcolor=ACCENT,
                        background=FIELD, bordercolor=LINE, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
                  foreground=[("readonly", TEXT), ("disabled", "#627586")],
                  selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
        style.configure("TSpinbox", fieldbackground=FIELD, foreground=TEXT, arrowcolor=ACCENT,
                        background=FIELD, bordercolor=LINE, padding=5)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT, font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", PANEL)], foreground=[("disabled", MUTED)])
        style.configure("Horizontal.TScale", background=PANEL, troughcolor=FIELD, bordercolor=LINE)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        borderwidth=0, rowheight=26, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=FIELD, foreground=MUTED,
                        borderwidth=0, font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#214c4a")], foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor=FIELD, background=ACCENT,
                        borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#286456")
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))

    def _make_vars(self):
        self.genre_var = tk.StringVar()
        self.style_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.scale_var = tk.StringVar()
        self.bpm_var = tk.StringVar()
        self.arrangement_var = tk.StringVar()
        self.seed_var = tk.StringVar()
        self.energy_var = tk.DoubleVar()
        self.complexity_var = tk.DoubleVar()
        self.variation_var = tk.DoubleVar()
        self.kick_var = tk.StringVar()
        self.humanize_var = tk.StringVar()
        self.automation_var = tk.BooleanVar(value=True)
        self.note_density_var = tk.StringVar(value=next(name for name, value in NOTE_DENSITIES.items() if value == 240))
        self.preview_view_var = tk.StringVar(value=PREVIEW_VIEWS[0])
        self.automation_lane_var = tk.StringVar()
        self.preview_bar_var = tk.StringVar(value="1")
        self.note_detail_var = tk.StringVar(value="Straight-line tiny notes · C0–C10 + matching velocity")
        self.status_var = tk.StringVar(value="Choose a style, then generate a song preview.")
        self.preview_state_var = tk.StringVar(value="READY TO COMPOSE")
        self.metrics_var = tk.StringVar(value="Your song starts here")
        self.section_var = tk.StringVar(value="Arrange first. Inspect the notes. Export when ready.")
        self.custom_var = tk.StringVar(value="Preset arrangement")
        self.tempo_help_var = tk.StringVar()
        self.piano_title_var = tk.StringVar(value="NOTE PREVIEW")
        self.warning_var = tk.StringVar()
        self.soundset_var = tk.StringVar(value="Built-in musical lanes")
        self._config_vars = [self.genre_var, self.style_var, self.key_var, self.scale_var,
                             self.bpm_var, self.arrangement_var, self.seed_var, self.energy_var,
                             self.complexity_var, self.variation_var, self.kick_var, self.humanize_var]

    def _control(self, widget, normal="normal"):
        self._controls.append((widget, normal))
        return widget

    def _make_ui(self):
        header = ttk.Frame(self.root, padding=(24, 18, 24, 14))
        header.pack(fill="x")
        ttk.Label(header, text="Genre MIDI Studio", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="FULL SONG COMPOSITION  /  MUSICAL MIDI + TINY-NOTE AUTOMATION",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))
        body = ttk.Frame(self.root, padding=(20, 0, 20, 0))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # The controls scroll independently so smaller laptop screens keep the preview visible.
        side = ttk.Frame(body, style="Panel.TFrame")
        side.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        self.controls_canvas = tk.Canvas(side, bg=PANEL, width=312, highlightthickness=0)
        self.controls_canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(side, orient="vertical", command=self.controls_canvas.yview)
        scroll.pack(side="right", fill="y")
        self.controls_canvas.configure(yscrollcommand=scroll.set)
        controls = ttk.Frame(self.controls_canvas, padding=15, style="Panel.TFrame")
        window = self.controls_canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", lambda _e: self.controls_canvas.configure(scrollregion=self.controls_canvas.bbox("all")))
        self.controls_canvas.bind("<Configure>", lambda e: self.controls_canvas.itemconfigure(window, width=e.width))
        self.controls_canvas.bind("<MouseWheel>", self._scroll_controls)
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        def label(text, row, column=0, colspan=2):
            ttk.Label(controls, text=text, style="PanelMuted.TLabel").grid(
                row=row, column=column, columnspan=colspan, sticky="w", pady=(9, 4))

        def combo(variable, values, row, column=0, colspan=2, command=None):
            widget = self._control(ttk.Combobox(controls, textvariable=variable, values=values,
                                                state="readonly", width=18), "readonly")
            widget.grid(row=row, column=column, columnspan=colspan, sticky="ew",
                        padx=(0, 5) if colspan == 1 and column == 0 else 0)
            if command:
                widget.bind("<<ComboboxSelected>>", command)
            return widget

        label("GENRE", 0)
        self.genre_combo = combo(self.genre_var, list(self._genre_names), 1, command=self._genre_changed)
        label("STYLE", 2)
        self.style_combo = combo(self.style_var, [], 3)
        self.style_combo.bind("<<ComboboxSelected>>", self._style_changed)
        label("KEY", 4, 0, 1)
        label("SCALE", 4, 1, 1)
        self.key_combo = combo(self.key_var, KEYS, 5, 0, 1)
        self.scale_combo = combo(self.scale_var, [], 5, 1, 1)
        label("TEMPO · BPM", 6)
        self.bpm_entry = self._control(ttk.Spinbox(controls, textvariable=self.bpm_var,
                                                   from_=130, to=180, increment=1, width=12))
        self.bpm_entry.grid(row=7, column=0, sticky="ew", padx=(0, 5))
        ttk.Label(controls, textvariable=self.tempo_help_var, style="PanelMuted.TLabel").grid(
            row=7, column=1, sticky="w")
        label("ARRANGEMENT", 8)
        self.arrangement_combo = combo(self.arrangement_var, list(ARRANGEMENTS), 9)
        arrangement_info = ttk.Frame(controls, style="Panel.TFrame")
        arrangement_info.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        ttk.Label(arrangement_info, textvariable=self.custom_var, style="PanelMuted.TLabel").pack(side="left")
        self.preset_button = ttk.Button(arrangement_info, text="Use preset", command=self._use_preset, padding=(6, 2))
        self.preset_button.pack(side="right")

        def slider(title, variable, row, tooltip):
            holder = ttk.Frame(controls, style="Panel.TFrame")
            holder.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 0))
            ttk.Label(holder, text=title, style="PanelMuted.TLabel").pack(side="left")
            number = ttk.Label(holder, text="0%", style="PanelMuted.TLabel")
            number.pack(side="right")
            variable.trace_add("write", lambda *_args: number.configure(text=f"{variable.get():.0%}"))
            scale = self._control(ttk.Scale(controls, variable=variable, from_=0, to=1))
            scale.grid(row=row + 1, column=0, columnspan=2, sticky="ew")
            Tooltip(scale, tooltip)

        slider("ENERGY", self.energy_var, 11, "Shapes section intensity, accents and arrangement layers; the song still includes deliberate quieter passages.")
        slider("NOTE DENSITY", self.complexity_var, 13, "Controls melodic and rhythmic detail. Higher values add busier phrases and ornamentation.")
        slider("VARIATION", self.variation_var, 15, "Controls phrase changes and fills while keeping the song's recurring musical identity.")
        label("SEED · REPEATABLE COMPOSITION", 17)
        self._control(ttk.Entry(controls, textvariable=self.seed_var, width=16)).grid(
            row=18, column=0, sticky="ew", padx=(0, 5))
        self.new_seed_button = self._control(ttk.Button(controls, text="New seed", command=self._new_seed))
        self.new_seed_button.grid(row=18, column=1, sticky="ew")
        label("KICK MAPPING", 19)
        kick_combo = combo(self.kick_var, list(KICK_MODES), 20)
        Tooltip(kick_combo, "Pitched: MIDI notes follow the musical key; use a pitch-tracking sampler with its root note configured. Fixed: use the sound set's stored kick trigger, or note 36 for built-in lanes. Assign your own hardstyle kick samples.")
        label("HUMANIZE · MILLISECONDS", 21)
        self._control(ttk.Spinbox(controls, textvariable=self.humanize_var, from_=0, to=15,
                                  increment=0.5, width=10)).grid(row=22, column=0, sticky="ew", padx=(0, 5))
        ttk.Label(controls, text="0 = exact grid", style="PanelMuted.TLabel").grid(row=22, column=1, sticky="w")
        soundset_frame = ttk.Frame(controls, style="Panel.TFrame")
        soundset_frame.grid(row=23, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Label(soundset_frame, text="SOUND SET · MIDI INSTRUMENT LANES", style="PanelMuted.TLabel").pack(anchor="w")
        ttk.Label(soundset_frame, textvariable=self.soundset_var, style="Panel.TLabel", wraplength=264).pack(anchor="w", pady=(4, 6))
        soundset_buttons = ttk.Frame(soundset_frame, style="Panel.TFrame")
        soundset_buttons.pack(fill="x")
        self._control(ttk.Button(soundset_buttons, text="Load MIDI", command=self._load_sound_set, padding=(8, 5))).pack(side="left", padx=(0, 5))
        self.map_button = ttk.Button(soundset_buttons, text="Map roles", command=self._edit_sound_set, padding=(8, 5))
        self.map_button.pack(side="left", padx=(0, 5))
        self.clear_soundset_button = ttk.Button(soundset_buttons, text="Clear", command=self._clear_sound_set, padding=(8, 5))
        self.clear_soundset_button.pack(side="left")
        self.automation_check = self._control(ttk.Checkbutton(controls, text="Include tiny-note automation",
                                                              variable=self.automation_var))
        self.automation_check.grid(row=24, column=0, columnspan=2, sticky="w", pady=(15, 4))
        label("STRAIGHT-LINE AUTOMATION · TINY NOTES", 25)
        self.density_combo = combo(self.note_density_var, list(NOTE_DENSITIES), 26)
        Tooltip(self.density_combo, "Automation uses tiny MIDI notes: C0 = 0%, C5 = 50%, C10 = 100%. Pitch and velocity follow the same percentage. Velocity uses 1 at the zero end because MIDI velocity 0 turns a note off. Every quarter-note beat has this many notes, including flat sections; adjacent notes are never merged. Each transition is a straight ramp.")
        recipes = ttk.Frame(controls, style="Panel.TFrame")
        recipes.grid(row=27, column=0, columnspan=2, sticky="ew", pady=(9, 0))
        self._control(ttk.Button(recipes, text="Load recipe", command=self._load)).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._control(ttk.Button(recipes, text="Save recipe", command=self._save)).pack(side="left", fill="x", expand=True)
        ttk.Label(controls, text="Music and automation use separate MIDI files.\nAssign synths and drum samples in your DAW.",
                  style="PanelMuted.TLabel", wraplength=270, justify="left").grid(
            row=28, column=0, columnspan=2, sticky="w", pady=(15, 4))
        # Sound-set selection is a primary input and stays at the top of the settings.
        for widget in controls.winfo_children():
            if widget is not soundset_frame and widget.winfo_manager() == "grid":
                widget.grid_configure(row=int(widget.grid_info()["row"]) + 1)
        soundset_frame.grid_configure(row=0, pady=(0, 10))
        # Binding each child avoids global mouse-wheel handlers affecting other windows.
        def bind_scroll(parent):
            for widget in parent.winfo_children():
                if not isinstance(widget, (ttk.Combobox, ttk.Spinbox, ttk.Scale)):
                    widget.bind("<MouseWheel>", self._scroll_controls, add="+")
                bind_scroll(widget)
        bind_scroll(controls)

        preview = ttk.Frame(body)
        preview.grid(row=0, column=1, sticky="nsew")
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(5, weight=1)
        preview.rowconfigure(8, weight=2)
        topline = ttk.Frame(preview)
        topline.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.state_label = ttk.Label(topline, textvariable=self.preview_state_var,
                                    foreground=ACCENT, font=("Segoe UI", 9, "bold"))
        self.state_label.pack(side="left")
        self.generate_button = ttk.Button(topline, text="Generate preview", style="Accent.TButton", command=self._generate)
        self.generate_button.pack(side="right")
        ttk.Label(preview, textvariable=self.metrics_var, style="Heading.TLabel").grid(row=1, column=0, sticky="w")
        self.section_label = ttk.Label(preview, textvariable=self.section_var, style="Muted.TLabel", wraplength=700)
        self.section_label.grid(row=2, column=0, sticky="w", pady=(4, 8))
        self.arrangement_canvas = tk.Canvas(preview, bg=PANEL, height=75, highlightthickness=0)
        self.arrangement_canvas.grid(row=3, column=0, sticky="ew")
        self.arrangement_canvas.bind("<Configure>", lambda _e: self._draw_arrangement())
        self.arrangement_canvas.bind("<Button-1>", self._select_section)
        self.track_list_heading = ttk.Label(preview, text="TRACKS  ·  Select a track and section to inspect its notes", style="Muted.TLabel")
        self.track_list_heading.grid(row=4, column=0, sticky="w", pady=(15, 6))
        tracks_frame = ttk.Frame(preview)
        self.tracks_frame = tracks_frame
        tracks_frame.grid(row=5, column=0, sticky="nsew")
        self.track_tree = ttk.Treeview(tracks_frame, columns=("name", "role", "channel", "notes"),
                                       show="headings", selectmode="browse", height=4)
        for key, title, width, anchor in (("name", "Track", 215, "w"), ("role", "Role", 125, "w"),
                                           ("channel", "Channel", 60, "center"), ("notes", "Notes", 70, "e")):
            self.track_tree.heading(key, text=title, anchor=anchor)
            self.track_tree.column(key, width=width, minwidth=50, anchor=anchor, stretch=key in ("name", "role"))
        self.track_tree.pack(side="left", fill="both", expand=True)
        tree_scroll = ttk.Scrollbar(tracks_frame, orient="vertical", command=self.track_tree.yview)
        tree_scroll.pack(side="right", fill="y")
        self.track_tree.configure(yscrollcommand=tree_scroll.set)
        self.track_tree.bind("<<TreeviewSelect>>", lambda _e: self._draw_piano())
        preview_controls = ttk.Frame(preview)
        preview_controls.grid(row=6, column=0, sticky="ew", pady=(10, 4))
        preview_controls.columnconfigure(1, weight=1)
        self.preview_view_combo = ttk.Combobox(preview_controls, textvariable=self.preview_view_var,
                                               values=PREVIEW_VIEWS, state="readonly", width=20)
        self.preview_view_combo.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.automation_lane_combo = ttk.Combobox(preview_controls, textvariable=self.automation_lane_var,
                                                  state="disabled", width=22)
        self.automation_lane_combo.grid(row=0, column=1, sticky="ew")
        self.note_preview_options = ttk.Frame(preview_controls)
        self.note_preview_options.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        ttk.Label(self.note_preview_options, text="2-bar view · start bar", style="Muted.TLabel").pack(side="left")
        self.preview_bar_spin = ttk.Spinbox(self.note_preview_options, textvariable=self.preview_bar_var,
                                            from_=1, to=1, width=5)
        self.preview_bar_spin.pack(side="left", padx=(6, 9))
        ttk.Label(self.note_preview_options, textvariable=self.note_detail_var, style="Muted.TLabel",
                  wraplength=400).pack(side="left", fill="x", expand=True)
        self.note_preview_options.grid_remove()
        self.piano_title_label = ttk.Label(preview, textvariable=self.piano_title_var, style="Muted.TLabel", wraplength=690)
        self.piano_title_label.grid(row=7, column=0, sticky="w", pady=(3, 6))
        self.piano_canvas = tk.Canvas(preview, bg=PANEL, height=180, highlightthickness=0)
        self.piano_canvas.grid(row=8, column=0, sticky="nsew")
        self.piano_canvas.bind("<Configure>", lambda _e: self._draw_piano())
        self.warning_label = ttk.Label(preview, textvariable=self.warning_var, foreground=AMBER, wraplength=710,
                                       font=("Segoe UI", 9))
        self.warning_label.grid(row=9, column=0, sticky="w", pady=(8, 0))
        preview.bind("<Configure>", lambda event: (self.section_label.configure(wraplength=max(250, event.width - 5)),
                                                   self.warning_label.configure(wraplength=max(250, event.width - 5)),
                                                   self.piano_title_label.configure(wraplength=max(250, event.width - 5))))
        footer = ttk.Frame(self.root, padding=(20, 12, 20, 15))
        footer.pack(side="bottom", fill="x", before=body)
        footer.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=140)
        self.progress.grid(row=0, column=0, sticky="ew", columnspan=3, pady=(0, 9))
        self.status_label = ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel", wraplength=620)
        self.status_label.grid(row=1, column=0, sticky="w", padx=(0, 15))
        self.folder_button = ttk.Button(footer, text="Open export folder", command=self._open_folder)
        self.folder_button.grid(row=1, column=1, padx=(0, 8))
        self.export_button = ttk.Button(footer, text="Export song pack", style="Accent.TButton", command=self._export)
        self.export_button.grid(row=1, column=2)
        footer.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(240, event.width - 370)))
        self._sync_buttons()

    def _scroll_controls(self, event):
        self.controls_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _profile(self):
        genre = self._genre_names.get(self.genre_var.get())
        return next((p for p in self.profiles if p["id"] == genre), self.profiles[0])

    def _genre_changed(self, _event=None):
        profile = self._profile()
        styles = profile["styles"]
        self._style_names = {value.get("name", key.title()): key for key, value in styles.items()}
        self.style_combo.configure(values=list(self._style_names))
        if self.style_var.get() not in self._style_names:
            self.style_var.set(next(iter(self._style_names)))
        scales = profile.get("scales", ["minor", "harmonic-minor"])
        self.scale_combo.configure(values=scales)
        if self.scale_var.get() not in scales:
            self.scale_var.set(scales[0])
        selected_style = styles[self._style_names[self.style_var.get()]]
        tempo = selected_style.get("bpm", profile["bpm"])
        self.bpm_entry.configure(from_=tempo["min"], to=tempo["max"])
        self.tempo_help_var.set(f"{tempo['min']}–{tempo['max']} BPM · set playback tempo in DAW")
        previous_arrangement = self._arrangement_names.get(self.arrangement_var.get(), "full")
        presets = profile.get("arrangements", {})
        names = {"full": "Full song", "compact": "Compact song", "drop": "Drop sketch"}
        def preset_bars(sections):
            return sum(selected_style.get("breakdown_bars", section["bars"])
                       if section["kind"] == "breakdown" else section["bars"] for section in sections)
        if presets:
            self._arrangement_names = {
                f"{names.get(key, key.replace('-', ' ').title())} · {preset_bars(sections)} bars": key
                for key, sections in presets.items()
            }
        else:
            self._arrangement_names = dict(ARRANGEMENTS)
        self.arrangement_combo.configure(values=list(self._arrangement_names))
        self.arrangement_var.set(next((name for name, key in self._arrangement_names.items() if key == previous_arrangement), next(iter(self._arrangement_names))))
        if not self._applying:
            self.bpm_var.set(str(tempo["default"]))
        self._mark_dirty()

    def _style_changed(self, _event=None):
        self._genre_changed()

    def _apply_config(self, config: Config):
        if config.genre not in self._genre_names.values():
            raise ValueError(f"The genre '{config.genre}' is not installed.")
        profile = next(p for p in self.profiles if p["id"] == config.genre)
        if config.style not in profile["styles"]:
            raise ValueError(f"The style '{config.style}' is not installed for this genre.")
        if config.scale not in profile.get("scales", ("minor", "harmonic-minor")):
            raise ValueError(f"The scale '{config.scale}' is not supported by this genre.")
        self._applying = True
        try:
            self.genre_var.set(next(name for name, key in self._genre_names.items() if key == config.genre))
            self._genre_changed()
            if config.style not in self._style_names.values():
                raise ValueError(f"The style '{config.style}' is not installed for this genre.")
            self.style_var.set(next(name for name, key in self._style_names.items() if key == config.style))
            self._style_changed()
            if config.key not in KEYS:
                self.key_combo.configure(values=(*KEYS, config.key))
            self.key_var.set(config.key)
            self.scale_var.set(config.scale)
            self.bpm_var.set(f"{config.bpm:g}")
            self.arrangement_var.set(next(name for name, key in self._arrangement_names.items() if key == config.arrangement))
            self.seed_var.set(str(config.seed))
            self.energy_var.set(config.energy)
            self.complexity_var.set(config.complexity)
            self.variation_var.set(config.variation)
            self.kick_var.set(next(name for name, key in KICK_MODES.items() if key == config.kick_mode))
            self.humanize_var.set(f"{config.humanize_ms:g}")
            self._custom_sections = json.loads(json.dumps(config.sections)) if config.sections is not None else None
            self._sound_set = config.sound_set
            self._lane_roles = dict(config.lane_roles)
            self._update_soundset_label()
            self._update_custom_label()
        finally:
            self._applying = False
        self._mark_dirty()

    def _update_custom_label(self):
        if self._custom_sections is None:
            self.custom_var.set("Preset arrangement")
        else:
            bars = sum(section["bars"] for section in self._custom_sections)
            self.custom_var.set(f"Recipe: {len(self._custom_sections)} sections · {bars} bars")
        self._sync_buttons()

    def _use_preset(self):
        self._custom_sections = None
        self._update_custom_label()
        self._mark_dirty()

    def _config(self) -> Config:
        try:
            config = Config(
                genre=self._genre_names[self.genre_var.get()], style=self._style_names[self.style_var.get()],
                key=self.key_var.get(), scale=self.scale_var.get(), bpm=float(self.bpm_var.get()),
                arrangement=self._arrangement_names[self.arrangement_var.get()], seed=int(self.seed_var.get()),
                energy=round(self.energy_var.get(), 4), complexity=round(self.complexity_var.get(), 4),
                variation=round(self.variation_var.get(), 4), kick_mode=KICK_MODES[self.kick_var.get()],
                humanize_ms=float(self.humanize_var.get()),
                sections=json.loads(json.dumps(self._custom_sections)) if self._custom_sections is not None else None,
                sound_set=self._sound_set, lane_roles=dict(self._lane_roles),
            ).validate()
        except (ValueError, KeyError, tk.TclError) as error:
            raise ValueError(f"Please check your settings. {error}") from error
        limits = self._profile()["bpm"]
        if not limits["min"] <= config.bpm <= limits["max"]:
            raise ValueError(f"{self._profile()['name']} tempo must be {limits['min']}–{limits['max']} BPM.")
        return config

    @staticmethod
    def _fingerprint(config: Config) -> str:
        # Numeric equality should survive recipe files that write 150 versus 150.0.
        data = config.to_dict()
        for field in ("bpm", "energy", "complexity", "variation", "humanize_ms"):
            data[field] = float(data[field])
        if data["sections"] is not None:
            for section in data["sections"]:
                if "energy" in section:
                    section["energy"] = float(section["energy"])
        return json.dumps(data, sort_keys=True)

    def _mark_dirty(self, *_args):
        if self._applying:
            return
        try:
            self._dirty = self._fingerprint(self._config()) != self._preview_config
        except ValueError:
            self._dirty = True
        if self._song is None:
            self.preview_state_var.set("READY TO COMPOSE")
            self.state_label.configure(foreground=ACCENT)
        elif self._dirty:
            self.preview_state_var.set("SETTINGS CHANGED · GENERATE TO UPDATE")
            self.state_label.configure(foreground=AMBER)
            self.status_var.set("The visible preview uses earlier settings. Generate again before exporting.")
        else:
            self.preview_state_var.set("PREVIEW READY")
            self.state_label.configure(foreground=ACCENT)
        self._sync_buttons()

    def _automation_changed(self, *_args):
        if self._song is not None and not self._dirty:
            suffix = (f"with straight-line tiny-note automation ({self._note_density()} notes per beat)"
                      if self.automation_var.get() else "without automation")
            self.status_var.set(f"Export will use this exact song preview, {suffix}.")

    def _note_density(self) -> int:
        return NOTE_DENSITIES.get(self.note_density_var.get(), 240)

    def _note_density_changed(self, *_args):
        self._automation_changed()
        self._draw_piano()

    def _preview_view_changed(self, *_args):
        automation = self.preview_view_var.get() == PREVIEW_VIEWS[1]
        self.automation_lane_combo.configure(state="readonly" if automation and self._note_lanes else "disabled")
        if automation:
            self.note_preview_options.grid()
            self.track_list_heading.grid_remove()
            self.tracks_frame.grid_remove()
            self.warning_label.grid_remove()
        else:
            self.note_preview_options.grid_remove()
            self.track_list_heading.grid()
            self.tracks_frame.grid()
            self.warning_label.grid()
        self._draw_piano()

    def _new_seed(self):
        self.seed_var.set(str(secrets.randbelow(4294967296)))

    def _sync_buttons(self):
        if not hasattr(self, "export_button"):
            return
        self.generate_button.configure(state="disabled" if self._busy else "normal")
        self.export_button.configure(state="normal" if self._song is not None and not self._dirty and not self._busy else "disabled")
        self.folder_button.configure(state="normal" if self._export_folder is not None and not self._busy else "disabled")
        self.preset_button.configure(state="normal" if self._custom_sections is not None and not self._busy else "disabled")
        self.arrangement_combo.configure(state="disabled" if self._custom_sections is not None or self._busy else "readonly")
        self.map_button.configure(state="normal" if self._sound_set is not None and not self._busy else "disabled")
        self.clear_soundset_button.configure(state="normal" if self._sound_set is not None and not self._busy else "disabled")

    def _start_worker(self, task, label):
        if self._busy:
            return
        self._busy = True
        for widget, _state in self._controls:
            widget.configure(state="disabled")
        self._sync_buttons()
        self.status_var.set(label)
        self.progress.start(12)

        def run():
            try:
                result = task()
                self._events.put(("ok", result))
            except Exception as error:
                self._events.put(("error", error))
        threading.Thread(target=run, name="genre-midi-worker", daemon=True).start()

    def _poll(self):
        try:
            kind, result = self._events.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy = False
            self.progress.stop()
            self.progress.configure(value=0)
            for widget, normal in self._controls:
                widget.configure(state=normal)
            if kind == "error":
                self.status_var.set("The operation could not be completed. Check the message and try again.")
                if not self._closing:
                    messagebox.showerror("Genre MIDI Studio", str(result), parent=self.root)
            elif result[0] == "preview":
                self._show_song(result[1])
            elif result[0] == "export":
                self._export_folder = result[1]
                count = len(list(self._export_folder.rglob("*.mid")))
                self.status_var.set(f"Exported {count} MIDI files and supporting song documents to {self._export_folder.name}.")
            elif result[0] == "sound_set" and not self._closing:
                self._show_role_dialog(result[1])
            self._sync_buttons()
            if self._closing:
                self.root.destroy()
                return
        self.root.after(80, self._poll)

    def _generate(self):
        try:
            config = self._config()
        except ValueError as error:
            messagebox.showerror("Check song settings", str(error), parent=self.root)
            return
        self._start_worker(lambda: ("preview", create_song(config)), "Composing the song and arranging its musical tracks…")

    def _show_song(self, song: Song):
        note_lanes = build_note_lanes(song, get_profile(song.config.genre))
        self._refreshing_preview = True
        self._song = song
        self._note_lanes = note_lanes
        counts = {}
        for lane in note_lanes:
            counts[lane["name"]] = counts.get(lane["name"], 0) + 1
        self._note_lane_names = {
            (lane["name"] if counts[lane["name"]] == 1 else f"{lane['name']} [{lane.get('source_track_id', lane['id'])}]"): lane
            for lane in note_lanes
        }
        self.automation_lane_combo.configure(values=list(self._note_lane_names))
        if self.automation_lane_var.get() not in self._note_lane_names:
            self.automation_lane_var.set(next(iter(self._note_lane_names), ""))
        self.preview_bar_spin.configure(to=song.bars)
        self.preview_bar_var.set("1")
        self._preview_config = self._fingerprint(song.config)
        self._dirty = False
        self._selected_section = 0
        seconds = round(song.bars * 4 * 60 / song.config.bpm)
        self.metrics_var.set(f"{song.bars} bars   ·   {seconds // 60}:{seconds % 60:02d}   ·   {song.config.key} {song.config.scale}   ·   {song.config.bpm:g} BPM")
        self.section_var.set(f"{len(song.sections)} sections · {len(song.tracks)} musical tracks · {sum(len(t.notes) for t in song.tracks):,} notes. Click a section below.")
        self.track_tree.delete(*self.track_tree.get_children())
        for index, track in enumerate(song.tracks):
            self.track_tree.insert("", "end", iid=str(index), values=(track.name, track.role, track.channel, f"{len(track.notes):,}"))
        if song.tracks:
            first = next((index for index, track in enumerate(song.tracks) if "lead" in track.id.lower()), 0)
            self.track_tree.selection_set(str(first))
            self.track_tree.see(str(first))
        warning_text = "  ·  ".join(song.warnings[:2])
        if len(warning_text) > 280:
            warning_text = warning_text[:277] + "…"
        if len(song.warnings) > 2:
            warning_text += f"  (+{len(song.warnings) - 2} more; full warnings in exported report.json.)"
        self.warning_var.set(warning_text)
        self._refreshing_preview = False
        self._mark_dirty()
        self.status_var.set(f"Preview ready. Seed {song.config.seed} reproduces this composition with the same settings and generator version.")
        self._draw_arrangement()
        self._preview_view_changed()

    def _draw_arrangement(self):
        canvas = self.arrangement_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 100)
        if self._song is None:
            canvas.create_text(width / 2, 37, text="Intro → build → drop → breakdown → final drop → outro", fill=MUTED, font=("Segoe UI", 10))
            return
        song = self._song
        for index, section in enumerate(song.sections):
            x0 = 8 + (width - 16) * section.start_bar / song.bars
            x1 = 8 + (width - 16) * section.end_bar / song.bars
            selected = index == self._selected_section
            canvas.create_rectangle(x0 + 1, 8, x1 - 1, 52, fill=SECTION_COLORS.get(section.kind, "#405d6d"),
                                    outline=ACCENT if selected else PANEL, width=2 if selected else 1)
            label = section.name if x1 - x0 > 90 else section.kind.capitalize()
            available_chars = max(2, int((x1 - x0 - 8) / 6.8))
            if len(label) > available_chars:
                label = label[:max(1, available_chars - 1)] + "…"
            canvas.create_text((x0 + x1) / 2, 29, text=label, fill=TEXT, font=("Segoe UI", 9, "bold"))
            if x1 - x0 > 30:
                canvas.create_text(x0 + 3, 64, text=str(section.start_bar + 1), fill=MUTED, anchor="w", font=("Segoe UI", 8))

    def _select_section(self, event):
        if self._song is None:
            return
        width = max(self.arrangement_canvas.winfo_width() - 16, 1)
        bar = max(0, min(self._song.bars - 0.001, (event.x - 8) / width * self._song.bars))
        self._selected_section = next((i for i, section in enumerate(self._song.sections) if section.start_bar <= bar < section.end_bar), 0)
        self.preview_bar_var.set(str(self._song.sections[self._selected_section].start_bar + 1))
        self._draw_arrangement()
        self._draw_piano()

    def _draw_piano(self):
        if self._refreshing_preview:
            return
        if self.preview_view_var.get() == PREVIEW_VIEWS[1]:
            self._draw_note_automation()
            return
        canvas = self.piano_canvas
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 100), max(canvas.winfo_height(), 60)
        selection = self.track_tree.selection()
        if self._song is None or not selection:
            canvas.create_text(width / 2, height / 2, text="Generate a preview to explore the musical notes.", fill=MUTED, font=("Segoe UI", 10))
            return
        song = self._song
        track = song.tracks[int(selection[0])]
        section = song.sections[self._selected_section]
        bars = min(8, section.bars)
        start, end = section.start_bar * BAR, (section.start_bar + bars) * BAR
        notes = [note for note in track.notes if note.start < end and note.end > start]
        self.piano_title_var.set(f"{track.name.upper()}  ·  {section.name}  ·  bars {section.start_bar + 1}–{section.start_bar + bars}" + (" (first 8)" if section.bars > 8 else ""))
        pitches = [note.pitch for note in notes]
        low, high = (min(pitches) - 1, max(pitches) + 1) if pitches else (48, 72)
        low, high = max(0, low), min(127, high)
        if high - low < 8:
            low, high = max(0, low - 4), min(127, high + 4)
        left, top, bottom = 42, 30, height - 22
        usable_w, usable_h = width - left - 10, max(20, bottom - top)
        row_h = usable_h / (high - low + 1)
        for pitch in range(low, high + 1):
            y0 = top + (high - pitch) * row_h
            if pitch % 12 in (1, 3, 6, 8, 10):
                canvas.create_rectangle(left, y0, width - 10, y0 + row_h, fill="#101923", outline="")
            if pitch % 12 == 0 or (high - low < 15 and pitch % 12 in (4, 7)):
                names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
                canvas.create_text(left - 7, y0 + row_h / 2, text=f"{names[pitch % 12]}{pitch // 12 - 1}",
                                   anchor="e", fill=MUTED, font=("Segoe UI", 8))
                canvas.create_line(left, y0, width - 10, y0, fill="#223140")
        chord_lookup = {chord.bar: chord.name for chord in song.chords}
        previous_chord = ""
        for chord in song.chords:
            if chord.bar <= section.start_bar:
                previous_chord = chord.name
        for bar in range(bars + 1):
            x = left + usable_w * bar / bars
            canvas.create_line(x, top, x, bottom, fill=LINE)
            if bar < bars:
                absolute_bar = section.start_bar + bar
                previous_chord = chord_lookup.get(absolute_bar, previous_chord)
                canvas.create_text(x + 5, 14, text=previous_chord, anchor="w", fill=ACCENT, font=("Segoe UI", 9, "bold"))
                canvas.create_text(x + 5, height - 11, text=str(absolute_bar + 1), anchor="w", fill=MUTED, font=("Segoe UI", 8))
                for beat in (1, 2, 3):
                    beat_x = x + usable_w / bars * beat / 4
                    canvas.create_line(beat_x, top, beat_x, bottom, fill="#1c2a36")
        for note in notes:
            x0 = left + usable_w * (max(start, note.start) - start) / (end - start)
            x1 = left + usable_w * (min(end, note.end) - start) / (end - start)
            y0 = top + (high - note.pitch) * row_h
            green = 135 + round(note.velocity / 127 * 78)
            color = f"#38{green:02x}af"
            canvas.create_rectangle(x0, y0 + 1, max(x0 + 2, x1 - 0.5), y0 + max(2, row_h - 1), fill=color, outline="")
        if not notes:
            canvas.create_text(left + usable_w / 2, top + usable_h / 2, text="This track rests in this section.",
                               fill=MUTED, font=("Segoe UI", 10))

    def _draw_note_automation(self):
        canvas = self.piano_canvas
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 100), max(canvas.winfo_height(), 60)
        lane = self._note_lane_names.get(self.automation_lane_var.get())
        if self._song is None or lane is None:
            self.piano_title_var.set("TINY-NOTE AUTOMATION")
            message = ("Generate a preview to inspect the tiny MIDI notes." if self._song is None
                       else "This sound set has no lead, bass or pad automation receivers.")
            canvas.create_text(width / 2, height / 2, text=message, fill=MUTED,
                               font=("Segoe UI", 10), width=max(80, width - 30))
            return
        song = self._song
        try:
            start_bar = max(0, min(song.bars - 1, int(self.preview_bar_var.get()) - 1))
        except ValueError:
            return  # A temporarily blank spinbox is valid while entering a bar number.
        bars = min(2, song.bars - start_bar)
        density = self._note_density()
        step = PPQ // density
        notes = preview_note_lane(song, lane, density=density, start_bar=start_bar, bars=bars)
        lane_title = lane["name"] if len(lane["name"]) <= 48 else lane["name"][:47] + "…"
        self.piano_title_var.set(f"{lane_title}  ·  bars {start_bar + 1}–{start_bar + bars}")
        note_ms = step / PPQ * 60000 / song.config.bpm
        ticks_label = "tick" if step == 1 else "ticks"
        self.note_detail_var.set(f"Up to {step} {ticks_label} / {note_ms:.3g} ms · C0–C10 + matching velocity")
        left, top = 81, 14
        velocity_bottom, velocity_top = height - 23, height - 50
        bottom = velocity_top - 14
        usable_w, usable_h = max(20, width - left - 10), max(20, bottom - top)
        row_h = usable_h / (NOTE_MAX + 1)
        # Keep the actual MIDI pitch rows: no interpolated line replaces the notes.
        for pitch in range(NOTE_MAX + 1):
            y = top + (NOTE_MAX - pitch) * row_h
            if pitch in (0, 30, 60, 90, NOTE_MAX):
                canvas.create_line(left, y + row_h / 2, left + usable_w, y + row_h / 2,
                                   fill=LINE, tags=("automation_grid",))
                label = {0: "C0 · 0%", 60: "C5 · 50%", NOTE_MAX: "C10 · 100%"}.get(pitch, f"{pitch / NOTE_MAX:.0%}")
                canvas.create_text(left - 7, y + row_h / 2, text=label, anchor="e",
                                   fill=MUTED, font=("Segoe UI", 8))
        canvas.create_text(left - 7, (velocity_top + velocity_bottom) / 2, text="Velocity", anchor="e",
                           fill=MUTED, font=("Segoe UI", 8))
        canvas.create_rectangle(left, velocity_top, left + usable_w, velocity_bottom,
                                fill="#101923", outline=LINE, tags=("automation_grid",))
        start, end = start_bar * BAR, (start_bar + bars) * BAR
        for beat in range(bars * 4 + 1):
            x = left + usable_w * beat / (bars * 4)
            canvas.create_line(x, top, x, top + usable_h, fill=LINE if beat % 4 == 0 else "#1c2a36",
                               tags=("automation_grid",))
            if beat < bars * 4:
                canvas.create_text(x + 3, height - 11, text=f"{start_bar + beat // 4 + 1}.{beat % 4 + 1}",
                                   anchor="w", fill=MUTED, font=("Segoe UI", 8))
        for note in notes:
            note_start, note_end = note["start"], note["start"] + note["duration"]
            x0 = left + usable_w * (max(start, note_start) - start) / (end - start)
            x1 = left + usable_w * (min(end, note_end) - start) / (end - start)
            y0 = top + (NOTE_MAX - note["pitch"]) * row_h
            canvas.create_rectangle(x0, y0, x1, y0 + row_h, fill=CYAN, outline="",
                                    tags=("automation_note",))
            velocity_y = velocity_bottom - (velocity_bottom - velocity_top) * note["velocity"] / 127
            canvas.create_rectangle(x0, velocity_y, x1, velocity_bottom, fill=ACCENT, outline="",
                                    tags=("automation_velocity",))

    def _load(self):
        path = filedialog.askopenfilename(parent=self.root, title="Load a song recipe", filetypes=[("Song recipes", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            config = load_recipe(Path(path))
            envelope = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            density = envelope.get("note_density", 240)
            if type(density) is not int or density not in NOTE_DENSITIES.values():
                raise ValueError("Recipe note_density must be 120, 240, 480 or 960 notes per beat.")
            self._apply_config(config)
            self.note_density_var.set(next(name for name, value in NOTE_DENSITIES.items() if value == density))
        except Exception as error:
            messagebox.showerror("Could not load recipe", str(error), parent=self.root)
            return
        self.status_var.set(f"Loaded {Path(path).name}. Generate a preview to compose this recipe.")

    def _update_soundset_label(self):
        if self._sound_set is None:
            self.soundset_var.set("Built-in musical lanes")
        else:
            used = sum(role != "skip" for role in self._lane_roles.values())
            self.soundset_var.set(f"{Path(self._sound_set).name}\n{used} mapped instrument lanes")

    def _clear_sound_set(self):
        self._sound_set = None
        self._lane_roles = {}
        self._update_soundset_label()
        self._mark_dirty()
        self.status_var.set("Using the built-in musical lanes. Generate to refresh the preview.")

    def _load_sound_set(self):
        path = filedialog.askopenfilename(parent=self.root, title="Load a MIDI sound set", filetypes=[("MIDI instrument lanes", "*.mid *.midi"), ("All files", "*.*")])
        if path:
            self._inspect_sound_set(Path(path))

    def _edit_sound_set(self):
        if self._sound_set:
            self._inspect_sound_set(Path(self._sound_set))

    def _inspect_sound_set(self, path: Path):
        self._start_worker(lambda: ("sound_set", inspect_sound_set(path)), "Reading the sound set's instrument lanes and suggested musical roles…")

    def _show_role_dialog(self, info: dict):
        slots = info["slots"]
        source = str(Path(info["path"]).resolve())
        current_roles = self._lane_roles if self._sound_set and Path(self._sound_set).resolve() == Path(source) else {}
        dialog = tk.Toplevel(self.root)
        dialog.title("Sound set · map musical roles")
        dialog.configure(bg=BG)
        dialog.geometry(f"820x{min(680, self.root.winfo_screenheight() - 130)}")
        dialog.minsize(700, 430)
        dialog.transient(self.root)
        dialog.grab_set()
        head = ttk.Frame(dialog, padding=(20, 16, 20, 10))
        head.pack(fill="x")
        ttk.Label(head, text="Map your instrument lanes", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(head, text=Path(source).name, foreground=ACCENT).pack(anchor="w", pady=(4, 6))
        ttk.Label(head, text="Choose what each lane should play. The generator keeps its name, channel and patch.\n"
                  "Use duplicate roles for layered instruments, or skip for unused lanes. The source MIDI stays unchanged.",
                  style="Muted.TLabel", wraplength=765).pack(anchor="w")
        table_frame = ttk.Frame(dialog, padding=(20, 4, 20, 0))
        table_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(table_frame, bg=PANEL, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)
        table = ttk.Frame(canvas, padding=12, style="Panel.TFrame")
        table.columnconfigure(0, weight=1)
        table_window = canvas.create_window((0, 0), window=table, anchor="nw")
        table.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(table_window, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-1 if event.delta > 0 else 1, "units"))
        for column, name in enumerate(("Original instrument lane", "Channel / patch", "Musical role")):
            ttk.Label(table, text=name, style="PanelMuted.TLabel").grid(row=0, column=column, sticky="w", padx=(0, 12), pady=(0, 10))
        role_vars = []
        for row, slot in enumerate(slots, start=1):
            ttk.Label(table, text=slot["name"], style="Panel.TLabel", wraplength=310).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=6)
            channel = slot.get("channel", 1)
            program = slot.get("program")
            patch_label = f"Ch {channel} · {'—' if program is None else str(program + 1)}"
            ttk.Label(table, text=patch_label, style="PanelMuted.TLabel").grid(row=row, column=1, sticky="w", padx=(0, 12))
            initial = current_roles.get(slot["id"], slot.get("role"))
            variable = tk.StringVar(value=initial if initial in LANE_ROLES else "unassigned")
            role_vars.append((slot, variable))
            dropdown = ttk.Combobox(table, textvariable=variable, values=("unassigned", *LANE_ROLES), state="readonly", width=16)
            dropdown.grid(row=row, column=2, sticky="ew", pady=6)
            hint = slot.get("pitch_hint")
            Tooltip(dropdown, f"Lane ID: {slot['id']}\nSuggested role: {slot.get('role') or 'unassigned'}\n"
                              f"Confidence: {slot.get('confidence', 'unknown')}\n"
                              f"Pitch hint: {hint if hint is not None else 'none'}\n"
                              "Check suggestions against the instrument you will use in your DAW.")
        foot = ttk.Frame(dialog, padding=(20, 12, 20, 16))
        foot.pack(side="bottom", fill="x", before=table_frame)
        warnings = info.get("warnings", [])
        help_text = "Every lane needs a role or skip before generation. Unknown lanes are left unassigned."
        ttk.Label(foot, text=help_text, foreground=AMBER, wraplength=765, font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 10))
        if warnings:
            warnings_box = tk.Text(foot, height=3, wrap="word", bg=PANEL, fg=AMBER, relief="flat",
                                   font=("Segoe UI", 9), padx=8, pady=5)
            warnings_box.insert("1.0", "\n".join(warnings))
            warnings_box.configure(state="disabled")
            warnings_box.pack(fill="x", pady=(0, 10))
            Tooltip(warnings_box, "Scroll here to read every sound-set warning.")
        buttons = ttk.Frame(foot)
        buttons.pack(fill="x")

        def apply_mapping():
            missing = [slot["name"] for slot, variable in role_vars if variable.get() not in LANE_ROLES]
            if missing:
                messagebox.showerror("Map remaining lanes", "Choose a musical role or skip for:\n" + "\n".join(missing[:12])
                                     + ("\n…" if len(missing) > 12 else ""), parent=dialog)
                return
            if not any(variable.get() != "skip" for _slot, variable in role_vars):
                messagebox.showerror("No active lanes", "Assign a musical role to at least one lane.", parent=dialog)
                return
            self._sound_set = source
            self._lane_roles = {slot["id"]: variable.get() for slot, variable in role_vars}
            self._update_soundset_label()
            self._mark_dirty()
            self.status_var.set(f"Sound set loaded: {Path(source).name}. Generate to fill its mapped instrument lanes.")
            dialog.destroy()

        ttk.Button(buttons, text="Apply sound set", style="Accent.TButton", command=apply_mapping).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=(0, 8))

    def _save(self):
        try:
            config = self._config()
        except ValueError as error:
            messagebox.showerror("Check song settings", str(error), parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Save song recipe", defaultextension=".json",
                                          initialfile=f"{config.genre}-{config.style}-{config.seed}.json",
                                          filetypes=[("Song recipe", "*.json")])
        if not path:
            return
        envelope = {"schema_version": 1, "generator_version": __version__,
                    "profile_version": self._profile().get("version", "1.0.0"),
                    "profile_sha256": profile_fingerprint(self._profile()), "config": config.to_dict(),
                    "note_density": self._note_density(), "automation_format": "tiny-pitch-notes"}
        try:
            Path(path).write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Could not save recipe", str(error), parent=self.root)
            return
        self.status_var.set(f"Saved reusable song recipe: {Path(path).name}")

    def _export(self):
        # Recheck the fingerprint as well as the button state: a stale preview must never export silently.
        self._mark_dirty()
        if self._song is None or self._dirty or self._busy:
            self.status_var.set("Generate a preview of the current settings before exporting.")
            return
        directory = filedialog.askdirectory(parent=self.root, title="Choose a parent folder for the new song pack", mustexist=True)
        if not directory:
            return
        song = self._song
        include_automation = self.automation_var.get()
        note_density = self._note_density()
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{song.config.genre}-{song.config.style}-seed-{song.config.seed}")
        base = f"{slug}-{datetime.now():%Y%m%d-%H%M%S}"
        output = Path(directory) / base
        counter = 2
        while output.exists():
            output = Path(directory) / f"{base}-{counter}"
            counter += 1

        def task():
            manifest = export_song(song, output, include_automation=include_automation, note_density=note_density)
            return "export", output, manifest
        self._start_worker(task, "Exporting musical MIDI, the recipe, routing instructions and selected automation…")

    def _open_folder(self):
        if self._export_folder is None:
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(self._export_folder))
            else:
                messagebox.showinfo("Export folder", str(self._export_folder), parent=self.root)
        except OSError as error:
            messagebox.showerror("Could not open folder", str(error), parent=self.root)

    def _close(self):
        if self._busy:
            self._closing = True
            self.status_var.set("Finishing the current operation before closing…")
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    try:
        StudioApp(root)
    except Exception as error:
        root.withdraw()
        messagebox.showerror("Genre MIDI Studio could not start", str(error), parent=root)
        root.destroy()
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
