# The Curve Construction Engine

Every FTP framework needs a real, defensible answer to one question:
what does it actually cost to fund money at every possible tenor,
today? This is the engine we built to answer it — an object-oriented
Python model that fits a base yield curve and a liquidity premium curve
from market data, then combines them into one term structure.

Think of what this module builds as the track itself — the line every
account eventually gets seated on. It doesn't decide *where* on that
track an account sits (that's the WAL/seating-type logic in
`../pipeline/`) or *which cabin* it rides in (asset or liability, also
decided in the pipeline) — it just builds the track, and prices every
point on it. See the top-level `README.md` for the full seating
narrative this engine is the foundation of.

## How we build it

```
Treasury bills + Government bonds ─► fit Nelson-Siegel curve ─► Base Rate ──┐
                                                                              ├─► + ─► Full FTP Rate
Deposit rate comparisons (competitor banks) ─► fit Nelson-Siegel curve ─► Liquidity Premium ─┘
```

We fit two entirely independent inputs into two entirely separate
curves, never one combined fit. T-bills and bonds get fitted directly
into the base curve's shape — no deposit data mixed into that fit at
all. Deposit rate comparisons across competitor banks get fitted
completely separately into their own curve, the liquidity premium — see
`docs/liquidity_premium_construction.md` for why we only ever use
competitor/market deposit data here, never our own quoted rate. We then
add the two fitted curves together to get the Full FTP Rate.

Deposits get one more, optional role on top of that: a 3-month spread
shift that can nudge the *base* curve, off by default, available for
cases where we specifically want a short-end deposit anchor on the base
side. That's a separate, secondary use of the same deposit data — it
doesn't change how the liquidity premium curve itself gets built.

## What's inside

- `ftp_curve_model.py` — the library module we import from everywhere
  else: `CurveConfig` (our scenario/stress parameters — parallel shifts,
  the optional 3-month spread-shift toggle, off by default), `DataObject`
  (loading, cleaning, bucketing), `CurveModelObject` (fits both the base
  curve and the liquidity premium curve, each its own Nelson-Siegel fit,
  plus the optional shift and the final combine step), `ExportObject`
  (our Excel reporting layer), and `run_scenario_suite()` /
  `compare_scenarios()` for running and comparing several stress
  scenarios in one pass.
- `01_curve_construction_demo.ipynb` — the notebook we actually run: a
  full end-to-end pass against the demo data in `../demo_data/`, plus a
  worked scenario-comparison example.

## Running it yourself

1. Generate demo inputs first — see `../demo_data/SYNTHETIC_DATA_GENERATOR.md`.
2. Open `01_curve_construction_demo.ipynb` and run every cell. It
   imports straight from `ftp_curve_model.py`, so there's nothing else
   to wire up.
3. What comes out: fitted curve parameters for both curves
   (Nelson-Siegel β₀/β₁/β₂/τ, one set for the base curve and a separate
   set for the liquidity premium), fit diagnostics (SSE/RMSE) for each,
   and Excel/plot exports — the base curve, the liquidity premium curve,
   and the combined Full FTP Rate — to whatever output directory you
   configure.

## Where we hit real limits

- **Liquidity premium data gets thin, fast.** We fit the LP curve off
  competitor deposit-rate data (we don't feed our own quoted rate into
  this model at all — see `docs/liquidity_premium_construction.md` for
  what that means for how the resulting curve should be read), which is
  typically much thinner than the T-bill/bond universe feeding the base
  curve — especially at the short end, and especially now that the
  3-month shift is off by default, since there's no short-end anchor
  pulling the base curve toward observed deposit rates before the LP
  spread gets computed. We handle this by exposing stressed LP
  parameters as a configurable input rather than trusting one
  sparse-data fit as the final word — run the stressed scenario
  alongside the base case and compare them.
- **We're only as good as the market data we're fed.** We surface
  problems — extrapolation-beyond-range warnings, monotonicity checks —
  but we can't correct for missing or erroneous source data ourselves.
  Curve quality is bounded by how complete the T-bill, bond, and deposit
  data actually is on any given day.
- **Nelson-Siegel assumes a shape.** The functional form (level, slope,
  curvature, hump) can underfit an unusual or multi-humped real curve at
  the extremes. Our fit diagnostics will flag that; they won't fix it.

## Dependencies

`pandas`, `numpy`, `scipy` (optimization + interpolation), `scikit-learn`
(isotonic regression), `python-dateutil`, `openpyxl`, `matplotlib`.

## A note on the technique

Nelson-Siegel curve fitting and liquidity-premium construction aren't
something we invented — they're established quantitative finance
techniques. See `../docs/` for the underlying math and the reasoning
behind each modeling choice we made on top of them: why the 3-month
anchor, why deposits shift rather than get fitted, and more.
