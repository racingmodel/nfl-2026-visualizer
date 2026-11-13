"""Exact Week 10 tidy release guard for the supervised Pages workflow."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import argparse
import csv
import hashlib
import io
import json
import os
import time


REPO = "racingmodel/nfl-2026-visualizer"
BASE = Path("week10_release")
SITE = "https://racingmodel.github.io/nfl-2026-visualizer/"
SCHEDULE = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
WORKFLOW_ID = "399724330"
RELEASE_ID = 'frontend-week10-recovery-bd0913766051f62c'
MANIFEST_SHA256 = '6cc743b046f98d7d53b57895faced2edca167f86ee571eef2af973691612ca5e'
PARENT_ASSETS_SHA256 = '29466c12d79894cf18c3490c8bb4cb2687ae561717d1c9bddfc21305942e9227'
REVIEW_BRANCH = 'codex/week10-recovery-controls-bd0913766051f62c'
APPROVAL_SCOPE = 'WEEK10_MIXED_ARCHIVAL_PREGAME_STATIC_ONCE'
TIDY_HASHES = {'classic_research.js': '593f219cebdfedf77f9e3e5670e79380ba454cd6059bd1d90a652861ad5b1476', 'index.html': '06139a1031cec53d33defa7f6767ed6bfbe489e5ff208cd60d907940d51770b7', 'local_week9.css': '04e42703d4163e83f0d350b1fb111ca96bf7474da104ff339308d5e88ab5b60f', 'model.html': 'd726317aee0afac31d9424271bc4f3a16f2d197d5e75475f519da4d5dedcf72b', 'performance.html': '17f4473ebca020ce0b3c8c0a90a7196b9a28cf80c52481e4ac04d2b33ddc7d47', 'players.html': 'ef2c1750d2489c4ecf819a6477c0bb4d550d532a2a0009769315f69c208d1356', 'player_roles.js': 'ffb8cdbbb60870d46793640def8ed116dbc188467aea7abe59e00c016df75588', 'prediction_display.js': '291e4869fecd15ab3d317590471f003150aa2e4945a7cca05930a43eb6cdf2c0', 'release_receipt.json': '2c8a3995a8f7e907619d6a45182b41a07545f93ffe42cba918d053891ce1dc51', 'value.html': '4156aa26782d4e2f2f4cf369ac588429c574d0669cd17e07ceddafda1eb09ced', 'week10_preview.js': '0f2c9e79a86a62bcba1d17d558115a618f58599d87676b0b0482d5df6640a745', 'data/hosted_catalog.json': '8ca4461db20547aa80563bfef945b0864038dce22eb734a1dfdf34a9164d0c13', 'data/model_c_week10.json': '71e819237db60e7b1bbf861a7db49b97e4ddcc749cf1abb558e3fc369aedcf2f', 'data/original_td_week10.json': 'd4310402a02c4a5e143665e472801c261b2366dac0cfa56efe9a8dfac408f36d', 'data/quarter_catalog.json': '2b98f1b881b18132ae43c320d48a02a16c163b58d1c70a680364148aa8ac138e', 'data/quarter_simulation_week10.json': '70e4aed82f3f33c38ab3d3ad36c1d0630213dffa8eec2f12106fc6cfef2f5caa', 'data/td_catalog.json': '70fee9ac00078b5bb31e5281fec4eb868ad645370cb90571f02e479cca4d206b', 'data/week10_context.json': '67e690a077b50e9d75dba4af03f176dcdd4d42b325c553027e59b907e9ee7821', 'data/week10_fixture_comparison.csv': 'd81e774f784fb3dc1e50d399403cb8f90479b9b24a10fdfd21912ee5b021d29f', 'data/week10_market_observations.csv': '85f653a62b671b77f3c6b25c93badeadcc46d98341c12ad10581198fae33c0e5', 'data/week10_publication_context.json': '3ac25116409abf80ef923e1f8de9d09473d737555f693c9f03d5ab5e7480510d', 'data/week10_public_prop_observations.csv': '8e5a0cbac2c943e34b7c28204ea58c2d4e74fbd8aa9b76b45ef320bb7215b58f', 'data/week9_quarter_metrics.csv': 'eac84914cfaea3626eab1deaafea51a02974d6ec79ba83ccd461ddcbebf94528', 'data/week9_td_metrics.csv': 'fc3e5c61ad5dd6329bdc2f9a85633bbc9ecdf520f82eee0d68d13c7d4e9bf9a6', 'data/week9_yardage_metrics.csv': 'ff4317e9b53375a428856d101dd796a5f85ebac1cefff3c183fe5ae650b91f92', 'data/weeks1_9_team_metrics.csv': 'fd68a9048296412e295fc180339e6bfac9aa61b515ec8345427c39059a10bc1a', 'data/research/2026-w10.json': '4a37a8c515ea9d00b299471c5ecac95d86e76d66c3f77a8037b43a1fa7a6454e'}
EXPECTED_HISTORY = ['2026_09_ARI_SEA', '2026_09_BUF_MIN', '2026_09_CIN_ATL', '2026_09_CLE_NO', '2026_09_DAL_IND', '2026_09_DEN_CAR', '2026_09_DET_MIA', '2026_09_GB_NE', '2026_09_HOU_LAC', '2026_09_JAX_BAL', '2026_09_LA_WAS', '2026_09_LV_SF', '2026_09_NYG_PHI', '2026_09_NYJ_KC', '2026_09_TB_CHI']
BASE_MAIN_SHA = '75856d8414383f1185af21623866bd288682a5ff'
ARCHIVE_DESCRIPTOR_SHA256 = '3ac25116409abf80ef923e1f8de9d09473d737555f693c9f03d5ab5e7480510d'
FROZEN_DATA_HASHES = {'data/certified_extensions_v32.json': 'e1453a2edf2066705f4a967bbe9de83fab369afc5a4700aded626d53831ad972', 'data/current_availability.json': 'b4f9875d0d1ea483401a7d8badf578938af32c90126153dede8a21a05ddd1ce7', 'data/current_week.json': 'acd3ca52da407a3fe59a9c890eaca815af24865d5b9ffe31d77c54287ab24fe4', 'data/dashboard_context_v32.json': '341cd9cf35a1f8011927d521a5b894f9b47b99b6afdcc835417f3cd54d1cef40', 'data/dashboard_manifest_v32.json': 'bf3da2096fb32e849a651e2f5fad94babcb1f2dc1b71bae75040f35d20f6c790', 'data/frontend_contract.json': '9df8a0612638c65c7db2bc72d1f5a124169bfc74010bed185365d297f3861d5c', 'data/frontend_release.json': '2345d311255082d21dc9acfe81f7106203859c16572efd443b0c31316692cfa6', 'data/hosted_catalog.json': '8ca4461db20547aa80563bfef945b0864038dce22eb734a1dfdf34a9164d0c13', 'data/market_observations_v32.json': 'a382a308dfb170e67498680d0676b4b2b53d3f0e7a17b4b5508521974df8b0a6', 'data/model_c_week10.json': '71e819237db60e7b1bbf861a7db49b97e4ddcc749cf1abb558e3fc369aedcf2f', 'data/official_performance_v32.json': '4f90a907bbf3a5b8b84d565b97abfbe9a02e72f92f030f8e709727c018d2eb30', 'data/original_td_component.json': '413d7f2b6729bbb6e31cec84dade08847dc69cbc0dbabbeb494a36d746507c34', 'data/original_td_week10.json': 'd4310402a02c4a5e143665e472801c261b2366dac0cfa56efe9a8dfac408f36d', 'data/original_td_week8.json': '413d7f2b6729bbb6e31cec84dade08847dc69cbc0dbabbeb494a36d746507c34', 'data/original_td_week9.json': '21d83d445cab5e61c18b6e968e2691575834b2596ec66847bccf550611b82cc8', 'data/passing_projections.json': 'edd6d6f5ae47d22bd33a630f8a1876ecaac9b5bc64f430bdf3a631e51437f792', 'data/performance.json': '41ff2c968d0162bd638d3d026cd10368242a5de8d60e2fd4b9836b2c931aa324', 'data/phase7_lab.json': 'e9e6b3c4cd6f3c3e7572f3565935582373b8687fdc419397ac75699cbe5c6697', 'data/phase7_performance.json': '93cf22e190473dc274b2f97c679c8b9ff564880af207ddd07beedb3ace682e9b', 'data/phase7_source_health.json': '24bc0dcc6a2964db73f94773dcff0291b87ad594942964c069b7447355ef86c6', 'data/player_presentation.json': 'a7a562bc7425c651dea529725591da8528e347bb4fb9d247e8ba42cd527ac5c9', 'data/player_roles.json': 'ac117c9ac6723d8e6c17bb3b2e84496b157981eaee6635d3ac68c62f3ee5b80c', 'data/prediction_display.json': '27764122b20e822fa430fc2be623954b8b6082cce2b5af41690ae69b684cee0a', 'data/quarter_catalog.json': '2b98f1b881b18132ae43c320d48a02a16c163b58d1c70a680364148aa8ac138e', 'data/quarter_simulation.json': '2174aa14331dc5970d29ba55e5348ac2e97612f9f0113323294d421d888bc87e', 'data/quarter_simulation_week10.json': '70e4aed82f3f33c38ab3d3ad36c1d0630213dffa8eec2f12106fc6cfef2f5caa', 'data/quarter_simulation_week8.json': '2174aa14331dc5970d29ba55e5348ac2e97612f9f0113323294d421d888bc87e', 'data/quarter_simulation_week9.json': '11c5da73a91433601939a3f0dd9e3da467bd151ae182948faa584012ca6e7461', 'data/receiver_discovery.json': '7a475e06921342976fcbae54df9ff5d3a936f3ec4c97f3bb2421ee13e39731ae', 'data/td_catalog.json': '70fee9ac00078b5bb31e5281fec4eb868ad645370cb90571f02e479cca4d206b', 'data/value.json': 'cc28a19ea203d4247f855c5839632c667fa969e50aa7ade3b030cd6bbc8d1ccf', 'data/week10_context.json': '67e690a077b50e9d75dba4af03f176dcdd4d42b325c553027e59b907e9ee7821', 'data/week10_fixture_comparison.csv': 'd81e774f784fb3dc1e50d399403cb8f90479b9b24a10fdfd21912ee5b021d29f', 'data/week10_market_observations.csv': '85f653a62b671b77f3c6b25c93badeadcc46d98341c12ad10581198fae33c0e5', 'data/week10_public_prop_observations.csv': '8e5a0cbac2c943e34b7c28204ea58c2d4e74fbd8aa9b76b45ef320bb7215b58f', 'data/week8_final_results.csv': '27eb522168184ef7f3f03f78dd4e0c6b1958e1d0a33ba0300dc0c1cda5c1d784', 'data/week8_first_issued_Model_A_evaluation.csv': 'd426a8c87637487c07db1d854a1f1e9534d6d4596514e88e7defb833bb3e647e', 'data/week8_model_A_vs_B.csv': 'e3bf1bdf39beb298a6decf572541363ab92ccf7ca8e4af5d1869bc6a2e8742fa', 'data/week8_model_B_game_event_count_evaluation.csv': 'f5ede97dea17111ffcb1dc9f7a7acafa76bdce26660a060e13b46e18c1f67eab', 'data/week8_participation.csv': 'e6a1a8147cdcfa2bc874eb7d1ea148e7085a89806072218c0f8403a3928b41f5', 'data/week8_passing_evaluation.csv': '2d8b3fb26ad8341223cd83f5616138db0c7a70e7e83a450bb65ff5f1600c1ddf', 'data/week8_player_actuals.csv': '5f83f686d59dd48739416c9489be7f07d231c72e9fba979cc6b53923effec09e', 'data/week8_player_metrics.csv': '8ffa361be0a20e2b79bbba182fae136dfb1bae613e722885f4ad9a92305580f4', 'data/week8_research.json': 'd364a53b3b8c8031e256194bcebc6ebc0ee17ad7cdeea72ae06fd7b4dd1cc083', 'data/week8_scoring_events.csv': 'c7da2accb03841e518a6650ead0b9fcfcdc236ce1e4a82c44aaaff08a870ab7d', 'data/week8_team_actual_statistics.csv': '7f6b69387710b98f3df4eb474aad25576563f6e1252cdb7a6bdfd2c965f11353', 'data/week8_team_quarter_actuals.csv': 'b97bbf5e54c8b2249e7a66201efc1f9b4eeab2e0cdc517a1c645b7a49238f476', 'data/week8_team_scoring_bias.csv': '9061d76b0c40c58ea4ecf8d0bebdd31a98a02652213929a3ac2e0040e019bd08', 'data/week9_local_context.json': '780512ec1246ca851244a8f17bfc4317588a6849a112a76e330edb15cf4d79d6', 'data/week9_market_prices.csv': 'f4ef8caf3af18d40efab10ca048c81ad843d28057a79b6c9e348acf9ce9fefe2', 'data/week9_model_A.csv': 'da5be5ea47fe8e3b9e918b25a339a4db253f7b422febc7b1efdfd3346a72dee8', 'data/week9_passing_predictions.csv': '707e53bfd58e009b8c195865db2df77c4404bf90453339293cd1fc8cc0d03c81', 'data/week9_player_predictions.csv': '289e1c4f679f11db6130190ee7eef94f17664d5a6c1f2fde8caf30b91ebf19ac', 'data/week9_quarter_metrics.csv': 'eac84914cfaea3626eab1deaafea51a02974d6ec79ba83ccd461ddcbebf94528', 'data/week9_quarter_table.csv': 'af8362fbc13c6988864d269f9105975c74ceef95ca679068c202df7760d900ce', 'data/week9_scoring_event_means.csv': '8aaae1bd5189c9ee05a376e8fdef7652ba6a8f203d7d92dd170dd210157d0d43', 'data/week9_td_metrics.csv': 'fc3e5c61ad5dd6329bdc2f9a85633bbc9ecdf520f82eee0d68d13c7d4e9bf9a6', 'data/week9_top6_td.csv': '39672d3c78a21a4f6f0baf8e74b27ea008b30a64e77cf40564d5bbc8a07abd22', 'data/week9_yardage_metrics.csv': 'ff4317e9b53375a428856d101dd796a5f85ebac1cefff3c183fe5ae650b91f92', 'data/weekly_research.json': '25cf91d77a70c6ae05a94230e0245d97f11a886d62a98c158bc22446b5866ad8', 'data/weekly_research.sha256': '3884ff09c9cda787e0e0ef92cbdbefa544a842e1b7a702a0689b6b0a85f605d6', 'data/weeks1_9_team_metrics.csv': 'fd68a9048296412e295fc180339e6bfac9aa61b515ec8345427c39059a10bc1a', 'data/week_01.json': '0dbdf3d73e873e8010bcf8113031d1a8a750771b96361b43ab5ad05b684e18e0', 'data/week_02.json': '1e6c4577b1223c3e91f5f69349859dd4abc26b538648c608b904bb2f2bfce970', 'data/week_03.json': '552bfdb715af5bbf0e8f381bfbfa1797aec36695d0eb4c6c79fe72a6ba4303c4', 'data/week_04.json': 'e850700ddc15918eb227631fbbe7c0312fa5969e6b6cbaee05bae4ec3aff6f9d', 'data/week_05.json': '9b0b4da38ab793bc656a3538fb9c8f9bc6fae8ea58340b93b78839f204f5bd52', 'data/week_06.json': '20935fd505e36eba26492528e4e303f406e51b8445c146dbf57ed5d7cf92be81', 'data/research/2026-w08.json': '25cf91d77a70c6ae05a94230e0245d97f11a886d62a98c158bc22446b5866ad8', 'data/research/2026-w09-original-team-quarter-parent.json': '4535564b485c19d515dea0dcc5ead7280fa5a505d72b5c4c5e4c0c7bc74aca2e', 'data/research/2026-w09.json': 'd0b8dfb6a73f6b9bd3fe8f2069e8fe43f3ca52d2e51b887012eb489202c20146', 'data/research/2026-w10.json': '4a37a8c515ea9d00b299471c5ecac95d86e76d66c3f77a8037b43a1fa7a6454e'}
FLAGS = ("RESEARCH_AUTOMATION_APPROVED", "RESEARCH_PUBLISH_ENABLED", "INITIAL_RESEARCH_RELEASE_ENABLED", "RESEARCH_MONITOR_ENABLED")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha(path):
    return digest(path.read_bytes())


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def clock(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "Missing timezone")
    return parsed.astimezone(timezone.utc)


def now():
    return datetime.now(timezone.utc)


def download(url, headers=None):
    with urlopen(Request(url, headers=headers or {}), timeout=20) as handle:
        return handle.read()


def api(path, **params):
    query = "?" + urlencode(params) if params else ""
    return json.loads(download("https://api.github.com/repos/" + REPO + "/" + path + query, {
        "Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json",
    }))


def inventory(site):
    require(all(not p.is_symlink() for p in site.rglob("*")), "Symlinks are outside scope")
    return {p.relative_to(site).as_posix(): sha(p) for p in site.rglob("*") if p.is_file()}


def exact_site_scope(manifest):
    parent = manifest["previous_live_release"]
    before, after = parent["files"], manifest["files"]
    require(parent["release_id"] == "frontend-week9-tidy-29466c12d79894cf"
            and parent["main"] == "99debb21c2fc067fff1e3d4c9b3a0c1aec25c869", "Live parent identity differs")
    require(digest(json.dumps(before, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            == PARENT_ASSETS_SHA256, "Wrong pinned live parent inventory")
    require(before.keys() <= after.keys(), "An inherited site file was deleted")
    changed = {name: after[name] for name in after if after[name] != before.get(name)}
    require(changed == manifest["changed_site_files"] == TIDY_HASHES, "Candidate differs from the exact reviewed scope")
    require(all(after[name] == before[name] for name in before if name.startswith("data/") and
                name not in {"data/hosted_catalog.json", "data/td_catalog.json", "data/quarter_catalog.json"}),
            "A historical prediction or model asset changed")


def package(base=BASE):
    path = base / "publication_manifest.json"
    require(sha(path) == MANIFEST_SHA256, "Pinned final manifest differs")
    manifest = read(path)
    require(manifest["schema"] == "controlled-week10-archival-recovery-v1"
            and manifest["release_id"] == RELEASE_ID, "Wrong final release contract")
    require(manifest["repository"] == REPO and manifest["destination"] == SITE, "Wrong publication destination")
    require(manifest["production_certified"] is False and manifest["publication_authorized"] is False
            and manifest["unvalidated_probability_displays_excluded"] is True, "Research scope differs")
    require(len(manifest["files"]) == manifest["file_count"] == 431, "Candidate inventory size differs")
    require(inventory(base / "site") == manifest["files"], "Candidate bytes differ")
    exact_site_scope(manifest)
    require(read(base / "site/release_receipt.json")["release_id"] == RELEASE_ID, "Site release identity differs")
    require(len(manifest["fixture_cutoffs"]) == 14
            and min(manifest["fixture_cutoffs"].values()) == manifest["first_cutoff_utc"],
            "Fixture cutoff contract differs")
    period = read(base / "site/data/research/2026-w10.json")
    quarters = read(base / "site/data/quarter_simulation_week10.json")
    c = read(base / "site/data/model_c_week10.json")
    require(manifest["eligible_week9_game_ids"] == EXPECTED_HISTORY and len(EXPECTED_HISTORY) == 15,
            "Full Week 9 history differs")
    require(sha(base / "site/data/research/2026-w10.json") == c["parent_a_sha256"]
            == manifest["model_a_parent_sha256"], "Model C parent differs")
    require(sha(base / "site/data/quarter_simulation_week10.json") == manifest["model_b_parent_sha256"],
            "Model B parent differs")
    require(c["edition"] == "FINAL" and c["actionable"] is False
            and c["edition_manifest_sha256"] == manifest["model_c_edition_manifest_sha256"], "Final C edition differs")
    require(len(period["games"]) == len(quarters["fixtures"]) == len(c["fixtures"]) == 14,
            "Model fixture population differs")
    boundary = clock("2026-11-12T01:15:00+00:00")
    for g in period["games"]:
        cutoff = clock(manifest["fixture_cutoffs"][g["game_id"]])
        require(boundary < clock(g["feature_asof_utc"]) <= clock(g["created_at_utc"]) < cutoff,
                "Model A issue falls outside the final history window")
    for g in quarters["fixtures"]:
        require(boundary < clock(g["feature_asof_utc"]) <= clock(g["created_at_utc"])
                < clock(manifest["fixture_cutoffs"][g["game_id"]]), "Model B issue clock differs")
    require(all(not g["alerts"] and "experimental_win_frequencies" not in g["C"]
                and "experimental_win_frequencies" not in g["NB"] for g in c["fixtures"]),
            "Unvalidated probability or alert display")
    archive_contract(manifest,base)
    return manifest


def approval_document(env, base=BASE):
    """Require a fresh, unedited owner comment on the exact merged installation PR."""
    comment_id = env.get("APPROVAL_COMMENT_ID", "")
    require(comment_id.isascii() and comment_id.isdecimal() and 0 < len(comment_id) <= 20, "Owner approval comment ID required")
    comment = api("issues/comments/" + comment_id)
    require(str(comment.get("id")) == comment_id and comment.get("user", {}).get("login") == "racingmodel"
            and comment.get("author_association") == "OWNER", "Approval comment must be written by the repository owner")
    require(comment["updated_at"] == comment["created_at"], "Edited owner approval comment is not accepted")
    prefix = "https://api.github.com/repos/" + REPO + "/issues/"
    issue_url = comment.get("issue_url", "")
    require(issue_url.startswith(prefix) and issue_url[len(prefix):].isascii() and issue_url[len(prefix):].isdecimal(),
            "Approval comment must belong to this repository")
    pr = api("pulls/" + issue_url[len(prefix):])
    require(pr.get("merged") is True and pr.get("merge_commit_sha") == env["GITHUB_SHA"]
            and pr["base"]["ref"] == "main" and pr["base"]["repo"]["full_name"] == REPO
            and pr["head"]["ref"] == REVIEW_BRANCH
            and pr["head"]["repo"]["full_name"] == REPO, "Approval must be on the exact merged Week 10 installation PR")
    observed = clock(comment["created_at"])
    require(clock(pr["merged_at"]) <= observed, "Approval comment predates the merge")
    body = comment.get("body", "").strip()
    require(len(body) <= 4096 and body.startswith("```json\n") and body.endswith("\n```"), "Expected one JSON approval block")
    grant = json.loads(body[len("```json\n"):-len("\n```")])
    require(grant.get("installation_head") == env["GITHUB_SHA"], "Owner approval binds a different commit")
    return grant | {"approved_at_utc": observed.isoformat(), "human_approval_reference": comment["html_url"]}


def authority(manifest, env, instant, base=BASE):
    require(env.get("GITHUB_REPOSITORY") == REPO and env.get("GITHUB_REF") == "refs/heads/main", "Wrong repo or branch")
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_RUN_ATTEMPT") == "1",
            "Owner dispatch only, one attempt")
    require(env.get("GITHUB_ACTOR") == env.get("GITHUB_TRIGGERING_ACTOR") == "racingmodel", "Wrong authenticated owner")
    require(env.get("INSTALLATION_HEAD") == env.get("GITHUB_SHA") and len(env.get("GITHUB_SHA", "")) == 40, "Wrong installation head")
    require(all(env.get(flag) == "false" for flag in FLAGS), "A routine publication flag is enabled or absent")
    require(env.get("REQUESTED_RELEASE") == manifest["release_id"], "Wrong release")
    control_path = base / "controls.json"
    request_path = base / "review_request.json"
    require(sha(control_path) == env.get("CONTROL_SHA256"), "Controls differ")
    controls = read(control_path)
    require(set(controls["files"]) == {"scripts/verify_week10_publication.py", "week10_release/review_request.json", "tests/test_week10_recovery_publication.py"}, "Wrong control inventory")
    require(all(sha(base.parent / name) == expected for name, expected in controls["files"].items()), "Control bytes differ")
    request = read(request_path)
    request_sha = sha(request_path)
    manifest_sha = sha(base / "publication_manifest.json")
    require(request_sha == env.get("REQUESTED_SHA"), "Wrong reviewed request")
    verify_asset_merge(request,env)
    grant = approval_document(env, base)
    for doc in (request, grant):
        require(doc["release_id"] == manifest["release_id"] and doc["manifest_sha256"] == manifest_sha
                and doc["repository"] == REPO and doc["destination"] == SITE and doc["scope"] == APPROVAL_SCOPE
                and doc["unattended"] is False, "Grant or request scope differs")
    require(grant["decision"] == "APPROVED" and grant["request_sha256"] == request_sha
            and bool(grant["human_approval_reference"]), "Fresh explicit grant missing")
    earliest = clock(manifest["archival_policy"]["publication_deadline_utc"])
    for doc in (request,grant):
        require(doc.get('publication_mode')==manifest['publication_mode'] and doc.get('archived_fixture_ids')==['2026_10_WAS_NYG'] and clock(doc.get('publication_deadline_utc','1970-01-01T00:00:00Z'))==earliest, 'Archival approval scope differs')
    approved = clock(grant["approved_at_utc"])
    expires = clock(grant["expires_at_utc"])
    require(clock(request["created_at_utc"]) <= approved <= instant < expires <= earliest, "Approval expired or future")
    require(expires <= approved + timedelta(hours=2), "Approval interval exceeds two hours")
    return approved, expires


def cutoff_check(raw, manifest, observed, games):
    current = [r for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
               if (r["season"], r["week"], r["game_type"]) == ("2026", "10", "REG")]
    require(len(current) == 14 and set(games) == {r["game_id"] for r in current}, "Fresh schedule scope differs")
    for row in current:
        game = games[row["game_id"]]
        require((row["away_team"], row["home_team"]) == (game["away_team"], game["home_team"]), "Fixture teams differ")
        kickoff = datetime.fromisoformat(row["gameday"] + "T" + row["gametime"]).replace(
            tzinfo=ZoneInfo("America/New_York")
        ).astimezone(timezone.utc)
        cutoff = clock(manifest["fixture_cutoffs"][row["game_id"]])
        status = next((row[key].strip().upper() for key in ("game_status", "status", "game_state", "state") if row.get(key)), "")
        require(kickoff == clock(game["kickoff_utc"]) and cutoff == kickoff - timedelta(minutes=1), "Kickoff or cutoff changed")
        require(status in {"", "PRE", "PRE_KICKOFF", "PREGAME", "SCHEDULED", "NOT_STARTED"}
                and not row["home_score"] and not row["away_score"] and observed < cutoff, "Fixture closed, live, final or ambiguous")


def live_check(files, attempts, nonce):
    served = {path: expected for path, expected in files.items() if Path(path).name != ".nojekyll"}

    def verify(item):
        path, expected = item
        try:
            actual = digest(download(SITE + quote(path, safe="/") + "?release-check=" + nonce))
            return {"path": path, "actual": actual, "expected": expected, "match": actual == expected}
        except OSError as error:
            return {"path": path, "expected": expected, "match": False, "error": type(error).__name__}

    for attempt in range(attempts):
        with ThreadPoolExecutor(10) as pool:
            results = list(pool.map(verify, served.items()))
        if all(row["match"] for row in results):
            break
        if attempt + 1 < attempts:
            time.sleep(5)
    return results



def verify_live_parent(manifest, phase, run_id):
    prior = manifest["previous_live_release"]["files"]
    if phase == "review":
        expected = prior
    else:
        paths = ("release_receipt.json", "index.html", "data/research/2026-w08.json", "data/research/2026-w09.json", "classic_research.js", "player_roles.js", "local_week9.js", "local_week9.css")
        expected = {name: prior[name] for name in paths}
    result = live_check(expected, 2 if phase == "review" else 1, f"{phase}-week10-parent-{run_id}")
    served = {name: value for name, value in expected.items() if Path(name).name != ".nojekyll"}
    require(len(result) == len(served) and {row["path"]: row.get("actual") for row in result} == served,
            "Last-known-good Week 9 website drifted")
    return result


# GitHub's server-side CheckRun.deployment relationship is required. Caller-supplied
# deployment payloads, matching SHAs, timestamps and status URLs cannot prove ownership.
CHECK_RUN_DEPLOYMENT_QUERY = """query($id:ID!) {
  node(id:$id) { ... on CheckRun {
    id databaseId name status repository { nameWithOwner }
    checkSuite { databaseId app { databaseId slug } }
    deployment { id databaseId environment commitOid repository { nameWithOwner } }
  } }
}"""


def graphql(query, variables):
    request = Request("https://api.github.com/graphql", data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
                      headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Content-Type": "application/json"})
    with urlopen(request, timeout=20) as handle:
        result = json.load(handle)
    require(not result.get("errors") and isinstance(result.get("data"), dict), "CheckRun deployment identity unavailable")
    return result["data"]


def current_publish_deployment(env):
    """Authenticate the running job, then follow GitHub's server-side check-run edge."""
    run_id = env.get("GITHUB_RUN_ID", "")
    require(run_id.isascii() and run_id.isdecimal() and 0 < len(run_id) <= 20
            and env.get("GITHUB_JOB") == "publish" and env.get("GITHUB_RUN_ATTEMPT") == "1",
            "Current publish job identity missing")
    run = api("actions/runs/" + run_id)
    require(str(run.get("id")) == run_id and run.get("workflow_id") == int(WORKFLOW_ID)
            and run.get("path") == ".github/workflows/manual-frontend-correction.yml"
            and isinstance(run.get("check_suite_id"), int) and run["check_suite_id"] > 0
            and run.get("repository", {}).get("full_name") == REPO
            and run.get("head_sha") == env["GITHUB_SHA"] and run.get("head_branch") == "main"
            and run.get("event") == "workflow_dispatch" and run.get("run_attempt") == 1
            and run.get("status") == "in_progress" and run.get("conclusion") is None
            and run.get("actor", {}).get("login") == run.get("triggering_actor", {}).get("login") == "racingmodel",
            "Current workflow run identity differs")
    result = api(f"actions/runs/{run_id}/attempts/1/jobs", per_page=100)
    require(isinstance(result.get("total_count"), int) and result["total_count"] <= 100
            and result["total_count"] == len(result.get("jobs", [])), "Current job history incomplete")
    matches = [j for j in result["jobs"] if j.get("name") == "publish"]
    require(len(matches) == 1, "Current publish job is ambiguous")
    job = matches[0]
    job_id = job.get("id")
    require(isinstance(job_id, int) and job_id > 0 and job.get("run_id") == run["id"]
            and job.get("run_attempt") == 1 and job.get("head_sha") == env["GITHUB_SHA"]
            and job.get("head_branch") == "main" and job.get("status") == "in_progress"
            and job.get("conclusion") is None and bool(job.get("node_id"))
            and job.get("check_run_url") == f"https://api.github.com/repos/{REPO}/check-runs/{job_id}"
            and job.get("html_url") == f"https://github.com/{REPO}/actions/runs/{run_id}/job/{job_id}",
            "Current check-run job differs")
    check = graphql(CHECK_RUN_DEPLOYMENT_QUERY, {"id": job["node_id"]}).get("node") or {}
    suite = check.get("checkSuite") or {}
    require(check.get("id") == job["node_id"] and check.get("databaseId") == job_id
            and check.get("name") == "publish" and check.get("status") == "IN_PROGRESS"
            and check.get("repository", {}).get("nameWithOwner") == REPO
            and suite.get("databaseId") == run.get("check_suite_id")
            and suite.get("app", {}).get("databaseId") == 15368
            and suite.get("app", {}).get("slug") == "github-actions", "CheckRun does not bind the current job")
    deployment = check.get("deployment") or {}
    require(isinstance(deployment.get("databaseId"), int) and deployment["databaseId"] > 0
            and bool(deployment.get("id")) and deployment.get("environment") == "github-pages"
            and deployment.get("commitOid") == env["GITHUB_SHA"]
            and deployment.get("repository", {}).get("nameWithOwner") == REPO,
            "CheckRun deployment identity unavailable")
    return job, deployment


def verify_deployments(deployments, env, phase):
    """Allow only the current job's server-bound in-progress record in pre-deploy."""
    own = current_publish_deployment(env) if phase == "pre-deploy" else None
    recognised = []
    for deployment in deployments:
        states = api(f"deployments/{deployment['id']}/statuses", per_page=1)
        require(bool(states), "An active or ambiguous Pages deployment exists")
        status = states[0]
        if status.get("state") in {"success", "failure", "error", "inactive"}:
            continue
        require(own is not None and status.get("state") == "in_progress", "An active or ambiguous Pages deployment exists")
        job, linked = own
        require(deployment.get("id") == linked["databaseId"] and deployment.get("node_id") == linked["id"]
                and deployment.get("environment") == deployment.get("original_environment") == "github-pages"
                and deployment.get("sha") == env["GITHUB_SHA"] and deployment.get("ref") == "main"
                and deployment.get("task") == "deploy"
                and deployment.get("repository_url") == f"https://api.github.com/repos/{REPO}"
                and (deployment.get("performed_via_github_app") or {}).get("id") == 15368
                and (deployment.get("performed_via_github_app") or {}).get("slug") == "github-actions",
                "Active deployment is not bound to this publish job")
        require(status.get("environment") == "github-pages"
                and status.get("deployment_url") == f"https://api.github.com/repos/{REPO}/deployments/{deployment['id']}"
                and status.get("log_url") == status.get("target_url") == job["html_url"],
                "Active deployment status does not bind this job")
        recognised.append(deployment["id"])
    require(phase != "pre-deploy" or len(recognised) == 1, "Exactly one authenticated current deployment is required")
    return {"phase": phase, "authenticated_current_deployment_ids": recognised}


"""Additional release-specific authority, never a generic cutoff exemption."""

def archive_contract(manifest, base=BASE):
    policy=manifest.get('archival_policy',{})
    require(manifest.get('publication_mode')=='MIXED_ARCHIVE_AND_PREGAME'
            and policy.get('mode')=='EXACT_IMMUTABLE_PREGAME_ARCHIVE_ONLY', 'Explicit archival scope required')
    require(policy.get('archived_fixture_ids')==['2026_10_WAS_NYG'], 'Only the reviewed WAS-NYG archive is authorised')
    context_path=base/'site/data/week10_publication_context.json'
    require(sha(context_path)==policy.get('descriptor_sha256')==ARCHIVE_DESCRIPTOR_SHA256, 'Archival descriptor differs')
    context=read(context_path)
    require(context.get('schema')=='week10-mixed-publication-context-v1' and context.get('release_id')==RELEASE_ID
            and context.get('publication_mode')==manifest['publication_mode'] and context.get('publication_authorized') is False,
            'Archival display contract differs')
    require(manifest.get('frozen_data_hashes')==FROZEN_DATA_HASHES, 'Frozen prediction/source pins differ')
    require(all(sha(base/'site'/name)==expected for name,expected in FROZEN_DATA_HASHES.items()),
            'A sealed forecast, player probability or historical/source asset changed')
    games={g['game_id']:g for g in read(base/'site/data/research/2026-w10.json')['games']}
    remaining=policy.get('remaining_pregame_fixture_ids',[])
    require(len(remaining)==len(set(remaining))==13 and set(remaining)==set(games)-{'2026_10_WAS_NYG'}
            and remaining==context.get('pregame_fixture_ids'), 'Remaining pregame population differs')
    earliest=min(clock(manifest['fixture_cutoffs'][gid]) for gid in remaining)
    require(clock(policy['publication_deadline_utc'])==earliest==clock(context['next_publication_deadline_utc']),
            'Remaining fixture cutoff was extended')
    require(manifest['first_cutoff_utc']==policy['original_first_cutoff_utc']
            and clock(manifest['first_cutoff_utc'])==clock('2026-11-13T01:14:00Z'), 'Original first cutoff must remain unchanged')
    require(context.get('archived')==manifest.get('archived_fixture_records') and len(context['archived'])==1,
            'Archived forecast identity differs')
    period_game=games['2026_10_WAS_NYG'];item=context['archived'][0]
    require(item['game_id']==period_game['game_id'] and item['display_status']=='ARCHIVED_PREGAME'
            and item['actuals_included'] is False and period_game.get('actuals') is None,
            'Archive cannot become a postgame forecast or silently ingest results')
    require(clock(item['original_pregame_cutoff_utc'])==clock(period_game['cutoff_utc'])
            and clock(item['kickoff_utc'])==clock(period_game['kickoff_utc']), 'Archived fixture clock differs')
    require(item['forecast_record_sha256']==digest(json.dumps(period_game,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()),
            'Archived pregame forecast was revised')
    cutoff=clock(period_game['cutoff_utc'])
    require(clock(period_game['feature_asof_utc'])<=clock(period_game['created_at_utc'])==clock(item['forecast_issued_at_utc'])<cutoff,
            'Archived forecast was issued after its original cutoff')
    td=read(base/'site/data/original_td_week10.json');rows=[r for r in td['rows'] if r['game_id']==period_game['game_id']]
    require(len(rows)==29 and item['td_rows_sha256']==digest(json.dumps(rows,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()),
            'Archived TD pool or probabilities differ')
    require(all(clock(r['known_at_utc'])<=clock(r['created_at_utc'])<cutoff for r in rows)
            and clock(item['td_issued_at_utc'])==max(clock(r['created_at_utc']) for r in rows), 'Archived TD issue clock differs')
    context_game=next(m for m in read(base/'site/data/week10_context.json')['matches'] if m['game_id']==period_game['game_id'])
    quotes=context_game['bookmaker_quotes']
    require(len(quotes)==5 and item['bookmaker_updated_at_utc'] is None
            and all(q['bookmaker_updated_at_utc'] is None and q['actionable'] is False for q in quotes), 'Archive quotes are not current executable prices')
    require(all(clock(q['provider_updated_at_utc'])<=clock(q['retrieved_at_utc'])<cutoff for q in quotes)
            and clock(item['market_retrieved_at_utc'])==max(clock(q['retrieved_at_utc']) for q in quotes), 'Archived quote issue/retrieval clock differs')
    return context


def publication_clock_check(raw, manifest, observed, games):
    """New mixed scope: one byte-locked archive; all 13 other cutoffs stay strict.

    The original all-pregame cutoff_check remains unchanged and still rejects
    this late release. Only the separately reviewed mixed-publication contract
    enters this function, never an old pregame approval.
    """
    require(manifest.get('schema')=='controlled-week10-archival-recovery-v1'
            and manifest.get('publication_mode')=='MIXED_ARCHIVE_AND_PREGAME', 'Late publication requires the new reviewed scope')
    policy=manifest['archival_policy'];require(policy['archived_fixture_ids']==['2026_10_WAS_NYG'], 'Wrong archived fixture')
    require(observed<clock(policy['publication_deadline_utc']), 'Remaining fixture publication window closed')
    current=[r for r in csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))) if (r['season'],r['week'],r['game_type'])==('2026','10','REG')]
    require(len(current)==14 and len({r['game_id'] for r in current})==14 and set(games)=={r['game_id'] for r in current}, 'Fresh schedule scope differs')
    archive=[];pregame=[]
    for row in current:
        game=games[row['game_id']]
        require((row['away_team'],row['home_team'])==(game['away_team'],game['home_team']), 'Fixture teams differ')
        kickoff=datetime.fromisoformat(row['gameday']+'T'+row['gametime']).replace(tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc)
        cutoff=clock(manifest['fixture_cutoffs'][row['game_id']])
        require(kickoff==clock(game['kickoff_utc']) and cutoff==kickoff-timedelta(minutes=1), 'Kickoff or cutoff changed')
        status=next((row[key].strip().upper() for key in ('game_status','status','game_state','state') if row.get(key)), '')
        if row['game_id']=='2026_10_WAS_NYG':
            require(observed>=cutoff and clock(game['created_at_utc'])<cutoff, 'Archive is not a frozen pregame issue')
            require(status in {'','LIVE','IN_PROGRESS','INPROGRESS','HALFTIME','DELAYED','SUSPENDED','FINAL','FINAL/OT','FINAL_OT','COMPLETE','COMPLETED','CLOSED'}, 'Archived schedule state is contradictory or ambiguous')
            archive.append(row['game_id'])
        else:
            require(row['game_id'] in policy['remaining_pregame_fixture_ids'], 'Unreviewed archival expansion')
            require(status in {'','PRE','PRE_KICKOFF','PREGAME','SCHEDULED','NOT_STARTED'}
                    and not row['home_score'] and not row['away_score'] and observed<cutoff,
                    'Remaining fixture closed, live, final or ambiguous')
            pregame.append(row['game_id'])
    require(archive==policy['archived_fixture_ids'] and set(pregame)==set(policy['remaining_pregame_fixture_ids']), 'Mixed fixture population differs')
    return {'archive_only':archive,'strict_pregame':pregame,'original_cutoffs_unchanged':True}


def all_run_records(endpoint):
    """No event/status/SHA/date filters: those omitted the accepted HTTP-500 run."""
    records=[];expected=None
    for page in range(1,21):
        result=api(endpoint,per_page=100,page=page)
        count=result.get('total_count');batch=result.get('workflow_runs')
        require(isinstance(count,int) and 0<=count<=2000 and isinstance(batch,list) and len(batch)<=100, 'Run history incomplete')
        if expected is None:expected=count
        require(count==expected, 'Run inventory changed during pagination')
        records.extend(batch)
        if len(records)==expected:break
        require(len(batch)==100 and len(records)<expected, 'Run history pagination incomplete')
    require(len(records)==expected and len({r.get('id') for r in records})==expected, 'Run inventory is incomplete or duplicated')
    require(all(isinstance(r.get('id'),int) and isinstance(r.get('workflow_id'),int) and isinstance(r.get('head_sha'),str)
                and len(r['head_sha'])==40 and bool(r.get('status')) for r in records), 'Run identity or status unavailable')
    return records


def check_cleared_predecessor():
    run=api('actions/runs/43257307848');suite=api('check-suites/117139170263');jobs=api('actions/runs/43257307848/jobs',per_page=100)
    require(run.get('id')==43257307848 and run.get('workflow_id')==int(WORKFLOW_ID)
            and run.get('check_suite_id')==117139170263 and run.get('head_sha')==BASE_MAIN_SHA
            and run.get('status')==suite.get('status')=='completed'
            and run.get('conclusion')==suite.get('conclusion')=='cancelled'
            and suite.get('id')==117139170263 and jobs.get('total_count')==0 and jobs.get('jobs')==[],
            'Original accepted dispatch is not conclusively cleared')


def check_first_dispatch(env, request_created):
    del request_created  # Never use filtered history as proof of no dispatch.
    rid=env.get('GITHUB_RUN_ID','')
    require(rid.isascii() and rid.isdecimal() and env.get('GITHUB_RUN_ATTEMPT')=='1', 'One authenticated run attempt required')
    check_cleared_predecessor()
    current=api('actions/runs/'+rid)
    require(str(current.get('id'))==rid and current.get('workflow_id')==int(WORKFLOW_ID)
            and current.get('head_sha')==env.get('GITHUB_SHA') and current.get('event')=='workflow_dispatch'
            and current.get('run_attempt')==1 and current.get('head_branch')=='main'
            and current.get('status')=='in_progress' and current.get('conclusion') is None
            and current.get('repository',{}).get('full_name')==REPO
            and current.get('actor',{}).get('login')==current.get('triggering_actor',{}).get('login')=='racingmodel',
            'Current dispatch identity differs')
    repository=all_run_records('actions/runs');workflow=all_run_records('actions/workflows/'+WORKFLOW_ID+'/runs')
    repo_workflow={r['id']:r for r in repository if r['workflow_id']==int(WORKFLOW_ID)}
    by_workflow={r['id']:r for r in workflow}
    require(repo_workflow.keys()==by_workflow.keys() and int(rid) in repo_workflow, 'Direct and unfiltered histories disagree')
    for key in repo_workflow:
        require(all(repo_workflow[key].get(k)==by_workflow[key].get(k) for k in ('head_sha','event','status','run_attempt','workflow_id')),
                'Unfiltered run inventories disagree')
    require(all(repo_workflow[int(rid)].get(k)==current.get(k) for k in ('head_sha','event','status','run_attempt','workflow_id')),
            'Direct run differs from unfiltered inventory')
    require(not any(str(r['id'])!=rid and r['status']!='completed' for r in repository), 'Other repository workflow activity')
    require(not any(str(r['id'])!=rid and r['head_sha']==env['GITHUB_SHA'] for r in repository),
            'One-time dispatch already attempted for this commit')
    return {'unfiltered_repository_runs':len(repository),'unfiltered_workflow_runs':len(workflow),'direct_current_run':int(rid),'cleared_predecessor':43257307848}


def verify_dispatch_workflow_states():
    workflows=api('actions/workflows',per_page=100)
    require(workflows.get('total_count')==len(workflows.get('workflows',[]))==5, 'Workflow inventory differs')
    require(all(w.get('state')==('active' if str(w.get('id'))==WORKFLOW_ID else 'disabled_manually') for w in workflows['workflows']),
            'Only the authorised manual workflow may be enabled')
    variables=api('actions/variables',per_page=100)
    require(variables.get('total_count',len(variables.get('variables',[])))<=100, 'Variable inventory incomplete')
    flags={v['name']:v['value'] for v in variables.get('variables',[])}
    require(all(flags.get(name)=='false' for name in FLAGS), 'A routine flag changed')


def verify_asset_merge(request, env):
    number=request.get('asset_pr_number')
    require(isinstance(number,int) and number>0 and request.get('base_main')==BASE_MAIN_SHA, 'Reviewed asset parent missing')
    asset_pr=api('pulls/'+str(number))
    require(asset_pr.get('merged') is True and asset_pr['base']['ref']=='main'
            and asset_pr['base']['repo']['full_name']==asset_pr['head']['repo']['full_name']==REPO
            and asset_pr['head']['sha']==request.get('asset_pr_head_sha')
            and asset_pr['head']['ref']==request.get('asset_review_branch'), 'Asset PR/head differs or is unmerged')
    commit=api('commits/'+asset_pr['merge_commit_sha'])
    require([r['sha'] for r in commit.get('parents',[])]==[BASE_MAIN_SHA,request['asset_pr_head_sha']], 'Asset PR must use the reviewed merge-commit parent')
    installed=api('commits/'+env['GITHUB_SHA'])
    require(len(installed.get('parents',[]))==2 and installed['parents'][0]['sha']==asset_pr['merge_commit_sha'],
            'Controls installation is not directly above the reviewed asset merge')
    return asset_pr['merge_commit_sha']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["review", "pre-deploy", "live"])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    env = os.environ
    manifest = package(BASE)
    result = {"phase": args.phase, "release_id": manifest["release_id"], "manifest_sha256": sha(BASE / "publication_manifest.json"),
              "approval_comment_id": env.get("APPROVAL_COMMENT_ID"),
              "run_id": env.get("GITHUB_RUN_ID"), "observed_at_utc": now().isoformat()}
    if args.phase == "live":
        results = live_check(manifest["files"], 5, str(env["GITHUB_RUN_ID"]))
        result.update(files=results, status="PASS" if len(results) == sum(Path(p).name != ".nojekyll" for p in manifest["files"]) and all(row["match"] for row in results) else "FAIL")
    else:
        _, expires = authority(manifest, env, now(), BASE)
        require(api("git/ref/heads/main")["object"]["sha"] == env["GITHUB_SHA"], "main moved after review")
        verify_dispatch_workflow_states()
        require(api("pages").get("build_type") == "workflow", "Pages configuration differs")
        deployments = api("deployments", environment="github-pages", per_page=100)
        require(len(deployments) < 100, "Deployment history incomplete")
        result["deployment_check"] = verify_deployments(deployments, env, args.phase)
        result["dispatch_inventory"] = check_first_dispatch(env, read(BASE / "review_request.json")["created_at_utc"])
        prior_check = verify_live_parent(manifest, args.phase, env["GITHUB_RUN_ID"])
        schedule = download(SCHEDULE)
        observed = now()
        games = {g["game_id"]: g for g in read(BASE / "site/data/research/2026-w10.json")["games"]}
        result["fixture_check"] = publication_clock_check(schedule, manifest, observed, games)
        require(now() < expires, "Approval expired while verifying")
        result.update(status="PASS", checked_at_utc=now().isoformat(), schedule_sha256=digest(schedule), live_parent=prior_check)
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=True, indent=2)
        handle.write("\n")
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
