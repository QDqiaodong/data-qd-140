"""SQLite-backed executable reservation book.

The book persists the immutable device parameters, the original contracts,
their lifecycle state, the execution cursor and prefix, the canonical plan and
every successful request receipt.  All mutating operations are serialized so
that the result is equivalent to some serial commit order, and each successful
add / cancel / advance bumps ``revision`` exactly once.

Two locking strategies are provided for concurrent connections:

* ``"external"`` -- solve outside the write transaction; before committing,
  the state is re-read under a lock and the problem is re-solved whenever
  another connection changed the reservation set, cursor or obligations.
* ``"transaction"`` -- hold the write lock for the whole solve; competing
  writers block on ``SQLITE_BUSY`` and can safely retry.
"""
import hashlib
import json
import sqlite3
from fractions import Fraction

from .planner import plan, InfeasiblePlan
from .validate import (validate_init, validate_add, validate_cancel,
                       validate_advance, parse_json, RequestError, Conflict)

SCHEMA_VERSION = 1


class BookError(RequestError):
    """A book-level operation could not be performed."""


SCHEMA = """
CREATE TABLE meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE slot(
    slot_index INTEGER PRIMARY KEY,
    charge_limit INTEGER NOT NULL,
    discharge_limit INTEGER NOT NULL,
    price INTEGER NOT NULL
);
CREATE TABLE contract(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id TEXT NOT NULL UNIQUE,
    start_slot INTEGER NOT NULL,
    end_slot INTEGER NOT NULL,
    energy INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','cancelled','completed')),
    delivered TEXT NOT NULL DEFAULT '0'
);
CREATE TABLE execution(
    slot_index INTEGER PRIMARY KEY,
    charge TEXT NOT NULL,
    discharge_total TEXT NOT NULL,
    soc_before TEXT NOT NULL,
    soc_after TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE plan_slot(
    slot_index INTEGER PRIMARY KEY,
    charge TEXT NOT NULL,
    discharge_total TEXT NOT NULL,
    soc_before TEXT NOT NULL,
    soc_after TEXT NOT NULL,
    deliveries TEXT NOT NULL,
    price INTEGER NOT NULL
);
CREATE TABLE plan_contract(
    contract_id TEXT PRIMARY KEY,
    planned TEXT NOT NULL,
    remaining TEXT NOT NULL
);
CREATE TABLE receipt(
    request_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    operation TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""


def _connect(db_path, timeout):
    conn = sqlite3.connect(db_path, timeout=timeout, isolation_level=None)
    # WAL lets readers and a single writer proceed concurrently, which the
    # external-locking strategy relies on during a solve.  Best-effort: if
    # another connection currently holds a lock, fall back to the default
    # rollback journal rather than failing the whole operation.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _rollback(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


class ReservationBook:
    """A reservation book bound to a SQLite database file."""

    def __init__(self, db_path, strategy="external", timeout=30.0,
                 solve_hook=None):
        if strategy not in ("external", "transaction"):
            raise ValueError("strategy must be 'external' or 'transaction'")
        self.db_path = db_path
        self.strategy = strategy
        self.timeout = timeout
        # Deterministic interception point for the concurrency acceptance
        # tests: called as solve_hook(event, book, state[, result]).
        self.solve_hook = solve_hook

    # ------------------------------------------------------------- lifecycle
    def initialize(self, raw):
        data = parse_json(raw)
        spec = validate_init(data)
        conn = _connect(self.db_path, self.timeout)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self._tables_exist(conn):
                raise BookError("database already initialized")
            self._create(conn, spec)
            state = self._snapshot_state(conn)
            result = self._solve(state)
            self._store_plan(conn, result)
            # init does not increment revision; the first add/cancel/advance
            # bumps it from 0 to 1.
            revision = 0
            receipt = self._receipt("init", parsed_request_id=None,
                                    digest=hashlib.sha256(bytes(raw)).hexdigest(),
                                    revision=revision, result=result)
            # init has no request_id, so it is not stored in the receipt table.
            conn.execute("COMMIT")
            return revision, receipt
        except InfeasiblePlan as exc:
            _rollback(conn)
            raise BookError("init has no feasible plan: " + str(exc))
        except Exception:
            _rollback(conn)
            raise
        finally:
            conn.close()

    @staticmethod
    def _tables_exist(conn):
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone() is not None

    def _create(self, conn, spec):
        # Execute DDL one statement at a time: executescript() would force a
        # commit and break the surrounding explicit transaction.
        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            conn.execute(stmt)
        meta = {
            "schema_version": str(SCHEMA_VERSION),
            "capacity": str(spec["capacity"]),
            "initial": str(spec["initial"]),
            "eta_c": str(spec["eta_c"]),
            "eta_d": str(spec["eta_d"]),
            "terminal_min": str(spec["terminal_min"]),
            "slot_count": str(len(spec["slots"])),
            "cursor": "0",
            "revision": "0",
        }
        conn.executemany("INSERT INTO meta(key,value) VALUES(?,?)",
                         list(meta.items()))
        conn.executemany(
            "INSERT INTO slot(slot_index,charge_limit,discharge_limit,price)"
            " VALUES(?,?,?,?)",
            [(i, s["charge_limit"], s["discharge_limit"], s["price"])
             for i, s in enumerate(spec["slots"])])

    # ----------------------------------------------------------------- state
    def _snapshot_state(self, conn):
        meta = dict(conn.execute("SELECT key,value FROM meta").fetchall())
        slots = [dict(charge_limit=r[0], discharge_limit=r[1], price=r[2])
                 for r in conn.execute(
                     "SELECT charge_limit,discharge_limit,price FROM slot"
                     " ORDER BY slot_index")]
        contracts = []
        for cid, start, end, energy, state, delivered in conn.execute(
                "SELECT contract_id,start_slot,end_slot,energy,state,"
                "delivered FROM contract ORDER BY seq"):
            done = Fraction(delivered)
            remaining = Fraction(0) if state == "cancelled" \
                else Fraction(energy) - done
            contracts.append({
                "contract_id": cid, "start": start, "end": end,
                "energy": energy, "state": state, "delivered": done,
                "remaining": remaining,
            })
        cursor = int(meta["cursor"])
        return {
            "meta": meta,
            "capacity": int(meta["capacity"]),
            "eta_c": Fraction(meta["eta_c"]),
            "eta_d": Fraction(meta["eta_d"]),
            "terminal_min": int(meta["terminal_min"]),
            "slots": slots,
            "cursor": cursor,
            "prefix_end_soc": self._prefix_end_soc(conn, meta, cursor),
            "contracts": contracts,
        }

    def _prefix_end_soc(self, conn, meta, cursor):
        if cursor == 0:
            return Fraction(meta["initial"])
        row = conn.execute(
            "SELECT soc_after FROM execution WHERE slot_index=?",
            (cursor - 1,)).fetchone()
        if row is None:
            raise BookError("execution prefix is missing slot %d" % (cursor - 1))
        return Fraction(row[0])

    # ----------------------------------------------------------------- solve
    def _solve(self, state):
        if self.solve_hook is not None:
            self.solve_hook("before_solve", self, state)
        result = plan(
            state["capacity"], state["eta_c"], state["eta_d"],
            state["terminal_min"], state["slots"], state["cursor"],
            state["prefix_end_soc"],
            [c for c in state["contracts"] if c["state"] == "active"])
        if self.solve_hook is not None:
            self.solve_hook("after_solve", self, state, result)
        return result

    @staticmethod
    def _fingerprint(state):
        parts = [str(state["cursor"]), str(state["prefix_end_soc"])]
        for c in state["contracts"]:
            parts.append("%s|%s|%s" % (c["contract_id"], c["state"],
                                       str(c["remaining"])))
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    # ------------------------------------------------------------ mutations
    def add_contract(self, raw):
        return self._mutate(raw, "add")

    def cancel_contract(self, raw):
        return self._mutate(raw, "cancel")

    def advance(self, raw):
        return self._mutate(raw, "advance")

    def _mutate(self, raw, operation):
        parsed = parse_json(raw)
        if not isinstance(parsed, dict) or "request_id" not in parsed:
            raise RequestError(operation + " requires request_id")
        request_id = parsed["request_id"]
        digest = hashlib.sha256(bytes(raw)).hexdigest()
        conn = _connect(self.db_path, self.timeout)
        try:
            prior = conn.execute(
                "SELECT digest,payload FROM receipt WHERE request_id=?",
                (request_id,)).fetchone()
        finally:
            conn.close()
        if prior is not None:
            if prior[0] != digest:
                raise Conflict("request_id reused with different bytes")
            stored = json.loads(prior[1])
            return stored["revision"], stored
        try:
            if self.strategy == "transaction":
                conn = _connect(self.db_path, self.timeout)
                try:
                    return self._run_transaction(conn, raw, parsed, digest,
                                                 operation)
                finally:
                    conn.close()
            return self._run_external(raw, parsed, digest, operation)
        except InfeasiblePlan as exc:
            # Validation passed but the request has no feasible plan.  Nothing
            # has been written, so the request id is not consumed and can be
            # retried with a different, feasible body.
            raise BookError("no feasible plan: " + str(exc))

    def _read_snapshot(self):
        conn = _connect(self.db_path, self.timeout)
        try:
            conn.execute("BEGIN")
            state = self._snapshot_state(conn)
            conn.execute("COMMIT")
            return state
        except Exception:
            _rollback(conn)
            raise
        finally:
            conn.close()

    def _run_external(self, raw, parsed, digest, operation):
        # Solve OUTSIDE any database transaction so other connections can
        # commit concurrently (the solve hook deliberately exercises this).
        state = self._read_snapshot()
        plan_result, change = self._apply(state, parsed, operation)
        fingerprint = self._fingerprint(state)

        for _ in range(1024):
            conn = _connect(self.db_path, self.timeout)
            try:
                conn.execute("BEGIN IMMEDIATE")
                fresh = self._snapshot_state(conn)
                if self._fingerprint(fresh) == fingerprint:
                    revision, receipt = self._commit(
                        conn, operation, parsed, digest, fresh, plan_result,
                        change, raw)
                    conn.execute("COMMIT")
                    return revision, receipt
                # The world changed underneath us; the old solution, even if it
                # still happens to be feasible, must not masquerade as the
                # optimum of the new state.
                _rollback(conn)
            finally:
                conn.close()
            # Re-solve from a fresh snapshot with no lock held.
            state = self._read_snapshot()
            plan_result, change = self._apply(state, parsed, operation)
            fingerprint = self._fingerprint(state)
        raise BookError("exhausted retries under concurrent contention")

    def _run_transaction(self, conn, raw, parsed, digest, operation):
        conn.execute("BEGIN IMMEDIATE")
        state = self._snapshot_state(conn)
        plan_result, change = self._apply(state, parsed, operation)
        revision, receipt = self._commit(
            conn, operation, parsed, digest, state, plan_result, change, raw)
        conn.execute("COMMIT")
        return revision, receipt

    def _apply(self, state, parsed, operation):
        """Validate and apply the request to an in-memory state, then solve.

        Returns ``(plan_result, change)`` where ``change`` describes what must
        be persisted.  Nothing is written here, so an infeasible add leaves
        the book untouched.
        """
        cursor = state["cursor"]
        slot_count = len(state["slots"])
        if operation == "add":
            known = {c["contract_id"] for c in state["contracts"]}
            req = validate_add(parsed, cursor, slot_count, known)
            trial = [dict(c) for c in state["contracts"]]
            trial.append({
                "contract_id": req["contract_id"], "start": req["start"],
                "end": req["end"], "energy": req["energy"],
                "state": "active", "delivered": Fraction(0),
                "remaining": Fraction(req["energy"]),
            })
            result = self._plan_for(state, trial)
            return result, ("add", req)

        if operation == "cancel":
            req = validate_cancel(parsed)
            target = next((c for c in state["contracts"]
                           if c["contract_id"] == req["contract_id"]), None)
            if target is None or target["state"] != "active":
                raise BookError("only an active contract can be cancelled: "
                                + req["contract_id"])
            trial = [dict(c) for c in state["contracts"]]
            for c in trial:
                if c["contract_id"] == req["contract_id"]:
                    c["state"] = "cancelled"
                    c["remaining"] = Fraction(0)
            result = self._plan_for(state, trial)
            return result, ("cancel", req)

        req = validate_advance(parsed, cursor, slot_count)
        # Advance feasibility is decided by freezing the existing canonical
        # plan prefix; the post-advance re-plan happens against real rows in
        # _commit, so here we only validate and describe the change.
        return None, ("advance", req)

    def _plan_for(self, state, trial_contracts):
        # Route through _solve so deterministic solve hooks fire for every
        # strategy (the concurrency acceptance tests rely on this).
        view = dict(state)
        view["contracts"] = trial_contracts
        return self._solve(view)

    # ------------------------------------------------------------- commit
    def _commit(self, conn, operation, parsed, digest, state, plan_result,
                change, raw):
        revision = int(state["meta"]["revision"]) + 1
        if operation == "add":
            _, req = change
            conn.execute(
                "INSERT INTO contract(contract_id,start_slot,end_slot,energy,"
                "state,delivered) VALUES(?,?,?,?,'active','0')",
                (req["contract_id"], req["start"], req["end"], req["energy"]))
            self._store_plan(conn, plan_result)
        elif operation == "cancel":
            _, req = change
            conn.execute(
                "UPDATE contract SET state='cancelled' WHERE contract_id=?",
                (req["contract_id"],))
            self._store_plan(conn, plan_result)
        else:
            _, req = change
            self._freeze_advance(conn, state, req["to_slot"])
            advanced = self._snapshot_state(conn)
            plan_result = plan(
                advanced["capacity"], advanced["eta_c"], advanced["eta_d"],
                advanced["terminal_min"], advanced["slots"],
                advanced["cursor"], advanced["prefix_end_soc"],
                [c for c in advanced["contracts"]
                 if c["state"] == "active"])
            self._store_plan(conn, plan_result)

        conn.execute("UPDATE meta SET value=? WHERE key='revision'",
                     (str(revision),))
        receipt = self._receipt(operation, parsed["request_id"], digest,
                                revision, plan_result)
        conn.execute(
            "INSERT INTO receipt(request_id,digest,operation,revision,payload)"
            " VALUES(?,?,?,?,?)",
            (parsed["request_id"], digest, operation, revision,
             json.dumps(receipt, sort_keys=True)))
        return revision, receipt

    def _freeze_advance(self, conn, state, to_slot):
        cursor = state["cursor"]
        rows = conn.execute(
            "SELECT slot_index,charge,discharge_total,soc_before,soc_after,"
            "deliveries FROM plan_slot WHERE slot_index>=? AND slot_index<?"
            " ORDER BY slot_index", (cursor, to_slot)).fetchall()
        if len(rows) != to_slot - cursor:
            raise BookError("plan does not cover the advance range")
        delta = {}
        for slot, charge, discharge, before, after, deliveries in rows:
            conn.execute(
                "INSERT INTO execution(slot_index,charge,discharge_total,"
                "soc_before,soc_after,detail) VALUES(?,?,?,?,?,?)",
                (slot, charge, discharge, before, after, deliveries))
            for cid, amount in json.loads(deliveries).items():
                delta[cid] = delta.get(cid, Fraction(0)) + Fraction(amount)
        for cid, amount in delta.items():
            energy, delivered = conn.execute(
                "SELECT energy,delivered FROM contract WHERE contract_id=?",
                (cid,)).fetchone()
            new_delivered = Fraction(delivered) + amount
            conn.execute(
                "UPDATE contract SET delivered=? WHERE contract_id=?",
                (str(new_delivered), cid))
            # A contract whose full grid-side energy has now been executed is
            # complete; it can never be cancelled afterwards.
            if new_delivered >= Fraction(energy):
                conn.execute(
                    "UPDATE contract SET state='completed' "
                    "WHERE contract_id=? AND state='active'", (cid,))
        conn.execute("UPDATE meta SET value=? WHERE key='cursor'",
                     (str(to_slot),))

    # ------------------------------------------------------------- plan I/O
    def _store_plan(self, conn, result):
        conn.execute("DELETE FROM plan_slot")
        conn.execute("DELETE FROM plan_contract")
        conn.executemany(
            "INSERT INTO plan_slot(slot_index,charge,discharge_total,"
            "soc_before,soc_after,deliveries,price) VALUES(?,?,?,?,?,?,?)",
            [(s["slot"], s["charge"], s["discharge_total"], s["soc_before"],
              s["soc_after"], json.dumps(s["deliveries"], sort_keys=True),
              s["price"]) for s in result["slots"]])
        conn.executemany(
            "INSERT INTO plan_contract(contract_id,planned,remaining) "
            "VALUES(?,?,?)",
            [(cid, v["planned"], v["remaining"])
             for cid, v in sorted(result["contracts"].items())])

    @staticmethod
    def _receipt(operation, parsed_request_id, digest, revision, result):
        return {
            "operation": operation,
            "revision": revision,
            "request_id": parsed_request_id,
            "request_sha256": digest,
            "plan_cost": None if result is None else result["cost"],
            "terminal_soc": None if result is None else result["terminal_soc"],
        }

    # --------------------------------------------------------------- status
    def status(self):
        conn = _connect(self.db_path, self.timeout)
        try:
            conn.execute("BEGIN")
            state = self._snapshot_state(conn)
            meta = state["meta"]
            plan_slots = [dict(slot=r[0], charge=r[1], discharge_total=r[2],
                               soc_before=r[3], soc_after=r[4],
                               deliveries=json.loads(r[5]), price=r[6])
                          for r in conn.execute(
                              "SELECT slot_index,charge,discharge_total,"
                              "soc_before,soc_after,deliveries,price FROM "
                              "plan_slot ORDER BY slot_index")]
            plan_contracts = {r[0]: {"planned": r[1], "remaining": r[2]}
                              for r in conn.execute(
                                  "SELECT contract_id,planned,remaining FROM "
                                  "plan_contract")}
            execution = [dict(slot=r[0], charge=r[1], discharge_total=r[2],
                              soc_before=r[3], soc_after=r[4],
                              deliveries=json.loads(r[5]))
                         for r in conn.execute(
                             "SELECT slot_index,charge,discharge_total,"
                             "soc_before,soc_after,detail FROM execution "
                             "ORDER BY slot_index")]
            receipts = [dict(request_id=r[0], digest=r[1], operation=r[2],
                             revision=r[3])
                        for r in conn.execute(
                            "SELECT request_id,digest,operation,revision FROM "
                            "receipt ORDER BY rowid")]
            contracts = [{
                "contract_id": c["contract_id"], "start": c["start"],
                "end": c["end"], "energy": c["energy"], "state": c["state"],
                "delivered": str(c["delivered"]),
                "remaining": str(c["remaining"]),
            } for c in state["contracts"]]
            conn.execute("COMMIT")
            return {
                "revision": int(meta["revision"]),
                "cursor": state["cursor"],
                "capacity": state["capacity"],
                "terminal_min": state["terminal_min"],
                "eta_c": meta["eta_c"],
                "eta_d": meta["eta_d"],
                "prefix_end_soc": str(state["prefix_end_soc"]),
                "contracts": contracts,
                "execution": execution,
                "plan": {"slots": plan_slots, "contracts": plan_contracts},
                "receipts": receipts,
            }
        except Exception:
            _rollback(conn)
            raise
        finally:
            conn.close()
