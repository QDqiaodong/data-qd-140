"""Invoice (ticket) audit over the correlated measurement domain.

For every invoice

``F(e) = sum prices[t-start] * import_t
         + demand_rate * max_k (sum_{k<=t<k+window} import_t / window)``

the module computes the exact minimum and maximum over the *whole*
measurement feasible polyhedron (device, SOC and every retained observation),
each with a full-slot attaining witness.

The demand peak is a real maximum.  Whenever ``demand_rate`` is positive a
peak variable is added together with one dominance inequality per window and,
for extrema/branching, an explicit equality tying it to one window's average;
a free variable merely sitting above every window average can never prove
feasibility.  When ``demand_rate`` is zero no peak variable or peak restriction
is introduced at all.

The joint conclusion is not derived from per-invoice ranges: it searches for a
single measurement curve satisfying every ``F(e) = amount`` at once.  The
equality is non-convex (disjunctive over which window attains the peak), so the
search branches over peak windows on top of one shared exact LP.
"""
from fractions import Fraction

from ..reserve.simplex import Simplex, Infeasible
from .domain import MeteringInfeasible, witness_to_jsonable

ZERO = Fraction(0)
ONE = Fraction(1)


class InvoiceInfeasible(Exception):
    """No curve satisfies the requested invoice equations."""


class BillingSolver:
    def __init__(self, domain, invoices):
        self.domain = domain
        self.invoices = invoices
        self.d = domain
        # One peak variable per positive demand-rate invoice.
        self.peak_var = {}
        n = domain.n
        for i, inv in enumerate(invoices):
            if inv["demand_rate"] > 0:
                self.peak_var[i] = n
                n += 1
        self.total_vars = n

    # ------------------------------------------------------------- row parts
    def energy_row(self, inv):
        """Coefficients of the energy charge part of F."""
        row = {}
        for t in range(inv["start"], inv["end"]):
            if t in self.d.charge_slots:
                price = inv["prices"][t - inv["start"]]
                if price:
                    row[self.d.energy_var(t)] = Fraction(price)
        return row

    def window_rows(self, inv):
        """One coefficient row per demand window (window-average numerator/w).

        Each row maps import variables to ``1/window`` so the row value is the
        window's average grid-side charging energy (an energy, never power).
        Discharge slots carry no coefficient, so discharge never offsets
        charging fees.
        """
        w = inv["window"]
        rows = []
        for k in range(inv["start"], inv["end"] - w + 1):
            row = {}
            for t in range(k, k + w):
                if t in self.d.charge_slots:
                    row[self.d.energy_var(t)] = ONE / w
            rows.append(row)
        return rows

    def dominance_rows(self, inv):
        """``peak >= window average`` for every window of the invoice."""
        if inv["demand_rate"] <= 0:
            return []
        p = self.peak_var[self.invoices.index(inv)]
        return [({p: ONE, **{j: -a for j, a in row.items()}}, ">=", ZERO)
                for row in self.window_rows(inv)]

    def _peak_equality(self, inv, window_row):
        p = self.peak_var[self.invoices.index(inv)]
        row = {p: ONE}
        for j, a in window_row.items():
            row[j] = -a
        return (row, "=", ZERO)

    # ----------------------------------------------------------------- solve
    def _solve(self, extra_rows, costs=None):
        constraints = self.d.base_constraints()
        constraints += self.d.reading_constraints(
            list(range(len(self.d.readings))))
        constraints += list(extra_rows)
        lp = Simplex(self.total_vars, constraints, costs or {},
                     self.d.levels)
        return lp.solve()

    def _feasible(self, extra_rows):
        try:
            self._solve(extra_rows)
            return True
        except (MeteringInfeasible, Infeasible):
            return False

    def _witness(self, values):
        return self.d.materialize(values[:self.d.n])

    # ------------------------------------------------------------ evaluation
    def evaluate(self, inv, witness):
        """Compute F independently from a materialized full-timeline witness."""
        energy = ZERO
        for t in range(inv["start"], inv["end"]):
            energy += (Fraction(inv["prices"][t - inv["start"]])
                       * Fraction(witness["slots"][t]["import"]))
        w = inv["window"]
        peak = ZERO
        for k in range(inv["start"], inv["end"] - w + 1):
            avg = sum((Fraction(witness["slots"][t]["import"])
                       for t in range(k, k + w)), ZERO) / w
            if avg > peak:
                peak = avg
        return energy + Fraction(inv["demand_rate"]) * peak, peak

    # -------------------------------------------------------------- extrema
    def invoice_bounds(self, i):
        """Exact (minimum, maximum, min_witness, max_witness) for invoice i."""
        inv = self.invoices[i]
        energy = self.energy_row(inv)
        dominance = self.dominance_rows(inv)

        # ---- minimum: one convex LP with a free dominated peak.
        costs = dict(energy)
        if inv["demand_rate"] > 0:
            costs[self.peak_var[i]] = Fraction(inv["demand_rate"])
        values = self._solve(dominance, costs)
        f_min, _ = self.evaluate(inv, self._witness(values))
        min_witness = self._witness(values)

        # ---- maximum: the max of a convex piecewise-linear function on a
        # polytope is attained on a region where one window is the peak.  Fix
        # each window in turn as an attained peak and take the largest value;
        # tied peaks are accepted naturally.
        if inv["demand_rate"] == 0:
            values = self._solve([], {j: -a for j, a in energy.items()})
            f_max, _ = self.evaluate(inv, self._witness(values))
            max_witness = self._witness(values)
        else:
            f_max = None
            max_witness = None
            for window_row in self.window_rows(inv):
                rows = dominance + [self._peak_equality(inv, window_row)]
                costs = {j: -a for j, a in energy.items()}
                costs[self.peak_var[i]] = -Fraction(inv["demand_rate"])
                try:
                    values = self._solve(rows, costs)
                except Infeasible:
                    continue
                witness = self._witness(values)
                value, _ = self.evaluate(inv, witness)
                if f_max is None or value > f_max:
                    f_max, max_witness = value, witness
            if f_max is None:
                raise InvoiceInfeasible(
                    "invoice %s has no attaining peak window"
                    % inv["invoice_id"])
        return f_min, f_max, min_witness, max_witness

    # ------------------------------------------------------------ joint solve
    def _joint_branch(self, fixed, remaining):
        """Recursively enforce invoice equalities on one shared curve."""
        if not remaining:
            try:
                return self._witness(self._solve(fixed))
            except Infeasible:
                return None
        i = remaining[0]
        inv = self.invoices[i]
        rest = remaining[1:]
        if inv["demand_rate"] == 0:
            # Pure energy equality: an exact linear row.
            trial = fixed + [(self.energy_row(inv), "=",
                              Fraction(inv["amount"]))]
            if self._feasible(trial):
                found = self._joint_branch(trial, rest)
                if found is not None:
                    return found
            return None
        # Relaxation with a free dominated peak.  Infeasibility here prunes
        # every peak branch; feasibility alone proves nothing (the peak may be
        # inflated), so the branches below pin it to a real window.
        relaxation = fixed + self.dominance_rows(inv) + [
            (self._f_row(inv), "=", Fraction(inv["amount"]))]
        if not self._feasible(relaxation):
            return None
        for window_row in self.window_rows(inv):
            trial = fixed + self.dominance_rows(inv) + [
                self._peak_equality(inv, window_row),
                (self._f_row(inv), "=", Fraction(inv["amount"]))]
            if not self._feasible(trial):
                continue
            found = self._joint_branch(trial, rest)
            if found is not None:
                return found
        return None

    def _f_row(self, inv):
        row = dict(self.energy_row(inv))
        if inv["demand_rate"] > 0:
            row[self.peak_var[self.invoices.index(inv)]] = Fraction(
                inv["demand_rate"])
        return row

    def joint_witness(self, subset=None):
        """Common full-slot curve with F(e)=amount for every selected invoice.

        Returns ``None`` when no such curve exists.  Zero-demand invoices are
        branched first (plain linear equalities); positive-demand invoices then
        enumerate their attained peak windows.
        """
        if subset is None:
            subset = list(range(len(self.invoices)))
        ordered = sorted(subset,
                         key=lambda i: self.invoices[i]["demand_rate"] > 0)
        return self._joint_branch([], ordered)

    def joint_conflict(self):
        """Minimal conflicting invoice set with a witness per single removal.

        Deletion filter: the returned set is infeasible while removing any one
        member yields a common witness for all the *other* invoices on the
        complete measurement domain -- every original meter observation,
        device limit and SOC/efficiency row stays enforced throughout.
        """
        kept = list(range(len(self.invoices)))
        for i in list(kept):
            trial = [j for j in kept if j != i]
            if self.joint_witness(trial) is None:
                kept = trial
        removals = []
        for i in kept:
            trial = [j for j in kept if j != i]
            witness = self.joint_witness(trial)
            if witness is not None and self.d.verify_witness(witness) \
                    is not None:  # pragma: no cover - defensive invariant
                raise InvoiceInfeasible(
                    "internal: joint witness violates measurement domain")
            removals.append({
                "invoice_id": self.invoices[i]["invoice_id"],
                "witness": witness,
            })
        return {
            "invoice_ids": [self.invoices[i]["invoice_id"] for i in kept],
            "removals": removals,
        }


def invoice_result_jsonable(inv_id, amount, f_min, f_max, min_witness,
                            max_witness):
    return {
        "invoice_id": inv_id,
        "amount": str(amount),
        "min": str(f_min),
        "max": str(f_max),
        "min_witness": witness_to_jsonable(min_witness),
        "max_witness": witness_to_jsonable(max_witness),
    }
