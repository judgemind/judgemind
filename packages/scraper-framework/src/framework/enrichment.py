"""Enrichment layer for resolving raw LLM extraction output against external data.

This module turns extracted text into resolved database entities by:
  1. Fuzzy-matching case numbers (Levenshtein distance + party overlap)
  2. Resolving judges against aliases and court directory snapshots
  3. Resolving parties against existing aliases
  4. (Future) Looking up court portals for canonical case data

The enrichment pipeline runs after LLM extraction and before DB writes,
producing resolved entity IDs or flagging low-confidence matches for review.

See: https://github.com/judgemind/judgemind/issues/1472
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Case number normalization
# ---------------------------------------------------------------------------

# Common California case number prefixes and their standardized forms.
_PREFIX_NORMALIZATION: dict[str, str] = {
    "civ": "CIV",
    "cv": "CIV",
    "civil": "CIV",
    "cr": "CR",
    "crim": "CR",
    "criminal": "CR",
    "fl": "FL",
    "fam": "FL",
    "family": "FL",
    "pr": "PR",
    "prob": "PR",
    "probate": "PR",
    "sc": "SC",
    "sm": "SC",
    "small": "SC",
    "jv": "JV",
    "juv": "JV",
    "juvenile": "JV",
}

# Pattern to split prefix from the numeric portion of a case number.
_CASE_PREFIX_RE = re.compile(
    r"^([A-Za-z]+)[\s\-]*(.+)$",
)


def normalize_case_number(raw: str) -> str:
    """Normalize a raw case number for comparison.

    Steps:
      1. Strip leading/trailing whitespace.
      2. Collapse internal whitespace to empty string.
      3. Remove hyphens and other separators.
      4. Standardize known prefixes (e.g., "cv" -> "CIV").
      5. Uppercase the result.

    Returns the normalized case number string.
    """
    cleaned = raw.strip()
    # Remove all whitespace and hyphens for comparison
    cleaned = re.sub(r"[\s\-]+", "", cleaned)

    # Try to standardize known prefixes
    m = _CASE_PREFIX_RE.match(cleaned)
    if m:
        prefix_raw = m.group(1).lower()
        numeric = m.group(2)
        if prefix_raw in _PREFIX_NORMALIZATION:
            return f"{_PREFIX_NORMALIZATION[prefix_raw]}{numeric}".upper()

    return cleaned.upper()


def _normalize_for_db_lookup(raw: str) -> str:
    """Normalize a case number for DB lookup against ``case_number_normalized``.

    This must match the normalization in ``ingestion.db.upsert_case`` which
    stores ``case_number.strip().lower().replace(" ", "").replace("-", "")``.
    """
    return raw.strip().lower().replace(" ", "").replace("-", "")


def levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings.

    Uses rapidfuzz for performance when available, falls back to a
    simple dynamic-programming implementation otherwise.
    """
    try:
        from rapidfuzz.distance import Levenshtein

        return Levenshtein.distance(s1, s2)
    except ImportError:
        # Stdlib fallback — O(n*m) DP
        if len(s1) < len(s2):
            return levenshtein_distance(s2, s1)
        if len(s2) == 0:
            return len(s1)
        prev_row = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            curr_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = prev_row[j + 1] + 1
                deletions = curr_row[j] + 1
                substitutions = prev_row[j] + (c1 != c2)
                curr_row.append(min(insertions, deletions, substitutions))
            prev_row = curr_row
        return prev_row[-1]


def compute_party_overlap(parties_a: list[str], parties_b: list[str]) -> float:
    """Compute the Jaccard similarity of two party name lists.

    Both lists are normalized to lowercase before comparison. Returns a
    float in [0.0, 1.0] where 1.0 means identical party sets.

    Returns 0.0 if both lists are empty.
    """
    if not parties_a and not parties_b:
        return 0.0

    set_a = {name.strip().lower() for name in parties_a if name.strip()}
    set_b = {name.strip().lower() for name in parties_b if name.strip()}

    if not set_a and not set_b:
        return 0.0

    intersection = set_a & set_b
    union = set_a | set_b

    if not union:
        return 0.0

    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# Data classes for enrichment results
# ---------------------------------------------------------------------------


@dataclass
class CaseMatch:
    """Result of a case number fuzzy match attempt."""

    case_id: str
    case_number: str
    match_type: str  # "exact", "fuzzy", or "new"
    confidence: float  # 0.0 to 1.0
    corrected_from: str | None = None  # original case number if corrected
    levenshtein_distance: int = 0
    party_overlap: float = 0.0


@dataclass
class JudgeResolution:
    """Result of resolving a judge name."""

    judge_id: str | None
    canonical_name: str | None
    match_type: str  # "alias", "directory", "new", "flagged", "invalid"
    confidence: float
    directory_mismatch: bool = False  # True if judge != directory for dept/date
    directory_judge: str | None = None  # expected judge from directory


@dataclass
class PartyResolution:
    """Result of resolving a party name."""

    party_id: str
    canonical_name: str
    match_type: str  # "alias", "new"
    confidence: float


@dataclass
class EnrichmentResult:
    """Aggregate result of enriching a single extracted ruling."""

    case_match: CaseMatch | None = None
    judge_resolution: JudgeResolution | None = None
    party_resolutions: list[PartyResolution] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # human-review flags


# ---------------------------------------------------------------------------
# Court portal lookup interface (stub)
# ---------------------------------------------------------------------------


class CourtPortalLookup(abc.ABC):
    """Abstract interface for querying court portals to get canonical case data.

    When implemented, this would validate case numbers, retrieve full party
    lists, and get filing dates from the court's own records.

    This is a stub interface for future implementation. Concrete subclasses
    will be court-specific (e.g., ``LASCPortalLookup`` for Los Angeles).

    See: https://github.com/judgemind/judgemind/issues/1472 (step 4)
    """

    @abc.abstractmethod
    def lookup_case(
        self,
        case_number: str,
        court_id: str,
    ) -> dict[str, object] | None:
        """Look up a case by number in the court portal.

        Parameters
        ----------
        case_number : str
            The case number to look up.
        court_id : str
            The court identifier (UUID string).

        Returns
        -------
        dict[str, object] | None
            A dictionary with canonical case data if found, or None if
            the case was not found or the portal is unavailable. Expected
            keys when implemented:
            - ``case_number``: canonical case number
            - ``case_title``: official case title
            - ``parties``: list of party dicts with ``name`` and ``role``
            - ``filing_date``: date the case was filed
            - ``case_type``: type of case (civil, criminal, etc.)
        """

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if the court portal is currently accessible.

        Returns
        -------
        bool
            True if the portal can be queried, False otherwise.
        """


# ---------------------------------------------------------------------------
# Enrichment engine
# ---------------------------------------------------------------------------


class EnrichmentEngine:
    """Resolves raw LLM extraction output against external data sources.

    Performs case number fuzzy matching, judge resolution, and party
    resolution using existing database records. Optionally cross-references
    against court directory snapshots for judge validation.

    Parameters
    ----------
    conn : psycopg.Connection
        A psycopg3 connection for database lookups.
    court_portal : CourtPortalLookup | None
        Optional court portal for canonical case data lookup.
        Not yet implemented — pass None for now.
    max_levenshtein : int
        Maximum Levenshtein distance for fuzzy case number matching.
        Default is 2.
    min_party_overlap : float
        Minimum party overlap (Jaccard similarity) required for a fuzzy
        case number match to be accepted. Default is 0.5 (50%).

        The floor is set at majority overlap to defeat the generic-party
        collision class: when two cases share only a single generic party
        (e.g. "State of Texas", "United States") the Jaccard score is
        typically 1/3 ≈ 0.33, well below the 0.5 threshold
        (Federal/CourtListener regression — see #4024).

        When party data is missing on either side (empty ``extracted_parties``
        or empty candidate parties) and the caller has explicitly provided
        a parties list (even an empty one), the fuzzy match is rejected —
        there is no way to validate the match against party identity, and
        silent acceptance collapses distinct rulings onto the same case
        (see #2467).  Callers that have no party information at all should
        pass ``extracted_parties=None`` to preserve the legacy
        distance-only acceptance behavior.
    max_fuzzy_candidates : int
        Maximum number of candidate rows returned by the fuzzy case
        query.  Acts as a safety cap to prevent fetching thousands of
        rows for courts with large case inventories.  Default is 500.
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        court_portal: CourtPortalLookup | None = None,
        max_levenshtein: int = 2,
        min_party_overlap: float = 0.5,
        max_fuzzy_candidates: int = 500,
    ) -> None:
        self._conn = conn
        self._court_portal = court_portal
        self._max_levenshtein = max_levenshtein
        self._min_party_overlap = min_party_overlap
        self._max_fuzzy_candidates = max_fuzzy_candidates

    # ------------------------------------------------------------------
    # Case number matching
    # ------------------------------------------------------------------

    def match_case_number(
        self,
        raw_case_number: str,
        court_id: str,
        extracted_parties: list[str] | None = None,
    ) -> CaseMatch:
        """Match a raw case number against existing cases in the same court.

        Strategy:
          1. Normalize the case number.
          2. Exact match on (court_id, case_number).
          3. If no exact match, fuzzy match against all cases in the court
             with Levenshtein distance <= max_levenshtein.
          4. Among fuzzy candidates, require party-identity validation:
             if the caller provided a parties list (``extracted_parties``
             is not None), BOTH sides must have non-empty parties AND
             overlap must be >= ``min_party_overlap``.  When party data
             is missing on either side the candidate is rejected — we
             cannot safely rewrite a case_number based on Levenshtein
             distance alone when there is no party-identity evidence to
             confirm the match (see #2467).
          5. If a good fuzzy match exists, use the existing case.
          6. Otherwise, signal that a new case should be created.

        Parameters
        ----------
        raw_case_number : str
            The case number as extracted by the LLM.
        court_id : str
            The court UUID.
        extracted_parties : list[str] | None
            Party names extracted from the same ruling, used for overlap
            checking during fuzzy matching.

            - ``None`` (legacy contract) — caller has no party data at all;
              fuzzy match is accepted on Levenshtein distance alone.
            - ``[]`` (empty list) — the LLM / scraper ran and produced
              no parties.  Fuzzy match is REJECTED because the absence
              of parties cannot distinguish between near-duplicate case
              numbers that belong to different cases (#2467).
            - non-empty list — used for Jaccard-overlap validation
              against candidate parties.

        Returns
        -------
        CaseMatch
            The match result with case_id (for existing matches), match type,
            and confidence score. For "new" matches, case_id is empty string.
        """
        normalized = normalize_case_number(raw_case_number)
        # DB lookup uses the raw case number's normalization (matching upsert_case),
        # not the prefix-standardized form used for Levenshtein comparison.
        db_normalized = _normalize_for_db_lookup(raw_case_number)

        # Step 1: Exact match
        exact = self._exact_case_match(db_normalized, court_id)
        if exact is not None:
            logger.debug(
                "Case number exact match",
                raw=raw_case_number,
                normalized=normalized,
                case_id=exact,
            )
            return CaseMatch(
                case_id=exact,
                case_number=normalized,
                match_type="exact",
                confidence=1.0,
            )

        # Step 2: Fuzzy match
        #
        # Party-identity validation (#2467, see also #4024): track whether the
        # caller has party information available for this ruling.
        # ``extracted_parties is None`` means "no party data known" (legacy
        # caller contract) — accept on distance alone.  An empty or non-empty
        # list means the caller DOES have a notion of the ruling's parties —
        # require non-empty parties on both sides AND Jaccard overlap >=
        # threshold to accept the fuzzy match.  Silent acceptance without party
        # validation collapses distinct rulings onto the same case_number
        # when near-duplicate numbers exist in the court.
        _parties_known = extracted_parties is not None
        if extracted_parties is None:
            extracted_parties = []

        candidates = self._fuzzy_case_candidates(db_normalized, court_id)

        best_match: CaseMatch | None = None
        for cand_id, cand_number, cand_parties in candidates:
            dist = levenshtein_distance(normalized, normalize_case_number(cand_number))
            if dist > self._max_levenshtein:
                continue

            if _parties_known:
                # Reject when either side lacks parties — cannot validate
                # identity without cross-check evidence (#2467).
                if not extracted_parties or not cand_parties:
                    continue
                overlap = compute_party_overlap(extracted_parties, cand_parties)
                if overlap < self._min_party_overlap:
                    continue
            else:
                # Legacy contract: no party data at all — accept on
                # distance alone.
                overlap = 0.0

            confidence = max(0.0, 1.0 - (dist / 5.0))
            if overlap > 0:
                confidence = min(1.0, confidence + overlap * 0.3)

            if best_match is None or confidence > best_match.confidence:
                best_match = CaseMatch(
                    case_id=cand_id,
                    case_number=cand_number,
                    match_type="fuzzy",
                    confidence=confidence,
                    corrected_from=raw_case_number,
                    levenshtein_distance=dist,
                    party_overlap=overlap,
                )

        if best_match is not None:
            logger.info(
                "Case number fuzzy match",
                raw=raw_case_number,
                normalized=normalized,
                matched_case=best_match.case_number,
                distance=best_match.levenshtein_distance,
                party_overlap=best_match.party_overlap,
                confidence=best_match.confidence,
            )
            return best_match

        # Step 3: No match — signal new case creation
        logger.debug(
            "No case number match found — new case",
            raw=raw_case_number,
            normalized=normalized,
            court_id=court_id,
        )
        return CaseMatch(
            case_id="",
            case_number=normalized,
            match_type="new",
            confidence=0.0,
        )

    def _exact_case_match(self, db_normalized: str, court_id: str) -> str | None:
        """Look up an exact case match using the ``case_number_normalized`` column.

        Parameters
        ----------
        db_normalized : str
            The case number normalized for DB lookup (via ``_normalize_for_db_lookup``).
            Must already be lowercase with spaces/hyphens removed.
        court_id : str
            The court UUID.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM cases
                WHERE court_id = %s::uuid
                  AND case_number_normalized = %s
                LIMIT 1
                """,
                (court_id, db_normalized),
            )
            row = cur.fetchone()
        return str(row[0]) if row else None

    # Minimum case-number length required to apply the prefix filter.
    # Shorter case numbers risk false negatives from an overly-narrow prefix.
    _PREFIX_FILTER_MIN_LEN = 4

    # Number of leading characters used for the prefix filter.
    _PREFIX_LENGTH = 2

    def _fuzzy_case_candidates(
        self,
        db_normalized: str,
        court_id: str,
    ) -> list[tuple[str, str, list[str]]]:
        """Fetch candidate cases from the same court for fuzzy matching.

        Returns a list of ``(case_id, case_number, party_names)`` tuples.
        Uses the ``case_number_normalized`` column for efficient length-based
        and prefix-based filtering, avoiding per-row function calls.

        The query applies two selectivity improvements beyond the original
        length-only filter:

        1. **Prefix filter** — when the normalized case number is at least
           ``_PREFIX_FILTER_MIN_LEN`` characters long, the query requires
           candidates to share the same leading ``_PREFIX_LENGTH`` characters.
           This exploits the fact that case numbers typically start with a
           year or type prefix (e.g. ``"24nncv..."``), dramatically reducing
           the candidate set for large courts.
        2. **Row cap** — the query is always limited to ``max_fuzzy_candidates``
           rows (default 500) as a safety net.  A warning is logged when
           the cap is reached, since the true best match may have been
           excluded.

        Parameters
        ----------
        db_normalized : str
            The case number normalized for DB lookup (via ``_normalize_for_db_lookup``).
        court_id : str
            The court UUID.
        """
        min_len = max(1, len(db_normalized) - self._max_levenshtein)
        max_len = len(db_normalized) + self._max_levenshtein

        use_prefix = len(db_normalized) >= self._PREFIX_FILTER_MIN_LEN
        prefix = db_normalized[: self._PREFIX_LENGTH] if use_prefix else ""

        if use_prefix:
            query = """
                SELECT c.id, c.case_number,
                       COALESCE(
                           ARRAY_AGG(p.canonical_name) FILTER (WHERE p.canonical_name IS NOT NULL),
                           ARRAY[]::text[]
                       ) AS party_names
                FROM cases c
                LEFT JOIN case_parties cp ON cp.case_id = c.id
                LEFT JOIN parties p ON p.id = cp.party_id
                WHERE c.court_id = %s::uuid
                  AND LENGTH(c.case_number_normalized) BETWEEN %s AND %s
                  AND LEFT(c.case_number_normalized, %s) = %s
                GROUP BY c.id, c.case_number
                ORDER BY c.id
                LIMIT %s
            """
            params: tuple[object, ...] = (
                court_id,
                min_len,
                max_len,
                self._PREFIX_LENGTH,
                prefix,
                self._max_fuzzy_candidates,
            )
        else:
            query = """
                SELECT c.id, c.case_number,
                       COALESCE(
                           ARRAY_AGG(p.canonical_name) FILTER (WHERE p.canonical_name IS NOT NULL),
                           ARRAY[]::text[]
                       ) AS party_names
                FROM cases c
                LEFT JOIN case_parties cp ON cp.case_id = c.id
                LEFT JOIN parties p ON p.id = cp.party_id
                WHERE c.court_id = %s::uuid
                  AND LENGTH(c.case_number_normalized) BETWEEN %s AND %s
                GROUP BY c.id, c.case_number
                ORDER BY c.id
                LIMIT %s
            """
            params = (court_id, min_len, max_len, self._max_fuzzy_candidates)

        with self._conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        if len(rows) >= self._max_fuzzy_candidates:
            logger.warning(
                "Fuzzy candidate set hit row cap — best match may be excluded",
                court_id=court_id,
                case_number=db_normalized,
                cap=self._max_fuzzy_candidates,
                prefix_used=prefix if use_prefix else None,
            )

        result: list[tuple[str, str, list[str]]] = []
        for row in rows:
            case_id = str(row[0])
            case_number = row[1]
            party_names = list(row[2]) if row[2] else []
            result.append((case_id, case_number, party_names))
        return result

    # ------------------------------------------------------------------
    # Judge resolution
    # ------------------------------------------------------------------

    def resolve_judge(
        self,
        raw_judge_name: str,
        court_id: str,
        department: str | None = None,
        hearing_date: datetime | None = None,
    ) -> JudgeResolution:
        """Resolve a raw judge name against aliases and directory snapshots.

        Strategy:
          1. Look up judge_aliases for an existing match.
          2. If found, cross-reference against court_directory_snapshots
             for the hearing date + department (if provided).
          3. If the alias match disagrees with the directory, flag for review.
          4. If no alias match, try creating/matching via the existing
             resolve_judge flow in db.py.

        Parameters
        ----------
        raw_judge_name : str
            The judge name as extracted by the LLM.
        court_id : str
            The court UUID.
        department : str | None
            The department string, for directory cross-reference.
        hearing_date : datetime | None
            The hearing date, for directory snapshot lookup.

        Returns
        -------
        JudgeResolution
            The resolution result with judge_id, match type, and any flags.
        """
        if not raw_judge_name or not raw_judge_name.strip():
            logger.warning("resolve_judge: empty judge name provided")
            return JudgeResolution(
                judge_id=None,
                canonical_name=None,
                match_type="invalid",
                confidence=0.0,
            )

        # Step 1: Check judge_aliases for existing match
        alias_match = self._lookup_judge_alias(raw_judge_name, court_id)

        if alias_match is not None:
            judge_id, canonical_name = alias_match
            resolution = JudgeResolution(
                judge_id=judge_id,
                canonical_name=canonical_name,
                match_type="alias",
                confidence=1.0,
            )

            # Step 2: Cross-reference with directory snapshot
            if department and hearing_date:
                self._cross_reference_directory(
                    resolution, canonical_name, court_id, department, hearing_date
                )

            return resolution

        # Step 3: No alias match — check directory snapshot directly
        if department and hearing_date:
            directory_judge = self._get_directory_judge(court_id, department, hearing_date)
            if directory_judge is not None:
                logger.info(
                    "Judge not in aliases but directory shows judge for dept",
                    raw_name=raw_judge_name,
                    department=department,
                    directory_judge=directory_judge,
                )

        # No alias match — signal that the caller should use the existing
        # resolve_judge flow in db.py to create/match the judge record.
        logger.debug(
            "No judge alias match — deferring to db.resolve_judge",
            raw_name=raw_judge_name,
            court_id=court_id,
        )
        return JudgeResolution(
            judge_id=None,
            canonical_name=None,
            match_type="new",
            confidence=0.0,
        )

    def _lookup_judge_alias(self, raw_name: str, court_id: str) -> tuple[str, str] | None:
        """Look up an existing judge alias, returning (judge_id, canonical_name)."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT ja.judge_id, j.canonical_name
                FROM judge_aliases ja
                JOIN judges j ON j.id = ja.judge_id
                WHERE LOWER(ja.raw_name) = LOWER(%s) AND j.court_id = %s::uuid
                LIMIT 1
                """,
                (raw_name, court_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return (str(row[0]), row[1])

    def _cross_reference_directory(
        self,
        resolution: JudgeResolution,
        canonical_name: str,
        court_id: str,
        department: str,
        hearing_date: datetime,
    ) -> None:
        """Cross-reference a judge match against the court directory snapshot.

        Mutates the resolution in place, setting directory_mismatch and
        directory_judge if the alias match disagrees with the snapshot.
        """
        directory_judge = self._get_directory_judge(court_id, department, hearing_date)
        if directory_judge is None:
            # No directory data — can't cross-reference
            return

        # Compare names (case-insensitive)
        if canonical_name.lower().strip() != directory_judge.lower().strip():
            resolution.directory_mismatch = True
            resolution.directory_judge = directory_judge
            resolution.confidence = 0.7  # lower confidence due to mismatch
            logger.warning(
                "Judge/directory mismatch — possible substitute or typo",
                canonical_name=canonical_name,
                directory_judge=directory_judge,
                department=department,
                hearing_date=hearing_date.isoformat(),
                court_id=court_id,
            )

    def _get_directory_judge(
        self,
        court_id: str,
        department: str,
        hearing_date: datetime,
    ) -> str | None:
        """Look up the expected judge for a department on a given date."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT mapping FROM court_directory_snapshots
                WHERE court_id = %s AND captured_at <= %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (court_id, hearing_date),
            )
            row = cur.fetchone()

        if row is None or row[0] is None:
            return None

        import json

        mapping_data = row[0]
        mapping: dict[str, str]
        if isinstance(mapping_data, dict):
            mapping = mapping_data
        else:
            try:
                mapping = json.loads(mapping_data)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to parse court directory mapping from DB",
                    court_id=court_id,
                    hearing_date=hearing_date.isoformat(),
                )
                return None

        # Try exact department match first, then case-insensitive
        if department in mapping:
            return mapping[department]
        dept_lower = department.lower().strip()
        for key, value in mapping.items():
            if key.lower().strip() == dept_lower:
                return value
        return None

    # ------------------------------------------------------------------
    # Party resolution
    # ------------------------------------------------------------------

    def resolve_party(
        self,
        raw_party_name: str,
    ) -> PartyResolution:
        """Resolve a raw party name against existing aliases.

        Strategy:
          1. Normalize the party name.
          2. Search party_aliases for a case-insensitive match.
          3. If found, return the existing party.
          4. If not found, signal that a new party should be created.

        Parameters
        ----------
        raw_party_name : str
            The party name as extracted by the LLM.

        Returns
        -------
        PartyResolution
            The resolution result with party_id, canonical name, and match type.
            For "new" matches, party_id is empty string.
        """
        normalized = self._normalize_party_name(raw_party_name)

        # Look up existing alias (case-insensitive)
        alias_match = self._lookup_party_alias(raw_party_name)
        if alias_match is not None:
            party_id, canonical_name = alias_match
            logger.debug(
                "Party alias match",
                raw_name=raw_party_name,
                canonical_name=canonical_name,
                party_id=party_id,
            )
            return PartyResolution(
                party_id=party_id,
                canonical_name=canonical_name,
                match_type="alias",
                confidence=1.0,
            )

        # No match — signal new party creation
        logger.debug(
            "No party alias match — new party",
            raw_name=raw_party_name,
            normalized=normalized,
        )
        return PartyResolution(
            party_id="",
            canonical_name=normalized,
            match_type="new",
            confidence=0.0,
        )

    def _lookup_party_alias(self, raw_name: str) -> tuple[str, str] | None:
        """Look up an existing party alias, returning (party_id, canonical_name)."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT pa.party_id, p.canonical_name
                FROM party_aliases pa
                JOIN parties p ON p.id = pa.party_id
                WHERE LOWER(pa.raw_name) = LOWER(%s)
                LIMIT 1
                """,
                (raw_name,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return (str(row[0]), row[1])

    @staticmethod
    def _normalize_party_name(raw_name: str) -> str:
        """Normalize a raw party name.

        Steps:
          1. Strip leading/trailing whitespace.
          2. Collapse internal whitespace.
          3. Strip common suffixes like "An Individual", "a corporation".
          4. Title-case the result.
        """
        import re as _re

        name = raw_name.strip()
        name = _re.sub(r"\s+", " ", name)
        # Strip common legal descriptors
        name = _re.sub(
            r",?\s*(?:an individual|a corporation|a limited liability company"
            r"|a partnership|a california corporation|individually"
            r"|as an individual|a domestic corporation"
            r"|a foreign corporation|et al\.?)\s*$",
            "",
            name,
            flags=_re.IGNORECASE,
        )
        name = name.strip().rstrip(",").strip()
        return name.title() if name else name

    # ------------------------------------------------------------------
    # Full enrichment pipeline
    # ------------------------------------------------------------------

    def enrich(
        self,
        *,
        case_number: str,
        court_id: str,
        judge_name: str | None = None,
        department: str | None = None,
        hearing_date: datetime | None = None,
        parties: list[str] | None = None,
    ) -> EnrichmentResult:
        """Run the full enrichment pipeline for a single extracted ruling.

        Parameters
        ----------
        case_number : str
            The extracted case number.
        court_id : str
            The court UUID.
        judge_name : str | None
            The extracted judge name, if any.
        department : str | None
            The extracted department, if any.
        hearing_date : datetime | None
            The extracted hearing date, if any.
        parties : list[str] | None
            The extracted party names, if any.

        Returns
        -------
        EnrichmentResult
            The aggregate enrichment result.
        """
        result = EnrichmentResult()
        flags: list[str] = []

        # 1. Case number matching
        result.case_match = self.match_case_number(case_number, court_id, extracted_parties=parties)
        if result.case_match.match_type == "fuzzy":
            flags.append(
                f"case_number_corrected: {result.case_match.corrected_from} "
                f"-> {result.case_match.case_number} "
                f"(dist={result.case_match.levenshtein_distance}, "
                f"overlap={result.case_match.party_overlap:.2f})"
            )

        # 2. Judge resolution
        if judge_name:
            result.judge_resolution = self.resolve_judge(
                judge_name, court_id, department=department, hearing_date=hearing_date
            )
            if result.judge_resolution.directory_mismatch:
                flags.append(
                    f"judge_directory_mismatch: extracted={judge_name}, "
                    f"directory={result.judge_resolution.directory_judge}"
                )

        # 3. Party resolution
        if parties:
            for party_name in parties:
                if not party_name.strip():
                    continue
                party_res = self.resolve_party(party_name)
                result.party_resolutions.append(party_res)

        result.flags = flags
        if flags:
            logger.info(
                "Enrichment flags for review",
                case_number=case_number,
                court_id=court_id,
                flags=flags,
            )

        return result
