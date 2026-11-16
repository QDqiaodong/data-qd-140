"""Invoice extrema, true-peak enforcement and joint conflict tests."""
import unittest
from fractions import Fraction

from storage_dispatch.metering.domain import MeteringDomain
from storage_dispatch.metering.billing import BillingSolver

F = Fraction


def make(capacity=20, initial=0, mc=10, md=10, ec=(1, 1), ed=(1, 1),
         requests=(10, 10)):
    return {"capacity": capacity, "initial": initial, "max_charge": mc,
            "max_discharge": md, "eta_c": F(*ec), "eta_d": F(*ed),
            "requests": list(requests)}


def rd(rid, ch, s, e, lo, up):
    return {"reading_id": rid, "channel": ch, "start": s, "end": e,
            "lower": F(lo), "upper": F(up)}


def inv(iid, s, e, prices, w, dr, amount):
    return {"invoice_id": iid, "start": s, "end": e, "prices": prices,
            "window": w, "demand_rate": dr, "amount": F(amount)}


class BillingTests(unittest.TestCase):
    def test_zero_demand_fee_has_no_peak_variable(self):
        dom = MeteringDomain(make(), [], 2)
        solver = BillingSolver(dom, [inv("A", 0, 2, [1, 1], 1, 0, 0)])
        self.assertEqual(solver.peak_var, {})
        lo, hi, wmin, wmax = solver.invoice_bounds(0)
        self.assertEqual((lo, hi), (F(0), F(20)))
        self.assertIsNone(dom.verify_witness(wmin))
        self.assertIsNone(dom.verify_witness(wmax))

    def test_demand_peak_is_a_real_max_not_a_free_variable(self):
        # Total import fixed at 10 over two slots; one window (w=2) so the
        # true peak average is 5.  An inflated free peak would claim 10.
        dom = MeteringDomain(
            make(capacity=20), [rd("t", "import", 0, 2, 10, 10)], 2)
        ticket = inv("A", 0, 2, [0, 0], 2, 10, 0)
        solver = BillingSolver(dom, [ticket])
        lo, hi, _, _ = solver.invoice_bounds(0)
        self.assertEqual((lo, hi), (F(50), F(50)))
        # amount 100 would need a free peak of 10: must be rejected.
        ticket["amount"] = F(100)
        self.assertIsNone(BillingSolver(dom, [ticket]).joint_witness())
        # amount 50 attains the real peak and is feasible.
        ticket["amount"] = F(50)
        witness = BillingSolver(dom, [ticket]).joint_witness()
        self.assertIsNotNone(witness)
        value, peak = BillingSolver(dom, [ticket]).evaluate(ticket, witness)
        self.assertEqual((value, peak), (F(50), F(5)))

    def test_all_zero_energy_positive_amount_is_impossible(self):
        dom = MeteringDomain(
            make(), [rd("z", "import", 0, 2, 0, 0)], 2)
        # Every positive amount is unattainable when the only curve is zero.
        tickets = [inv("A", 0, 2, [1, 1], 1, 5, 3),
                   inv("B", 0, 2, [0, 0], 2, 7, "1/2")]
        solver = BillingSolver(dom, tickets)
        self.assertIsNone(solver.joint_witness())
        # Each ticket is individually infeasible too, so the minimal conflict
        # may legitimately collapse to one member; every removal witness is
        # still the unique all-zero curve and satisfies all observations.
        conflict = solver.joint_conflict()
        self.assertTrue(set(conflict["invoice_ids"]).issubset({"A", "B"}))
        for item in conflict["removals"]:
            self.assertIsNotNone(item["witness"])
            self.assertIsNone(dom.verify_witness(item["witness"]))
            self.assertEqual(item["witness"]["total_import"], F(0))
        # And an amount of exactly zero is possible.
        zero_tickets = [inv("A", 0, 2, [1, 1], 1, 5, 0),
                        inv("B", 0, 2, [0, 0], 2, 7, 0)]
        self.assertIsNotNone(
            BillingSolver(dom, zero_tickets).joint_witness())

    def test_tied_peaks_are_legal_and_extreme_attained(self):
        # Two slots, w=1: equal imports give two tied peak windows.
        dom = MeteringDomain(
            make(), [rd("t", "import", 0, 2, 8, 8)], 2)
        ticket = inv("A", 0, 2, [0, 0], 1, 10, 0)
        solver = BillingSolver(dom, [ticket])
        lo, hi, wmin, wmax = solver.invoice_bounds(0)
        # peak <= 8 (each slot <=10 but total fixed at 8): max fee 80.
        self.assertEqual(hi, F(80))
        value, peak = solver.evaluate(ticket, wmax)
        self.assertEqual(value, hi)

    def test_fractional_inputs_and_independent_F_check(self):
        d = make(capacity=30, mc=12, ec=(3, 4), ed=(2, 3),
                 requests=(8, -6, 12))
        dom = MeteringDomain(d, [rd("a", "import", 0, 3, "3/2", 20)], 3)
        ticket = inv("A", 0, 3, [2, 0, 3], 2, 4, "7/3")
        solver = BillingSolver(dom, [ticket])
        lo, hi, wmin, wmax = solver.invoice_bounds(0)
        self.assertIsInstance(lo, Fraction)
        self.assertIsInstance(hi, Fraction)
        self.assertEqual(solver.evaluate(ticket, wmin)[0], lo)
        self.assertEqual(solver.evaluate(ticket, wmax)[0], hi)
        self.assertIsNone(dom.verify_witness(wmin))
        self.assertIsNone(dom.verify_witness(wmax))

    def test_minimal_invoice_conflict_and_removal_witnesses(self):
        # The mandated linkage: total import 10, two tickets each demand 10 in
        # a disjoint single slot -> individually in range, jointly impossible.
        dom = MeteringDomain(
            make(capacity=10), [rd("t", "import", 0, 2, 10, 10)], 2)
        tickets = [inv("I0", 0, 1, [1], 1, 0, 10),
                   inv("I1", 1, 2, [1], 1, 0, 10)]
        solver = BillingSolver(dom, tickets)
        for i in range(2):
            lo, hi, _, _ = solver.invoice_bounds(i)
            self.assertEqual((lo, hi), (F(0), F(10)))
        self.assertIsNone(solver.joint_witness())
        conflict = solver.joint_conflict()
        self.assertEqual(set(conflict["invoice_ids"]), {"I0", "I1"})
        curves = []
        for item in conflict["removals"]:
            witness = item["witness"]
            self.assertIsNone(dom.verify_witness(witness))
            curves.append(tuple(str(s["import"]) for s in witness["slots"]))
            # Independently re-evaluate the retained ticket's real F: it must
            # equal that ticket's amount on this single shared curve.
            retained = next(t for t in tickets
                            if t["invoice_id"] != item["invoice_id"])
            value, _ = solver.evaluate(retained, witness)
            self.assertEqual(value, retained["amount"])
        # Removing I0 lets slot 1 carry the 10; removing I1 lets slot 0.
        self.assertEqual(set(curves), {("0", "10"), ("10", "0")})

    def test_peak_window_change_and_nonconvex_branch(self):
        # Total import 10 across two slots; expensive slot 0, window 1.
        dom = MeteringDomain(
            make(capacity=20), [rd("t", "import", 0, 2, 10, 10)], 2)
        ticket = inv("A", 0, 2, [100, 1], 1, 10, 0)
        solver = BillingSolver(dom, [ticket])
        lo, hi, wmin, wmax = solver.invoice_bounds(0)
        # min: import all in cheap slot 1 (peak window 1); max all in slot 0.
        self.assertEqual(wmin["slots"][0]["import"], F(0))
        self.assertEqual(wmin["slots"][1]["import"], F(10))
        self.assertEqual(wmax["slots"][0]["import"], F(10))
        self.assertEqual(wmax["slots"][1]["import"], F(0))
        # Interior amount selects a non-convex branch with a rational split.
        ticket["amount"] = F(505)
        witness = BillingSolver(dom, [ticket]).joint_witness()
        self.assertIsNotNone(witness)
        self.assertEqual(
            BillingSolver(dom, [ticket]).evaluate(ticket, witness)[0],
            F(505))
        self.assertIsNone(dom.verify_witness(witness))


if __name__ == "__main__":
    unittest.main()
