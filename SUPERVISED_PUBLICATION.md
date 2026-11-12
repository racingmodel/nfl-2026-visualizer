# Supervised final Week 10 publication

The staged release is `frontend-week10-refresh-6060d616be7e911b`. This package has local validation and no deployment authority.

The refreshed 430-asset candidate is under `artifacts/research/week10_closeout_20261112T152815Z/candidate/week10_release/site/`. Four market/source-clock/release files change from the preceding sealed Week 10 package; all frozen model forecasts, roles and historical data remain unchanged. Use the new reviewed manifest and installed guard after merge. Private weights, raw captures and preparation data remain outside the public release.

The publishing route remains workflow **399724330**, `.github/workflows/manual-frontend-correction.yml`, at the existing GitHub Pages URL.

## Exact review pins

| File | SHA-256 |
| --- | --- |
| week10_release/publication_manifest.json | `674adaf8ff579c0a219a7c02b5235b087a6353f480eef0a09d489cb0fb669ff0` |
| week10_release/review_request.json | `52f421532f739fefa08e0ffa132c19dda790d45fb949469cbf0739bd02aa5321` |
| week10_release/controls.json | `339d12c72dc97289a9369c07730f0f1620a1e92a5312a5e853a3c646b75aa0d6` |
| scripts/verify_week10_publication.py | `2cf513c67922f6feeb30415dae671e0b851a0ca9c51bd6e2c50c5bcbd81af84d` |
| .github/workflows/manual-frontend-correction.yml | `6fc5eb64fa3a1d6f4b586a8b8670029fb131bf3ff755bb97c0fe08e196f1e754` |

The current live parent is `frontend-week9-tidy-29466c12d79894cf`, main `99debb21c2fc067fff1e3d4c9b3a0c1aec25c869`. The first fixture cutoff is **13 November 2026 at 01:14 GMT**. Every runtime check uses its actual clock.

## Review and installation

The self-deployment guard fix from PR #12 is installed on main `94050a87317ca3ebfe030d4148d8bfff15388181`. This follow-up changes four site files for fresh quote observations, displayed source/retrieval clocks and release identity. Spread point lines and totals did not change; ten prices across four fixtures did. All Model A/B/C, TD, rushing, receiving, passing, roles and historical forecast data remain unchanged.

Review this release from `codex/week10-market-refresh-6060d616be7e911b` to main. Its 430-file package must match the new manifest. Verify the four market/presentation changes and separate manifest/control/request/guide pins. Preserve the authenticated CheckRun deployment checks and all owner, exact-commit, single-dispatch, expiry and cutoff gates.

After separately authorised review and merge, verify the actual new main merge SHA and obtain a fresh unedited owner comment on that merged release PR. Neither consumed approval `6824322618` nor any previous request authorises this revision. Require separate permission to enable only workflow 399724330 and dispatch once. Keep other workflows and routine flags disabled/false. Preserve the Week 9 live parent until supervised publication succeeds.

First cutoff: **13 November 2026 at 01:14 GMT**. Stop this pregame route after cutoff; do not extend or backdate. Verify all 425 served files and the Week 10 display after deployment, then disable the publishing workflow again.

## New authenticated owner approval

Only after the exact installation PR is merged, the repository owner must post one new, unedited comment on that merged correction PR. The comment must contain exactly one fenced JSON block beginning with a newline after `json`. Fill the two placeholders with the verified merged main SHA and a real UTC expiry. The expiry must be after the intended dispatch, at most two hours after the comment, and no later than the first cutoff.

```json
{
  "decision": "APPROVED",
  "repository": "racingmodel/nfl-2026-visualizer",
  "destination": "https://racingmodel.github.io/nfl-2026-visualizer/",
  "release_id": "frontend-week10-refresh-6060d616be7e911b",
  "manifest_sha256": "674adaf8ff579c0a219a7c02b5235b087a6353f480eef0a09d489cb0fb669ff0",
  "request_sha256": "52f421532f739fefa08e0ffa132c19dda790d45fb949469cbf0739bd02aa5321",
  "installation_head": "<VERIFIED_MERGED_MAIN_SHA>",
  "scope": "WEEK10_FINAL_MODEL_C_STATIC_ONCE",
  "expires_at_utc": "<ACTUAL_UTC_EXPIRY>",
  "unattended": false
}
```

The installed guard authenticates the comment's owner association, author, creation/edit times, exact merged correction PR, source branch, repository, installation commit, scope, hashes and expiry through GitHub. Neither Week 9 approval nor consumed Week 10 comment `6824322618` can satisfy this new request.

## One separately authorised dispatch

After the owner grants separate permission to enable and dispatch, use the following PowerShell commands with the verified main SHA and new approval comment ID. These are instructions only.

```powershell
$installationHead = '<VERIFIED_MERGED_MAIN_SHA>'
$approvalCommentId = '<NEW_OWNER_COMMENT_ID>'
gh workflow enable 399724330 --repo racingmodel/nfl-2026-visualizer
gh workflow run 399724330 --repo racingmodel/nfl-2026-visualizer --ref main --field release_id=frontend-week10-refresh-6060d616be7e911b --field review_request_sha256=52f421532f739fefa08e0ffa132c19dda790d45fb949469cbf0739bd02aa5321 --field installation_head=$installationHead --field approval_comment_id=$approvalCommentId
```

Only this workflow may be enabled for the approved operation. The other four workflows and routine flags retain their reviewed settings. The guard permits one workflow attempt and rejects another dispatch for the same installation commit. A failed first attempt requires a new reviewed correction and authority.

## Actions, Pages and live verification

Verify that the run is bound to the exact merged main SHA and new owner comment. The review job must pass package, current parent, authenticated authority, one-time, platform and fresh fixture checks. The publish job repeats those checks immediately before deployment. Its Pages deployment must succeed for the same commit.

The live check compares all **425 served files** against the exact 430-file package, with the five `.nojekyll` markers excluded from HTTP verification. Check the new `release_receipt.json`, all historical forecast hashes, the Model C asset, current role filters and the desktop/mobile views. A partial or failed live check leaves publication completion unverified.

This document grants no owner approval, workflow enablement, dispatch or deployment authority. The correction PR must be reviewed and separately authorised for merge before the new publication procedure begins.
