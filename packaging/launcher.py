"""
Packaged-app entry point.

This is the ONLY file that needs to know it's running frozen (PyInstaller)
vs. as a normal script. Everything else in the project (main.py,
core/config_loader.py, dashboard/app.py, reporting/*, logs, etc.) uses
plain relative paths like "config/systems.yaml", "logs/", "reports/" --
those are relative to the CURRENT WORKING DIRECTORY, not to this file.

So the fix is simple and low-risk: before importing ANY project module,
change the working directory to wherever the .exe actually lives (its
install folder). After that, every existing relative path in the
codebase resolves correctly, exactly as it did during development --
no other file needs to change.
"""
import os
import sys


def get_install_dir() -> str:
    """
    Returns the folder the app is running from:
      - Frozen (PyInstaller onefile .exe): the folder containing the .exe
        itself (NOT the temp _MEIPASS extraction folder -- that's
        read-only and wiped after exit, so it's wrong for config/logs).
      - Running as a normal .py script (dev mode): the project root
        (two levels up from this file, since this file lives in packaging/).
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_exists(install_dir: str) -> bool:
    return os.path.isfile(os.path.join(install_dir, "config", "systems.yaml")) and \
        os.path.isfile(os.path.join(install_dir, ".env"))


def main():
    install_dir = get_install_dir()
    os.chdir(install_dir)  # <-- the one line that makes everything else "just work"

    # Make sure the project's own modules (core, dashboard, main, etc.)
    # are importable -- when frozen, PyInstaller already puts them on
    # sys.path via the bundle, so this only matters in dev mode.
    if install_dir not in sys.path:
        sys.path.insert(0, install_dir)

    if not config_exists(install_dir):
        print("No configuration found -- launching first-time setup wizard...")
        from packaging.setup_wizard import run_wizard
        completed = run_wizard(install_dir)
        if not completed:
            print("Setup was cancelled. Run the app again to retry setup.")
            sys.exit(0)

    # Config exists (either already did, or the wizard just wrote it) --
    # start the dashboard, which owns the background scheduler.
    import uvicorn
    import webbrowser
    import threading

    port = 8000
    url = f"http://127.0.0.1:{port}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"Starting SAP BASIS Monitor dashboard at {url}")
    uvicorn.run("dashboard.app:app", host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
