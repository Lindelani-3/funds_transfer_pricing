# The Consolidation Pipeline — How It's Built

Our "consolidation pipeline" is a Python engine that takes the FTP curve
(see `../curve_model/`) and actually applies it — account by account,
across the whole balance sheet. If the curve is the track (see
`../curve_model/CURVE_CONSTRUCTION_ENGINE.md`), this is where every
account actually gets seated on it: we decide *what type of seating*
each account gets, *what type of seat* (cabin) it rides in, look up its
rate, charge or credit it accordingly, and roll the results up from
daily through weekly, monthly, and segment-level consolidation. See the
top-level `README.md` for the full narrative this pipeline carries out.

**This folder tells the architecture story. It doesn't contain the full
production source** — the pipeline encodes a substantial amount of
bank-specific business logic (product classification rules, WAL
methodology, reconciliation handling) that we're deliberately not
publishing here, for IP reasons. Full source is available on request.

## How the run flows

```
Meta → Naretail / Prev Naretail → Run Naretail ─┐
Meta → ABF / Prev ABF           → Run ABF       ─┼─► Daily Conso (per branch)
                                                   │
                                                   ├─► Weekly Conso
                                                   ├─► Monthly Conso
                                                   └─► Segment Conso (all branches)
```

We run daily consolidation per branch, then build weekly, monthly, and
segment-level views from those already-produced lower-level outputs
rather than re-deriving everything from raw data at every level.

## How we decide each account's type of seating

Every account resolves to one `CHOSEN_WAL` — the tenor point, the seat,
we look its rate up at — via one of three types of seating, chosen by
product type:

- **Contractual — a fixed, reserved seat.** A real, reliable maturity
  date exists and genuinely predicts behavior (fixed-term deposits, for
  instance): we pass the real tenor straight through.
- **Persistence (structural split) — the same cabin, four seats at
  once.** No maturity date exists at all, but the balance clearly has
  real staying power (call and current accounts): we split it into up
  to four behavioral tranches — overnight, short, medium, and long term
  — and price each one separately, all four staying in the account's
  own cabin.
- **Prepayment — a seat closer in than the paperwork says.** A real
  maturity date exists but overstates actual life (amortizing loans):
  we apply a behavioral adjustment for early settlement and refinancing.

## How we decide each account's type of seat

Separately from *how* an account got seated, every account also carries
a cabin — asset or liability — derived from its GL line. Cabin is what
decides whether the pipeline charges the account (an asset, drawing
funding) or credits it (a liability, supplying funding) once its seat is
known.

In practice, type of seating and cabin line up closely: structural split
is practically always the liability cabin (call/current/notice accounts
are liabilities almost by definition), and prepayment is practically
always the asset cabin (loans are assets almost by definition). That's
a real pattern in how products get classified, not a rule the pipeline
enforces. It also isn't purely cosmetic where it does interact: for
structural-split tranches specifically, the tenor-class WAL reference
lookup is keyed on cabin as well as tenor classification, so the same
tenor classification can carry a genuinely different WAL for an asset
than for a liability — cabin can occasionally decide *which* seat gets
picked, not just which direction the resulting payment flows.

See `../docs/` for the full reasoning and worked examples behind each
of these.

## Try it yourself

`sample_snippets.py` — six short, runnable functions covering both types
of seating and both types of seat, simplified from the real pipeline but
built to reproduce the exact TERM001/CALL001/HOME001 numbers used
throughout this repo's docs and the top-level README. Run it directly:

```bash
python sample_snippets.py
```

Prefer reading it as a walkthrough rather than a script? Open
`sample_snippets_demo.ipynb` instead — same six functions, imported
from `sample_snippets.py` (so there's exactly one copy of the logic),
narrated one at a time with each result rendered as a table rather than
printed to a console.

## What else lives here (architecture only, not published)

- A classification and product-mapping layer, with an override mechanism
  for correcting misclassified accounts after the initial mapping runs
  — this is also where each account's cabin (asset/liability) gets
  derived from its GL line.
- Reconciliation and data-quality tracking — account-status flagging,
  product-mapping-gap detection — threaded through every consolidation
  level.
- A reference-data model built on three mapping workbooks (product and
  classification rules, WAL and tenor rules, run scope) — see
  `../demo_data/SYNTHETIC_DATA_GENERATOR.md` for the anonymized versions
  of these.

## Where we hit real limits

- **We're subject to whatever structure and quality the source systems
  give us.** Our outputs are only as reliable as the raw extracts
  feeding them — schema drift or data-quality issues upstream propagate
  through rather than getting silently corrected. Our answer is to
  surface it (account-status flags, product-mapping-gap tracking at
  every level) rather than mask it, but that's not the same as fixing it
  for you.
- **Mapping coverage needs upkeep.** A genuinely new or reclassified
  product needs its own mapping entry before it prices correctly. Until
  then, we flag it as unmapped rather than silently dropping or
  mispricing it — by design, but it does mean the mapping tables are a
  living artifact, not a one-time setup.
- **We drew a line on scope.** Credit-risk/ECL integration and full
  NII/EVE scenario testing are deliberately outside this stage — this
  pipeline handles FTP pricing and consolidation, not the broader IRRBB
  scenario suite.

## Dependencies

`pandas`, `numpy`, `openpyxl` (our I/O layer).
