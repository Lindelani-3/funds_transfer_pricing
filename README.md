# Funds Transfer Pricing (FTP) Model (sanitised demo)

**Purpose:** Reproducible, technical demonstration of an FTP workflow tuned for a small, thin market. 

**Status:** Public repo holds partial, synthetic, non-operational code and sample data only. Proprietary implementation details, production parameters and full pipelines have been redacted to protect IP.

---

### Project Summary

I led the design and development of Nedbank Eswatini’s first Funds Transfer Pricing (FTP) framework to quantify the internal cost of funds and improve product-level profitability reporting. The model was built using mostly Python and Excel, integrating both internal and external data.

Using isotonic regression and PCHIP smoothing, to ensure monotonicity and continuity, I fitted a yield-curve through the Nelson–Siegel term-structure model and adjusted with behavioral and liquidity-premium factors and computations to capture customer behavior and funding stickiness. Facing notable data challenges like biased internal deposit data because of concessions and inconsistent pricing, and limited market competition, Eswatini’s lack of an overnight index rate, and the tenor-limited activity of the local bond market to name a few. I still ensured collaboration across Treasury, Finance, Risk, and Business; defining unit/product strategies, repricing rules, and governance processes for the bank’s FTP curve management.

Implementing an automated, transparent, and reproducible model that aligns with Basel guidance. The framework now underpins internal pricing, liquidity management, and profitability attribution within the bank.

---

### Repository structure

README.md
ftp_full_showcase.ipynb         # simplified, sanitized demonstration
/data
  treasury_bills_data.csv
  bonds_data.csv
  market_deposits_data.csv
/src
  data.py                  # skeleton: parametric fit & validator
  helper.py                  # skeleton: parametric fit & validator
  curve.py                  # skeleton: parametric fit & validator
  nelsonsiegelfit.py                  # skeleton: parametric fit & validator
/docs
  methodology.md                 # equations, assumptions, governance notes

---

### Methodology (concise, technical)

1.	Preprocessing
	•	Clean raw yields and account snapshots.
	•	Standardise units (continuous annual rates, decimals).
	•	Aggregate yields into defined tenor buckets (3-month increments up to 30 yrs).

2.	Monotonic enforcement & smoothing
	•	Enforce non-decreasing yields with isotonic regression.
	•	Interpolate gaps and reduce wiggle using PCHIP (shape-preserving cubic interpolation).

3.	Base curve construction
	•	Merge T-bills (<1yr) and bonds (≥1yr).
	•	Fit parametric model: Nelson–Siegel
$r(t)=\beta_0 + \beta_1\frac{1-e^{-t/\tau}}{t/\tau} + \beta_2\left(\frac{1-e^{-t/\tau}}{t/\tau}-e^{-t/\tau}\right)$
	•	Fit by non-linear least squares; constrain \tau>0.


5.	3-month discount-factor shift
	•	Compute deposit − T-bill spread (3m) using PCHIP-interpolated points.
	•	Horizontally shift base term to better reflect short-term deposit observables.

<img width="467" height="75" alt="ftp base term add" src="https://github.com/user-attachments/assets/0533602d-007c-4f6e-88d6-dbdc0f0719ad" />


7.	Liquidity premium computed as the difference between product-specific required yield and market-price-of-money point.

<img width="570" height="95" alt="ftp behavioural liquidity term" src="https://github.com/user-attachments/assets/c379b1d9-6c58-4238-9c01-fe3998aac193" />


8.	FTP allocation
	•	Map accounts → cohorts → tenor buckets.
	•	Assign FTP rate per account = fitted_rate(t_bucket) + cohort adjustments + liquidity premium.
	•	Produce per-account FTP cost and cohort aggregates.
	
9.	QA & governance
	•	Store parametric snapshots and pointwise curves with version id, hash of inputs, QA statistics (RMS error, constraint residuals) and run metadata.
	•	Maintain runbook and SOPs for inputs, frequency and reconciliation.

---
