"""Bound automatic capture retries without a tight native-rendering loop."""

RETRY_INITIAL_MS = 1000
RETRY_MAX_MS = 30_000


def retry_delay_ms(attempt: int) -> int:
    """Back off from one second to thirty seconds for repeated failures."""
    exponent = min(max(attempt - 1, 0), 5)
    return min(RETRY_MAX_MS, RETRY_INITIAL_MS * 2**exponent)
