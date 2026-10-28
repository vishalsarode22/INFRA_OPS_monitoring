from sap_gui.reliability import RetryPolicy, classify_gui_exception, is_non_retryable, is_session_failure


def test_tcode_retry_policy_is_three_attempts():
    p = RetryPolicy(max_attempts=3, initial_delay_seconds=2, backoff_multiplier=2)
    assert p.max_attempts == 3
    assert p.delay_before_retry(1) == 2
    assert p.delay_before_retry(2) == 4


def test_non_retryable_auth_failure_is_classified():
    exc = RuntimeError("Authorization failure for transaction")
    assert classify_gui_exception(exc) == "non_retryable"
    assert is_non_retryable(exc) is True


def test_session_failure_is_recoverable():
    exc = RuntimeError("SAP GUI scripting session disconnected")
    assert classify_gui_exception(exc) == "session"
    assert is_session_failure(exc) is True


def test_unknown_gui_error_is_transient():
    exc = RuntimeError("temporary COM timeout")
    assert classify_gui_exception(exc) == "transient"
