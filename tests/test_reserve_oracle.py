"""Cross-check the production planner against an independent vertex oracle.

The planner's generated linear program is intercepted and solved both by the
production simplex and by the exhaustive vertex enumeration in ``_oracle``.
The two must agree on the minimum cost.  Instances are deliberately tiny so
the combinatorial enumeration is exhaustive rather than heuristic.
"""
import random
import unittest
from fractions import Fraction

import storage_dispatch.reserve.planner as planner_mod
from storage_dispatch.reserve.simplex import Simplex
from tests._oracle import brute_force_optimum

F = Fraction


class OracleCrossCheck(unittest.TestCase):
    def setUp(self):
        self.captured = []
        self._original_init = Simplex.__init__

        def capture(instance, n, constraints, costs, levels=None,
                    pivot_limit=1_000_000):
            self.captured.append({
                "n": n, "constraints": constraints, "costs": dict(costs),
                "levels": dict(levels or {})})
            self._original_init(instance, n, constraints, costs, levels,
                                pivot_limit)

        Simplex.__init__ = capture

    def tearDown(self):
        Simplex.__init__ = self._original_init

    def test_agrees_with_vertex_enumeration(self):
        rng = random.Random(2026)
        checked = 0
        # Single-slot instances produce a tiny LP (few hundred vertices), so
        # the independent enumeration is exhaustive and fast.
        for _ in range(60):
            slots = [{"charge_limit": 0,
                      "discharge_limit": rng.randrange(1, 4), "price": 0}]
            initial = rng.randrange(0, 5)
            contracts = [{"contract_id": "A", "start": 0, "end": 1,
                          "remaining": rng.randrange(1, 3)}]
            try:
                result = planner_mod.plan(
                    6, F(1), F(1), 0, slots, 0, F(initial), contracts)
            except planner_mod.InfeasiblePlan:
                continue
            lp = self.captured[-1]
            try:
                oracle = brute_force_optimum(lp["n"], lp["constraints"],
                                             lp["costs"])
            except ValueError:
                continue  # enumeration too large to be a sound oracle
            self.assertIsNotNone(oracle, "oracle found no feasible vertex")
            self.assertEqual(Fraction(result["cost"]), oracle[0])
            checked += 1
        self.assertGreaterEqual(checked, 6, "too few instances cross-checked")


if __name__ == "__main__":
    unittest.main()
