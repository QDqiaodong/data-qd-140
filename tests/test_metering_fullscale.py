"""Executable 24-slot / 64-reading / 4-invoice full-scale sample."""
import json
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from storage_dispatch.metering.audit import run_ingest, run_audit
from storage_dispatch.metering.domain import MeteringDomain
from storage_dispatch.metering.billing import BillingSolver
from storage_dispatch.metering.schema import (parse_strict_json,
                                              validate_ingest,
                                              validate_invoices)

ROOT = Path(__file__).resolve().parents[1]


class FullScaleTests(unittest.TestCase):
    def test_full_pipeline_on_shipped_fixture(self):
        source_path = ROOT / "fixtures" / "metering_source.json"
        invoice_path = ROOT / "fixtures" / "metering_invoices.json"
        source_bytes = source_path.read_bytes()
        invoice_bytes = invoice_path.read_bytes()
        device, readings, slots = validate_ingest(
            parse_strict_json(source_bytes))
        self.assertEqual((slots, len(readings)), (24, 64))
        invoices = validate_invoices(
            parse_strict_json(invoice_bytes), slots)
        self.assertEqual(len(invoices), 4)

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "full.db"
            receipt = run_ingest(db, source_bytes)
            report = run_audit(db, receipt["dataset_sha256"], invoice_bytes,
                               Path(td) / "unused.json")
        self.assertEqual(report["status"], "possible")
        self.assertEqual(len(report["invoices"]), 4)

        domain = MeteringDomain(device, readings, slots)
        solver = BillingSolver(domain, invoices)
        for entry, ticket in zip(report["invoices"], invoices):
            self.assertEqual(entry["invoice_id"], ticket["invoice_id"])
            self.assertTrue(Fraction(entry["min"]) <= ticket["amount"]
                            <= Fraction(entry["max"]))
        # The single common witness satisfies every ticket exactly and every
        # original observation on the complete 24-slot timeline.
        witness = report["joint_witness"]
        self.assertEqual(len(witness["slots"]), 24)
        self.assertEqual(witness["conservation_residual"], "0")
        rebuilt_witness = _rebuild(witness, domain)
        for ticket in invoices:
            value, _ = solver.evaluate(ticket, rebuilt_witness)
            self.assertEqual(value, ticket["amount"])
        self.assertIsNone(domain.verify_witness(rebuilt_witness))

    def test_cli_commands_match_library_results(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / "m.db"
            out = td / "report.json"
            r = subprocess.run(
                [sys.executable, "-m", "storage_dispatch.cli", "metering",
                 "ingest", "--db", str(db),
                 "--source", str(ROOT / "fixtures" / "metering_source.json")],
                capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(r.returncode, 0, r.stderr)
            sha = json.loads(r.stdout)["dataset_sha256"]
            r = subprocess.run(
                [sys.executable, "-m", "storage_dispatch.cli", "metering",
                 "audit", "--db", str(db), "--dataset", sha,
                 "--invoices", str(ROOT / "fixtures" /
                                   "metering_invoices.json"),
                 "--output", str(out)],
                capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_text())["status"],
                             "possible")


def _rebuild(json_witness, domain):
    """Turn canonical-string JSON witness back into Fraction values."""
    rebuilt = dict(json_witness)
    rebuilt["slots"] = [
        {k: (Fraction(v) if k not in ("slot", "request", "direction") else v)
         for k, v in item.items()} for item in json_witness["slots"]]
    for key in ("total_import", "total_export", "total_loss", "initial_soc",
                "final_soc", "conservation_residual"):
        rebuilt[key] = Fraction(json_witness[key])
    return rebuilt


if __name__ == "__main__":
    unittest.main()
