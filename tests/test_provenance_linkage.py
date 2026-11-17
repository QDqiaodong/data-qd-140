"""End-to-end linkage acceptance: cross-slot provenance and feed atomicity.

Implements the two-device, two-slot scenario from the feature contract with
the exact hand-computed expectations, the source-rename invariance of the
physical SOC, and the failed-feed/retry equivalence of session and batch.
"""
import json
import unittest
from fractions import Fraction

from storage_dispatch.provenance import (TraceSession, trace_document,
                                         trace_provenance, Unbalanced)


def scenario(source_name="B", unbalanced=False):
    link_sent = "15" if unbalanced else "14"
    return {
        "buses": ["U", "V"],
        "devices": [
            {"device_id": "A", "bus": "U",
             "device": {"capacity": 100, "initial": 4, "max_charge": 100,
                        "max_discharge": 100,
                        "charge_efficiency": {"numerator": 1,
                                              "denominator": 1},
                        "discharge_efficiency":
                            {"numerator": 1, "denominator": 2},
                        "requests": [-2, 0]},
             "initial_origins": {"R": "4"}},
            {"device_id": "Bdev", "bus": "V",
             "device": {"capacity": 100, "initial": 0, "max_charge": 100,
                        "max_discharge": 100,
                        "charge_efficiency":
                            {"numerator": 1, "denominator": 2},
                        "discharge_efficiency":
                            {"numerator": 1, "denominator": 1},
                        "requests": [4, -2]},
             "initial_origins": {"R": "0"}}],
        "frames": [
            {"injections": [{"source_id": source_name, "bus": "U",
                             "energy": "2"}],
             "links": [
                 {"link_id": "l1", "from_bus": "U", "to_bus": "V",
                  "sent": link_sent, "efficiency": "1"},
                 {"link_id": "l2", "from_bus": "V", "to_bus": "U",
                  "sent": "10", "efficiency": "1"}],
             "sinks": []},
            {"injections": [], "links": [],
             "sinks": [{"sink_id": "t1", "bus": "V", "energy": "2"}]}],
    }


def origins(slot_report, device_id, field):
    device_report = next(d for d in slot_report["devices"]
                         if d["device_id"] == device_id)
    return {entry["source_id"]: entry["energy"]
            for entry in device_report[field]}


class LinkageAcceptanceTests(unittest.TestCase):
    def test_hand_computed_two_slot_trace(self):
        report = trace_document(scenario())
        slot0, slot1 = report["slots"]

        a0 = next(d for d in slot0["devices"] if d["device_id"] == "A")
        self.assertEqual(a0["grid_out"], "2")
        self.assertEqual(a0["loss"], "2")
        self.assertEqual(origins(slot0, "A", "conversion_loss_origins"),
                         {"R": "2"})
        self.assertEqual(origins(slot0, "A", "output_origins"), {"R": "2"})

        b0 = next(d for d in slot0["devices"] if d["device_id"] == "Bdev")
        self.assertEqual(b0["grid_in"], "4")
        self.assertEqual(b0["loss"], "2")
        self.assertEqual(origins(slot0, "Bdev", "after_origins"),
                         {"R": "1", "B": "1"})
        self.assertEqual(
            origins(slot0, "Bdev", "conversion_loss_origins"),
            {"R": "1", "B": "1"})

        # Both buses share the 50/50 composition once mixed.
        for bus_report in slot0["buses"]:
            self.assertEqual(
                {e["source_id"]: e["share"]
                 for e in bus_report["shares"]},
                {"R": "1/2", "B": "1/2"})

        sink = {entry["source_id"]: entry["energy"]
                for entry in slot1["sinks"][0]["origins"]}
        self.assertEqual(sink, {"R": "1", "B": "1"})

        summary = {row["source_id"]: row for row in report["source_summary"]}
        self.assertEqual(summary["R"]["conservation_residual"], "0")
        self.assertEqual(summary["B"]["conservation_residual"], "0")
        self.assertEqual(summary["R"]["initial"], "4")
        self.assertEqual(summary["B"]["external_injection"], "2")

    def test_rename_changes_origins_but_not_physical_soc(self):
        report_b = trace_document(scenario("B"))
        report_c = trace_document(scenario("C"))
        for key in ("initial", "final"):
            self.assertEqual(
                [d[key] for d in report_b["devices"]],
                [d[key] for d in report_c["devices"]])
        b_inventory = origins(report_b["slots"][0], "Bdev", "after_origins")
        c_inventory = origins(report_c["slots"][0], "Bdev", "after_origins")
        self.assertEqual(set(b_inventory), {"R", "B"})
        self.assertEqual(set(c_inventory), {"R", "C"})
        sink_b = {e["source_id"] for e in
                  report_b["slots"][1]["sinks"][0]["origins"]}
        sink_c = {e["source_id"] for e in
                  report_c["slots"][1]["sinks"][0]["origins"]}
        self.assertEqual(sink_b, {"R", "B"})
        self.assertEqual(sink_c, {"R", "C"})

    def test_failed_feed_leaves_state_untouched_and_retry_matches_batch(self):
        broken = scenario(unbalanced=True)
        session = TraceSession(broken)
        snapshot_soc = session.socs()
        snapshot_inventory = session.inventories()
        with self.assertRaises(Unbalanced):
            session.feed(broken["frames"][0])
        self.assertEqual(session.slot, 0)
        self.assertEqual(session.socs(), snapshot_soc)
        self.assertEqual(session.inventories(), snapshot_inventory)
        self.assertEqual(session.slot_reports(), [])

        fixed = scenario()
        session.feed(fixed["frames"][0])
        self.assertEqual(session.slot, 1)
        session.feed(fixed["frames"][1])
        retried = session.finish()
        fresh = trace_document(fixed)
        self.assertEqual(json.dumps(retried, sort_keys=True),
                         json.dumps(fresh, sort_keys=True))

        # A second failed attempt mid-stream also rolls back fully.
        session2 = TraceSession(fixed)
        session2.feed(fixed["frames"][0])
        soc_after_one = session2.socs()
        with self.assertRaises(Unbalanced):
            session2.feed(broken["frames"][0])
        self.assertEqual(session2.slot, 1)
        self.assertEqual(session2.socs(), soc_after_one)

    def test_finish_requires_every_slot(self):
        fixed = scenario()
        session = TraceSession(fixed)
        session.feed(fixed["frames"][0])
        with self.assertRaises(Exception):
            session.finish()

    def test_batch_bytes_api_includes_sha(self):
        raw = json.dumps(scenario()).encode()
        report = trace_provenance(raw)
        import hashlib
        self.assertEqual(report["source_sha256"],
                         hashlib.sha256(raw).hexdigest())
        self.assertEqual(report["schema_version"], 2)

    def test_per_source_conservation_on_report(self):
        report = trace_document(scenario())
        for row in report["source_summary"]:
            lhs = Fraction(row["initial"]) + Fraction(
                row["external_injection"])
            rhs = (Fraction(row["sink_delivery"])
                   + Fraction(row["total_loss"])
                   + Fraction(row["final_inventory"]))
            self.assertEqual(lhs, rhs)
            self.assertEqual(row["conservation_residual"], "0")


if __name__ == "__main__":
    unittest.main()
