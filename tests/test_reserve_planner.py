"""Unit tests for the exact canonical planner."""
import unittest
from fractions import Fraction

from storage_dispatch.reserve.planner import plan, InfeasiblePlan

F = Fraction


def charge_slots(prices_and_limits, discharge_limits=None):
    out = []
    for i, (price, cl) in enumerate(prices_and_limits):
        dl = discharge_limits[i] if discharge_limits else 0
        out.append({"charge_limit": cl, "discharge_limit": dl, "price": price})
    return out


class PlannerTests(unittest.TestCase):
    def test_no_contracts_feasible_plan(self):
        slots = [{"charge_limit": 5, "discharge_limit": 0, "price": 1},
                 {"charge_limit": 0, "discharge_limit": 5, "price": 2}]
        r = plan(100, F(1), F(1), 0, slots, 0, F(0), [])
        self.assertEqual(r["cost"], "0")
        self.assertTrue(all(s["charge"] == "0" for s in r["slots"]))

    def test_terminal_min_forces_cheapest_charge(self):
        slots = [{"charge_limit": 50, "discharge_limit": 0, "price": 3},
                 {"charge_limit": 50, "discharge_limit": 0, "price": 1}]
        r = plan(100, F(1), F(1), 7, slots, 0, F(0), [])
        # Charging is cheapest in slot 1.
        self.assertEqual(r["slots"][0]["charge"], "0")
        self.assertEqual(r["slots"][1]["charge"], "7")
        self.assertEqual(r["cost"], "7")
        self.assertEqual(r["terminal_soc"], "7")

    def test_overlapping_windows_share_discharge_limit(self):
        slots = [{"charge_limit": 0, "discharge_limit": 0, "price": 0},
                 {"charge_limit": 0, "discharge_limit": 5, "price": 0},
                 {"charge_limit": 0, "discharge_limit": 5, "price": 0}]
        # Two contracts overlap on slot 1 whose shared limit is 5.
        r = plan(1000, F(1), F(1), 0, slots, 0, F(100), [
            {"contract_id": "A", "start": 1, "end": 3, "remaining": 5},
            {"contract_id": "B", "start": 1, "end": 2, "remaining": 3}])
        s1 = r["slots"][1]
        self.assertLessEqual(F(s1["discharge_total"]), 5)
        self.assertEqual(F(s1["deliveries"]["B"]), 3)
        self.assertEqual(F(r["contracts"]["A"]["planned"])
                         + F(r["contracts"]["B"]["planned"]), 8)

    def test_over_capacity_is_infeasible_not_greedy(self):
        slots = [{"charge_limit": 0, "discharge_limit": 4, "price": 0}]
        with self.assertRaises(InfeasiblePlan):
            plan(1000, F(1), F(1), 0, slots, 0, F(100), [
                {"contract_id": "A", "start": 0, "end": 1, "remaining": 3},
                {"contract_id": "B", "start": 0, "end": 1, "remaining": 3}])

    def test_not_per_contract_greedy(self):
        # A greedy per-contract scheduler would charge in the first usable
        # slot; the global optimum uses the cheaper later slot.
        slots = [
            {"charge_limit": 100, "discharge_limit": 0, "price": 10},
            {"charge_limit": 100, "discharge_limit": 0, "price": 1},
            {"charge_limit": 0, "discharge_limit": 100, "price": 0}]
        r = plan(1000, F(1), F(1), 0, slots, 0, F(0), [
            {"contract_id": "A", "start": 2, "end": 3, "remaining": 9}])
        self.assertEqual(r["slots"][0]["charge"], "0")
        self.assertEqual(r["slots"][1]["charge"], "9")
        self.assertEqual(r["cost"], "9")

    def test_canonical_tie_delivers_in_latest_slot(self):
        # Equal prices and two delivery slots; lex-min pushes deliveries to
        # the later slot because the earlier slot's variables are minimized
        # first.
        slots = [{"charge_limit": 0, "discharge_limit": 0, "price": 0},
                 {"charge_limit": 0, "discharge_limit": 10, "price": 0},
                 {"charge_limit": 0, "discharge_limit": 10, "price": 0}]
        r = plan(1000, F(1), F(1), 0, slots, 0, F(100), [
            {"contract_id": "A", "start": 1, "end": 3, "remaining": 4},
            {"contract_id": "B", "start": 1, "end": 3, "remaining": 4}])
        self.assertEqual(r["slots"][1]["discharge_total"], "0")
        self.assertEqual(r["slots"][2]["deliveries"], {"A": "4", "B": "4"})

    def test_canonical_tie_contract_id_ascii_order(self):
        # Two slots, both windows span both.  Slot 0 shared limit is 3, slot 1
        # limit is 7; the two contracts need 5 each so slot 0 must carry 3.
        # Lex-min minimizes slot-0 variables in id order, so the ASCII-smaller
        # id A is driven to zero there and B absorbs the binding delivery.
        slots = [{"charge_limit": 0, "discharge_limit": 3, "price": 0},
                 {"charge_limit": 0, "discharge_limit": 7, "price": 0}]
        r = plan(1000, F(1), F(1), 0, slots, 0, F(100), [
            {"contract_id": "Z", "start": 0, "end": 2, "remaining": 5},
            {"contract_id": "A", "start": 0, "end": 2, "remaining": 5}])
        d0 = r["slots"][0]["deliveries"]
        self.assertEqual(F(d0.get("A", "0")) + F(d0.get("Z", "0")), 3)
        self.assertEqual(F(d0.get("A", "0")), 0)
        self.assertEqual(F(d0["Z"]), 3)

    def test_fractional_charge_efficiency(self):
        slots = [{"charge_limit": 100, "discharge_limit": 0, "price": 1},
                 {"charge_limit": 0, "discharge_limit": 100, "price": 0}]
        # eta_c = 1/2: to store 3 SOC units, 6 grid units must be purchased.
        r = plan(1000, F(1, 2), F(1), 3, slots, 0, F(0), [])
        self.assertEqual(r["slots"][0]["charge"], "6")
        self.assertEqual(r["terminal_soc"], "3")
        self.assertEqual(r["cost"], "6")

    def test_fractional_discharge_efficiency(self):
        slots = [{"charge_limit": 0, "discharge_limit": 100, "price": 0}]
        # eta_d = 2/3: delivering 1 grid unit drains 3/2 SOC.
        r = plan(1000, F(1), F(2, 3), 0, slots, 0, F(2), [
            {"contract_id": "A", "start": 0, "end": 1, "remaining": 1}])
        self.assertEqual(r["slots"][0]["discharge_total"], "1")
        self.assertEqual(r["terminal_soc"], str(F(2) - F(3, 2)))

    def test_all_outputs_are_canonical_fraction_strings(self):
        slots = [{"charge_limit": 7, "discharge_limit": 0, "price": 1},
                 {"charge_limit": 0, "discharge_limit": 7, "price": 0}]
        r = plan(1000, F(4, 5), F(4, 5), 0, slots, 0, F(0), [
            {"contract_id": "A", "start": 1, "end": 2, "remaining": 2}])
        for s in r["slots"]:
            for key in ("soc_before", "soc_after", "charge",
                        "discharge_total"):
                # Every value round-trips through Fraction exactly.
                self.assertEqual(str(Fraction(s[key])), s[key])

    def test_replan_after_advance_uses_prefix_soc(self):
        slots = [{"charge_limit": 0, "discharge_limit": 10, "price": 0},
                 {"charge_limit": 0, "discharge_limit": 10, "price": 0}]
        # cursor=1, prefix ended with SOC 5; deliver 5 from there.
        r = plan(1000, F(1), F(1), 0, slots, 1, F(5), [
            {"contract_id": "A", "start": 1, "end": 2, "remaining": 5}])
        self.assertEqual(len(r["slots"]), 1)
        self.assertEqual(r["slots"][0]["soc_before"], "5")
        self.assertEqual(r["terminal_soc"], "0")


if __name__ == "__main__":
    unittest.main()
