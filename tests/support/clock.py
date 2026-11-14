"""A deterministic clock for polling tests, with optional ledger changes on each sleep."""


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.elapsed

    def time(self):
        return 1800000000 + self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds
        self.on_sleep()
