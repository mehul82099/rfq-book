#!/usr/bin/env python3
"""rfq-book: zero-dependency small-batch RFQ and quote comparison CLI.

Record the parts you need made in small batches, the RFQs you send to
contract manufacturers, and the quotes that come back (unit price, MOQ,
tooling charge, lead time, validity). The book answers: what does each
quote really cost at your batch size, whose MOQ rules them out, who has
not replied, and which vendors actually respond to small-batch RFQs -
so the next sourcing round starts from your own record, not memory.

Python 3.9+, standard library only. One JSON file, offline, no accounts.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from datetime import date

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "rfq_book.json")


class RfqBookError(Exception):
    """User-facing error (bad input, unknown ref, ...)."""


def today_iso():
    return date.today().isoformat()


def parse_date(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise RfqBookError("bad date %r (expected YYYY-MM-DD)" % text)


def parse_amount(text, allow_zero=False):
    """Rupees string -> integer paise; max 2 decimals."""
    text = (text or "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        raise RfqBookError(
            "bad amount %r (e.g. 850 or 850.50, paise beyond 2 decimals "
            "not accepted)" % text)
    rupees, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    paise = int(rupees) * 100 + int(frac)
    if paise < 0 or (paise == 0 and not allow_zero):
        raise RfqBookError("amount must be positive")
    return paise


def format_paise(paise):
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    s = str(paise // 100)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return "%s%s.%02d" % (sign, s, paise % 100)


def parse_count(text, what):
    try:
        n = int(text)
    except (TypeError, ValueError):
        raise RfqBookError("bad %s %r (expected a whole number)"
                           % (what, text))
    if n <= 0:
        raise RfqBookError("%s must be at least 1" % what)
    return n


def days_between(a_iso, b_iso):
    return (parse_date(b_iso) - parse_date(a_iso)).days


def total_at_qty(quote, qty):
    """Total cost in paise for a batch of qty: tooling + unit price x qty."""
    return quote["tooling"] + quote["unit_price"] * qty


def moq_blocks(quote, qty):
    """True when the vendor's MOQ exceeds the requested batch size."""
    return quote["moq"] > qty


def quote_expired(quote, as_of=None):
    if not quote["valid_until"]:
        return False
    as_of = parse_date(as_of) if as_of else date.today()
    return as_of > parse_date(quote["valid_until"])


def waiting_days(rfq, as_of=None):
    as_of = parse_date(as_of) if as_of else date.today()
    return (as_of - parse_date(rfq["sent"])).days


class Book:
    def __init__(self):
        self.vendors = {}  # name -> {name, contact, capabilities, created}
        self.parts = {}    # ref  -> part dict
        self.rfqs = []     # list of {part, vendor, sent}
        self.quotes = []   # list of quote dicts (see record_quote)

    @classmethod
    def load(cls, path):
        book = cls()
        if not os.path.exists(path):
            return book
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise RfqBookError("cannot read %s: %s" % (path, exc))
        book.vendors = data.get("vendors", {})
        book.parts = data.get("parts", {})
        book.rfqs = data.get("rfqs", [])
        book.quotes = data.get("quotes", [])
        return book

    def save(self, path):
        data = {"vendors": self.vendors, "parts": self.parts,
                "rfqs": self.rfqs, "quotes": self.quotes}
        fd, tmp_name = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- vendors and parts ----------------------------------------------

    def add_vendor(self, name, contact="", capabilities=""):
        name = (name or "").strip()
        if not name:
            raise RfqBookError("vendor name cannot be empty")
        if name in self.vendors:
            raise RfqBookError("vendor %r already exists" % name)
        self.vendors[name] = {"name": name, "contact": contact,
                              "capabilities": capabilities,
                              "created": today_iso()}
        return self.vendors[name]

    def get_vendor(self, name):
        try:
            return self.vendors[name]
        except KeyError:
            raise RfqBookError("unknown vendor %r (add it first)" % (name,))

    def add_part(self, ref, name, qty, needed_by=None, specs=""):
        ref = (ref or "").strip()
        if not ref:
            raise RfqBookError("part ref cannot be empty")
        if ref in self.parts:
            raise RfqBookError("part ref %r already exists" % ref)
        if needed_by:
            parse_date(needed_by)
        self.parts[ref] = {"ref": ref, "name": name, "qty": qty,
                           "needed_by": needed_by, "specs": specs,
                           "created": today_iso()}
        return self.parts[ref]

    def get_part(self, ref):
        try:
            return self.parts[ref]
        except KeyError:
            raise RfqBookError("unknown part %r (add it first)" % (ref,))

    # -- RFQs and quotes --------------------------------------------------

    def find_rfq(self, part_ref, vendor):
        for rfq in self.rfqs:
            if rfq["part"] == part_ref and rfq["vendor"] == vendor:
                return rfq
        return None

    def send_rfq(self, part_ref, vendor, when):
        self.get_part(part_ref)
        self.get_vendor(vendor)
        parse_date(when)
        if self.find_rfq(part_ref, vendor):
            raise RfqBookError(
                "an RFQ for part %s to %s is already recorded; one per "
                "part-vendor pair" % (part_ref, vendor))
        rfq = {"part": part_ref, "vendor": vendor, "sent": when}
        self.rfqs.append(rfq)
        return rfq

    def quotes_for(self, part_ref, vendor=None):
        return [q for q in self.quotes
                if q["part"] == part_ref
                and (vendor is None or q["vendor"] == vendor)]

    def record_quote(self, part_ref, vendor, unit_price, moq, tooling,
                     lead_days, received, valid_until=None):
        rfq = self.find_rfq(part_ref, vendor)
        if not rfq:
            raise RfqBookError(
                "no RFQ recorded for part %s to %s; record the RFQ first "
                "(you can only log quotes you asked for)" % (part_ref,
                                                             vendor))
        if parse_date(received) < parse_date(rfq["sent"]):
            raise RfqBookError(
                "quote received %s is before the RFQ was sent %s"
                % (received, rfq["sent"]))
        if valid_until and parse_date(valid_until) < parse_date(received):
            raise RfqBookError(
                "valid-until %s is before the quote date %s"
                % (valid_until, received))
        if self.quotes_for(part_ref, vendor):
            raise RfqBookError(
                "a quote from %s for part %s is already recorded"
                % (vendor, part_ref))
        quote = {"part": part_ref, "vendor": vendor,
                 "unit_price": unit_price, "moq": moq, "tooling": tooling,
                 "lead_days": lead_days, "received": received,
                 "valid_until": valid_until, "awarded": False}
        self.quotes.append(quote)
        return quote

    def award(self, part_ref, vendor):
        self.get_part(part_ref)
        candidates = self.quotes_for(part_ref, vendor)
        if not candidates:
            raise RfqBookError("no quote from %s for part %s to award"
                               % (vendor, part_ref))
        for q in self.quotes_for(part_ref):
            q["awarded"] = False
        candidates[0]["awarded"] = True
        return candidates[0]

    # -- reporting --------------------------------------------------------

    def comparison(self, part_ref, as_of=None):
        """Eligible quotes (MOQ fits batch) sorted by total cost at qty."""
        part = self.get_part(part_ref)
        quotes = self.quotes_for(part_ref)
        rows = []
        for q in quotes:
            rows.append({
                "quote": q,
                "blocked": moq_blocks(q, part["qty"]),
                "expired": quote_expired(q, as_of),
                "total": total_at_qty(q, part["qty"]),
            })
        eligible = [r for r in rows if not r["blocked"]]
        eligible.sort(key=lambda r: (r["total"], r["quote"]["lead_days"],
                                     r["quote"]["vendor"]))
        blocked = [r for r in rows if r["blocked"]]
        blocked.sort(key=lambda r: r["quote"]["moq"])
        return part, eligible, blocked

    def awaiting(self, as_of=None):
        rows = []
        for rfq in self.rfqs:
            if not self.quotes_for(rfq["part"], rfq["vendor"]):
                rows.append((rfq, waiting_days(rfq, as_of)))
        rows.sort(key=lambda row: (-row[1], row[0]["part"]))
        return rows

    def vendor_stats(self, name, as_of=None):
        rfqs = [r for r in self.rfqs if r["vendor"] == name]
        answered = []
        response_days = []
        for r in rfqs:
            qs = self.quotes_for(r["part"], name)
            if qs:
                answered.append(r)
                response_days.append(days_between(r["sent"],
                                                  qs[0]["received"]))
        awarded = [q for q in self.quotes
                   if q["vendor"] == name and q["awarded"]]
        moq_blocked = 0
        for q in self.quotes:
            if q["vendor"] == name:
                part = self.parts.get(q["part"])
                if part and moq_blocks(q, part["qty"]):
                    moq_blocked += 1
        return {
            "rfqs": len(rfqs),
            "answered": len(answered),
            "response_pct": (round(100.0 * len(answered) / len(rfqs))
                             if rfqs else None),
            "avg_response_days": (round(sum(response_days)
                                        / len(response_days))
                                  if response_days else None),
            "quotes": len([q for q in self.quotes if q["vendor"] == name]),
            "moq_blocked": moq_blocked,
            "awarded": len(awarded),
        }

    def summary(self, as_of=None):
        return {
            "vendors": len(self.vendors),
            "parts": len(self.parts),
            "rfqs": len(self.rfqs),
            "quotes": len(self.quotes),
            "awaiting": len(self.awaiting(as_of)),
            "awarded": sum(1 for q in self.quotes if q["awarded"]),
        }

    def export_csv(self, out_path, as_of=None):
        fields = ["part", "part_name", "batch_qty", "vendor", "rfq_sent",
                  "quote_received", "response_days", "unit_price", "moq",
                  "moq_fits_batch", "tooling", "lead_days", "valid_until",
                  "total_at_batch", "awarded"]
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(fields)
            for rfq in self.rfqs:
                part = self.parts.get(rfq["part"], {})
                qs = self.quotes_for(rfq["part"], rfq["vendor"])
                if not qs:
                    writer.writerow([
                        rfq["part"], part.get("name", ""),
                        part.get("qty", ""), rfq["vendor"], rfq["sent"],
                        "", "", "", "", "", "", "", "", "", ""])
                    continue
                for q in qs:
                    writer.writerow([
                        rfq["part"], part.get("name", ""), part.get("qty",
                                                                    ""),
                        rfq["vendor"], rfq["sent"], q["received"],
                        days_between(rfq["sent"], q["received"]),
                        format_paise(q["unit_price"]), q["moq"],
                        "no" if moq_blocks(q, part.get("qty", 0)) else "yes",
                        format_paise(q["tooling"]), q["lead_days"],
                        q["valid_until"] or "",
                        format_paise(total_at_qty(q, part.get("qty", 0))),
                        "yes" if q["awarded"] else "no",
                    ])


def _pct(value):
    return "-" if value is None else "%d%%" % value


def cmd_vendor_add(book, args):
    book.add_vendor(args.name, contact=args.contact or "",
                    capabilities=args.capabilities or "")
    print("added vendor %s" % args.name)


def cmd_part_add(book, args):
    part = book.add_part(args.ref, args.name, parse_count(args.qty, "qty"),
                         needed_by=args.needed_by, specs=args.specs or "")
    print("recorded part %s (%s): batch of %d%s" % (
        part["ref"], part["name"], part["qty"],
        ", needed by %s" % part["needed_by"] if part["needed_by"] else ""))


def cmd_rfq(book, args):
    rfq = book.send_rfq(args.part, args.vendor, args.date)
    print("recorded RFQ for part %s to %s, sent %s"
          % (rfq["part"], rfq["vendor"], rfq["sent"]))


def cmd_quote(book, args):
    q = book.record_quote(
        args.part, args.vendor, parse_amount(args.unit_price),
        parse_count(args.moq, "MOQ"),
        parse_amount(args.tooling, allow_zero=True),
        parse_count(args.lead, "lead time"), args.date,
        valid_until=args.valid_until)
    part = book.get_part(args.part)
    flag = " [MOQ %d exceeds batch of %d]" % (q["moq"], part["qty"]) \
        if moq_blocks(q, part["qty"]) else ""
    print("recorded quote from %s for %s: %s/unit, MOQ %d, tooling %s, "
          "lead %d days, total at batch %s%s" % (
              q["vendor"], q["part"], format_paise(q["unit_price"]),
              q["moq"], format_paise(q["tooling"]), q["lead_days"],
              format_paise(total_at_qty(q, part["qty"])), flag))


def cmd_quotes(book, args):
    part, eligible, blocked = book.comparison(args.part, args.as_of)
    if not eligible and not blocked:
        print("%s: no quotes recorded yet" % args.part)
        return
    print("%s (%s), batch of %d - quotes ranked by total at batch:" % (
        part["ref"], part["name"], part["qty"]))
    rank = 1
    for row in eligible:
        q = row["quote"]
        marks = []
        if row["expired"]:
            marks.append("validity expired")
        if q["awarded"]:
            marks.append("AWARDED")
        suffix = "  [%s]" % "; ".join(marks) if marks else ""
        print("  %d. %-22s %s/unit x %d + tooling %s = %s, lead %d days%s"
              % (rank, q["vendor"], format_paise(q["unit_price"]),
                 part["qty"], format_paise(q["tooling"]),
                 format_paise(row["total"]), q["lead_days"], suffix))
        rank += 1
    for row in blocked:
        q = row["quote"]
        print("  -  %-22s MOQ %d exceeds batch of %d (not ranked)"
              % (q["vendor"], q["moq"], part["qty"]))


def cmd_awaiting(book, args):
    rows = book.awaiting(args.as_of)
    if not rows:
        print("no unanswered RFQs")
        return
    as_of = args.as_of or today_iso()
    print("unanswered RFQs as of %s:" % as_of)
    for rfq, days in rows:
        state = "sent today" if days == 0 else "waiting %d days" % days
        print("  part %-10s to %-22s sent %s, %s" % (
            rfq["part"], rfq["vendor"], rfq["sent"], state))


def cmd_vendors(book, args):
    if not book.vendors:
        print("no vendors yet")
        return
    print("%-22s %5s %9s %9s %13s %11s %8s" % (
        "vendor", "RFQs", "answered", "response", "avg days", "MOQ-blocked",
        "awarded"))
    for name in sorted(book.vendors):
        s = book.vendor_stats(name, args.as_of)
        print("%-22s %5d %9d %9s %13s %11d %8d" % (
            name, s["rfqs"], s["answered"], _pct(s["response_pct"]),
            s["avg_response_days"] if s["answered"] else "-",
            s["moq_blocked"], s["awarded"]))


def cmd_award(book, args):
    q = book.award(args.part, args.vendor)
    part = book.get_part(args.part)
    flag = " (note: MOQ %d exceeds batch of %d)" % (q["moq"], part["qty"]) \
        if moq_blocks(q, part["qty"]) else ""
    print("awarded part %s to %s at %s/unit%s" % (
        args.part, args.vendor, format_paise(q["unit_price"]), flag))


def cmd_export(book, args):
    book.export_csv(args.path, args.as_of)
    print("wrote %s" % args.path)


def cmd_summary(book, args):
    s = book.summary(args.as_of)
    print("vendors: %d, parts: %d, RFQs: %d, quotes: %d, unanswered: %d, "
          "awarded: %d" % (s["vendors"], s["parts"], s["rfqs"], s["quotes"],
                           s["awaiting"], s["awarded"]))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rfq-book",
        description="Small-batch RFQ and quote comparison book: true cost "
                    "at your batch size, MOQ blockers, and who actually "
                    "responds.")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="JSON data file (default: %(default)s)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_as_of(p):
        p.add_argument("--as-of", default=None,
                       help="compute waiting/expiry as of YYYY-MM-DD "
                            "(default: today)")

    p = sub.add_parser("vendor", help="vendor add")
    p.add_argument("name")
    p.add_argument("--contact", default=None)
    p.add_argument("--capabilities", default=None)
    p.set_defaults(func=cmd_vendor_add)

    p = sub.add_parser("part", help="part add: what you need made")
    p.add_argument("ref")
    p.add_argument("name")
    p.add_argument("--qty", required=True, help="batch size, whole units")
    p.add_argument("--needed-by", default=None, help="YYYY-MM-DD")
    p.add_argument("--specs", default=None)
    p.set_defaults(func=cmd_part_add)

    p = sub.add_parser("rfq", help="record an RFQ sent to a vendor")
    p.add_argument("part")
    p.add_argument("vendor")
    p.add_argument("date", help="date sent, YYYY-MM-DD")
    p.set_defaults(func=cmd_rfq)

    p = sub.add_parser("quote", help="record a quote received")
    p.add_argument("part")
    p.add_argument("vendor")
    p.add_argument("unit_price", help="rupees per unit, e.g. 850 or 850.50")
    p.add_argument("--moq", required=True, help="minimum order quantity")
    p.add_argument("--tooling", default="0",
                   help="one-time tooling charge in rupees (default 0)")
    p.add_argument("--lead", required=True, help="lead time in days")
    p.add_argument("--valid-until", default=None, help="YYYY-MM-DD")
    p.add_argument("date", help="date received, YYYY-MM-DD")
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("quotes", help="compare quotes for a part")
    p.add_argument("part")
    add_as_of(p)
    p.set_defaults(func=cmd_quotes)

    p = sub.add_parser("awaiting", help="RFQs still without a quote")
    add_as_of(p)
    p.set_defaults(func=cmd_awaiting)

    p = sub.add_parser("vendors", help="per-vendor response scorecard")
    add_as_of(p)
    p.set_defaults(func=cmd_vendors)

    p = sub.add_parser("award", help="mark a vendor's quote as awarded")
    p.add_argument("part")
    p.add_argument("vendor")
    p.set_defaults(func=cmd_award)

    p = sub.add_parser("export", help="export RFQs and quotes to CSV")
    p.add_argument("path")
    add_as_of(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("summary", help="one-line totals")
    add_as_of(p)
    p.set_defaults(func=cmd_summary)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        book = Book.load(args.db)
        args.func(book, args)
        book.save(args.db)
    except RfqBookError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
