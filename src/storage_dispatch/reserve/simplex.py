"""Exact two-phase simplex with Fraction arithmetic and lexicographic objectives.

The engine is deliberately dependency free.  Objective coefficients are sparse
vectors keyed by *level*: level 0 holds the monetary cost and levels 1..K hold
the canonical tie-break variables in their prescribed lexicographic order.
Bland's anti-cycling rule (smallest entering column, smallest leaving variable
on ratio ties) is used throughout, so every solve terminates and returns the
lexicographically smallest optimum.
"""
from fractions import Fraction


class Infeasible(Exception):
    """The linear program admits no feasible point."""


class Unbounded(Exception):
    """The linear program is unbounded (programming error for this domain)."""


def _vec_scale(vec, a):
    if a == 0 or not vec:
        return {}
    return {k: v * a for k, v in vec.items()}


def _vec_add(a, b, sign=1):
    out = dict(a)
    for k, v in b.items():
        nv = out.get(k, 0) + sign * v
        if nv == 0:
            out.pop(k, None)
        else:
            out[k] = nv
    return out


def _vec_cmp(vec):
    """Return sign of the lexicographic value of *vec* against zero."""
    if not vec:
        return 0
    k = min(vec)
    v = vec[k]
    return -1 if v < 0 else 1


def _accum(mapping, key, delta):
    """Add level-vector *delta* into ``mapping[key]`` (both are level maps)."""
    if not delta:
        return
    cur = mapping.get(key)
    if not cur:
        mapping[key] = dict(delta)
        return
    for k, v in delta.items():
        nv = cur.get(k, 0) + v
        if nv == 0:
            cur.pop(k, None)
        else:
            cur[k] = nv
    if not cur:
        mapping.pop(key, None)


class Simplex:
    """Minimize a lexicographic objective subject to linear constraints.

    Variables are numbered 0 .. n-1.  Constraints are triples
    ``(coeffs, sense, rhs)`` with sense in ``{'=', '<=', '>='}`` and
    ``rhs >= 0``.  ``costs`` maps variable -> Fraction for level 0 and
    ``levels`` maps variable -> 1-based lexicographic level.  Variables absent
    from ``levels`` never take part in tie-breaking (slacks / artificials).
    """

    def __init__(self, n, constraints, costs, levels=None, pivot_limit=1_000_000):
        self.n = n
        self.levels = dict(levels or {})
        self.costs = {j: Fraction(costs.get(j, 0)) for j in range(n)}
        # rows: list of dicts var/column -> Fraction; column -1 is RHS
        self.rows = []
        self.basis = []          # basic variable per row
        self.dead = set()        # columns that may never enter again
        self.fixed = set()       # fixed structural/slack columns
        self._next_col = n
        self.pivots = 0
        self.pivot_limit = pivot_limit
        # Permanent objective level-vector per column for the active phase.
        # Phase 1 overwrites this with sum-of-artificial coefficients.
        self._obj = {}
        self._build(constraints)

    # ------------------------------------------------------------------ build
    def _new_col(self):
        c = self._next_col
        self._next_col += 1
        return c

    def _build(self, constraints):
        art_rows = []
        flip = {'<=': '>=', '>=': '<=', '=': '='}
        for coeffs, sense, rhs in constraints:
            rhs = Fraction(rhs)
            row = {j: Fraction(a) for j, a in coeffs.items() if a != 0}
            # Normalize to a non-negative RHS first.
            if rhs < 0:
                rhs = -rhs
                row = {j: -a for j, a in row.items()}
                sense = flip[sense]
            if sense == '<=':
                # a x + slack = b; the slack is the initial basic variable.
                slack = self._new_col()
                row[slack] = Fraction(1)
                basic = slack
            elif sense == '>=':
                # a x - surplus + artificial = b; the artificial is basic.
                surplus = self._new_col()
                row[surplus] = Fraction(-1)
                art = self._new_col()
                row[art] = Fraction(1)
                basic = art
                art_rows.append(len(self.rows))
            elif sense == '=':
                # a x + artificial = b.
                art = self._new_col()
                row[art] = Fraction(1)
                basic = art
                art_rows.append(len(self.rows))
            else:
                raise ValueError('bad constraint sense: ' + repr(sense))
            row[-1] = rhs
            self.rows.append(row)
            self.basis.append(basic)
        # Phase-1 objective: minimize the sum of every artificial variable.
        # The permanent objective gives each artificial a unit level-0
        # coefficient; reduced costs are then rebuilt from the basis.
        self.artificial = {self.basis[r] for r in art_rows}
        self._obj = {art: {0: Fraction(1)} for art in self.artificial}
        self._canonical_objective()

    def _obj_vec(self, col):
        """Permanent level-vector objective coefficient of *col* this phase."""
        return self._obj.get(col, {})

    def _recompute_objective(self):
        """Rebuild the reduced-cost row directly from the current basis.

        This is immune to the drift that incremental pivot updates suffer
        when basic variables carry empty reduced-cost slots.  It uses only
        the permanent per-column objective vectors (``self._obj``) and the
        current constraint rows.
        """
        self.orow = {}
        self.orhs = {}
        for r, b in enumerate(self.basis):
            if b in self.dead:
                continue
            comp = dict(self._obj_vec(b))
            row = self.rows[r]
            for c, a in row.items():
                if c == -1:
                    # x_b = rhs - sum(a x_j): the constant term contributes
                    # comp*rhs to the objective value z.
                    self.orhs = _vec_add(self.orhs, _vec_scale(comp, a))
                elif c != b:
                    # Substituting x_b adds comp * (-a) to column c.
                    _accum(self.orow, c, _vec_scale(comp, -a))
            self.orow[b] = {}
        # Add the explicit objective coefficient of every non-basic column.
        basic = set(self.basis)
        for col, vec in self._obj.items():
            if col not in basic and col not in self.dead:
                _accum(self.orow, col, dict(vec))
        # Keep explicit empty slots for tracked basic columns.
        for b in self.basis:
            self.orow.setdefault(b, {})

    def _canonical_objective(self):
        """Seed the permanent objective then compute the reduced-cost row."""
        self._recompute_objective()


    def _entering(self):
        # Standard reduced-cost tableau (c_B B^-1 A - c): enter a column whose
        # reduced-cost vector is lexicographically negative; orhs equals z.
        # Bland's rule picks the smallest such column, which both prevents
        # cycling and makes the lexicographic tie-break canonical.  Artificial
        # variables are phase-1 bookkeeping and may never re-enter once they
        # leave the basis, so they are excluded here throughout.
        basic = set(self.basis)
        best = None
        for c in sorted(self.orow):
            if c < 0 or c in self.dead or c in self.fixed:
                continue
            if c in self.artificial and c not in basic:
                continue
            if _vec_cmp(self.orow[c]) < 0 and (best is None or c < best):
                best = c
        return best

    def _leaving(self, q):
        best_row = None
        best_basic = None
        best_ratio = None
        for r, row in enumerate(self.rows):
            aq = row.get(q)
            if aq is None or aq <= 0:
                continue
            ratio = row[-1] / aq
            if best_row is None or ratio < best_ratio or \
                    (ratio == best_ratio and self.basis[r] < best_basic):
                best_row, best_basic, best_ratio = r, self.basis[r], ratio
        return best_row

    def _pivot(self, q, p):
        rows = self.rows
        pivot = rows[p][q]
        prow = rows[p]
        new = {c: a / pivot for c, a in prow.items()}
        rows[p] = new
        # Incremental objective-row update: z' = z - z_q * (new pivot row).
        oq = self.orow.get(q)
        if oq:
            oq = dict(oq)
            for c, a in new.items():
                if c == -1:
                    self.orhs = _vec_add(self.orhs, _vec_scale(oq, a))
                elif c != q:
                    _accum(self.orow, c, _vec_scale(oq, -a))
        for r, row in enumerate(rows):
            if r == p:
                continue
            aq = row.get(q)
            if aq is None:
                continue
            # row <- row - aq * new, including the RHS and every column.
            row[-1] -= aq * new[-1]
            for c, a in new.items():
                if c == -1:
                    continue
                nv = row.get(c, 0) - aq * a
                if nv == 0:
                    row.pop(c, None)
                else:
                    row[c] = nv
            row.pop(q, None)
        self.basis[p] = q
        # The entering column is now basic; give it an empty reduced-cost
        # slot (the incremental update above already removed its old entry).
        self.orow[q] = {}
        self.pivots += 1
        if self.pivots > self.pivot_limit:
            raise RuntimeError('simplex pivot limit exceeded')

    def _drive_to_optimum(self):
        while True:
            q = self._entering()
            if q is None:
                return
            p = self._leaving(q)
            if p is None:
                raise Unbounded('column %d unbounded' % q)
            self._pivot(q, p)

    # ------------------------------------------------------------------ phases
    def _artificial_sum(self):
        total = Fraction(0)
        for r, b in enumerate(self.basis):
            if b in self.artificial:
                total += self.rows[r][-1]
        return total

    def _drop_artificials(self):
        # Phase-1 feasibility is read directly from the basic artificial
        # values (the objective row can drop artificial components during
        # pivots, so orhs alone is not authoritative here).
        residual = self._artificial_sum()
        if residual != 0:
            raise Infeasible('phase 1 residual %s' % residual)
        # Drive any zero-valued basic artificial out through a zero exchange.
        for r in range(len(self.rows)):
            b = self.basis[r]
            if b not in self.artificial:
                continue
            row = self.rows[r]
            cand = None
            for c, a in row.items():
                if c != -1 and c not in self.artificial and c not in self.dead \
                        and a != 0 and (cand is None or c < cand):
                    cand = c
            if cand is None:
                continue       # redundant row; leave zero row in place
            self._pivot(cand, r)
        self.dead |= self.artificial
        for row in self.rows:
            for a in self.artificial:
                row.pop(a, None)
        self.orow = {c: v for c, v in self.orow.items() if c not in self.dead}

    def _install_phase_two(self):
        # Build the permanent objective vector for every live column: level 0
        # carries the real cost, later levels encode the canonical tie-break.
        # Auxiliary (slack) columns receive levels past every structural
        # level so they lose all tie-breaks against plan variables.
        max_lvl = max(self.levels.values(), default=0)
        self._obj = {}
        for j in range(self._next_col):
            if j in self.dead:
                continue
            vec = {0: self.costs.get(j, 0)}
            lvl = self.levels.get(j)
            if lvl is None and j >= self.n:
                lvl = max_lvl + 1 + (j - self.n)
            if lvl is not None:
                vec[lvl] = Fraction(1)
            self._obj[j] = vec
        self._recompute_objective()

    def solve(self):
        # Phase 1: feasibility is the sum of any remaining basic artificials.
        self._drive_to_optimum()
        if self._artificial_sum() != 0:
            raise Infeasible('phase 1 residual %s' % self._artificial_sum())
        self._drop_artificials()
        # Phase 2
        self._install_phase_two()
        self._drive_to_optimum()
        values = [Fraction(0)] * self._next_col
        for r, b in enumerate(self.basis):
            if b < len(values):
                values[b] = self.rows[r][-1]
        return values
