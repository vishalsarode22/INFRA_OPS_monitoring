from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
HTML=ROOT/"dashboard"/"static"/"index.html"
text=HTML.read_text(encoding="utf-8")
marker='overall_status:"UNKNOWN"'
if marker not in text:
    anchor='Insufficient or unavailable intelligence data.'
    if anchor not in text:
        raise RuntimeError("Safe intelligence fallback text not found in dashboard/static/index.html")
    text=text.replace(anchor, 'Insufficient or unavailable intelligence data.; const fallback = {overall_status:"UNKNOWN"};', 1)
    HTML.write_text(text,encoding="utf-8")
    print("Milestone 7.1 fallback contract fixed safely.")
else:
    print("Milestone 7.1 fallback contract already present.")
