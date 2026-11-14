# Weekly editorial workflow

A Tsunami issue records what the journal expects and how the outcome will be decided. Claude Code can run [the project skill](../.claude/skills/tsunami-editorial/SKILL.md) from the checkout. The Python commands also work without an agent.

## Prepare the evidence

Start with a specific week. For the standard questions, measure the S&P 500 price return between two Friday closes and ask whether it reaches +2% or −2%. For a named event, state the decision, issuer and deadline precisely enough for someone else to resolve it. Keep names or headlines that cannot be decided from primary sources out of the issue.

```sh
week=YYYY-MM-DD
desk=".editorial/$week"
mkdir -p "$desk"
python3 -m tsunami propose-week --week "$week" > "$desk/proposal.json"
```

The proposal contains a close-to-close frequency from FRED and the source's SHA-256. A missing close, unsupported session or elapsed lock produces an abstention. A named event can still get an issue when its own window remains open.

Write a JSON array at `$desk/sources.json` with any supplemental sources. This format example uses a placeholder issuer.

```json
[
  {
    "title": "Issuer notice",
    "url": "https://example.org/investor/notice",
    "available_at": "2026-11-13T15:00:00Z",
    "retrieved_at": "2026-11-14T18:15:00Z",
    "path": "notice.html"
  }
]
```

Availability is the first defensible public time. Use a conservative bound when a release gives only a date and describe that bound in the source title or rationale. The optional `path` names a local snapshot no larger than 2 MB relative to the manifest. Its bytes stay local. An optional `sha256` supplies a published or independently calculated digest. A digest without a snapshot is a declaration. Omit `retrieved_at` for a link-only historical source when the actual lookup time was never recorded.

```sh
python3 -m tsunami editorial prepare --week "$week" \
  --proposal "$desk/proposal.json" --sources "$desk/sources.json" \
  --output "$desk/packet.json"
```

## Write and review

Start from the proposal's `records`. Save the edited list as `$desk/candidate.json`. Keep the frozen baseline and source citation for a market question. Each new citation must match its packet URL, availability, and SHA-256 when one was captured. Set each forecast's evidence cutoff to a time after its last retrieval, and use one common issue time. Review the ten-year count against the shorter available history and the current evidence. State what moves the probability and what would change your view. The two equity tails share one return and their joint probability cannot exceed 100%. For a named event without a relevant reference class, use `null` for its baseline and state where judgment enters.

```sh
python3 -m tsunami editorial review --week "$week" \
  --packet "$desk/packet.json" --candidate "$desk/candidate.json" \
  --output "$desk/review.json"
```

The review receipt binds the issue to the packet. It checks time order, citation identity, baseline integrity and the relationship between opposite market forecasts. The agent or editor still has to read the documents and review the probabilities. A corrected candidate can get a new receipt in a different file, or the command can write digest-named receipts into a directory.

## Publish

For a Friday-to-Friday question, publish before 09:30 Eastern the following Monday. Stage with the same packet and review. Staging runs the checks again and writes the issue to the checkout. Publishing uses the existing immutable release workflow.

```sh
python3 -m tsunami editorial stage --week "$week" --packet "$desk/packet.json" \
  --candidate "$desk/candidate.json" --report "$desk/review.json"
python3 -m tsunami validate

git add "forecasts/$week.json"
git commit -m "Forecast issue $week"
git push origin main
tag="forecast-$week"
gh release create "$tag" "forecasts/$week.json" --target "$(git rev-parse HEAD)" \
  --title "Tsunami forecast $week" --notes "Weekly market research."
gh release verify-asset "$tag" "forecasts/$week.json"
python3 -m tsunami attest --repository Noptus/tsunami-research --week "$week"
python3 -m tsunami validate
```

Commit and push the verified publication receipt. If the tag already exists, verify the asset and attest it. Corrected research can follow as another dated issue with the original still in the archive. An issue that misses the prospective window can remain a research draft.

## Resolve and score

```sh
python3 -m tsunami settle-markets
python3 -m tsunami validate
```

The market resolver waits for both closing observations and records evidence for its decision. It keeps the source digest and threshold outcome without republishing index levels. For a named event, check the whole interval in the declared primary sources. Follow the published no-event barrier when the decision depends on silence. Use `python3 -m tsunami adjudicate --help` to record an evidence-based decision.

The performance view averages the squared difference between each probability and its binary outcome. A smaller Brier score is better. Compare to the baseline on the same questions. The CPI replay and the historical study stay outside the prospective totals.
