# Supervised final Week 10 publication

The staged release is `frontend-week10-final-bbf8e10133b9118f`. This package has local validation and no deployment authority.

The complete review mirror is `artifacts/research/w10_release_bbf8e10133b9118f`. It contains the 430 exact static assets under `week10_release/site/`, three release documents, the new guard and a proposed replacement for the existing manual workflow. Private weights, raw captures and research preparation data are outside that mirror.

The publishing route remains workflow **399724330**, `.github/workflows/manual-frontend-correction.yml`, at the existing GitHub Pages URL.

## Exact review pins

| File | SHA-256 |
| --- | --- |
| week10_release/publication_manifest.json | `3f941c63beb60c79891891503b2dc9000982e04ac1bf71fbf3deac7af7815f03` |
| week10_release/review_request.json | `5886b999548c2e19390d067eccc24ab24bddec5cfb78a7e44abfc5468f672210` |
| week10_release/controls.json | `92ad7ca5653bd057acf67af5c3828fa7021cc9601e86a09d63a3417a39a58848` |
| scripts/verify_week10_publication.py | `46f96cf4c5d25e43f7c4f6a3f28d786daaf209480dc1c32206bdbae9cdaaf279` |
| .github/workflows/manual-frontend-correction.yml | `a43f97d99b89b85797d733dbb90525f86286b4c3e39581049fc1a347b05c8238` |

The current live parent is `frontend-week9-tidy-29466c12d79894cf`, main `99debb21c2fc067fff1e3d4c9b3a0c1aec25c869`. The first fixture cutoff is **13 November 2026 at 01:14 GMT**. Every runtime check uses its actual clock.

## Review and installation

1. Review the final local dashboard, per-fixture forecasts, player holds, exact site inventory, proposed guard and workflow diff. The guard tests include authentic-package acceptance and altered assets, old parent, stale/edited approvals, wrong actor/commit, repeat dispatch and closed/live fixture rejection. The approval fixtures used by those tests exist only in memory.
2. Review the assets PR from `codex/week10-final-assets-bbf8e10133b9118f`, targeting `main`. It contains 430 exact site files and their publication manifest. Review the controls PR from `codex/week10-final-controls-bbf8e10133b9118f`, initially targeting the assets branch. Its five files comprise the proposed manual workflow, new guard, exact review request, controls document and this guide. The sealed candidate and its publication/review-request hashes are identical in both reviews.
3. After owner review, merge the assets PR with a merge commit and retain its branch. The repository currently permits merge commits. Retarget the controls PR to `main` and verify that its diff still contains exactly the five control/document files. Review and merge the controls PR only with the owner's approval. The final controls PR must have base `main` and its exact source branch must match `REVIEW_BRANCH`. Independently verify the actual controls merge SHA and every staged hash, workflow state, Pages configuration, routine flags, absence of active runs/deployments and live parent. The new owner approval belongs on the merged controls PR and binds its actual merged main SHA. If the assets PR is squash/rebase merged, recheck the controls ancestry and prepare a fresh reviewed base before proceeding.
4. Run a fresh independent fixture check. All 14 games must still be pregame and before their individual cutoffs. If the first cutoff is reached, stop this pregame route and request a separately approved archival plan.

## New authenticated owner approval

Only after the exact installation PR is merged, the repository owner must post one new, unedited comment on that merged PR. The comment must contain exactly one fenced JSON block beginning with a newline after `json`. Fill the two placeholders with the verified merged main SHA and a real UTC expiry. The expiry must be after the intended dispatch, at most two hours after the comment, and no later than the first cutoff.

```json
{
  "decision": "APPROVED",
  "repository": "racingmodel/nfl-2026-visualizer",
  "destination": "https://racingmodel.github.io/nfl-2026-visualizer/",
  "release_id": "frontend-week10-final-bbf8e10133b9118f",
  "manifest_sha256": "3f941c63beb60c79891891503b2dc9000982e04ac1bf71fbf3deac7af7815f03",
  "request_sha256": "5886b999548c2e19390d067eccc24ab24bddec5cfb78a7e44abfc5468f672210",
  "installation_head": "<VERIFIED_MERGED_MAIN_SHA>",
  "scope": "WEEK10_FINAL_MODEL_C_STATIC_ONCE",
  "expires_at_utc": "<ACTUAL_UTC_EXPIRY>",
  "unattended": false
}
```

The installed guard authenticates the comment's owner association, author, creation/edit times, exact merged PR, source branch, repository, installation commit, scope, hashes and expiry through GitHub. The Week 9 approval and its previous dispatches cannot satisfy this request.

## One separately authorised dispatch

After the owner grants separate permission to enable and dispatch, use the following PowerShell commands with the verified main SHA and new approval comment ID. These are instructions only.

```powershell
$installationHead = '<VERIFIED_MERGED_MAIN_SHA>'
$approvalCommentId = '<NEW_OWNER_COMMENT_ID>'
gh workflow enable 399724330 --repo racingmodel/nfl-2026-visualizer
gh workflow run 399724330 --repo racingmodel/nfl-2026-visualizer --ref main --field release_id=frontend-week10-final-bbf8e10133b9118f --field review_request_sha256=5886b999548c2e19390d067eccc24ab24bddec5cfb78a7e44abfc5468f672210 --field installation_head=$installationHead --field approval_comment_id=$approvalCommentId
```

Only this workflow may be enabled for the approved operation. The other four workflows and routine flags retain their reviewed settings. The guard permits one workflow attempt and rejects another dispatch for the same installation commit. A failed first attempt requires a new reviewed correction and authority.

## Actions, Pages and live verification

Verify that the run is bound to the exact merged main SHA and new owner comment. The review job must pass package, current parent, authenticated authority, one-time, platform and fresh fixture checks. The publish job repeats those checks immediately before deployment. Its Pages deployment must succeed for the same commit.

The live check compares all **425 served files** against the exact 430-file package, with the five `.nojekyll` markers excluded from HTTP verification. Check the new `release_receipt.json`, all historical forecast hashes, the Model C asset, current role filters and the desktop/mobile views. A partial or failed live check leaves publication completion unverified.

No file changes, approval, workflow enablement, dispatch or deployment have been performed remotely by this preparation.
