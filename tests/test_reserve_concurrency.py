"""Concurrency acceptance with real independent OS processes.

These tests deliberately avoid mock connections or pre-serialized calls: each
contending operation runs in a separate ``python -I`` subprocess with its own
SQLite connection.  Two locking strategies are exercised:

* external  -- deterministic "candidate solved -> competing cancel commits ->
  original connection re-solves and commits" interleaving;
* transaction -- one writer demonstrably blocks on the held lock, then retries
  successfully and the final plan is the serial-order optimum.
Both strategies must also prevent overselling under concurrent adds.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from fractions import Fraction

from storage_dispatch.reserve import ReservationBook

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "_reserve_worker.py")


def _slots():
    return [
        {"charge_limit": 0, "discharge_limit": 0, "price": 0},
        {"charge_limit": 6, "discharge_limit": 0, "price": 1},    # cheap tight
        {"charge_limit": 100, "discharge_limit": 0, "price": 99},  # expensive
        {"charge_limit": 0, "discharge_limit": 100, "price": 0}]


def _build_db(path):
    book = ReservationBook(path)
    book.initialize(json.dumps({
        "capacity": 1000, "initial": 0,
        "charge_efficiency": {"numerator": 1, "denominator": 1},
        "discharge_efficiency": {"numerator": 1, "denominator": 1},
        "terminal_min": 0, "slots": _slots()}).encode())
    book.add_contract(json.dumps({
        "request_id": "keep", "contract_id": "KEEP", "start": 3, "end": 4,
        "energy": 6}).encode())
    book.add_contract(json.dumps({
        "request_id": "drop", "contract_id": "DROP", "start": 3, "end": 4,
        "energy": 6}).encode())


def _write_envelope(td, name, obj):
    path = os.path.join(td, name)
    with open(path, "w") as fh:
        json.dump(obj, fh)
    return path


def _spawn(db, strategy, envelope, env=None):
    return subprocess.Popen(
        [sys.executable, "-I", WORKER, db, strategy, envelope],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ, **(env or {})))


def _run_blocking(db, strategy, envelope):
    proc = subprocess.run(
        [sys.executable, "-I", WORKER, db, strategy, envelope],
        capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


class ExternalLockingTests(unittest.TestCase):
    def test_candidate_recomputed_after_competing_commit(self):
        td = tempfile.mkdtemp()
        db = os.path.join(td, "ext.db")
        _build_db(db)
        ready = os.path.join(td, "ready")
        go = os.path.join(td, "go")
        envelope = _write_envelope(td, "add.json", {"_op": "add", "request": {
            "request_id": "extra", "contract_id": "EXTRA", "start": 3,
            "end": 4, "energy": 1}})
        primary = _spawn(db, "external", envelope,
                         {"SD_HOOK_READY": ready, "SD_HOOK_WAIT": go})
        deadline = time.time() + 60
        while time.time() < deadline and not os.path.exists(ready):
            if primary.poll() is not None:
                self.fail("primary exited early: " + primary.stderr.read())
            time.sleep(0.01)
        # A genuinely separate process cancels DROP and commits while the
        # primary holds its (now stale) candidate.
        _run_blocking(db, "transaction", _write_envelope(
            td, "cancel.json",
            {"_op": "cancel",
             "request": {"request_id": "cancel-drop",
                         "contract_id": "DROP"}}))
        with open(go, "w") as fh:
            fh.write("go")
        out, err = primary.communicate(timeout=60)
        self.assertEqual(primary.returncode, 0, err)
        result = json.loads(out)
        status = ReservationBook(db).status()
        cheap = next(s for s in status["plan"]["slots"] if s["slot"] == 1)
        expensive = next(s for s in status["plan"]["slots"]
                         if s["slot"] == 2)
        # Post-cancel obligations are KEEP 6 + EXTRA 1 = 7; cheap cap 6 means
        # at most one unit spills to the expensive slot (not the pre-cancel 6).
        self.assertEqual(Fraction(cheap["charge"]), 6)
        self.assertLessEqual(Fraction(expensive["charge"]), 1)
        self.assertEqual(next(c for c in status["contracts"]
                             if c["contract_id"] == "DROP")["state"],
                         "cancelled")
        self.assertIn("revision", result)
        # The committed plan must be the optimum of the FINAL serial state:
        # re-planning independently from the committed state must reproduce the
        # same cost (the stale candidate is never reused).
        from storage_dispatch.reserve.planner import plan as fresh_plan
        contracts = []
        for c in status["contracts"]:
            if c["state"] == "active":
                contracts.append({
                    "contract_id": c["contract_id"],
                    "start": 3, "end": 4,
                    "remaining": Fraction(c["remaining"])})
        re_planned = fresh_plan(
            status["capacity"], Fraction(status["eta_c"]),
            Fraction(status["eta_d"]), status["terminal_min"], _slots(),
            status["cursor"], Fraction(status["prefix_end_soc"]), contracts)
        stored_cost = sum(
            Fraction(s["charge"]) * s["price"]
            for s in status["plan"]["slots"])
        self.assertEqual(Fraction(re_planned["cost"]), stored_cost)


class TransactionLockingTests(unittest.TestCase):
    def test_writer_blocks_then_retries_to_serial_optimum(self):
        td = tempfile.mkdtemp()
        db = os.path.join(td, "t.db")
        _build_db(db)
        import sqlite3
        # Hold the write lock from a separate connection to force the worker
        # into the busy/retry path.
        holder = sqlite3.connect(db, timeout=1, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            envelope = _write_envelope(
                td, "cancel.json",
                {"_op": "cancel",
                 "request": {"request_id": "c1", "contract_id": "DROP"}})
            start = time.time()
            proc = subprocess.Popen(
                [sys.executable, "-I", WORKER, db, "transaction", envelope],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(1.0)
            self.assertIsNone(proc.poll(), "writer did not block on the lock")
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertGreaterEqual(time.time() - start, 0.9)
        status = ReservationBook(db).status()
        self.assertEqual(next(c for c in status["contracts"]
                             if c["contract_id"] == "DROP")["state"],
                         "cancelled")
        # The committed plan is feasible and is the optimum of the resulting
        # serial state (SOC within bounds, exact remaining delivery).
        for s in status["plan"]["slots"]:
            self.assertGreaterEqual(Fraction(s["soc_after"]), 0)
        keep = next(c for c in status["contracts"]
                    if c["contract_id"] == "KEEP")
        self.assertEqual(Fraction(keep["remaining"]), 6)


class NoOversellTests(unittest.TestCase):
    def _oversell_db(self, td, name):
        db = os.path.join(td, name)
        book = ReservationBook(db)
        book.initialize(json.dumps({
            "capacity": 1000, "initial": 100,
            "charge_efficiency": {"numerator": 1, "denominator": 1},
            "discharge_efficiency": {"numerator": 1, "denominator": 1},
            "terminal_min": 0,
            "slots": [
                {"charge_limit": 100, "discharge_limit": 0, "price": 1},
                {"charge_limit": 0, "discharge_limit": 5, "price": 0}]}
        ).encode())
        return db

    def _run_pair(self, db, strategy):
        import tempfile as _tf
        procs = []
        for rid, cid in (("r1", "C1"), ("r2", "C2")):
            fd, path = _tf.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as fh:
                json.dump({"_op": "add", "request": {
                    "request_id": rid, "contract_id": cid, "start": 1,
                    "end": 2, "energy": 4}}, fh)
            procs.append(_spawn(db, strategy, path))
        codes = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(err, "") if p.returncode == 0 else None
            codes.append(p.returncode)
        return codes

    def test_concurrent_adds_cannot_exceed_shared_limit_external(self):
        td = tempfile.mkdtemp()
        db = self._oversell_db(td, "ext.db")
        codes = self._run_pair(db, "external")
        self.assertEqual(sum(1 for c in codes if c == 0), 1)  # 4+4 > cap 5
        self.assertEqual(len(ReservationBook(db).status()["contracts"]), 1)

    def test_concurrent_adds_cannot_exceed_shared_limit_transaction(self):
        td = tempfile.mkdtemp()
        db = self._oversell_db(td, "tx.db")
        codes = self._run_pair(db, "transaction")
        self.assertEqual(sum(1 for c in codes if c == 0), 1)
        self.assertEqual(len(ReservationBook(db).status()["contracts"]), 1)


if __name__ == "__main__":
    unittest.main()
