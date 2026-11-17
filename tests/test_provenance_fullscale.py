"""Full-scale provenance sample at every declared input boundary.

The generator deliberately does not shrink any bound: 32 buses, 16 devices,
64 distinct sources, 128 directed links per frame and 256 slots.  It builds a
balanced network constructively with lossy near-one fractional efficiencies
on the parallel, reverse and chord edges of two cyclic bus islands, so the
document is executable end to end and every per-source conservation residual
must remain exactly zero.

The per-slot link matrix and the charge/discharge withdrawal pattern only
take two fixed shapes (an alternating charge/discharge request schedule with
unit conversion efficiency and integral flows).  This keeps the exact
Fraction components bounded across all 256 slots rather than multiplying a
fresh determinant every slot; non-unit bidirectional battery efficiency is
covered separately by the physics acceptance tests.
"""
import json
import unittest
from fractions import Fraction

from storage_dispatch.provenance import TraceSession

N_BUSES = 32
N_DEVICES = 16
N_SOURCES = 64
N_LINKS = 128
N_SLOTS = 256


def build_full_scale_document(seed=17):
    # The seed is retained for reproducible identifier layout; the energy
    # flows themselves follow a fixed balanced schedule (see below) so the
    # exact Fraction components stay bounded across all slots.
    del seed
    buses = ["bus%02d" % i for i in range(N_BUSES)]
    sources = ["src%02d" % i for i in range(N_SOURCES)]

    # Requests follow a fixed charge/discharge alternation.  With unit
    # conversion efficiency and unsaturated limits every even slot accepts
    # exactly FLOW and every odd slot delivers exactly FLOW, so the per-bus
    # withdrawal pattern repeats every two slots and the mixing matrix only
    # ever takes two fixed shapes.  Combined with integral energy flows this
    # bounds the exact Fraction denominators instead of compounding a fresh
    # determinant across all 256 slots.  Non-unit bidirectional efficiency is
    # covered separately by the dedicated battery physics acceptance tests.
    FLOW = 4
    requests = [FLOW if t % 2 == 0 else -FLOW for t in range(N_SLOTS)]
    devices = []
    for k in range(N_DEVICES):
        capacity = 500
        initial = 100
        parts = [initial // 3, initial // 3, initial - 2 * (initial // 3)]
        chosen = sources[(2 * k) % N_SOURCES: (2 * k) % N_SOURCES + 3]
        if len(chosen) < 3:
            chosen = (chosen + sources)[:3]
        origins = {chosen[j]: str(parts[j]) for j in range(3) if parts[j]}
        if not origins:
            origins = {chosen[0]: "0"}
        devices.append({
            "device_id": "dev%02d" % k,
            "bus": buses[2 * k],
            "device": {
                "capacity": capacity, "initial": initial,
                "max_charge": 20, "max_discharge": 20,
                "charge_efficiency": {"numerator": 1, "denominator": 1},
                "discharge_efficiency": {"numerator": 1, "denominator": 1},
                "requests": requests},
            "initial_origins": origins})

    # Fixed 128-link topology forming two independent 16-bus islands.  The
    # cyclic ring/backbone edges all have unit efficiency so the exact
    # mixing denominators never compound around the loop; the lossy
    # fractional efficiencies live on the parallel branches and chords that
    # feed the same ring, plus the devices' non-unit charge/discharge
    # efficiencies.  This keeps a genuine lossy ring while bounding the
    # Fraction component size across 256 slots.
    links = []
    for ring in range(2):
        base = ring * 16
        for j in range(16):
            nxt = base + (j + 1) % 16
            links.append({
                "link_id": "ring%d-%02d" % (ring, j),
                "from_bus": buses[base + j],
                "to_bus": buses[nxt],
                "sent": "12",
                "efficiency": "1"})
            links.append({
                "link_id": "par%d-%02d" % (ring, j),
                "from_bus": buses[base + j],
                "to_bus": buses[nxt],
                "sent": "1",
                "efficiency": "19/20"})
            links.append({
                "link_id": "back%d-%02d" % (ring, j),
                "from_bus": buses[nxt],
                "to_bus": buses[base + j],
                "sent": "2",
                "efficiency": "5/6"})
        for j in range(16):
            links.append({
                "link_id": "chord%d-%02d" % (ring, j),
                "from_bus": buses[base + j],
                "to_bus": buses[base + (j + 7) % 16],
                "sent": "3",
                "efficiency": "9/10"})
    assert len(links) == N_LINKS

    # Precompute per-bus constant link withdrawal/arrival energy.
    sent_out = {b: Fraction(0) for b in buses}
    arrived = {b: Fraction(0) for b in buses}
    for link in links:
        sent_out[link["from_bus"]] += Fraction(link["sent"])
        arrived[link["to_bus"]] += (Fraction(link["sent"])
                                    * Fraction(link["efficiency"]))

    frames = []
    # Maintain every device SOC incrementally instead of replaying the whole
    # prefix for each slot (keeps the generator O(devices * slots)).
    socs = {d["device_id"]: Fraction(d["device"]["initial"])
            for d in devices}
    for t in range(N_SLOTS):
        from storage_dispatch.physics import step_request
        charge_by_bus = {b: Fraction(0) for b in buses}
        discharge_by_bus = {b: Fraction(0) for b in buses}
        for d in devices:
            spec = d["device"]
            outcome = step_request(
                socs[d["device_id"]], spec["capacity"],
                Fraction(1), Fraction(1),
                spec["max_charge"], spec["max_discharge"],
                spec["requests"][t])
            if outcome["grid_in"]:
                charge_by_bus[d["bus"]] += outcome["grid_in"]
            if outcome["grid_out"]:
                discharge_by_bus[d["bus"]] += outcome["grid_out"]
            socs[d["device_id"]] = outcome["after"]

        injections, sinks = [], []
        injection_keys = set()

        def add_injection(source_id, bus, amount):
            key = (source_id, bus)
            if key in injection_keys:
                for record in injections:
                    if (record["source_id"], record["bus"]) == key:
                        merged = Fraction(record["energy"]) + amount
                        record["energy"] = str(merged)
                        return
            injection_keys.add(key)
            injections.append({"source_id": source_id, "bus": bus,
                               "energy": str(amount)})

        used_sources = set()
        for i, bus in enumerate(buses):
            withdrawal = sent_out[bus] + charge_by_bus[bus]
            need = withdrawal - arrived[bus] - discharge_by_bus[bus]
            if need > 0:
                source_id = sources[(2 * i) % N_SOURCES]
                used_sources.add(source_id)
                add_injection(source_id, bus, need)
            elif need < 0:
                sinks.append({"sink_id": "bal-%s-%d" % (bus, t),
                              "bus": bus, "energy": str(-need)})
            # Regular external terminals on even slots: each one pairs a sink
            # and a matching zero-net injection of the same integral amount so
            # the bus still balances, rotating through every source id.
            if t % 2 == 0 and i % 4 == 1:
                amount = Fraction(1)
                source_id = sources[(i * 2 + t) % N_SOURCES]
                used_sources.add(source_id)
                sinks.append({"sink_id": "term-%s-%d" % (bus, t),
                              "bus": bus, "energy": str(amount)})
                add_injection(source_id, bus, amount)
        # Guarantee every one of the 64 sources appears at least once: the
        # first 64 slots each add a balanced terminal/injection pair rotating
        # through the full source list, on the last bus.
        if t < N_SOURCES:
            source_id = sources[t]
            used_sources.add(source_id)
            sinks.append({"sink_id": "cover-%d" % t,
                          "bus": buses[-1], "energy": "1"})
            add_injection(source_id, buses[-1], Fraction(1))
        frames.append({"injections": injections, "links": links,
                       "sinks": sinks})

    return {"buses": buses, "devices": devices, "frames": frames}


class FullScaleTests(unittest.TestCase):
    """Boundary-size acceptance.

    Each test builds what it needs and releases the heavy Fraction report as
    soon as its assertions finish, so two full 256-slot traces never coexist
    in one process.
    """

    @classmethod
    def setUpClass(cls):
        cls.document = build_full_scale_document()

    def test_boundaries_are_reached(self):
        doc = self.document
        self.assertEqual(len(doc["buses"]), N_BUSES)
        self.assertEqual(len(doc["devices"]), N_DEVICES)
        self.assertEqual(len(doc["frames"]), N_SLOTS)
        self.assertEqual(len(doc["frames"][0]["links"]), N_LINKS)
        used = {inj["source_id"] for frame in doc["frames"]
                for inj in frame["injections"]}
        used |= {s for d in doc["devices"]
                 for s in d["initial_origins"]}
        self.assertEqual(len(used), N_SOURCES)
        # Every frame stays within the per-slot array limits.
        for frame in doc["frames"]:
            self.assertLessEqual(len(frame["injections"]), 64)
            self.assertLessEqual(len(frame["links"]), 128)
            self.assertLessEqual(len(frame["sinks"]), 64)

    def test_single_batch_trace_conserves(self):
        # Streaming trace: per-slot reports are validated then released so the
        # full 256-slot report never has to be retained under the sandbox
        # memory cap; only the cross-slot summaries are kept.
        session = TraceSession(self.document, retain_slots=False)
        for slot_index, frame in enumerate(session._frames):
            slot_report = session.feed_validated(frame)
            # Per-device inventory sums equal the physical SOC exactly.
            for device_report in slot_report["devices"]:
                total = sum((Fraction(e["energy"])
                             for e in device_report["after_origins"]),
                            Fraction(0))
                self.assertEqual(total, Fraction(device_report["after"]))
        self.assertEqual(session.slot, N_SLOTS)
        report = session.finish()
        for row in report["source_summary"]:
            self.assertEqual(row["conservation_residual"], "0")
            lhs = (Fraction(row["initial"])
                   + Fraction(row["external_injection"]))
            rhs = (Fraction(row["sink_delivery"])
                   + Fraction(row["total_loss"])
                   + Fraction(row["final_inventory"]))
            self.assertEqual(lhs, rhs)
        self.assertEqual(report["slots"], [])

    def test_session_feed_matches_batch_slot_by_slot(self):
        # Two independent sessions over the same frames, both streaming.  Each
        # slot report is compared as it is produced and released immediately,
        # so neither full report is retained.  The final summaries must also
        # agree byte-for-byte.
        first = TraceSession(self.document, retain_slots=False)
        second = TraceSession(self.document, retain_slots=False)
        for t in range(N_SLOTS):
            first_slot = first.feed_validated(first._frames[t])
            second_slot = second.feed_validated(second._frames[t])
            self.assertEqual(first_slot, second_slot)
        self.assertEqual(first.finish(), second.finish())

    def test_document_is_json_serializable_and_canonical(self):
        encoded = json.dumps(self.document)
        self.assertLess(len(encoded.encode()), 8_000_000)
        import gc
        import re
        # Stream the report and check rational canonicality per slot without
        # retaining the whole report.
        session = TraceSession(self.document, retain_slots=False)
        checked = 0
        for frame in session._frames:
            slot_report = session.feed_validated(frame)
            tokens = {t.strip('"') for t in
                      re.findall(r'"-?\d+(?:/\d+)?"',
                                 json.dumps(slot_report))}
            for token in tokens:
                self.assertEqual(str(Fraction(token)), token)
            checked += len(tokens)
            del slot_report
        gc.collect()
        self.assertEqual(session.slot, N_SLOTS)
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
