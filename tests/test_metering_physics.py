"""Shared physics contract: v1 replay and the metering domain must agree.

These tests do not merely check that each side conserves energy on its own;
they force one identical energy curve through both implementations and compare
every per-slot SOC and loss term, under non-unit bidirectional efficiencies,
full charge, empty discharge and zero requests.
"""
import unittest
from fractions import Fraction

from storage_dispatch.dispatch import simulate
from storage_dispatch.model import analyze
from storage_dispatch.metering.domain import MeteringDomain

F = Fraction


def force_curve_device(requests, ec=(4, 5), ed=(2, 3), capacity=100,
                       initial=20, mc=200, md=50):
    device = {"capacity": capacity, "initial": initial, "max_charge": mc,
              "max_discharge": md, "eta_c": F(*ec), "eta_d": F(*ed),
              "requests": list(requests)}
    replay = simulate(capacity, initial, mc, md, F(*ec), F(*ed), requests)
    readings = []
    for step in replay["steps"]:
        t = step["slot"]
        if step["grid_in"] != "0":
            readings.append({"reading_id": f"i{t}", "channel": "import",
                             "start": t, "end": t + 1,
                             "lower": F(step["grid_in"]),
                             "upper": F(step["grid_in"])})
        if step["grid_out"] != "0":
            readings.append({"reading_id": f"e{t}", "channel": "export",
                             "start": t, "end": t + 1,
                             "lower": F(step["grid_out"]),
                             "upper": F(step["grid_out"])})
    return device, readings, replay


class PhysicsContractTests(unittest.TestCase):
    def assert_curve_matches_replay(self, device, readings, replay, slots):
        dom = MeteringDomain(device, readings, slots)
        witness = dom.witness()
        self.assertIsNone(dom.verify_witness(witness))
        for step, item in zip(replay["steps"], witness["slots"]):
            self.assertEqual(step["before"], str(item["soc_before"]))
            self.assertEqual(step["after"], str(item["soc_after"]))
            self.assertEqual(step["grid_in"], str(item["import"]))
            self.assertEqual(step["grid_out"], str(item["export"]))
            self.assertEqual(step["loss"], str(item["loss"]))
        self.assertEqual(replay["final"], str(witness["final_soc"]))
        self.assertEqual(replay["loss"], str(witness["total_loss"]))
        self.assertEqual(witness["conservation_residual"], 0)

    def test_non_unit_bidirectional_efficiencies(self):
        device, readings, replay = force_curve_device([50, -60, -10, 200])
        self.assert_curve_matches_replay(device, readings, replay, 4)

    def test_full_charge_and_empty_discharge(self):
        # Full battery rejects the charge request; an empty battery rejects the
        # discharge request -- both saturated behaviours, shared contract.
        device, readings, replay = force_curve_device(
            [10, -10], capacity=100, initial=100, mc=10, md=10)
        self.assert_curve_matches_replay(device, readings, replay, 2)
        report = analyze({
            "capacity": 100, "initial": 100, "max_charge": 10,
            "max_discharge": 10,
            "charge_efficiency": {"numerator": 4, "denominator": 5},
            "discharge_efficiency": {"numerator": 2, "denominator": 3},
            "requests": [10, -10]})
        self.assertEqual(report["steps"][0]["grid_in"], "0")
        self.assertEqual(report["steps"][0]["unmet"], "10")
        empty = analyze({
            "capacity": 100, "initial": 0, "max_charge": 10,
            "max_discharge": 10,
            "charge_efficiency": {"numerator": 4, "denominator": 5},
            "discharge_efficiency": {"numerator": 2, "denominator": 3},
            "requests": [-10]})
        self.assertEqual(empty["steps"][0]["grid_out"], "0")
        self.assertEqual(empty["steps"][0]["unmet"], "10")

    def test_zero_request_is_charge_direction_in_both(self):
        device, readings, replay = force_curve_device([0, 0, 5])
        dom = MeteringDomain(device, readings, 3)
        witness = dom.witness()
        for t in (0, 1):
            self.assertEqual(witness["slots"][t]["direction"], "charge")
            self.assertEqual(witness["slots"][t]["e"], F(0))
        self.assert_curve_matches_replay(device, readings, replay, 3)

    def test_v1_report_identical_to_recorded_fixture(self):
        import json
        from pathlib import Path
        source = json.loads(
            (Path(__file__).resolve().parents[1] / "fixtures" /
             "input.json").read_text())
        report = analyze(source)
        self.assertEqual(
            (report["final"], report["grid_in"], report["grid_out"],
             report["loss"], report["unmet"]),
            ("100", "175", "48", "47", "97"))


if __name__ == "__main__":
    unittest.main()
