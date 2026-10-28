import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestrator import run_monitoring_cycle

if __name__ == "__main__":
    result = run_monitoring_cycle(system="TST", client="000")

    print(f"\nSystem: {result.system}  Client: {result.client}")
    print(f"Overall status: {result.overall_status.value}")
    print(f"Cycle time: {result.cycle_timestamp}")
    print(f"Errors: {result.errors}\n")

    for m in result.metrics:
        print(f"{m.name:30s} {m.display_value:10s} status={m.status.value:8s} detail={m.detail}")