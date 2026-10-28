from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "dashboard" / "static" / "index.html"

old = 'async function loadAllData() {\n  const res = await fetch("/api/systems");\n  allSystems = await res.json();\n\n  systemStatuses = {};\n  await Promise.all(allSystems.map(async s => {\n    const r = await fetch("/api/status/" + encodeURIComponent(s.name));\n    systemStatuses[s.name] = await r.json();\n  }));\n'
new = 'async function loadAllData() {\n  const res = await fetch("/api/systems");\n  allSystems = await res.json();\n\n  // System cards consume the unified overview contract.\n  systemStatuses = {};\n  await Promise.all(allSystems.map(async s => {\n    try {\n      const r = await fetch("/api/systems/" + encodeURIComponent(s.name) + "/overview");\n      if (!r.ok) throw new Error("Overview request failed: " + r.status);\n      const overview = await r.json();\n      systemStatuses[s.name] = { ...overview, available: true };\n    } catch (err) {\n      console.warn("Unable to load overview for " + s.name, err);\n      systemStatuses[s.name] = {\n        system: s.name, client: s.client, available: false,\n        overall_status: "UNKNOWN", metrics: [], incidents: [], events: [],\n        operational_intelligence: null, ai_analysis: null\n      };\n    }\n  }));\n'
text = HTML.read_text(encoding="utf-8")
if '"/api/systems/" + encodeURIComponent(s.name) + "/overview"' in text:
    print("Milestone 7.0 integration already applied.")
elif old not in text:
    raise RuntimeError("Expected dashboard loadAllData block was not found.")
else:
    HTML.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("Milestone 7.0 dashboard integration applied.")