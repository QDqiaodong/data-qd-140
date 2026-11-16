"""Independent-process worker for concurrency acceptance tests.

Invoked as a separate OS process so contention is real (two SQLite
connections in two processes), not a simulated or pre-serialized call.  The
optional solve-hook files provide a controllable interleaving: the worker
signals "candidate computed" and then blocks until released.
"""
import json
import os
import sys
import time

# Running under "python -I" ignores PYTHONPATH, so locate the package from the
# worker's own location: tests/_reserve_worker.py -> ../src
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from storage_dispatch.reserve import ReservationBook


def _make_hook(ready_file, wait_file):
    state = {"fired": False}

    def hook(event, book, snapshot, result=None):
        if event != "after_solve" or state["fired"]:
            return
        state["fired"] = True
        if ready_file:
            with open(ready_file, "w") as fh:
                fh.write("ready")
        if wait_file:
            deadline = time.time() + 60
            while time.time() < deadline and not os.path.exists(wait_file):
                time.sleep(0.01)
    return hook


def main():
    db_path, strategy, envelope_path = sys.argv[1:4]
    envelope = json.loads(open(envelope_path, "rb").read())
    operation = envelope["_op"]
    request = envelope["request"]
    ready = os.environ.get("SD_HOOK_READY")
    wait = os.environ.get("SD_HOOK_WAIT")
    book = ReservationBook(db_path, strategy=strategy,
                           solve_hook=_make_hook(ready, wait))
    raw = json.dumps(request).encode()
    if operation == "add":
        rev, receipt = book.add_contract(raw)
    elif operation == "cancel":
        rev, receipt = book.cancel_contract(raw)
    else:
        rev, receipt = book.advance(raw)
    print(json.dumps({"revision": rev, "receipt": receipt}))


if __name__ == "__main__":
    main()
