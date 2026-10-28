from pywinauto import Desktop

windows = Desktop(backend="win32").windows()
for w in windows:
    try:
        title = w.window_text()
        if title.strip():
            print(repr(title))
    except Exception:
        pass