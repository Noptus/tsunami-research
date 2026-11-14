# Do released CPI changes add predictive information?

This archival walk-forward study asks whether the BLS headline and core monthly changes improve a one-day U.S. two-year Treasury direction estimate over the previous yield change's sign. The study tests the figures actually present in individual release documents. It does not use consensus data.

## Protocol

Run `python3 -m tsunami study` for the complete machine-readable analysis. The published site links the generated JSON with every paired prediction and outcome.

1. Start with releases dated January 2020 through November 10, 2026. Extract the embargo and first relevant monthly CPI statements from BLS archive pages. The original HTML hash and retrieval time identify the captured response. The repository includes compact excerpts with separately verified compressed and normalized-text hashes. The full original HTML hashes for this batch require an independent re-fetch to verify.
2. Link the release date, last two observed pre-release sessions and first observed session after release. All observations come from a November 13, 2026 ALFRED DGS2 vintage. The target is 1 when next-session yield exceeds release-session yield. Flat observations count as 0.
3. Compare a Laplace-smoothed event-rate baseline, a ridge logistic model using the last pre-release Treasury change's sign, and the same ridge fit with two additional features for headline and core CPI. Use an expanding prior-event window, 24 minimum training observations, training-only standardization and fixed regularization. Prior labels wait two calendar days after their next observation. Skip missing release-day observations or a next-observation gap longer than five days.
4. Evaluate all methods on identical release dates. Brier error is the squared difference between the stated probability and the binary outcome. Lower is better.

## Results

The capture includes 81 BLS releases. The December 18, 2025 release is excluded because it reports a two-month change following the federal shutdown. One captured event lacked the required sessions. After the 24-label warmup, 56 releases from February 10, 2022 through November 10, 2026 contribute matched scores.

| Model | Mean Brier |
| --- | ---: |
| Smoothed event-rate baseline | 0.2846 |
| Ridge logistic with Treasury lag alone | 0.2878 |
| Same logistic model with CPI headline and core changes | 0.2923 |

Adding CPI increased mean Brier by 0.0046 in the matched logistic comparison. The study provides no affirmative evidence of incremental forecasting value for these two CPI features. A single model choice, small dependent time sample and lack of market expectations limit the result. The analytical lag rule does not reconstruct earlier publication times, and the later ALFRED vintage can differ from contemporaneously known market levels.

The one-release archive in the main README uses a separately frozen pre-cutoff yield vintage and a daily baseline. It is a distinct estimate from this monthly event study.

## Reproduction

The committed excerpt set supports offline reproduction. The optional `python3 -m tools.capture_study /path/to/new-fixture` fetches the original official archive again. Archive bytes can change, so a re-fetch produces a new data capture instead of certifying that an editable page always served the same bytes.

- [BLS CPI release archive](https://www.bls.gov/bls/news-release/cpi.htm)
- [Frozen ALFRED DGS2 vintage](https://alfred.stlouisfed.org/graph/alfredgraph.csv?id=DGS2&cosd=2019-10-01&coed=2026-11-13&vintage_date=2026-11-13)
