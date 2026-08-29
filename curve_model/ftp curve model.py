"""
ftp_curve_model - FTP (Funds Transfer Pricing) curve construction engine.

Builds the base yield curve and liquidity-premium curve, combines them into
a full FTP term curve, and exports the reporting workbooks the Excel
reference layer consumes.

Typical usage (see the companion notebook for the full worked example)::

    from ftp_curve_model import DataObject, CurveConfig, CurveModelObject, ExportObject

    data = DataObject(tbills_file=..., bonds_file=..., deposits_file=...).load_data()
    data.prepare_data(model_version="v1")

    config = CurveConfig(model_version="v1", run_purpose="Pricing")
    model = CurveModelObject(data, config).run()
    ExportObject(model).export_all()

Everything in this module is pure library code - no file paths, scenario
choices, or other run-specific values are hardcoded here. Those belong in
the notebook (or script) that imports this module.
"""

import os
import itertools
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

from scipy.optimize import minimize, differential_evolution
from scipy.interpolate import CubicSpline, PchipInterpolator, interp1d
from sklearn.isotonic import IsotonicRegression
from typing import List, Tuple, Optional
from datetime import datetime
from dateutil.relativedelta import relativedelta

from dataclasses import dataclass, field, replace
import platform
import getpass
import logging

logger = logging.getLogger(__name__)

__all__ = [
    "CurveConfig",
    "DataObject",
    "CurveModelObject",
    "ExportObject",
    "run_scenario_suite",
    "compare_scenarios",
    "DAYS_PER_YEAR",
    "BPS_PER_DECIMAL",
    "bps_to_decimal",
    "decimal_to_bps",
    "DEFAULT_EXPORT_DIR",
    "ensure_export_dir",
    "list_deposit_rate_columns",
    "run_lp_parameter_sweep",
    "LP_SWEEPABLE_PARAMS",
]



## CONSTANTS


# Day-count basis used everywhere a date difference is converted to a
# tenor in years (load_tbills / load_bonds) and back to a day label
# (format_tenor_label / output_ftp_csv). Previously load_tbills/load_bonds
# used 365.25 while the export/label helpers used a hardcoded 365 - these
# now share one constant so tenor <-> day-label round-trips are consistent.
#
# NOTE: this is a leap-year-aware average year length, not the ACT/365
# (fixed) convention declared in ftp_model_summary's `curve_daycount`
# default. That label vs this constant should be reconciled with the
# desk's official day-count policy - flagging here rather than silently
# picking one.
DAYS_PER_YEAR = 365.25

# 1.00% = 100 bps = 0.01 decimal. Used by CurveConfig.parallel_shift_dec,
# compute_stress_lp, and compare_scenarios - previously each of these
# had its own literal "10000.0".
BPS_PER_DECIMAL = 10000.0


def bps_to_decimal(bps: float) -> float:
    """Convert basis points to a decimal rate (100 -> 0.01)."""
    return bps / BPS_PER_DECIMAL


def decimal_to_bps(dec: float) -> float:
    """Convert a decimal rate to basis points (0.01 -> 100)."""
    return dec * BPS_PER_DECIMAL


# Default export directory, used wherever a caller doesn't specify their
# own (DataObject, CurveConfig, ExportObject all accept an `export_dir`
# override). Kept as one constant so "where do outputs go" has a single
# default, same as everything else in this section.
DEFAULT_EXPORT_DIR = "export"


def ensure_export_dir(export_dir: str) -> str:
    """Create `export_dir` if it doesn't exist yet, then return it unchanged.

    Every export path in this module is built by joining an `export_dir`
    with a filename, so calling this once per export destination is
    enough to guarantee the directory is there before any `.to_excel(...)`
    call - custom export locations (e.g. a per-run subfolder) don't need
    to be created by hand first.
    """
    os.makedirs(export_dir, exist_ok=True)
    return export_dir


@dataclass
class CurveConfig:
    """
    All hyperparameters for a single FTP curve run.

    Kept as one object (rather than loose notebook variables) so a scenario
    run is just a copy of this config with one field changed - see
    `with_parallel_shift()` below.
    """
    # --- tenor grids ---
    first_tenor: float = 0.001
    last_tenor: float = 30.0
    t_grid_10_bounds: Tuple[float, float] = (0.01, 1.0)

    # --- market / regulatory ---
    repo_rate: float = 7.50    # asset overnight
    call_rate: float = 7.55    # liability overnight
    prime_rate: float = 10.5
    reg_cap: float = 12.0
    epsilon: float = 1e-3
    tbill_cutoff: float = 1.0
    bond_cutoff: float = 10.0
    check_tenors: List[float] = field(
        default_factory=lambda: [0.25, 0.50, 1.0, 3.0, 5.0, 10.0, 20.0, 30.0]
    )

    # --- base NS curve penalties ---
    base_lambda_level: float = 0.001
    base_lambda_curvature: float = 0.002
    base_lambda_mono: float = 2.0
    base_starts: int = 30

    # --- liquidity premium curve ---
    # NOTE: lsm_alpha/lsm_tau_s below are unused by any computation - they
    # only ever land in the exported summary's "LSM alpha"/"LSM tau_s"
    # rows. The values that actually drive the stress overlay are
    # apply_lp_stress/lp_stress_alpha/lp_stress_tau_s/lp_max_stress_bps
    # further down. Left as-is (not this task's scope to remove/rename
    # established fields) but flagging so the two don't get confused.
    lsm_alpha: float = 0.2
    lsm_tau_s: float = 5.0
    max_lp_dec: float = 0.003
    lp_lambda_level: float = 0.01
    lp_lambda_curvature: float = 0.002
    lp_lambda_mono: float = 20.0
    lp_starts: int = 50
    # Observed-LP stress overlay (build_observed_lp / compute_stress_lp).
    # apply_lp_stress=False skips the overlay entirely - Stress_LP is then
    # all zeros and Total_LP == Structural_LP.
    # IMPORTANT: lp_fit_target (below) defaults to "Stress_LP".
    # If you set apply_lp_stress=False, the LP curve is fit to that
    # now-all-zero column unless you also change lp_fit_target to
    # "Total_LP" or "Total_LP_Monotonic" - otherwise you get a flat-zero
    # liquidity premium curve, not "the structural premium without stress".
    apply_lp_stress: bool = True
    lp_stress_alpha: float = 0.30
    lp_stress_tau_s: float = 2.5
    lp_max_stress_bps: float = 100.0
    lp_short_tolerance: float = 0.05

    # Which column of build_observed_lp()'s output the LP curve is fit to.
    # "Stress_LP" (current/default behaviour) is the stress OVERLAY only.
    # "Total_LP" is structural + stress; "Total_LP_Monotonic" is that same
    # total after isotonic smoothing (which CurveModelObject always
    # requests via apply_isotonic=True, so it's always available).
    # FLAG FOR REVIEW: fitting to "Stress_LP" alone means the NS liquidity
    # curve is being fit to the stress add-on rather than the full
    # structural+stress liquidity premium - confirm this is intentional
    # before relying on lp_dec_grid as "the" liquidity premium.
    lp_fit_target: str = "Stress_LP"

    # --- scenario shock (this is the +/-100bps test lever) ---
    parallel_shift_bps: float = 0.0

    # When False, the deposit-vs-3-month-T-bill "observability" spread is
    # NOT added to the base curve (only the scenario parallel_shift_bps
    # still is). The spread itself is still computed and logged either
    # way - this only controls whether it gets added into
    # base_term_shifted / the final FTP curve.
    apply_3m_shift: bool = True

    # --- run metadata ---
    model_version: str = "v1-2"
    run_purpose: str = "Treasury Trial 002"
    data_cutoff_date: str = "2026-01-31"

    # Where CurveModelObject/ExportObject write their Excel outputs.
    # Independent of DataObject's tbills_export_dir/bonds_export_dir/
    # deposits_export_dir (raw market-data exports can live somewhere
    # different from curve/scenario outputs), but usually set to the same
    # folder for a given run.
    export_dir: str = DEFAULT_EXPORT_DIR

    # Whether CurveModelObject.apply_shifts() writes its base-curve
    # (pre/post-shift, bucketed) diagnostic workbook to export_dir. Off by
    # default - this is a minor diagnostic artifact, distinct from the
    # curve/rate outputs ExportObject writes (those are controlled
    # separately, per-artifact, via ExportObject.export_all()'s own
    # bucketed/csv/summary flags).
    export_base_curve: bool = False

    # --- execution mode ---
    # When False, CurveModelObject skips all plotting and the verbose
    # NS-fit/LP/diagnostic print statements. Off by default only matters
    # once you flip it - default True preserves today's fully-interactive
    # behaviour. Set False for batch/scenario runs (e.g. run_scenario_suite)
    # where rendering ~15 plots per scenario is wasted work.
    visualize: bool = True

    def __post_init__(self):
        if self.first_tenor <= 0:
            raise ValueError(f"first_tenor must be > 0, got {self.first_tenor}")
        if self.last_tenor <= self.first_tenor:
            raise ValueError(
                f"last_tenor ({self.last_tenor}) must be > first_tenor ({self.first_tenor})"
            )
        if self.tbill_cutoff >= self.bond_cutoff:
            raise ValueError(
                f"tbill_cutoff ({self.tbill_cutoff}) must be < bond_cutoff ({self.bond_cutoff})"
            )

    @property
    def t_grid_long(self) -> np.ndarray:
        n = int(self.last_tenor / self.first_tenor)
        return np.linspace(self.first_tenor, self.last_tenor, n)

    @property
    def t_grid_10(self) -> np.ndarray:
        lo, hi = self.t_grid_10_bounds
        return np.linspace(lo, hi, 100)

    @property
    def parallel_shift_dec(self) -> float:
        """parallel_shift_bps expressed as a decimal rate."""
        return bps_to_decimal(self.parallel_shift_bps)

    def with_parallel_shift(self, bps: float, **overrides) -> "CurveConfig":
        """Return a copy of this config with an absolute parallel shift (in
        bps) applied to the base curve, plus any other field overrides
        (e.g. model_version) for tagging exported files distinctly."""
        return replace(self, parallel_shift_bps=bps, **overrides)


## LOAD DATA


@dataclass(frozen=True)
class MarketWindow:
    """
    Market observation window configuration.
    """
    name: str
    months: Optional[int] = None
    half_life_days: Optional[int] = None
    @property
    def is_latest(self):
        return self.months is None
    
    
def compute_exponential_weights(
    dates,
    reference_date,
    half_life_days=30,
):
    """
    Compute exponentially decaying weights.
    Parameters
    ----------
    dates : pd.Series or array-like
    reference_date : pd.Timestamp
    half_life_days : int
    Returns
    -------
    np.ndarray
        Normalized exponential weights.
    """
    dates = pd.to_datetime(dates)
    age_days = (
        reference_date - dates
    ).dt.days.values.astype(float)
    decay_lambda = np.log(2.0) / half_life_days
    weights = np.exp(
        -decay_lambda * age_days
    )
    weight_sum = weights.sum()
    if weight_sum <= 0:
        raise ValueError(
            "Exponential weights sum to zero."
        )
    return weights / weight_sum


def filter_market_window(
    df,
    date_col,
    window,
):
    """
    Filter dataframe to market window.
    """
    latest_date = df[date_col].max()
    if window.is_latest:
        out = df.loc[
            df[date_col] == latest_date
        ].copy()
        out["_ew_weight"] = 1.0
        return out
    cutoff = latest_date - pd.DateOffset(
        months=window.months
    )
    out = df.loc[
        df[date_col] >= cutoff
    ].copy()
    out["_ew_weight"] = compute_exponential_weights(
        dates=out[date_col],
        reference_date=latest_date,
        half_life_days=window.half_life_days,
    )
    return out


def log_market_summary(
    df,
    dataset_name,
    date_col,
):
    """
    Structured logging for market datasets.
    """
    first_obs = df[date_col].min()
    last_obs = df[date_col].max()
    logger.info(
        "\n%s\n"
        "Samples      = %s\n"
        "Min Yield    = %.3f%%\n"
        "Max Yield    = %.3f%%\n"
        "Avg Yield    = %.3f%%\n"
        "First Obs    = %s\n"
        "Last Obs     = %s\n",
        dataset_name,
        len(df),
        df["Yield"].min() * 100,
        df["Yield"].max() * 100,
        df["Yield"].mean() * 100,
        first_obs,
        last_obs,
    )
    
    
def log_bond_window_summary(
    raw_window_df,
    window_name,
    weights,
):
    """
    Detailed bond market diagnostics.

    `raw_window_df` must already be in DECIMAL form (e.g. 0.1150 for
    11.50%), same convention as log_market_summary - this function always
    multiplies by 100 for display. Passing it the raw percent-form data
    (e.g. 11.50) double-scales the printed/logged yield (11.50 -> 1150%).
    """
    obs_dates = raw_window_df.index
    first_obs = obs_dates.min()
    last_obs = obs_dates.max()
    yields_flat = raw_window_df.values.flatten()
    yields_flat = yields_flat[
        np.isfinite(yields_flat)
    ]
    effective_n = (
        1.0 / np.sum(weights**2)
    )
    logger.info(
        "\nGovernment Bonds [%s]\n"
        "Observation Dates = %s\n"
        "First Obs         = %s\n"
        "Last Obs          = %s\n"
        "Min Yield         = %.3f%%\n"
        "Max Yield         = %.3f%%\n"
        "Avg Yield         = %.3f%%\n"
        "Effective N       = %.2f\n"
        "EW Max Weight     = %.4f\n"
        "EW Min Weight     = %.4f\n",
        window_name,
        len(obs_dates),
        first_obs,
        last_obs,
        yields_flat.min() * 100,
        yields_flat.max() * 100,
        yields_flat.mean() * 100,
        effective_n,
        weights.max(),
        weights.min(),
    )


def log_deposit_summary(
    df,
    source_counts=None,
    dataset_name="Market Deposits",
):
    """
    Structured logging for the deposit-rate dataset, matching the style of
    log_market_summary / log_bond_window_summary above.

    Deposits are a single point-in-time snapshot with no observation date
    column (unlike t-bills/bonds), so tenor range is reported in place of
    first/last observation date. `df` must contain decimal 'Tenor' and
    'Yield' columns. `source_counts`, if given, is a dict of
    {column_name: n} reporting how many rows' final Yield came from each
    configured source column (primary first, then fallbacks in priority
    order) - column names are whatever load_deposits' primary_col /
    fallback_cols were called with, so this isn't tied to any specific
    bank's column naming.
    """
    message = (
        "\n%s\n"
        "Samples      = %s\n"
        "Min Tenor    = %.2f yrs\n"
        "Max Tenor    = %.2f yrs\n"
        "Min Yield    = %.3f%%\n"
        "Max Yield    = %.3f%%\n"
        "Avg Yield    = %.3f%%\n"
    )
    args = [
        dataset_name,
        len(df),
        df["Tenor"].min(),
        df["Tenor"].max(),
        df["Yield"].min() * 100,
        df["Yield"].max() * 100,
        df["Yield"].mean() * 100,
    ]
    if source_counts:
        for col_name, count in source_counts.items():
            message += f"{col_name + ' quotes':<14} = %s\n"
            args.append(count)
    logger.info(message, *args)



def load_tbills(
    file_path,
    export=False,
    export_dir=DEFAULT_EXPORT_DIR,
):
    """
    Load treasury bill datasets using:
    - latest snapshot
    - exponentially weighted windows
    """
    df = pd.read_csv(file_path)
    # -----------------------------------------
    # Cleaning
    # -----------------------------------------
    numeric_cols = [
        "Average Discount Rate",
        "Average Competitive Yield",
    ]
    for col in numeric_cols:
        df[col] = (
            df[col]
            .astype(str)
            .str.replace(r"^[A-Z]{3}\s*", "", regex=True)  # strip a leading 3-letter currency code, if present
            .str.replace("%", "", regex=False)
            .str.strip()
        )
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )
    # -----------------------------------------
    # Dates
    # -----------------------------------------
    df["Settlement Date"] = pd.to_datetime(
        df["Settlement Date"],
        errors="coerce"
    )
    df["Maturity Date"] = pd.to_datetime(
        df["Maturity Date"],
        errors="coerce"
    )
    # -----------------------------------------
    # Derived fields
    # -----------------------------------------
    df["Tenor"] = (
        df["Maturity Date"]
        - df["Settlement Date"]
    ).dt.days / DAYS_PER_YEAR
    df["Yield"] = (
        df["Average Competitive Yield"] / 100.0
    )
    # -----------------------------------------
    # Window configurations
    # -----------------------------------------
    windows = [
        MarketWindow(
            name="latest",
        ),
        MarketWindow(
            name="ew_1m",
            months=1,
            half_life_days=10,
        ),
        MarketWindow(
            name="ew_6m",
            months=6,
            half_life_days=45,
        ),
    ]
    out = {}
    for w in windows:
        w_df = filter_market_window(
            df=df,
            date_col="Settlement Date",
            window=w,
        )
        # -------------------------------------
        # EW aggregation
        # -------------------------------------
        curve_df = (
            w_df
            .groupby("Tenor")
            .apply(
                lambda g: np.average(
                    g["Yield"],
                    weights=g["_ew_weight"],
                )
            )
            .reset_index(name="Yield")
            .sort_values("Tenor")
            .reset_index(drop=True)
        )
        out[w.name] = curve_df
        log_market_summary(
            w_df,
            dataset_name=f"Treasury Bills [{w.name}]",
            date_col="Settlement Date",
        )
        if export:
            curve_df.to_excel(
                f"{ensure_export_dir(export_dir)}/tbills_{w.name}.xlsx",
                index=False,
            )
    return out


def load_bonds(
    file_path,
    export=False,
    export_dir=DEFAULT_EXPORT_DIR,
):
    """
    Load government bond datasets using:
    - latest snapshot
    - exponentially weighted windows
    """
    raw = pd.read_csv(
        file_path,
        low_memory=False,
    )
    # -----------------------------------------
    # Observation dates
    # -----------------------------------------
    raw["Maturity date"] = pd.to_datetime(
        raw["Maturity date"],
        format="%d-%b-%y",
    )
    raw = raw.sort_values(
        "Maturity date"
    )
    raw.set_index(
        "Maturity date",
        inplace=True,
    )
    raw.bfill(inplace=True)
    raw.columns = [
        col.split(".")[0]
        for col in raw.columns
    ]
    maturity_dates = [
        pd.to_datetime(
            col,
            format="%d-%b-%y",
        )
        for col in raw.columns
    ]
    windows = [
        MarketWindow(
            name="latest",
        ),
        MarketWindow(
            name="ew_1m",
            months=1,
            half_life_days=10,
        ),
        MarketWindow(
            name="ew_6m",
            months=6,
            half_life_days=45,
        ),
    ]
    out = {}
    for w in windows:
        latest_date = raw.index.max()
        # -------------------------------------
        # Latest snapshot
        # -------------------------------------
        if w.is_latest:
            w_df = raw.iloc[[-1]].copy()
            weights = np.array([1.0])
        else:
            cutoff = latest_date - pd.DateOffset(
                months=w.months
            )
            w_df = raw.loc[
                raw.index >= cutoff
            ].copy()
            weights = compute_exponential_weights(
                dates=pd.Series(w_df.index),
                reference_date=latest_date,
                half_life_days=w.half_life_days,
            )
        # -------------------------------------
        # EW average yields
        # -------------------------------------
        ew_yields = np.average(
            w_df.values,
            axis=0,
            weights=weights,
        )
        valuation_date = latest_date
        tenors = np.array([
            (
                maturity_date
                - valuation_date
            ).days / DAYS_PER_YEAR
            for maturity_date in maturity_dates
        ])
        bonds_df = pd.DataFrame({
            "Tenor": tenors,
            "Yield": ew_yields / 100.0,
        })
        bonds_df = (
            bonds_df
            .groupby("Tenor", as_index=False)
            .agg(
                Yield=("Yield", "mean")
            )
            .sort_values("Tenor")
            .reset_index(drop=True)
        )
        out[w.name] = bonds_df
        log_bond_window_summary(
            # w_df is still in raw percent form here (e.g. 11.50, not
            # 0.1150) - convert to decimal so log_bond_window_summary's
            # "* 100 for display" matches log_market_summary's convention.
            # Previously this passed raw percent values straight through,
            # so the logged yields were 100x too large (e.g. 1150.00%
            # instead of 11.50%).
            raw_window_df=w_df / 100.0,
            window_name=w.name,
            weights=weights,
        )
        if export:
            bonds_df.to_excel(
                f"{ensure_export_dir(export_dir)}/bonds_{w.name}.xlsx",
                index=False,
            )
    return out


def list_deposit_rate_columns(file_path: str) -> List[str]:
    """
    Returns the deposit-rate source columns available in `file_path`
    (every column except the first, which load_deposits always treats as
    the tenor column). Use this to see valid choices for load_deposits'
    `primary_col` / `fallback_cols` before picking them.
    """
    df = pd.read_csv(file_path, nrows=0)
    return list(df.columns[1:])


def load_deposits(
    file_path: str,
    primary_col: str,
    fallback_cols: Optional[List[str]] = None,
    export: bool = False,
    export_dir: str = DEFAULT_EXPORT_DIR,
) -> pd.DataFrame:
    """
    Load and preprocess deposit-rate data.

    Expects a CSV whose first column is tenor and remaining columns are
    named rate sources (e.g. bank names) - see list_deposit_rate_columns()
    to inspect what's available in a given file.

    `primary_col` is the source used wherever it has a value.
    `fallback_cols` (at most 2, tried in list order) fill in wherever a
    row is missing a value in `primary_col`, then in the previous
    fallback: fallback_cols[0] is tried before fallback_cols[1].

    Both `primary_col` and every entry in `fallback_cols` must exactly
    match a column name in the file - there's no default source anymore,
    so a run with an unrecognised name fails fast with the list of
    columns that were actually found, rather than silently picking one.

    Returns a DataFrame with numeric Tenor (yrs) and decimal Yield.
    """
    if fallback_cols is None:
        fallback_cols = []
    if len(fallback_cols) > 2:
        raise ValueError(
            f"fallback_cols supports at most 2 columns (tried in priority "
            f"order), got {len(fallback_cols)}: {fallback_cols}"
        )
    if primary_col in fallback_cols:
        raise ValueError(
            f"primary_col ({primary_col!r}) cannot also appear in fallback_cols ({fallback_cols!r})"
        )

    df = pd.read_csv(file_path)
    tenor_source_col = df.columns[0]
    df = df.rename(columns={tenor_source_col: 'Tenor'})

    available = [c for c in df.columns if c != 'Tenor']
    requested = [primary_col, *fallback_cols]
    missing = [c for c in requested if c not in available]
    if missing:
        raise ValueError(
            f"Deposit column(s) not found in {file_path}: {missing}. "
            f"Available columns: {available}"
        )

    df['Tenor'] = (
        df['Tenor']
          .astype(str)
          .str.strip()
          .str.replace(r"\s+", "", regex=True)
    )
    df['Tenor'] = pd.to_numeric(df['Tenor'], errors='coerce')

    for col in requested:
        df[col] = (
            df[col]
              .astype(str)
              .str.strip()
              .str.replace(r"[^\d\.]+", "", regex=True)
        )
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # Priority fill: primary_col first, then each fallback in list order.
    # source_counts tracks which column each row's final Yield actually
    # came from (not just which columns have data), for log_deposit_summary.
    df['Yield'] = df[primary_col]
    used_col = pd.Series(pd.NA, index=df.index, dtype="object")
    used_col[df['Yield'].notna()] = primary_col
    for fb_col in fallback_cols:
        still_missing = df['Yield'].isna()
        df.loc[still_missing, 'Yield'] = df.loc[still_missing, fb_col]
        newly_filled = still_missing & df['Yield'].notna()
        used_col[newly_filled] = fb_col

    df = df.dropna(subset=['Tenor', 'Yield'])
    source_counts = used_col.loc[df.index].value_counts().to_dict()

    df['Yield'] = df['Yield'] / 100.0

    df = df.sort_values('Tenor').reset_index(drop=True)
    log_deposit_summary(df, source_counts=source_counts, dataset_name="Market Deposits")
    if export: df.to_excel(f'{ensure_export_dir(export_dir)}/deposits.xlsx', index=False)

    return df[['Tenor', 'Yield']]


## PREPARE DATA


TENOR_BUCKETS = np.array([0.1,0.25,0.5,0.75,1.0,1.5,2.0,3.0,5.0,7.0,10.0,20.0,30.0])

def assign_bucket(x, buckets=TENOR_BUCKETS):
    """Find the nearest bucket for tenor x."""
    idx = np.abs(buckets - x).argmin()
    return buckets[idx]

def bucket_and_merge(df, tenor_col='Tenor', yield_col='Yield', how='mean'):
    """
    1. Assign each tenor to nearest bucket.
    2. Aggregate yields in each bucket.
    3. Compute discrete discount factor for each bucket
    4. Return bucket summary (Tenor, Yield).
    """
    df = df.copy()
    df['Bucket'] = df[tenor_col].apply(assign_bucket)

    agg = (
        df
        .groupby('Bucket', as_index=False)
        .agg({ yield_col: how })
        .rename(columns={ yield_col: 'Yield' })
    )
    # discrete DF for each bucket
    agg['DiscountFactor'] = 1.0 / (1.0 + agg['Yield'] * agg['Bucket'])

    agg = agg.sort_values('Bucket').reset_index(drop=True)
    agg.rename(columns={'Bucket':'Tenor'}, inplace=True)
    
    return agg



def find_monotonic_violations(df: pd.DataFrame, yield_col='Yield') -> pd.DataFrame:
    """
    Returns the rows where `yield_col` decreases vs. the previous tenor.
    Operates on a sorted COPY - does not mutate the caller's DataFrame
    (the original implementation discarded the result of .sort_values()
    and wrote a 'Diff' column directly onto the DataFrame passed in).
    """
    df = df.sort_values('Tenor').reset_index(drop=True).copy()
    df['Diff'] = df[yield_col].diff()
    violations = df[df['Diff'] < 0]

    return violations[['Tenor', yield_col, 'Diff']]


def enforce_monotonic_isotonic(df: pd.DataFrame, yield_col='Yield') -> pd.DataFrame:
    """
    Uses isotonic regression to find best-fitting non decreasing sequence of yields.
    """
    df_iso = df.sort_values('Tenor').reset_index(drop=True)
    ir = IsotonicRegression(increasing=True, out_of_bounds='clip')
    y_iso = ir.fit_transform(df_iso['Tenor'], df_iso[yield_col])
    fitted_col = f'{yield_col}_'
    df_iso[fitted_col] = y_iso
    out = df_iso[['Tenor', fitted_col]].rename(columns={fitted_col: yield_col})

    return out


class DataObject:
    """
    Loads and prepares the three market datasets (t-bills, bonds, deposits)
    used to build the FTP curve. All heavy lifting is delegated to the
    existing load_tbills / load_bonds / load_deposits / bucket_and_merge /
    find_monotonic_violations / enforce_monotonic_isotonic functions defined
    above - this class just sequences and stores their outputs.

    Each asset type (t-bills, bonds, deposits) has its own independent
    export switch + directory, covering both that asset's raw/window
    market-data dump (from load_tbills/load_bonds/load_deposits) and its
    prepared/bucketed data (written in prepare_data()). Leave an
    `export_<asset>` flag False (the default) to skip that asset's
    exports entirely - the corresponding `<asset>_export_dir` is only
    used when its flag is True.
    """

    def __init__(
        self,
        tbills_file,
        bonds_file,
        deposits_file,
        deposit_primary_source,
        deposit_fallback_sources=None,
        export_tbills=False,
        tbills_export_dir=DEFAULT_EXPORT_DIR,
        export_bonds=False,
        bonds_export_dir=DEFAULT_EXPORT_DIR,
        export_deposits=False,
        deposits_export_dir=DEFAULT_EXPORT_DIR,
    ):
        self.tbills_file = tbills_file
        self.bonds_file = bonds_file
        self.deposits_file = deposits_file
        # Which deposits-file column to use as the primary yield source,
        # and up to 2 fallback columns (tried in list order) for rows
        # where the primary is missing. Must match column names that
        # actually exist in deposits_file - see list_deposit_rate_columns().
        self.deposit_primary_source = deposit_primary_source
        self.deposit_fallback_sources = deposit_fallback_sources or []

        self.export_tbills = export_tbills
        self.tbills_export_dir = tbills_export_dir
        self.export_bonds = export_bonds
        self.bonds_export_dir = bonds_export_dir
        self.export_deposits = export_deposits
        self.deposits_export_dir = deposits_export_dir

        # raw loads
        self.tbill_sets = None
        self.bond_sets = None
        self.tbills_data = None
        self.bonds_data = None
        self.deposit_rates_data = None

        # cleaned / bucketed
        self.tbill_df = None
        self.bond_df = None
        self.deposit_bkt = None
        self.tbill_bkt = None
        self.bond_bkt = None

        # data governance metadata (set via set_cutoff_dates)
        self.tbills_data_cutoff_date = None
        self.bonds_data_cutoff_date = None
        self.deposits_data_cutoff_date = None
        self.snapshot_window = "Latest"

    def _require_files(self):
        """Fail fast with a clear error rather than a deep pandas traceback."""
        missing = [
            path for path in (self.tbills_file, self.bonds_file, self.deposits_file)
            if not os.path.isfile(path)
        ]
        if missing:
            raise FileNotFoundError(
                f"DataObject input file(s) not found: {missing}"
            )

    def load_data(self):
        self._require_files()
        self.tbill_sets = load_tbills(
            self.tbills_file, export=self.export_tbills, export_dir=self.tbills_export_dir
        )
        self.bond_sets = load_bonds(
            self.bonds_file, export=self.export_bonds, export_dir=self.bonds_export_dir
        )
        self.deposit_rates_data = load_deposits(
            self.deposits_file,
            primary_col=self.deposit_primary_source,
            fallback_cols=self.deposit_fallback_sources,
            export=self.export_deposits,
            export_dir=self.deposits_export_dir,
        )

        self.tbills_data = self.tbill_sets["latest"]
        self.bonds_data = self.bond_sets["latest"]
        return self

    def set_cutoff_dates(self, tbills=None, bonds=None, deposits=None, snapshot_window=None):
        """Records the data-governance cutoff dates shown in the model summary export."""
        if tbills is not None:
            self.tbills_data_cutoff_date = tbills
        if bonds is not None:
            self.bonds_data_cutoff_date = bonds
        if deposits is not None:
            self.deposits_data_cutoff_date = deposits
        if snapshot_window is not None:
            self.snapshot_window = snapshot_window
        return self

    def prepare_data(self, model_version="v1-2"):
        """Monotonic-violation checks + isotonic cleanup + tenor bucketing."""
        if self.tbills_data is None:
            raise RuntimeError("Call load_data() before prepare_data().")

        for name, df in [("tbills", self.tbills_data), ("bonds", self.bonds_data)]:
            violations = find_monotonic_violations(df)
            if not violations.empty:
                logger.warning("Monotonic violations detected in %s:\n%s", name, violations)

        self.tbill_df = enforce_monotonic_isotonic(self.tbills_data)
        self.bond_df = enforce_monotonic_isotonic(self.bonds_data)
        if self.export_tbills:
            self.tbill_df.to_excel(f"{ensure_export_dir(self.tbills_export_dir)}/tbills_df.xlsx", index=False)
        if self.export_bonds:
            self.bond_df.to_excel(f"{ensure_export_dir(self.bonds_export_dir)}/bonds_df.xlsx", index=False)

        self.deposit_bkt = bucket_and_merge(self.deposit_rates_data, tenor_col="Tenor", yield_col="Yield")
        self.tbill_bkt = bucket_and_merge(self.tbills_data, tenor_col="Tenor", yield_col="Yield")
        self.bond_bkt = bucket_and_merge(self.bonds_data, tenor_col="Tenor", yield_col="Yield")
        if self.export_deposits:
            self.deposit_bkt.to_excel(f"{ensure_export_dir(self.deposits_export_dir)}/deposit_bkt_{model_version}.xlsx", index=False)
        if self.export_tbills:
            self.tbill_bkt.to_excel(f"{ensure_export_dir(self.tbills_export_dir)}/tbill_bkt_{model_version}.xlsx", index=False)
        if self.export_bonds:
            self.bond_bkt.to_excel(f"{ensure_export_dir(self.bonds_export_dir)}/bond_bkt_{model_version}.xlsx", index=False)

        return self

    def print_specs(self):
        print("\nMarket datasets")
        print("---------------------------")
        print(f"T-Bill windows : {list(self.tbill_sets.keys()) if self.tbill_sets else None}")
        print(f"Bond windows   : {list(self.bond_sets.keys()) if self.bond_sets else None}")
        n_dep = len(self.deposit_rates_data) if self.deposit_rates_data is not None else 0
        print(f"Deposits       : {n_dep} observations")


## VISUAL HELPER


def graph_tbill_deposit(tenor_grid, tbill_curve, deposit_curve, tbill_obs, deposit_obs):
    plt.figure(figsize=(12, 4))
    plt.plot(tenor_grid, tbill_curve, label='Interpolated T-Bill Curve', color='C0')
    plt.plot(tenor_grid, deposit_curve, label='Interpolated Deposit Curve', color='C3')

    # Only show scatter points up to 1 year
    plt.scatter(tbill_obs['Tenor'], tbill_obs['Yield'], label='T-Bill Observed Yields', color='C0', marker='o')
    plt.scatter(deposit_obs['Tenor'], deposit_obs['Yield'], label='Deposit Observed Yields', color='C3', marker='x')

    plt.title('T-Bill vs Deposit Rate Curves (≤ 1 Year)')
    plt.xlabel('Tenor (Years)')
    plt.ylabel('Yield (%)')
    plt.xlim(0.0, 1.05)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.show()

    return


def graph_fit_obs(t_grid_long, tenors_base, yields_base, base_term, fit=''):
    plt.figure(figsize=(12,5))
    # Base NS
    plt.plot(t_grid_long, base_term, '-o',
              label='Base Term Curve', color='C0')

    # Market input
    # plt.scatter(tenors_base, yields_base, '-o', label='Actual Yields', color='C0', alpha=0.5)
    plt.scatter(tenors_base, yields_base, label='Actual Yields', color='C0', linewidth=2, marker='x', s=50, alpha=0.8)
    plt.xlabel("Tenor (yrs)"); plt.ylabel("Yield (%)")
    plt.legend(); plt.grid(True);

    if fit == 'NS':
        plt.title("Base Yield Curves (NS vs Observed Input)")
    if fit == 'NSS':
        plt.title("Base Yield Curves (NSS vs Observed Input)")

    plt.show()

    return


def show_ns_params(ns_params_base_asset, sse, rmse):
    b0, b1, b2, tau = ns_params_base_asset
    print("=== Asset Base NS Parameters ===")
    print(f"β₀   = {b0:.4f}%   (long-term level)")
    print(f"β₁   = {b1:.4f}    (short-term slope)")
    print(f"β₂   = {b2:.4f}    (mid-curve curvature)")
    print(f"τ    = {tau:.4f} yrs (hump decay)")
    print(f"SSE  = {sse:.4f}, RMSE = {rmse:.4f}%")

    return


def graph_ns_shift_yields(t_grid_long, tenors_base, yields_base, base_term, base_term_shifted):
    """
    Assume the following variables are already defined:
    t_grid_long : 1D array of fine tenors for plotting (e.g. np.linspace(0.01,30,300))
    base_term : NS-fitted base‐term yields on t_grid_long
    base_term_shifted : Deposit-DF backsolved (shifted) base‐term yields on t_grid_long
    tenors_base, yields_base : the 3-way input points (Tenor, Yield) used for the NS fit
    """

    plt.figure(figsize=(10,6))

    # 1) NS-fitted base term curve
    plt.plot(t_grid_long, base_term,
    label='NS-Fitted Base Term', color='C0', linewidth=2)

    # 2) Deposit-DF shifted base term
    plt.plot(t_grid_long, base_term_shifted,
    label='Deposit-DF Shifted Base Term', color='C1', linestyle='--', linewidth=2)

    # 3) 3-way input yield points (T-bill, deposit-proxy, bond)
    plt.scatter(tenors_base, yields_base,
    label='TBill-Bond-DepositProxy Yields', color='black', marker='o', s=50, alpha=0.7)


    plt.title('FTP Base Term: NS Fit vs Deposit-DF Shift vs 3-Way Input Yields')
    plt.xlabel('Tenor (years)')
    plt.ylabel('Yield')
    plt.xlim(0, max(t_grid_long))
    plt.ylim(min(min(base_term_shifted), min(yields_base)) * 0.95,
    max(max(base_term), max(yields_base)) * 1.05)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.show()

    return


def graph_base(t_grid_long, tenors_base, yields_base, base_term_shifted, asset_on_curve, asset_on_rate):
    plt.figure(figsize=(12,6))

    plt.plot(t_grid_long, base_term_shifted, '--', label='Shifted by 3 M Δ', color='C2')

    plt.plot(t_grid_long, asset_on_curve,   label='Asset ON (Repo Rate)',   color='C4', linestyle='-')
    plt.scatter([0.01], [asset_on_rate], color='black', zorder=5)
    plt.text(0.02, asset_on_rate + 0.1, f'Repo {asset_on_rate:.2f}%', fontsize=8)

    plt.xlabel("Tenor (years)")
    plt.ylabel("Yield")
    plt.title("Base Term Curve Shifted by 3 M Deposit–T-Bill Spread")
    plt.legend()
    plt.grid(True)
    plt.show()

    return


def full_ftp_graph(t_grid_long, base_term_shifted, ftp_rate):
    plt.figure(figsize=(14,6))
    plt.plot(t_grid_long, base_term_shifted*100,        label='Base Term (%)', color='C1')
    plt.plot(t_grid_long, ftp_rate*100,         label='FTP = Base+LP (%)', color='C0', linestyle='--')
    plt.fill_between(t_grid_long,
                     base_term_shifted*100,
                     ftp_rate*100,
                     color='C0', alpha=0.3,
                     label='Liquidity Layer')

    plt.xlabel('Tenor (yrs)')
    plt.ylabel('Rate / Spread (%)')
    plt.title('Liquidity Premium: 3-Point NS Fit')
    plt.legend(); plt.grid(True); plt.show()



def check_limits(tenors, t_grid, term_curve, prime_rate, reg_cap):
    for T in tenors:
        y_val = np.interp(T, t_grid, term_curve)
        print(f"FTP({T:.2f} yr) = {y_val:.2f}%", end='')
        if y_val > prime_rate and T<=10:
            print(f"  ⚠️ exceeds prime rate [{prime_rate}%]")
        elif y_val > reg_cap:
            print(f"  ⚠️ exceeds regulatory cap [{reg_cap}%]")
        else:
            print(f"  ✔ within limits. prime rate {prime_rate}% & regulation cap {reg_cap}% not exceeded.")


## CURVE CONSTRUCTION


def ns_yield(t, beta0, beta1, beta2, tau):
    """
    Numerically stable Nelson-Siegel yield.
    """
    tau = max(float(tau), 1e-6)
    t = np.asarray(t, dtype=float)
    x = np.clip(t / tau, 1e-6, 1e6)
    exp_x = np.exp(-x)
    term1 = (1 - exp_x) / x
    term2 = term1 - exp_x
    return beta0 + beta1 * term1 + beta2 * term2

def ns_curve(tenors, params):
    """
    Vectorized NS curve evaluation.
    """
    params = np.asarray(params, dtype=float)
    tenors = np.asarray(tenors, dtype=float)
    beta0, beta1, beta2, tau = params
    return ns_yield(
        np.asarray(tenors, dtype=float),
        beta0,
        beta1,
        beta2,
        tau
    )

def ns_cost(params, tenors, yields_obs, weights=None):
    y_pred = ns_curve(tenors, params)
    w = weights if weights is not None else np.ones_like(yields_obs)
    return np.sum(w * (yields_obs - y_pred)**2)

def fit_ns(
    tenors,
    yields,
    weights=None,
    bounds=None,
    init_center=None,
    starts=10,
    constraints=None,
    visualize=False,
    custom_cost=None,
    custom_cost_factory=None,
    tol=1e-6,
    progress_interval=5,
    verbose=True
):
    tenors = np.asarray(tenors, dtype=float)
    yields = np.asarray(yields, dtype=float)
    if weights is None:
        weights = np.ones_like(yields)
    weights = np.asarray(weights, dtype=float)
    
    init_center = np.asarray(init_center, dtype=float).ravel() if init_center is not None else None
    if init_center is None:
        print("init_center must be provided for robust LP/base fitting.")
        init_center = np.array([
            np.mean(yields),
            0.0,
            0.0,
            np.median(tenors[tenors > 0]) if np.any(tenors > 0) else 1.0
        ], dtype=float)
    
    # -----------------------------------------
    # Build objective function
    # -----------------------------------------
    if custom_cost_factory is not None:
        objective_fn = custom_cost_factory()
    elif custom_cost is not None:
        objective_fn = custom_cost
    else:
        objective_fn = ns_cost
    def objective(p):
        p = np.asarray(
            p,
            dtype=float
        ).reshape(-1)
        return objective_fn(
            params=p,
            tenors=tenors,
            yields_obs=yields,
            weights=weights
        )
    # -----------------------------------------
    # Start generator
    # -----------------------------------------
    def gen_start(center):
        center = np.asarray(center, dtype=float).ravel()
        perturbs = np.array([0.15, 0.25, 0.25, 0.40])
        out = []
        for i, p in enumerate(center):
            if not np.isfinite(p):
                raise ValueError("init_center contains non-finite values")
            scale = np.random.uniform(
                1 - perturbs[i],
                1 + perturbs[i]
            )
            out.append(float(p) * float(scale))
        return out

    # -----------------------------------------
    # Unique initializations
    # ----------------------------------------- 
    inits = []
    while len(inits) < starts:
        init = tuple(gen_start(init_center))
        if not any(
            np.allclose(init, prev, atol=tol)
            for prev in inits
        ):
            inits.append(init)
    # -----------------------------------------
    # Optimization loop
    # -----------------------------------------
    best_sse = np.inf
    best_params = None
    successful_starts = 0
    for idx, init in enumerate(inits, start=1):
        try:
            res = minimize(
                objective,
                x0=init,
                method='SLSQP',
                bounds=bounds,
                constraints=constraints or []
            )
            if res.success:
                successful_starts += 1
                if res.fun < best_sse:
                    best_sse = res.fun
                    best_params = res.x
            if verbose:
                if (
                    idx % progress_interval == 0
                    or idx == starts
                ):
                    print(
                        f"[{idx}/{starts}] "
                        f"Success={successful_starts} | "
                        f"Best SSE={best_sse:.8f}"
                    )
        except Exception as e:
            if verbose:
                print(f"Start {idx} failed: {e}")
    # -----------------------------------------
    # Final validation
    # -----------------------------------------
    if best_params is None:
        raise RuntimeError(
            "NS fitting failed to converge."
        )
    # -----------------------------------------
    # Final fitted curve
    # -----------------------------------------
    y_fit = ns_curve(tenors, best_params)
    diagnostics = build_ns_diagnostics(
        tenors=tenors,
        yields_obs=yields,
        yields_fit=y_fit,
        best_params=best_params,
        best_sse=best_sse,
        successful_starts=successful_starts,
        total_starts=starts
    )
    # -----------------------------------------
    # Visualization
    # -----------------------------------------
    if visualize:
        residuals = yields - y_fit
        plt.figure(figsize=(12, 5))
        plt.scatter(
            tenors,
            yields,
            label='Observed'
        )
        plt.plot(
            tenors,
            y_fit,
            label=f'NS Fit (RMSE={diagnostics["rmse"]:.6f})'
        )
        plt.title("Nelson-Siegel Fit")
        plt.xlabel("Tenor")
        plt.ylabel("Yield")
        plt.grid()
        plt.legend()
        plt.show()
        plt.figure(figsize=(12, 5))
        plt.stem(
            tenors,
            residuals,
            basefmt=" "
        )
        plt.title("Residuals")
        plt.xlabel("Tenor")
        plt.ylabel("Obs - Fit")
        plt.grid()
        plt.show()
    return (
        best_params,
        diagnostics["sse"],
        diagnostics["rmse"],
        diagnostics
    )


def build_ns_diagnostics(
    tenors,
    yields_obs,
    yields_fit,
    best_params,
    best_sse,
    successful_starts,
    total_starts
):
    """
    NS fit diagnostics.
    """
    residuals = yields_obs - yields_fit
    rmse = np.sqrt(
        np.mean(residuals**2)
    )
    mae = np.mean(
        np.abs(residuals)
    )
    max_abs_error = np.max(
        np.abs(residuals)
    )
    diagnostics = {
        "params": best_params,
        "sse": best_sse,
        "rmse": rmse,
        "mae": mae,
        "max_abs_error": max_abs_error,
        "successful_starts": successful_starts,
        "failed_starts": total_starts - successful_starts,
        "residuals": residuals
    }
    return diagnostics




def monotonicity_penalty(
    curve,
    lambda_mono=10.0,
):
    """
    Penalize downward slope violations.
    Only penalizes:
        curve[i] > curve[i+1]
    """
    curve = np.asarray(curve, dtype=float)
    diffs = np.diff(curve)
    violations = np.maximum(-diffs, 0.0)
    return lambda_mono * np.sum(
        violations**2
    )


def make_penalized_ns_cost(
    lambda_level=0.0005,
    lambda_curvature=0.006,
    lambda_mono=30.0,
):
    """
    Returns a fully configured cost function.
    """
    def cost(
        params,
        tenors,
        yields_obs,
        weights=None,
    ):
        params = np.asarray(
            params,
            dtype=float
        ).reshape(-1)
        tenors = np.asarray(
            tenors,
            dtype=float
        )
        yields_obs = np.asarray(
            yields_obs,
            dtype=float
        )
        y_fit = np.asarray(
            ns_curve(tenors, params),
            dtype=float
        )
        resid = yields_obs - y_fit
        if weights is None:
            weights = np.ones_like(resid)
        weights = np.asarray(
            weights,
            dtype=float
        )
        mse = np.sum(
            weights * resid**2
        ) / (np.sum(weights) + 1e-12)
        # -----------------------------
        # Level penalty
        # -----------------------------
        level_penalty = lambda_level * np.mean(y_fit**2)
        # -----------------------------
        # Curvature penalty
        # -----------------------------
        beta2 = params[2]
        curvature_penalty = lambda_curvature * beta2**2
        # -----------------------------
        # Monotonicity penalty
        # -----------------------------
        mono_penalty = monotonicity_penalty(
            y_fit,
            lambda_mono=lambda_mono
        )
        return mse + level_penalty + curvature_penalty + mono_penalty
    return cost



def printout_ftprates(t_grid_long, base_term_shifted, lp_dec_grid, ftp_rate):
    print("")
    for t_target in [0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 7.5, 10.0, 20.0, 30.0]:
        idx = np.argmin(np.abs(t_grid_long - t_target))
        print(f"At t={t_target:.2f} → LP = {lp_dec_grid[idx]:.3%}, Base = {base_term_shifted[idx]:.6%}, FTP = {ftp_rate[idx]:.6%}")
    print("")


## -------- BASE --------

def build_ns_bounds_base(
    tenors,
    yields,
    level_buffer_mult=1.5,
    slope_mult=2.0,
    curvature_mult=2.0,
    tau_min=0.05,
    tau_max_mult=2.0
):
    """
    Data-driven NS bounds for BASE curve fitting.
    """
    tenors = np.asarray(tenors, dtype=float)
    yields = np.asarray(yields, dtype=float)
    y_min = np.min(yields)
    y_max = np.max(yields)
    y_range = max(y_max - y_min, 0.005)
    beta0_bounds = (
        max(0.0, y_min - y_range),
        y_max + level_buffer_mult * y_range
    )
    beta1_bounds = (
        -slope_mult * y_range,
        slope_mult * y_range
    )
    beta2_bounds = (
        -curvature_mult * y_range,
        curvature_mult * y_range
    )
    tau_bounds = (
        tau_min,
        max(tenors) * tau_max_mult
    )
    return [
        beta0_bounds,
        beta1_bounds,
        beta2_bounds,
        tau_bounds
    ]


def build_ns_init_base(
    tenors,
    yields
):
    """
    Data-driven NS initialization for BASE curve.
    """
    tenors = np.asarray(tenors, dtype=float)
    yields = np.asarray(yields, dtype=float)
    sort_idx = np.argsort(tenors)
    tenors = tenors[sort_idx]
    yields = yields[sort_idx]
    # -----------------------------
    # Long-run level
    # -----------------------------
    long_end_n = min(3, len(yields))
    beta0_init = np.mean(yields[-long_end_n:])
    # -----------------------------
    # Short-end slope
    # -----------------------------
    beta1_init = yields[0] - beta0_init
    # -----------------------------
    # Curvature estimate
    # -----------------------------
    mid_idx = np.argmin(
        np.abs(tenors - np.median(tenors))
    )
    beta2_init = (
        yields[mid_idx]
        - 0.5 * (yields[0] + beta0_init)
    )
    # -----------------------------
    # Decay factor
    # -----------------------------
    tau_init = np.median(tenors)
    return [
        beta0_init,
        beta1_init,
        beta2_init,
        tau_init
    ]


def build_ns_weights_base(
    tenors,
    short_end_boost=1.5,
    belly_boost=1.2,
    long_end_decay=True
):
    """
    Base curve weighting scheme.
    """
    tenors = np.asarray(tenors, dtype=float)
    weights = np.ones_like(tenors)
    # -----------------------------
    # Short-end emphasis
    # -----------------------------
    weights[tenors <= 1.0] *= short_end_boost
    # -----------------------------
    # Belly support
    # -----------------------------
    weights[
        (tenors > 1.0) &
        (tenors <= 5.0)
    ] *= belly_boost
    # -----------------------------
    # Reduce sparse tail dominance
    # -----------------------------
    if long_end_decay:
        weights *= 1 / (1 + 0.05 * tenors)
    return weights



## -------- LIQUIDITY PREMIUM --------


def build_lp_weights(
    tenors,
    short_end_strength=2.5,
    belly_strength=1.5,
    long_end_strength=0.8
):
    """
    LP-specific weighting structure.
    Goals:
    - stabilize short end near zero
    - preserve belly dynamics
    - avoid unstable long-end extrapolation
    """
    tenors = np.asarray(tenors, dtype=float)
    weights = np.ones_like(tenors)
    # Short-end stabilization
    weights[tenors <= 1] *= short_end_strength
    # Belly support
    weights[(tenors > 1) & (tenors <= 5)] *= belly_strength
    # Slight long-end dampening
    weights[tenors > 5] *= long_end_strength
    return weights

def build_lp_init(
    tenors,
    observed_lp
):
    """
    Data-driven initialization for LP NS fit.
    """
    tenors = np.asarray(tenors, dtype=float)
    observed_lp = np.asarray(observed_lp, dtype=float)
    short_avg = np.mean(observed_lp[tenors <= 1])
    long_avg = np.mean(observed_lp[tenors >= 5])
    beta0 = max(long_avg, 0.001)
    beta1 = short_avg - beta0
    beta2 = max(
        np.max(observed_lp) - beta0,
        0.001
    )
    tau = 2.0
    return [beta0, beta1, beta2, tau]


def build_lp_bounds(
    max_long_end_lp=0.005
):
    """
    LP-specific NS parameter bounds.
    LP expected:
    - small magnitude
    - smooth
    - positive
    """
    return [
        (0.0000, max_long_end_lp),   # beta0
        (-0.500, 0.500),           # beta1
        (-0.500, 0.500),           # beta2
        (0.25, 10.0)                 # tau
    ]


def build_lp_anchor_constraints(
    short_tolerance=0.005
):
    """
    Enforces near-zero LP at very short tenor.
    """
    return [
        {
            'type': 'ineq',
            'fun': lambda p:
                short_tolerance - abs(ns_yield(0.1, *p))
        }
    ]


def audit_and_standardise_yields(yields, label="series"):
    """
    Ensures yields are in DECIMAL form internally.
    Flags likely unit issues.
    """
    y = np.array(yields, dtype=float)
    max_y = np.nanmax(np.abs(y))
    # Heuristic checks
    if max_y > 1.0:
        logger.warning(
            "[UNIT WARNING] %s: values > 1 detected. Likely in %% form. Converting to decimals.",
            label,
        )
        y = y / 100.0
    elif max_y < 0.001:
        logger.warning(
            "[UNIT WARNING] %s: values extremely small. Possibly already scaled or double-converted.",
            label,
        )
    # sanity range check for banking yields
    if np.any(y < -0.05) or np.any(y > 0.25):
        logger.warning("[DATA WARNING] %s: values outside typical yield range.", label)
    return y


def liquidity_stress(t, alpha=0.4, tau_s=2.5):
    """
    Multiplicative liquidity stress factor.
    Parameters
    ----------
    t : array-like
        Tenors in years.
    alpha : float
        Maximum proportional uplift.
        Example:
            alpha = 0.40 → max +40%
    tau_s : float
        Stress build-up horizon in years.
    """
    t = np.asarray(t, dtype=float)
    return 1.0 + alpha * (
        1.0 - np.exp(-t / tau_s)
    )


def winsorize_lp(
    lp,
    lower_q=0.00,
    upper_q=0.98,
):
    """
    Winsorize LP observations to reduce
    extreme outlier influence.
    Parameters
    ----------
    lp : array-like
        Liquidity premium observations (decimals).
    lower_q : float
        Lower quantile.
    upper_q : float
        Upper quantile.
    Returns
    -------
    np.ndarray
    """
    lp = np.asarray(lp, dtype=float).ravel()
    lower = np.quantile(lp, lower_q)
    upper = np.quantile(lp, upper_q)
    return np.clip(lp, lower, upper)



def compute_structural_lp(
    tenors,
    market_deposit_yields,
    base_curve_yields,
    floor_at_zero=True,
    apply_winsorize=True,
    winsor_upper_q=0.98,
):
    """
    Structural LP extraction.
    This is the clean economic liquidity spread:
        deposit yield - base curve yield
    No stress adjustment is applied here.
    """
    t = np.asarray(tenors, dtype=float).ravel()
    m = audit_and_standardise_yields(
        market_deposit_yields,
        "market_deposits"
    )
    b = audit_and_standardise_yields(
        base_curve_yields,
        "base_curve"
    )
    m = np.asarray(m, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    if not (len(t) == len(m) == len(b)):
        raise ValueError(
            "tenors, market_deposit_yields, and "
            "base_curve_yields must have same length."
        )
    structural_lp = m - b
    # -------------------------------------------------
    # Optional winsorization
    # -------------------------------------------------
    if apply_winsorize:
        structural_lp = winsorize_lp(
            structural_lp,
            upper_q=winsor_upper_q
        )
    # -------------------------------------------------
    # Optional floor at zero
    # -------------------------------------------------
    if floor_at_zero:
        structural_lp = np.maximum(
            structural_lp,
            0.0
        )
    return structural_lp


def compute_stress_lp(
    tenors,
    structural_lp,
    alpha=0.3,
    tau_s=2.5,
    max_stress_bps=75.0,
):
    """
    Compute capped liquidity stress overlay.
    Stress is treated as a scenario layer,
    NOT part of the structural LP.
    Parameters
    ----------
    structural_lp : array-like
        Structural LP in decimals.
    max_stress_bps : float or None
        Maximum additional stress contribution.
        Example:
            75 bps = 0.75%
    """
    t = np.asarray(tenors, dtype=float).ravel()
    structural_lp = np.asarray(
        structural_lp,
        dtype=float
    ).ravel()
    if len(t) != len(structural_lp):
        raise ValueError(
            "tenors and structural_lp must match."
        )
    stress_factor = liquidity_stress(
        t,
        alpha=alpha,
        tau_s=tau_s
    )
    # -------------------------------------------------
    # Stress contribution only
    # -------------------------------------------------
    stress_lp = structural_lp * (
        stress_factor - 1.0
    )
    # -------------------------------------------------
    # Cap ONLY the stress component
    # -------------------------------------------------
    if max_stress_bps is not None:
        max_stress_dec = bps_to_decimal(max_stress_bps)
        stress_lp = np.minimum(
            stress_lp,
            max_stress_dec
        )
    return stress_lp


def build_observed_lp(
    tenors,
    market_deposit_yields,
    base_curve_yields,
    apply_stress=False,
    stress_alpha=0.3,
    stress_tau_s=2.5,
    max_stress_bps=75.0,
    floor_at_zero=True,
    apply_winsorize=True,
    winsor_upper_q=0.98,
    apply_isotonic=False,
    verbose=True,
):
    """
    Full LP decomposition pipeline.
    Final LP:
        structural LP + capped stress overlay
    """
    t = np.asarray(tenors, dtype=float).ravel()
    # -------------------------------------------------
    # Structural LP
    # -------------------------------------------------
    structural_lp = compute_structural_lp(
        tenors=t,
        market_deposit_yields=market_deposit_yields,
        base_curve_yields=base_curve_yields,
        floor_at_zero=floor_at_zero,
        apply_winsorize=apply_winsorize,
        winsor_upper_q=winsor_upper_q,
    )
    # -------------------------------------------------
    # Stress overlay
    # -------------------------------------------------
    if apply_stress:
        stress_lp = compute_stress_lp(
            tenors=t,
            structural_lp=structural_lp,
            alpha=stress_alpha,
            tau_s=stress_tau_s,
            max_stress_bps=max_stress_bps,
        )
    else:
        stress_lp = np.zeros_like(structural_lp)
    # -------------------------------------------------
    # Total LP
    # -------------------------------------------------
    total_lp = structural_lp + stress_lp
    # -------------------------------------------------
    # Diagnostics dataframe
    # -------------------------------------------------
    df = pd.DataFrame({
        "Tenor": t,
        "Structural_LP": structural_lp,
        "Stress_LP": stress_lp,
        "Total_LP": total_lp,
    })
    # -------------------------------------------------
    # Optional monotonic smoothing
    # IMPORTANT:
    # applied LAST
    # -------------------------------------------------
    if apply_isotonic:
        iso_df = enforce_monotonic_isotonic(
            df.rename(
                columns={"Total_LP": "Yield"}
            )
        )
        total_lp = iso_df["Yield"].values
        df["Total_LP_Monotonic"] = total_lp
    # -------------------------------------------------
    # Logging
    # -------------------------------------------------
    if verbose:
        print("\nObserved LP Components")
        print("-" * 70)
        for row in df.itertuples():
            print(
                f"{row.Tenor:>5.2f}Y | "
                f"Structural={row.Structural_LP*100:>6.3f}% | "
                f"Stress={row.Stress_LP*100:>6.3f}% | "
                f"Total={row.Total_LP*100:>6.3f}%"
            )
        print("\n\n")
            
    #df.to_excel(f"export/lp_obs_{model_version}.xlsx", index=False)
    
    return df


class CurveModelObject:
    """
    Builds one FTP curve (base term -> 3m/scenario shift -> liquidity
    premium -> full FTP) for a given DataObject + CurveConfig.

    One instance = one scenario. Stress-testing +/-100bps vs base is just
    constructing multiple CurveModelObjects that share the same DataObject
    but use configs with different `parallel_shift_bps`. Set
    `config.visualize = False` to skip all plotting/console output for
    fast, batch-friendly scenario runs. Set `config.apply_3m_shift = False`
    to build the curve without the deposit-vs-3m-T-bill observability
    shift (the spread is still computed and logged either way).
    """

    def __init__(self, data: DataObject, config: CurveConfig):
        self.data = data
        self.config = config

        # short-end spread
        self.dep_diff = None
        self.shift_3m = None
        self.shift_3m_applied = None

        # base curve
        self._tenors_base = None
        self._yields_base = None
        self.ns_params_base = None
        self.base_diag = None
        self.base_term = None
        self.base_term_shifted = None

        # liquidity premium curve
        self.obs_lp_df = None
        self.ns_params_lp = None
        self.lp_diag = None
        self.lp_dec_grid = None

        # combined output
        self.ftp_rate = None
        self.ftp_df = None

    # ---- step 1: 3-month deposit vs t-bill spread ----
    def fit_short_end_spread(self):
        cfg = self.config
        tbill_obs = self.data.tbill_bkt[self.data.tbill_bkt["Tenor"] <= 1.0]
        deposit_obs = self.data.deposit_bkt[self.data.deposit_bkt["Tenor"] <= 1.0]

        tbill_interp = PchipInterpolator(tbill_obs["Tenor"], tbill_obs["Yield"], extrapolate=False)
        deposit_interp = PchipInterpolator(deposit_obs["Tenor"], deposit_obs["Yield"], extrapolate=False)

        t_grid_10 = cfg.t_grid_10
        tbill_grid = np.array([0.25, 0.5, 0.75, 1.0])
        tbill_rates = tbill_interp(tbill_grid)
        dep_rates = deposit_interp(tbill_grid)

        self.dep_diff = (dep_rates - tbill_rates) * 100

        if cfg.visualize:
            tbill_curve = tbill_interp(t_grid_10)
            deposit_curve = deposit_interp(t_grid_10)
            graph_tbill_deposit(t_grid_10, tbill_curve, deposit_curve, tbill_obs, deposit_obs)

            labels = ["3 M    (0.25 yr)", "6 M    (0.50 yr)", "9 M    (0.75 yr)", "12 M   (1.00 yr)"]
            for lbl, diff in zip(labels, self.dep_diff):
                print(f"[Deposit - T Bill]  @ {lbl} = {diff:.2f}% ({diff*100:.2f} bps)")

        self.shift_3m = self.dep_diff[0] / 100.0
        return self.shift_3m

    # ---- step 2: base NS curve fit ----
    def fit_base_curve(self):
        cfg, d = self.config, self.data
        tbills_for_base = d.tbill_df[d.tbill_df["Tenor"] <= cfg.tbill_cutoff]
        bonds_for_base = d.bond_df[(d.bond_df["Tenor"] >= cfg.tbill_cutoff) & (d.bond_df["Tenor"] <= cfg.bond_cutoff)]

        base_df = pd.DataFrame({
            "Tenor": np.concatenate([tbills_for_base["Tenor"].to_numpy(), bonds_for_base["Tenor"].to_numpy()]),
            "Yield": np.concatenate([tbills_for_base["Yield"].to_numpy(), bonds_for_base["Yield"].to_numpy()]),
        }).sort_values("Tenor").reset_index(drop=True)

        # Reuses the same unit-detection/standardisation logic the LP
        # pipeline uses (cell 22), instead of a separately-maintained
        # median>1.0 check that only handled the "already decimal" case.
        base_df["Yield"] = audit_and_standardise_yields(base_df["Yield"], label="base_df")

        tenors_base = base_df["Tenor"].to_numpy()
        yields_base = base_df["Yield"].to_numpy()

        bounds_ns = build_ns_bounds_base(tenors_base, yields_base)
        init_ns = build_ns_init_base(tenors_base, yields_base)
        weights_ns = build_ns_weights_base(tenors_base)
        base_cost_factory = lambda: make_penalized_ns_cost(
            lambda_level=cfg.base_lambda_level,
            lambda_curvature=cfg.base_lambda_curvature,
            lambda_mono=cfg.base_lambda_mono,
        )

        (self.ns_params_base, sse_base, rmse_base, self.base_diag) = fit_ns(
            tenors=tenors_base,
            yields=yields_base,
            weights=weights_ns,
            bounds=None,
            init_center=init_ns,
            starts=cfg.base_starts,
            constraints=None,
            visualize=cfg.visualize,
            custom_cost_factory=base_cost_factory,
        )
        self.base_term = ns_curve(cfg.t_grid_long, self.ns_params_base)

        if cfg.visualize:
            show_ns_params(self.ns_params_base, sse_base, rmse_base)
            graph_fit_obs(cfg.t_grid_long, tenors_base, yields_base, self.base_term)

        self._tenors_base, self._yields_base = tenors_base, yields_base
        return self.base_term

    # ---- step 3: apply 3m spread shift + scenario parallel shock ----
    def apply_shifts(self):
        cfg = self.config
        if self.shift_3m is None:
            self.fit_short_end_spread()
        if self.base_term is None:
            self.fit_base_curve()

        self.shift_3m_applied = cfg.apply_3m_shift
        applied_3m_shift_dec = self.shift_3m if cfg.apply_3m_shift else 0.0
        self.base_term_shifted = self.base_term + applied_3m_shift_dec + cfg.parallel_shift_dec

        # Not gated by cfg.visualize - whether the 3m shift was actually
        # applied is a governance-relevant modelling choice, not just a
        # diagnostic, so it always lands in the log regardless of how
        # noisy/quiet this run is otherwise.
        logger.info(
            "3m observability shift: %s (computed %.4f%%, %.4f%% actually applied)",
            "applied" if cfg.apply_3m_shift else "SKIPPED",
            self.shift_3m * 100,
            applied_3m_shift_dec * 100,
        )

        if cfg.visualize:
            base_term_pct = self.base_term * 100
            base_term_shifted_pct = self.base_term_shifted * 100
            ftp_plot_df = pd.DataFrame({
                "Tenor": cfg.t_grid_long,
                "Base Term (%)": base_term_pct,
                "Shifted Base Term (%)": base_term_shifted_pct,
            })
            ftp_plot_bkt = bucket_and_merge(ftp_plot_df, tenor_col="Tenor", yield_col="Shifted Base Term (%)")
            if cfg.export_base_curve:
                ftp_plot_bkt.to_excel(f"{ensure_export_dir(cfg.export_dir)}/ftp_base_df_{cfg.model_version}.xlsx", index=False)

            graph_ns_shift_yields(cfg.t_grid_long, self._tenors_base, self._yields_base, base_term_pct, base_term_shifted_pct)
            check_limits(cfg.check_tenors, cfg.t_grid_long, base_term_shifted_pct, cfg.prime_rate, cfg.reg_cap)
        elif cfg.export_base_curve:
            # Export still happens headless - only the plot/console report is skipped.
            base_term_shifted_pct = self.base_term_shifted * 100
            ftp_plot_df = pd.DataFrame({
                "Tenor": cfg.t_grid_long,
                "Base Term (%)": self.base_term * 100,
                "Shifted Base Term (%)": base_term_shifted_pct,
            })
            ftp_plot_bkt = bucket_and_merge(ftp_plot_df, tenor_col="Tenor", yield_col="Shifted Base Term (%)")
            ftp_plot_bkt.to_excel(f"{ensure_export_dir(cfg.export_dir)}/ftp_base_df_{cfg.model_version}.xlsx", index=False)

        return self.base_term_shifted

    # ---- step 4: liquidity premium curve fit ----
    def fit_liquidity_premium(self):
        cfg = self.config
        if self.base_term_shifted is None:
            self.apply_shifts()

        deposit_df = self.data.deposit_bkt.copy().reset_index(drop=True)
        deposit_yields = deposit_df["Yield"].astype(float).to_numpy()
        deposit_tenors = deposit_df["Tenor"].astype(float).to_numpy()

        base_interp = PchipInterpolator(x=cfg.t_grid_long, y=self.base_term_shifted, extrapolate=True)
        base_yields_at_dep = base_interp(deposit_tenors)

        self.obs_lp_df = build_observed_lp(
            tenors=deposit_tenors,
            market_deposit_yields=deposit_yields,
            base_curve_yields=base_yields_at_dep,
            apply_stress=cfg.apply_lp_stress,
            stress_alpha=cfg.lp_stress_alpha,
            stress_tau_s=cfg.lp_stress_tau_s,
            max_stress_bps=cfg.lp_max_stress_bps,
            apply_isotonic=True,
            verbose=cfg.visualize,
        )
        # See CurveConfig.lp_fit_target docstring: defaults to "Stress_LP"
        # (unchanged behaviour) but is now a named, reviewable choice
        # rather than a hardcoded column lookup.
        obs_lp = self.obs_lp_df[cfg.lp_fit_target]

        bounds_lp = build_lp_bounds(max_long_end_lp=cfg.max_lp_dec)
        init_lp = build_lp_init(deposit_tenors, obs_lp)
        weights_lp = build_lp_weights(
            deposit_tenors, short_end_strength=2.5, belly_strength=1.5, long_end_strength=0.8
        )
        # available if/when anchor constraints are wired back into fit_ns:
        # anchors_lp = build_lp_anchor_constraints(short_tolerance=cfg.lp_short_tolerance)

        lp_cost_factory = lambda: make_penalized_ns_cost(
            lambda_level=cfg.lp_lambda_level,
            lambda_curvature=cfg.lp_lambda_curvature,
            lambda_mono=cfg.lp_lambda_mono,
        )
        (self.ns_params_lp, sse_lp, rmse_lp, self.lp_diag) = fit_ns(
            tenors=deposit_tenors,
            yields=obs_lp,
            weights=weights_lp,
            bounds=bounds_lp,
            init_center=init_lp,
            starts=cfg.lp_starts,
            constraints=None,
            visualize=cfg.visualize,
            custom_cost_factory=lp_cost_factory,
        )
        if cfg.visualize:
            show_ns_params(self.ns_params_lp, sse_lp, rmse_lp)

        self.lp_dec_grid = np.maximum(ns_curve(cfg.t_grid_long, self.ns_params_lp), 0.0)
        return self.lp_dec_grid

    # ---- step 5: combine base + LP into the full FTP curve ----
    def combine_full_curve(self):
        cfg = self.config
        if self.lp_dec_grid is None:
            self.fit_liquidity_premium()

        self.ftp_rate = self.base_term_shifted + self.lp_dec_grid

        if cfg.visualize:
            full_ftp_graph(cfg.t_grid_long, self.base_term_shifted, self.ftp_rate)
            printout_ftprates(cfg.t_grid_long, self.base_term_shifted, self.lp_dec_grid, self.ftp_rate)

        self.ftp_df = pd.DataFrame({
            "Tenor": cfg.t_grid_long,
            "Base_Term": self.base_term_shifted,
            "Liquidity": self.lp_dec_grid,
            "Full_FTP": self.ftp_rate,
        })
        return self.ftp_df

    def run(self):
        """Runs the full pipeline end to end and returns the final ftp_df."""
        self.fit_short_end_spread()
        self.fit_base_curve()
        self.apply_shifts()
        self.fit_liquidity_premium()
        self.combine_full_curve()
        return self.ftp_df


## EXPORTING FILES


def format_tenor_label(t):
    days = int(round(t * DAYS_PER_YEAR))
    if days <= 7:
        return f"{days}D"
    if days < 30:
        weeks = int(round(days / 7))
        return f"{weeks}W"
    if days < 365:
        months = int(round(days / 30))
        return f"{months}M"
    years = int(round(t))
    return f"{years}Y"


def bucket_ftp_curve(
    ftp_df,
    bucket_tenors,
    output_path,
    compounding="annual"
):
    """
    Bucket FTP curve, compute discrete DFs, and export to Excel.

    Parameters
    ----------
    ftp_df : DataFrame
        Must contain: Tenor, Base_Term, Liquidity, Full_FTP (decimals)
    bucket_tenors : list[float]
        Target tenors in years (e.g. [0.25, 0.5, 1, 2, 3, 5, 10])
    output_path : str
        Excel output path
    compounding : str
        'annual' or 'continuous'
    """

    rows = []

    for T in bucket_tenors:
        # nearest tenor on grid
        idx = (ftp_df['Tenor'] - T).abs().idxmin()
        row = ftp_df.loc[idx]

        y = row['Full_FTP']

        # discount factor
        if compounding == "annual":
            df = 1.0 / (1.0 + y) ** T
        elif compounding == "continuous":
            df = np.exp(-y * T)
        else:
            raise ValueError("Unsupported compounding")

        rows.append({
            "Tenor_years": T,
            "Tenor_label": format_tenor_label(T),
            "Base_yield_%": row['Base_Term'] * 100,
            "Liquidity_%": row['Liquidity'] * 100,
            "Full_FTP_%": y * 100,
            "Discount_Factor": df
        })

    out_df = pd.DataFrame(rows)

    out_df = out_df.sort_values("Tenor_years").reset_index(drop=True)
    out_df.to_excel(output_path, index=False)

    logger.info("FTP Curve File has been exported: %s", output_path)

    return out_df




def output_ftp_csv(
    tenors,
    base_term,
    lp_dec,
    ftp_rate,
    out_path,
    compounding="annual"
):
    """
    Export FTP curve to CSV for Excel recording template.

    Parameters
    ----------
    tenors : array-like
        Tenors in years
    base_term : array-like
        Base term curve (decimals)
    lp_dec : array-like
        Liquidity premium (decimals, floored if required)
    ftp_rate : array-like
        Full FTP rate = base + lp (decimals)
    out_path : str
        Output CSV path
    compounding : str
        'annual' or 'continuous'
    """

    tenor_days = np.round(tenors * DAYS_PER_YEAR).astype(int)
    tenor_words = [format_tenor_label(t) for t in tenors]

    # Discount factors
    if compounding == "annual":
        df = 1.0 / (1.0 + ftp_rate) ** tenors
    elif compounding == "continuous":
        df = np.exp(-ftp_rate * tenors)
    else:
        raise ValueError("Unsupported compounding")   
    
    out_df = pd.DataFrame({
        'Tenor_yrs'   : tenors,
        'Tenor_days'    : tenor_days,
        'Tenor_label'   : tenor_words,
        'Base_Term_dec'     : base_term,
        'Liquidity_dec'  : lp_dec,
        'Full_FTP_dec'      : ftp_rate,
        'Discount_Factor': df
    })   

    # Optional reporting columns
    out_df["Base_Term_pct"] = out_df["Base_Term_dec"] * 100
    out_df["Liquidity_pct"] = out_df["Liquidity_dec"] * 100
    out_df["Full_FTP_pct"]  = out_df["Full_FTP_dec"] * 100

    # Sort and export
    out_df = out_df.sort_values("Tenor_yrs").reset_index(drop=True)
    out_df.to_excel(out_path, index=False)

    logger.info("FTP Rates have been exported: %s", out_path)

    return out_df




def ftp_model_summary(
    base_ns_params,
    base_lambda_level,
    base_lambda_curvature,
    base_lambda_mono,
    base_sse,
    base_rmse,
    lp_ns_params,
    lp_lambda_level,
    lp_lambda_curvature,
    lp_lambda_mono,
    max_lp_dec,
    lp_sse,
    lp_rmse,
    lsm_alpha=None,
    lsm_tau_s=None,
    model_name="FTP Base + Liquidity Premium",
    model_version="v1.0",
    run_purpose="Pricing",
    snapshot_window=None,
    tbills_data_cutoff_date=None,
    bonds_data_cutoff_date=None,
    deposits_data_cutoff_date=None,
    curve_currency="USD",
    curve_daycount="ACT/365",
    curve_compounding="Annual",
    df_3m=None,
    df_3m_applied=None,
    out_path="export/ftp_curve_output.xlsx",
):
    """
    Produces a governance-ready FTP model summary table.

    Returns
    -------
    pd.DataFrame with columns:
        Metric/Parameter | Value
    """

    # --- timestamps & environment ---
    run_timestamp = datetime.now()
    run_date = run_timestamp.date().isoformat()
    run_time = run_timestamp.strftime("%H:%M:%S")
    run_user = getpass.getuser()
    run_env = platform.platform()

    # --- unpack NS parameters ---
    base_b0, base_b1, base_b2, base_tau = base_ns_params
    lp_b0, lp_b1, lp_b2, lp_tau = lp_ns_params

    rows = [
        # --- Model identity ---
        ("Model name", model_name),
        ("Model version", model_version),
        ("Run purpose", run_purpose),

        # --- Run governance ---
        ("Model run date", run_date),
        ("Model run time", run_time),
        ("Run by (system user)", run_user),
        ("Execution environment", run_env),

        # --- Data governance ---
        ("Snapshot Window Type", snapshot_window),
        ("Treasury Bills data cutoff date", tbills_data_cutoff_date),
        ("Govenment Bonds data cutoff date", bonds_data_cutoff_date),
        ("Market Deposits data cutoff date", deposits_data_cutoff_date),
        ("Curve currency", curve_currency),
        ("Day count convention", curve_daycount),
        ("Compounding convention", curve_compounding),

        # --- Core discounting ---
        ("3m Shift (dec)", df_3m),
        ("3m Shift Applied", None if df_3m_applied is None else ("Yes" if df_3m_applied else "No")),
        
        # --- NS cost function (Base) ---
        ("Base λ_level", base_lambda_level),
        ("Base λ_curvature", base_lambda_curvature),
        ("Base λ_mono", base_lambda_mono),

        # --- Base curve (NS) ---
        ("Base b_0", base_b0),
        ("Base b_1", base_b1),
        ("Base b_2", base_b2),
        ("Base tau", base_tau),
        ("Base sse", base_sse),
        ("Base rmse", base_rmse),
        
        # --- NS cost function (Liquidty Premium) ---
        ("LP λ_level", lp_lambda_level),
        ("LP λ_curvature", lp_lambda_curvature),
        ("LP λ_mono", lp_lambda_mono),

        # --- Liquidity premium curve (NS) ---
        ("Max LP (dec)", max_lp_dec),
        ("LP b_0", lp_b0),
        ("LP b_1", lp_b1),
        ("LP b_2", lp_b2),
        ("LP tau", lp_tau),
        ("LP sse", lp_sse),
        ("LP rmse", lp_rmse),

        # --- Smoothing / behavioural controls ---
        ("LSM alpha", lsm_alpha),
        ("LSM tau_s", lsm_tau_s),
    ]   
    
    out_df = pd.DataFrame(rows, columns=["Metric/Parameter", "Value"])
    out_df.to_excel(out_path, index=False)

    logger.info("FTP Summary has been exported: %s", out_path)

    return out_df


class ExportObject:
    """
    Writes the Excel outputs the reporting/reference layer consumes, for a
    CurveModelObject that has already been run(). Every scenario gets its
    own file set because ExportObject reads model_version off the model's
    config, so file names never collide across scenarios.
    """

    DEFAULT_BUCKET_TENORS = [
        0, 1/365, 30/365, 60/365, 0.25, 0.5, 0.75, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
        11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
    ]

    def __init__(self, model: CurveModelObject, bucket_tenors=None, compounding="annual", export_dir=None):
        if model.ftp_df is None:
            raise RuntimeError("Call model.run() before exporting.")
        self.model = model
        self.bucket_tenors = bucket_tenors or self.DEFAULT_BUCKET_TENORS
        self.compounding = compounding
        # Defaults to the CurveConfig's export_dir so a scenario run's
        # outputs land wherever its config says to, without repeating the
        # path at every ExportObject(...) call site; pass export_dir=...
        # here to send this particular model's outputs somewhere else.
        self.export_dir = export_dir if export_dir is not None else model.config.export_dir

        self.bucketed_df = None
        self.ftp_curve_csv = None
        self.ftp_summary_df = None

    def export_bucketed_curve(self, out_path=None):
        cfg = self.model.config
        out_path = out_path or f"{ensure_export_dir(self.export_dir)}/Full_FTP_Bucketed_Curve_{cfg.model_version}.xlsx"
        self.bucketed_df = bucket_ftp_curve(
            ftp_df=self.model.ftp_df,
            bucket_tenors=self.bucket_tenors,
            output_path=out_path,
            compounding=self.compounding,
        )
        return self.bucketed_df

    def export_curve_csv(self, out_path=None):
        cfg, m = self.model.config, self.model
        out_path = out_path or f"{ensure_export_dir(self.export_dir)}/ftp_curve_output_{cfg.model_version}.xlsx"
        self.ftp_curve_csv = output_ftp_csv(
            tenors=cfg.t_grid_long,
            base_term=m.base_term_shifted,
            lp_dec=m.lp_dec_grid,
            ftp_rate=m.ftp_rate,
            out_path=out_path,
            compounding=self.compounding,
        )
        return self.ftp_curve_csv

    def export_summary(self, out_path=None):
        cfg, m, d = self.model.config, self.model, self.model.data
        out_path = out_path or f"{ensure_export_dir(self.export_dir)}/ftp_model_output_ver_{cfg.model_version}.xlsx"
        self.ftp_summary_df = ftp_model_summary(
            base_ns_params=m.ns_params_base,
            base_lambda_level=cfg.base_lambda_level,
            base_lambda_curvature=cfg.base_lambda_curvature,
            base_lambda_mono=cfg.base_lambda_mono,
            base_sse=m.base_diag["sse"],
            base_rmse=m.base_diag["rmse"],
            lp_ns_params=m.ns_params_lp,
            lp_lambda_level=cfg.lp_lambda_level,
            lp_lambda_curvature=cfg.lp_lambda_curvature,
            lp_lambda_mono=cfg.lp_lambda_mono,
            max_lp_dec=cfg.max_lp_dec,
            lp_sse=m.lp_diag["sse"],
            lp_rmse=m.lp_diag["rmse"],
            lsm_alpha=cfg.lsm_alpha,
            lsm_tau_s=cfg.lsm_tau_s,
            model_version=cfg.model_version,
            run_purpose=cfg.run_purpose,
            snapshot_window=d.snapshot_window,
            tbills_data_cutoff_date=d.tbills_data_cutoff_date,
            bonds_data_cutoff_date=d.bonds_data_cutoff_date,
            deposits_data_cutoff_date=d.deposits_data_cutoff_date,
            df_3m=m.shift_3m,
            df_3m_applied=m.shift_3m_applied,
            out_path=out_path,
        )
        return self.ftp_summary_df

    def export_all(self, bucketed=True, csv=True, summary=True):
        """
        Writes the requested subset of outputs (all three by default).
        Each is independently optional - e.g.
        `ExportObject(model).export_all(csv=False)` skips just the rates
        CSV/xlsx while still writing the bucketed curve and summary.
        """
        if bucketed:
            self.export_bucketed_curve()
        if csv:
            self.export_curve_csv()
        if summary:
            self.export_summary()
        return self


def run_scenario_suite(data: DataObject, base_config: CurveConfig, shocks_bps=(0, 100, -100), export=True):
    """
    Runs the full curve build once per parallel shock in `shocks_bps`
    (e.g. base / +100bps / -100bps) against the same DataObject, and
    exports each scenario to its own Excel file set.

    Tip: pass a base_config with visualize=False for a fast, quiet batch
    run - each CurveModelObject otherwise plots/prints its full diagnostic
    output for every scenario.

    Returns
    -------
    dict[str, CurveModelObject] keyed by scenario label ("base", "+100bps", "-100bps", ...)
    """
    results = {}
    for bps in shocks_bps:
        sign = "+" if bps > 0 else ("-" if bps < 0 else "")
        label = "base" if bps == 0 else f"{sign}{abs(bps):g}bps"
        cfg = base_config.with_parallel_shift(bps, model_version=f"{base_config.model_version}_{label}")
        model = CurveModelObject(data, cfg)
        model.run()
        results[label] = model
        if export:
            ExportObject(model).export_all()
    return results


def compare_scenarios(scenario_results, tenors_to_check=(0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 20, 30)):
    """
    Builds a Tenor x Scenario comparison table of FTP rates, plus a bps
    delta-vs-base column per non-base scenario. Handy for sanity-checking
    a +/-100bps test run in one glance.
    """
    rows = []
    for label, model in scenario_results.items():
        for t in tenors_to_check:
            val = np.interp(t, model.config.t_grid_long, model.ftp_rate)
            rows.append({"Scenario": label, "Tenor": t, "FTP": val})
    comp_df = pd.DataFrame(rows).pivot(index="Tenor", columns="Scenario", values="FTP")

    if "base" in comp_df.columns:
        for col in list(comp_df.columns):
            if col != "base":
                comp_df[f"{col}_vs_base_bps"] = decimal_to_bps(comp_df[col] - comp_df["base"])
    return comp_df


## LP PARAMETER SWEEP


# CurveConfig fields this sweep is meant for. Restricted deliberately -
# run_lp_parameter_sweep fits the base curve ONCE and reuses it for every
# combination (base curve construction doesn't depend on any LP
# parameter), so sweeping a field that DOES affect the base curve (e.g.
# base_lambda_mono, apply_3m_shift, parallel_shift_bps) would silently
# have no effect on the results - the shared base curve wouldn't be
# re-fit to reflect it. Sweep those via run_scenario_suite / a manual
# loop over full CurveModelObject.run() calls instead.
LP_SWEEPABLE_PARAMS = {
    "apply_lp_stress",
    "lp_stress_alpha",
    "lp_stress_tau_s",
    "lp_lambda_level",
    "lp_lambda_curvature",
    "lp_lambda_mono",
    "lp_max_stress_bps",
    "max_lp_dec",
    "lp_fit_target",
    "lp_starts",
    "lp_short_tolerance",
}


def run_lp_parameter_sweep(
    data: DataObject,
    base_config: CurveConfig,
    param_grid: dict,
    max_combinations: int = 200,
) -> pd.DataFrame:
    """
    Fits the liquidity-premium curve once per combination in the
    Cartesian product of `param_grid`'s values, reusing a single shared
    base-curve fit across all of them (see LP_SWEEPABLE_PARAMS above for
    why only LP-stage fields are supported here).

    Parameters
    ----------
    data : DataObject
        Already load_data()'d and prepare_data()'d.
    base_config : CurveConfig
        Supplies every field NOT being swept (model_version, export_dir,
        apply_3m_shift, non-swept LP params, ...). Its own `visualize` is
        ignored - every run in the sweep is forced quiet regardless, so
        a sweep of any real size doesn't render a plot per combination.
    param_grid : dict[str, list]
        CurveConfig field name -> candidate values, restricted to
        LP_SWEEPABLE_PARAMS (raises ValueError listing the offending
        keys otherwise). E.g. {"lp_stress_alpha": [0.1, 0.3, 0.5],
        "lp_stress_tau_s": [1.5, 2.5, 5.0]}.
    max_combinations : int, default 200
        Safety cap on the Cartesian product size - raises ValueError
        (with the actual count, so you can see how far over you are)
        rather than silently running for hours. Each combination is one
        full liquidity-premium NS fit, so total runtime scales linearly
        with the number of combinations and with `lp_starts`.

    Returns
    -------
    pd.DataFrame, one row per combination: the swept parameter values,
    LP curve summary stats over the full tenor grid (lp_mean, lp_max,
    lp_min, lp_std, lp_range = lp_max - lp_min), and the resulting Full
    FTP rate at 1/5/10/30y for context. `lp_std` is the stability
    measure - lower means a flatter (more stable) liquidity premium
    curve across tenors.
    """
    unsupported = sorted(set(param_grid) - LP_SWEEPABLE_PARAMS)
    if unsupported:
        raise ValueError(
            f"run_lp_parameter_sweep only supports sweeping LP-stage "
            f"fields (the base curve is fit once and shared across every "
            f"combination): {unsupported} would silently have no effect. "
            f"Supported fields: {sorted(LP_SWEEPABLE_PARAMS)}"
        )
    if not param_grid:
        raise ValueError("param_grid is empty - nothing to sweep.")

    keys = list(param_grid.keys())
    combos = list(itertools.product(*(param_grid[k] for k in keys)))
    if len(combos) > max_combinations:
        raise ValueError(
            f"param_grid produces {len(combos)} combinations, over "
            f"max_combinations={max_combinations}. Shrink the grids, or "
            f"raise max_combinations if you really want to run that many "
            f"liquidity-premium fits."
        )

    # Base curve doesn't depend on any LP_SWEEPABLE_PARAMS field, so fit
    # it once here and copy the result onto each combination's model
    # instead of re-running fit_short_end_spread/fit_base_curve/
    # apply_shifts (the expensive, random-restart NS fit) every time.
    ref_cfg = replace(base_config, visualize=False)
    ref_model = CurveModelObject(data, ref_cfg)
    ref_model.fit_short_end_spread()
    ref_model.fit_base_curve()
    ref_model.apply_shifts()

    rows = []
    for combo_values in combos:
        combo = dict(zip(keys, combo_values))
        cfg = replace(base_config, visualize=False, **combo)
        model = CurveModelObject(data, cfg)

        # Reuse the shared base-curve state instead of refitting it.
        model.dep_diff = ref_model.dep_diff
        model.shift_3m = ref_model.shift_3m
        model.shift_3m_applied = ref_model.shift_3m_applied
        model._tenors_base = ref_model._tenors_base
        model._yields_base = ref_model._yields_base
        model.ns_params_base = ref_model.ns_params_base
        model.base_diag = ref_model.base_diag
        model.base_term = ref_model.base_term
        model.base_term_shifted = ref_model.base_term_shifted

        model.fit_liquidity_premium()
        model.combine_full_curve()

        lp = model.lp_dec_grid
        row = dict(combo)
        row.update({
            "lp_mean": lp.mean(),
            "lp_max": lp.max(),
            "lp_min": lp.min(),
            "lp_std": lp.std(),
            "lp_range": lp.max() - lp.min(),
            "ftp_1y": np.interp(1.0, cfg.t_grid_long, model.ftp_rate),
            "ftp_5y": np.interp(5.0, cfg.t_grid_long, model.ftp_rate),
            "ftp_10y": np.interp(10.0, cfg.t_grid_long, model.ftp_rate),
            "ftp_30y": np.interp(30.0, cfg.t_grid_long, model.ftp_rate),
        })
        rows.append(row)

    logger.info("LP parameter sweep: %d combination(s) run.", len(rows))
    return pd.DataFrame(rows)
