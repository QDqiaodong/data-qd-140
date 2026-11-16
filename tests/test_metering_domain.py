"""Metering domain: correlated feasibility, witnesses, minimal conflicts."""
import unittest
from fractions import Fraction

from storage_dispatch.metering.domain import MeteringDomain, MeteringInfeasible

F = Fraction


def device(capacity=10, initial=0, mc=10, md=10, ec=(1, 1), ed=(1, 1),
           requests=(10, 10)):
    return {"capacity": capacity, "initial": initial, "max_charge": mc,
            "max_discharge": md, "eta_c": F(*ec), "eta_d": F(*ed),
            "requests": list(requests)}


def rd(rid, ch, s, e, lo, up):
    return {"reading_id": rid, "channel": ch, "start": s, "end": e,
            "lower": F(lo), "upper": F(up)}


class DomainTests(unittest.TestCase):
    def test_empty_observations_feasible_and_canonical(self):
        dom = MeteringDomain(device(), [], 2)
        self.assertTrue(dom.is_feasible())
        w = dom.witness()
        self.assertIsNone(dom.verify_witness(w))
        # Lexicographically smallest feasible point is the all-zero curve.
        self.assertEqual([s["e"] for s in w["slots"]], [F(0), F(0)])
        self.assertEqual(w["conservation_residual"], 0)

    def test_direction_is_request_bound_not_replay_value(self):
        # A negative request slot can accept anything from 0 up to the bound.
        dom = MeteringDomain(device(requests=(-10, 10)), [], 2)
        w = dom.witness()
        self.assertEqual(w["slots"][0]["direction"], "discharge")
        self.assertEqual(w["slots"][0]["import"], 0)
        self.assertEqual(w["slots"][1]["export"], 0)

    def test_soc_coupling_correlates_slots(self):
        # cap 10, initial 0, two charge slots, total import pinned to 10:
        # any split c0+c1=10 with each <=10 is feasible; independent per-slot
        # boxes would wrongly allow c0=c1=10.
        dom = MeteringDomain(device(capacity=10),
                             [rd("t", "import", 0, 2, 10, 10)], 2)
        self.assertTrue(dom.is_feasible())
        dom2 = MeteringDomain(device(capacity=10),
                              [rd("t", "import", 0, 2, 11, 11)], 2)
        self.assertFalse(dom2.is_feasible())

    def test_non_unit_efficiency_energy_and_losses(self):
        d = device(capacity=100, initial=10, mc=50, md=50, ec=(4, 5),
                   ed=(2, 3), requests=(20, -15))
        dom = MeteringDomain(d, [rd("a", "import", 0, 1, 10, 10),
                                 rd("b", "export", 1, 2, 6, 6)], 2)
        w = dom.witness()
        self.assertIsNone(dom.verify_witness(w))
        # charge e=10 -> soc +8, loss 2; discharge e=6 -> soc -9, loss 3.
        self.assertEqual(w["slots"][0]["loss"], F(2))
        self.assertEqual(w["slots"][0]["soc_after"], F(18))
        self.assertEqual(w["slots"][1]["loss"], F(3))
        self.assertEqual(w["slots"][1]["soc_after"], F(9))

    def test_loss_reading_binds_correlated_curve(self):
        d = device(capacity=100, initial=0, mc=50, md=50, ec=(4, 5),
                   ed=(1, 1), requests=(20, 20))
        # loss = e/5; total loss 2 means total import 10.
        dom = MeteringDomain(d, [rd("L", "loss", 0, 2, 2, 2)], 2)
        w = dom.witness()
        self.assertIsNone(dom.verify_witness(w))
        self.assertEqual(w["total_import"], F(10))

    def test_contradiction_kept_and_minimal_conflict(self):
        d = device()
        readings = [rd("a", "import", 0, 2, 10, 10),
                    rd("b", "import", 0, 2, 20, 20)]
        dom = MeteringDomain(d, readings, 2)
        conflict = dom.reading_conflict()
        self.assertIsNotNone(conflict)
        # Singleton minimal conflicts are valid: one tight reading alone may be
        # infeasible against the device/SOC constraints (import 20 exceeds the
        # capacity of 10).  Whatever the returned set, it must be infeasible
        # while each single removal restores feasibility.
        ids = set(conflict["reading_ids"])
        self.assertTrue(ids.issubset({"a", "b"}))
        self.assertEqual(len(conflict["removals"]), len(ids))
        kept_indices = [i for i, r in enumerate(readings)
                        if r["reading_id"] in ids]
        for item in conflict["removals"]:
            # Independently re-solve the reduced system (minimal set minus the
            # named member) and substitute the returned witness back into it.
            retained = [i for i in kept_indices
                        if readings[i]["reading_id"] != item["reading_id"]]
            self.assertIsNone(dom.verify_witness(item["witness"], retained))
            fresh = dom.witness(reading_indices=retained)
            self.assertIsNone(dom.verify_witness(fresh, retained))
            # The minimal conflict set itself stays infeasible.
            self.assertFalse(dom.is_feasible(reading_indices=kept_indices))

    def test_conflict_minimality_with_three_readings(self):
        d = device(requests=(5, 5))
        readings = [rd("x", "import", 0, 1, 5, 5),
                    rd("y", "import", 1, 2, 5, 5),
                    rd("z", "import", 0, 2, 0, 0)]  # contradicts x+y
        dom = MeteringDomain(d, readings, 2)
        conflict = dom.reading_conflict()
        # The deletion filter returns an inclusion-minimal set containing z
        # (x and y alone are jointly feasible); it may keep z alone or alongside
        # another member, but every single removal must restore feasibility.
        self.assertIn("z", conflict["reading_ids"])
        kept_ids = set(conflict["reading_ids"])
        kept_indices = [i for i, r in enumerate(readings)
                        if r["reading_id"] in kept_ids]
        for item in conflict["removals"]:
            retained = [i for i in kept_indices
                        if readings[i]["reading_id"] != item["reading_id"]]
            # The removal witness is independently reproducible and satisfies
            # every retained reading and device/SOC constraint.
            self.assertIsNone(dom.verify_witness(item["witness"], retained))
            fresh = dom.witness(reading_indices=retained)
            self.assertIsNone(dom.verify_witness(fresh, retained))
        self.assertFalse(dom.is_feasible(reading_indices=kept_indices))
        # Explicitly confirm the feasible core: x and y alone are jointly
        # feasible, while a zero-total reading is feasible on its own.
        self.assertTrue(dom.is_feasible(reading_indices=[0, 1]))
        self.assertTrue(dom.is_feasible(reading_indices=[2]))

    def test_every_removal_witness_independently_substitutes_back(self):
        # A denser contradictory system: several tight readings where the
        # deletion filter leaves a multi-member minimal set.  Every removal
        # witness is re-derived through a fresh solve and checked against the
        # complete reduced constraint system (device + all retained readings).
        d = device(capacity=10, requests=(5, 5, 5))
        readings = [
            rd("p", "import", 0, 3, 12, 12),     # total import fixed at 12
            rd("q", "import", 0, 1, 5, 5),       # slot 0 already at its max
            rd("r", "import", 1, 2, 5, 5),       # slot 1 at its max
            rd("s", "import", 2, 3, 5, 5),       # forces 15 > 12, contradiction
        ]
        dom = MeteringDomain(d, readings, 3)
        conflict = dom.reading_conflict()
        self.assertIsNotNone(conflict)
        kept = [i for i, r in enumerate(readings)
                if r["reading_id"] in set(conflict["reading_ids"])]
        # The minimal set is genuinely infeasible and properly minimal.
        self.assertFalse(dom.is_feasible(reading_indices=kept))
        for item in conflict["removals"]:
            retained = [i for i in kept
                        if readings[i]["reading_id"] != item["reading_id"]]
            # Fresh, independent solve of the reduced system.
            fresh = dom.witness(reading_indices=retained)
            self.assertIsNone(dom.verify_witness(fresh, retained))
            # The shipped witness equals the re-derived witness point on the
            # full timeline (canonical zero-cost solution is deterministic).
            for a, b in zip(item["witness"]["slots"], fresh["slots"]):
                self.assertEqual(a["e"], b["e"])
                self.assertEqual(a["soc_after"], b["soc_after"])

    def test_outside_window_discharge_tightens_invoice_window(self):
        # cap 10, initial 10: slot 0 discharge of 10 empties the battery, so a
        # later charge slot is bounded by the empty SOC boundary.
        d = device(capacity=10, initial=10, requests=(-10, 10))
        dom = MeteringDomain(d, [rd("x", "export", 0, 1, 10, 10)], 2)
        w = dom.witness()
        # slot 1 still has room to charge 10 after full discharge; tighten
        # further with an SOC-adjacent reading.
        self.assertIsNone(dom.verify_witness(w))
        dom2 = MeteringDomain(
            d, [rd("x", "export", 0, 1, 10, 10),
                rd("y", "import", 1, 2, 3, 3)], 2)
        self.assertTrue(dom2.is_feasible())
        w2 = dom2.witness()
        self.assertEqual(w2["slots"][1]["import"], F(3))


if __name__ == "__main__":
    unittest.main()
