import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from companies.domain import ListedBranch, Placement
from companies.schemas import Branch, Contact, Segment, SegmentStatus

RETRYABLE = (SegmentStatus.NEW, SegmentStatus.FAILED)


@dataclass(frozen=True)
class StoredBranch:
    """What joining needs of a branch already in the base."""

    company_id: str
    org_id: str | None
    name: str
    city: str
    contacts: list[tuple[str, str]]


@dataclass(frozen=True)
class StoredCompany:
    company_id: str
    name: str
    city: str
    branches: list[Branch]
    contacts: list[Contact]
    review_count: int


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


class CompanyRepository:
    """SQL to segments, companies, branches and their contacts."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    # --- segments -----------------------------------------------------------

    def claim(self, segment: Segment, now: datetime) -> bool:
        """Mark a new or failed segment as being collected; False when it is not."""
        self._db.execute(
            "INSERT INTO segments (city, rubric, status, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (city, rubric) DO NOTHING",
            (*segment, SegmentStatus.NEW, _utc(now)))
        cursor = self._db.execute(
            "UPDATE segments SET status = ?, error = NULL, updated_at = ?"
            " WHERE city = ? AND rubric = ? AND status IN (?, ?)",
            (SegmentStatus.COLLECTING, _utc(now), *segment, *RETRYABLE))
        return cursor.rowcount == 1

    def finish(self, segment: Segment, branches: int, capped: bool, now: datetime) -> None:
        self._db.execute(
            "UPDATE segments SET status = ?, branches = ?, capped = ?, error = NULL,"
            " updated_at = ? WHERE city = ? AND rubric = ?",
            (SegmentStatus.COLLECTED, branches, capped, _utc(now), *segment))

    def fail(self, segment: Segment, reason: str, now: datetime) -> None:
        self._db.execute(
            "UPDATE segments SET status = ?, error = ?, updated_at = ?"
            " WHERE city = ? AND rubric = ?",
            (SegmentStatus.FAILED, reason, _utc(now), *segment))

    def forget_collecting(self) -> None:
        self._db.execute("UPDATE segments SET status = ? WHERE status = ?",
                         (SegmentStatus.NEW, SegmentStatus.COLLECTING))

    def segment_rows(self) -> dict[Segment, sqlite3.Row]:
        rows = self._db.execute(
            "SELECT city, rubric, status, branches, capped, error, updated_at FROM segments")
        return {Segment(row["city"], row["rubric"]): row for row in rows}

    def company_counts(self) -> dict[Segment, int]:
        rows = self._db.execute(
            "SELECT s.city, s.rubric, count(DISTINCT b.company_id) FROM branch_segments s"
            " JOIN branches b USING (branch_id) GROUP BY s.city, s.rubric")
        return {Segment(city, rubric): count for city, rubric, count in rows}

    # --- branches and companies ---------------------------------------------

    def branches(self) -> dict[str, StoredBranch]:
        contacts: dict[str, list[tuple[str, str]]] = {}
        for branch_id, kind, handle in self._db.execute(
                "SELECT branch_id, kind, handle FROM branch_contacts"
                " ORDER BY branch_id, kind, handle"):
            contacts.setdefault(branch_id, []).append((kind, handle))
        rows = self._db.execute(
            "SELECT branch_id, company_id, org_id, coalesce(org_name, name), city FROM branches")
        return {branch_id: StoredBranch(company_id, org_id, name, city, contacts.get(branch_id, []))
                for branch_id, company_id, org_id, name, city in rows}

    def add_company(self, company_id: str, name: str, city: str, now: datetime) -> None:
        self._db.execute(
            "INSERT INTO companies (company_id, name, city, created_at) VALUES (?, ?, ?, ?)",
            (company_id, name, city, _utc(now)))

    def add_branch(self, listed: ListedBranch, city: str, source_url: str,
                   placement: Placement) -> None:
        self._db.execute(
            "INSERT INTO branches (branch_id, company_id, org_id, name, org_name, city, address,"
            " review_count, source_url, rule, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (listed.branch_id, placement.company_id, listed.org_id, listed.name, listed.org_name,
             city, listed.address, listed.review_count, source_url, placement.rule,
             placement.confidence))

    def refresh_branch(self, listed: ListedBranch) -> None:
        """Update what 2GIS shows of a known branch; its company never changes here."""
        self._db.execute(
            "UPDATE branches SET org_id = ?, name = ?, org_name = ?, address = ?,"
            " review_count = ? WHERE branch_id = ?",
            (listed.org_id, listed.name, listed.org_name, listed.address,
             listed.review_count, listed.branch_id))

    def replace_contacts(self, branch_id: str, handles: list[tuple[str, str]]) -> None:
        self._db.execute("DELETE FROM branch_contacts WHERE branch_id = ?", (branch_id,))
        self._db.executemany(
            "INSERT INTO branch_contacts (branch_id, kind, handle) VALUES (?, ?, ?)",
            [(branch_id, kind, handle) for kind, handle in handles])

    def link_segment(self, branch_id: str, segment: Segment) -> None:
        self._db.execute(
            "INSERT INTO branch_segments (branch_id, city, rubric) VALUES (?, ?, ?)"
            " ON CONFLICT DO NOTHING", (branch_id, *segment))

    def companies(self) -> list[StoredCompany]:
        """Return every company with its branches and contacts, each sorted."""
        branches: dict[str, list[Branch]] = {}
        reviews: dict[str, int] = {}
        for row in self._db.execute(
                "SELECT company_id, branch_id, name, address, source_url, rule, confidence,"
                " review_count FROM branches ORDER BY branch_id"):
            branches.setdefault(row["company_id"], []).append(Branch.model_validate(dict(row)))
            # One count per company: 2GIS may show the org's reviews on every branch.
            reviews[row["company_id"]] = max(reviews.get(row["company_id"], 0),
                                             row["review_count"])
        contacts: dict[str, list[Contact]] = {}
        for company_id, kind, handle in self._db.execute(
                "SELECT DISTINCT b.company_id, c.kind, c.handle FROM branch_contacts c"
                " JOIN branches b USING (branch_id) ORDER BY b.company_id, c.kind, c.handle"):
            contacts.setdefault(company_id, []).append(Contact(kind=kind, handle=handle))
        return [StoredCompany(company_id, name, city, branches.get(company_id, []),
                              contacts.get(company_id, []), reviews.get(company_id, 0))
                for company_id, name, city in self._db.execute(
                    "SELECT company_id, name, city FROM companies ORDER BY company_id")]
