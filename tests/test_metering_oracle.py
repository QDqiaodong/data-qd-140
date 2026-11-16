"""Cross-validation against an independent, exact continuous oracle.

The oracle in ``tests/_metering_oracle.py`` never imports the production
simplex, domain or planner: it eliminates SOC analytically and enumerates
polyhedron vertices with its own Gaussian elimination.  Expected values here
come exclusively from that oracle, never from the solver under test.  All
domains are continuous rational polyhedra -- no integer grids or sampling.
"""
import random
import unittest
from fractions import Fraction

from tests._metering_oracle import EnergyOracle
from storage_dispatch.metering.domain import MeteringDomain
from storage_dispatch.metering.billing import BillingSolver

F = Fraction


def rd(rid, ch, s, e, lo, up):
    return {"reading_id": rid, "channel": ch, "start": s, "end": e,
            "lower": F(lo), "upper": F(up)}


class OracleCrossCheck(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(2026)

    def _random_case(self):
        rng = self.rng
        t = rng.randrange(2, 5)
        requests = [rng.randrange(-6, 7) for _ in range(t)]
        device = {
            "capacity": rng.randrange(1, 12),
            "initial": rng.randrange(0, 6),
            "max_charge": rng.randrange(0, 8),
            "max_discharge": rng.randrange(0, 8),
            "eta_c": F(rng.randrange(1, 6), 6),
            "eta_d": F(rng.randrange(1, 5), 5),
            "requests": requests,
        }
        readings = []
        for i in range(rng.randrange(0, 3)):
            s = rng.randrange(0, t)
            e = rng.randrange(s + 1, t + 1)
            lo = F(rng.randrange(0, 5), rng.randrange(1, 4))
            up = lo + F(rng.randrange(0, 6), 2)
            readings.append(rd(f"r{i}", rng.choice(
                ("import", "export", "loss")), s, e, lo, up))
        return device, readings, t

    def _random_invoice(self, t):
        rng = self.rng
        s = rng.randrange(0, t)
        e = rng.randrange(s + 1, t + 1)
        return {
            "invoice_id": "X", "start": s, "end": e,
            "prices": [rng.randrange(0, 5) for _ in range(e - s)],
            "window": rng.randrange(1, e - s + 1),
            "demand_rate": rng.randrange(0, 6),
            "amount": F(0),
        }

    def test_feasibility_matches_oracle(self):
        matches = 0
        for _ in range(30):
            device, readings, t = self._random_case()
            oracle = EnergyOracle(device, readings)
            domain = MeteringDomain(device, readings, t)
            self.assertEqual(oracle.feasible(), domain.is_feasible())
            if oracle.feasible():
                self.assertIsNone(domain.verify_witness(domain.witness()))
                matches += 1
        self.assertGreater(matches, 10)

    def test_invoice_extrema_match_oracle(self):
        for _ in range(20):
            device, readings, t = self._random_case()
            oracle = EnergyOracle(device, readings)
            domain = MeteringDomain(device, readings, t)
            if not oracle.feasible():
                continue
            ticket = self._random_invoice(t)
            o_min, o_max = oracle.invoice_extrema(ticket)
            solver = BillingSolver(domain, [ticket])
            p_min, p_max, w_min, w_max = solver.invoice_bounds(0)
            self.assertEqual((o_min, o_max), (p_min, p_max))
            # Attaining witnesses reproduce the extremum under an independent
            # evaluation and obey every device/observation constraint.
            self.assertEqual(solver.evaluate(ticket, w_min)[0], p_min)
            self.assertEqual(solver.evaluate(ticket, w_max)[0], p_max)
            self.assertIsNone(domain.verify_witness(w_min))
            self.assertIsNone(domain.verify_witness(w_max))

    def test_joint_conclusion_matches_oracle_including_branches(self):
        both_ways = {True: 0, False: 0}
        for _ in range(20):
            device, readings, t = self._random_case()
            oracle = EnergyOracle(device, readings)
            domain = MeteringDomain(device, readings, t)
            if not oracle.feasible():
                continue
            a = self._random_invoice(t)
            b = self._random_invoice(t)
            b["invoice_id"] = "Y"
            a["amount"] = F(self.rng.randrange(0, 14), 2)
            b["amount"] = F(self.rng.randrange(0, 14), 2)
            expected = oracle.joint_feasible([a, b])
            actual = BillingSolver(domain, [a, b]).joint_witness() is not None
            self.assertEqual(expected, actual)
            both_ways[expected] += 1
            if actual:
                witness = BillingSolver(
                    domain, [a, b]).joint_witness()
                self.assertIsNone(domain.verify_witness(witness))
        # The sample must exercise both the non-convex feasible branches and
        # the infeasible branch outcomes.
        self.assertGreater(both_ways[True], 0)
        self.assertGreater(both_ways[False], 0)

    def test_explicit_peak_window_change_branch(self):
        # Window-1 peak must switch windows between the two extrema; the
        # oracle enumerates the peak regions independently.
        device = {"capacity": 20, "initial": 0, "max_charge": 10,
                  "max_discharge": 10, "eta_c": F(1), "eta_d": F(1),
                  "requests": [10, 10]}
        readings = [rd("t", "import", 0, 2, 10, 10)]
        ticket = {"invoice_id": "A", "start": 0, "end": 2,
                  "prices": [100, 1], "window": 1, "demand_rate": 10,
                  "amount": F(0)}
        oracle = EnergyOracle(device, readings)
        domain = MeteringDomain(device, readings, 2)
        o_min, o_max = oracle.invoice_extrema(ticket)
        p_min, p_max, w_min, w_max = \
            BillingSolver(domain, [ticket]).invoice_bounds(0)
        self.assertEqual((o_min, o_max), (p_min, p_max))
        self.assertEqual(w_min["slots"][0]["import"], F(0))
        self.assertEqual(w_max["slots"][1]["import"], F(0))


if __name__ == "__main__":
    unittest.main()
