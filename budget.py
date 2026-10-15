"""Thread-safe cap on outbound AI calls. Every HTTP attempt, including retries, must reserve a slot
first, so retries can never push the server past the configured limits.

Limits (environment variables, 0 = unlimited, not recommended):
  GEMINI_DAILY_CALL_LIMIT  calls per UTC day for the whole server (default 100)
  GEMINI_RUN_CALL_LIMIT    calls a single report run may make, retries included (default 15)

Counters live in memory: they reset when the server restarts or redeploys."""
import os
import threading
import time


def _env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


class BudgetExceeded(Exception):
    def __init__(self, scope, limit):
        self.scope, self.limit = scope, limit
        if scope == "daily":
            msg = (f"This server's daily AI call budget ({limit} calls) is used up. It resets at 00:00 UTC. "
                   "The owner can raise GEMINI_DAILY_CALL_LIMIT.")
        else:
            msg = (f"This run reached its limit of {limit} AI calls (retries included), so the remaining "
                   "questions were not sent. The owner can raise GEMINI_RUN_CALL_LIMIT.")
        super().__init__(msg)


class CallBudget:
    def __init__(self, daily_limit=100, run_limit=15, clock=time.time):
        self.daily_limit, self.run_limit, self.clock = daily_limit, run_limit, clock
        self._lock = threading.Lock()
        self._local = threading.local()
        self._day, self._used = self._today(), 0

    def _today(self):
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def _roll(self):
        today = self._today()
        if today != self._day:
            self._day, self._used = today, 0

    def begin_run(self, limit=None):
        self._local.run = {"limit": self.run_limit if limit is None else limit, "used": 0}

    def end_run(self):
        self._local.run = None

    def reserve(self):
        """Claim one call or raise BudgetExceeded. Checks both limits before counting either."""
        with self._lock:
            self._roll()
            run = getattr(self._local, "run", None)
            if run and run["limit"] > 0 and run["used"] >= run["limit"]:
                raise BudgetExceeded("run", run["limit"])
            if self.daily_limit > 0 and self._used >= self.daily_limit:
                raise BudgetExceeded("daily", self.daily_limit)
            self._used += 1
            if run:
                run["used"] += 1

    def remaining(self):
        """Calls left today, or None when unlimited."""
        with self._lock:
            self._roll()
            return None if self.daily_limit <= 0 else max(0, self.daily_limit - self._used)


BUDGET = CallBudget(_env_int("GEMINI_DAILY_CALL_LIMIT", 100), _env_int("GEMINI_RUN_CALL_LIMIT", 15))


def reserve():
    BUDGET.reserve()
