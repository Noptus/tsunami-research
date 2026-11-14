# Do released CPI changes add predictive information?

This archival walk-forward study asks whether the BLS headline and core monthly changes improve a one-day U.S. two-year Treasury direction estimate over the previous available yield change's sign. The study uses the figures in the archived BLS releases. It does not use consensus estimates.

## Protocol

Run `python3 -m tsunami study` for the date-vintaged study. The public site links per-release predictions, outcomes and provenance. The first version remains available through `python3 -m tsunami study --single-vintage`.

1. Start with releases dated January 2020 through November 10, 2026. Extract the embargo and first relevant monthly CPI statements from BLS archive pages. The original HTML hash and retrieval time identify the downloaded response. The repository includes compact excerpts with verified compressed and normalized-text hashes. Checking the full original HTML hashes requires an independent re-fetch.
2. Use two distinct ALFRED DGS2 vintages per release. The pre-release file uses the prior Eastern date and is treated as available at its end. The outcome file uses seven days after release, except the final release, which uses the November 13 vintage. The label compares the release session to the next observed session. Flat observations count as zero. The source supplies dates rather than first-print receipt times. Release and outcome values were looked up in separate snapshots after the events.
3. Compare a Laplace-smoothed event rate by lag sign, a ridge logistic model on the last pre-release yield change's sign, and the same ridge model with two CPI features. Use at least 24 eligible past labels and training-only standardization. Prior labels enter only after the recorded post-release vintage bound. The November 10 event's lagged value uses data through November 6, the last observation visible in the November 9 vintage.
4. Evaluate all methods on identical release dates. Brier error is the squared difference between probability and outcome. Lower is better. Estimate an exploratory 95% interval on the mean paired CPI-minus-lag-only difference by circular resampling of contiguous six-release blocks, 4,000 replicates with a fixed seed. The interval describes score-pair variability under that resampling assumption, conditional on the historical model fits.

## Results

The capture includes 81 BLS releases. The December 18, 2025 release is excluded because it reports a two-month change following the federal shutdown. The April 10, 2020 release is excluded from scoring because ALFRED has no release-session observation. After the 24-label warmup, 56 releases from February 10, 2022 through November 10, 2026 contribute matched scores.

| Model | Date-vintaged Brier | Initial single-vintage Brier |
| --- | ---: | ---: |
| Smoothed event-rate baseline | 0.2924 | 0.2846 |
| Ridge logistic with Treasury lag alone | 0.2812 | 0.2878 |
| Same ridge logistic with CPI headline and core | 0.2840 | 0.2923 |

Adding CPI increased mean Brier by 0.0028 in the matched date-vintaged comparison. Its exploratory 95% interval was −0.0149 to +0.0215. The initial single-vintage comparison had a +0.0046 increment. The study leaves the incremental value of these two raw CPI features uncertain. The one-release pilot in the README uses a separately frozen pre-cutoff vintage and a daily, rather than monthly event-conditioned, baseline.

## Reproduction

Run `python3 -m tools.capture_study /path/to/new-fixture`, then `python3 -m tools.capture_yield_vintages /path/to/new-fixture` to re-fetch the archive. The second tool caches completed event pairs and rate-limits sequential requests. Archive bytes can change after publication and a re-fetch records its own retrieval times.

- [BLS CPI release archive](https://www.bls.gov/bls/news-release/cpi.htm)
- [ALFRED real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)
