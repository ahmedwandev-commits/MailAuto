"""Reads Mapping.xlsx (job code -> Maker / Checker / Manager)."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from .textnorm import norm

VALID_CATEGORIES = {"maker": "Maker", "checker": "Checker", "manager": "Manager"}
GRANT_ALIASES = {
    "grant serving": "serving",
    "grant monitoring": "monitoring",
    "grant supervising": "supervising",
    "grant alerts receiving": "alerts_receiving",
}


class MappingError(Exception):
    pass


@dataclass
class JobMapping:
    job_code: str
    category: str
    role_code: str | None
    sheet_grants: set[str] = field(default_factory=set)
    rows: list[int] = field(default_factory=list)


@dataclass
class MappingTable:
    jobs: dict[str, JobMapping]
    warnings: list[str]
    source: str = ""

    def category_for(self, job_code: str | None) -> str | None:
        if not job_code:
            return None
        jm = self.jobs.get(str(job_code))
        return jm.category if jm else None

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for jm in self.jobs.values():
            out[jm.category] += 1
        return dict(out)


def _code(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    m = re.search(r"\d{5,}", norm(value))
    return m.group(0) if m else None


def load_mapping(path: str | Path, category_rules: dict | None = None) -> MappingTable:
    path = Path(path)
    if not path.exists():
        raise MappingError(f"Mapping file not found: {path}")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise MappingError("Mapping file is empty")

    header = [norm(h) for h in rows[0]]

    def col(*names):
        for n in names:
            if norm(n) in header:
                return header.index(norm(n))
        return None

    c_role, c_grant = col("role code"), col("branch role")
    c_job, c_cat = col("job", "job code"), col("word file", "category")
    if c_job is None or c_cat is None:
        raise MappingError(f"Mapping file needs columns 'job' and 'Word File'. Found: {rows[0]}")

    jobs: dict[str, JobMapping] = {}
    warnings: list[str] = []
    for i, row in enumerate(rows[1:], start=2):
        code = _code(row[c_job] if c_job < len(row) else None)
        if not code:
            continue
        cat_raw = norm(row[c_cat] if c_cat < len(row) else "")
        cat = VALID_CATEGORIES.get(cat_raw)
        if not cat:
            raise MappingError(f"Row {i}: unknown 'Word File' value '{row[c_cat]}' (expected Maker/Checker/Manager)")
        role = row[c_role] if c_role is not None and c_role < len(row) else None
        role = None if role is None or norm(role) in ("", "null", "none") else str(role).strip()
        grant = GRANT_ALIASES.get(norm(row[c_grant])) if c_grant is not None and c_grant < len(row) else None

        jm = jobs.get(code)
        if jm is None:
            jm = jobs[code] = JobMapping(code, cat, role)
        elif jm.category != cat:
            raise MappingError(
                f"Job code {code} is '{jm.category}' on row {jm.rows[0]} but '{cat}' on row {i}. Fix the mapping file.")
        if grant:
            jm.sheet_grants.add(grant)
        jm.rows.append(i)

    # Cross-check sheet grants against the category rules (informational only)
    if category_rules:
        for jm in jobs.values():
            expected = set(category_rules.get(jm.category, {}).get("grants", []))
            if jm.sheet_grants and expected and jm.sheet_grants != expected:
                missing = sorted(expected - jm.sheet_grants)
                extra = sorted(jm.sheet_grants - expected)
                warnings.append(
                    f"Job {jm.job_code} ({jm.category}, rows {jm.rows}): sheet grants differ from rules"
                    + (f" - missing {missing}" if missing else "") + (f" - extra {extra}" if extra else "")
                    + ". Rules from config/rules.yaml are used.")
    return MappingTable(jobs=jobs, warnings=warnings, source=str(path))
