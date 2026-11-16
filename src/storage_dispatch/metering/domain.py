"""The metering feasible domain: one correlated rational energy polyhedron.

The domain describes what the device *could actually have accepted* given the
already-made requests and the meter observations.  It is deliberately not a
slot-by-slot collection of independent bounds: the SOC state couples every
slot, so feasibility, witnesses and all later invoice extrema are decided on
the full polyhedron by one exact linear program.

Variables (all non-negative structural energy/SOC variables):

* ``c_t`` -- grid-side charge energy accepted in slot *t*, which exists only
  when the slot request is non-negative;
* ``d_t`` -- grid-side discharge energy, which exists only for negative
  requests; a slot therefore moves in exactly the request's direction;
* ``s_t`` -- SOC at the boundary before slot *t*, for ``t = 0..T``.

Constraints always retained ("slot capacity constraints"):

* ``s_0 = device.initial`` and ``0 <= s_t <= capacity`` for every boundary;
* ``0 <= c_t <= min(request_t, max_charge)`` on charge slots,
  ``0 <= d_t <= min(-request_t, max_discharge)`` on discharge slots;
* the SOC transition from :mod:`storage_dispatch.physics`:
  charge ``s_{t+1} = s_t + eta_c c_t``,
  discharge ``s_{t+1} = s_t - d_t / eta_d``.

Each reading adds one correlated range row over *all* slots of its
``[start, end)`` window on its channel:
``lower <= sum(channel energy or loss over the window) <= upper``.

No curve is ever interpolated and no independent per-slot box is substituted.
The exact two-phase :mod:`~storage_dispatch.reserve.simplex` engine is reused;
the future reservation planner is never used as a metering curve.
"""
from fractions import Fraction

from ..physics import is_charge, slot_losses
from ..reserve.simplex import Simplex, Infeasible

ZERO = Fraction(0)
ONE = Fraction(1)


class MeteringInfeasible(Exception):
    """The measurement domain (or the added rows) admits no rational curve."""


class MeteringDomain:
    """Build and query the measurement feasible polyhedron."""

    def __init__(self, device, readings, slots):
        self.device = device
        self.readings = list(readings)
        self.slots = slots
        self.eta_c = Fraction(device["eta_c"])
        self.eta_d = Fraction(device["eta_d"])
        self.requests = device["requests"]
        self.capacity = Fraction(device["capacity"])
        self.initial = Fraction(device["initial"])
        self.max_charge = Fraction(device["max_charge"])
        self.max_discharge = Fraction(device["max_discharge"])
        self.charge_slots = {t for t, r in enumerate(self.requests)
                             if is_charge(r)}
        # Variable layout.
        self.c_idx = {}
        self.d_idx = {}
        n = 0
        for t in range(slots):
            if t in self.charge_slots:
                self.c_idx[t] = n
            else:
                self.d_idx[t] = n
            n += 1
        self.s_idx = {t: n + t for t in range(slots + 1)}
        self.n = n + slots + 1
        # Canonical lexicographic order: slot ascending, energy variable of
        # the slot first, then SOC boundaries.  Zero-cost feasibility solves
        # therefore return a deterministic canonical witness.
        self.levels = {}
        level = 1
        for t in range(slots):
            self.levels[self.energy_var(t)] = level
            level += 1
        for t in range(slots + 1):
            self.levels[self.s_idx[t]] = level
            level += 1

    # ------------------------------------------------------------ structure
    def energy_var(self, t):
        return self.c_idx[t] if t in self.charge_slots else self.d_idx[t]

    def base_constraints(self):
        """Device/SOC constraints that are retained for every diagnosis."""
        rows = [({self.s_idx[0]: ONE}, "=", self.initial)]
        for t in range(self.slots):
            ev = self.energy_var(t)
            if t in self.charge_slots:
                bound = min(Fraction(self.requests[t]), self.max_charge)
                rows.append(({ev: ONE}, "<=", bound))
                trow = {self.s_idx[t + 1]: ONE, self.s_idx[t]: -ONE,
                        ev: -self.eta_c}
            else:
                bound = min(Fraction(-self.requests[t]), self.max_discharge)
                rows.append(({ev: ONE}, "<=", bound))
                trow = {self.s_idx[t + 1]: ONE, self.s_idx[t]: -ONE,
                        ev: ONE / self.eta_d}
            rows.append((trow, "=", ZERO))
        for t in range(1, self.slots + 1):
            rows.append(({self.s_idx[t]: ONE}, "<=", self.capacity))
            rows.append(({self.s_idx[t]: -ONE}, "<=", ZERO))
        return rows

    def reading_row(self, reading):
        """Coefficient row of one observation over its channel window."""
        row = {}
        for t in range(reading["start"], reading["end"]):
            ev = self.energy_var(t)
            if reading["channel"] == "import":
                if t in self.charge_slots:
                    row[ev] = row.get(ev, ZERO) + ONE
            elif reading["channel"] == "export":
                if t not in self.charge_slots:
                    row[ev] = row.get(ev, ZERO) + ONE
            else:  # loss
                charge = t in self.charge_slots
                coeff = (ONE - self.eta_c) if charge else (
                    ONE / self.eta_d - ONE)
                row[ev] = row.get(ev, ZERO) + coeff
        return row

    def reading_constraints(self, indices):
        rows = []
        for i in indices:
            reading = self.readings[i]
            row = self.reading_row(reading)
            # Both bounds are always emitted as separate rows so that every
            # retained reading restricts the curve on both sides.
            rows.append((dict(row), "<=", reading["upper"]))
            rows.append((dict(row), ">=", reading["lower"]))
        return rows

    # ---------------------------------------------------------------- solving
    def solve(self, extra_rows=(), reading_indices=None, costs=None):
        """Solve the domain LP and return raw variable values (Fractions).

        ``reading_indices`` selects which observations are enforced (all by
        default); device/SOC rows are always present.  ``extra_rows`` appends
        invoice-derived rows.  ``costs`` optionally sets a linear objective;
        with no objective the canonical lexicographically smallest feasible
        point is returned.
        """
        if reading_indices is None:
            reading_indices = range(len(self.readings))
        constraints = self.base_constraints()
        constraints += self.reading_constraints(list(reading_indices))
        constraints += list(extra_rows)
        lp = Simplex(self.n, constraints, costs or {}, self.levels)
        try:
            return lp.solve()
        except Infeasible as exc:
            raise MeteringInfeasible(str(exc))

    def is_feasible(self, reading_indices=None, extra_rows=()):
        try:
            self.solve(reading_indices=reading_indices, extra_rows=extra_rows)
            return True
        except MeteringInfeasible:
            return False

    # ------------------------------------------------------------- witnesses
    def materialize(self, values):
        """Turn raw LP values into a full-timeline energy/SOC witness."""
        witness_slots = []
        total_in = total_out = total_loss = ZERO
        for t in range(self.slots):
            charge = t in self.charge_slots
            e = values[self.energy_var(t)]
            if e < 0:
                raise MeteringInfeasible("solver returned negative energy")
            grid_in = e if charge else ZERO
            grid_out = ZERO if charge else e
            loss = slot_losses(charge, self.eta_c, self.eta_d, e)
            before = values[self.s_idx[t]]
            after = values[self.s_idx[t + 1]]
            total_in += grid_in
            total_out += grid_out
            total_loss += loss
            witness_slots.append({
                "slot": t,
                "request": self.requests[t],
                "direction": "charge" if charge else "discharge",
                "e": e,
                "import": grid_in,
                "export": grid_out,
                "loss": loss,
                "soc_before": before,
                "soc_after": after,
            })
        final_soc = values[self.s_idx[self.slots]]
        return {
            "slots": witness_slots,
            "total_import": total_in,
            "total_export": total_out,
            "total_loss": total_loss,
            "initial_soc": self.initial,
            "final_soc": final_soc,
            "conservation_residual": (self.initial + total_in - total_out
                                      - total_loss - final_soc),
        }

    def witness(self, reading_indices=None, extra_rows=(), costs=None):
        values = self.solve(reading_indices=reading_indices,
                            extra_rows=extra_rows, costs=costs)
        return self.materialize(values)

    def verify_witness(self, witness, reading_indices=None):
        """Independently re-check every device and observation constraint."""
        if reading_indices is None:
            reading_indices = range(len(self.readings))
        slots = witness["slots"]
        if len(slots) != self.slots:
            return "slot count mismatch"
        soc = self.initial
        for t, item in enumerate(slots):
            if item["slot"] != t:
                return "slot order mismatch at %d" % t
            charge = t in self.charge_slots
            if item["direction"] != ("charge" if charge else "discharge"):
                return "direction mismatch at %d" % t
            e = Fraction(item["e"])
            if e < 0:
                return "negative energy at %d" % t
            if charge:
                if e > min(Fraction(self.requests[t]), self.max_charge):
                    return "charge bound violated at %d" % t
                if Fraction(item["import"]) != e or item["export"] != 0:
                    return "import/export mismatch at %d" % t
            else:
                if e > min(Fraction(-self.requests[t]), self.max_discharge):
                    return "discharge bound violated at %d" % t
                if Fraction(item["export"]) != e or item["import"] != 0:
                    return "import/export mismatch at %d" % t
            if Fraction(item["loss"]) != slot_losses(
                    charge, self.eta_c, self.eta_d, e):
                return "loss mismatch at %d" % t
            if Fraction(item["soc_before"]) != soc:
                return "SOC chain broken at %d" % t
            soc = soc + (e * self.eta_c if charge else -e / self.eta_d)
            if not (ZERO <= soc <= self.capacity):
                return "SOC out of range at %d" % t
            if Fraction(item["soc_after"]) != soc:
                return "SOC after mismatch at %d" % t
        if witness["conservation_residual"] != 0:
            return "conservation residual nonzero"
        for i in reading_indices:
            reading = self.readings[i]
            total = ZERO
            for t in range(reading["start"], reading["end"]):
                item = slots[t]
                total += Fraction(item[reading["channel"]])
            if not (reading["lower"] <= total <= reading["upper"]):
                return "reading %s violated" % reading["reading_id"]
        return None

    # ------------------------------------------------------- conflict report
    def reading_conflict(self):
        """Return a minimal infeasible reading set, or ``None`` if feasible.

        A deletion filter produces an inclusion-minimal set: the set itself is
        infeasible while dropping any single member (all device/SOC rows
        retained) restores feasibility.  Every removal gets its own full
        witness.  Indices always refer to the original reading list, including
        members discarded earlier by the filter, so each removal witness is
        solved and checked against the exact set of retained observations.
        """
        if self.is_feasible():
            return None
        # Deletion filter over the original indices.  At the end ``kept`` is
        # an inclusion-minimal infeasible subset: the set itself is infeasible,
        # while dropping any single member and keeping the other members of
        # ``kept`` (all device/SOC rows always present) restores feasibility.
        # Each removal witness is therefore independently reproducible by
        # solving that exact reduced system and re-checking every retained
        # reading and every device/SOC constraint.
        kept = list(range(len(self.readings)))
        for i in range(len(self.readings)):
            if i not in kept:
                continue
            trial = [j for j in kept if j != i]
            if not self.is_feasible(reading_indices=trial):
                kept = trial
        removals = []
        for i in kept:
            trial = [j for j in kept if j != i]
            witness = self.witness(reading_indices=trial)
            if self.verify_witness(witness, trial) is not None:  # pragma: no cover
                raise MeteringInfeasible(
                    "internal: removal witness failed substitution check")
            removals.append({
                "reading_id": self.readings[i]["reading_id"],
                "witness": witness,
            })
        return {
            "reading_ids": [self.readings[i]["reading_id"] for i in kept],
            "removals": removals,
        }


def witness_to_jsonable(witness):
    """Exact-Fraction witness -> canonical-string JSON structure."""
    out = dict(witness)
    out["slots"] = [{k: (str(v) if isinstance(v, Fraction) else v)
                     for k, v in item.items()} for item in witness["slots"]]
    for key in ("total_import", "total_export", "total_loss", "initial_soc",
                "final_soc", "conservation_residual"):
        out[key] = str(witness[key])
    return out
