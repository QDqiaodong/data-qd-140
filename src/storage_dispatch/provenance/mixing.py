"""Exact same-slot bus mixing with arbitrary directed cycles.

Every bus is a single homogeneous mixing vessel with no inventory between
slots.  For bus *b* let ``Q_b`` be its total positive withdrawal (outgoing
link ``sent`` plus device charge accepted plus external sink energy).  Each
source *s* then obeys, simultaneously across every bus,

    Q_b * p[b][s] = known[b][s]
                  + sum over links a -> b of sent * efficiency * p[a][s]

so cyclic bus graphs are solved as one coupled linear system in exact
:class:`Fraction` arithmetic.  There is no topological ordering (cycles would
drop edges), no iteration and no floating-point tolerance anywhere.  A
singular system means a positive-flow closed component whose composition no
external or battery source can reach -- an :class:`AmbiguousOrigin` -- and an
energy mismatch is an :class:`Unbalanced`; imbalance is always reported first.
"""
from fractions import Fraction

from .schema import AmbiguousOrigin, Unbalanced


def _gauss_jordan(matrix, rhs_columns):
    """Exact Gauss-Jordan over Fractions with batched RHS columns.

    All RHS columns ride along in the same elimination passes, so an
    n-bus / m-source slot performs only n row normalizations instead of
    n*m independent solves.  Singularity (a zero pivot with no usable
    exchange row) returns ``None``.
    """
    n = len(matrix)
    m = len(rhs_columns)
    a = [list(matrix[i]) + [rhs_columns[k][i] for k in range(m)]
         for i in range(n)]
    rank = 0
    for col in range(n):
        pivot = rank
        while pivot < n and a[pivot][col] == 0:
            pivot += 1
        if pivot == n:
            return None
        if pivot != rank:
            a[rank], a[pivot] = a[pivot], a[rank]
        pivot_value = a[rank][col]
        pivot_row = [value / pivot_value for value in a[rank]]
        a[rank] = pivot_row
        for row in range(n):
            if row == rank:
                continue
            factor = a[row][col]
            if factor == 0:
                continue
            target = a[row]
            a[row] = [target[j] - factor * pivot_row[j]
                      for j in range(n + m)]
        rank += 1
    return [[a[i][n + k] for i in range(n)] for k in range(m)]


def solve_slot(buses, injections, discharge_by_bus, charge_by_bus,
               sinks, links):
    """Solve one slot's mixing problem with exact source composition.

    Parameters
    ----------
    buses:
        Ordered list of every bus id in the slot.
    injections:
        Normalized external injection records ``{source_id, bus, energy}``.
    discharge_by_bus:
        Mapping bus -> mapping source_id -> grid-side battery discharge.
    charge_by_bus:
        Mapping bus -> total grid-side charge energy actually accepted.
    sinks:
        Normalized sink records ``{sink_id, bus, energy}``.
    links:
        Normalized link records with exact ``sent``/``efficiency`` Fractions.

    Returns the bus share table, per-link arrivals/losses, per-sink source
    amounts and per-bus charge source amounts.  Raises :class:`Unbalanced`
    before any ambiguity check and :class:`AmbiguousOrigin` for an
    undetermined positive-flow closed component.
    """
    # Bus-level known injections and total withdrawals, plus the source totals.
    known = {bus: {} for bus in buses}
    sources = set()
    for record in injections:
        if record["energy"] == 0:
            continue
        bucket = known[record["bus"]]
        bucket[record["source_id"]] = (
            bucket.get(record["source_id"], Fraction(0)) + record["energy"])
        sources.add(record["source_id"])
    for bus, origins in discharge_by_bus.items():
        bucket = known[bus]
        for source_id, amount in origins.items():
            if amount == 0:
                continue
            bucket[source_id] = bucket.get(source_id, Fraction(0)) + amount
            sources.add(source_id)

    withdrawals = {bus: Fraction(0) for bus in buses}
    arrivals = {bus: Fraction(0) for bus in buses}
    for record in links:
        if record["sent"] == 0:
            continue
        withdrawals[record["from_bus"]] += record["sent"]
        arrivals[record["to_bus"]] += (
            record["sent"] * record["efficiency"])
    for bus, amount in charge_by_bus.items():
        withdrawals[bus] += amount
    for record in sinks:
        if record["energy"] == 0:
            continue
        withdrawals[record["bus"]] += record["energy"]

    known_total = {bus: sum(bucket.values(), Fraction(0))
                   for bus, bucket in known.items()}
    active = [bus for bus in buses if withdrawals[bus] > 0]
    for bus in buses:
        total_in = known_total[bus] + arrivals[bus]
        if total_in != withdrawals[bus]:
            raise Unbalanced(
                "bus %s is unbalanced: arrivals %s != withdrawals %s"
                % (bus, total_in, withdrawals[bus]))

    active_index = {bus: i for i, bus in enumerate(active)}

    # The mixing equations only couple buses joined by positive-flow links,
    # so decompose into weakly connected components and solve each one
    # independently -- a 32-bus slot typically yields much smaller matrices.
    neighbors = {bus: set() for bus in active}
    for record in links:
        if record["sent"] == 0:
            continue
        if record["from_bus"] in active_index and \
                record["to_bus"] in active_index:
            neighbors[record["from_bus"]].add(record["to_bus"])
            neighbors[record["to_bus"]].add(record["from_bus"])
    components = []
    seen = set()
    for bus in active:
        if bus in seen:
            continue
        stack = [bus]
        component = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            component.append(current)
            stack.extend(neighbors[current] - seen)
        components.append(sorted(component))

    shares = {bus: {} for bus in active}
    for component in components:
        size = len(component)
        matrix = [[Fraction(0) for _ in range(size)] for _ in range(size)]
        local_index = {bus: i for i, bus in enumerate(component)}
        for i, bus in enumerate(component):
            matrix[i][i] = withdrawals[bus]
        for record in links:
            if record["sent"] == 0:
                continue
            if record["from_bus"] not in local_index or \
                    record["to_bus"] not in local_index:
                continue
            i = local_index[record["to_bus"]]
            j = local_index[record["from_bus"]]
            matrix[i][j] -= record["sent"] * record["efficiency"]

        # Rank probe: a singular component matrix is a positive-flow closed
        # component no external or battery source can reach.  The probe is
        # independent of the known-source list, so a sourced network next to
        # a sourceless pure cycle cannot hide the ambiguity.
        if _gauss_jordan(matrix, [[Fraction(0) for _ in component]]) is None:
            raise AmbiguousOrigin(
                "positive-flow closed loop has no reachable external or "
                "battery source; its origin composition is not uniquely "
                "determined")
        # Only sources with a known injection into this component can have
        # non-zero shares inside it, so build local RHS columns rather than
        # carrying all 64 global sources through every component solve.
        local_sources = sorted({
            source_id for bus in component
            for source_id in known[bus] if known[bus][source_id] != 0})
        rhs_columns = [
            [known[bus].get(source_id, Fraction(0)) for bus in component]
            for source_id in local_sources]
        columns = _gauss_jordan(matrix, rhs_columns)
        if columns is None:  # pragma: no cover - ruled out by the probe
            raise AmbiguousOrigin(
                "positive-flow closed loop has no reachable external or "
                "battery source; its origin composition is not uniquely "
                "determined")
        for source_id, solution in zip(local_sources, columns):
            for bus, value in zip(component, solution):
                if value < 0:
                    raise Unbalanced(
                        "bus %s yields a negative source share for %s"
                        % (bus, source_id))
                if value != 0:
                    shares[bus][source_id] = value

    for bus in active:
        share_sum = sum(shares[bus].values(), Fraction(0))
        if share_sum != 1:
            raise Unbalanced(
                "bus %s source shares sum to %s, expected 1"
                % (bus, share_sum))

    # Allocate the withdrawals with the common per-bus source composition.
    link_results = []
    for record in links:
        if record["sent"] == 0:
            link_results.append({"link_id": record["link_id"],
                                 "from_bus": record["from_bus"],
                                 "to_bus": record["to_bus"],
                                 "sent": Fraction(0),
                                 "arrived": Fraction(0),
                                 "loss": Fraction(0),
                                 "arrival_origins": {},
                                 "loss_origins": {}})
            continue
        upstream = shares[record["from_bus"]]
        arrived = record["sent"] * record["efficiency"]
        loss = record["sent"] - arrived
        arrival_origins = {s: arrived * p for s, p in upstream.items()}
        loss_origins = {s: loss * p for s, p in upstream.items()}
        link_results.append({
            "link_id": record["link_id"], "from_bus": record["from_bus"],
            "to_bus": record["to_bus"], "sent": record["sent"],
            "arrived": arrived, "loss": loss,
            "arrival_origins": {s: v for s, v in arrival_origins.items()
                                if v != 0},
            "loss_origins": {s: v for s, v in loss_origins.items()
                             if v != 0}})

    sink_results = []
    for record in sinks:
        if record["energy"] == 0:
            sink_results.append({"sink_id": record["sink_id"],
                                 "bus": record["bus"],
                                 "energy": Fraction(0), "origins": {}})
            continue
        composition = shares[record["bus"]]
        sink_results.append({
            "sink_id": record["sink_id"], "bus": record["bus"],
            "energy": record["energy"],
            "origins": {s: record["energy"] * p
                        for s, p in composition.items()
                        if record["energy"] * p != 0}})

    charge_results = {}
    for bus, amount in charge_by_bus.items():
        if amount == 0:
            charge_results[bus] = {}
            continue
        composition = shares[bus]
        charge_results[bus] = {s: amount * p for s, p in composition.items()
                               if amount * p != 0}

    return {"shares": shares, "links": link_results,
            "sinks": sink_results, "charge_origins": charge_results}
