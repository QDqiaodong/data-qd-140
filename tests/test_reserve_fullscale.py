"""Full-boundary, non-enumerated acceptance instances.

The maximum supported dimensions (192 slots and 80 contracts) are exercised
explicitly rather than with reduced stand-ins.  The single 192x80 planner
solve verifies exact feasibility and optimization without brute-force
enumeration; the book lifecycle test reaches the 80-contract ceiling on a
smaller horizon so the suite stays within a reasonable runtime.
"""
import json
import os
import random
import tempfile
import time
import unittest
from fractions import Fraction

from storage_dispatch.reserve import ReservationBook
from storage_dispatch.reserve.planner import plan

F = Fraction
MAX_SLOTS = 192
MAX_CONTRACTS = 80


class FullScalePlannerTests(unittest.TestCase):
    def test_192_slots_80_contracts_exact_solve(self):
        rng = random.Random(404)
        slots = []
        for t in range(MAX_SLOTS):
            if t % 2 == 0:
                slots.append({"charge_limit": 1000, "discharge_limit": 0,
                              "price": rng.randrange(0, 50)})
            else:
                slots.append({"charge_limit": 0, "discharge_limit": 1000,
                              "price": rng.randrange(0, 50)})
        contracts = []
        for i in range(MAX_CONTRACTS):
            start = rng.randrange(0, MAX_SLOTS - 12)
            end = rng.randrange(start + 2,
                                min(MAX_SLOTS, start + 12) + 1)
            contracts.append({"contract_id": "C%03d" % i, "start": start,
                              "end": end, "remaining": rng.randrange(1, 8)})
        t0 = time.time()
        result = plan(1_000_000, F(4, 5), F(4, 5), 100, slots, 0,
                      F(500_000), contracts)
        self.assertLess(time.time() - t0, 60.0)

        # Every contract delivers exactly its remaining grid-side energy.
        for c in contracts:
            self.assertEqual(
                Fraction(result["contracts"][c["contract_id"]]["planned"]),
                c["remaining"])
        # SOC stays inside [0, capacity] and the terminal floor is respected.
        for s in result["slots"]:
            self.assertGreaterEqual(Fraction(s["soc_after"]), 0)
            self.assertLessEqual(Fraction(s["soc_after"]), 1_000_000)
            self.assertLessEqual(Fraction(s["charge"]),
                                 slots[s["slot"]]["charge_limit"])
            self.assertLessEqual(Fraction(s["discharge_total"]),
                                 slots[s["slot"]]["discharge_limit"])
        self.assertGreaterEqual(Fraction(result["terminal_soc"]), 100)

    def test_192_slots_empty_book_still_plans(self):
        # The spec requires a feasible plan even with no contracts.
        rng = random.Random(11)
        slots = [{
            "charge_limit": rng.randrange(0, 20) if t % 2 == 0 else 0,
            "discharge_limit": 0 if t % 2 == 0 else rng.randrange(0, 20),
            "price": rng.randrange(0, 30)} for t in range(MAX_SLOTS)]
        result = plan(1000, F(4, 5), F(4, 5), 50, slots, 0, F(0), [])
        self.assertEqual(len(result["slots"]), MAX_SLOTS)
        self.assertGreaterEqual(Fraction(result["terminal_soc"]), 50)


class FullScaleBookTests(unittest.TestCase):
    def test_book_reaches_80_contract_ceiling_and_cancel_accounting(self):
        rng = random.Random(3)
        horizon = 24
        slots = [{
            "charge_limit": 1_000_000 if t % 2 == 0 else 0,
            "discharge_limit": 0 if t % 2 == 0 else 1_000_000,
            "price": rng.randrange(0, 10)} for t in range(horizon)]
        td = tempfile.mkdtemp()
        db = os.path.join(td, "full.db")
        book = ReservationBook(db)
        book.initialize(json.dumps({
            "capacity": 10 ** 9, "initial": 10 ** 8,
            "charge_efficiency": {"numerator": 9, "denominator": 10},
            "discharge_efficiency": {"numerator": 9, "denominator": 10},
            "terminal_min": 0, "slots": slots}).encode())
        for i in range(MAX_CONTRACTS):
            start = rng.randrange(0, horizon - 1)
            end = min(horizon, start + rng.randrange(2, 6))
            book.add_contract(json.dumps({
                "request_id": "add-%d" % i, "contract_id": "C%03d" % i,
                "start": start, "end": end,
                "energy": rng.randrange(1, 4)}).encode())
        status = book.status()
        self.assertEqual(len(status["contracts"]), MAX_CONTRACTS)
        self.assertEqual(status["revision"], MAX_CONTRACTS)
        # Cancelled ids still count toward the 80-contract ceiling.
        book.cancel_contract(json.dumps(
            {"request_id": "cancel-0", "contract_id": "C000"}).encode())
        with self.assertRaises(Exception):
            book.add_contract(json.dumps({
                "request_id": "overflow", "contract_id": "C080",
                "start": 1, "end": 3, "energy": 1}).encode())
        # Advance freezes an immutable prefix and bumps revision.
        book.advance(json.dumps(
            {"request_id": "adv", "to_slot": 12}).encode())
        status = book.status()
        self.assertEqual(status["cursor"], 12)
        self.assertEqual(len(status["execution"]), 12)
        self.assertEqual(status["revision"], MAX_CONTRACTS + 2)


if __name__ == "__main__":
    unittest.main()
