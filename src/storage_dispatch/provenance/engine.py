"""Per-slot provenance tracing engine and the batch entry point.

The engine owns two pieces of state that cross slot boundaries:

* the physical SOC of every battery, advanced exclusively by the shared
  :func:`storage_dispatch.physics.step_request` -- the same step the v1 replay
  uses, so accepted energy, saturation and losses can never diverge; and
* the per-source internal inventory of every battery, whose values always sum
  exactly to that battery's SOC.

Nothing else crosses slots: bus mixing proportions are recomputed from
scratch each slot and never cached.  A failed :meth:`TraceSession.feed` leaves
every SOC, inventory and the slot index untouched because all per-slot work is
done on local copies and committed only after the full slot succeeds.
"""
import hashlib
from fractions import Fraction

from ..physics import step_request
from .mixing import solve_slot
from .schema import (ProvenanceError, validate_document, validate_frame,
                     parse_strict_json)

ZERO = Fraction(0)
ONE = Fraction(1)


def _sorted_origins(mapping):
    """Serialize a source -> Fraction map, omitting zero entries."""
    return [{"source_id": source_id, "energy": str(mapping[source_id])}
            for source_id in sorted(mapping) if mapping[source_id] != 0]


class TraceSession:
    """Stateful, slot-by-slot provenance trace.

    Construct with a validated document (the parsed ``{buses, devices,
    frames}`` object).  Each :meth:`feed` consumes exactly one frame using the
    devices' request for that slot; :meth:`finish` is legal only after every
    slot has succeeded.  The batch helper :func:`trace_provenance` runs the
    identical feed/finish path over pre-validated frames.
    """

    def __init__(self, document, source_sha256=None, retain_slots=True):
        """Build a session over a validated document.

        When ``retain_slots`` is false the per-slot reports are not kept on
        the instance: each committed report is returned from :meth:`feed` as
        usual, but :meth:`finish` omits the ``slots`` array.  This keeps the
        memory footprint flat for very long traces; the per-source summary
        and final battery inventories are always retained.
        """
        normalized = validate_document(document)
        self._buses = normalized["buses"]
        self._bus_set = set(normalized["buses"])
        self._slots = len(normalized["frames"])
        self._frames = normalized["frames"]
        self._source_sha256 = source_sha256
        self._all_sources = set(normalized["sources"])
        self._retain_slots = retain_slots

        self._devices = []
        self._initial_map = {}
        for entry in normalized["devices"]:
            spec = entry["device"]
            origins = dict(entry["initial_origins"])
            self._initial_map[entry["device_id"]] = origins
            self._devices.append({
                "device_id": entry["device_id"],
                "bus": entry["bus"],
                "spec": spec,
                "soc": Fraction(spec["initial"]),
                "inventory": dict(origins),
            })

        self._slot = 0
        self._slot_reports = []
        self._totals = {source_id: {
            "initial": ZERO, "external_injection": ZERO,
            "sink_delivery": ZERO, "link_loss": ZERO,
            "conversion_loss": ZERO}
            for source_id in self._all_sources}
        for entry in normalized["devices"]:
            for source_id, amount in entry["initial_origins"].items():
                self._totals[source_id]["initial"] += amount

    # ------------------------------------------------------------------ state
    @property
    def slot(self):
        """Index of the next slot to feed (0 before the first feed)."""
        return self._slot

    @property
    def slots(self):
        return self._slots

    @property
    def finished(self):
        return self._slot == self._slots

    @property
    def buses(self):
        return list(self._buses)

    @property
    def source_ids(self):
        return set(self._all_sources)

    def socs(self):
        """Current physical SOC per device (exact Fractions)."""
        return {d["device_id"]: d["soc"] for d in self._devices}

    def inventories(self):
        """Deep copy of every device's per-source internal inventory."""
        return {d["device_id"]: dict(d["inventory"]) for d in self._devices}

    def slot_reports(self):
        return [dict(report) for report in self._slot_reports]

    # ------------------------------------------------------------------- feed
    def feed(self, frame):
        """Validate and trace one frame; commit state only on full success."""
        if self.finished:
            raise ProvenanceError("all slots have already been traced")
        normalized = validate_frame(frame, self._bus_set, self._all_sources)
        report = self._run_slot(self._slot, normalized)
        self._commit(report)
        self._slot += 1
        return report

    def feed_validated(self, normalized_frame):
        """Feed an already normalized frame (used by the batch path)."""
        if self.finished:
            raise ProvenanceError("all slots have already been traced")
        report = self._run_slot(self._slot, normalized_frame)
        self._commit(report)
        self._slot += 1
        return report

    def _run_slot(self, slot_index, frame):
        """Compute a complete slot on local copies; never mutates self."""
        # Snapshot the slot-start state and take the shared physical step.
        starts, steps = [], []
        for device in self._devices:
            spec = device["spec"]
            starts.append((device["soc"], dict(device["inventory"])))
            steps.append(step_request(
                device["soc"], spec["capacity"], spec["eta_c"],
                spec["eta_d"], spec["max_charge"], spec["max_discharge"],
                spec["requests"][slot_index]))

        # Battery discharge is a bus injection whose composition is the
        # slot-start inventory; inventory removals and conversion losses are
        # derived from that same composition.
        discharge_by_bus = {bus: {} for bus in self._buses}
        discharge_info = []
        for device, start, outcome in zip(self._devices, starts, steps):
            soc_before, inventory_before = start
            d = outcome["grid_out"]
            output_origins, loss_origins, removals = {}, {}, {}
            if d != 0:
                if soc_before == 0:
                    raise ProvenanceError(
                        "device %s cannot deliver from an empty SOC"
                        % device["device_id"])
                for source_id, amount in inventory_before.items():
                    if amount == 0:
                        continue
                    grid_out = d * amount / soc_before
                    internal_out = (d / device["spec"]["eta_d"]) * \
                        amount / soc_before
                    output_origins[source_id] = grid_out
                    removals[source_id] = internal_out
                    loss_origins[source_id] = internal_out - grid_out
                bucket = discharge_by_bus[device["bus"]]
                for source_id, amount in output_origins.items():
                    bucket[source_id] = bucket.get(source_id, ZERO) + amount
            discharge_info.append((output_origins, loss_origins, removals))

        # Charging is a bus withdrawal using the physical accepted energy.
        charge_by_bus = {bus: ZERO for bus in self._buses}
        for device, outcome in zip(self._devices, steps):
            if outcome["grid_in"] != 0:
                charge_by_bus[device["bus"]] += outcome["grid_in"]

        solution = solve_slot(
            self._buses, frame["injections"], discharge_by_bus,
            charge_by_bus, frame["sinks"], frame["links"])
        shares = solution["shares"]
        charge_origins = solution["charge_origins"]

        # Build the post-slot battery state on local copies.
        device_reports = []
        new_states = []
        for device, start, outcome, info in zip(
                self._devices, starts, steps, discharge_info):
            soc_before, inventory_before = start
            output_origins, discharge_loss, removals = info
            inventory_after = dict(inventory_before)
            input_origins, charge_loss = {}, {}
            if outcome["charge"]:
                c = outcome["grid_in"]
                if c != 0:
                    eta_c = device["spec"]["eta_c"]
                    composition = charge_origins[device["bus"]]
                    for source_id, grid_part in composition.items():
                        accepted = eta_c * grid_part
                        inventory_after[source_id] = (
                            inventory_after.get(source_id, ZERO) + accepted)
                        input_origins[source_id] = grid_part
                        charge_loss[source_id] = (ONE - eta_c) * grid_part
                    # Exact SOC/inventory agreement with the physical step.
                    if sum(inventory_after.values(), ZERO) != outcome["after"]:
                        raise ProvenanceError(
                            "device %s inventory does not match physical SOC"
                            % device["device_id"])
            else:
                for source_id, internal_out in removals.items():
                    inventory_after[source_id] = (
                        inventory_after.get(source_id, ZERO) - internal_out)
                if sum(inventory_after.values(), ZERO) != outcome["after"]:
                    raise ProvenanceError(
                        "device %s inventory does not match physical SOC"
                        % device["device_id"])
            conversion_loss = dict(discharge_loss)
            for source_id, amount in charge_loss.items():
                conversion_loss[source_id] = (
                    conversion_loss.get(source_id, ZERO) + amount)
            new_states.append((outcome["after"], inventory_after))
            device_reports.append({
                "device_id": device["device_id"],
                "bus": device["bus"],
                "request": device["spec"]["requests"][slot_index],
                "before": str(soc_before),
                "after": str(outcome["after"]),
                "grid_in": str(outcome["grid_in"]),
                "grid_out": str(outcome["grid_out"]),
                "loss": str(outcome["loss"]),
                "unmet": str(outcome["unmet"]),
                "before_origins": _sorted_origins(inventory_before),
                "after_origins": _sorted_origins(inventory_after),
                "input_origins": _sorted_origins(input_origins),
                "output_origins": _sorted_origins(output_origins),
                "conversion_loss_origins": _sorted_origins(
                    conversion_loss),
            })
        device_reports.sort(key=lambda item: item["device_id"])

        bus_reports = [{
            "bus": bus,
            "withdrawal": str(
                sum((link["sent"] for link in frame["links"]
                     if link["sent"] != 0 and link["from_bus"] == bus),
                    ZERO)
                + charge_by_bus[bus]
                + sum((sink["energy"] for sink in frame["sinks"]
                       if sink["energy"] != 0 and sink["bus"] == bus),
                      ZERO)),
            "shares": [{"source_id": s, "share": str(p)}
                       for s, p in sorted(shares.get(bus, {}).items())],
        } for bus in self._buses]

        link_reports = [{
            "link_id": item["link_id"],
            "from_bus": item["from_bus"],
            "to_bus": item["to_bus"],
            "sent": str(item["sent"]),
            "arrived": str(item["arrived"]),
            "loss": str(item["loss"]),
            "arrival_origins": _sorted_origins(item["arrival_origins"]),
            "loss_origins": _sorted_origins(item["loss_origins"]),
        } for item in sorted(solution["links"], key=lambda x: x["link_id"])]

        sink_reports = [{
            "sink_id": item["sink_id"],
            "bus": item["bus"],
            "energy": str(item["energy"]),
            "origins": _sorted_origins(item["origins"]),
        } for item in sorted(solution["sinks"], key=lambda x: x["sink_id"])]

        injection_reports = sorted(
            ({"source_id": item["source_id"], "bus": item["bus"],
              "energy": str(item["energy"]),
              "origins": [{"source_id": item["source_id"],
                           "energy": str(item["energy"])}]
              if item["energy"] != 0 else []}
             for item in frame["injections"]),
            key=lambda item: (item["source_id"], item["bus"]))

        slot_totals = {
            "injection": {}, "sink": {}, "link_loss": {},
            "conversion_loss": {}, "new_sources": set(frame["sources"])}
        for item in frame["injections"]:
            if item["energy"] == 0:
                continue
            slot_totals["injection"][item["source_id"]] = (
                slot_totals["injection"].get(item["source_id"], ZERO)
                + item["energy"])
        for item in solution["sinks"]:
            for source_id, amount in item["origins"].items():
                slot_totals["sink"][source_id] = (
                    slot_totals["sink"].get(source_id, ZERO) + amount)
        for item in solution["links"]:
            for source_id, amount in item["loss_origins"].items():
                slot_totals["link_loss"][source_id] = (
                    slot_totals["link_loss"].get(source_id, ZERO) + amount)
        for report in device_reports:
            for entry in report["conversion_loss_origins"]:
                source_id = entry["source_id"]
                amount = Fraction(entry["energy"])
                slot_totals["conversion_loss"][source_id] = (
                    slot_totals["conversion_loss"].get(source_id, ZERO)
                    + amount)

        return {
            "slot": slot_index,
            "devices": device_reports,
            "buses": bus_reports,
            "links": link_reports,
            "sinks": sink_reports,
            "injections": injection_reports,
            "_new_states": new_states,
            "_totals": slot_totals,
        }

    def _commit(self, report):
        new_states = report.pop("_new_states")
        slot_totals = report.pop("_totals")
        for device, (new_soc, new_inventory) in zip(self._devices,
                                                    new_states):
            device["soc"] = new_soc
            device["inventory"] = {s: v for s, v in new_inventory.items()
                                   if v != 0}
        for source_id in slot_totals.pop("new_sources"):
            if source_id not in self._totals:
                self._totals[source_id] = {
                    "initial": ZERO, "external_injection": ZERO,
                    "sink_delivery": ZERO, "link_loss": ZERO,
                    "conversion_loss": ZERO}
            self._all_sources.add(source_id)
        mapping = {"injection": "external_injection",
                   "sink": "sink_delivery",
                   "link_loss": "link_loss",
                   "conversion_loss": "conversion_loss"}
        for local_key, total_key in mapping.items():
            for source_id, amount in slot_totals[local_key].items():
                self._totals[source_id][total_key] += amount
        if self._retain_slots:
            self._slot_reports.append(report)

    # ---------------------------------------------------------------- finish
    def finish(self):
        """Return the complete JSON-able provenance report."""
        if not self.finished:
            raise ProvenanceError(
                "finish requires all %d slots to succeed; %d traced"
                % (self._slots, self._slot))
        device_summaries = []
        final_inventory = {}
        for device in sorted(self._devices, key=lambda d: d["device_id"]):
            for source_id, amount in device["inventory"].items():
                final_inventory[source_id] = (
                    final_inventory.get(source_id, ZERO) + amount)
        for device in sorted(self._devices, key=lambda d: d["device_id"]):
            initial_origins = {
                s: a for s, a in self._initial_map[device["device_id"]].items()
                if a != 0}
            device_summaries.append({
                "device_id": device["device_id"],
                "bus": device["bus"],
                "initial": str(Fraction(device["spec"]["initial"])),
                "final": str(device["soc"]),
                "initial_origins": _sorted_origins(initial_origins),
                "final_origins": _sorted_origins(device["inventory"]),
            })

        source_summary = []
        for source_id in sorted(self._all_sources):
            totals = self._totals[source_id]
            final = final_inventory.get(source_id, ZERO)
            total_loss = totals["link_loss"] + totals["conversion_loss"]
            residual = (totals["initial"] + totals["external_injection"]
                        - totals["sink_delivery"] - total_loss - final)
            if residual != 0:
                raise ProvenanceError(
                    "source %s conservation residual is %s"
                    % (source_id, residual))
            source_summary.append({
                "source_id": source_id,
                "initial": str(totals["initial"]),
                "external_injection": str(totals["external_injection"]),
                "sink_delivery": str(totals["sink_delivery"]),
                "link_loss": str(totals["link_loss"]),
                "conversion_loss": str(totals["conversion_loss"]),
                "total_loss": str(total_loss),
                "final_inventory": str(final),
                "conservation_residual": "0",
            })

        report = {
            "schema_version": 2,
            "buses": list(self._buses),
            "devices": device_summaries,
            "source_summary": source_summary,
        }
        if self._retain_slots:
            report["slots"] = self._slot_reports
        else:
            # Streaming mode: callers consumed each report from feed().
            report["slots"] = []
        if self._source_sha256 is not None:
            report["source_sha256"] = self._source_sha256
        return report


def trace_document(document, source_sha256=None):
    """Batch-trace an already parsed and validated document."""
    session = TraceSession(document, source_sha256=source_sha256)
    for frame in session._frames:
        session.feed_validated(frame)
    return session.finish()


def trace_provenance(raw):
    """Batch API: strict JSON bytes in, full provenance report out."""
    data = parse_strict_json(raw)
    digest = hashlib.sha256(bytes(raw)).hexdigest()
    return trace_document(data, source_sha256=digest)
