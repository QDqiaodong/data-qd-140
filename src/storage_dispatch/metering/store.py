"""SQLite persistence for metering source bytes and parsed observations.

The metering tables live alongside (but never share rows with) any reservation
book tables in the same database file.  A whole ingest is one atomic
transaction; identical raw bytes always resolve to the same SHA-256 dataset
and never overwrite another dataset.  Structurally valid but mutually
contradictory observations are stored verbatim -- feasibility diagnosis is the
job of :mod:`storage_dispatch.metering.domain`, not the ingest layer.
"""
import hashlib
import json
import sqlite3

METERING_SCHEMA = """
CREATE TABLE IF NOT EXISTS metering_dataset(
    dataset_sha TEXT PRIMARY KEY,
    raw_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metering_device(
    dataset_sha TEXT PRIMARY KEY,
    capacity INTEGER NOT NULL,
    initial_soc INTEGER NOT NULL,
    max_charge INTEGER NOT NULL,
    max_discharge INTEGER NOT NULL,
    eta_c TEXT NOT NULL,
    eta_d TEXT NOT NULL,
    slot_count INTEGER NOT NULL,
    requests_json TEXT NOT NULL,
    FOREIGN KEY(dataset_sha) REFERENCES metering_dataset(dataset_sha)
);
CREATE TABLE IF NOT EXISTS metering_slot(
    dataset_sha TEXT NOT NULL,
    slot_index INTEGER NOT NULL,
    request INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('charge','discharge')),
    PRIMARY KEY(dataset_sha, slot_index),
    FOREIGN KEY(dataset_sha) REFERENCES metering_dataset(dataset_sha)
);
CREATE TABLE IF NOT EXISTS metering_reading(
    dataset_sha TEXT NOT NULL,
    seq INTEGER NOT NULL,
    reading_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('import','export','loss')),
    start_slot INTEGER NOT NULL,
    end_slot INTEGER NOT NULL,
    lower TEXT NOT NULL,
    upper TEXT NOT NULL,
    PRIMARY KEY(dataset_sha, seq),
    UNIQUE(dataset_sha, reading_id),
    FOREIGN KEY(dataset_sha) REFERENCES metering_dataset(dataset_sha)
);
"""


class StoreError(RuntimeError):
    """A metering database operation failed."""


def _connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(conn):
    conn.executescript(METERING_SCHEMA)


def digest_of(raw):
    return hashlib.sha256(raw).hexdigest()


def ingest(db_path, raw, device, readings, slots, now_iso):
    """Atomically store one dataset; return (sha, reused_existing)."""
    sha = digest_of(raw)
    conn = _connect(db_path)
    try:
        initialize(conn)
        existing = conn.execute(
            "SELECT 1 FROM metering_dataset WHERE dataset_sha=?",
            (sha,)).fetchone()
        if existing is not None:
            conn.commit()
            return sha, True
        with conn:  # single atomic commit; rollback on any error
            conn.execute(
                "INSERT INTO metering_dataset(dataset_sha, raw_bytes, "
                "created_at) VALUES(?,?,?)", (sha, raw, now_iso))
            conn.execute(
                "INSERT INTO metering_device(dataset_sha, capacity, "
                "initial_soc, max_charge, max_discharge, eta_c, eta_d, "
                "slot_count, requests_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (sha, device["capacity"], device["initial"],
                 device["max_charge"], device["max_discharge"],
                 str(device["eta_c"]), str(device["eta_d"]), slots,
                 json.dumps(device["requests"])))
            for t, request in enumerate(device["requests"]):
                direction = "charge" if request >= 0 else "discharge"
                conn.execute(
                    "INSERT INTO metering_slot(dataset_sha, slot_index, "
                    "request, direction) VALUES(?,?,?,?)",
                    (sha, t, request, direction))
            for seq, reading in enumerate(readings):
                conn.execute(
                    "INSERT INTO metering_reading(dataset_sha, seq, "
                    "reading_id, channel, start_slot, end_slot, lower, "
                    "upper) VALUES(?,?,?,?,?,?,?,?)",
                    (sha, seq, reading["reading_id"], reading["channel"],
                     reading["start"], reading["end"],
                     str(reading["lower"]), str(reading["upper"])))
        return sha, False
    finally:
        conn.close()


def open_snapshot(db_path, dataset_sha):
    """Open a read-only, consistent snapshot of one dataset.

    The database is opened through a ``mode=ro`` URI so no source data can be
    modified.  A missing dataset or malformed content raises :class:`StoreError`.
    """
    uri = "file:%s?mode=ro" % str(db_path).replace("?", "%3f")
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        raise StoreError("cannot open read-only database: " + str(exc))
    conn.execute("PRAGMA query_only=ON")
    row = conn.execute(
        "SELECT raw_bytes FROM metering_dataset WHERE dataset_sha=?",
        (dataset_sha,)).fetchone()
    if row is None:
        conn.close()
        raise StoreError("unknown dataset: " + dataset_sha)
    return conn, bytes(row[0])


def load_dataset(conn, dataset_sha):
    """Rebuild the validated device/readings structures from a snapshot."""
    from fractions import Fraction
    from ..physics import is_charge

    drow = conn.execute(
        "SELECT capacity, initial_soc, max_charge, max_discharge, eta_c, "
        "eta_d, slot_count, requests_json FROM metering_device "
        "WHERE dataset_sha=?", (dataset_sha,)).fetchone()
    if drow is None:
        raise StoreError("dataset has no device record: " + dataset_sha)
    (capacity, initial, max_charge, max_discharge, eta_c_s, eta_d_s,
     slots, requests_json) = drow
    device = {
        "capacity": capacity,
        "initial": initial,
        "max_charge": max_charge,
        "max_discharge": max_discharge,
        "eta_c": Fraction(eta_c_s),
        "eta_d": Fraction(eta_d_s),
        "requests": json.loads(requests_json),
    }
    slot_rows = conn.execute(
        "SELECT slot_index, request, direction FROM metering_slot "
        "WHERE dataset_sha=? ORDER BY slot_index", (dataset_sha,)).fetchall()
    if [r[0] for r in slot_rows] != list(range(slots)):
        raise StoreError("slot rows are not contiguous for " + dataset_sha)
    for t, request, direction in slot_rows:
        expected = "charge" if is_charge(request) else "discharge"
        if direction != expected:
            raise StoreError("stored direction disagrees with request")
    readings = []
    for r in conn.execute(
            "SELECT reading_id, channel, start_slot, end_slot, lower, upper "
            "FROM metering_reading WHERE dataset_sha=? ORDER BY seq",
            (dataset_sha,)):
        readings.append({
            "reading_id": r[0],
            "channel": r[1],
            "start": r[2],
            "end": r[3],
            "lower": Fraction(r[4]),
            "upper": Fraction(r[5]),
        })
    return device, readings, slots
