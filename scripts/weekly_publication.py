"""Separate exact initial publication from later fixed-policy automation approval.

No command deploys, enables a workflow, or signs without an operator reference.
The inference engine and its existing publication validation remain unchanged.
"""
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
import shutil
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nfl2026.weekly_research.core import (
    canonical, clock, digest, immutable, load, sha, utc, validate_publication,
)

INITIAL = "INITIAL_EXACT_RELEASE_ONLY"
AUTOMATED = "UNATTENDED_FIXED_MODEL_UPDATES"
DYNAMIC = {"data/weekly_research.json", "data/weekly_research.sha256", "release_receipt.json"}


def verify_controls(path):
    if path is None or sha(path) != os.environ.get("RESEARCH_DEPLOYMENT_CONTROL_SHA256"):
        raise ValueError("Externally pinned deployment controls differ/absent")
    controls = load(path)
    for n, expected in controls["files"].items():
        p = (ROOT / n).resolve()
        if not p.is_relative_to(ROOT.resolve()) or not p.is_file() or sha(p) != expected:
            raise ValueError("Reviewed deployment implementation differs: " + n)
    return sha(path)


def inventory(site):
    site = Path(site)
    if any(p.is_symlink() for p in site.rglob("*")):
        raise ValueError("Symlink in publication tree")
    return {p.relative_to(site).as_posix(): sha(p) for p in site.rglob("*") if p.is_file()}


def verify_snapshot(site, manifest, policy):
    actual = inventory(site)
    if actual != manifest["files"]:
        raise ValueError("Exact deployment inventory differs")
    if any(n.endswith((".joblib", ".pkl", ".pickle", ".tar.gz", ".py")) for n in actual):
        raise ValueError("Private/code artifacts in public site")
    receipt = load(Path(site) / "release_receipt.json")
    if receipt["release_id"] != manifest["candidate_release_id"]:
        raise ValueError("Candidate identity differs")
    if sha(Path(site) / "release_receipt.json") != manifest["candidate_manifest_sha256"]:
        raise ValueError("Candidate manifest differs")
    for n, expected in receipt["files"].items():
        if actual.get(n) != expected:
            raise ValueError("Immutable candidate asset differs: " + n)
    if {n: h for n, h in actual.items() if n not in DYNAMIC} != policy["static_assets"]:
        raise ValueError("Static preservation envelope differs")
    data = load(Path(site) / "data/weekly_research.json")
    if sha(Path(site) / "data/weekly_research.json") != (Path(site) / "data/weekly_research.sha256").read_text(encoding="utf-8").strip():
        raise ValueError("Data hash differs")
    if len(data["games"]) != manifest["fixtures"] or sum(len(g["players"]) for g in data["games"]) != manifest["conditional_players"]:
        raise ValueError("Forecast population differs")
    return data


def check_grant(approval, purpose, key, now, policy):
    if approval.get("decision") != "APPROVED" or not key or len(key) < 32:
        raise ValueError("Separate explicit approval/signing authority absent")
    grant = approval.get("grant", {})
    signature = hmac.new(key.encode(), canonical(grant), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, approval.get("signature", "")):
        raise ValueError("Approval signature differs")
    if grant.get("purpose") != purpose or grant.get("policy_sha256") != digest(policy):
        raise ValueError("Approval stage/policy differs")
    if approval.get("policy_sha256") != digest(policy):
        raise ValueError("Approval policy differs")
    if not utc(grant["approved_at_utc"]) <= utc(now) < utc(grant["expires_at_utc"]):
        raise ValueError("Approval future/expired")
    if not grant.get("explicit_human_approval_reference"):
        raise ValueError("Explicit human approval reference absent")
    return grant


def verify_initial(site, manifest_path, request_path, policy_path, approval_path, key, now=None, control_path=None):
    now = now or clock()
    manifest, request, policy, approval = map(load, [manifest_path, request_path, policy_path, approval_path])
    if os.environ.get("RESEARCH_INITIAL_PUBLICATION_VERIFIED", "false") == "true":
        raise ValueError("Initial publication already verified; initial authority retired")
    request_sha = sha(request_path)
    if request_sha != os.environ.get("INITIAL_RESEARCH_REQUEST_SHA256"):
        raise ValueError("Externally pinned initial request differs/absent")
    if request["purpose"] != INITIAL or request["unattended_authorized"] is not False:
        raise ValueError("Initial scope expanded")
    if sha(manifest_path) != request["deployment_manifest_sha256"] or digest(policy) != request["policy_sha256"]:
        raise ValueError("Reviewed package differs")
    if verify_controls(control_path) != request["deployment_control_sha256"]:
        raise ValueError("Initial deployment control scope differs")
    if not utc(request["created_at_utc"]) <= utc(now) < utc(request["review_expires_at_utc"]):
        raise ValueError("Initial review expired")
    grant = check_grant(approval, INITIAL, key, now, policy)
    if grant.get("request_sha256") != request_sha or utc(grant["expires_at_utc"]) > utc(request["review_expires_at_utc"]):
        raise ValueError("Initial approval does not bind exact request/expiry")
    data = verify_snapshot(site, manifest, policy)
    validate_publication(policy, approval, key, now, site, data)
    return dict(status="PASS_EXACT_INITIAL_APPROVAL", purpose=INITIAL, request_sha256=request_sha,
                policy_sha256=digest(policy), deployment_manifest_sha256=sha(manifest_path),
                data_sha256=sha(Path(site) / "data/weekly_research.json"),
                release_id=manifest["candidate_release_id"], unattended_authorized=False)


def verify_unattended(site, policy_path, approval_path, live_path, bundle, state, key, now=None, control_path=None):
    now = now or clock()
    policy, approval, live = map(load, [policy_path, approval_path, live_path])
    grant = check_grant(approval, AUTOMATED, key, now, policy)
    if live.get("status") != "PASS_LIVE_INITIAL_PUBLICATION" or live.get("policy_sha256") != digest(policy):
        raise ValueError("Successful initial live publication evidence absent")
    if grant.get("initial_live_verification_sha256") != sha(live_path):
        raise ValueError("Separate automation approval does not bind live verification")
    if utc(grant["approved_at_utc"]) <= utc(live["actual_completed_at_utc"]):
        raise ValueError("Automation approval must follow actual initial verification")
    control_sha = verify_controls(control_path)
    if grant.get("deployment_control_sha256") != control_sha or live.get("deployment_control_sha256") != control_sha:
        raise ValueError("Separately approved deployment controls differ")
    # Existing implementation/schema/model/accepted-state/publication gates still run.
    from scripts.weekly_release import verify
    result = verify(Path(site), Path(policy_path), Path(approval_path), Path(bundle), Path(state))
    result.update(purpose=AUTOMATED, initial_live_verification_sha256=sha(live_path))
    return result


def preserve_envelope(site, template, policy):
    """Copy only already-reviewed archival assets, never forecasts, into future exports."""
    site, template = Path(site), Path(template)
    for n, expected in policy["static_assets"].items():
        dest = (site / n).resolve()
        src = (template / n).resolve()
        if not dest.is_relative_to(site.resolve()) or not src.is_relative_to(template.resolve()):
            raise ValueError("Envelope path escape")
        if dest.exists():
            if sha(dest) != expected:
                raise ValueError("Existing static asset differs: " + n)
        else:
            if not n.startswith("previous-public/") and n != ".nojekyll":
                raise ValueError("Missing non-archive static asset: " + n)
            if not src.is_file() or sha(src) != expected:
                raise ValueError("Archive identity differs")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)


def verify_live(site, manifest_path, request_path, policy_path, fetch=None):
    manifest, request, policy = map(load, [manifest_path, request_path, policy_path])
    verify_snapshot(site, manifest, policy)
    if request["deployment_manifest_sha256"] != sha(manifest_path) or request["policy_sha256"] != digest(policy):
        raise ValueError("Live verification scope differs")
    base = policy["site"].rstrip("/") + "/"
    def get(n):
        url = base + n + "?review=" + sha(manifest_path)[:16]
        if fetch:
            content = fetch(url)
        else:
            with urllib.request.urlopen(url, timeout=20) as response:
                content = response.read()
        return hashlib.sha256(content).hexdigest()
    def check(item):
        n, expected = item
        try:
            actual = get(n)
            return dict(path=n, expected=expected, actual=actual, match=actual == expected)
        except Exception as exc:
            return dict(path=n, expected=expected, match=False, error=type(exc).__name__)
    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = list(pool.map(check, manifest["files"].items()))
    return dict(status="PASS_LIVE_INITIAL_PUBLICATION" if all(c["match"] for c in checks) else "FAIL_LIVE_PROPAGATION",
                actual_completed_at_utc=clock().isoformat(), policy_sha256=digest(policy),
                initial_request_sha256=sha(request_path), deployment_manifest_sha256=sha(manifest_path),
                deployment_control_sha256=request["deployment_control_sha256"],
                release_id=manifest["candidate_release_id"], checks=checks,
                github_run_id=os.environ.get("GITHUB_RUN_ID"), no_unattended_approval_granted=True)


def sign(purpose, policy_path, out, reference, expires, key, request_path=None, live_path=None, control_path=None):
    """Operator-only. Never invoked by either hosted workflow."""
    if len(key) < 32 or not reference:
        raise ValueError("Operator-held key and explicit human approval reference required")
    now = clock()
    if not now < utc(expires) <= utc("2027-02-01T00:00:00Z"):
        raise ValueError("Bounded approval expiry required")
    policy = load(policy_path)
    control_sha = verify_controls(control_path)
    grant = dict(purpose=purpose, policy_sha256=digest(policy), approved_at_utc=now.isoformat(),
                 expires_at_utc=expires, explicit_human_approval_reference=reference, deployment_control_sha256=control_sha)
    if purpose == INITIAL:
        request = load(request_path)
        if utc(expires) > utc(request["review_expires_at_utc"]) or request["policy_sha256"] != digest(policy) or request["deployment_control_sha256"] != control_sha:
            raise ValueError("Initial review scope/expiry differs")
        grant["request_sha256"] = sha(request_path)
    elif purpose == AUTOMATED:
        live = load(live_path)
        if live.get("status") != "PASS_LIVE_INITIAL_PUBLICATION" or live.get("policy_sha256") != digest(policy) or live.get("deployment_control_sha256") != control_sha or utc(live["actual_completed_at_utc"]) >= now:
            raise ValueError("Actual successful initial publication must precede separate approval")
        grant["initial_live_verification_sha256"] = sha(live_path)
    else:
        raise ValueError("Unknown approval purpose")
    approval = dict(decision="APPROVED", policy_sha256=digest(policy), grant=grant,
                    signature=hmac.new(key.encode(), canonical(grant), hashlib.sha256).hexdigest())
    immutable(out, approval)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["initial-verify", "unattended-verify", "preserve", "live-verify", "sign"])
    for name in ["site", "manifest", "request", "policy", "approval", "live", "bundle", "state", "template", "out"]:
        p.add_argument("--" + name, type=Path)
    p.add_argument("--purpose", choices=[INITIAL, AUTOMATED])
    p.add_argument("--explicit-approval-reference")
    p.add_argument("--expires-at")
    p.add_argument("--control", type=Path, default=Path("config/research_deployment_controls.json"))
    a = p.parse_args()
    if a.command == "sign":
        sign(a.purpose, a.policy, a.out, a.explicit_approval_reference, a.expires_at,
             os.environ.get("RESEARCH_POLICY_HMAC_KEY", ""), a.request, a.live, a.control)
        print("Signed only the separately approved stage; no deployment/enablement occurred.")
        return
    if a.command == "preserve":
        preserve_envelope(a.site, a.template, load(a.policy))
        return
    if a.command == "initial-verify":
        r = verify_initial(a.site, a.manifest, a.request, a.policy, a.approval, os.environ.get("RESEARCH_POLICY_HMAC_KEY", ""), control_path=a.control)
    elif a.command == "unattended-verify":
        r = verify_unattended(a.site, a.policy, a.approval, a.live, a.bundle, a.state, os.environ.get("RESEARCH_POLICY_HMAC_KEY", ""), control_path=a.control)
    else:
        r = verify_live(a.site, a.manifest, a.request, a.policy)
    immutable(a.out, r)
    print(json.dumps({k: v for k, v in r.items() if k != "checks"}))
    if r["status"].startswith("FAIL"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
