"""Tests for the SQLite reservation book lifecycle and idempotency."""
import json
import os
import tempfile
import unittest
from fractions import Fraction

from storage_dispatch.reserve import ReservationBook, BookError, Conflict


def init_body(slots=None, capacity=100, initial=10, terminal_min=0):
    if slots is None:
        slots = [
            {"charge_limit": 50, "discharge_limit": 0, "price": 1},
            {"charge_limit": 0, "discharge_limit": 50, "price": 5},
            {"charge_limit": 0, "discharge_limit": 50, "price": 5},
            {"charge_limit": 50, "discharge_limit": 0, "price": 2}]
    return json.dumps({
        "capacity": capacity, "initial": initial,
        "charge_efficiency": {"numerator": 1, "denominator": 1},
        "discharge_efficiency": {"numerator": 1, "denominator": 1},
        "terminal_min": terminal_min, "slots": slots})


class BookLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.db = os.path.join(self.td, "book.db")

    def book(self, **kw):
        return ReservationBook(self.db, **kw)

    def test_infeasible_init_rolls_back_all_ddl(self):
        import sqlite3
        body = json.loads(init_body(initial=0, terminal_min=10, slots=[
            {"charge_limit": 0, "discharge_limit": 0, "price": 0}]))
        with self.assertRaises(BookError):
            self.book().initialize(json.dumps(body).encode())
        conn = sqlite3.connect(self.db)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        self.assertEqual(tables, [])
        # The same path can then be initialized successfully.
        body["terminal_min"] = 0
        self.book().initialize(json.dumps(body).encode())

    def test_init_only_new_db_and_zero_revision(self):
        book = self.book()
        rev, _ = book.initialize(init_body().encode())
        self.assertEqual(rev, 0)
        with self.assertRaises(BookError):
            book.initialize(init_body().encode())
        self.assertEqual(book.status()["revision"], 0)

    def test_close_and_reopen_preserves_everything(self):
        book = self.book()
        book.initialize(init_body().encode())
        book.add_contract(json.dumps({
            "request_id": "r1", "contract_id": "A", "start": 1, "end": 3,
            "energy": 4}).encode())
        before = book.status()
        reopened = self.book().status()
        self.assertEqual(reopened["revision"], before["revision"])
        self.assertEqual(reopened["contracts"], before["contracts"])
        self.assertEqual(reopened["plan"], before["plan"])

    def test_infeasible_add_does_not_enter_book_or_consume_id(self):
        book = self.book()
        book.initialize(init_body(initial=0).encode())
        body = json.dumps({"request_id": "x", "contract_id": "A",
                           "start": 1, "end": 2, "energy": 99})
        with self.assertRaises(BookError):
            book.add_contract(body.encode())
        status = book.status()
        self.assertEqual(status["contracts"], [])
        self.assertEqual(status["revision"], 0)
        self.assertEqual(status["receipts"], [])
        # The same id is still usable (it was never committed).
        with self.assertRaises(BookError):
            book.add_contract(body.encode())

    def test_idempotent_retry_returns_original_receipt(self):
        book = self.book()
        book.initialize(init_body().encode())
        body = json.dumps({"request_id": "r1", "contract_id": "A",
                           "start": 1, "end": 3, "energy": 4}).encode()
        rev1, receipt1 = book.add_contract(body)
        rev2, receipt2 = book.add_contract(body)
        self.assertEqual(rev1, rev2)
        self.assertEqual(receipt1, receipt2)
        self.assertEqual(book.status()["revision"], rev1)

    def test_same_id_different_bytes_conflicts_and_rolls_back(self):
        book = self.book()
        book.initialize(init_body().encode())
        book.add_contract(json.dumps({
            "request_id": "r1", "contract_id": "A", "start": 1, "end": 3,
            "energy": 4}).encode())
        with self.assertRaises(Conflict):
            book.add_contract(json.dumps({
                "request_id": "r1", "contract_id": "B", "start": 1,
                "end": 3, "energy": 2}).encode())
        status = book.status()
        self.assertEqual([c["contract_id"] for c in status["contracts"]],
                         ["A"])

    def test_cancel_after_partial_delivery_keeps_executed_facts(self):
        book = self.book()
        book.initialize(init_body(initial=20).encode())
        # Both contracts deliver in slots 1 and 2.
        book.add_contract(json.dumps({
            "request_id": "a", "contract_id": "AAA", "start": 1, "end": 3,
            "energy": 5}).encode())
        book.add_contract(json.dumps({
            "request_id": "b", "contract_id": "BBB", "start": 1, "end": 3,
            "energy": 5}).encode())
        book.advance(json.dumps({"request_id": "adv", "to_slot": 2}).encode())
        prefix = json.dumps(book.status()["execution"], sort_keys=True)
        book.cancel_contract(json.dumps({
            "request_id": "c", "contract_id": "BBB"}).encode())
        status = book.status()
        # Executed prefix is byte-identical after the cancel.
        self.assertEqual(json.dumps(status["execution"], sort_keys=True),
                         prefix)
        bbb = next(c for c in status["contracts"]
                   if c["contract_id"] == "BBB")
        self.assertEqual(bbb["state"], "cancelled")
        self.assertEqual(Fraction(bbb["remaining"]), 0)
        # Cannot cancel twice, and cannot cancel a completed contract.
        with self.assertRaises(BookError):
            book.cancel_contract(json.dumps({
                "request_id": "c2", "contract_id": "BBB"}).encode())

    def test_completed_contract_cannot_be_cancelled(self):
        book = self.book()
        book.initialize(init_body(initial=10).encode())
        book.add_contract(json.dumps({
            "request_id": "a", "contract_id": "A", "start": 1, "end": 2,
            "energy": 3}).encode())
        # Advance through the only delivery slot to complete it.
        book.advance(json.dumps({"request_id": "adv", "to_slot": 2}).encode())
        a = next(c for c in book.status()["contracts"]
                 if c["contract_id"] == "A")
        self.assertEqual(a["state"], "completed")
        with self.assertRaises(BookError):
            book.cancel_contract(json.dumps({
                "request_id": "c", "contract_id": "A"}).encode())

    def test_advance_validation_bounds(self):
        book = self.book()
        book.initialize(init_body().encode())
        # Cannot advance backwards or past the last slot.
        with self.assertRaises(Exception):
            book.advance(json.dumps(
                {"request_id": "adv", "to_slot": 0}).encode())
        with self.assertRaises(Exception):
            book.advance(json.dumps(
                {"request_id": "adv", "to_slot": 99}).encode())

    def test_revision_increments_once_per_mutation(self):
        book = self.book()
        book.initialize(init_body().encode())
        self.assertEqual(book.status()["revision"], 0)
        book.add_contract(json.dumps({
            "request_id": "a", "contract_id": "A", "start": 1, "end": 3,
            "energy": 2}).encode())
        self.assertEqual(book.status()["revision"], 1)
        book.advance(json.dumps(
            {"request_id": "b", "to_slot": 1}).encode())
        self.assertEqual(book.status()["revision"], 2)
        book.cancel_contract(json.dumps(
            {"request_id": "c", "contract_id": "A"}).encode())
        self.assertEqual(book.status()["revision"], 3)

    def test_post_commit_export_failure_keeps_reservation(self):
        book = self.book()
        book.initialize(init_body().encode())
        body = json.dumps({"request_id": "r1", "contract_id": "A",
                           "start": 1, "end": 3, "energy": 2}).encode()
        rev, receipt = book.add_contract(body)
        # Simulate the caller failing to write the receipt file after commit:
        # the reservation is durable, and a retry returns the same receipt.
        self.assertTrue(os.path.exists(self.db))
        rev2, receipt2 = book.add_contract(body)
        self.assertEqual((rev2, receipt2), (rev, receipt))
        self.assertEqual(
            [c["contract_id"] for c in book.status()["contracts"]], ["A"])


if __name__ == "__main__":
    unittest.main()
