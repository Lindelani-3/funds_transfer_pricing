"""
pipeline/sample_snippets.py

Illustrative, self-contained re-implementations of a handful of
techniques from the real consolidation pipeline (see
PIPELINE_ARCHITECTURE.md) -- simplified for clarity, not the full
production functions. The real pipeline's product classification,
multi-source (naretail/ABF) reconciliation, and edge-case handling are
intentionally not published here; see the top-level README's "License
& legal" section for why.

These six functions walk the same cascade documented across docs/ --
type of seating (contractual/structural-split/prepayment), type of seat
(asset/liability), and the resulting charge/credit/margin -- and are
built to run against the exact TERM001 / CALL001 / HOME001 examples
used throughout this repo's documentation, so the numbers below match
what you've already seen in the README and docs/behavioural_wal.md.

Run directly for a worked demo of all six:

    python sample_snippets.py
"""

import numpy as np
import pandas as pd


# --------------------------------------------------------------------- #
# 1. Contractual -- the account speaks for itself
# --------------------------------------------------------------------- #

def contractual_wal(contractual_tenor: pd.Series, manual_override: pd.Series) -> pd.Series:
    """
    CALCULATED_WAL for CONTRACTUAL-type accounts: the real tenor,
    passed straight through. A missing tenor first checks a manual
    entry (a known contractual fact filling a real data gap, not a
    behavioural estimate); only falls back to a 1/365-year floor if
    neither exists -- keeps a data gap from silently becoming a
    zero-WAL account. See docs/behavioural_wal.md.
    """
    filled = contractual_tenor.fillna(manual_override)
    return filled.fillna(1 / 365)


# --------------------------------------------------------------------- #
# 2. Type of seating -- WAL_TYPE routing
# --------------------------------------------------------------------- #

def derive_wal_type(alco_class: pd.Series, wal_rules: pd.DataFrame) -> pd.Series:
    """
    Looks up WAL_TYPE (CONTRACTUAL / STRUCTURAL SPLIT / PREPAYMENT) per
    account from a reference table keyed on ALCO_CLASS. Any class not
    found in `wal_rules` defaults to CONTRACTUAL -- the most
    conservative fallback: a plain passthrough of contractual tenor, no
    behavioural calculation attempted for a product with no
    applicability information on file. See docs/behavioural_wal.md.
    """
    lookup = wal_rules.set_index("ALCO_CLASS")["WAL_TYPE"]
    return alco_class.map(lookup).fillna("CONTRACTUAL")


# --------------------------------------------------------------------- #
# 3. Type of seat -- asset vs. liability (the "cabin")
# --------------------------------------------------------------------- #

def derive_asset_liability(gl_line: pd.Series) -> pd.Series:
    """
    ASSET if the GL line starts with '1', LIABILITY otherwise -- the
    same convention used everywhere in the real pipeline. Decides which
    direction FTP charges flow in compute_ftp_charges() below. This is
    a separate axis from WAL_TYPE (see the README's "type of seating vs
    type of seat" section) -- correlated with it in practice, but not
    derived from it.
    """
    return np.where(gl_line.astype(str).str.startswith("1"), "ASSET", "LIABILITY")


# --------------------------------------------------------------------- #
# 4. Structural split -- one account, four seats at once
# --------------------------------------------------------------------- #

TRANCHE_PCT_COLS = {
    "OVERNIGHT": "OVERNIGHT_PCT",
    "SHORT TERM": "SHORT_PCT",
    "MEDIUM TERM": "MEDIUM_PCT",
    "LONG TERM": "LONG_PCT",
}


def expand_structural_split_tranches(
    account: pd.Series, split_pct: pd.Series, tenor_class_wal: pd.DataFrame
) -> pd.DataFrame:
    """
    Splits one STRUCTURAL-SPLIT account into up to four tranche rows.
    `split_pct` is that account's own row from a branch/ALCO-class-keyed
    split-percentage table; `tenor_class_wal` supplies each tranche's
    own WAL, keyed on (tenor classification, type) -- see
    docs/persistence_structural_split_wal.md for why TYPE is part of
    that key, not just tenor classification. The account's real,
    full-balance `OUT_BAL` is deliberately never touched here -- only
    `TRANCHE_OUT_BAL` varies per row.
    """
    rows = []
    for tenor_class, pct_col in TRANCHE_PCT_COLS.items():
        pct = split_pct.get(pct_col, 0.0)
        wal_row = tenor_class_wal[
            (tenor_class_wal["TENOR_CLASSIFICATION"] == tenor_class)
            & (tenor_class_wal["TYPE"] == account["TYPE"])
        ]
        wal = wal_row["FINAL_WAL"].iloc[0] if len(wal_row) else np.nan
        rows.append(
            {
                "ACC_NO": account["ACC_NO"],
                "TENOR_CLASSIFICATION": tenor_class,
                "TRANCHE_OUT_BAL": account["OUT_BAL"] * pct,
                "CALCULATED_WAL": wal,
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- #
# 5. Prepayment WAL -- two-tier lookup
# --------------------------------------------------------------------- #

def lookup_prepayment_wal(ac_cat: str, alco_class: str, prepayment_ref: pd.DataFrame) -> float:
    """
    Tier 1: exact AC_CAT match. Tier 2 (only if tier 1 misses): mean
    FINAL_PREPAYMENT_ADJUSTED_WAL across every AC_CAT sharing the same
    ALCO_CLASS -- a genuine broader aggregate, not a first-match guess.
    See docs/prepayment_wal.md for why this is its own dedicated tier
    rather than built on a generic value-substitution fallback.
    """
    exact = prepayment_ref[prepayment_ref["AC_CAT"] == ac_cat]
    if len(exact):
        return exact["FINAL_PREPAYMENT_ADJUSTED_WAL"].iloc[0]

    broader = prepayment_ref[prepayment_ref["ALCO_CLASS"] == alco_class]
    if len(broader):
        return broader["FINAL_PREPAYMENT_ADJUSTED_WAL"].mean()

    return np.nan


# --------------------------------------------------------------------- #
# 6. Charging (or crediting) each seat
# --------------------------------------------------------------------- #

def compute_ftp_charges(df: pd.DataFrame) -> pd.DataFrame:
    """
    BASE_CHARGE / LP_CHARGE / FTP_CHARGE and realised daily margin, per
    row. The formulas themselves are identical for assets and
    liabilities -- ASSET_LIABILITY (see derive_asset_liability) doesn't
    change the arithmetic, just how the resulting FTP_CHARGE gets read:
    money owed BY an asset's seat, money owed TO a liability's seat.
    See docs/ftp_charges_credits_realised_margin.md.
    """
    df = df.copy()
    df["FTP_RATE"] = df["BASE_RATE"] + df["LP"]
    df["PRICING_GAP"] = np.where(
        df["OBS_RATE"].notna() & (df["OBS_RATE"] != 0),
        df["OBS_RATE"] - df["FTP_RATE"],
        np.nan,
    )
    df["BASE_CHARGE_DAILY_ACCRUAL"] = df["TRANCHE_OUT_BAL"] * df["BASE_RATE"] / 365
    df["LP_CHARGE_DAILY_ACCRUAL"] = df["TRANCHE_OUT_BAL"] * df["LP"] / 365
    df["FTP_CHARGE_DAILY_ACCRUAL"] = df["TRANCHE_OUT_BAL"] * df["FTP_RATE"] / 365
    df["DAILY_PRODUCT_MARGIN_VALUE"] = np.where(
        df["PRICING_GAP"].notna(),
        df["TRANCHE_OUT_BAL"].abs() * df["PRICING_GAP"] / 365,
        np.nan,
    )
    return df


# ======================================================================= #
# Demo -- reproduces TERM001 / CALL001 / HOME001 from docs/behavioural_wal.md
# ======================================================================= #

if __name__ == "__main__":
    pd.set_option("display.float_format", lambda v: f"{v:,.4f}")

    print("=== 1 & 2. TERM001 -- Contractual seating ===")
    wal_rules = pd.DataFrame(
        {"ALCO_CLASS": ["CALL_LIAB", "HOME_LOAN"], "WAL_TYPE": ["STRUCTURAL SPLIT", "PREPAYMENT"]}
    )
    term001_class = pd.Series(["TERM_DEPOSIT"])  # not in wal_rules -> defaults to CONTRACTUAL
    print("WAL_TYPE:", derive_wal_type(term001_class, wal_rules).iloc[0])
    term001_wal = contractual_wal(pd.Series([2.00]), pd.Series([np.nan]))
    print("CALCULATED_WAL:", term001_wal.iloc[0], "\n")

    print("=== 3 & 4. CALL001 -- Structural split (liability cabin) ===")
    call001 = pd.Series({"ACC_NO": "CALL001", "OUT_BAL": 500_000.0, "TYPE": "LIABILITY"})
    split_pct = pd.Series(
        {"OVERNIGHT_PCT": 0.18, "SHORT_PCT": 0.22, "MEDIUM_PCT": 0.35, "LONG_PCT": 0.25}
    )
    tenor_class_wal = pd.DataFrame(
        {
            "TENOR_CLASSIFICATION": ["OVERNIGHT", "SHORT TERM", "MEDIUM TERM", "LONG TERM"],
            "TYPE": ["LIABILITY"] * 4,
            "FINAL_WAL": [0.0027, 0.62, 1.85, 4.20],
        }
    )
    cabin = derive_asset_liability(pd.Series(["2001"]))[0]
    print(f"GL line '2001' -> {cabin} (doesn't start with '1')")
    print(expand_structural_split_tranches(call001, split_pct, tenor_class_wal).to_string(index=False), "\n")

    print("=== 5. HOME001 -- Prepayment (asset cabin) ===")
    prepayment_ref = pd.DataFrame(
        {"AC_CAT": ["HOME_LOAN_STD"], "ALCO_CLASS": ["HOME_LOAN"], "FINAL_PREPAYMENT_ADJUSTED_WAL": [7.85]}
    )
    home001_wal = lookup_prepayment_wal("HOME_LOAN_STD", "HOME_LOAN", prepayment_ref)
    print(f"Contractual WAL: 12.4 yrs  ->  CALCULATED_WAL: {home001_wal} yrs\n")

    print("=== 6. HOME001 -- Charges, credits, and realised margin ===")
    home001 = pd.DataFrame(
        {
            "ACC_NO": ["HOME001"],
            "TRANCHE_OUT_BAL": [2_000_000.0],
            "BASE_RATE": [0.068],
            "LP": [0.011],
            "OBS_RATE": [0.095],
        }
    )
    print(compute_ftp_charges(home001).round(4).to_string(index=False))
