from quantpilot.diagnostics import classify


def test_diagnostic_requires_valid_actual_signal_report():
    row = {"total_signals": "20", "has_bias": "False", "biased_entry_signals": "0", "biased_exit_signals": "0", "biased_indicators": ""}
    assert classify([row]) == "passed_sampled"
    assert classify([]) == "unverified"
    assert classify([{**row, "has_bias": "unknown"}]) == "unverified"
    assert classify([{**row, "total_signals": "0"}]) == "unverified"
    assert classify([{**row, "biased_indicators": "&-return6"}]) == "flagged"
