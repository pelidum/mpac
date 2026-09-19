import time

_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 900  # 15 minutes


class LoginRateLimiter:
    def __init__(
        self, max_attempts: int = _MAX_ATTEMPTS, window_seconds: int = _WINDOW_SECONDS
    ):
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def _prune(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self._window_seconds
        timestamps = [t for t in self._attempts.get(key, []) if t > cutoff]
        if timestamps:
            self._attempts[key] = timestamps
        else:
            self._attempts.pop(key, None)
        return timestamps

    def is_blocked(self, ip: str) -> bool:
        return len(self._prune(ip)) >= self._max_attempts

    def seconds_until_unblocked(self, ip: str) -> int:
        timestamps = self._prune(ip)
        if len(timestamps) < self._max_attempts:
            return 0
        oldest_relevant = timestamps[-(self._max_attempts)]
        return max(1, int(oldest_relevant + self._window_seconds - time.monotonic()))

    def record_failure(self, ip: str) -> None:
        self._prune(ip)
        self._attempts.setdefault(ip, []).append(time.monotonic())

    def clear(self, ip: str) -> None:
        self._attempts.pop(ip, None)


login_limiter = LoginRateLimiter()
