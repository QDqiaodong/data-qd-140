"""SQLite ingest persistence, read-only snapshots and CLI boundary tests."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from storage_dispatch.metering.schema import parse_strict_json, validate_ingest
from storage_dispatch.metering.store import (ingest, open_snapshot,
                                             load_dataset, digest_of,
                                             StoreError)
from storage_dispatch.metering.audit import run_ingest, run_audit

ROOT = Path(__file__).resolve().parents[1]

DEVICE = {
    "capacity": 10, "initial": 0, "max_charge": 10, "max_discharge": 10,
    "charge_efficiency": {"numerator": 1, "denominator": 1},
    "discharge_efficiency": {"numerator": 1, "denominator": 1},
    "requests": [10, 10],
}


def source_bytes(readings):
    return json.dumps({"device": DEVICE, "readings": readings}).encode()


class StoreTests(unittest.TestCase):
    def test_reopen_round_trip_and_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "m.db"
            raw = source_bytes([
                {"reading_id": "a", "channel": "import", "start": 0,
                 "end": 2, "lower": "5", "upper": "5"}])
            data = parse_strict_json(raw)
            device, readings, slots = validate_ingest(data)
            sha, reused = ingest(db, raw, device, readings, slots,
                                 "2026-01-01T00:00:00Z")
            self.assertFalse(reused)
            sha2, reused2 = ingest(db, raw, device, readings, slots,
                                   "2026-01-01T00:00:00Z")
            self.assertTrue(reused2)
            self.assertEqual(sha, sha2)
            self.assertEqual(sha, digest_of(raw))

            conn, stored_raw = open_snapshot(db, sha)
            try:
                self.assertEqual(stored_raw, raw)
                dev2, rds2, slots2 = load_dataset(conn, sha)
            finally:
                conn.close()
            self.assertEqual(slots2, 2)
            self.assertEqual(dev2["requests"], [10, 10])
            self.assertEqual(rds2[0]["lower"], Fraction(5))
            # Read-only connection refuses writes.
            uri = f"file:{db}?mode=ro"
            ro = sqlite3.connect(uri, uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("DELETE FROM metering_reading")
            ro.close()

    def test_contradictory_observations_are_stored(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "m.db"
            raw = source_bytes([
                {"reading_id": "a", "channel": "import", "start": 0,
                 "end": 2, "lower": "10", "upper": "10"},
                {"reading_id": "b", "channel": "import", "start": 0,
                 "end": 2, "lower": "20", "upper": "20"}])
            device, readings, slots = validate_ingest(parse_strict_json(raw))
            sha, _ = ingest(db, raw, device, readings, slots,
                            "2026-01-01T00:00:00Z")
            conn, _ = open_snapshot(db, sha)
            try:
                _, stored, _ = load_dataset(conn, sha)
            finally:
                conn.close()
            self.assertEqual([r["reading_id"] for r in stored], ["a", "b"])

    def test_distinct_bytes_keep_distinct_datasets(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "m.db"
            r1 = source_bytes([])
            r2 = source_bytes([
                {"reading_id": "a", "channel": "loss", "start": 0,
                 "end": 1, "lower": "0", "upper": "1"}])
            shas = set()
            for raw in (r1, r2, r1):
                device, readings, slots = validate_ingest(
                    parse_strict_json(raw))
                sha, _ = ingest(db, raw, device, readings, slots,
                                "2026-01-01T00:00:00Z")
                shas.add(sha)
            self.assertEqual(len(shas), 2)

    def test_unknown_dataset(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "m.db"
            raw = source_bytes([])
            device, readings, slots = validate_ingest(parse_strict_json(raw))
            ingest(db, raw, device, readings, slots, "2026-01-01T00:00:00Z")
            with self.assertRaises(StoreError):
                open_snapshot(db, "deadbeef")


class CliTests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "storage_dispatch.cli", *args],
            capture_output=True, text=True, cwd=ROOT)

    def test_ingest_audit_flow_and_statuses(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / "m.db"
            src = td / "source.json"
            src.write_bytes(source_bytes([
                {"reading_id": "t", "channel": "import", "start": 0,
                 "end": 2, "lower": "10", "upper": "10"}]))
            invoices = td / "invoices.json"
            invoices.write_text(json.dumps([
                {"invoice_id": "I0", "start": 0, "end": 1, "prices": [1],
                 "window": 1, "demand_rate": 0, "amount": "10"},
                {"invoice_id": "I1", "start": 1, "end": 2, "prices": [1],
                 "window": 1, "demand_rate": 0, "amount": "10"}]))
            r = self.cli("metering", "ingest", "--db", str(db),
                         "--source", str(src))
            self.assertEqual(r.returncode, 0, r.stderr)
            sha = json.loads(r.stdout)["dataset_sha256"]
            out = td / "out.json"
            r = self.cli("metering", "audit", "--db", str(db),
                         "--dataset", sha, "--invoices", str(invoices),
                         "--output", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report["status"], "impossible")
            self.assertEqual(report["source_sha256"], sha)
            self.assertEqual(report["invoice_source_sha256"],
                             hashlib.sha256(invoices.read_bytes()).hexdigest())
            # Empty invoice array on a feasible domain is possible.
            invoices.write_text("[]")
            r = self.cli("metering", "audit", "--db", str(db),
                         "--dataset", sha, "--invoices", str(invoices),
                         "--output", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_text())["status"],
                             "possible")

    def test_inconsistent_audit_has_no_invoice_section(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / "m.db"
            src = td / "source.json"
            src.write_bytes(source_bytes([
                {"reading_id": "a", "channel": "import", "start": 0,
                 "end": 2, "lower": "10", "upper": "10"},
                {"reading_id": "b", "channel": "import", "start": 0,
                 "end": 2, "lower": "20", "upper": "20"}]))
            r = self.cli("metering", "ingest", "--db", str(db),
                         "--source", str(src))
            sha = json.loads(r.stdout)["dataset_sha256"]
            invoices = td / "invoices.json"
            invoices.write_text(json.dumps([
                {"invoice_id": "I0", "start": 0, "end": 1, "prices": [1],
                 "window": 1, "demand_rate": 0, "amount": "1"}]))
            out = td / "out.json"
            r = self.cli("metering", "audit", "--db", str(db),
                         "--dataset", sha, "--invoices", str(invoices),
                         "--output", str(out))
            self.assertEqual(r.returncode, 0)
            report = json.loads(out.read_text())
            self.assertEqual(report["status"], "inconsistent")
            self.assertNotIn("invoices", report)
            self.assertNotIn("invoice_conflict", report)
            self.assertIn("reading_conflict", report)

    def test_bad_input_exits_2_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / "m.db"
            src = td / "bad.json"
            src.write_text('{"device":{},"readings":[]}')
            r = self.cli("metering", "ingest", "--db", str(db),
                         "--source", str(src))
            self.assertEqual(r.returncode, 2)
            self.assertTrue(r.stderr.strip())

            good = td / "good.json"
            good.write_bytes(source_bytes([]))
            r = self.cli("metering", "ingest", "--db", str(db),
                         "--source", str(good))
            sha = json.loads(r.stdout)["dataset_sha256"]
            invoices = td / "invoices.json"
            invoices.write_text("[not-json")
            out = td / "out.json"
            out.write_text("prior")
            r = self.cli("metering", "audit", "--db", str(db),
                         "--dataset", sha, "--invoices", str(invoices),
                         "--output", str(out))
            self.assertEqual(r.returncode, 2)
            self.assertEqual(out.read_text(), "prior")

    def test_same_path_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / "m.db"
            src = td / "source.json"
            src.write_bytes(source_bytes([]))
            r = self.cli("metering", "ingest", "--db", str(db),
                         "--source", str(src))
            sha = json.loads(r.stdout)["dataset_sha256"]
            invoices = td / "invoices.json"
            invoices.write_text("[]")
            r = self.cli("metering", "audit", "--db", str(db),
                         "--dataset", sha, "--invoices", str(invoices),
                         "--output", str(invoices))
            self.assertEqual(r.returncode, 2)
            link = td / "link.json"
            link.symlink_to(invoices)
            r = self.cli("metering", "audit", "--db", str(db),
                         "--dataset", sha, "--invoices", str(invoices),
                         "--output", str(link))
            self.assertEqual(r.returncode, 2)
            r = self.cli("metering", "audit", "--db", str(db),
                         "--dataset", sha, "--invoices", str(invoices),
                         "--output", str(db))
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
