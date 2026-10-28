from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "sap_gui" / "screenshot.py"
s = TARGET.read_text(encoding="utf-8")

old_sig = "def capture_screenshot(session, tcode: str) -> str:"
new_sig = "def capture_screenshot(session, tcode: str, output_dir: str = None) -> str:"
if old_sig not in s:
    raise RuntimeError("capture_screenshot signature not found")
s = s.replace(old_sig, new_sig, 1)

old_block = '''    screenshots_dir = _screenshots_dir_for_today()
    timestamp = datetime.now().strftime("%H%M%S")
    filename = f"{tcode}_{timestamp}.png"
    filepath = os.path.join(screenshots_dir, filename)
'''
new_block = '''    screenshots_dir = output_dir or _screenshots_dir_for_today()
    os.makedirs(screenshots_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%H%M%S_%f")
    filename = f"{tcode}_{timestamp}.png"
    filepath = os.path.join(screenshots_dir, filename)
'''
if old_block not in s:
    raise RuntimeError("screenshot path block not found")
s=s.replace(old_block,new_block,1)
TARGET.write_text(s,encoding="utf-8")
print("Screenshot patch applied.")
