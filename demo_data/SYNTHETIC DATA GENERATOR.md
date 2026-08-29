# The Synthetic Data Generator

We wanted anyone cloning this repo to be able to run it end to end
without touching a single real number, so we built this: a generator
that produces a complete, self-contained set of synthetic inputs for
both the curve model and the pipeline. Every account number, balance,
and rate is randomly generated with a fixed seed — reproducible, and
nothing here is drawn from, derived from, or resembles any real
institution's actual figures.

## What it produces

**Reference workbooks** (drive the pipeline's type-of-seating and cabin
logic — see `../pipeline/PIPELINE_ARCHITECTURE.md`):
- `ftp_mappings.xlsx` — product/GL mapping, classification rules, and
  the asset/liability (cabin) derivation
- `wal_mappings.xlsx` — tenor info, structural split %, prepayment WAL
  — the reference data behind each type of seating
- `run_mappings.xlsx` — branches to run, weekday/week-number scope

**Curve model inputs** (synthetic market data):
- `treasury_bills_data_ii.csv` — long format, one row per auction
- `bond_yields_data_ii.csv` — wide format, one row per observation date
- `deposit_rates_compare_ii.csv` — one row per tenor, one column per
  **competitor bank** (there's no own-quote column at all — the curve
  model only ever consumes market rates; we deliberately leave one
  competitor's rate blank at one tenor, so the real fallback-to-next-
  competitor logic actually gets exercised)

**Raw daily extracts** — a week of naretail / ABF / TB_GL data across
five generic branches (Corporate, SME, Private, Retail, Treasury),
covering every type of seating our WAL cascade needs to demonstrate —
contractual, structural-split, and prepayment — across both cabins,
asset and liability, side by side.

## Running it yourself

```bash
python generate_dummy_data.py
```

or work through `02_generate_demo_data.ipynb` cell by cell. Output
lands in `./demo_data/` by default. Run this once before firing up
either `../curve_model/01_curve_construction_demo.ipynb` or the
pipeline.

## Where we hit real limits

- **We built for schema, not for every edge case.** We reproduce real
  column-level quirks faithfully — long vs. wide formats, positional
  column renaming, embedded-unit text fields — because those are
  structural. What we don't reproduce is the messier reality the
  production pipeline handles day to day: duplicate accounts, historical
  corrections landing mid-period, partial or missing daily files. A
  clean run against this demo data isn't proof the pipeline handles
  every real-world case — just that the core logic works.
- **It's small and fixed-seed, on purpose.** One week of data across
  five branches is enough to exercise every code path — each WAL type,
  the deposit fallback logic, the extrapolation warning — but it isn't
  representative of production scale or volume-driven edge cases.

## Disclaimer

Branch codes and product names (Home Loan, Call Account, and so on) are
generic retail/corporate banking terms, not identifying of any real
institution. Currency is a placeholder ("USD"). Every figure is randomly
generated with `numpy`'s seeded RNG — reproducible, not real.
