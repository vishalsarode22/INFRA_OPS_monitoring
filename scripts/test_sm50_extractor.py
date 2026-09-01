import os
import sys

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


from sap_gui.scripting_connection import get_scripting_session
from sap_gui.sm50_extractor import extract_sm50_rows


session = get_scripting_session()

rows = extract_sm50_rows(session)

print("=" * 80)
print("SM50 EXTRACTOR TEST")
print("=" * 80)

print("Rows:", len(rows))

for row in rows:
    print(row)