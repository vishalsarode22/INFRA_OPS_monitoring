"""
Central definition of the app's base directory, used for all writable
data (config, logs, reports, snapshots) and bundled static assets.

This is the current working directory at import time -- in dev mode
that's the project root (everything is normally run with cwd there,
e.g. `D:\\SAP_BASIS_MONITOR> python main.py`). In the packaged .exe,
packaging/launcher.py explicitly sets the working directory to the
install folder BEFORE importing any other project module, so this
resolves correctly there too.

Do NOT use __file__-based directory resolution (e.g.
os.path.dirname(os.path.abspath(__file__))) for this purpose anywhere
in the project -- __file__ resolves inside PyInstaller's temporary
extraction folder when frozen, which is read-only and gets wiped after
the app exits. Always import BASE_DIR from here instead.
"""
import os

BASE_DIR = os.getcwd()
