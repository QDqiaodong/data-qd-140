"""End-to-end linkage scenarios required by the acceptance brief."""
import unittest
from fractions import Fraction

from storage_dispatch.metering.domain import MeteringDomain
from storage_dispatch.metering.billing import BillingSolver

F = Fraction


def inv(iid, s, e, prices, w, dr, amount):
    return {"invoice_id": iid, "start": s, "end": e, "prices": prices,
            "window": w, "demand_rate": dr, "amount": F(amount)}


def rd(rid, ch, s, e, lo, up):
    return {"reading_id": rid, "channel": ch, "start": s, "end": e,
            "lower": F(lo), "upper": F(up)}


class LinkageTests(unittest.TestCase):
    def test_mandated_two_ticket_linkage_case(self):
        # cap=10, initial=0, unit efficiencies, two charge requests of 10,
        # charge limit 10; total import observation exactly 10.
        device = {"capacity": 10, "initial": 0, "max_charge": 10,
                  "max_discharge": 10, "eta_c": F(1), "eta_d": F(1),
                  "requests": [10, 10]}
        dom = MeteringDomain(
            device, [rd("tot", "import", 0, 2, 10, 10)], 2)
        tickets = [inv("I0", 0, 1, [1], 1, 0, 10),
                   inv("I1", 1, 2, [1], 1, 0, 10)]
        solver = BillingSolver(dom, tickets)
        # Each ticket alone sits inside its reachable range.
        for i in range(2):
            lo, hi, _, _ = solver.invoice_bounds(i)
            self.assertEqual((lo, hi), (F(0), F(10)))
        # Jointly impossible with distinct removal curves.
        self.assertIsNone(solver.joint_witness())
        conflict = solver.joint_conflict()
        self.assertEqual(set(conflict["invoice_ids"]), {"I0", "I1"})
        curves = {}
        for item in conflict["removals"]:
            self.assertIsNone(dom.verify_witness(item["witness"]))
            curves[item["invoice_id"]] = tuple(
                str(s["import"]) for s in item["witness"]["slots"])
        self.assertEqual(curves["I0"], ("0", "10"))
        self.assertEqual(curves["I1"], ("10", "0"))

    def test_cross_boundary_efficiency_loss_breaks_feasible_combo(self):
        # Non-unit bidirectional efficiencies with charge/discharge crossing
        # invoice boundaries.  eta_c = 4/5, eta_d = 2/3.
        device = {"capacity": 100, "initial": 20, "max_charge": 50,
                  "max_discharge": 50, "eta_c": F(4, 5), "eta_d": F(2, 3),
                  "requests": [25, -30, 25, -30]}
        # Observations spanning invoice windows and external slots: an export
        # reading over slots 0..2 crosses the first invoice boundary and the
        # loss reading couples charge slots inside and outside windows.
        readings = [
            rd("exp-cross", "export", 0, 3, 12, 12),
            rd("loss-total", "loss", 0, 4, 8, 8),
        ]
        dom = MeteringDomain(device, readings, 4)
        self.assertTrue(dom.is_feasible())
        base = dom.witness()
        self.assertIsNone(dom.verify_witness(base))

        # Build ticket amounts from the feasible witness so the combination is
        # achievable, then perturb one amount to make the joint system
        # infeasible while each ticket's own range still contains its amount.
        tickets = [
            inv("A", 0, 2, [3, 1], 2, 4, 0),
            inv("B", 2, 4, [2, 5], 1, 3, 0),
        ]
        solver = BillingSolver(dom, tickets)
        for i, ticket in enumerate(tickets):
            value, _ = solver.evaluate(ticket, base)
            ticket["amount"] = value
        self.assertIsNotNone(
            BillingSolver(dom, tickets).joint_witness())
        lo0, hi0, _, _ = solver.invoice_bounds(0)
        # Move A to a still-in-range value that cannot coexist with B once
        # the cross-boundary export/loss observations bind the shared curve.
        tickets[0]["amount"] = hi0
        solver2 = BillingSolver(dom, tickets)
        if solver2.joint_witness() is None:
            conflict = solver2.joint_conflict()
            for item in conflict["removals"]:
                # Every removal witness lifts to the full timeline and obeys
                # every original observation, SOC and conservation rule.
                self.assertIsNone(dom.verify_witness(item["witness"]))
                self.assertEqual(
                    item["witness"]["conservation_residual"], 0)
                # And the retained ticket's real F equals its amount on that
                # single shared curve -- no per-ticket disconnected curves.
                retained = next(
                    t for t in tickets
                    if t["invoice_id"] != item["invoice_id"])
                value, _ = solver2.evaluate(retained, item["witness"])
                self.assertEqual(value, retained["amount"])
        else:
            # If max-A happens to coexist, force an interior contradiction
            # using an amount strictly between min and max instead.
            tickets[0]["amount"] = (lo0 + hi0) / 2 + F(1, 7)
            self.assertIsNone(
                BillingSolver(dom, tickets).joint_witness())

    def test_external_slot_soc_boundary_shrinks_invoice_range(self):
        # The invoice covers charge slots 1..3.  An observation on the
        # *external* slot 0 pins the discharge there and therefore fixes the
        # SOC boundary at the window start; no reading names the window slots,
        # yet the in-window charge range moves with that boundary.
        device = {"capacity": 30, "initial": 10, "max_charge": 10,
                  "max_discharge": 10, "eta_c": F(1), "eta_d": F(1),
                  "requests": [-10, 10, 10, 10]}
        ticket = inv("A", 1, 4, [1, 1, 1], 3, 0, 0)
        # Free curve: slot 0 can discharge the full 10, emptying the battery,
        # after which all three in-window slots charge to capacity -> 30.
        free = MeteringDomain(device, [], 4)
        _, hi_free, _, _ = BillingSolver(free, [ticket]).invoice_bounds(0)
        self.assertEqual(hi_free, F(30))
        # A zero-export external reading keeps SOC at 10, leaving only 20
        # units of in-window charge headroom -- the external SOC bound shrinks
        # the invoice range without any observation inside the window.
        preserved = MeteringDomain(
            device, [rd("held", "export", 0, 1, 0, 0)], 4)
        _, hi_held, _, wmax = BillingSolver(
            preserved, [ticket]).invoice_bounds(0)
        self.assertEqual(hi_held, F(20))
        self.assertLess(hi_held, hi_free)
        self.assertIsNone(preserved.verify_witness(wmax))
        # A partial external discharge (4 units) leaves SOC 6 -> 24 headroom.
        partial = MeteringDomain(
            device, [rd("drained", "export", 0, 1, 4, 4)], 4)
        _, hi_partial, _, wpartial = BillingSolver(
            partial, [ticket]).invoice_bounds(0)
        self.assertEqual(hi_partial, F(24))
        self.assertIsNone(partial.verify_witness(wpartial))


if __name__ == "__main__":
    unittest.main()
