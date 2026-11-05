"""Exact Week 9 tidy release guard for the supervised Pages workflow."""

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
BASE = Path("week9_release")
SITE = "https://racingmodel.github.io/nfl-2026-visualizer/"
SCHEDULE = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
WORKFLOW_ID = "399724330"
RELEASE_ID = 'frontend-week9-tidy-29466c12d79894cf'
MANIFEST_SHA256 = '471ade754b7ac785d88a4ed1f57c8a1d4921f36bbdac7771f769c94e7ca5fee9'
PARENT_ASSETS_SHA256 = '8c37b193b4729e6b110f4e0046314fec8802a7c9f7b32b39f86d288e85b006d9'
REVIEW_BRANCH = 'codex/week9-tidy-controls-29466c12d79894cf'
APPROVAL_SCOPE = 'WEEK9_TIDY_EXACT_STATIC_ONCE'
TIDY_HASHES = {'classic_research.js': '3b7123b76efb7bc03a49cb82388d1250f84c17b25d13bba6d02a9074e1a0baef', 'local_week9.css': '54d45c33472bd99047acf8dcdbfe7ef4731f7486c75de87a4d9d0badb854954d', 'local_week9.js': 'c0265148acef4f2c210f4c8825b67fedc067c4530183066eb0ad709c0bdda2a9', 'player_roles.js': '77d580e443eb30f8b23f897359c0b13f5102eebed9330c5e55702360a85cfe42'}
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
    require(digest(json.dumps(before, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            == PARENT_ASSETS_SHA256, "Wrong pinned Week 9 parent inventory")
    require(before.keys() == after.keys(), "Candidate file set differs from parent")
    changed = {name: after[name] for name in after if after[name] != before[name]}
    require(changed == manifest["changed_site_files"] == TIDY_HASHES, "Candidate is outside the exact four-file scope")
    require(sum(name.startswith("data/") for name in after) == 60, "Frozen data inventory differs")
    require(after["release_receipt.json"] == before["release_receipt.json"]
            == manifest["inherited_site_receipt_sha256"], "Inherited site receipt hash differs")


def package(base=BASE):
    path = base / "publication_manifest.json"
    require(sha(path) == MANIFEST_SHA256, "Pinned tidy manifest differs")
    manifest = read(path)
    require(manifest["schema"] == "controlled-week9-static-tidy-v1" and manifest["release_id"] == RELEASE_ID,
            "Wrong tidy manifest contract")
    require(manifest["repository"] == REPO and manifest["destination"] == SITE, "Wrong publication destination")
    require(manifest["production_certified"] is False and manifest["publication_authorized"] is False
            and manifest["research_only_shadow_models_excluded"] is True, "Research scope differs")
    require(len(manifest["files"]) == manifest["file_count"] == 417, "Candidate inventory size differs")
    require(inventory(base / "site") == manifest["files"], "Candidate bytes differ")
    exact_site_scope(manifest)
    require(read(base / "site/release_receipt.json")["release_id"] == manifest["previous_live_release"]["release_id"],
            "Inherited Week 9 site receipt differs")
    require(len(manifest["fixture_cutoffs"]) == 15 and min(manifest["fixture_cutoffs"].values()) == manifest["first_cutoff_utc"],
            "Fixture cutoff contract differs")
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
            and pr["head"]["repo"]["full_name"] == REPO, "Approval must be on the exact merged Week 9 installation PR")
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
    require(set(controls["files"]) == {"scripts/verify_week9_publication.py", "week9_release/review_request.json"}, "Wrong control inventory")
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
               if (r["season"], r["week"], r["game_type"]) == ("2026", "9", "REG")]
    require(len(current) == 15 and set(games) == {r["game_id"] for r in current}, "Fresh schedule scope differs")
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
        paths = ("release_receipt.json", "index.html", "data/research/2026-w08.json", "data/research/2026-w09.json", *TIDY_HASHES)
        expected = {name: prior[name] for name in paths}
    result = live_check(expected, 2 if phase == "review" else 1, f"{phase}-week9-parent-{run_id}")
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
        result.update(files=results, status="PASS" if len(results) == 412 and all(row["match"] for row in results) else "FAIL")
    else:
        _, expires = authority(manifest, env, now())
        require(api("git/ref/heads/main")["object"]["sha"] == env["GITHUB_SHA"], "main moved after review")
        active = []
        for state in ("queued", "in_progress", "requested", "waiting", "pending"):
            active.extend(api("actions/runs", status=state, per_page=100)["workflow_runs"])
        require(not any(str(r["id"]) != env["GITHUB_RUN_ID"] for r in active), "Other repository workflow activity")
        check_first_dispatch(env, read(BASE / "review_request.json")["created_at_utc"])
        prior_check = verify_live_parent(manifest, args.phase, env["GITHUB_RUN_ID"])
        schedule = download(SCHEDULE)
        observed = now()
        games = {g["game_id"]: g for g in read(BASE / "site/data/research/2026-w09.json")["games"]}
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
