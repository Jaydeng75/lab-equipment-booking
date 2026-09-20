"""Offline proof of exponential retry backoff and dead-lettering.

Run with:
    python -m scripts.retry_demo
"""
from app.memory import RunStore


class ManualClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def main() -> None:
    clock = ManualClock()
    store = RunStore(":memory:", clock=clock)
    store.migrate()
    thread = store.create_thread("22CS045")
    run_id = store.enqueue(thread, "retry demo", "mock", max_attempts=2)

    first = store.claim_next("worker-A", 10)
    assert first is not None
    status = store.fail_attempt(run_id, "worker-A", "provider_unavailable", True, backoff_seconds=2)
    assert status == "queued"
    assert store.claim_next("too-early", 10) is None
    print("attempt 1 -> queued with 2 second backoff")

    clock.advance(2)
    second = store.claim_next("worker-B", 10)
    assert second is not None and second.attempts == 2
    status = store.fail_attempt(run_id, "worker-B", "provider_unavailable", True, backoff_seconds=2)
    assert status == "dead"
    print("attempt 2 -> dead-lettered after max attempts")
    print("PASS: retry/backoff/dead-lettering works")


if __name__ == "__main__":
    main()
