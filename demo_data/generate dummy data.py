"""
Synthetic data generator for the FTP pipeline demo/showcase.

Produces a complete, self-contained set of inputs -- three reference
workbooks (ftp mappings.xlsx, wal mappings.xlsx, run mappings.xlsx) and a
week of raw daily extracts (naretail, ABF, TB GL) across 5 generic branches
-- so the pipeline can be cloned and run end-to-end with zero real bank
data. Every account number, balance, and rate below is randomly generated
(fixed seed, for reproducibility) -- nothing here is drawn from, derived
from, or resembles any real institution's actual figures. Branch codes
(100/300/400/600/890) and product names (Home Loan, Call Accounts, etc.)
are generic retail/corporate banking terms, not identifying of any real
bank.

Run this once to populate ./demo_data/, then point the full pipeline's
config cell at that folder (see pipeline/PIPELINE_ARCHITECTURE.md --
full pipeline source isn't published in this repo, available on request).
"""
import datetime as dt
import os
import numpy as np
import pandas as pd
import openpyxl
from openpyxl.styles import Font

RNG = np.random.default_rng(42)
OUT_DIR = "demo_data"
CCY = "USD"  # generic demo currency -- not the real bank's own currency
BRANCHES = [100, 300, 400, 600, 890]
BRANCH_NAMES = {100: "CORPORATE", 300: "SME", 400: "PRIVATE", 600: "RETAIL", 890: "TREASURY"}

# One week of business days for the demo -- extend DAYS for a longer run
START_DATE = dt.date(2026, 4, 1)
DAYS = [START_DATE + dt.timedelta(days=i) for i in range(6)]  # 1 "previous day" (week 0) + 5 real days

PRODUCTS = [
    # (PROD_DESC, AC_CAT, GL_LINE, ALCO_CLASS, BS_TYPE, WAL_TYPE, branches)
    ("Home Loan", "MORL", 1002, "RESIDENTIAL MORTGAGE", "ASSET", "CONTRACTUAL", [100, 300, 400, 600]),
    ("Business Loan", "CLBL", 1003, "COMMERCIAL MORTGAGE", "ASSET", "CONTRACTUAL", [100, 300]),
    ("Overdraft", "C007", 1005, "OVERDRAFTS", "ASSET", "CONTRACTUAL", [100, 300, 400, 600]),
    ("Call Account", "C008", 2001, "CALL ACCOUNTS", "LIABILITY", "STRUCTURAL SPLIT", [100, 300, 400, 600]),
    ("Current Account", "C001", 2002, "CURRENT ACCOUNTS", "LIABILITY", "STRUCTURAL SPLIT", [100, 300, 400, 600]),
    ("Term Deposit", "S002", 2003, "TERM DEPOSITS", "LIABILITY", "CONTRACTUAL", [100, 300, 400, 600]),
    ("Notice Deposit", "ND05", 2004, "NOTICE DEPOSITS", "LIABILITY", "CONTRACTUAL", [400, 600]),
    ("Treasury Bill", "MPD3", 1010, "TREASURY BILLS", "ASSET", "PREPAYMENT", [890]),
    ("Interbank Placement", "MPX3", 1011, "GOVERNMENT GUARANTEED", "ASSET", "CONTRACTUAL", [890]),
]

os.makedirs(OUT_DIR, exist_ok=True)


def build_reference_workbooks():
    # ---- ftp mappings.xlsx ----
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("product")
    ws.append(["BRANCH CODE", "PRODUCT", "GL LINE", "SOURCE", "ALCO CLASSIFICATION",
               "ALCO SEGMENT", "BS TYPE", "AC_CAT", "CURRENCY"])
    for prod, cat, gl, alco, bs_type, wal_type, branches in PRODUCTS:
        source = "ABF FILE" if prod in ("Treasury Bill", "Interbank Placement") else "NARETAIL FILE"
        for br in branches:
            ws.append([br, prod, gl, source, alco, BRANCH_NAMES[br], bs_type, cat, CCY])

    ws2 = wb.create_sheet("exclusions")
    ws2.append(["BRANCH CODE", "EXCLUDED PRODUCT", "REASON"])

    ws3 = wb.create_sheet("component info")
    ws3.append([" TYPE ", " BS PRODUCT ", "ALCO CLASS", "BASE RATE", "LIQUIDITY PREMIUM",
                "CREDIT RISK", "PREPAYMENT WAL", "STRUCTURAL SPLIT", "NOTES"])
    seen_alco = set()
    for prod, cat, gl, alco, bs_type, wal_type, branches in PRODUCTS:
        if alco in seen_alco:
            continue
        seen_alco.add(alco)
        ws3.append([bs_type, prod, alco, "Y", "Y", "Y" if bs_type == "ASSET" else "N",
                    "Y" if wal_type == "PREPAYMENT" else "N", "Y" if wal_type == "STRUCTURAL SPLIT" else "N", ""])

    ws4 = wb.create_sheet("fallback")
    ws4.append(["CURRENT_VALUE", "REFERENCE_SHEET", "OVERRIDE_FIELD", "OVERRIDE_VALUE"])

    ws5 = wb.create_sheet("obs rate info")
    ws5.append(["BRANCH CODE", "AC CAT", "PROD DESC", "OBS RATE TYPE", "OBS RATE TYPE DESC"])

    ws6 = wb.create_sheet("ftp rates")
    ws6.append(["Tenor_yrs", "Base_Term_dec", "Liquidity_dec"])
    # A simple, upward-sloping demo curve (both base rate and liquidity
    # premium increase with tenor) -- Tenor_yrs MUST be sorted ascending,
    # lookup_ftp_rate relies on it for merge_asof's own correctness.
    for tenor, base, liq in [
        (0.0027, 0.030, 0.000), (0.0833, 0.035, 0.001), (0.25, 0.040, 0.002),
        (0.5, 0.045, 0.003), (1, 0.050, 0.005), (2, 0.055, 0.008),
        (5, 0.065, 0.015), (10, 0.075, 0.025), (20, 0.085, 0.035),
    ]:
        ws6.append([tenor, base, liq])

    wb.save(f"{OUT_DIR}/ftp_mappings.xlsx")

    # ---- wal mappings.xlsx ----
    wb2 = openpyxl.Workbook()
    wb2.remove(wb2.active)

    ws = wb2.create_sheet("tenor info")
    ws.append(["TENOR DESC", "STRICTLY GREATER THAN X VALUE", "CLASSIFICATION"])
    for desc, thresh, cls in [("Overnight", -1, "OVERNIGHT"), ("Short", 0.0833, "SHORT TERM"),
                                ("Medium", 1, "MEDIUM TERM"), ("Long", 5, "LONG TERM")]:
        ws.append([desc, thresh, cls])

    ws2 = wb2.create_sheet("manual contractual wal")
    ws2.append(["BRANCH CODE", "ACC NO", "AC CAT", "PRODUCT", "ALCO CLASSIFICATION",
                "ALCO SEGMENT", "WAL", "WAL ASSUMPTION REASON"])
    ws2.append(["ALL", "ALL", "ND05", "Notice Deposit", "NOTICE DEPOSITS", "ALL", 0.0877,
                "32-day notice period"])

    ws3 = wb2.create_sheet("manual behavioural wal")
    ws3.append(["BRANCH CODE", "ACC NO", "AC CAT", "WAL TYPE", "TENOR CLASS", "WAL",
                "WAL ASSUMPTION REASON"])

    # "structural split" header detection requires the first 3 columns to be
    # BRANCH PROD / BRANCH CODE / BS LINE specifically (matching the real
    # workbook's own layout) -- BRANCH PROD/BS LINE aren't otherwise read,
    # ALCO CLASS and the 4 *_DEC columns (space-separated, not underscored)
    # are what the pipeline actually uses.
    ws4 = wb2.create_sheet("structural split")
    ws4.append(["BRANCH PROD", "BRANCH CODE", "BS LINE", "ALCO CLASS", "VOLATILE DEC",
                "STABLE SHORT TERM DEC", "STABLE MEDIUM TERM DEC", "STABLE LONG TERM DEC"])
    for br in BRANCHES:
        ws4.append([f"{br}-CALL", br, "2001", "CALL ACCOUNTS", 0.05, 0.10, 0.25, 0.60])
        ws4.append([f"{br}-CURR", br, "2002", "CURRENT ACCOUNTS", 0.10, 0.15, 0.30, 0.45])

    ws5 = wb2.create_sheet("prepayment wal")
    ws5.append(["BRANCH_CODE", "AC_CAT", "ALCO_CLASS", "FINAL_PREPAYMENT_ADJUSTED_WAL"])
    ws5.append([890, "MPD3", "TREASURY BILLS", 0.5])

    ws6 = wb2.create_sheet("tenor class wal")
    ws6.append(["BRANCH_CODE", "TENOR_CLASSIFICATION", "FINAL_WAL"])
    for br in BRANCHES:
        for tc, wal in [("OVERNIGHT", 0.0027), ("SHORT TERM", 0.25), ("MEDIUM TERM", 2.5), ("LONG TERM", 8.0)]:
            ws6.append([br, tc, wal])

    ws7 = wb2.create_sheet("wal type")
    ws7.append(["WAL_TYPE", "PREPAYMENT WAL", "STRUCTURAL SPLIT"])
    ws7.append(["CONTRACTUAL", "N", "N"])
    ws7.append(["PREPAYMENT", "Y", "N"])
    ws7.append(["STRUCTURAL SPLIT", "N", "Y"])

    wb2.save(f"{OUT_DIR}/wal_mappings.xlsx")

    # ---- run mappings.xlsx ----
    wb3 = openpyxl.Workbook()
    wb3.remove(wb3.active)

    ws = wb3.create_sheet("branches")
    ws.append(["BRANCH CODE"])
    for br in BRANCHES:
        ws.append([br])

    ws2 = wb3.create_sheet("weekday info")
    ws2.append(["WEEKDAY", "WEEK NO", "NOTES"])
    ws2.append([int(DAYS[0].strftime("%Y%m%d")), 0, "Week 0 reference -- previous day for the first real day"])
    for d in DAYS[1:]:
        ws2.append([int(d.strftime("%Y%m%d")), 1, ""])

    ws3 = wb3.create_sheet("segment map")
    ws3.append(["BRANCH NAME", "BRANCH CODE"])
    for br, name in BRANCH_NAMES.items():
        ws3.append([name, br])

    wb3.save(f"{OUT_DIR}/run_mappings.xlsx")

    for f in ["ftp_mappings.xlsx", "wal_mappings.xlsx", "run_mappings.xlsx"]:
        _bold_headers(f"{OUT_DIR}/{f}")


def _bold_headers(path):
    wb = openpyxl.load_workbook(path)
    for ws in wb.worksheets:
        for cell in ws[1]:
            cell.font = Font(bold=True)
    wb.save(path)


print("Building reference workbooks...")
build_reference_workbooks()
print(f"Done -- {OUT_DIR}/ftp_mappings.xlsx, wal_mappings.xlsx, run_mappings.xlsx")


def generate_ftp_curve_model_inputs():
    """Synthetic market data for the SEPARATE FTP rate curve construction
    model (ftp_curve_model.py) -- treasury bills, government bonds, and
    comparative deposit rates. Its own market-data inputs, nothing to do
    with naretail/ABF/TB_GL.

    Written to ./input/ (a sibling of demo_data/, NOT nested inside it) --
    matching that model's own default file paths.

    Schema, matching load_tbills/load_bonds/load_deposits:

    - treasury_bills_data_ii.csv: LONG format, one row per (settlement
      date, tenor) auction. Columns "Settlement Date"/"Maturity Date"
      (Tenor is DERIVED as (Maturity-Settlement).days/365.25, there's no
      direct Tenor column at all) and "Average Discount Rate"/"Average
      Competitive Yield" -- both TEXT, with a currency code and "%"
      embedded and stripped by load_tbills before parsing (only
      Competitive Yield actually feeds into the model's own "Yield",
      Discount Rate is still required as a column since load_tbills
      processes it unconditionally). Multiple settlement dates needed
      (not just one) because load_tbills' own "ew_1m"/"ew_6m"
      exponentially-weighted windows need a real date range to weight
      across.
    - bond_yields_data_ii.csv: WIDE format, genuinely different shape
      from tbills -- first column "Maturity date" (despite the name,
      this becomes each ROW's own OBSERVATION date once set as the
      index) with every OTHER column itself a date string naming one
      specific bond's own real maturity; cell values are yields in
      PERCENT (not decimal -- load_bonds divides by 100 itself).
    - deposit_rates_compare_ii.csv: single snapshot, three competitor
      columns -- every column here is a market/competitor rate, never
      an "own" quoted rate (see docs/liquidity_premium_construction.md
      for why). load_deposits renames only the first column positionally
      to "Tenor"; the rate columns are matched by their real header text
      via primary_col/fallback_cols, not by position. One row's
      comparator_1 rate is deliberately left blank here, to exercise the
      real fallback-to-comparator_2 path.
    """
    input_dir = "input"
    os.makedirs(input_dir, exist_ok=True)
    # The model's own logging setup (module import time) writes to
    # export/ftp_model.log unconditionally -- without this folder existing
    # first, importing the model at all would crash before any of its own
    # code even runs.
    os.makedirs("export", exist_ok=True)

    rng = np.random.default_rng(7)  # separate stream from the main RNG above -- independent, still reproducible

    # --- Treasury bills: long format, one row per (settlement date, tenor) ---
    tbill_tenor_days = [91, 182, 273, 364]  # standard T-bill auction tenors
    settlement_dates = [dt.date(2025, 10, 1) + dt.timedelta(days=i) for i in range(210)]  # ~7 months, so ew_6m has real coverage
    tbill_rows = []
    for sd in settlement_dates:
        for td in tbill_tenor_days:
            maturity = sd + dt.timedelta(days=td)
            base_yield = 7.0 + 0.003 * td / 91  # mild upward slope by tenor
            noise = rng.normal(0, 0.08)
            disc_rate = round(base_yield - 0.15 + noise, 3)
            comp_yield = round(base_yield + noise, 3)
            tbill_rows.append({
                "Settlement Date": sd.strftime("%d-%b-%y"),
                "Maturity Date": maturity.strftime("%d-%b-%y"),
                "Average Discount Rate": f"{CCY} {disc_rate}%",
                "Average Competitive Yield": f"{CCY} {comp_yield}%",
            })
    pd.DataFrame(tbill_rows).to_csv(f"{input_dir}/treasury_bills_data_ii.csv", index=False)

    # --- Government bonds: wide format -- one row per observation date, one column per bond maturity ---
    bond_maturities = [dt.date(2027, 3, 1), dt.date(2028, 3, 1), dt.date(2029, 3, 1),
                       dt.date(2031, 3, 1), dt.date(2033, 3, 1), dt.date(2036, 3, 1)]
    obs_dates = [dt.date(2025, 9, 1) + dt.timedelta(days=i) for i in range(210)]
    base_yields = [7.8, 8.1, 8.4, 8.8, 9.1, 9.4]  # percent, not decimal -- load_bonds itself divides by 100
    bond_wide_rows = []
    for od in obs_dates:
        row = {"Maturity date": od.strftime("%d-%b-%y")}
        for bm, base in zip(bond_maturities, base_yields):
            row[bm.strftime("%d-%b-%y")] = round(base + rng.normal(0, 0.06), 3)
        bond_wide_rows.append(row)
    pd.DataFrame(bond_wide_rows).to_csv(f"{input_dir}/bond_yields_data_ii.csv", index=False)

    # --- Deposit rates: single snapshot, three competitor banks side by side ---
    # Every column is a market/competitor observation -- the curve model
    # never consumes an "own" quoted rate (see docs/liquidity_premium_construction.md).
    deposit_tenors = [0.25, 0.50, 1.00, 2.00, 3.00, 5.00]
    comparator_1_base = [5.5, 6.2, 6.8, 7.2, 7.5, 7.8]
    deposit_rows = []
    for i, (t, c1) in enumerate(zip(deposit_tenors, comparator_1_base)):
        comparator_2 = round(c1 + rng.normal(0, 0.3), 2)
        comparator_3 = round(c1 + rng.normal(0, 0.4), 2)
        c1_val = "" if i == 2 else f"{c1}%"  # blank comparator_1's rate at one tenor deliberately -- exercises the real fallback-to-comparator_2 path
        deposit_rows.append({
            "Tenor (yrs)": t,
            "comparator_1 Rate": c1_val,
            "comparator_2 Rate": f"{comparator_2}%",
            "comparator_3 Rate": f"{comparator_3}%",
        })
    pd.DataFrame(deposit_rows).to_csv(f"{input_dir}/deposit_rates_compare_ii.csv", index=False)

    print(f"  treasury_bills_data_ii.csv: {len(tbill_rows)} rows ({len(settlement_dates)} settlement dates x {len(tbill_tenor_days)} tenors)")
    print(f"  bond_yields_data_ii.csv: {len(bond_wide_rows)} rows x {len(bond_maturities)} bond columns")
    print(f"  deposit_rates_compare_ii.csv: {len(deposit_rows)} rows (1 deliberately blank comparator_1 rate, to exercise the real fallback)")


def generate_daily_extracts():
    """Generates FLEXCUBE_NARETAIL_D{yymmdd}.xlsx, FLEXCUBE_ABFFILE_LOANS_D{yymmdd}.xlsx,
    and TB_GL_REP_DET_{yyyymmdd}.xlsx for every day in DAYS -- account balances
    walk randomly day to day (not independently redrawn) so accrual-movement-based
    OBS_RATE tiers have something realistic to compute from."""
    # Persistent per-account state so balances/rates walk realistically day to day
    accounts = {}  # acc_no -> dict(branch, product info, balance, rate, maturity...)
    acc_counter = 0

    def _new_account(prod, cat, gl, alco, bs_type, wal_type, br, is_abf):
        nonlocal acc_counter
        acc_counter += 1
        prefix = "ABF" if is_abf else "NRT"
        acc_no = f"{br}{prefix}{acc_counter:06d}"
        base_bal = float(RNG.uniform(5_000, 2_000_000))
        sign = -1 if bs_type == "LIABILITY" else 1
        rate = float(RNG.uniform(2.0, 11.0))
        maturity = START_DATE + dt.timedelta(days=int(RNG.integers(30, 3650))) if wal_type != "STRUCTURAL SPLIT" else None
        accounts[acc_no] = dict(branch=br, prod=prod, cat=cat, gl=gl, alco=alco, bs_type=bs_type,
                                 wal_type=wal_type, balance=base_bal * sign, rate=rate, maturity=maturity,
                                 is_abf=is_abf, prev_dr_accr=0.0, prev_cr_accr=0.0)
        return acc_no

    # Seed ~40-120 accounts per branch per product (realistic small-demo scale)
    for prod, cat, gl, alco, bs_type, wal_type, branches in PRODUCTS:
        is_abf = prod in ("Treasury Bill", "Interbank Placement")
        for br in branches:
            n = int(RNG.integers(40, 120))
            for _ in range(n):
                _new_account(prod, cat, gl, alco, bs_type, wal_type, br, is_abf)

    for day in DAYS:
        naretail_rows, abf_rows = [], []
        for acc_no, a in accounts.items():
            # Random walk the balance a little each day, and accrue interest
            a["balance"] *= float(1 + RNG.normal(0, 0.01))
            daily_accrual = abs(a["balance"]) * (a["rate"] / 100) / 365
            if a["bs_type"] == "ASSET":
                dr_accr = a.get("prev_dr_accr", 0.0) + daily_accrual
                cr_accr = 0.0
            else:
                dr_accr = 0.0
                cr_accr = a.get("prev_cr_accr", 0.0) + daily_accrual
            prev_dr, prev_cr = a["prev_dr_accr"], a["prev_cr_accr"]
            a["prev_dr_accr"], a["prev_cr_accr"] = dr_accr, cr_accr

            if a["is_abf"]:
                abf_rows.append(dict(
                    EXTRACT_DATE=day, BRANCH_CODE=a["branch"], BRANCH_NAME=BRANCH_NAMES[a["branch"]],
                    CUSTOMER_NAME1=f"Demo Customer {acc_no}", ACC_NO=acc_no, OUT_BAL=abs(a["balance"]),
                    ARREAR_AMT=0.0, INST_AMT=0.0, LAST_PAID=day, INT_RATE=a["rate"], AC_CAT=a["cat"],
                    VALUE_DATE=START_DATE, DR_ACCRUED_AMT=dr_accr, CR_ACCRUED_AMT=cr_accr,
                    **{"PRINCIPAL OUTSTANDING GL": a["gl"]},
                    PROD_DESC=a["prod"], ACC_CURRENCY=CCY, MATURITY_DATE=a["maturity"],
                ))
            else:
                dr_rate = a["rate"] if a["bs_type"] == "ASSET" else 0.0
                cr_rate = a["rate"] if a["bs_type"] == "LIABILITY" else 0.0
                naretail_rows.append(dict(
                    EXTRACT_DATE=day, BRANCH_NO=a["branch"], BZA_BR_NAME=BRANCH_NAMES[a["branch"]],
                    NAME=f"Demo Customer {acc_no}", ACC_NO=acc_no, OUT_BAL=a["balance"], LAST_TXN=day,
                    DR_INT_RATE=dr_rate, CR_INT_RATE=cr_rate,
                    PREV_INT_AMT_DR=prev_dr, PREV_INT_AMT_CR=prev_cr, AC_CAT=a["cat"],
                    LD_DATE=a["maturity"], ACC_CURRENCY=CCY, DR_ACCRUED_AMT=dr_accr, CR_ACCRUED_AMT=cr_accr,
                    PROD_DESC=a["prod"], INT_PROD=0.0, GL_LINE=a["gl"],
                ))

        pd.DataFrame(naretail_rows).to_excel(f"{OUT_DIR}/FLEXCUBE_NARETAIL_D{day:%y%m%d}.xlsx", index=False)
        pd.DataFrame(abf_rows).to_excel(f"{OUT_DIR}/FLEXCUBE_ABFFILE_LOANS_D{day:%y%m%d}.xlsx", index=False)
        # Empty TB GL report -- the pipeline handles this gracefully; populate with
        # real GL closing balances if you want to exercise the TB_CHECK reconciliation
        pd.DataFrame(columns=["GL_CODE", "CCY_CODE", "BRANCH_CODE", "CLOSING_BALANCE_LCY"]).to_excel(
            f"{OUT_DIR}/TB_GL_REP_DET_{day:%Y%m%d}.xlsx", index=False)
        print(f"  {day}: {len(naretail_rows)} naretail rows, {len(abf_rows)} ABF rows")


print("\nGenerating daily extracts...")
generate_daily_extracts()
print(f"\nDone. {len(DAYS)} days x 5 branches generated in ./{OUT_DIR}/")
print("Point the full pipeline's config cell at this folder to run the demo end-to-end (see pipeline/PIPELINE_ARCHITECTURE.md).")

print("\nGenerating FTP rate curve construction model inputs...")
generate_ftp_curve_model_inputs()
print("Done. Written to ./input/ -- matches ftp_curve_model's own default file paths directly.")
