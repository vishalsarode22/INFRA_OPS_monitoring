"""Small persistence contract for operational-intelligence runtime state.

The runtime itself owns serialization so this module is intentionally minimal.
It exists as a stable place for future state-version migrations.
"""

STATE_VERSION = 1
