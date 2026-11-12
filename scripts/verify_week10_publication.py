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
RELEASE_ID = 'frontend-week10-final-bbf8e10133b9118f'
MANIFEST_SHA256 = '3f941c63beb60c79891891503b2dc9000982e04ac1bf71fbf3deac7af7815f03'
PARENT_ASSETS_SHA256 = '29466c12d79894cf18c3490c8bb4cb2687ae561717d1c9bddfc21305942e9227'
REVIEW_BRANCH = 'codex/week10-final-controls-bbf8e10133b9118f'
APPROVAL_SCOPE = 'WEEK10_FINAL_MODEL_C_STATIC_ONCE'
TIDY_HASHES = {'classic_research.js': '2c8040af068a3efdb1e6b3325dde16013d35788a597fdb49ea82f3a0a3f69587', 'data/hosted_catalog.json': '8ca4461db20547aa80563bfef945b0864038dce22eb734a1dfdf34a9164d0c13', 'data/model_c_week10.json': '71e819237db60e7b1bbf861a7db49b97e4ddcc749cf1abb558e3fc369aedcf2f', 'data/original_td_week10.json': '980136715f31d1b5fba3fe49b21def6153a29fd0541ca97b0e5e52d8576d29bf', 'data/quarter_catalog.json': '2b98f1b881b18132ae43c320d48a02a16c163b58d1c70a680364148aa8ac138e', 'data/quarter_simulation_week10.json': '70e4aed82f3f33c38ab3d3ad36c1d0630213dffa8eec2f12106fc6cfef2f5caa', 'data/research/2026-w10.json': '4a37a8c515ea9d00b299471c5ecac95d86e76d66c3f77a8037b43a1fa7a6454e', 'data/td_catalog.json': '350765bdfd61cf3755367d5febb4094dfdff73ad32a01a3277bc449e9e951f4f', 'data/week10_context.json': '01f0ff36963845425ff35c0b2c085f002adc8230faafc95b134b3453892165c4', 'data/week10_fixture_comparison.csv': 'd81e774f784fb3dc1e50d399403cb8f90479b9b24a10fdfd21912ee5b021d29f', 'data/week10_market_observations.csv': 'aefce77a5d5b8ee53ac3e3a5610fb702637954d995f19e29847dd04f7e14ebb8', 'data/week10_public_prop_observations.csv': '8e5a0cbac2c943e34b7c28204ea58c2d4e74fbd8aa9b76b45ef320bb7215b58f', 'data/week9_quarter_metrics.csv': 'eac84914cfaea3626eab1deaafea51a02974d6ec79ba83ccd461ddcbebf94528', 'data/week9_td_metrics.csv': 'fc3e5c61ad5dd6329bdc2f9a85633bbc9ecdf520f82eee0d68d13c7d4e9bf9a6', 'data/week9_yardage_metrics.csv': 'ff4317e9b53375a428856d101dd796a5f85ebac1cefff3c183fe5ae650b91f92', 'data/weeks1_9_team_metrics.csv': 'fd68a9048296412e295fc180339e6bfac9aa61b515ec8345427c39059a10bc1a', 'index.html': '06139a1031cec53d33defa7f6767ed6bfbe489e5ff208cd60d907940d51770b7', 'model.html': 'd726317aee0afac31d9424271bc4f3a16f2d197d5e75475f519da4d5dedcf72b', 'performance.html': '17f4473ebca020ce0b3c8c0a90a7196b9a28cf80c52481e4ac04d2b33ddc7d47', 'player_roles.js': 'ffb8cdbbb60870d46793640def8ed116dbc188467aea7abe59e00c016df75588', 'players.html': 'ef2c1750d2489c4ecf819a6477c0bb4d550d532a2a0009769315f69c208d1356', 'prediction_display.js': '291e4869fecd15ab3d317590471f003150aa2e4945a7cca05930a43eb6cdf2c0', 'release_receipt.json': 'f095d159e7403a9b1cb0ca0d2c13cb91ed87507234651257b335c446303f8498', 'value.html': '4156aa26782d4e2f2f4cf369ac588429c574d0669cd17e07ceddafda1eb09ced', 'week10_preview.js': '233428a045dc2176447e5b46ae4f4249089cc926ce7b45ee7919b3ce41f2400b'}
EXPECTED_HISTORY = ['2026_09_ARI_SEA', '2026_09_BUF_MIN', '2026_09_CIN_ATL', '2026_09_CLE_NO', '2026_09_DAL_IND', '2026_09_DEN_CAR', '2026_09_DET_MIA', '2026_09_GB_NE', '2026_09_HOU_LAC', '2026_09_JAX_BAL', '2026_09_LA_WAS', '2026_09_LV_SF', '2026_09_NYG_PHI', '2026_09_NYJ_KC', '2026_09_TB_CHI']
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
    require(manifest["schema"] == "controlled-week10-final-model-c-v1"
            and manifest["release_id"] == RELEASE_ID, "Wrong final release contract")
    require(manifest["repository"] == REPO and manifest["destination"] == SITE, "Wrong publication destination")
    require(manifest["production_certified"] is False and manifest["publication_authorized"] is False
            and manifest["unvalidated_probability_displays_excluded"] is True, "Research scope differs")
    require(len(manifest["files"]) == manifest["file_count"] == 430, "Candidate inventory size differs")
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
    require(set(controls["files"]) == {"scripts/verify_week10_publication.py", "week10_release/review_request.json"}, "Wrong control inventory")
    require(all(sha(base.parent / name) == expected for name, expected in controls["files"].items()), "Control bytes differ")
    request = read(request_path)
    request_sha = sha(request_path)
    manifest_sha = sha(base / "publication_manifest.json")
    require(request_sha == env.get("REQUESTED_SHA"), "Wrong reviewed request")
    grant = approval_document(env, base)
    for doc in (request, grant):
        require(doc["release_id"] == manifest["release_id"] and doc["manifest_sha256"] == manifest_sha
                and doc["repository"] == REPO and doc["destination"] == SITE and doc["scope"] == APPROVAL_SCOPE
                and doc["unattended"] is False, "Grant or request scope differs")
    require(grant["decision"] == "APPROVED" and grant["request_sha256"] == request_sha
            and bool(grant["human_approval_reference"]), "Fresh explicit grant missing")
    earliest = clock(manifest["first_cutoff_utc"])
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


def check_first_dispatch(env, request_created):
    runs = api(f"actions/workflows/{WORKFLOW_ID}/runs", event="workflow_dispatch",
               created=">=" + request_created, per_page=100)
    require(runs["total_count"] <= 100, "Run history incomplete")
    require(not any(str(run["id"]) != env["GITHUB_RUN_ID"] and run["head_sha"] == env["GITHUB_SHA"]
                    for run in runs["workflow_runs"]), "One-time dispatch already attempted for this commit")


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["review", "pre-deploy", "live"])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    env = os.environ
    manifest = package()
    result = {"phase": args.phase, "release_id": manifest["release_id"], "manifest_sha256": sha(BASE / "publication_manifest.json"),
              "approval_comment_id": env.get("APPROVAL_COMMENT_ID"),
              "run_id": env.get("GITHUB_RUN_ID"), "observed_at_utc": now().isoformat()}
    if args.phase == "live":
        results = live_check(manifest["files"], 5, str(env["GITHUB_RUN_ID"]))
        result.update(files=results, status="PASS" if len(results) == sum(Path(p).name != ".nojekyll" for p in manifest["files"]) and all(row["match"] for row in results) else "FAIL")
    else:
        _, expires = authority(manifest, env, now())
        require(api("git/ref/heads/main")["object"]["sha"] == env["GITHUB_SHA"], "main moved after review")
        active = []
        for state in ("queued", "in_progress", "requested", "waiting", "pending"):
            active.extend(api("actions/runs", status=state, per_page=100)["workflow_runs"])
        require(not any(str(r["id"]) != env["GITHUB_RUN_ID"] for r in active), "Other repository workflow activity")
        require(api("pages").get("build_type") == "workflow", "Pages configuration differs")
        deployments = api("deployments", environment="github-pages", per_page=100)
        require(len(deployments) < 100, "Deployment history incomplete")
        for deployment in deployments:
            states = api(f"deployments/{deployment['id']}/statuses", per_page=1)
            require(states and states[0]["state"] in {"success", "failure", "error", "inactive"},
                    "An active or ambiguous Pages deployment exists")
        check_first_dispatch(env, read(BASE / "review_request.json")["created_at_utc"])
        prior_check = verify_live_parent(manifest, args.phase, env["GITHUB_RUN_ID"])
        schedule = download(SCHEDULE)
        observed = now()
        games = {g["game_id"]: g for g in read(BASE / "site/data/research/2026-w10.json")["games"]}
        cutoff_check(schedule, manifest, observed, games)
        require(now() < expires, "Approval expired while verifying")
        result.update(status="PASS", checked_at_utc=now().isoformat(), schedule_sha256=digest(schedule), live_parent=prior_check)
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=True, indent=2)
        handle.write("\n")
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
