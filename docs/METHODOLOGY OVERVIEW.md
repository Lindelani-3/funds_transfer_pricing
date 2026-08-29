# FTP Framework — Methodology Overview

Every other document in `docs/` goes deep on one specific piece of this
framework — one curve, one WAL type, one calculation. This one is the
front door: the end-to-end story of what we built, why FTP exists in
the first place, and a map to everything else, for anyone starting cold.

## What FTP is actually for

A bank doesn't fund itself one loan at a time, and it doesn't take
deposits one account at a time either — Treasury sits in the middle,
pooling every source of funding and every use of funding into one
balance sheet. Funds Transfer Pricing is how we put a real, defensible
price on that pooling: every asset gets charged for the funding it
uses, every liability gets credited for the funding it provides, both
priced off the same internal curve. Without it, a business unit's
reported profitability depends heavily on how Treasury happened to fund
or invest that day — with it, profitability reflects the business
decision itself (what to lend, what deposits to gather, at what price),
cleanly separated from Treasury's own funding and investment choices.

We treat this as a genuinely strategic tool, not just an accounting
mechanism — see `docs/utilisation_of_funds_internal_transfer_pricing.md`
for why we built it as a zero-sum, double-entry system, and what that
design specifically buys us in terms of funding strategy and
profitability visibility.

## The three layers, in order

**1. We build a curve.** Every price in this framework traces back to
one term structure — a base rate (pure term risk, fitted from T-bills
and bonds) plus a liquidity premium (the extra cost of less-liquid
funding, fitted entirely from competitor/market deposit rates — we
don't feed our own quoted rate into this model). See
`docs/base_curve_construction.md`, `docs/liquidity_premium_construction.md`,
and `docs/full_ftp_rate.md` for how we build it and why we built it as
two separate fits rather than one.

**2. We decide how long each account's money actually stays.** A curve
is only useful once we know which point on it applies to a given
account — and that's rarely as simple as reading a maturity date. We
route every account through one of three behavioural WAL approaches
(contractual, persistence/structural-split, or prepayment-adjusted)
based on how that specific product actually behaves. See
`docs/behavioural_wal.md`, `docs/persistence_structural_split_wal.md`,
`docs/prepayment_wal.md`, and `docs/behavioural_wal_assumptions.md` for
the full walkthrough and the reasoning behind it.

**3. We turn a rate into money.** Once an account has a curve point, we
compute what it actually costs or earns — a daily charge, a daily
credit, and a realised margin against the account's own real observed
rate — then roll all of that up from daily through weekly, monthly, and
segment-level consolidation. See
`docs/ftp_charges_credits_realised_margin.md` for the mechanics.

## Where the numbers come from

We draw on three broad categories of input, kept deliberately separate
so each can be validated on its own terms:

- **Market data** — T-bill auctions, bond yields, and comparator deposit
  rates, feeding the curve itself.
- **Internal balance and rate data** — the actual book: balances, GL
  classifications, and observed rates, feeding every account-level
  calculation.
- **Behavioural reference data** — structural-split percentages,
  tenor-class WALs, and prepayment adjustments, all maintained as their
  own reference sheets rather than hardcoded, so they can be recalibrated
  against real observed behaviour without touching the model itself.

See `demo_data/SYNTHETIC_DATA_GENERATOR.md` for the concrete shape of
every one of these inputs, reproduced synthetically.

## Principles we build the governance around

We didn't design this as a model that runs once and is trusted forever.
A few principles we treat as non-negotiable in any real implementation:

- **Someone owns the model, specifically** — not "the team," a named
  function accountable for its assumptions and its outputs.
- **Curve changes are deliberate, not incidental** — a curve used for
  live pricing shouldn't shift mid-period as a side effect of a
  parameter tweak elsewhere; changes get reviewed and locked in on a
  known cadence.
- **Every override is traceable** — anywhere the model's own output gets
  manually corrected, that correction is recorded with a reason, not
  applied silently (see `docs/behavioural_wal_assumptions.md` for where
  this applies inside the WAL cascade specifically).
- **Oversight sits above the model, not inside it** — a body like ALCO
  (or an equivalent asset-liability governance function) reviews outputs
  and assumptions periodically; the model produces numbers, it doesn't
  self-certify them.

## Map of the documentation

| Layer | How it works | Why we built it that way |
|---|---|---|
| Base curve | `curve_model/CURVE_CONSTRUCTION_ENGINE.md`, `docs/base_curve_construction.md` | `docs/full_ftp_rate.md` |
| Liquidity premium | `curve_model/CURVE_CONSTRUCTION_ENGINE.md`, `docs/liquidity_premium_construction.md` | `docs/full_ftp_rate.md` |
| Behavioural WAL (all types) | `docs/behavioural_wal.md` | `docs/behavioural_wal_assumptions.md` |
| — Persistence / structural split | `docs/persistence_structural_split_wal.md` | `docs/behavioural_wal_assumptions.md` |
| — Prepayment | `docs/prepayment_wal.md` | `docs/behavioural_wal_assumptions.md` |
| Charges, credits, realised margin | `docs/ftp_charges_credits_realised_margin.md` | `docs/utilisation_of_funds_internal_transfer_pricing.md` |
| Consolidation pipeline | `pipeline/PIPELINE_ARCHITECTURE.md` | — |
| Demo data | `demo_data/SYNTHETIC_DATA_GENERATOR.md` | — |

---

## References

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

### Further reading (industry practice)

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
