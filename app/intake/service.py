import hashlib
from collections import OrderedDict
from pathlib import Path

from app.catalog import SPEC
from app.errors import DomainIssue
from app.intake.normalize import canonical
from app.intake.reader import read_records


def _path(profile: str, path: str | None):
    if path:
        return Path(path)
    from app.config import SAMPLES
    candidate = SAMPLES / f"{profile}.csv"
    if profile in {"clean", "dirty"} or candidate.is_file():
        return candidate
    raise DomainIssue("SAMPLE_UNKNOWN")


def load(profile: str, path: str | None = None):
    sample = _path(profile, path)
    if not sample.is_file():
        raise DomainIssue("SAMPLE_UNKNOWN")
    raw = sample.read_bytes()
    return load_bytes(raw, profile=profile, source_path=sample)


def load_csv_text(text: str, profile: str = "adhoc"):
    if text is None:
        raise DomainIssue("EMPTY_FILE")
    return load_bytes(str(text).encode("utf-8"), profile=profile, source_path=Path("text.csv"))


def load_bytes(raw: bytes, profile: str = "adhoc", source_path=None):
    from app.intake.limits import MAX_FILE, MAX_ROWS
    if len(raw) > MAX_FILE:
        raise DomainIssue("FILE_TOO_LARGE", 413)
    parsed = read_records(raw, SPEC["columns"], MAX_ROWS)
    issues = []
    counts = {"input": len(parsed["records"]), "accepted": 0, "isolated": 0, "duplicates": 0, "ignored": 0}
    valid = []
    for record in parsed["records"]:
        if record["blank"]:
            counts["ignored"] += 1
            issues.append({"line": record["line"], "key": "", "code": "BLANK_ROW"})
        elif record["repeated"]:
            counts["ignored"] += 1
            issues.append({"line": record["line"], "key": "", "code": "REPEATED_HEADER"})
        elif len(record["cells"]) != len(SPEC["columns"]):
            counts["isolated"] += 1
            issues.append({"line": record["line"], "key": record["cells"][0] if record["cells"] else "", "code": "COLUMN_COUNT"})
        else:
            row = dict(zip(SPEC["columns"], record["cells"]))
            row["_line"] = record["line"]
            if not row.get(SPEC["key"]):
                counts["isolated"] += 1
                issues.append({"line": record["line"], "key": "", "code": "KEY_MISSING"})
            else:
                valid.append(row)
    grouped = OrderedDict()
    for row in valid:
        grouped.setdefault(row[SPEC["key"]], []).append(row)
    accepted = []
    for key, rows in grouped.items():
        signatures = {tuple(row[column] for column in SPEC["columns"]) for row in rows}
        if len(signatures) == 1:
            accepted.append(rows[0])
            counts["accepted"] += 1
            counts["duplicates"] += len(rows) - 1
            for row in rows[1:]:
                issues.append({"line": row["_line"], "key": key, "code": "EXACT_DUPLICATE"})
        else:
            counts["isolated"] += len(rows)
            for row in rows:
                issues.append({"line": row["_line"], "key": key, "code": "CONFLICT_DUPLICATE"})
    accepted.sort(key=lambda row: row["_line"])
    digest = hashlib.sha256(raw).hexdigest()
    return {"profile": profile, "path": source_path or Path("<bytes>"), "sha256": digest,
            "rows": accepted, "issues": issues, "counts": counts}
