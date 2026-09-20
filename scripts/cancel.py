"""Request cancellation of a queued/running run from another terminal.

Usage:
    python -m scripts.cancel RUN_ID
"""
import argparse

from app.config import open_stores


def main() -> None:
    p = argparse.ArgumentParser(description="Cancel a queued or running agent run")
    p.add_argument("run_id")
    args = p.parse_args()
    store, _ = open_stores()
    status = store.request_cancel(args.run_id)
    if status is None:
        raise SystemExit(f"unknown run id: {args.run_id}")
    if status == "cancelled":
        print(f"{args.run_id}: cancelled")
    elif status == "running":
        print(f"{args.run_id}: cancellation requested; worker will stop between steps")
    else:
        print(f"{args.run_id}: already {status}")


if __name__ == "__main__":
    main()
