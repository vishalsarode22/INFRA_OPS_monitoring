"""
InfraBeatOps runs as three processes (see packaging/SERVICES.md):

    python -m services.web        UI and live RFC reads        (service)
    python -m services.collector  scheduler and sweeps         (logged-on task on Windows)
    python -m services.jobs       mail, AI, maintenance,       (service)
                                  "monitor is down" watchdog

They share one SQLite database (core/store.py) and nothing else.
"""
