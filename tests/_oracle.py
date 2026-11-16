"""Independent small-scale exhaustive oracle for the planner.

For tiny instances the full LP is small enough to enumerate basic solutions by
choosing, for every constraint, which variable is basic.  This oracle is
deliberately written independently of the production simplex and planner (no
shared optimization code) so it can act as a cross-check of both feasibility
and the cost optimum.  It only handles the bounded dimensions used below.
"""
from fractions import Fraction
from itertools import combinations
import math

from storage_dispatch.reserve.planner import plan

F = Fraction


def _solve_linear_system(A, b):
    """Gaussian elimination with Fractions. A is list of rows (dicts->lists)."""
    n = len(A[0])
    mat = [list(map(Fraction, row)) + [Fraction(b[i])]
           for i, row in enumerate(A)]
    row_count = len(mat)
    r = 0
    pivots = []
    for col in range(n):
        pivot = next((k for k in range(r, row_count) if mat[k][col] != 0),
                     None)
        if pivot is None:
            continue
        mat[r], mat[pivot] = mat[pivot], mat[r]
        factor = mat[r][col]
        mat[r] = [v / factor for v in mat[r]]
        for k in range(row_count):
            if k != r and mat[k][col] != 0:
                f = mat[k][col]
                mat[k] = [mat[k][j] - f * mat[r][j] for j in range(n + 1)]
        pivots.append(col)
        r += 1
        if r == row_count:
            break
    for k in range(r, row_count):
        if mat[k][n] != 0:
            return None  # inconsistent
    x = [F(0)] * n
    for i, col in enumerate(pivots):
        x[col] = mat[i][n]
    return x


def brute_force_optimum(n, constraints, costs, max_combos=5_000):
    """Minimize c.x subject to constraints, x >= 0. Enumerates vertices.

    Constraints are (coeffs dict, sense, rhs). Returns (min_cost, x) or None
    when infeasible.  Raises ``ValueError`` if the (exponential) enumeration
    would exceed ``max_combos``; callers skip such larger instances rather than
    accepting a partial (and therefore unsound) enumeration.
    """
    rows = []
    for coeffs, sense, rhs in constraints:
        vec = [F(0)] * n
        for j, a in coeffs.items():
            vec[j] = F(a)
        rows.append((vec, sense, F(rhs)))
    # Non-negativity bounds x_j >= 0 act as candidate active constraints.
    candidates = rows + [([F(-1) if j == k else 0 for j in range(n)], "<=",
                         F(0)) for k in range(n)]
    total = math.comb(len(candidates), n)
    if total > max_combos:
        raise ValueError("enumeration too large: %d candidates" % total)
    best = None
    # A vertex is determined by n active, independent constraints.
    for chosen in combinations(range(len(candidates)), n):
        A = [candidates[k][0] for k in chosen]
        b = [candidates[k][2] for k in chosen]
        x = _solve_linear_system(A, b)
        if x is None:
            continue
        if any(v < 0 for v in x):
            continue
        feasible = True
        for vec, sense, rhs in rows:
            lhs = sum(vec[j] * x[j] for j in range(n))
            if sense == "<=" and lhs > rhs:
                feasible = False
            elif sense == ">=" and lhs < rhs:
                feasible = False
            elif sense == "=" and lhs != rhs:
                feasible = False
            if not feasible:
                break
        if not feasible:
            continue
        cost = sum(F(costs.get(j, 0)) * x[j] for j in range(n))
        if best is None or cost < best[0]:
            best = (cost, x)
    return best
