from tkinter import font, ttk


PALETTES = {
    "dark": {
        "background": "#101620", "panel": "#192330", "field": "#111b26",
        "edge": "#2c3d4d", "raised": "#243545", "hover": "#30475b",
        "text": "#f0f6fa", "muted": "#a7bbcb", "dim": "#74899b",
        "accent": "#82e5ba", "accent_hover": "#a7f3d2", "accent_text": "#0a2b20",
        "warning": "#ffc884", "screen": "#091713", "grid": "#203c30",
        "cross": "#385849", "readout": "#9bc3b0", "trace": "#95f6b0",
        "trace_fill": "#123424", "marker": "#fbd490",
    },
    "light": {
        "background": "#edf1f5", "panel": "#ffffff", "field": "#f3f6f8",
        "edge": "#cbd7e0", "raised": "#e9eef3", "hover": "#dae6ed",
        "text": "#192c3d", "muted": "#4c6273", "dim": "#637581",
        "accent": "#137353", "accent_hover": "#0d5d42", "accent_text": "#ffffff",
        "warning": "#924a0d", "screen": "#f4faf6", "grid": "#d3e4d9",
        "cross": "#91b3a0", "readout": "#47695b", "trace": "#006743",
        "trace_fill": "#dceee3", "marker": "#8a4b0f",
    },
}


def apply_theme(root, theme: str) -> dict[str, str]:
    colors = PALETTES[theme]
    root.configure(background=colors["background"])
    style = ttk.Style(root)
    style.theme_use("clam")
    font.nametofont("TkDefaultFont").configure(family="Segoe UI", size=10)
    font.nametofont("TkTextFont").configure(family="Segoe UI", size=10)
    style.configure(".", background=colors["panel"], foreground=colors["text"],
                    bordercolor=colors["edge"], lightcolor=colors["edge"],
                    darkcolor=colors["edge"], troughcolor=colors["field"],
                    font=("Segoe UI", 10))
    style.configure("Surface.TFrame", background=colors["background"])
    style.configure("Card.TFrame", relief="solid", borderwidth=1)
    style.configure("TButton", padding=(11, 6), background=colors["raised"],
                    borderwidth=1, focusthickness=1, focuscolor=colors["edge"])
    style.map("TButton", background=[("active", colors["hover"])],
              foreground=[("disabled", colors["dim"])],
              bordercolor=[("focus", colors["accent"])])
    style.configure("Primary.TButton", background=colors["accent"],
                    foreground=colors["accent_text"], font=("Segoe UI Semibold", 10))
    style.map("Primary.TButton", background=[("disabled", colors["raised"]),
                                             ("active", colors["accent_hover"])],
              foreground=[("disabled", colors["dim"])])
    style.configure("Compact.TButton", padding=(8, 4), font=("Segoe UI", 9))
    style.configure("TEntry", padding=(7, 5), fieldbackground=colors["field"],
                    insertcolor=colors["text"], selectbackground=colors["accent"],
                    selectforeground=colors["accent_text"])
    style.configure("TCombobox", padding=(6, 4), fieldbackground=colors["field"],
                    background=colors["raised"], arrowcolor=colors["muted"], arrowsize=11)
    style.map("TCombobox", fieldbackground=[("readonly", colors["field"])],
              foreground=[("disabled", colors["dim"]), ("readonly", colors["text"])],
              selectbackground=[("readonly", colors["field"])],
              selectforeground=[("readonly", colors["text"])])
    root.option_add("*TCombobox*Listbox.background", colors["field"])
    root.option_add("*TCombobox*Listbox.foreground", colors["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", colors["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", colors["accent_text"])
    style.configure("TNotebook", padding=0, borderwidth=0)
    style.configure("TNotebook.Tab", padding=(9, 6), background=colors["raised"])
    style.map("TNotebook.Tab", background=[("selected", colors["panel"])],
              foreground=[("selected", colors["accent"])])
    style.configure("Vertical.TScrollbar", arrowsize=10, background=colors["raised"],
                    arrowcolor=colors["muted"], troughcolor=colors["field"],
                    bordercolor=colors["field"], lightcolor=colors["edge"], darkcolor=colors["edge"])
    style.configure("TCheckbutton", padding=(0, 3), indicatorbackground=colors["field"])
    style.map("TCheckbutton", indicatorbackground=[("selected", colors["accent"])])
    return colors
