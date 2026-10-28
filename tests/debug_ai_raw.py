import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestrator import run_monitoring_cycle
from evaluation.ai_analyzer import _build_prompt, _call_real_ai

if __name__ == "__main__":
    result = run_monitoring_cycle(system="TST", client="000")
    prompt = _build_prompt(result)
    raw = _call_real_ai(prompt)
    print("RAW RESPONSE:")
    print(repr(raw))
    print("\n--- readable ---\n")
    print(raw)