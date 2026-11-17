"""Battery step parity between the v1 replay and the provenance session.

The physical per-slot step is shared, so the accepted grid energy, SOC, loss
and unmet produced by ``dispatch.simulate`` must match the provenance slot
report term by term -- including non-unit bidirectional efficiencies, full
charge/discharge saturation and zero requests.  The per-source inventory
movement is checked independently here, not taken from the engine.
"""
import random
import unittest
from fractions import Fraction

from storage_dispatch.dispatch import simulate
from storage_dispatch.physics import step_request, is_charge
from storage_dispatch.provenance import TraceSession


def v1_device(capacity, initial, eta_c, eta_d, mc, md, requests):
    return {
        "capacity": capacity, "initial": initial, "max_charge": mc,
        "max_discharge": md,
        "charge_efficiency": {"numerator": eta_c.numerator,
                              "denominator": eta_c.denominator},
        "discharge_efficiency": {"numerator": eta_d.numerator,
                                 "denominator": eta_d.denominator},
        "requests": requests,
    }


def minimal_document(devices, slots, charge_supply=None,
                     discharge_sinks=None):
    """Build a document with one isolated bus per device.

    ``charge_supply`` / ``discharge_sinks`` map device_id -> slot -> exact
    grid-side energy; values are derived independently from
    :func:`step_request`, never from the engine.
    """
    buses = sorted({d["bus"] for d in devices})
    frames = []
    for t in range(slots):
        injections, sinks = [], []
        for d in devices:
            supply = (charge_supply or {}).get(d["device_id"], {})
            if supply.get(t):
                injections.append({"source_id": "GRID", "bus": d["bus"],
                                   "energy": str(supply[t])})
            drain = (discharge_sinks or {}).get(d["device_id"], {})
            if drain.get(t):
                sinks.append({"sink_id": "sink-%s-%d" % (d["device_id"], t),
                              "bus": d["bus"], "energy": str(drain[t])})
        frames.append({"injections": injections, "links": [], "sinks": sinks})
    return {"buses": buses,
            "devices": [{"device_id": d["device_id"], "bus": d["bus"],
                         "device": d["device"],
                         "initial_origins": d["origins"]} for d in devices],
            "frames": frames}


def expected_flows(capacity, initial, eta_c, eta_d, mc, md, requests):
    """Independent v1 simulation giving each slot's accepted grid energy."""
    soc = Fraction(initial)
    supply, drains = {}, {}
    for t, request in enumerate(requests):
        outcome = step_request(soc, capacity, eta_c, eta_d, mc, md, request)
        if outcome["grid_in"]:
            supply[t] = outcome["grid_in"]
        if outcome["grid_out"]:
            drains[t] = outcome["grid_out"]
        soc = outcome["after"]
    return supply, drains


class StepParityTests(unittest.TestCase):
    def _random_case(self, seed):
        rng = random.Random(seed)
        capacity = rng.randrange(10, 200)
        initial = rng.randrange(0, capacity + 1)
        eta_c = Fraction(rng.randrange(1, 10), rng.randrange(10, 60))
        eta_d = Fraction(rng.randrange(1, 10), rng.randrange(10, 60))
        mc = rng.randrange(0, 50)
        md = rng.randrange(0, 50)
        requests = [rng.randrange(-40, 41) for _ in range(rng.randrange(1, 40))]
        return capacity, initial, eta_c, eta_d, mc, md, requests

    def test_step_request_matches_v1_simulate(self):
        for seed in range(60):
            capacity, initial, eta_c, eta_d, mc, md, requests = \
                self._random_case(seed)
            replay = simulate(capacity, initial, mc, md, eta_c, eta_d,
                              requests)
            soc = Fraction(initial)
            for i, request in enumerate(requests):
                outcome = step_request(soc, capacity, eta_c, eta_d, mc, md,
                                       request)
                expected = replay["steps"][i]
                self.assertEqual(str(soc), expected["before"])
                self.assertEqual(str(outcome["after"]), expected["after"])
                self.assertEqual(str(outcome["grid_in"]),
                                 expected["grid_in"])
                self.assertEqual(str(outcome["grid_out"]),
                                 expected["grid_out"])
                self.assertEqual(str(outcome["loss"]), expected["loss"])
                self.assertEqual(str(outcome["unmet"]), expected["unmet"])
                self.assertEqual(is_charge(request), outcome["charge"])
                soc = outcome["after"]
            self.assertEqual(str(soc), replay["final"])

    def test_saturation_boundaries(self):
        eta = Fraction(1, 3)
        # Full charge: request exceeds both limit and headroom.
        out = step_request(Fraction(9), 10, eta, eta, 100, 100, 10)
        self.assertEqual(out["grid_in"], Fraction(3))
        self.assertEqual(out["after"], 10)
        self.assertEqual(out["unmet"], 7)
        # Full discharge: request exceeds limit and SOC delivery.
        out = step_request(Fraction(9), 10, eta, eta, 100, 2, -100)
        self.assertEqual(out["grid_out"], Fraction(2))
        self.assertEqual(out["after"], Fraction(3))
        self.assertEqual(out["unmet"], 98)
        # Zero request: charge direction, nothing accepted, no origins move.
        out = step_request(Fraction(5), 10, eta, eta, 100, 100, 0)
        self.assertTrue(out["charge"])
        self.assertEqual((out["grid_in"], out["grid_out"], out["loss"],
                          out["unmet"]), (Fraction(0),) * 4)
        self.assertEqual(out["after"], 5)

    def test_session_physical_fields_match_v1(self):
        eta_c, eta_d = Fraction(3, 4), Fraction(2, 5)
        requests = [12, -8, 0, 30, -40, 5]
        device = v1_device(50, 20, eta_c, eta_d, 9, 7, requests)
        origins = {"R": "12", "G": "8"}
        supply, drains = expected_flows(50, 20, eta_c, eta_d, 9, 7, requests)
        doc = minimal_document(
            [{"device_id": "D", "bus": "b0", "device": device,
              "origins": origins}], len(requests),
            {"D": supply}, {"D": drains})
        session = TraceSession(doc)
        for frame in doc["frames"]:
            session.feed(frame)
        report = session.finish()
        replay = simulate(50, 20, 9, 7, eta_c, eta_d, requests)
        for slot_report, step in zip(report["slots"], replay["steps"]):
            actual = slot_report["devices"][0]
            for key in ("before", "after", "grid_in", "grid_out", "loss",
                        "unmet"):
                self.assertEqual(actual[key], step[key], key)

    def test_inventory_movement_independent_oracle(self):
        eta_c, eta_d = Fraction(2, 3), Fraction(4, 7)
        requests = [9, -6, 15, -10, 0, -30]
        device = v1_device(80, 12, eta_c, eta_d, 10, 8, requests)
        origins = {"R": "9", "G": "3"}
        supply, drains = expected_flows(80, 12, eta_c, eta_d, 10, 8, requests)
        doc = minimal_document(
            [{"device_id": "D", "bus": "b0", "device": device,
              "origins": origins}], len(requests),
            {"D": supply}, {"D": drains})
        session = TraceSession(doc)
        for frame in doc["frames"]:
            session.feed(frame)
        report = session.finish()

        # Independent bookkeeping using only the spec and the slot outcomes.
        inventory = {"R": Fraction(9), "G": Fraction(3)}
        soc = Fraction(12)
        for t, request in enumerate(requests):
            outcome = step_request(soc, 80, eta_c, eta_d, 10, 8, request)
            slot_report = report["slots"][t]["devices"][0]
            if outcome["grid_out"]:
                d = outcome["grid_out"]
                for source, amount in list(inventory.items()):
                    removed = (d / eta_d) * amount / soc
                    delivered = d * amount / soc
                    inventory[source] -= removed
                    self.assertEqual(
                        Fraction(dict(
                            (e["source_id"], e["energy"])
                            for e in slot_report["output_origins"])[source]),
                        delivered)
                    self.assertEqual(
                        Fraction(dict(
                            (e["source_id"], e["energy"])
                            for e in slot_report[
                                "conversion_loss_origins"])[source]),
                        removed - delivered)
            if outcome["grid_in"]:
                # Isolated bus: the single GRID injection has share 1, so the
                # accepted part and the conversion loss are entirely GRID.
                c = outcome["grid_in"]
                inventory["GRID"] = inventory.get("GRID", Fraction(0)) \
                    + eta_c * c
                inputs = {e["source_id"]: Fraction(e["energy"])
                          for e in slot_report["input_origins"]}
                self.assertEqual(inputs, {"GRID": c})
                losses = {e["source_id"]: Fraction(e["energy"])
                          for e in slot_report[
                              "conversion_loss_origins"]}
                self.assertEqual(losses["GRID"], (1 - eta_c) * c)
            self.assertEqual(
                sum(inventory.values(), Fraction(0)), outcome["after"])
            soc = outcome["after"]

    def test_charge_inventory_with_split_origins(self):
        # Two external sources feed one bus while the battery charges; the
        # independent oracle computes the accepted shares from the bus mix.
        eta_c = Fraction(3, 5)
        device = v1_device(40, 0, eta_c, Fraction(1), 10, 10, [10, -6])
        doc = {
            "buses": ["b0"],
            "devices": [{"device_id": "D", "bus": "b0", "device": device,
                         "initial_origins": {"R": "0"}}],
            "frames": [
                {"injections": [
                    {"source_id": "R", "bus": "b0", "energy": "6"},
                    {"source_id": "G", "bus": "b0", "energy": "4"}],
                 "links": [], "sinks": []},
                {"injections": [], "links": [],
                 "sinks": [{"sink_id": "s", "bus": "b0", "energy": "6"}]}],
        }
        report = TraceSession(doc)
        for frame in doc["frames"]:
            report.feed(frame)
        result = report.finish()
        slot0 = result["slots"][0]["devices"][0]
        # grid_in = min(10, 10, 40/eta_c) = 10; shares 6/10 and 4/10.
        self.assertEqual(slot0["grid_in"], "10")
        accepted = {e["source_id"]: Fraction(e["energy"])
                    for e in slot0["after_origins"]}
        self.assertEqual(accepted["R"], eta_c * 6)
        self.assertEqual(accepted["G"], eta_c * 4)
        charge_loss = {e["source_id"]: Fraction(e["energy"])
                       for e in slot0["conversion_loss_origins"]}
        self.assertEqual(charge_loss["R"], (1 - eta_c) * 6)
        self.assertEqual(charge_loss["G"], (1 - eta_c) * 4)
        self.assertEqual(sum(accepted.values(), Fraction(0)),
                         Fraction(slot0["after"]))
        # Slot 1 discharge at eta_d = 1 drains the mixed inventory.
        sink = {e["source_id"]: Fraction(e["energy"])
                for e in result["slots"][1]["sinks"][0]["origins"]}
        self.assertEqual(sink["R"], eta_c * 6)
        self.assertEqual(sink["G"], eta_c * 4)


if __name__ == "__main__":
    unittest.main()
