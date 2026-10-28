# Milestone 1 test fix

Replaces the stale import in `tests/test_tcode_screenshot.py`.
The manual SAP GUI test now uses `get_scripting_session()` and performs
SAP/COM imports only when executed directly, so the normal pytest suite
does not require a live SAP GUI session.

After applying:

    python -m pytest tests -v

To run the manual SAP GUI screenshot check:

    python tests/test_tcode_screenshot.py
