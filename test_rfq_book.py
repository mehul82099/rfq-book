#!/usr/bin/env python3
"""Tests for rfq_book. Run: python3 -m unittest test_rfq_book -v"""

import contextlib
import csv
import io
import json
import os
import tempfile
import unittest

import rfq_book as rb


def book_with_vendor(name="PRECISION WORKS"):
    book = rb.Book()
    book.add_vendor(name)
    return book


def book_with_part(qty=200):
    book = book_with_vendor()
    book.add_part("PRT-1", "gear housing", qty, needed_by="2026-12-15")
    return book


def book_with_rfq(qty=200):
    book = book_with_part(qty)
    book.send_rfq("PRT-1", "PRECISION WORKS", "2026-11-01")
    return book


class ParseTest(unittest.TestCase):
    def test_parse_amount(self):
        self.assertEqual(rb.parse_amount("850"), 85000)
        self.assertEqual(rb.parse_amount("850.50"), 85050)
        self.assertEqual(rb.parse_amount("0", allow_zero=True), 0)
        with self.assertRaises(rb.RfqBookError):
            rb.parse_amount("0")
        for bad in ("abc", "10.123", "1,000", "-5"):
            with self.assertRaises(rb.RfqBookError, msg=bad):
                rb.parse_amount(bad)

    def test_format_paise_indian_grouping(self):
        self.assertEqual(rb.format_paise(17000050), "1,70,000.50")
        self.assertEqual(rb.format_paise(85000), "850.00")
        self.assertEqual(rb.format_paise(99), "0.99")

    def test_parse_count(self):
        self.assertEqual(rb.parse_count("200", "qty"), 200)
        with self.assertRaises(rb.RfqBookError):
            rb.parse_count("0", "qty")
        with self.assertRaises(rb.RfqBookError):
            rb.parse_count("two", "qty")

    def test_parse_date(self):
        self.assertEqual(rb.parse_date("2026-11-01").isoformat(),
                         "2026-11-01")
        with self.assertRaises(rb.RfqBookError):
            rb.parse_date("Nov 1 2026")


class VendorPartTest(unittest.TestCase):
    def test_add_vendor_and_part(self):
        book = book_with_part(200)
        self.assertEqual(book.get_part("PRT-1")["qty"], 200)
        self.assertEqual(book.get_vendor("PRECISION WORKS")["name"],
                         "PRECISION WORKS")

    def test_duplicate_vendor_rejected(self):
        book = book_with_vendor()
        with self.assertRaises(rb.RfqBookError):
            book.add_vendor("PRECISION WORKS")

    def test_duplicate_part_rejected(self):
        book = book_with_part()
        with self.assertRaises(rb.RfqBookError):
            book.add_part("PRT-1", "other", 100)

    def test_empty_names_rejected(self):
        with self.assertRaises(rb.RfqBookError):
            rb.Book().add_vendor(" ")
        with self.assertRaises(rb.RfqBookError):
            rb.Book().add_part(" ", "x", 10)


class RfqQuoteRulesTest(unittest.TestCase):
    def test_rfq_records(self):
        book = book_with_rfq()
        rfq = book.find_rfq("PRT-1", "PRECISION WORKS")
        self.assertEqual(rfq["sent"], "2026-11-01")

    def test_rfq_unknown_part_or_vendor(self):
        book = rb.Book()
        with self.assertRaises(rb.RfqBookError):
            book.send_rfq("PRT-1", "V", "2026-11-01")

    def test_duplicate_rfq_rejected(self):
        book = book_with_rfq()
        with self.assertRaises(rb.RfqBookError):
            book.send_rfq("PRT-1", "PRECISION WORKS", "2026-11-02")

    def test_quote_without_rfq_rejected(self):
        book = book_with_part()
        with self.assertRaises(rb.RfqBookError):
            book.record_quote("PRT-1", "PRECISION WORKS", 85000, 100, 0,
                              21, "2026-11-05")

    def test_quote_before_rfq_rejected(self):
        book = book_with_rfq()
        with self.assertRaises(rb.RfqBookError):
            book.record_quote("PRT-1", "PRECISION WORKS", 85000, 100, 0,
                              21, "2026-10-30")

    def test_duplicate_quote_rejected(self):
        book = book_with_rfq()
        book.record_quote("PRT-1", "PRECISION WORKS", 85000, 100, 0, 21,
                          "2026-11-05")
        with self.assertRaises(rb.RfqBookError):
            book.record_quote("PRT-1", "PRECISION WORKS", 90000, 100, 0,
                              21, "2026-11-06")

    def test_valid_until_before_received_rejected(self):
        book = book_with_rfq()
        with self.assertRaises(rb.RfqBookError):
            book.record_quote("PRT-1", "PRECISION WORKS", 85000, 100, 0,
                              21, "2026-11-05", valid_until="2026-11-04")

    def test_totals_and_moq(self):
        book = book_with_rfq(qty=200)
        q = book.record_quote("PRT-1", "PRECISION WORKS", 85000, 150,
                              2500000, 21, "2026-11-05")
        # 850.00 x 200 + 25,000.00 = 1,95,000.00
        self.assertEqual(rb.total_at_qty(q, 200), 19500000)
        self.assertFalse(rb.moq_blocks(q, 200))
        q2 = book.find_rfq("PRT-1", "PRECISION WORKS")
        self.assertIsNotNone(q2)
        book.add_vendor("BIG SHOP")
        book.send_rfq("PRT-1", "BIG SHOP", "2026-11-01")
        q3 = book.record_quote("PRT-1", "BIG SHOP", 70000, 1000, 0, 14,
                               "2026-11-04")
        self.assertTrue(rb.moq_blocks(q3, 200))

    def test_quote_expiry(self):
        book = book_with_rfq()
        q = book.record_quote("PRT-1", "PRECISION WORKS", 85000, 100, 0,
                              21, "2026-11-05", valid_until="2026-11-20")
        self.assertFalse(rb.quote_expired(q, "2026-11-20"))
        self.assertTrue(rb.quote_expired(q, "2026-11-21"))


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        # Part batch 200; three vendors.
        self.book = book_with_part(qty=200)
        self.book.add_vendor("ALPHA CNC")
        self.book.add_vendor("BIG SHOP")
        for v in ("PRECISION WORKS", "ALPHA CNC", "BIG SHOP"):
            self.book.send_rfq("PRT-1", v, "2026-11-01")
        # PRECISION: 850/unit + 25k tooling -> 1,95,000.00
        self.book.record_quote("PRT-1", "PRECISION WORKS", 85000, 100,
                               2500000, 21, "2026-11-05")
        # ALPHA: 900/unit + 0 tooling -> 1,80,000.00 (cheaper total)
        self.book.record_quote("PRT-1", "ALPHA CNC", 90000, 100, 0, 28,
                               "2026-11-03")
        # BIG SHOP: cheapest unit but MOQ 1000 -> blocked
        self.book.record_quote("PRT-1", "BIG SHOP", 60000, 1000, 0, 14,
                               "2026-11-02")

    def test_ranking_by_total_at_batch(self):
        part, eligible, blocked = self.book.comparison("PRT-1")
        self.assertEqual([r["quote"]["vendor"] for r in eligible],
                         ["ALPHA CNC", "PRECISION WORKS"])
        self.assertEqual(eligible[0]["total"], 18000000)
        self.assertEqual(eligible[1]["total"], 19500000)
        self.assertEqual([r["quote"]["vendor"] for r in blocked],
                         ["BIG SHOP"])

    def test_tooling_changes_winner(self):
        # Same unit prices would mislead; tooling flips the ranking here.
        part, eligible, _ = self.book.comparison("PRT-1")
        self.assertEqual(eligible[0]["quote"]["vendor"], "ALPHA CNC")

    def test_awaiting_lists_unanswered(self):
        self.book.add_vendor("SILENT INDUSTRIES")
        self.book.send_rfq("PRT-1", "SILENT INDUSTRIES", "2026-11-10")
        rows = self.book.awaiting("2026-11-23")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0]["vendor"], "SILENT INDUSTRIES")
        self.assertEqual(rows[0][1], 13)

    def test_award(self):
        q = self.book.award("PRT-1", "ALPHA CNC")
        self.assertTrue(q["awarded"])
        others = self.book.quotes_for("PRT-1", "PRECISION WORKS")
        self.assertFalse(others[0]["awarded"])

    def test_award_without_quote_rejected(self):
        self.book.add_vendor("NO QUOTE CO")
        self.book.send_rfq("PRT-1", "NO QUOTE CO", "2026-11-01")
        with self.assertRaises(rb.RfqBookError):
            self.book.award("PRT-1", "NO QUOTE CO")

    def test_award_switches_cleanly(self):
        self.book.award("PRT-1", "ALPHA CNC")
        q = self.book.award("PRT-1", "PRECISION WORKS")
        self.assertTrue(q["awarded"])
        self.assertFalse(self.book.quotes_for("PRT-1", "ALPHA CNC")[0]
                         ["awarded"])

    def test_expired_quote_marked_in_comparison(self):
        _, eligible, _ = self.book.comparison("PRT-1", as_of="2027-01-01")
        # quotes had no valid_until, so none expire
        self.assertFalse(any(r["expired"] for r in eligible))


class VendorStatsTest(unittest.TestCase):
    def setUp(self):
        self.book = book_with_part(qty=200)
        self.book.add_vendor("ALPHA CNC")
        # PRECISION: 2 RFQs, 1 answered in 4 days, quote awarded
        self.book.add_part("PRT-2", "bracket", 50)
        self.book.send_rfq("PRT-1", "PRECISION WORKS", "2026-11-01")
        self.book.send_rfq("PRT-2", "PRECISION WORKS", "2026-11-02")
        self.book.record_quote("PRT-1", "PRECISION WORKS", 85000, 100, 0,
                               21, "2026-11-05")
        self.book.award("PRT-1", "PRECISION WORKS")
        # ALPHA: 1 RFQ, answered in 10 days, MOQ blocks
        self.book.send_rfq("PRT-1", "ALPHA CNC", "2026-11-01")
        self.book.record_quote("PRT-1", "ALPHA CNC", 70000, 500, 0, 21,
                               "2026-11-11")

    def test_precision_stats(self):
        s = self.book.vendor_stats("PRECISION WORKS")
        self.assertEqual(s["rfqs"], 2)
        self.assertEqual(s["answered"], 1)
        self.assertEqual(s["response_pct"], 50)
        self.assertEqual(s["avg_response_days"], 4)
        self.assertEqual(s["awarded"], 1)
        self.assertEqual(s["moq_blocked"], 0)

    def test_alpha_stats(self):
        s = self.book.vendor_stats("ALPHA CNC")
        self.assertEqual(s["response_pct"], 100)
        self.assertEqual(s["avg_response_days"], 10)
        self.assertEqual(s["moq_blocked"], 1)

    def test_empty_vendor_stats(self):
        self.book.add_vendor("NEWCO")
        s = self.book.vendor_stats("NEWCO")
        self.assertEqual(s["rfqs"], 0)
        self.assertIsNone(s["response_pct"])


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "book.json")

    def test_roundtrip(self):
        book = book_with_rfq()
        book.record_quote("PRT-1", "PRECISION WORKS", 85000, 100, 500000,
                          21, "2026-11-05", valid_until="2026-12-01")
        book.award("PRT-1", "PRECISION WORKS")
        book.save(self.path)
        loaded = rb.Book.load(self.path)
        q = loaded.quotes_for("PRT-1", "PRECISION WORKS")[0]
        self.assertEqual(q["tooling"], 500000)
        self.assertTrue(q["awarded"])
        self.assertEqual(q["valid_until"], "2026-12-01")

    def test_load_missing_file_gives_empty_book(self):
        book = rb.Book.load(os.path.join(self.tmp.name, "nope.json"))
        self.assertEqual(len(book.vendors), 0)

    def test_corrupt_file_raises_user_error(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ nope")
        with self.assertRaises(rb.RfqBookError):
            rb.Book.load(self.path)

    def test_save_is_atomic_replace(self):
        book = book_with_vendor()
        book.save(self.path)
        self.assertTrue(os.path.exists(self.path))
        leftovers = [n for n in os.listdir(self.tmp.name) if n != "book.json"]
        self.assertEqual(leftovers, [])
        with open(self.path, encoding="utf-8") as fh:
            json.load(fh)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "book.json")

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rb.main(["--db", self.db] + list(argv))
        return code, out.getvalue()

    def seed(self):
        self.run_cli("vendor", "PRECISION WORKS")
        self.run_cli("vendor", "ALPHA CNC")
        self.run_cli("vendor", "BIG SHOP")
        self.run_cli("part", "PRT-101", "gear housing", "--qty", "200")
        self.run_cli("rfq", "PRT-101", "PRECISION WORKS", "2026-11-01")
        self.run_cli("rfq", "PRT-101", "ALPHA CNC", "2026-11-01")
        self.run_cli("rfq", "PRT-101", "BIG SHOP", "2026-11-01")

    def test_full_flow_and_outputs(self):
        self.seed()
        code, out = self.run_cli("quote", "PRT-101", "PRECISION WORKS",
                                 "850", "--moq", "100", "--tooling",
                                 "25000", "--lead", "21", "2026-11-05")
        self.assertEqual(code, 0)
        self.assertIn("850.00/unit", out)
        self.assertIn("total at batch 1,95,000.00", out)

        code, out = self.run_cli("quote", "PRT-101", "ALPHA CNC", "900",
                                 "--moq", "100", "--lead", "28",
                                 "2026-11-03")
        self.assertEqual(code, 0)

        code, out = self.run_cli("quote", "PRT-101", "BIG SHOP", "600",
                                 "--moq", "1000", "--lead", "14",
                                 "2026-11-02")
        self.assertEqual(code, 0)
        self.assertIn("MOQ 1000 exceeds batch of 200", out)

        code, out = self.run_cli("quotes", "PRT-101")
        self.assertEqual(code, 0)
        self.assertIn("1. ALPHA CNC", out)          # 1,80,000.00 total
        self.assertIn("2. PRECISION WORKS", out)    # 1,95,000.00 total
        self.assertIn("BIG SHOP", out)
        self.assertIn("not ranked", out)

        code, out = self.run_cli("award", "PRT-101", "ALPHA CNC")
        self.assertEqual(code, 0)
        self.assertIn("awarded part PRT-101 to ALPHA CNC", out)

        code, out = self.run_cli("quotes", "PRT-101")
        self.assertIn("AWARDED", out)

        code, out = self.run_cli("vendors")
        self.assertEqual(code, 0)
        self.assertIn("PRECISION WORKS", out)
        self.assertIn("100%", out)

        code, out = self.run_cli("summary")
        self.assertEqual(code, 0)
        self.assertIn("vendors: 3, parts: 1, RFQs: 3, quotes: 3", out)

    def test_cli_awaiting(self):
        self.seed()
        code, out = self.run_cli("awaiting", "--as-of", "2026-11-15")
        self.assertEqual(code, 0)
        self.assertIn("waiting 14 days", out)
        self.assertIn("PRECISION WORKS", out)

    def test_cli_quote_without_rfq_exit_code(self):
        self.run_cli("vendor", "V")
        self.run_cli("part", "P", "widget", "--qty", "10")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = rb.main(["--db", self.db, "quote", "P", "V", "10",
                            "--moq", "1", "--lead", "7", "2026-11-05"])
        self.assertEqual(code, 2)
        self.assertIn("record the RFQ first", err.getvalue())

    def test_cli_export(self):
        self.seed()
        self.run_cli("quote", "PRT-101", "PRECISION WORKS", "850",
                     "--moq", "100", "--tooling", "25000", "--lead", "21",
                     "2026-11-05")
        csv_path = os.path.join(self.tmp.name, "out.csv")
        code, out = self.run_cli("export", csv_path)
        self.assertEqual(code, 0)
        with open(csv_path, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0][0], "part")
        self.assertEqual(len(rows), 4)  # header + 3 RFQ rows
        pw = [r for r in rows[1:] if r[3] == "PRECISION WORKS"][0]
        self.assertEqual(pw[5], "2026-11-05")   # quote received
        self.assertEqual(pw[6], "4")            # response days
        self.assertEqual(pw[7], "850.00")
        self.assertEqual(pw[13], "1,95,000.00")  # total at batch
        alpha = [r for r in rows[1:] if r[3] == "ALPHA CNC"][0]
        self.assertEqual(alpha[5], "")          # no quote yet


if __name__ == "__main__":
    unittest.main()
