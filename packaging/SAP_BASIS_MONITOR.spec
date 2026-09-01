# PyInstaller spec for SAP BASIS Monitor.
# Build from the PROJECT ROOT (D:\SAP_BASIS_MONITOR) with:
#   pyinstaller packaging\SAP_BASIS_MONITOR.spec
#
# This produces dist\SAP_BASIS_MONITOR.exe as a single file. It does NOT
# bundle config\templates\, dashboard\static\, config\thresholds.yaml, or
# config\monitoring_tasks.yaml -- those ship as loose files next to the
# .exe via the Inno Setup installer (see packaging\installer.iss), since
# the app reads/writes them relative to its own folder (see utils/paths.py)
# and some (systems.yaml, .env) are created fresh by the wizard per machine.

block_cipher = None

a = Analysis(
    ['launcher.py'],
    pathex=['..'],  # project root, so `import core`, `import dashboard`, etc. resolve
    binaries=[],
    datas=[],
    hiddenimports=[
        # Windows COM / SAP GUI Scripting automation
        'win32com', 'win32com.client', 'win32com.client.gencache',
        'win32api', 'win32con', 'win32gui', 'win32process', 'win32event',
        'pythoncom', 'pywintypes',
        'pywinauto', 'pywinauto.application', 'pywinauto.findwindows',
        # Web server
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan', 'uvicorn.lifespan.on',
        # Reporting / OCR / SSH
        'reportlab.graphics.barcode',
        'pytesseract', 'PIL', 'PIL._tkinter_finder',
        'paramiko',
        'openpyxl',
        'yaml',
        'dotenv',
        # Project packages (in case PyInstaller's import scan misses any
        # dynamic `from main import ...` style calls)
        'core', 'dashboard', 'reporting', 'notifications', 'collectors',
        'sap_gui', 'evaluation', 'utils', 'main', 'packaging.setup_wizard',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='SAP_BASIS_MONITOR',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # keep False -- UPX-compressed pywin32 DLLs are a
                         # common source of false-positive antivirus flags
    console=True,        # keep a console window for now so errors are visible;
                          # switch to False once the app is stable
    icon=None,            # point at a .ico file here if you want a custom icon
    onefile=True,
)
