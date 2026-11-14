# Weekly forecast protocol

A Tsunami issue contains probabilistic research forecasts about a defined observation or announcement. The archive keeps the original question, probability, evidence, adjudication rule, and first score together. A forecast is eligible for the prospective scorecard after its issue asset matches a timestamped GitHub release.

## 1. Collect evidence and choose questions

- Define the target before inspecting its outcome. For equity tails, use one fixed closing index, an exact return threshold, and two predeclared exchange sessions. The standard pair is S&P 500 up at least 2% and down at least 2% from the previous Friday close to the next Friday close. Skip a pair if either endpoint is a market holiday.
- For a named event, use an unambiguous action and an issuer, regulator, or other primary confirming authority. Define what qualifies for both outcomes, the event window in Eastern time, how to handle conflicting sources, and when missing evidence warrants a void.
- Pull primary evidence that was public by the recorded cutoff. Cite the original document and its verifiable release time. If it gives only a date, record a conservative availability bound and say how it was assigned. Record the exact retrieval time for mutable feeds.
- Run the historical tail-frequency baseline. Review the date range, counts, sample size and known calendar effects. Compare it to a shorter lookback as a sensitivity check. For a singular company event without a matched reference class, leave the baseline blank and mark the probability as judgment.
- Write a rationale, a counterargument and what information would change the call. For the same return window, keep the joint tail probabilities at or below one. Keep fixed thresholds on the scorecard through weak weeks. A headline forecast can have a low probability and still be worth tracking.

## 2. Validate and publish before the forecast window

Run these commands from the checkout. Replace the example week with the actual issue date.

```sh
week=2026-11-20
python3 -m tsunami validate
python3 -m tsunami baseline --week "$week"
```

Place the validated issue in `forecasts/$week.json`. Record a common UTC issue time after the evidence cutoff. For a Friday-to-Friday market forecast, publish before the following Monday's regular open. Use main and an immutable release with the issue file attached. Verify the asset before committing the receipt.

```sh
git add "forecasts/$week.json"
git commit -m "Forecast issue $week"
git push origin main
tag="forecast-$week"
gh release create "$tag" "forecasts/$week.json" --target "$(git rev-parse HEAD)" \
  --title "Tsunami forecast $week" --notes "Frozen weekly forecast issue."
gh release verify-asset "$tag" "forecasts/$week.json"
python3 -m tsunami attest --repository Noptus/tsunami-research --week "$week"
python3 -m tsunami validate
```

Commit and push the generated publication receipt. If the release already exists, verify its asset against the candidate issue and reuse it. Correct substantive forecast errors in a new issue, with the original preserved. If publication misses the cutoff, leave the research draft out of the prospective scorecard.

## 3. Resolve and score

The market resolver polls the original data source after the endpoint close and waits for both observations. The report retains the resolved condition, timestamp and evidence digest. It does not redistribute third-party index levels.

```sh
python3 -m tsunami settle-markets
python3 -m tsunami validate
```

For a named event, check the full interval in the predeclared sources. Record authoritative links and a concise account of the search. When a negative decision depends on no announcement by the deadline, observe any verification delay set in that question. Leave the event pending if its sources are unavailable or ambiguous. See `python3 -m tsunami adjudicate --help` for the review command.

Read Brier scores per forecast and in aggregate. Compare with a baseline only on records where both were declared. The two equity tails are dependent, and a handful of weekly issues cannot establish calibration or an investment edge.
