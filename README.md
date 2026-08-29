# FTP Framework — Curve Construction, Behavioural Pricing & Charge Consolidation

An end-to-end Funds Transfer Pricing (FTP) framework: a curve
construction engine, a behavioural WAL model, and an account-level
pricing and consolidation pipeline, built in Python. This repo is a
public, demo-data-only companion to a full FTP framework — everything
here either runs on synthetic data or documents architecture without
exposing production source (see **License & Legal** below for exactly
where that line sits and why).

> 📊 **[VISUAL: Full FTP Curve — Base Rate + Liquidity Premium, plotted
> against tenor]**

Even without the chart, the shape is easy to read from the numbers
themselves — a sample cut of the curve at a few illustrative tenor
points:

| Tenor | Base Rate | Liquidity Premium | Full FTP Rate |
|---|---|---|---|
| 3M (0.25y) | 5.1% | 0.7% | 5.8% |
| 1Y | 6.0% | 1.0% | 7.0% |
| 5Y | 6.8% | 1.1% | 7.9% |
| 10Y | 7.0% | 1.1% | 8.1% |
| 20Y | 7.0% | 1.1% | 8.1% |
| 30Y | 7.0% | 1.1% | 8.1% |

The base rate does almost all the work at the long end (it flattens, as
Nelson-Siegel curves do); the liquidity premium adds a smaller, steadier
markup across the whole curve rather than growing without bound —
exactly the shape the LP-specific weighting and bounds in
`curve_model/CURVE_CONSTRUCTION_ENGINE.md` are built to produce.

---

## The problem, in one page

Without an FTP framework, a bank is typically able to assess
profitability at the whole-book level — the balance sheet nets out, and
aggregate earnings are real — but can't properly allocate the cost of
funding down to individual segments, branches, or business units.
Treasury pools every source of funding and every use of funding into
one balance sheet; that pooling is efficient at the book level, but it
breaks the moment you ask a segment-level question, because there's no
honest way to say which segment's lending was funded by which
segment's deposits.

Left unresolved, that gap doesn't just create uncertainty — it actively
overstates or understates segment-level interest income and interest
expenditure, in whichever direction that segment's funding mix happens
to differ from the book average. A segment that happens to be funded by
disproportionately cheap deposits looks more profitable than it
actually is; a segment funded by disproportionately expensive wholesale
money looks weaker than it actually is. Roll that up, and it's not just
an internal reporting quirk — it's a distortion in headline segment
earnings, the numbers that actually drive decisions about where to
grow, where to reprice, and where a business line is genuinely adding
value versus riding a funding-mix accident.

A single blended "average cost of funds" can't fix this, because three
different kinds of product behave completely differently in ways an
average can't see at all:

- A **fixed-term deposit** has a real maturity date and genuinely
  behaves like one.
- A **call account** has no maturity date at all, but a meaningful
  share of the balance sits there for years, not days.
- A **home loan** has a real maturity date that overstates how long it
  actually stays on the book, because customers refinance and settle
  early.

On top of *how long*, there's a second question pooling also erases:
*which way the money moves*. A loan is drawing on the bank's funding; a
deposit is supplying it. Collapse both into one pooled number and you
lose that distinction too — you can no longer tell whether a business
unit is expensively borrowing from the pool or cheaply feeding it.

Price every product the same way, and ignore which direction the money
moves, and you misprice most of the balance sheet. That's the problem
this framework exists to solve — and the clearest way we've found to
think about the solution is as a line of train cars running along a
track. Every account's money needs a real place on that line, and
answering that honestly means answering two separate questions, not
one:

- **Which car is it in?** That's decided by *type of seating* — a
  fixed reservation (contractual), a split across several cars at once
  because the money doesn't all behave the same way (persistence), or a
  car closer to the front than the paperwork implies (prepayment).
- **Which cabin, within that car?** That's decided by *type of seat* —
  asset or liability. The car decides the price everyone in it pays;
  the cabin decides which direction that price actually moves — charged
  out, for an asset, or credited in, for a liability.

Until both are answered, we can't know what any account truly costs, or
what it's truly worth.

## The solution, end to end

Told simply, it's three moves, each building on the last:

1. **We lay the track — a curve that prices money at every tenor.**
2. **We work out which car each account's money rides in**, decided by
   its type of seating — contractual, persistence, or prepayment.
3. **We price the ride by car, and charge or credit by cabin** — asset
   or liability decides which direction the money moves.

### 1. We build a curve that prices money at every tenor

Every price in this framework traces back to one term structure, and we
build it as **two genuinely separate curves added together** rather
than one blended fit:

- A **base curve**, fitted with a Nelson-Siegel model — a parsimonious,
  four-parameter curve shape (level, slope, curvature, decay) —
  directly off T-bill auction data and government bond yields. No
  deposit data touches this fit at all. We chose Nelson-Siegel
  specifically because it's smooth and hard to overfit: an unstable,
  kinked curve creates internal pricing anomalies a business unit could
  structure around, and a technique with 35+ years of use in fixed
  income is one we can defend to an auditor or a board without asking
  anyone to trust a black box (Nelson & Siegel, 1987).
- A **liquidity premium curve**, fitted completely separately, off
  **competitor deposit rate data only** — never our own quoted rate.
  That's a deliberate choice: fitted this way, the curve prices a
  genuine, independently-observable *market* liquidity premium, not
  just whatever our own book happened to cost to fund that day. It's
  also why the data can get thin at the short end (competitor rate
  cards don't always publish every tenor) — a real limitation we handle
  with a capped, documented stress overlay rather than pretending a
  sparse fit is the final word.

We keep these two fits separate on purpose, not for elegance: term risk
and liquidity risk are genuinely different things a bank manages
differently, and a combined curve would hide how much of any price is
one versus the other. An international survey of large-bank transfer
pricing practice found exactly this failure mode widespread — banks
that priced liquidity as one blended average, rather than attributing
it separately from term cost, systematically under-penalised long-term
funding commitments and over-rewarded long-term funding benefits, which
in turn quietly encouraged more balance-sheet maturity transformation
than the bank's own risk appetite would otherwise support (Grant, 2011).

*Deep dive: `curve_model/CURVE_CONSTRUCTION_ENGINE.md` for the code,
`docs/base_curve_construction.md` and
`docs/liquidity_premium_construction.md` for the full data walkthrough,
`docs/full_ftp_rate.md` for every design decision behind this layer.*

### 2. We work out which car each account's money rides in

The track is only a price list until we know which car on it a given
account actually rides in — and, as the problem above shows, that's
rarely as simple as "read the maturity date." Some money has a reserved
car from day one. Some money has no reservation at all, and we have to
work out from its own behaviour which car it really belongs in. And
some money looks like it's booked for the back of the train, but walks
out early far more often than the contract suggests. We route every
account through one of three **types of seating**, based on how that
specific product actually behaves — a question that's closely linked
to, but not quite the same as, *which cabin* it rides in (asset or
liability), which we come back to in step 3:

**Contractual — a fixed, reserved car.** A real, reliable maturity date
exists and predicts behaviour, so we put the account in exactly the car
that date says and leave it there, no adjustment. This is also the
regulator's own position: the Basel Committee's IRRBB standard treats
non-maturity deposit repricing and loan prepayment as two *separate*
disclosure requirements, precisely because they're different modelling
problems that a maturity-date passthrough can't solve (Basel Committee
on Banking Supervision [BCBS368], 2016).

**Persistence (structural split) — the same cabin, riding in four cars
at once.** No maturity date exists, but the balance clearly doesn't all
leave at once either. Rather than guessing a single car for the whole
balance, we let the account ride in four cars simultaneously —
overnight, short, medium, and long term — using the Minimum Balance
Method, and price each car separately. It's genuinely one account split
across four real positions on the track at the same time, not four
different accounts — and every one of those four cars carries the
account in the *same cabin* (asset or liability), since it's still the
same balance, just fractioned by how long each part of it actually
tends to stay:

> 📊 **[VISUAL: One account split into four tranches — bar chart of
> balance by tranche, WAL labelled]**

| Tranche | Balance | Share of account | WAL |
|---|---|---|---|
| Overnight | 90,000 | 18% | 0.0027 yrs |
| Short Term | 110,000 | 22% | 0.62 yrs |
| Medium Term | 175,000 | 35% | 1.85 yrs |
| Long Term | 125,000 | 25% | 4.20 yrs |
| **Total** | **500,000** | **100%** | — |

This is a real simplification (four discrete buckets standing in for a
continuous persistence distribution), but a defensible one: coarse
enough to calibrate reliably, fine enough to separate genuinely volatile
balance from genuinely core balance. The technique has its own dedicated
academic literature for exactly this reason — early work built
arbitrage-free frameworks for valuing demand deposits as a blend of a
market-linked component and a genuinely sticky core (Jarrow & Van
Deventer, 1998), later extended into a full stochastic three-factor
model treating deposit volumes, deposit rates, and market rates as
separate risk drivers (Kalkbrener & Willing, 2004).

**Prepayment — a car closer to the front than the paperwork says.** A
real maturity date exists but systematically overstates actual life,
because customers refinance and settle early. We look up an
already-adjusted figure from a dedicated reference sheet:

> 📊 **[VISUAL: Contractual WAL vs. Prepayment-Adjusted WAL — bar chart
> showing the gap]**

| | Contractual WAL (raw maturity schedule) | Prepayment-Adjusted WAL (what we actually price) |
|---|---|---|
| HOME001 | 12.4 years | 7.85 years |
| Implied early runoff | — | 4.55 years |

That gap — 12.4 contractual years against a 7.85-year adjusted figure —
isn't a rounding choice, it's the quantified cost of ignoring real
prepayment behaviour. This correction isn't optional, either: for any
loan that permits prepayment, weighted average life *cannot* be computed
from the amortization schedule alone. A prepayment assumption is a
required input to the calculation, not an enhancement to it.

For persistence and prepayment rows specifically, we also allow a
manual, sheet-driven override — a deliberate, traceable correction for a
specific account where we know the modelled figure is wrong for an
identifiable reason. We never route contractual rows through this, since
that would just duplicate the contractual passthrough one step earlier.
This mirrors standard model-risk practice: supervisory guidance on model
risk management explicitly calls for "effective challenge" of models —
documented, critical analysis able to catch and correct model
limitations, not blind trust in the output (Board of Governors of the
Federal Reserve System & Office of the Comptroller of the Currency [SR
11-7], 2011).

The three types of seating, side by side:

| | Contractual | Persistence (structural split) | Prepayment |
|---|---|---|---|
| **Applies to** | Fixed-term deposits | Call/current accounts, notice deposits | Home loans, business loans |
| **Maturity date?** | Real and trusted | Doesn't exist | Real but overstates life |
| **Adjustment applied** | None — passthrough | Split into 4 behavioural tranches | Adjusted via reference lookup |
| **Output shape** | 1 row, 1 WAL | Up to 4 rows, 4 WALs | 1 row, 1 (shorter) WAL |
| **Manual override available?** | No | Yes | Yes |
| **Example (this doc)** | TERM001 — 2.00 yrs | CALL001 — 4 tranches | HOME001 — 12.4 → 7.85 yrs |

*Deep dive: `docs/behavioural_wal.md` for the full walkthrough,
`docs/persistence_structural_split_wal.md` and `docs/prepayment_wal.md`
for the code-level mechanics, `docs/behavioural_wal_assumptions.md` for
every design decision behind this layer.*

### 3. We price by car, charge or credit by cabin

Once we know which car an account's money rides in (step 2 — its type
of seating), one question is still open: which cabin is it riding in?
Every cabin in a given car pays exactly the same price — the car alone
decides that — but which cabin decides which direction the money
actually moves:

- An **asset** (a loan), red cabin, is *using* funding — it's riding on
  money the bank had to source from somewhere else — so it gets
  **charged** that car's rate.
- A **liability** (a deposit), blue cabin, is *providing* funding —
  it's the money the bank sourced in the first place — so it gets
  **credited** that car's rate.

In practice, the two cabins line up closely with type of seating, but
not because anything forces them to. Structural split is *practically
always* blue cabin — call accounts, current accounts, notice deposits
are liabilities almost by definition — and prepayment is *practically
always* red cabin — home loans and business loans are assets almost by
definition. That's a real pattern in how products actually get
classified, not a rule the model enforces; nothing stops an asset from
being routed through structural split if a product genuinely needed it.
And where that pattern does bend, cabin can matter more than we let on
above: for structural-split tranches specifically, the reference lookup
for each car's own WAL is keyed on cabin too, so the same tenor
classification can carry a genuinely different WAL for an asset than for
a liability. So cabin isn't purely "same car, different direction of
payment" in every case — for that one type of seating, it can shift
*which car* gets picked in the first place. Worth knowing, even though
it's the exception rather than the rule.

Cabin aside, this is what makes the mechanism a genuine internal market
for funds rather than a one-directional cost allocation. Split by
component, not just as one blended number:

```
BASE_CHARGE  = balance × Base Rate  × (1/365)
LP_CHARGE    = balance × LP         × (1/365)
FTP_CHARGE   = balance × Full FTP   × (1/365)   (= BASE_CHARGE + LP_CHARGE, by construction)
PRICING_GAP  = Observed Rate − Full FTP Rate
MARGIN       = balance × PRICING_GAP × (1/365)
```

For an asset, `FTP_CHARGE` is money flowing *out* of the business unit
to Treasury — the cost of the car it's riding in. For a liability,
that same calculation is money flowing *in* from Treasury — the value
of the funding it's providing by riding there. `PRICING_GAP` and
`MARGIN` flip meaning the same way: for a loan, it's "am I earning more
from the customer than my car costs me"; for a deposit, it's "am I
paying the customer less than my car is actually worth to the bank."

We designed this as a genuine **double-entry, zero-sum mechanism**:
Treasury sits as the mirror counterparty to every charge or credit — the
exact amount charged to every asset's car is, in aggregate, the exact
amount credited to every liability's car. A perfectly matched book (an
asset riding in the same car as the liability funding it) nets to
exactly zero, by construction. A real, non-zero net position across the
whole bank is expected, not a defect — it's the quantified cost or
benefit of real maturity transformation: assets and liabilities riding
in genuinely different cars. That's a fundamentally different thing
from a non-zero position caused by a data gap (a product that simply
never reached FTP pricing, so it never boarded at all), and telling the
two apart is a reconciliation exercise the framework is built to
support, not something the pricing formulas resolve by themselves. We
also recompute weighted averages fresh at every rollup level (daily →
weekly → monthly → segment) from that period's own totals, rather than
averaging already-averaged daily figures — the two aren't
mathematically equivalent once balances move within a period, and
getting this wrong introduces a systematic bias at exactly the level
most business decisions actually get made from.

*Deep dive: `docs/ftp_charges_credits_realised_margin.md` for the
mechanics, `docs/utilisation_of_funds_internal_transfer_pricing.md` for
every design decision behind this layer.*

## Walking one account through, start to finish

Take `HOME001` — a home loan, R2,000,000 balance, 12.4-year contractual
maturity.

1. **WAL**: it's a `PREPAYMENT`-type product, so we don't use 12.4 years
   — we look up the prepayment-adjusted figure, **7.85 years**.
2. **Curve lookup**: at 7.85 years, we read off the base and liquidity
   premium curves independently, then add them.
3. **Charge**: each rate component gets its own daily accrual charge.
4. **Margin**: the account's own observed customer rate, compared
   against the full FTP rate, gives the realised margin.
5. **Rollup**: that daily figure feeds into HOME001's branch's weekly,
   monthly, and segment consolidation — recomputed from each period's
   own totals, not averaged from the daily numbers directly.

The same walk-through as a ledger — every field HOME001 actually
carries by the time it reaches consolidation:

| Field | Value | How we got it |
|---|---|---|
| `OUT_BAL` | R2,000,000 | Real balance, from the balance extract |
| `CURRENT_AVG_CONTRACTUAL_WAL` | 12.4 yrs | Raw amortization schedule |
| `CHOSEN_WAL` | 7.85 yrs | Prepayment-adjusted lookup, not the contractual figure |
| `BASE_RATE` | ~6.8% | Base curve, read at 7.85 yrs |
| `LP` | ~1.1% | Liquidity premium curve, read at 7.85 yrs |
| `FTP_RATE` | ~7.9% | `BASE_RATE + LP` |
| `OBS_RATE` | 9.5% | The loan's own real observed customer rate |
| `PRICING_GAP` | 1.6% | `OBS_RATE − FTP_RATE` |
| `BASE_CHARGE_DAILY_ACCRUAL` | ≈ R373 | `OUT_BAL × BASE_RATE × 1/365` |
| `LP_CHARGE_DAILY_ACCRUAL` | ≈ R60 | `OUT_BAL × LP × 1/365` |
| `FTP_CHARGE_DAILY_ACCRUAL` | ≈ R433 | `BASE_CHARGE + LP_CHARGE` |
| `DAILY_PRODUCT_MARGIN_VALUE` | ≈ R88 | `OUT_BAL × PRICING_GAP × 1/365` |

Because HOME001 is an **asset**, riding in the red cabin, every one of
those charge figures is money flowing *out* of the branch that booked
it, to Treasury — the price of the car it's riding in.

Now the mirror image: `CALL001`'s Long Term tranche (from the structural
split example above) — a **liability**, blue cabin, R125,000, riding in
the car for a WAL of 4.20 years:

| Field | Value | How we got it |
|---|---|---|
| `TRANCHE_OUT_BAL` | R125,000 | This tranche's share of the account |
| `CHOSEN_WAL` | 4.20 yrs | Tenor-class WAL lookup for the Long Term tranche |
| `FTP_RATE` | ~7.5% | Curve read at 4.20 yrs |
| `FTP_CHARGE_DAILY_ACCRUAL` | ≈ R25.68 | `TRANCHE_OUT_BAL × FTP_RATE × 1/365` |

Same formula, same mechanism — but because this tranche rides in the
**liability** cabin, that ≈R25.68 is money flowing *in* from Treasury to
the branch, not out. It's the value of the funding this tranche of the
account provides, priced at exactly the same rate an asset riding in
the same car would be charged. That symmetry — one track, one car
lookup, one formula, direction determined purely by cabin — is what
makes the whole mechanism a genuine internal market rather than a
one-way cost.

One account, twelve fields, and a clear answer to "is this loan actually
profitable once it's honestly charged for its own funding" — which is
the entire point of the exercise.


## Repository structure

```
curve_model/    Full source — the curve construction engine (base + LP)
pipeline/       Architecture only — the consolidation & pricing engine
demo_data/      Full source — synthetic data generator (zero real data)
docs/           Every "how" and "why" explainer, plus this methodology overview
assets/         Placeholder spot for charts (data tables carry the visuals for now)
```

| Layer | How it's built | Why we built it that way |
|---|---|---|
| Base curve | `curve_model/CURVE_CONSTRUCTION_ENGINE.md`, `docs/base_curve_construction.md` | `docs/full_ftp_rate.md` |
| Liquidity premium | `curve_model/CURVE_CONSTRUCTION_ENGINE.md`, `docs/liquidity_premium_construction.md` | `docs/full_ftp_rate.md` |
| Behavioural WAL (all types) | `docs/behavioural_wal.md` | `docs/behavioural_wal_assumptions.md` |
| — Persistence / structural split | `docs/persistence_structural_split_wal.md` | `docs/behavioural_wal_assumptions.md` |
| — Prepayment | `docs/prepayment_wal.md` | `docs/behavioural_wal_assumptions.md` |
| Charges, credits, margin | `docs/ftp_charges_credits_realised_margin.md` | `docs/utilisation_of_funds_internal_transfer_pricing.md` |
| Consolidation pipeline | `pipeline/PIPELINE_ARCHITECTURE.md` | — |
| Demo data | `demo_data/SYNTHETIC_DATA_GENERATOR.md` | — |
| Full methodology map | `docs/methodology_overview.md` | — |

## Getting started

```bash
pip install -r requirements.txt
python demo_data/generate_dummy_data.py        # builds synthetic inputs, zero real data
jupyter notebook curve_model/01_curve_construction_demo.ipynb
```

The notebook runs the curve engine end to end against the synthetic
data and reproduces (illustratively) the kind of output shown in the
chart at the top of this document.

## Where this framework has real limits

Briefly — each is covered in depth in its own doc:

- **Liquidity premium data is inherently thinner than the T-bill/bond
  universe**, and fitting it purely off competitor deposit data (never
  our own book) means the resulting curve is a market-observed
  liquidity premium, not a direct measure of our own funding cost —
  worth being explicit about wherever it's used (`docs/liquidity_premium_construction.md`).
- **Every layer is bounded by upstream data quality** — we surface gaps
  and warnings, we don't silently correct for them
  (`pipeline/PIPELINE_ARCHITECTURE.md`, `curve_model/CURVE_CONSTRUCTION_ENGINE.md`).
- **Zero-sum is a design target, not a guarantee** — genuine tenor
  mismatch, data-mapping gaps, and classification issues all produce
  non-zero net positions, and each needs a different diagnosis, not one
  blanket explanation (`docs/utilisation_of_funds_internal_transfer_pricing.md`).
- **Reference data (structural split %, tenor-class WAL, prepayment
  adjustments) needs active maintenance** — a new or reclassified
  product prices sensibly only once its reference data catches up
  (`docs/persistence_structural_split_wal.md`, `docs/prepayment_wal.md`).

## References

**Academic & regulatory sources**

Basel Committee on Banking Supervision. (2013). *Basel III: The
liquidity coverage ratio and liquidity risk monitoring tools* (BCBS238).
Bank for International Settlements. https://www.bis.org/publ/bcbs238.pdf

Basel Committee on Banking Supervision. (2016). *Standards: Interest
rate risk in the banking book* (BCBS368). Bank for International
Settlements. https://www.bis.org/bcbs/publ/d368.pdf

Board of Governors of the Federal Reserve System, & Office of the
Comptroller of the Currency. (2011). *Supervisory guidance on model
risk management* (SR 11-7 / OCC Bulletin 2011-12).
https://www.federalreserve.gov/boarddocs/srletters/2011/sr1107.pdf

Grant, J. (2011). *Liquidity transfer pricing: A guide to better
practice* (FSI Occasional Paper No. 10). Financial Stability Institute,
Bank for International Settlements. https://www.bis.org/fsi/fsipapers10.pdf

Jarrow, R. A., & Van Deventer, D. R. (1998). The arbitrage-free
valuation and hedging of demand deposits and credit card loans.
*Journal of Banking and Finance, 22*(3), 249–272.

Kalkbrener, M., & Willing, J. (2004). Risk management of non-maturing
liabilities. *Journal of Banking & Finance, 28*(7), 1547–1568.

Nelson, C. R., & Siegel, A. F. (1987). Parsimonious modeling of yield
curves. *The Journal of Business, 60*(4), 473–489.

**Industry practice (further reading)**

Oracle. *Understanding funds transfer pricing rules* (PeopleSoft
Enterprise Performance Management documentation).
https://docs.oracle.com/cd/E41507_01/epm91pbr3/eng/epm/pftp/concept_UnderstandingFundsTransferPricingRules-399c7c.html

ElysianNxt. *How do banks model behavioral assumptions for IRRBB
calculations?*
https://www.elysiannxt.com/how-do-banks-model-behavioral-assumptions-for-irrbb-calculations/

KPMG International. *IRRBB series: Prepayment modelling.*
https://assets.kpmg.com/content/dam/kpmgsites/ie/pdf/insights/banking/ie-prepayment-modelling.pdf.coredownload.inline.pdf

CostPerform. *What is funds transfer pricing in banking?*
https://www.costperform.com/what-is-funds-transfer-pricing-a-complete-guide-for-banks/

Wikipedia. *Weighted-average life.*
https://en.wikipedia.org/wiki/Weighted-average_life

## License & legal

**This is an independent, personal portfolio project**, built to
demonstrate FTP methodology and software design skill — not an official
publication of any employer, and not a disclosure of any employer's
proprietary implementation, internal data, or trade secrets. This
project has been reviewed and cleared for public release by the
author's line manager. A few things worth being explicit about:

- **The problem statement is a generic industry framing, not a claim
  about any specific institution.** The "problem, in one page" section
  above describes what happens at any bank that lacks an FTP
  framework — it's standard motivation for why FTP exists as a
  discipline, not a statement that a named employer's headline earnings
  are currently overstated or that any specific institution has this
  gap today. No figures, findings, or institution-specific claims are
  made or implied anywhere in this repo.

- **No real data anywhere.** Every input in this repo — market data,
  balances, rates, account numbers — is synthetically generated with a
  fixed random seed (see `demo_data/SYNTHETIC_DATA_GENERATOR.md`).
  Nothing here is drawn from, derived from, or resembles any real
  institution's actual figures.
- **Tiered code exposure, deliberately.** `curve_model/` and
  `demo_data/` are full, runnable source — the curve-fitting techniques
  involved (Nelson-Siegel, liquidity premium decomposition) are
  established, publicly-documented quantitative finance methods, not
  proprietary to any one institution. `pipeline/` is architecture and
  methodology documentation only — the consolidation and classification
  logic encodes substantial real business-rule design, and full source
  for that layer is intentionally not published here.
- **Not a statement of any employer's actual methodology, findings, or
  positions.** Where this documentation references real regulatory
  standards or academic literature, that's this author's own synthesis
  for explanatory purposes — not a disclosure of any institution's
  internal policy, model parameters, or results.
- **License.** The code in this repository (`curve_model/`, `pipeline/`,
  `demo_data/`) is released under the MIT License — see `LICENSE`. Free
  to use, modify, and redistribute, with attribution and without
  warranty. The written content in `docs/` and this README represents
  the author's own analysis and is offered for reference in the same
  spirit; please attribute if you reuse it substantially.

If anything here needs to come down or be corrected, that's a one-line
request away — this repo exists to demonstrate work fairly and
accurately, not to overstate what's public domain versus proprietary.
