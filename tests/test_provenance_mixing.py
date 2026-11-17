"""Exact bus-mixing tests with hand calculations and an independent oracle.

Expected values for the small networks are derived by hand below.  A second,
independently written linear-equation oracle (its own variable ordering, its
own Gaussian elimination over Fractions) checks the larger cyclic networks.
The engine under test never generates an expectation.
"""
import copy
import unittest
from fractions import Fraction

from storage_dispatch.provenance import (solve_slot, AmbiguousOrigin,
                                         Unbalanced)


def fr(x):
    return Fraction(x)


# --------------------------------------------------------------------- oracle
def oracle_solve(buses, injections, discharge_by_bus, charge_by_bus, sinks,
                 links):
    """Reference solver built directly from the mixing balance equations."""
    sources = sorted({i["source_id"] for i in injections
                      if i["energy"] != 0}
                     | {s for m in discharge_by_bus.values()
                        for s in m if m[s] != 0})
    withdrawal = {b: Fraction(0) for b in buses}
    arrivals = {b: Fraction(0) for b in buses}
    known = {b: {} for b in buses}
    for inj in injections:
        if inj["energy"] == 0:
            continue
        known[inj["bus"]][inj["source_id"]] = \
            known[inj["bus"]].get(inj["source_id"], Fraction(0)) + \
            inj["energy"]
    for bus, origins in discharge_by_bus.items():
        for s, v in origins.items():
            if v:
                known[bus][s] = known[bus].get(s, Fraction(0)) + v
    for link in links:
        if link["sent"] == 0:
            continue
        withdrawal[link["from_bus"]] += link["sent"]
        arrivals[link["to_bus"]] += link["sent"] * link["efficiency"]
    for bus, v in charge_by_bus.items():
        withdrawal[bus] += v
    for sink in sinks:
        if sink["energy"] != 0:
            withdrawal[sink["bus"]] += sink["energy"]
    for bus in buses:
        balance = sum(known[bus].values(), Fraction(0)) + arrivals[bus]
        if balance != withdrawal[bus]:
            raise Unbalanced("oracle: unbalanced bus " + bus)
    active = [b for b in buses if withdrawal[b] > 0]

    # Independent elimination on variables p_{b,s}; solve each source alone.
    def eliminate(matrix, rhs):
        n = len(rhs)
        a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
        row = 0
        for col in range(n):
            pivot = next((r for r in range(row, n) if a[r][col] != 0), None)
            if pivot is None:
                return None
            a[row], a[pivot] = a[pivot], a[row]
            pv = a[row][col]
            a[row] = [v / pv for v in a[row]]
            for r in range(n):
                if r != row and a[r][col] != 0:
                    f = a[r][col]
                    a[r] = [x - f * y for x, y in zip(a[r], a[row])]
            row += 1
        return [a[i][n] for i in range(n)]

    # Matrix rows correspond to buses; Q_b p_b - sum arrivals p_a = known_b.
    shares = {b: {} for b in active}
    for source in sources:
        matrix = [[Fraction(0) for _ in active] for _ in active]
        rhs = []
        for i, bus in enumerate(active):
            matrix[i][i] = withdrawal[bus]
            for link in links:
                if link["sent"] == 0:
                    continue
                if link["to_bus"] == bus:
                    j = active.index(link["from_bus"])
                    matrix[i][j] -= link["sent"] * link["efficiency"]
            rhs.append(known[bus].get(source, Fraction(0)))
        solution = eliminate(matrix, rhs)
        if solution is None:
            raise AmbiguousOrigin("oracle: singular")
        for bus, value in zip(active, solution):
            if value != 0:
                shares[bus][source] = value
    return shares


class HandCalculatedMixingTests(unittest.TestCase):
    def test_split_and_remerge_two_sources(self):
        # b0: R=6, G=2 -> links to b1 (3) and b2 (5); b1,b2 each sink all.
        result = solve_slot(
            ["b0", "b1", "b2"],
            [{"source_id": "R", "bus": "b0", "energy": Fraction(6)},
             {"source_id": "G", "bus": "b0", "energy": Fraction(2)}],
            {}, {"b0": Fraction(0)},
            [{"sink_id": "s1", "bus": "b1", "energy": Fraction(3)},
             {"sink_id": "s2", "bus": "b2", "energy": Fraction(5)}],
            [{"link_id": "l1", "from_bus": "b0", "to_bus": "b1",
              "sent": Fraction(3), "efficiency": Fraction(1)},
             {"link_id": "l2", "from_bus": "b0", "to_bus": "b2",
              "sent": Fraction(5), "efficiency": Fraction(1)}])
        expected_share = {"R": Fraction(3, 4), "G": Fraction(1, 4)}
        for bus in ("b0", "b1", "b2"):
            self.assertEqual(result["shares"][bus], expected_share)
        sinks = {s["sink_id"]: s["origins"] for s in result["sinks"]}
        self.assertEqual(sinks["s1"], {"R": Fraction(9, 4),
                                       "G": Fraction(3, 4)})
        self.assertEqual(sinks["s2"], {"R": Fraction(15, 4),
                                       "G": Fraction(5, 4)})

    def test_parallel_lossy_edges(self):
        # Two parallel links b0->b1, eff 1 and 1/2; b0 holds R=4, G=4.
        result = solve_slot(
            ["b0", "b1"],
            [{"source_id": "R", "bus": "b0", "energy": Fraction(4)},
             {"source_id": "G", "bus": "b0", "energy": Fraction(4)}],
            {}, {},
            [{"sink_id": "s", "bus": "b1", "energy": Fraction(6)}],
            [{"link_id": "p1", "from_bus": "b0", "to_bus": "b1",
              "sent": Fraction(4), "efficiency": Fraction(1)},
             {"link_id": "p2", "from_bus": "b0", "to_bus": "b1",
              "sent": Fraction(4), "efficiency": Fraction(1, 2)}])
        link = {l["link_id"]: l for l in result["links"]}
        self.assertEqual(link["p1"]["arrived"], 4)
        self.assertEqual(link["p1"]["loss"], 0)
        self.assertEqual(link["p2"]["arrived"], 2)
        self.assertEqual(link["p2"]["loss"], 2)
        # Half share on every flow; loss inherits the upstream composition.
        self.assertEqual(link["p2"]["loss_origins"],
                         {"R": Fraction(1), "G": Fraction(1)})
        self.assertEqual(result["shares"]["b1"],
                         {"R": Fraction(1, 2), "G": Fraction(1, 2)})
        sink = result["sinks"][0]["origins"]
        self.assertEqual(sink, {"R": Fraction(3), "G": Fraction(3)})

    def test_fraction_driven_large_cycle_exact_shares(self):
        # 4-bus ring where the circulating throughput (100) dwarfs the two
        # fresh half-unit injections.  Balanced design: forward links 100,
        # return link 199/2, b3 sink 1 (b0 balance: 1/2 + 199/2 = 100;
        # b3 balance: 100 + 1/2 = 199/2 + 1).  Hand solution of
        #   100 p0 = 1/2 + (199/2) p3, p1 = p2 = p0, (201/2) p3 = 100 p0
        # gives p_R,0 = p_R,1 = p_R,2 = 201/400 and p_R,3 = 1/2.
        links = [
            {"link_id": "l01", "from_bus": "b0", "to_bus": "b1",
             "sent": Fraction(100), "efficiency": Fraction(1)},
            {"link_id": "l12", "from_bus": "b1", "to_bus": "b2",
             "sent": Fraction(100), "efficiency": Fraction(1)},
            {"link_id": "l23", "from_bus": "b2", "to_bus": "b3",
             "sent": Fraction(100), "efficiency": Fraction(1)},
            {"link_id": "l30", "from_bus": "b3", "to_bus": "b0",
             "sent": Fraction(199, 2), "efficiency": Fraction(1)},
        ]
        sinks = [{"sink_id": "s3", "bus": "b3", "energy": Fraction(1)}]
        injections = [
            {"source_id": "R", "bus": "b0", "energy": Fraction(1, 2)},
            {"source_id": "G", "bus": "b3", "energy": Fraction(1, 2)}]
        result = solve_slot(["b0", "b1", "b2", "b3"], injections, {}, {},
                            sinks, links)
        p0 = Fraction(201, 400)
        p3 = Fraction(1, 2)
        shares = result["shares"]
        self.assertEqual(shares["b0"]["R"], p0)
        self.assertEqual(shares["b1"]["R"], p0)
        self.assertEqual(shares["b2"]["R"], p0)
        self.assertEqual(shares["b3"]["R"], p3)
        self.assertEqual(shares["b0"]["G"], Fraction(1) - p0)
        self.assertEqual(shares["b3"]["G"], Fraction(1) - p3)
        # Every bus composition stays a partition of unity.
        for bus in ("b0", "b1", "b2", "b3"):
            self.assertEqual(sum(shares[bus].values(), Fraction(0)), 1)
        sink = result["sinks"][0]["origins"]
        self.assertEqual(sink["R"], Fraction(1, 2))
        self.assertEqual(sink["G"], Fraction(1, 2))
        # Unit efficiencies mean no link loss: both fresh halves reach the sink.
        self.assertEqual(sum(sink.values(), Fraction(0)), 1)


class OracleAndReorderingTests(unittest.TestCase):
    def _cyclic_network(self, seed):
        rng = __import__("random").Random(seed)
        buses = ["b%d" % i for i in range(6)]
        links = []
        # A ring plus chords and a reverse edge, all lossy.
        ring = [(i, (i + 1) % 6) for i in range(6)]
        chords = [(0, 3), (2, 5), (4, 1), (3, 0)]
        for k, (a, b) in enumerate(ring + chords):
            sent = Fraction(rng.randrange(1, 40))
            eta = Fraction(rng.randrange(2, 9), rng.randrange(9, 20))
            links.append({"link_id": "lk%d" % k, "from_bus": buses[a],
                          "to_bus": buses[b], "sent": sent,
                          "efficiency": eta})
        withdrawal = {b: Fraction(0) for b in buses}
        arrivals = {b: Fraction(0) for b in buses}
        for link in links:
            withdrawal[link["from_bus"]] += link["sent"]
            arrivals[link["to_bus"]] += link["sent"] * link["efficiency"]
        # Guarantee balance with non-negative external flows: deficit buses
        # receive an external injection, surplus buses host an extra sink.
        sinks, injections = [], []
        sink_seq = inject_seq = 0
        for i, bus in enumerate(buses):
            gap = withdrawal[bus] - arrivals[bus]
            if gap > 0:
                injections.append({"source_id": "R" if i % 2 else "G",
                                   "bus": bus, "energy": gap})
                inject_seq += 1
            elif gap < 0:
                sinks.append({"sink_id": "bal%d" % sink_seq, "bus": bus,
                              "energy": -gap})
                sink_seq += 1
            if rng.randrange(2):
                extra = Fraction(rng.randrange(1, 15))
                sinks.append({"sink_id": "sk%d" % i, "bus": bus,
                              "energy": extra})
                injections.append({"source_id": "G" if i % 2 else "R",
                                   "bus": bus, "energy": extra})
        self.assertTrue(injections)
        return buses, injections, sinks, links

    def test_matches_independent_oracle(self):
        for seed in range(25):
            buses, injections, sinks, links = self._cyclic_network(seed)
            result = solve_slot(buses, injections, {}, {}, sinks, links)
            expected = oracle_solve(buses, injections, {}, {}, sinks, links)
            for bus in buses:
                self.assertEqual(result["shares"][bus], expected[bus])
            # Per-source total sink delivery equals the fresh injected energy
            # (link losses only reduce circulating energy, all sinks balance).
            delivered = {}
            for sink in result["sinks"]:
                for s, v in sink["origins"].items():
                    delivered[s] = delivered.get(s, Fraction(0)) + v
            injected = {}
            for inj in injections:
                injected[inj["source_id"]] = injected.get(
                    inj["source_id"], Fraction(0)) + inj["energy"]
            lost = {}
            for link in result["links"]:
                for s, v in link["loss_origins"].items():
                    lost[s] = lost.get(s, Fraction(0)) + v
            for s in injected:
                self.assertEqual(
                    delivered.get(s, Fraction(0)) + lost.get(s, Fraction(0)),
                    injected[s])

    def test_array_reorderings_are_equivalent(self):
        buses, injections, sinks, links = self._cyclic_network(7)
        base = solve_slot(buses, injections, {}, {}, sinks, links)
        # A fixed spread of permutations (full factorial would be enormous);
        # reorder inputs, links and sinks independently each time.
        rng = __import__("random").Random(11)
        for _ in range(12):
            perm_inj = injections[:]
            perm_links = links[:]
            perm_sinks = sinks[:]
            rng.shuffle(perm_inj)
            rng.shuffle(perm_links)
            rng.shuffle(perm_sinks)
            reordered = solve_slot(buses, perm_inj, {}, {}, perm_sinks,
                                   perm_links)
            for bus in buses:
                self.assertEqual(reordered["shares"][bus],
                                 base["shares"][bus])
            self.assertEqual(
                {l["link_id"]: l["arrival_origins"]
                 for l in reordered["links"]},
                {l["link_id"]: l["arrival_origins"]
                 for l in base["links"]})
            self.assertEqual(
                {s["sink_id"]: s["origins"] for s in reordered["sinks"]},
                {s["sink_id"]: s["origins"] for s in base["sinks"]})

    def test_zero_flow_reverse_edge_creates_no_ambiguity(self):
        links = [
            {"link_id": "fwd", "from_bus": "b0", "to_bus": "b1",
             "sent": Fraction(5), "efficiency": Fraction(1)},
            {"link_id": "rev", "from_bus": "b1", "to_bus": "b0",
             "sent": Fraction(0), "efficiency": Fraction(1)},
        ]
        result = solve_slot(
            ["b0", "b1"],
            [{"source_id": "R", "bus": "b0", "energy": Fraction(5)}],
            {}, {},
            [{"sink_id": "s", "bus": "b1", "energy": Fraction(5)}], links)
        self.assertEqual(result["shares"]["b0"], {"R": Fraction(1)})
        self.assertEqual(result["shares"]["b1"], {"R": Fraction(1)})
        zero_link = next(l for l in result["links"] if l["link_id"] == "rev")
        self.assertEqual(zero_link["arrival_origins"], {})
        self.assertEqual(zero_link["loss_origins"], {})

    def test_ambiguous_then_unbalanced_priority(self):
        links = [
            {"link_id": "a", "from_bus": "b0", "to_bus": "b1",
             "sent": Fraction(5), "efficiency": Fraction(1)},
            {"link_id": "b", "from_bus": "b1", "to_bus": "b0",
             "sent": Fraction(5), "efficiency": Fraction(1)}]
        with self.assertRaises(AmbiguousOrigin):
            solve_slot(["b0", "b1"], [], {}, {}, [], links)
        broken = copy.deepcopy(links)
        broken[1]["sent"] = Fraction(4)
        with self.assertRaises(Unbalanced):
            solve_slot(["b0", "b1"], [], {}, {}, [], broken)


if __name__ == "__main__":
    unittest.main()
