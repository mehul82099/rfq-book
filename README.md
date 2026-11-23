# rfq-book

A zero-dependency command-line small-batch RFQ and quote comparison book.
Record the parts you need made in small batches, the RFQs you send to
contract manufacturers, and the quotes that come back (unit price, MOQ,
tooling charge, lead time, validity). The book answers: what does each
quote really cost at your batch size, whose MOQ rules them out, who has
not replied, and which vendors actually respond to small-batch RFQs - so
the next sourcing round starts from your own record, not memory.

Single Python file, standard library only (Python 3.9+). Fully offline;
all data lives in one local JSON file you own.

## Why this exists

On Razorpay's public "Fix My Itch" list, one entry asks: "Why do
businesses struggle finding reliable contract manufacturers for small
batches?" (page fetched and entry re-verified on 2026-11-23;
https://razorpay.com/m/fix-my-itch/). That list is curated by Razorpay -
it shows the problem was submitted and published, not how common it is or
that a tool like this is in demand.

No tool can make a factory love a 200-piece order. But the struggle has a
paper-trail side that a record can fix: quotes are hard to compare when
one vendor hides tooling in the unit price and another has an MOQ ten
times your batch, and "reliable" is a guess when nobody remembers who
answered the last three RFQs. This book keeps that record.

## What it does

- Vendors: add each contract manufacturer once, with contact and
  capability notes.
- Parts: record what you need made with the batch size (`--qty`) and an
  optional needed-by date.
- RFQs: log each RFQ sent (one per part-vendor pair; duplicates rejected).
- Quotes: log unit price, MOQ, tooling charge, lead time and validity -
  only against a recorded RFQ, never before it was sent, and only one
  quote per part-vendor pair.
- Compare (`quotes`): every eligible quote ranked by true total at your
  batch size (unit price x batch + tooling). Vendors whose MOQ exceeds
  your batch are shown separately, not ranked. Expired quotes are
  flagged.
- Reliability (`vendors`): per vendor - RFQs sent, answered, response
  rate, average days to quote, how often their MOQ blocked a small batch,
  awards won.
- Follow-up (`awaiting`): RFQs still without a quote, with days waiting.
- Award (`award`): mark the winning quote for a part (one at a time;
  re-awarding moves the mark).
- Export (`export`): all RFQs and quotes to CSV.
- Amounts are stored as integer paise and printed with Indian digit
  grouping (1,95,000.00).

## Install and run

Requires Python 3.9 or newer. Nothing else.

```sh
python3 rfq_book.py --help
```

Data is stored in `rfq_book.json` next to the script (override with
`--db PATH`).

## Example

```console
$ python3 rfq_book.py vendor "PRECISION WORKS" --capabilities "CNC, small batches"
added vendor PRECISION WORKS

$ python3 rfq_book.py vendor "ALPHA CNC"
added vendor ALPHA CNC

$ python3 rfq_book.py vendor "BIG SHOP"
added vendor BIG SHOP

$ python3 rfq_book.py part PRT-101 "gear housing" --qty 200 --needed-by 2026-12-15
recorded part PRT-101 (gear housing): batch of 200, needed by 2026-12-15

$ python3 rfq_book.py rfq PRT-101 "PRECISION WORKS" 2026-11-01
recorded RFQ for part PRT-101 to PRECISION WORKS, sent 2026-11-01

$ python3 rfq_book.py rfq PRT-101 "ALPHA CNC" 2026-11-01
recorded RFQ for part PRT-101 to ALPHA CNC, sent 2026-11-01

$ python3 rfq_book.py rfq PRT-101 "BIG SHOP" 2026-11-01
recorded RFQ for part PRT-101 to BIG SHOP, sent 2026-11-01

$ python3 rfq_book.py quote PRT-101 "PRECISION WORKS" 850 --moq 100 --tooling 25000 --lead 21 2026-11-05
recorded quote from PRECISION WORKS for PRT-101: 850.00/unit, MOQ 100, tooling 25,000.00, lead 21 days, total at batch 1,95,000.00

$ python3 rfq_book.py quote PRT-101 "ALPHA CNC" 900 --moq 100 --lead 28 2026-11-03
recorded quote from ALPHA CNC for PRT-101: 900.00/unit, MOQ 100, tooling 0.00, lead 28 days, total at batch 1,80,000.00

$ python3 rfq_book.py quote PRT-101 "BIG SHOP" 600 --moq 1000 --lead 14 2026-11-02
recorded quote from BIG SHOP for PRT-101: 600.00/unit, MOQ 1000, tooling 0.00, lead 14 days, total at batch 1,20,000.00 [MOQ 1000 exceeds batch of 200]

$ python3 rfq_book.py quotes PRT-101
PRT-101 (gear housing), batch of 200 - quotes ranked by total at batch:
  1. ALPHA CNC              900.00/unit x 200 + tooling 0.00 = 1,80,000.00, lead 28 days
  2. PRECISION WORKS        850.00/unit x 200 + tooling 25,000.00 = 1,95,000.00, lead 21 days
  -  BIG SHOP               MOQ 1000 exceeds batch of 200 (not ranked)

$ python3 rfq_book.py vendors
vendor                  RFQs  answered  response      avg days MOQ-blocked  awarded
ALPHA CNC                  1         1      100%             2           0        0
BIG SHOP                   1         1      100%             1           1        0
PRECISION WORKS            1         1      100%             4           0        0

$ python3 rfq_book.py award PRT-101 "ALPHA CNC"
awarded part PRT-101 to ALPHA CNC at 900.00/unit
```

(Every line above is real output from a run of this script, with numbers
chosen so the math is easy to check: the cheapest unit price loses to
tooling, and the lowest total is disqualified by MOQ.)

## What else is out there (as of 2026-11-23)

Manufacturing sourcing platforms exist and do much more - Zetwerk and
Karkhana.io match buyers with factories and manage production, and
comparison services like FabFlow cover the platform landscape itself.
This repo does not claim to be the only tool in this space, and it is not
a marketplace. Its scope is deliberately smaller: an offline,
zero-install record of the RFQs and quotes you already exchange by phone
and email, so comparisons and vendor response history live in a file you
fully own. If you want factory discovery or managed production, use a
platform.

## Limits

- No discovery: it does not find manufacturers, send RFQs or verify
  capabilities. It records what you send and receive.
- The comparison is arithmetic, not advice: total at batch = unit price x
  batch + tooling. Quality, reliability beyond response history, payment
  terms and logistics are not modeled.
- Statistics describe only what you recorded; they are not proof about
  vendors you never sent an RFQ to.
- Averages round to whole days (half days round to the nearest even).
- Single user, single file. No multi-user access, no sync.
- Durability: saves are atomic (write to a temp file, then replace), which
  protects against interruption mid-save, not against deleting the file.
  Back up the JSON file like any other business record.

## Tests

```sh
python3 -m unittest test_rfq_book -v
```

35 tests cover money and count parsing, Indian formatting, vendor/part/
RFQ/quote validation (duplicates, out-of-order dates, quotes without an
RFQ), total-at-batch and MOQ math, ranking, expiry flags, awaiting lists,
per-vendor response statistics, awarding, CSV export, atomic save/load
roundtrip, and the CLI end to end. CI runs the suite on Ubuntu and
Windows against Python 3.9 and 3.13.

## License

MIT. See [LICENSE](LICENSE).
