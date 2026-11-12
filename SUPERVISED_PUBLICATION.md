# Supervised final Week 10 publication

The staged release is `frontend-week10-final-bbf8e10133b9118f`. This package has local validation and no deployment authority.

The sealed site mirror remains `artifacts/research/w10_release_bbf8e10133b9118f`, with 430 exact assets under `week10_release/site/`. This correction replaces only the separately reviewed publication controls and their tests. After merge, use the installed guard and the new pins below, rather than the earlier guard in that historical mirror. Private weights, raw captures and research preparation data remain outside the public release.

The publishing route remains workflow **399724330**, `.github/workflows/manual-frontend-correction.yml`, at the existing GitHub Pages URL.

## Exact review pins

| File | SHA-256 |
| --- | --- |
| week10_release/publication_manifest.json | `3f941c63beb60c79891891503b2dc9000982e04ac1bf71fbf3deac7af7815f03` |
| week10_release/review_request.json | `a3e58051a1a2fb49a9cb9160754fac176c0789e0ffc23ee014fef2399900113c` |
| week10_release/controls.json | `541ecce54e6f6a61d69fa7af36bbc1e5bd3511afc424c9a524f4bd89f38b7fd7` |
| scripts/verify_week10_publication.py | `82e8c5b835ac8472744ff5c9b4e30296a3b8c4d68feafc7ebc3486ed45ce59b2` |
| .github/workflows/manual-frontend-correction.yml | `4da580544d799ef3ce5714ce525a862988a816982b1d26c0dfd5d461194e26cb` |

The current live parent is `frontend-week9-tidy-29466c12d79894cf`, main `99debb21c2fc067fff1e3d4c9b3a0c1aec25c869`. The first fixture cutoff is **13 November 2026 at 01:14 GMT**. Every runtime check uses its actual clock.

## Review and installation

The prior installation commit is `9fc569f07af90182b77e7510267411222eed338f`. Its run `43226774165` consumed the one allowed attempt and failed before deploying. Owner comment `6824322618` cannot authorise this correction.

1. Review the separate guard correction PR from `codex/week10-guard-self-deployment-20261112` to `main`. It contains only the guard, two read-only job permissions, refreshed request/control pins, this guide, and focused regression tests/fixture. All 430 site assets and the publication manifest remain unchanged.
2. The guard permits only the deployment reached through the running publish job's **server-side CheckRun.deployment relationship**, validated against the authenticated run, job, repository, environment, commit, check suite and GitHub Actions app. Status URLs and matching SHAs are corroborating checks and cannot grant an exception alone. Unknown/null/inaccessible identity fails closed. All unrelated active or ambiguous deployments remain blocked. Environment tracking remains enabled.
3. The existing review job remains outside the deployment environment. Moving the final guard into another non-deployment job would leave a gap before publication and would not establish the publish job's identity. The final check therefore remains immediately before deploy-pages in the publish job. No environment deployment-tracking setting is disabled.
4. After owner review and separate merge authorisation, use a merge commit and retain the source branch. Independently verify the new actual main merge SHA, these exact pins, disabled workflows, false routine flags, no other active run/deployment and unchanged Week 9 live parent. Use a fresh owner comment on this **merged correction PR**, binding its actual merge SHA and the new request hash. The branch must remain `codex/week10-guard-self-deployment-20261112` and the final PR base must be `main`.
5. Run a fresh independent fixture check. All 14 games must still be pregame and before their individual cutoffs. This pregame route closes at **13 November 2026 at 01:14 GMT**. After cutoff, obtain a separately authorised archival plan rather than extending this request.

## New authenticated owner approval

Only after the exact installation PR is merged, the repository owner must post one new, unedited comment on that merged correction PR. The comment must contain exactly one fenced JSON block beginning with a newline after `json`. Fill the two placeholders with the verified merged main SHA and a real UTC expiry. The expiry must be after the intended dispatch, at most two hours after the comment, and no later than the first cutoff.

```json
{
  "decision": "APPROVED",
  "repository": "racingmodel/nfl-2026-visualizer",
  "destination": "https://racingmodel.github.io/nfl-2026-visualizer/",
  "release_id": "frontend-week10-final-bbf8e10133b9118f",
  "manifest_sha256": "3f941c63beb60c79891891503b2dc9000982e04ac1bf71fbf3deac7af7815f03",
  "request_sha256": "a3e58051a1a2fb49a9cb9160754fac176c0789e0ffc23ee014fef2399900113c",
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
gh workflow run 399724330 --repo racingmodel/nfl-2026-visualizer --ref main --field release_id=frontend-week10-final-bbf8e10133b9118f --field review_request_sha256=a3e58051a1a2fb49a9cb9160754fac176c0789e0ffc23ee014fef2399900113c --field installation_head=$installationHead --field approval_comment_id=$approvalCommentId
```

Only this workflow may be enabled for the approved operation. The other four workflows and routine flags retain their reviewed settings. The guard permits one workflow attempt and rejects another dispatch for the same installation commit. A failed first attempt requires a new reviewed correction and authority.

## Actions, Pages and live verification

Verify that the run is bound to the exact merged main SHA and new owner comment. The review job must pass package, current parent, authenticated authority, one-time, platform and fresh fixture checks. The publish job repeats those checks immediately before deployment. Its Pages deployment must succeed for the same commit.

The live check compares all **425 served files** against the exact 430-file package, with the five `.nojekyll` markers excluded from HTTP verification. Check the new `release_receipt.json`, all historical forecast hashes, the Model C asset, current role filters and the desktop/mobile views. A partial or failed live check leaves publication completion unverified.

This document grants no owner approval, workflow enablement, dispatch or deployment authority. The correction PR must be reviewed and separately authorised for merge before the new publication procedure begins.
