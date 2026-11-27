"""Test doubles for the fetch path and for the code that runs on it; each says what it stands in for."""

from __future__ import annotations


class Clock:
    """A clock that only advances when something waits on it."""

    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def __call__(self):
        """Return the current fake time."""
        return self.now

    def wait(self, seconds):
        """Advance by the pause and report that nobody asked to stop."""
        self.waits.append(seconds)
        self.now += seconds
        return False
