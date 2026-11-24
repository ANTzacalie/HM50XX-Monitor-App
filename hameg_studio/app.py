import sys
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from pathlib import Path
from time import monotonic
from tkinter import filedialog, messagebox, ttk

from serial import SerialException
from serial.tools import list_ports

from . import APP_NAME, DEVELOPER_NAME, LICENSE_NAME, PROJECT_URL, RELEASE_DATE
from .demo import DemoAnalyzer
from .display import SpectrumDisplay
from .i18n import translate
from .preferences import Preferences, TRACE_FOCUS_LEVELS, TRACE_INTENSITY_LEVELS
from .profiles import PROFILES
from .protocol import ProtocolError, SerialAnalyzer, TraceTransferError
from .spectrum import spectrum_from_trace
from .theme import apply_theme


CONTROL_TABS = {
    "tuning": ("cf", "sp", "sr", "st", "rl", "at", "db", "ra"),
    "signal": ("bw", "ba", "vf", "vm", "dm", "mk", "mf", "df", "lv"),
    "instrument": ("du", "es", "et", "sv", "rc"),
}
ON_OFF_SETTINGS = {"vf", "ra", "ba", "dm", "es", "et"}
OPTION_LABELS = {"vm": "mode", "mk": "marker"}
FIXED_BNC_TEST_MHZ = 48


@dataclass
class Control:
    variable: tk.StringVar
    widget: ttk.Widget
    apply_button: ttk.Button | None
    options: dict[str, int]
    shown_value: str = ""


class SpectrumStudio:
    def __init__(self, root, *, start_demo=False, preferences=None):
        self.root = root
        self.preferences = preferences or Preferences.load()
        self.profile = PROFILES[self.preferences.model]
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hm50xx")
        self.analyzer = None
        self.future = None
        self.current_job = None
        self.pending_job = None
        self.closing = False
        self.demo = False
        self.live = False
        self.instrument = {}
        self.unreadable_values = {}
        self.spectrum = None
        self.trace_error: TraceTransferError | None = None
        self.plot_settings = {}
        self.single_shot_settings = {}
        self.marker = None
        self.port_names = []
        self.port = tk.StringVar()
        self.baud = tk.StringVar()
        self.console_output = tk.StringVar()
        self.dialog = None
        self.test_tune_button = None
        self.active_tab = 0
        self.next_capture = 0
        self.next_state = 0
        self.message = ("ready", {}, False)
        width = max(940, min(1320, self.root.winfo_screenwidth() - 60))
        height = max(600, min(850, self.root.winfo_screenheight() - 100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(940, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        self.scan_ports()
        if start_demo:
            self.toggle_demo()
        self.root.after(35, self.tick)

    def t(self, key, **values):
        return translate(self.preferences.language, key, **values)

    def label(self, parent, text="", *, color="text", size=10, bold=False,
              surface=False, **options):
        family = "Segoe UI Semibold" if bold else "Segoe UI"
        return tk.Label(parent, text=text, font=(family, size), borderwidth=0,
                        bg=self.colors["background" if surface else "panel"],
                        fg=self.colors[color], **options)

    def button(self, parent, key, command, *, style="TButton"):
        return ttk.Button(parent, text=self.t(key), command=command, style=style)

    def build(self):
        if hasattr(self, "content") and self.content.winfo_exists():
            self.active_tab = self.tabs.index("current")
            self.content.destroy()
        self.colors = apply_theme(self.root, self.preferences.theme)
        self.root.title(APP_NAME)
        self.content = ttk.Frame(self.root, style="Surface.TFrame", padding=(20, 17, 20, 12))
        self.content.pack(fill="both", expand=True)
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(2, weight=1)
        self.controls = {}
        self.remote_widgets = []
        self.plot_readouts = {}
        self.build_header()
        self.build_connection()
        self.build_workspace()
        self.build_footer()
        self.update_port_choices()
        self.sync_view(force=True)

    def build_header(self):
        header = ttk.Frame(self.content, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        next_theme = "light" if self.preferences.theme == "dark" else "dark"
        self.button(header, next_theme, self.toggle_theme).pack(side="left")
        family, product = APP_NAME.split(" ", 1)
        self.label(header, family, color="accent", size=18, bold=True, surface=True).pack(
            side="left", padx=(16, 8))
        self.label(header, product, size=17, bold=True, surface=True).pack(side="left")
        self.model_var = tk.StringVar(value=self.profile.model)
        self.model_box = ttk.Combobox(header, values=tuple(PROFILES), textvariable=self.model_var,
                                      width=13, state="readonly", font=("Segoe UI Semibold", 11))
        self.model_box.pack(side="right")
        self.model_box.bind("<<ComboboxSelected>>", self.switch_model)
        self.button(header, "settings", self.show_preferences).pack(side="right", padx=(0, 8))

    def build_connection(self):
        line = ttk.Frame(self.content, style="Card.TFrame", padding=(12, 8))
        line.grid(row=1, column=0, sticky="ew", pady=(0, 13))
        self.status_dot = self.label(line, "●", size=11, color="dim")
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_label = self.label(line, size=10, bold=True)
        self.status_label.pack(side="left", padx=(0, 15))
        self.label(line, self.t("port"), color="muted").pack(side="left", padx=(0, 5))
        self.port_box = ttk.Combobox(line, textvariable=self.port, width=16, state="readonly")
        self.port_box.pack(side="left", padx=(0, 6))
        self.scan_button = self.button(line, "refresh_ports", self.scan_ports, style="Compact.TButton")
        self.scan_button.pack(side="left", padx=(0, 13))
        self.label(line, self.t("baud"), color="muted").pack(side="left", padx=(0, 5))
        self.baud_box = ttk.Combobox(line, textvariable=self.baud, width=8, state="readonly")
        self.baud_box.pack(side="left", padx=(0, 8))
        self.connect_button = self.button(line, "connect", self.toggle_connection, style="Primary.TButton")
        self.connect_button.pack(side="left")
        self.demo_button = self.button(line, "demo", self.toggle_demo)
        self.demo_button.pack(side="right")

    def build_workspace(self):
        body = ttk.Frame(self.content, style="Surface.TFrame")
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        plot_card = ttk.Frame(body, style="Card.TFrame", padding=12)
        plot_card.grid(row=0, column=0, sticky="nsew", padx=(0, 13))
        plot_card.columnconfigure(0, weight=1)
        plot_card.rowconfigure(2, weight=1)
        self.build_plot(plot_card)
        rail = ttk.Frame(body, style="Surface.TFrame", width=335)
        rail.grid(row=0, column=1, sticky="nsew")
        rail.grid_propagate(False)
        rail.columnconfigure(0, weight=1)
        rail.rowconfigure(2, weight=1)
        self.build_generator(rail)
        self.build_display_controls(rail)
        self.build_tabs(rail)

    def build_plot(self, card):
        toolbar = ttk.Frame(card)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 11))
        self.label(toolbar, self.t("spectrum"), size=17, bold=True).pack(side="left")
        self.live_status = self.label(toolbar, color="muted", size=9)
        self.live_status.pack(side="left", padx=12)
        self.live_button = self.button(toolbar, "resume", self.toggle_live)
        self.live_button.pack(side="right")
        self.capture_button = self.button(toolbar, "capture", lambda: self.capture("capture"))
        self.capture_button.pack(side="right", padx=(0, 6))
        self.average_button = self.button(toolbar, "average", self.toggle_average)
        self.average_button.pack(side="right", padx=(0, 6))
        meters = ttk.Frame(card)
        meters.grid(row=1, column=0, sticky="ew", pady=(0, 9))
        for index, key in enumerate(("cf", "sp", "bw", "rl", "at")):
            meters.columnconfigure(index, weight=1, uniform="meter")
            meter = ttk.Frame(meters, style="Card.TFrame", padding=(8, 5))
            meter.grid(row=0, column=index, sticky="ew", padx=(0, 4) if index < 4 else 0)
            self.label(meter, self.t(key), color="muted", size=9).pack(anchor="w")
            value = self.label(meter, "—", size=10, bold=True, anchor="w")
            value.pack(fill="x")
            self.plot_readouts[key] = value
        self.plot = SpectrumDisplay(card, self.colors, self.t, self.marker_changed)
        self.plot.marker = self.marker
        self.plot.grid(row=2, column=0, sticky="nsew")
        tools = ttk.Frame(card)
        tools.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        self.marker_status = self.label(tools, color="muted", size=9, anchor="w")
        self.marker_status.pack(side="left", fill="x", expand=True)
        self.clear_button = self.button(tools, "clear", lambda: self.plot.select_marker(None),
                                        style="Compact.TButton")
        self.clear_button.pack(side="right")
        self.marker_right_button = ttk.Button(tools, text="→", width=3, style="Compact.TButton",
                                              command=partial(self.plot.move_marker, 1))
        self.marker_right_button.pack(side="right", padx=(0, 6))
        self.marker_left_button = ttk.Button(tools, text="←", width=3, style="Compact.TButton",
                                             command=partial(self.plot.move_marker, -1))
        self.marker_left_button.pack(side="right", padx=(0, 4))
        self.peak_button = self.button(tools, "peak", self.plot.peak, style="Compact.TButton")
        self.peak_button.pack(side="right", padx=(0, 6))

    def build_generator(self, rail):
        card = ttk.Frame(rail, style="Card.TFrame", padding=12)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.label(card, self.t(self.profile.generator_name), size=14, bold=True).pack(anchor="w")
        tracking = self.profile.generator_name == "tracking_generator"
        self.label(card, self.t("tracking_detail" if tracking else "source_detail"), color="muted",
                   size=9, wraplength=305, justify="left").pack(anchor="w", pady=(1, 8))
        output = ttk.Frame(card)
        output.pack(fill="x", pady=(0, 6))
        self.label(output, self.t("output"), bold=True).pack(side="left")
        self.output_status = self.label(output, color="muted", size=9, bold=True)
        self.output_status.pack(side="left", padx=8)
        self.output_button = self.button(output, "enable_output", self.toggle_output)
        self.output_button.pack(side="right")
        self.build_control(card, "tl", label="tip_level")
        self.label(card, self.t("level_range", low=f"{self.profile.generator_min:+.1f}",
                                high=f"{self.profile.generator_max:+.1f}", step=self.profile.generator_step),
                   color="muted", size=9).pack(anchor="w", pady=(5, 0))
        if tracking:
            test_row = ttk.Frame(card)
            test_row.pack(fill="x", pady=(9, 0))
            self.label(test_row, f"{FIXED_BNC_TEST_MHZ} MHz · BNC", color="muted", size=9).pack(side="left")
            self.test_signal_button = self.button(test_row, "test_signal", self.show_fixed_test_signal)
            self.test_signal_button.pack(side="right")
        else:
            self.test_signal_button = self.output_button

    def build_display_controls(self, rail):
        card = ttk.Frame(rail, style="Card.TFrame", padding=(9, 6))
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.label(card, self.t("pc_display"), color="muted", size=9).pack(side="left")
        self.intens_button = ttk.Button(
            card, style="Compact.TButton",
            command=partial(self.cycle_plot_preference, "trace_intensity", TRACE_INTENSITY_LEVELS),
        )
        self.intens_button.pack(side="right")
        self.focus_button = ttk.Button(
            card, style="Compact.TButton",
            command=partial(self.cycle_plot_preference, "trace_focus", TRACE_FOCUS_LEVELS),
        )
        self.focus_button.pack(side="right", padx=(0, 5))
        self.update_plot_appearance()

    def cycle_plot_preference(self, key, levels):
        index = levels.index(getattr(self.preferences, key))
        setattr(self.preferences, key, levels[(index + 1) % len(levels)])
        self.update_plot_appearance()
        self.save_preferences()

    def update_plot_appearance(self):
        focus, intensity = self.preferences.trace_focus, self.preferences.trace_intensity
        self.plot.set_appearance(focus, intensity)
        self.focus_button.configure(text=f"FOCUS · {self.t(f'focus_{focus}')}")
        self.intens_button.configure(text=f"INTENS · {intensity}%")

    def build_tabs(self, rail):
        self.tabs = ttk.Notebook(rail)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        for tab_name, keys in CONTROL_TABS.items():
            panel = ttk.Frame(self.tabs, padding=(9, 8))
            self.tabs.add(panel, text=self.t(tab_name))
            canvas = tk.Canvas(panel, background=self.colors["panel"], highlightthickness=0)
            scroll = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
            canvas.pack(side="left", fill="both", expand=True)
            canvas.configure(yscrollcommand=partial(self.show_scrollbar, scroll))
            rows = ttk.Frame(canvas, padding=(0, 0, 6, 0))
            window = canvas.create_window(0, 0, window=rows, anchor="nw")
            canvas.bind("<Configure>", lambda event, c=canvas, w=window: c.itemconfigure(w, width=event.width))
            rows.bind("<Configure>", lambda _event, c=canvas: c.configure(scrollregion=c.bbox("all")))
            for key in keys:
                if key in self.profile.settings:
                    self.build_control(rows, key)
            if tab_name == "instrument":
                self.build_instrument_tools(rows)
            if tab_name == "signal":
                self.add_action(rows, "copy_trace")
            self.bind_mousewheel(canvas, canvas)
        self.tabs.select(self.active_tab)

    @staticmethod
    def show_scrollbar(scrollbar, first, last):
        scrollbar.set(first, last)
        if float(first) == 0.0 and float(last) == 1.0:
            scrollbar.pack_forget()
        elif not scrollbar.winfo_manager():
            scrollbar.pack(side="right", fill="y")

    def bind_mousewheel(self, widget, canvas):
        def scroll(event):
            if canvas.yview() != (0.0, 1.0) and not isinstance(event.widget, ttk.Combobox):
                canvas.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            return None

        widget.bind("<MouseWheel>", scroll, add="+")
        for child in widget.winfo_children():
            self.bind_mousewheel(child, canvas)

    def build_control(self, parent, key, *, label=None):
        setting = self.profile.settings[key]
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        title = self.label(row, self.t(label or key), color="muted", size=9, anchor="w")
        title.pack(side="left", fill="x", expand=True)
        variable = tk.StringVar()
        if not setting.writable:
            widget = self.label(row, textvariable=variable, bold=True, size=9)
            widget.pack(side="right", padx=(0, 4))
            self.controls[key] = Control(variable, widget, None, {})
            return

        apply_button = self.button(row, "apply", partial(self.apply_setting, key),
                                   style="Compact.TButton")
        apply_button.configure(width=6)
        apply_button.pack(side="right", padx=(4, 0))
        if setting.unit:
            self.label(row, setting.unit, size=8, color="muted", width=6).pack(side="right")
        options = {self.option_label(key, value): value for value in setting.options}
        if options:
            width = 14 if key in OPTION_LABELS else 8
            widget = ttk.Combobox(row, values=tuple(options), textvariable=variable, width=width)
        else:
            widget = ttk.Entry(row, textvariable=variable, width=9, justify="right")
        widget.pack(side="right")
        widget.bind("<Return>", lambda _event: self.apply_setting(key))
        self.controls[key] = Control(variable, widget, apply_button, options)

    def option_label(self, key, value):
        if key in OPTION_LABELS:
            return self.t(f"{OPTION_LABELS[key]}_{value}")
        if key in ON_OFF_SETTINGS:
            return self.t("on" if value else "off")
        if key == "du":
            return ("dBm", "dBmV", "dBµV")[value]
        return str(value)

    def add_action(self, parent, key):
        button = self.button(parent, key, partial(self.run_action, key))
        button.pack(fill="x", pady=(6, 0))
        self.remote_widgets.append(button)

    def build_instrument_tools(self, parent):
        self.add_action(parent, "single_sweep")
        self.control_button = self.button(parent, "release_control", self.toggle_remote)
        self.control_button.pack(fill="x", pady=(6, 0))
        self.sync_button = self.button(parent, "sync", self.request_state)
        self.sync_button.pack(fill="x", pady=(6, 0))
        self.console_button = self.button(parent, "console", self.show_console)
        self.console_button.pack(fill="x", pady=(6, 0))
        self.trace_diagnostic_button = self.button(parent, "save_trace_diagnostic", self.save_trace_diagnostic)
        self.trace_diagnostic_button.pack(fill="x", pady=(6, 0))

    def build_footer(self):
        footer = ttk.Frame(self.content, style="Surface.TFrame")
        footer.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        details = ttk.Frame(footer, style="Surface.TFrame")
        details.pack(side="right")
        self.footer_info = self.label(details, color="muted", surface=True, size=9)
        self.footer_info.pack(anchor="e")
        self.developer_credit = self.label(details, self.t("developer_credit", name=DEVELOPER_NAME),
                                           color="muted", surface=True, size=9)
        self.developer_credit.pack(anchor="e")
        self.footer_message = self.label(footer, color="muted", surface=True, size=9, anchor="w",
                                         justify="left")
        self.footer_message.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.footer_message.bind("<Configure>", lambda event: self.footer_message.configure(
            wraplength=event.width))

    def set_message(self, key, problem=False, **values):
        self.message = (key, values, problem)
        self.update_message()

    def update_message(self):
        key, values, problem = self.message
        message = self.t(key, **values)
        if values.get("detail"):
            message += f"  {values['detail']}"
        self.footer_message.configure(text=message, fg=self.colors["warning" if problem else "muted"])

    def save_preferences(self):
        try:
            self.preferences.save()
        except OSError as error:
            self.set_message("no_settings_save", True, detail=str(error))

    def toggle_theme(self):
        self.preferences.theme = "light" if self.preferences.theme == "dark" else "dark"
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.destroy()
        self.build()
        self.save_preferences()

    def scan_ports(self):
        try:
            self.port_names = [info.device for info in sorted(list_ports.comports())]
        except OSError as error:
            self.report_error(error)
            return
        self.update_port_choices()
        if self.analyzer is None:
            self.set_message("ready" if self.port_names else "no_port_hint")
        self.sync_view()

    def update_port_choices(self):
        self.port_box.configure(values=self.port_names or (self.t("no_ports"),))
        remembered = self.preferences.ports.get(self.profile.model)
        if self.port.get() not in self.port_names:
            self.port.set(remembered if remembered in self.port_names
                          else next(iter(self.port_names), self.t("no_ports")))
        self.baud_box.configure(values=(self.t("auto"), *self.profile.baud_rates))
        if not self.baud.get():
            remembered_baud = self.preferences.baud_rates.get(self.profile.model)
            self.baud.set(remembered_baud if remembered_baud in self.profile.baud_rates else self.t("auto"))

    def switch_model(self, _event=None):
        chosen = self.model_var.get()
        if chosen == self.profile.model:
            return
        if self.foreground_busy():
            self.model_var.set(self.profile.model)
            return
        if self.analyzer is not None:
            restart_demo = self.demo
            self.disconnect(lambda: self.select_model(chosen, restart_demo))
        else:
            self.select_model(chosen)

    def select_model(self, model, restart_demo=False):
        self.profile = PROFILES[model]
        self.preferences.model = model
        self.port.set("")
        self.baud.set("")
        self.build()
        self.save_preferences()
        if restart_demo:
            self.toggle_demo()

    def toggle_connection(self):
        if self.analyzer is not None:
            self.disconnect()
            return
        port, baud_text = self.port.get(), self.baud.get()
        if port not in self.port_names:
            self.set_message("no_port_hint", True)
            return
        baud = int(baud_text) if baud_text.isdecimal() else None
        fast = self.preferences.fast_link and baud is None
        self.preferences.ports[self.profile.model] = port
        if baud is None:
            self.preferences.baud_rates.pop(self.profile.model, None)
        else:
            self.preferences.baud_rates[self.profile.model] = baud
        self.save_preferences()
        self.set_message("connecting")
        profile = self.profile

        def connect():
            analyzer = SerialAnalyzer.connect(profile, port, baud, fast)
            try:
                return analyzer, analyzer.read_state()
            except Exception:
                with suppress(OSError, ProtocolError):
                    analyzer.close()
                raise

        self.submit("connecting", connect, lambda result: self.connected(*result))

    def toggle_demo(self):
        if self.demo:
            self.disconnect()
        elif self.analyzer is None and not self.foreground_busy():
            analyzer = DemoAnalyzer(self.profile)
            self.connected(analyzer, analyzer.read_state(), demo=True)

    def connected(self, analyzer, state, demo=False):
        self.analyzer, self.demo = analyzer, demo
        self.trace_error = None
        self.instrument = state
        self.unreadable_values = {}
        self.single_shot_settings = {}
        self.live = self.preferences.live_on_connect or demo
        self.next_capture = 0
        self.next_state = monotonic() + 3
        self.set_message("demo_status" if demo else "online")
        self.sync_view(force=True)

    def disconnect(self, after=None, *, reason=None):
        analyzer = self.analyzer
        self.live = False
        self.set_message("disconnecting")

        def done(error):
            self.clear_connection()
            if after:
                after()
            if reason or error:
                self.report_error(reason or error)

        def disconnect_analyzer():
            try:
                analyzer.close()
            except (OSError, ProtocolError) as error:
                return error
            return None

        self.submit("disconnecting", disconnect_analyzer, done)

    def clear_connection(self):
        self.analyzer, self.demo, self.live = None, False, False
        self.instrument, self.unreadable_values = {}, {}
        self.single_shot_settings = {}
        self.spectrum, self.plot_settings, self.marker = None, {}, None
        self.plot.marker = None
        self.set_message("offline")
        self.sync_view(force=True)

    def foreground_busy(self):
        return self.pending_job is not None or (
            self.current_job is not None and self.current_job[0] not in {"stream", "state"}
        )

    def submit(self, kind, work, callback):
        job = kind, work, callback
        if self.future is None:
            self.start_job(job)
        elif not self.foreground_busy():
            self.pending_job = job
        else:
            self.set_message("wait")
        self.sync_view()

    def start_job(self, job):
        self.current_job = job
        self.future = self.executor.submit(job[1])

    def tick(self):
        if self.future is not None and self.future.done():
            job = self.current_job
            future, self.future, self.current_job = self.future, None, None
            try:
                result = future.result()
            except Exception as error:
                if not self.closing:
                    self.report_error(error)
                    if job[0] in {"stream", "state", "read-settings"}:
                        self.live = False
                    if job[0] in {"state", "read-settings"}:
                        self.instrument = {}
                        if self.pending_job and self.pending_job[0] != "disconnecting":
                            self.pending_job = None
                    if isinstance(error, (SerialException, OSError)) and self.analyzer is not None:
                        self.pending_job = None
                        self.disconnect(reason=error)
            else:
                job[2](result)
            self.sync_view()

        if self.future is None:
            if self.closing:
                self.finish_close()
                return
            if self.pending_job is not None:
                job, self.pending_job = self.pending_job, None
                self.start_job(job)
                self.sync_view()
            elif self.analyzer is not None:
                now = monotonic()
                if now >= self.next_state and not self.demo:
                    self.next_state = now + 3
                    self.start_job(("state", self.analyzer.read_state, self.state_received))
                elif self.live and self.instrument.get("kl") == 1 and now >= self.next_capture:
                    self.capture("stream")
        self.root.after(35, self.tick)

    def report_error(self, error):
        if isinstance(error, TraceTransferError):
            self.trace_error = error
            self.set_message("trace_transfer_error", True, command=error.command, count=len(error.response))
        else:
            self.set_message("transfer_error", True, detail=str(error))

    def save_trace_diagnostic(self):
        error = self.trace_error
        if error is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title=self.t("save_trace_diagnostic"),
            initialfile=f"{error.model}-trace-diagnostic.txt", defaultextension=".txt",
        )
        if path:
            try:
                Path(path).write_text(error.diagnostic_text(), encoding="utf-8")
            except OSError as write_error:
                self.set_message("diagnostic_save_failed", True, detail=str(write_error))
            else:
                self.set_message("diagnostic_saved")

    def state_received(self, state):
        self.instrument = state
        self.next_state = monotonic() + 3
        self.sync_view()

    def request_state(self):
        if self.analyzer is None:
            return
        self.set_message("reading")
        read = partial(self.analyzer.read_state, retry_missing=True)
        self.submit("read-settings", read, self.manual_state_received)

    def manual_state_received(self, state):
        self.state_received(state)
        self.set_message("restored")

    def capture(self, kind):
        if self.analyzer is None or self.instrument.get("kl") != 1:
            return
        analyzer = self.analyzer

        def capture_with_state():
            state = self.instrument.copy()
            return analyzer.capture(), state

        self.submit(kind, capture_with_state, lambda result: self.trace_received(*result))

    def trace_received(self, trace, state):
        self.spectrum = spectrum_from_trace(trace, state)
        self.plot_settings = state
        self.next_capture = monotonic() + self.preferences.stream_interval_ms / 1000
        if self.message[0] == "trace_transfer_error":
            self.set_message("trace_restored")
        self.sync_view()

    def toggle_live(self):
        self.live = not self.live
        self.next_capture = 0
        self.sync_view()

    def average_active(self) -> bool:
        return self.instrument.get("vm") == 3 and (
            "dm" not in self.profile.settings or self.instrument.get("dm") == 1
        )

    def toggle_average(self):
        self.write_setting("vm", 0 if self.average_active() else 3, "vm")

    def toggle_output(self):
        current = self.instrument.get("tg")
        if current == 0:
            level = self.controls["tl"]
            if "tl" not in self.instrument:
                return
            if level.variable.get() != level.shown_value:
                self.set_message("set_output_level_first", True)
                return
        self.write_setting("tg", int(current == 0), "output")

    def toggle_remote(self):
        self.write_setting("kl", int(self.instrument.get("kl") != 1), "remote")

    def apply_setting(self, key):
        control = self.controls[key]
        text = control.variable.get().strip()
        value = control.options.get(text, text.replace(",", "."))
        setting = self.profile.settings[key]
        field = self.t("tip_level" if key == "tl" else key)
        try:
            number = setting.validate(value)
        except (ValueError, ArithmeticError):
            detail = self.t("valid_options", values=", ".join(control.options)) if control.options else (
                self.t("value_range", low=setting.minimum, high=setting.maximum)
            )
            if setting.step is not None:
                detail += f" · Δ {setting.step}"
            self.set_message("invalid_value", True, field=field, detail=detail)
            return
        self.write_setting(key, number, "tip_level" if key == "tl" else key)

    def write_setting(self, key, value, label, *, require_tracking_off=False):
        if self.analyzer is None or self.foreground_busy():
            return
        if key != "kl" and self.instrument.get("kl") != 1:
            return
        analyzer = self.analyzer
        self.set_message("working")

        def write():
            try:
                if require_tracking_off and self.profile.parse_reply("tg", analyzer.execute("#tg")) != 0:
                    return None, {"tg": 1}, None
                averaging_on = key == "vm" and value == 3
                averaging_off = key == "vm" and value == 0 and self.instrument.get("vm") == 3
                actual = (analyzer.set_average(averaging_on) if averaging_on or averaging_off
                          else analyzer.set_value(key, value))
            except ProtocolError as error:
                return None, None, error
            try:
                return actual, analyzer.read_state(), None
            except ProtocolError as error:
                return actual, None, error

        def update(result):
            actual, state, read_error = result
            if actual is None and state is not None:
                self.instrument.update(state)
                self.set_message("test_signal_tracking_off", True)
                return
            if actual is None:
                self.instrument.pop(key, None)
                self.live = False
                self.next_state = 0
                self.report_error(read_error)
                return
            if self.profile.settings[key].readable:
                self.instrument[key] = actual
            else:
                self.unreadable_values[key] = actual
            control = self.controls.get(key)
            if control is not None:
                self.fill_control(key, control, actual, force=True)
            if key == "es":
                self.single_shot_settings = {}
                if value == 1:
                    self.live = False
            if key == "vm":
                self.next_capture = 0
            if read_error is None:
                self.state_received(state)
                if Decimal(str(actual)) != Decimal(str(value)):
                    shown = self.option_label("vm", int(actual)) if key == "vm" else actual
                    self.set_message("actual_value", key == "vm", field=self.t(label), value=shown)
                else:
                    self.set_message("updated", field=self.t(label))
            else:
                self.live = False
                self.next_state = 0
                self.instrument = {key: actual} if self.profile.settings[key].readable else {}
                self.report_error(read_error)

        self.submit("working", write, update)

    def run_action(self, key):
        self.run_command(self.profile.action_command(key))

    def run_command(self, command, *, console=False):
        if self.analyzer is None or self.foreground_busy():
            return
        analyzer = self.analyzer
        normalized_command = command.strip().lower()
        self.set_message("working")
        if console:
            self.console_output.set(self.t("working"))

        def execute():
            state_before = self.instrument.copy()
            try:
                reply = analyzer.execute(command)
            except (ProtocolError, ValueError) as error:
                return None, state_before, None, error
            try:
                return reply, state_before, analyzer.read_state(), None
            except ProtocolError as error:
                return reply, state_before, None, error

        def update(result):
            reply, state_before, state, error = result
            if reply is None:
                key = normalized_command[1:3]
                if isinstance(error, ProtocolError) and len(normalized_command) > 3:
                    self.instrument.pop(key, None)
                    self.live = False
                    self.next_state = 0
                    if normalized_command == "#ss1":
                        self.single_shot_settings = {}
                if console:
                    self.console_output.set(f"{self.t('transfer_error')} · {error}")
                self.report_error(error)
                return
            trace_state = state_before
            if normalized_command == "#ss1" and self.profile.model == "HM5014-2" and not self.demo:
                trace_state, self.single_shot_settings = self.single_shot_settings, state_before
            if hasattr(reply, "samples"):
                self.trace_received(reply, trace_state)
                response = self.t("point_count", count=len(reply.samples))
            else:
                response = reply
                if normalized_command == "#ss1" and response.startswith("Sweep started"):
                    response = self.t("first_sweep")
            if console:
                self.console_output.set(response)
            if normalized_command in ("#es0", "#es1"):
                self.single_shot_settings = {}
                if normalized_command == "#es1":
                    self.live = False
            if error is None:
                self.state_received(state)
                self.set_message("acknowledged", detail=response if response != "RD" else "")
            else:
                self.live = False
                self.next_state = 0
                self.instrument = {}
                self.report_error(error)

        self.submit("working", execute, update)

    def fill_control(self, key, control, value, *, force):
        if value is None:
            text = "—"
        elif key == "lv":
            unit = "dB" if self.instrument.get("mk") == 2 else "dBm"
            text = f"{value:+.1f} {unit}"
        elif control.options:
            text = self.option_label(key, int(value))
        else:
            text = f"{value:.{self.profile.settings[key].decimals}f}"
        if control.variable.get() == control.shown_value or force:
            if control.variable.get() != text:
                control.variable.set(text)
            control.shown_value = text

    def sync_view(self, *, force=False):
        online = self.analyzer is not None
        locked = self.instrument.get("kl") == 1
        busy = self.foreground_busy() or self.closing
        remote = online and locked and not busy
        status = "demo_status" if self.demo else "online" if online else "offline"
        if self.current_job and self.current_job[0] in ("connecting", "disconnecting"):
            status = self.current_job[0]
        self.status_label.configure(text=self.t(status))
        self.status_dot.configure(fg=self.colors["accent" if online else "dim"])
        self.connect_button.configure(text=self.t("disconnect" if online else "connect"),
                                      state="normal" if not busy and not self.demo and
                                      (online or self.port_names) else "disabled")
        self.demo_button.configure(text=self.t("end_demo" if self.demo else "demo"),
                                   state="normal" if not busy and (self.demo or not online) else "disabled")
        self.model_box.configure(state="disabled" if busy else "readonly")
        for widget in (self.port_box, self.baud_box):
            widget.configure(state="disabled" if online or busy else "readonly")
        self.scan_button.configure(state="disabled" if online or busy else "normal")
        for button in (self.capture_button, self.live_button, self.average_button, *self.remote_widgets):
            button.configure(state="normal" if remote else "disabled")
        averaging = self.average_active()
        self.average_button.configure(text=self.t("average_on" if averaging else "average"),
                                      style="Primary.TButton" if averaging else "TButton")
        self.live_button.configure(text=self.t("pause" if self.live else "resume"))
        self.live_status.configure(text=self.t("live" if self.live and locked else "paused") if online else "")
        for button in (self.sync_button, self.control_button, self.console_button):
            button.configure(state="normal" if online and not busy else "disabled")
        self.trace_diagnostic_button.configure(state="normal" if self.trace_error and not busy else "disabled")
        self.control_button.configure(text=self.t("release_control" if locked else "take_control"))

        output = self.instrument.get("tg")
        self.output_status.configure(text=self.t({0: "off", 1: "on"}.get(output, "unknown")),
                                     fg=self.colors["accent" if output == 1 else "muted"])
        can_toggle = remote and (output != 0 or "tl" in self.instrument)
        turn_on = output == 0 or not online
        if self.profile.generator_name == "test_source":
            output_key = "test_signal_on" if turn_on else "test_signal_off"
        else:
            output_key = "enable_output" if turn_on else "disable_output"
        self.output_button.configure(text=self.t(output_key), state="normal" if can_toggle else "disabled")
        if self.test_tune_button is not None and self.test_tune_button.winfo_exists():
            can_tune = remote and output == 0 and self.profile.model == "HM5014-2"
            self.test_tune_button.configure(state="normal" if can_tune else "disabled")
        for key, control in self.controls.items():
            setting = self.profile.settings[key]
            value = self.instrument.get(key, self.unreadable_values.get(key))
            self.fill_control(key, control, value, force=force)
            if setting.writable:
                control.widget.configure(state=("readonly" if control.options else "normal")
                                         if remote else "disabled")
                control.apply_button.configure(state="normal" if remote else "disabled")

        displayed = self.plot_settings if self.spectrum else self.instrument
        for key, readout in self.plot_readouts.items():
            value = displayed.get(key)
            if self.spectrum:
                value = {"cf": self.spectrum.center_mhz, "sp": self.spectrum.span_mhz,
                         "rl": self.spectrum.reference_dbm}.get(key, value)
            unit = self.profile.settings[key].unit
            readout.configure(text="—" if value is None else f"{value:g} {unit}")
        self.peak_button.configure(state="normal" if self.spectrum else "disabled")
        hint = "trace_start" if online else "trace_hint"
        if self.plot.spectrum is not self.spectrum or self.plot.empty_hint != hint:
            self.plot.show(self.spectrum, hint)
        self.marker_changed()
        self.update_message()

        info = [self.profile.model]
        if online and not self.demo:
            info.extend((f"{self.analyzer.baud_rate} baud", "8N1"))
            if self.analyzer.firmware:
                info.append(f"{self.t('fw')} {self.analyzer.firmware}")
        if online:
            if "kl" in self.instrument:
                info.append(self.t("remote" if locked else "local"))
            if self.instrument.get("uc") in (0, 1):
                info.append(self.t("uncalibrated" if self.instrument["uc"] else "calibrated"))
            missing = set(self.profile.query_keys).difference(self.instrument)
            if self.instrument.get("mk") not in (1, 2):
                missing.discard("lv")
            if missing:
                info.append(self.t("partial_readback"))
        self.footer_info.configure(text="  ·  ".join(info))

    def marker_changed(self):
        self.marker = self.plot.marker
        has_marker = self.spectrum is not None and self.marker is not None
        self.clear_button.configure(state="normal" if has_marker else "disabled")
        self.marker_left_button.configure(state="normal" if has_marker and self.marker > 0 else "disabled")
        can_move_right = has_marker and self.marker < len(self.spectrum.samples) - 1
        self.marker_right_button.configure(state="normal" if can_move_right else "disabled")
        if self.spectrum is None:
            text = ""
        elif self.marker is None:
            text = self.t("marker_help")
        else:
            frequency = self.spectrum.frequency(self.marker)
            level = self.spectrum.level(self.marker)
            position = (f"{frequency:.3f} MHz" if frequency is not None else
                        f"{100 * self.marker / (len(self.spectrum.samples) - 1):.1f}%")
            amplitude = f"  ·  {level:+.1f} dBm" if level is not None else ""
            text = f"M1  {position}{amplitude}"
        self.marker_status.configure(text=text)

    def open_dialog(self, title):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            return None
        dialog = tk.Toplevel(self.root)
        dialog.title(self.t(title))
        dialog.configure(background=self.colors["panel"])
        dialog.transient(self.root)
        dialog.resizable(False, False)
        self.dialog = dialog
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        return frame

    def show_preferences(self):
        frame = self.open_dialog("preferences")
        if frame is None:
            return
        self.label(frame, self.t("preferences"), size=15, bold=True).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.label(frame, self.t("language"), color="muted").grid(row=1, column=0, sticky="w")
        language = ttk.Combobox(frame, values=("English", "Română"), width=14, state="readonly")
        language.current(self.preferences.language == "ro")
        language.grid(row=1, column=1, sticky="e", pady=4)
        live = tk.BooleanVar(value=self.preferences.live_on_connect)
        fast = tk.BooleanVar(value=self.preferences.fast_link)
        ttk.Checkbutton(frame, text=self.t("live_on_connect"), variable=live).grid(
            row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(frame, text=self.t("fast_link"), variable=fast).grid(
            row=3, column=0, columnspan=2, sticky="w")
        self.label(frame, self.t("poll_delay"), color="muted").grid(
            row=4, column=0, sticky="w", padx=(0, 12))
        interval = ttk.Combobox(frame, values=tuple(f"{i} ms" for i in (100, 250, 500, 1000, 2000)),
                                width=14, state="readonly")
        interval.set(f"{self.preferences.stream_interval_ms} ms")
        interval.grid(row=4, column=1, sticky="e", pady=4)

        ttk.Separator(frame).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 10))
        self.label(frame, self.t("developer_credit", name=DEVELOPER_NAME), bold=True).grid(
            row=6, column=0, columnspan=2, sticky="w")
        self.label(frame, self.t("release_date", date=RELEASE_DATE), color="muted").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(3, 0))
        self.label(frame, self.t("license_notice"), color="muted", size=9, wraplength=365,
                   justify="left").grid(row=8, column=0, columnspan=2, sticky="w", pady=(3, 0))

        dialog = self.dialog
        links = ttk.Frame(frame)
        links.grid(row=9, column=0, columnspan=2, sticky="w", pady=(9, 0))
        ttk.Button(links, text=LICENSE_NAME, command=partial(self.show_license, dialog),
                   style="Compact.TButton").pack(side="left", padx=(0, 7))
        ttk.Button(links, text="GitHub · HM50XX-Monitor-App", command=partial(self.open_project_page, dialog),
                   style="Compact.TButton").pack(side="left")

        def save():
            self.preferences.language = ("en", "ro")[language.current()]
            self.preferences.live_on_connect = live.get()
            self.preferences.fast_link = fast.get()
            self.preferences.stream_interval_ms = int(interval.get().split()[0])
            self.dialog.destroy()
            self.build()
            self.set_message("saved")
            self.save_preferences()

        buttons = ttk.Frame(frame)
        buttons.grid(row=10, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.button(buttons, "cancel", self.dialog.destroy).pack(side="left", padx=(0, 7))
        self.button(buttons, "save", save, style="Primary.TButton").pack(side="left")

    def open_project_page(self, parent):
        with suppress(OSError, webbrowser.Error):
            if webbrowser.open_new_tab(PROJECT_URL):
                return
        messagebox.showerror("GitHub", self.t("project_link_failed", url=PROJECT_URL), parent=parent)

    def show_license(self, parent):
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
        try:
            license_text = (base / "LICENSE").read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            messagebox.showerror(LICENSE_NAME, self.t("license_unavailable"), parent=parent)
            return

        window = tk.Toplevel(parent)
        window.title(LICENSE_NAME)
        window.transient(parent)
        panel = ttk.Frame(window, padding=12)
        panel.pack(fill="both", expand=True)
        controls = ttk.Frame(panel)
        controls.pack(side="bottom", fill="x", pady=(8, 0))
        self.button(controls, "close_dialog", window.destroy).pack(side="right")
        content = tk.Text(panel, height=24, width=76, wrap="word", padx=8, pady=6,
                          background=self.colors["panel"], foreground=self.colors["text"],
                          font=("Consolas", 10))
        scroll = ttk.Scrollbar(panel, command=content.yview)
        content.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        content.pack(side="left", fill="both", expand=True)
        content.insert("1.0", license_text)
        content.configure(state="disabled")
        window.bind("<Escape>", lambda _event: window.destroy())

    def show_console(self):
        frame = self.open_dialog("console")
        if frame is None:
            return
        self.console_output.set("")
        self.label(frame, self.t("command_help"), color="muted", wraplength=390,
                   justify="left").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        command = ttk.Entry(frame, width=31, font=("Consolas", 11))
        command.grid(row=1, column=0, sticky="ew", padx=(0, 8))

        def send():
            self.run_command(command.get(), console=True)

        self.button(frame, "send", send, style="Primary.TButton").grid(row=1, column=1)
        self.label(frame, textvariable=self.console_output, color="accent", justify="left",
                   wraplength=390).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        command.bind("<Return>", lambda _event: send())
        command.focus_set()

    def show_fixed_test_signal(self):
        frame = self.open_dialog("test_signal")
        if frame is None:
            return
        dialog = self.dialog
        self.label(frame, f"TEST SIGNAL · {FIXED_BNC_TEST_MHZ} MHz", size=15, bold=True).pack(anchor="w")
        self.label(frame, self.t("fixed_test_signal_help"), color="muted", wraplength=400,
                   justify="left").pack(anchor="w", pady=(8, 10))
        self.label(frame, self.t("test_signal_tracking_off"), color="muted", wraplength=400,
                   justify="left").pack(anchor="w")

        def tune():
            dialog.destroy()
            self.write_setting("cf", FIXED_BNC_TEST_MHZ, "cf", require_tracking_off=True)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        self.test_tune_button = self.button(actions, "test_signal_center", tune, style="Primary.TButton")
        self.test_tune_button.pack(side="right")
        self.button(actions, "close_dialog", dialog.destroy).pack(side="right", padx=(0, 7))
        self.sync_view()

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.pending_job = None
        self.live = False
        self.sync_view()

    def finish_close(self):
        if self.analyzer is not None:
            analyzer, self.analyzer = self.analyzer, None
            self.start_job(("disconnecting", analyzer.close, lambda _result: None))
            self.root.after(35, self.tick)
            return
        self.executor.shutdown(wait=False)
        self.root.destroy()
