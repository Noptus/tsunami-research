# Tsunami Research

**A public ledger for market forecasts that can be checked.**

Tsunami freezes a probability, links the evidence available at issue, and records outcomes against rules published with the forecast. It includes a reproducible CPI replay and a weekly ledger for large market moves and precisely defined corporate events.

## Start here

Python 3.11 or newer is enough. The research replay, test suite and static site use the standard library.

```sh
python3 -m tsunami replay
python3 -m tsunami study
python3 -m unittest discover -s tests -v
python3 -m tsunami validate
python3 -m tsunami build
python3 -m http.server --directory _site 8000
```

The [public ledger](https://noptus.github.io/tsunami-research/) separates locked forecasts, resolved forecasts and archived analysis. [The weekly editorial runbook](docs/weekly-editorial.md) covers research, immutability, scorekeeping and the next issue.

## CPI replay

The November 10, 2026 release was embargoed until 08:30 Eastern. The replay cutoff is 08:45 Eastern. The BLS archive reports monthly headline CPI of **+0.4%** and core CPI of **+0.3%**. Inputs are the [BLS release](https://www.bls.gov/news.release/archives/cpi_11102026.htm) and an [ALFRED DGS2 vintage](https://alfred.stlouisfed.org/graph/alfredgraph.csv?id=DGS2&cosd=2025-05-01&coed=2026-11-09&vintage_date=2026-11-09) fixed to the previous calendar day.

The prediction target is the direction from release-session DGS2 to next observed session DGS2. The baseline uses the fraction of increases in the previous 252 daily Treasury yield changes, with Laplace smoothing. A second estimate conditions on the most recent observed yield change's sign. CPI figures inform a separately disclosed narrative. They never change either numerical estimate.

| Quantity | Replay result |
| --- | ---: |
| Base-rate probability of higher yield | 45.67% |
| Conditional probability of higher yield | 43.10% |
| Release-session DGS2 | 4.73% |
| Next observed session DGS2 | 4.86% |
| Change | +13 basis points |
| Base-rate Brier error | 0.2952 |
| Conditional Brier error | 0.3237 |

November 11 is missing in the [frozen evaluation vintage](https://alfred.stlouisfed.org/graph/alfredgraph.csv?id=DGS2&cosd=2026-11-10&coed=2026-11-12&vintage_date=2026-11-13), so the next observation is November 12. The evaluation values are from the November 13 vintage. Per-observation first-print timestamps were unavailable in these exports.

The source fixture preserves URL, recorded embargo or conservative vintage bound, observed retrieval time and SHA-256. Forecast inputs are loaded before outcome bytes. The stored BLS page is compressed losslessly, with checksums for both compressed bytes and source bytes. The timestamp and source assertions can be inspected in [the manifest](data/cpi-2026-11-10/event.json). Source data were retrieved after the event. This is an archival reconstruction and supplies one Brier trial.

## CPI feature study

The [research protocol](docs/cpi-study.md) compares an event-rate baseline and two matched logistic models, with and without the current CPI figures. The site links row-level predictions, outcomes and provenance. This is exploratory historical work using one frozen yield vintage.

## Weekly track record

- The first issue uses subjective probabilities anchored to a ten-year empirical frequency for the S&P 500 tails. Named-event judgment is published with its source hierarchy, explicit YES criteria, and negative-resolution checks.
- The site only includes prospective records after verification of the entire issue asset in an immutable GitHub release. Both the declared lock and server publication precede the first Monday session for market tail questions.
- Market outcomes are checked after an eight-hour grace period and then retried on schedule. If close data remain absent after the predeclared void cutoff, an evidence-bearing void is recorded. Named events use a human review of the predeclared official sources.
- Each result references its original forecast digest. Aggregate scores use resolved prospective forecasts. Baseline comparisons use matched forecasts. Two questions about opposite tails of one weekly return are dependent.
- FRED SP500 is a copyrighted index series. Prices are fetched transiently for calculation. The text, digest, threshold outcome and derived counts can be audited without redistributing raw index levels.

The code is [MIT licensed](LICENSE). Third-party evidence keeps its original terms. Forecasts are research outputs.
