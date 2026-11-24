import argparse
import ctypes
import sys

from hameg_studio import APP_NAME


def configure_windows_dpi() -> None:
    if sys.platform == "win32":
        # System-aware layout lets Tk apply Windows' configured text scale.
        ctypes.windll.user32.SetProcessDPIAware()


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--demo", action="store_true", help="Open a simulated analyzer")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    configure_windows_dpi()

    import tkinter as tk

    from hameg_studio.app import SpectrumStudio

    root = tk.Tk()
    app = SpectrumStudio(root, start_demo=args.demo or args.smoke_test)
    if args.smoke_test:
        errors = []

        def callback_failed(_error_type, error, _traceback):
            errors.append(error)
            app.close()

        root.report_callback_exception = callback_failed
        root.after(1800, app.close)
    root.mainloop()
    if args.smoke_test:
        if errors:
            raise RuntimeError("Desktop smoke test failed") from errors[0]
        if app.spectrum is None or len(app.spectrum.samples) != 2001:
            raise RuntimeError("Desktop smoke test did not display a complete trace")


if __name__ == "__main__":
    main()
