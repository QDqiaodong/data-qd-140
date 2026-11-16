"""Canonical exact planner for the executable reservation book.

Given the immutable device parameters, the executed prefix (cursor, prefix-end
SOC and per-contract already-delivered energy) and the set of active contracts
with their remaining obligations, :func:`plan` solves one exact linear program
with :mod:`fractions.Fraction` arithmetic:

* first minimize total grid charging cost ``sum_t price_t * c_t``;
* among all cost-optimal solutions take the lexicographically smallest vector
  ordered by slot ascending and, within a slot, ``c_t`` first then the
  per-contract deliveries ``x_{i,t}`` sorted by contract id (ASCII order).

The LP uses explicit SOC state variables with sparse per-slot transition rows
instead of dense cumulative constraints, and delivery variables are only
created inside each contract's window.  No energy is discretized or rounded;
infeasibility is reported, never guessed.
"""
from fractions import Fraction

from .simplex import Simplex, Infeasible

ZERO = Fraction(0)


class InfeasiblePlan(Exception):
    """No dispatch can satisfy the remaining obligations."""


def plan(capacity, eta_c, eta_d, terminal_min, slots,
         cursor, prefix_end_soc, contracts):
    """Return the canonical plan for the not-yet-executed horizon.

    Parameters
    ----------
    capacity, terminal_min:
        Device bounds in energy units.
    eta_c, eta_d:
        Charge/discharge efficiencies as exact Fractions (0 < eta <= 1).
    slots:
        List of ``{charge_limit, discharge_limit, price}`` per slot.
    cursor:
        First slot still open to planning; slots ``[0, cursor)`` are frozen.
    prefix_end_soc:
        Exact SOC at slot *cursor* produced by the executed prefix.
    contracts:
        Active contracts as dicts ``{contract_id, start, end, remaining}``
        where ``remaining`` is the grid-side energy still to deliver.
    """
    slot_count = len(slots)
    horizon = list(range(cursor, slot_count))
    h = len(horizon)
    # Contracts with positive remaining obligation, sorted by contract id in
    # ASCII order (Python str ordering equals ASCII for the allowed id set).
    active = sorted((c for c in contracts if c["remaining"] > 0),
                    key=lambda c: c["contract_id"])
    m = len(active)
    prefix_end_soc = Fraction(prefix_end_soc)

    # Delivery variables exist only inside each contract's open window.
    windows = []          # list of (lo, hi) open slot ranges per contract
    x_index = {}          # (contract ordinal, slot) -> variable index
    n = 0
    c_of = {}
    for t in horizon:
        c_of[t] = n
        n += 1
    s_of = {}
    for t in range(cursor, slot_count + 1):
        s_of[t] = n
        n += 1
    for i, c in enumerate(active):
        lo = max(c["start"], cursor)
        hi = c["end"]
        if lo >= hi:
            raise InfeasiblePlan("contract %s has no open delivery slot"
                                 % c["contract_id"])
        windows.append((lo, hi))
        for t in range(lo, hi):
            x_index[(i, t)] = n
            n += 1

    constraints = []
    costs = {}
    levels = {}
    next_level = 1

    # Canonical lex levels follow the required vector order exactly: slot
    # ascending, c_t first, then x_{i,t} by ASCII contract id.  SOC state
    # variables are determined by c and x, so they take levels past every
    # plan variable and never disturb the canonical tie-break.
    for t in horizon:
        levels[c_of[t]] = next_level
        next_level += 1
        costs[c_of[t]] = Fraction(slots[t]["price"])
        for i, c in enumerate(active):
            if (i, t) in x_index:
                levels[x_index[(i, t)]] = next_level
                next_level += 1
    for t in range(cursor, slot_count + 1):
        levels[s_of[t]] = next_level
        next_level += 1

    inv_eta_d = Fraction(1) / eta_d

    for t in horizon:
        # Slot charge limit.
        constraints.append(({c_of[t]: Fraction(1)}, "<=",
                            Fraction(slots[t]["charge_limit"])))
        # Shared discharge limit over every contract active in this slot.
        drow = {x_index[(i, t)]: Fraction(1) for i in range(m)
                if (i, t) in x_index}
        constraints.append((drow, "<=",
                            Fraction(slots[t]["discharge_limit"])))
        # SOC transition: s_{t+1} = s_t + eta_c c_t - sum_i x_it / eta_d,
        # written as s_{t+1} - s_t - eta_c c_t + sum_i x_it / eta_d = 0.
        trow = {s_of[t + 1]: Fraction(1), s_of[t]: Fraction(-1),
                c_of[t]: -eta_c}
        for i in range(m):
            if (i, t) in x_index:
                trow[x_index[(i, t)]] = inv_eta_d
        constraints.append((trow, "=", ZERO))

    # The prefix fixes the SOC at the cursor; every later state has both an
    # upper capacity bound and an explicit non-negativity bound.  Structural
    # variables are not implicitly non-negative in the LP, so s_t >= 0 must
    # be stated as a real constraint (written -s_t <= 0).
    constraints.append(({s_of[cursor]: Fraction(1)}, "=", prefix_end_soc))
    for t in range(cursor + 1, slot_count + 1):
        constraints.append(({s_of[t]: Fraction(1)}, "<=",
                            Fraction(capacity)))
        constraints.append(({s_of[t]: Fraction(-1)}, "<=", ZERO))
    # Terminal condition.
    constraints.append(({s_of[slot_count]: Fraction(1)}, ">=",
                        Fraction(terminal_min)))

    # Exact remaining delivery per active contract.
    for i, c in enumerate(active):
        lo, hi = windows[i]
        row = {x_index[(i, t)]: Fraction(1) for t in range(lo, hi)}
        constraints.append((row, "=", Fraction(c["remaining"])))

    lp = Simplex(n, constraints, costs, levels)
    try:
        values = lp.solve()
    except Infeasible as exc:
        raise InfeasiblePlan(str(exc))

    return _materialize(values, eta_c, eta_d, slots, cursor, active,
                        contracts, c_of, s_of, x_index, lp)


def _materialize(values, eta_c, eta_d, slots, cursor, active,
                 all_contracts, c_of, s_of, x_index, lp):
    slot_count = len(slots)
    deliveries = {c["contract_id"]: {} for c in all_contracts}
    plan_slots = []
    total_cost = ZERO
    for t in range(cursor, slot_count):
        before = values[s_of[t]]
        c_t = values[c_of[t]]
        delivered = {}
        discharge_total = ZERO
        for i, c in enumerate(active):
            var = x_index.get((i, t))
            if var is None:
                continue
            val = values[var]
            if val != 0:
                delivered[c["contract_id"]] = val
                deliveries[c["contract_id"]][t] = val
                discharge_total += val
        after = values[s_of[t + 1]]
        total_cost += Fraction(slots[t]["price"]) * c_t
        plan_slots.append({
            "slot": t,
            "soc_before": str(before),
            "charge": str(c_t),
            "deliveries": {cid: str(v) for cid, v in sorted(delivered.items())},
            "discharge_total": str(discharge_total),
            "soc_after": str(after),
            "price": slots[t]["price"],
        })

    contract_view = {}
    for c in all_contracts:
        done = sum(deliveries[c["contract_id"]].values(), ZERO)
        contract_view[c["contract_id"]] = {
            "planned": str(done),
            "remaining": str(c["remaining"] - done),
        }
    return {
        "cursor": cursor,
        "slots": plan_slots,
        "cost": str(total_cost),
        "terminal_soc": str(values[s_of[slot_count]]),
        "contracts": contract_view,
        "pivots": lp.pivots,
        "_values": values,
        "_active": [c["contract_id"] for c in active],
    }
