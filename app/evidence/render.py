from app.errors import DomainIssue
from app.evidence.manifest import stable_digest


def render(run):
    if run is None:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    if run["status"] != "APPROVED":
        raise DomainIssue("NOT_APPROVED", 409)
    artifact = {
        "schema": "workbench-evidence/v1",
        "run_id": run["run_id"],
        "project": run["project"],
        "rule": run["rule"],
        "profile": run["profile"],
        "source": run["source"],
        "counts": run["counts"],
        "ledger": run["ledger"],
        "summary": run["summary"],
        "review": {"status": run["status"], "revision": run["revision"], "comment": run["comment"]},
    }
    ledger = run.get("ledger") or {}
    # 让证据顶层也能直接读到“用了哪次承诺”和每票赔付
    for key in ("promises", "commitments", "shipments", "payouts", "settlements", "holds"):
        if key in ledger:
            artifact[key] = ledger[key]
    artifact["artifact_sha256"] = stable_digest(artifact)
    return artifact
