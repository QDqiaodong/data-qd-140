"""Independent exact oracle for the metering domain and invoice audit.

This oracle is deliberately written without using the production simplex,
:class:`~storage_dispatch.metering.domain.MeteringDomain` or the reservation
planner.  It works directly in the grid-side energy space ``e_0..e_{T-1}``
(the direction of every slot is fixed by its request): the SOC state is
eliminated analytically, so the whole measurement domain becomes one system of
exact linear inequalities in the *e* variables.

Feasibility and bounded linear extrema are answered by enumerating vertices
from active-constraint combinations and solving linear systems with an
independent Gaussian elimination.  This is an exact, continuous rational
oracle -- no integer grids, no floating point and no sampling.  It only
supports the deliberately small instances used by the tests and raises
``OracleTooLarge`` instead of ever returning a partial (unsound) answer.
"""
from fractions import Fraction
from itertools import combinations, product

F = Fraction
ZERO = F(0)
ONE = F(1)


class OracleTooLarge(Exception):
    """The instance exceeds the exact-enumeration budget."""


def _gaussian(A, b):
    """Solve A x = b with exact Fractions; return None if inconsistent.

    A consistent but under-determined system (fewer pivots than variables)
    returns the particular solution whose free variables are zero.  Such a
    point is still a legitimate vertex candidate: feasibility filtering and
    de-duplication in :class:`EnergyOracle` decide whether it actually belongs
    to the polytope, so discarding these systems would miss vertices that lie
    on redundant/overlapping active constraints.
    """
    n = len(A[0])
    mat = [list(map(F, row)) + [F(b[i])] for i, row in enumerate(A)]
    rows = len(mat)
    r = 0
    pivots = []
    for col in range(n):
        pivot = next((k for k in range(r, rows) if mat[k][col] != 0), None)
        if pivot is None:
            continue
        mat[r], mat[pivot] = mat[pivot], mat[r]
        factor = mat[r][col]
        mat[r] = [v / factor for v in mat[r]]
        for k in range(rows):
            if k != r and mat[k][col] != 0:
                f = mat[k][col]
                mat[k] = [mat[k][j] - f * mat[r][j] for j in range(n + 1)]
        pivots.append(col)
        r += 1
    for k in range(r, rows):
        if mat[k][n] != 0:
            return None
    x = [F(0)] * n
    for i, col in enumerate(pivots):
        x[col] = mat[i][n]
    return x


class EnergyOracle:
    """Exact polyhedron in grid-side energy space."""

    def __init__(self, device, readings, max_combos=200_000):
        self.T = len(device["requests"])
        self.requests = device["requests"]
        self.cap = F(device["capacity"])
        self.initial = F(device["initial"])
        self.mc = F(device["max_charge"])
        self.md = F(device["max_discharge"])
        self.eta_c = F(device["eta_c"])
        self.eta_d = F(device["eta_d"])
        self.charge = [r >= 0 for r in self.requests]
        self.readings = readings
        self.max_combos = max_combos
        self._rows = self._build_rows()

    # ------------------------------------------------------------ inequality
    def _add(self, rows, coeffs, sense, rhs):
        """Normalize every inequality to coeffs . e <= rhs."""
        if sense == "<=":
            rows.append((list(coeffs), F(rhs)))
        elif sense == ">=":
            rows.append(([-a for a in coeffs], -F(rhs)))
        else:
            raise ValueError("oracle only stores inequalities")

    def _build_rows(self):
        T = self.T
        rows = []
        for t in range(T):
            vec = [F(0)] * T
            vec[t] = ONE
            if self.charge[t]:
                self._add(rows, vec, "<=",
                          min(F(self.requests[t]), self.mc))
            else:
                self._add(rows, vec, "<=",
                          min(F(-self.requests[t]), self.md))
            self._add(rows, vec, ">=", ZERO)
        # SOC boundaries: s_{t+1} = initial + sum(charge eta_c e - discharge
        # e/eta_d); require 0 <= s_{t+1} <= cap for every boundary t >= 0.
        for boundary in range(1, T + 1):
            coeffs = []
            for t in range(T):
                if t >= boundary:
                    coeffs.append(ZERO)
                elif self.charge[t]:
                    coeffs.append(self.eta_c)
                else:
                    coeffs.append(-ONE / self.eta_d)
            self._add(rows, coeffs, "<=", self.cap - self.initial)
            self._add(rows, coeffs, ">=", -self.initial)
        for reading in self.readings:
            coeffs = [F(0)] * T
            for t in range(reading["start"], reading["end"]):
                ch = self.charge[t]
                if reading["channel"] == "import":
                    if ch:
                        coeffs[t] += ONE
                elif reading["channel"] == "export":
                    if not ch:
                        coeffs[t] += ONE
                else:
                    coeffs[t] += (ONE - self.eta_c) if ch else (
                        ONE / self.eta_d - ONE)
            self._add(rows, coeffs, "<=", F(reading["upper"]))
            self._add(rows, coeffs, ">=", F(reading["lower"]))
        return rows

    # ------------------------------------------------------------- vertices
    def _satisfies(self, e, rows=None):
        if any(v < 0 for v in e):
            return False
        if rows is None:
            rows = self._rows
        for vec, rhs in rows:
            if sum(vec[j] * e[j] for j in range(self.T)) > rhs:
                return False
        return True

    def vertices(self, extra_rows=()):
        """All polytope vertices (exact), with optional extra <= rows."""
        rows = self._rows + [(list(c), F(r)) for c, r in extra_rows]
        total = 1
        from math import comb
        total = comb(len(rows), self.T)
        if total > self.max_combos:
            raise OracleTooLarge(
                "enumeration budget exceeded: %d" % total)
        found = []
        for chosen in combinations(range(len(rows)), self.T):
            A = [rows[k][0] for k in chosen]
            b = [rows[k][1] for k in chosen]
            x = _gaussian(A, b)
            if x is None or not self._satisfies(x, rows):
                continue
            if x not in found:
                found.append(x)
        return found

    def feasible(self, extra_rows=()):
        try:
            return len(self.vertices(extra_rows)) > 0
        except OracleTooLarge:
            # Fallback for feasibility only: try vertex via every active set
            # without materializing the full list; same exact enumeration.
            rows = self._rows + [(list(c), F(r)) for c, r in extra_rows]
            for chosen in combinations(range(len(rows)), self.T):
                A = [rows[k][0] for k in chosen]
                b = [rows[k][1] for k in chosen]
                x = _gaussian(A, b)
                if x is not None and self._satisfies(x, rows):
                    return True
            return False

    def linear_extremum(self, objective, maximum=False):
        """Exact min/max of a linear objective over the domain vertices."""
        verts = self.vertices()
        values = [sum(objective[j] * e[j] for j in range(self.T))
                  for e in verts]
        if not values:
            return None, None
        target = max(values) if maximum else min(values)
        return target, verts[values.index(target)]

    # -------------------------------------------------------------- invoices
    def _import_vec(self, inv):
        return [ONE if (inv["start"] <= t < inv["end"] and self.charge[t])
                else ZERO for t in range(self.T)]

    def invoice_value(self, inv, e):
        energy = sum(F(inv["prices"][t - inv["start"]]) * e[t]
                     for t in range(inv["start"], inv["end"])
                     if self.charge[t])
        w = inv["window"]
        peak = ZERO
        for k in range(inv["start"], inv["end"] - w + 1):
            avg = sum(e[t] for t in range(k, k + w)
                      if self.charge[t]) / w
            peak = max(peak, avg)
        return energy + F(inv["demand_rate"]) * peak

    def invoice_extrema(self, inv):
        """Exact min/max of the convex piecewise-linear invoice function.

        The domain is partitioned into closed regions, each fixing which
        demand window attains the peak; inside one region F is a linear
        function whose extrema sit on that region's vertices.  Both extrema
        are therefore obtained by exact vertex enumeration over every peak
        region (the zero-rate case is linear on the whole polytope).
        """
        verts = self.vertices()
        if inv["demand_rate"] == 0:
            vals = [self.invoice_value(inv, e) for e in verts]
            return min(vals), max(vals)
        f_min = None
        f_max = None
        windows = list(range(inv["start"], inv["end"] - inv["window"] + 1))
        for k in windows:
            extra = self._peak_region_rows(inv, k)
            region_verts = self.vertices(extra)
            for e in region_verts:
                value = self.invoice_value(inv, e)
                f_min = value if f_min is None else min(f_min, value)
                f_max = value if f_max is None else max(f_max, value)
        return f_min, f_max

    def _peak_region_rows(self, inv, peak_k):
        """Inequality rows pinning window *peak_k* as an attained maximum."""
        T = self.T
        w = inv["window"]
        rows = []
        pk = [F(0)] * T
        for t in range(peak_k, peak_k + w):
            if self.charge[t]:
                pk[t] = ONE / w
        for k in range(inv["start"], inv["end"] - w + 1):
            other = [F(0)] * T
            for t in range(k, k + w):
                if self.charge[t]:
                    other[t] = ONE / w
            # pk.avg >= other.avg  ->  (other - pk) . e <= 0
            rows.append(([other[j] - pk[j] for j in range(T)], ZERO))
        return rows

    def joint_feasible(self, invoices):
        """Is there a single domain curve with F_i(e) = amount_i for all?

        Zero-demand invoices contribute exact linear equality rows.  Positive
        demand invoices branch over every peak window; each complete branch is
        a plain linear system whose feasibility is enumerated exactly.
        """
        fixed_rows = []          # normalized <= rows
        branchers = []
        for inv in invoices:
            energy = [F(0)] * self.T
            for t in range(inv["start"], inv["end"]):
                if self.charge[t]:
                    energy[t] = F(inv["prices"][t - inv["start"]])
            if inv["demand_rate"] == 0:
                fixed_rows.append((list(energy), F(inv["amount"])))
                fixed_rows.append(([-a for a in energy],
                                   -F(inv["amount"])))
            else:
                branchers.append(inv)
        if not branchers:
            return self.feasible(fixed_rows)
        choices = [list(range(inv["start"],
                              inv["end"] - inv["window"] + 1))
                   for inv in branchers]
        for selection in product(*choices):
            rows = list(fixed_rows)
            for inv, k in zip(branchers, selection):
                rows += self._joint_invoice_rows(inv, k)
            if self.feasible(rows):
                return True
        return False

    def _joint_invoice_rows(self, inv, peak_k):
        """Equality rows for one invoice given its attaining peak window."""
        T = self.T
        w = inv["window"]
        rows = self._peak_region_rows(inv, peak_k)
        # F = energy + d * peak_k_avg = amount, encoded as two <= rows.
        fvec = [F(0)] * T
        for t in range(inv["start"], inv["end"]):
            if self.charge[t]:
                fvec[t] += F(inv["prices"][t - inv["start"]])
                if peak_k <= t < peak_k + w:
                    fvec[t] += F(inv["demand_rate"]) / w
        rows.append((list(fvec), F(inv["amount"])))
        rows.append(([-a for a in fvec], -F(inv["amount"])))
        return rows
