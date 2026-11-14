# Tsunami Research

Tsunami is a weekly market research journal. Each issue records a probability, the evidence available at the time, and the rule that will decide the outcome. The archive includes dated issues, resolved forecasts and reproducible research on CPI releases.

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

Read the [journal](https://noptus.github.io/tsunami-research/) or use the [weekly editorial workflow](docs/weekly-editorial.md) to produce a new issue. Claude Code can use the [editorial skill](.claude/skills/tsunami-editorial/SKILL.md) from this checkout.

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

The [research protocol](docs/cpi-study.md) compares an event-rate baseline and two matched logistic models, with and without the current CPI figures. The site links row-level predictions, outcomes and provenance. The archive uses 162 date-specific pre- and post-event yield snapshots. Adding CPI increased matched Brier by 0.0028 over 56 scored releases (exploratory 95% block interval −0.0149 to +0.0215).

## Forward CPI capture

Use the [official BLS schedule](https://www.bls.gov/schedule/news_release/cpi.htm) to choose the embargo and run a capture within the following 15 minutes.

```sh
python3 -m tsunami capture-cpi --embargo 2026-12-10T08:30:00-05:00 --output captures/2026-12-10
```

The tool verifies the official release text against the expected embargo, checks vintage dates, hashes both retrieved sources, and seals a local forecast manifest. No outcome request runs in this path. Publish the content digest to a timestamped external record before the outcome if you want verifiable prospective issuance. The example has not yet run against the live December release.

## Weekly track record

- The first issue uses subjective probabilities anchored to a ten-year empirical frequency for the S&P 500 tails. Named-event judgment is published with its source hierarchy, explicit YES criteria, and negative-resolution checks.
- The site only includes prospective records after verification of the entire issue asset in an immutable GitHub release. Both the declared lock and server publication precede the first Monday session for market tail questions.
- Market outcomes are checked after an eight-hour grace period and then retried on schedule. If close data remain absent after the predeclared void cutoff, an evidence-bearing void is recorded. Named events use a human review of the predeclared official sources.
- Each result references its original forecast digest. Aggregate scores use resolved prospective forecasts. Baseline comparisons use matched forecasts. Two questions about opposite tails of one weekly return are dependent.
- FRED SP500 is a copyrighted index series. Prices are fetched transiently for calculation. The text, digest, threshold outcome and derived counts can be audited without redistributing raw index levels.

The code is [MIT licensed](LICENSE). Third-party evidence keeps its original terms. Forecasts are research outputs.
