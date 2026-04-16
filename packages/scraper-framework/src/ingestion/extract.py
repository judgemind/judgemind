"""Extraction utilities for the ingestion pipeline.

Provides normalization functions for outcome and motion_type values,
regex-based extraction for case numbers, hearing dates, judge names,
and case type inference, plus deterministic case title cleanup.

LLM enrichment (#1467) replaced regex-based extraction of motion_type,
outcome, case_title, and parties from ruling text.  The regex extraction
functions for those fields were removed in #2178.
"""

from __future__ import annotations

import re
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Motion type normalization patterns
# ---------------------------------------------------------------------------

# Ordered so more specific patterns match first (e.g. "summary adjudication"
# before "summary judgment").
_MOTION_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(?:motion\s+for\s+)?summary\s+adjudication\b",
            re.IGNORECASE,
        ),
        "msj_partial",
    ),
    (
        re.compile(
            r"\bpartial\s+summary\s+judgment\b",
            re.IGNORECASE,
        ),
        "msj_partial",
    ),
    (
        re.compile(
            r"\b(?:motion\s+for\s+)?summary\s+judgment\b",
            re.IGNORECASE,
        ),
        "msj",
    ),
    (re.compile(r"\bmotion\s+to\s+dismiss\b", re.IGNORECASE), "mtd"),
    (re.compile(r"\bmotion\s+in\s+limine\b", re.IGNORECASE), "mil"),
    (re.compile(r"\bdemurrer\b", re.IGNORECASE), "demurrer"),
    # "compel arbitration" must come before broad "motion to compel" (#2061)
    (
        re.compile(
            r"\b(?:motions?\s+to\s+)?compel\s+arbitration\b",
            re.IGNORECASE,
        ),
        "motion_to_compel_arbitration",
    ),
    (re.compile(r"\bmotions?\s+to\s+compel\b", re.IGNORECASE), "motion_to_compel"),
    (
        re.compile(r"\banti[- ]?slapp\b", re.IGNORECASE),
        "anti_slapp",
    ),
    (re.compile(r"\bmotion\s+to\s+strike\b", re.IGNORECASE), "motion_to_strike"),
    (
        re.compile(r"\bpreliminary\s+injunction\b", re.IGNORECASE),
        "preliminary_injunction",
    ),
    # --- New patterns added for issue #260 ---
    (
        re.compile(r"\bex\s+parte\s+application\b", re.IGNORECASE),
        "ex_parte_application",
    ),
    (
        re.compile(r"\bex\s+parte\s+motion\b", re.IGNORECASE),
        "ex_parte_application",
    ),
    (
        re.compile(
            r"\bpetition\s+for\s+writ\s+of\s+(?:mandate|mandamus)\b",
            re.IGNORECASE,
        ),
        "petition_writ_of_mandate",
    ),
    (
        re.compile(
            r"\bpetition\s+for\s+writ\s+of\s+habeas\s+corpus\b",
            re.IGNORECASE,
        ),
        "petition_habeas_corpus",
    ),
    (
        re.compile(
            r"\bclass\s+action\s+settlement\b|\bpreliminary\s+approval\b",
            re.IGNORECASE,
        ),
        "class_action_settlement",
    ),
    # --- New patterns added for issue #1815 (remaining OC null motion_type) ---
    (
        re.compile(
            r"\bPAGA\s+settlement\b|\bapproval\s+of\s+PAGA\b",
            re.IGNORECASE,
        ),
        "paga_settlement",
    ),
    (
        re.compile(
            r"\bsettlement\s+(?:agreement|approval|hearing)\b"
            r"|\bapproval\s+of\s+(?:\w+\s+)*?settlement\b",
            re.IGNORECASE,
        ),
        "settlement_approval",
    ),
    # --- New patterns added for issue #1767 (probate/non-standard event types) ---
    (
        re.compile(
            r"\bpetition\s+(?:for\s+)?(?:probate|to\s+administer\s+estate"
            r"|for\s+letters)\b",
            re.IGNORECASE,
        ),
        "petition_for_probate",
    ),
    (
        re.compile(
            r"\b(?:guardianship\s+petition|petition\s+for\s+"
            r"(?:guardianship|conservatorship))\b",
            re.IGNORECASE,
        ),
        "guardianship_petition",
    ),
    (
        re.compile(r"\baccounting\b", re.IGNORECASE),
        "accounting",
    ),
    (
        re.compile(r"\bshow\s+cause\s+hearing\b", re.IGNORECASE),
        "show_cause_hearing",
    ),
    (
        re.compile(r"\btrust\s+petition\b", re.IGNORECASE),
        "trust_petition",
    ),
    (
        re.compile(r"\bpetition\b", re.IGNORECASE),
        "petition",
    ),
    (
        re.compile(r"\border\s+to\s+show\s+cause\b", re.IGNORECASE),
        "osc",
    ),
    (
        re.compile(r"\bmotion\s+to\s+quash\b", re.IGNORECASE),
        "motion_to_quash",
    ),
    (
        re.compile(r"\bmotion\s+for\s+reconsideration\b", re.IGNORECASE),
        "motion_for_reconsideration",
    ),
    (
        re.compile(r"\bmotion\s+for\s+protective\s+order\b", re.IGNORECASE),
        "motion_for_protective_order",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+attorney['\u2018\u2019]?s?['\u2018\u2019]?\s*fees\b",
            re.IGNORECASE,
        ),
        "motion_for_attorney_fees",
    ),
    (
        re.compile(
            r"\bmotion\s+to\s+set\s+aside\s+(?:the\s+)?default\b",
            re.IGNORECASE,
        ),
        "motion_to_set_aside_default",
    ),
    # "Set Aside/Vacate Default" — OC uses slash notation
    (
        re.compile(
            r"\bset\s+aside\s*/\s*vacate\s+(?:the\s+)?default\b",
            re.IGNORECASE,
        ),
        "motion_to_set_aside_default",
    ),
    # "Motion to Set Aside Dismissal" — Riverside, OC (distinct from default)
    (
        re.compile(
            r"\bmotion\s+to\s+set\s+aside\s+(?:the\s+)?dismissals?\b",
            re.IGNORECASE,
        ),
        "motion_to_set_aside_dismissal",
    ),
    (
        re.compile(r"\bmotion\s+to\s+vacate\b", re.IGNORECASE),
        "motion_to_vacate",
    ),
    # --- New patterns added for issue #421 ---
    (
        re.compile(r"\bdefault\s+judgment\b", re.IGNORECASE),
        "default_judgment",
    ),
    (
        re.compile(r"\bto\s+be\s+relieved\s+as\s+counsel\b", re.IGNORECASE),
        "motion_to_be_relieved_as_counsel",
    ),
    (
        re.compile(r"\bmotion\s+for\s+leave\b", re.IGNORECASE),
        "motion_for_leave_to_amend",
    ),
    (
        re.compile(r"\bmotion\s+for\s+sanctions\b", re.IGNORECASE),
        "motion_for_sanctions",
    ),
    (
        re.compile(r"\bmotion\s+for\s+relief\b", re.IGNORECASE),
        "motion_for_relief",
    ),
    (
        re.compile(r"\bmotion\s+for\s+pro\s+hac\s+vice\b", re.IGNORECASE),
        "motion_pro_hac_vice",
    ),
    (
        re.compile(r"\bmotion\s+to\s+substitute\b", re.IGNORECASE),
        "motion_to_substitute",
    ),
    (
        re.compile(r"\bMILs?\b"),
        "mil",
    ),
    (
        re.compile(r"\bmotion\s+to\s+tax\s+costs\b", re.IGNORECASE),
        "motion_to_tax_costs",
    ),
    (
        re.compile(r"\bwrit\s+of\s+possession\b", re.IGNORECASE),
        "writ_of_possession",
    ),
    (
        re.compile(r"\bmotion\s+for\s+new\s+trial\b", re.IGNORECASE),
        "motion_for_new_trial",
    ),
    # --- New patterns added for issue #1783 ---
    (
        re.compile(
            r"\bmotion\s+for\s+judgment\s+on\s+the\s+pleadings\b",
            re.IGNORECASE,
        ),
        "motion_for_judgment_on_the_pleadings",
    ),
    (
        re.compile(
            r"\bmotion\s+to\s+deem\b.*\badmissions?\s+admitted\b",
            re.IGNORECASE,
        ),
        "deem_admissions_admitted",
    ),
    (
        re.compile(r"\bmotion\s+to\s+deem\s+requests?\b", re.IGNORECASE),
        "deem_admissions_admitted",
    ),
    # --- New patterns added for issue #2022 (Riverside enrichment gaps) ---
    (
        re.compile(
            r"\bmotion\s+(?:for\s+|to\s+)?(?:enter\s+|entry\s+of\s+)?stipulated\s+judgment\b",
            re.IGNORECASE,
        ),
        "motion_for_stipulated_judgment",
    ),
    (
        re.compile(
            r"\bmotion\s+(?:for\s+)?entry\s+of\s+judgment\b",
            re.IGNORECASE,
        ),
        "motion_for_entry_of_judgment",
    ),
    (
        re.compile(r"\bmotion\s+to\s+bifurcate\b", re.IGNORECASE),
        "motion_to_bifurcate",
    ),
    (
        re.compile(r"\bmotion\s+for\s+class\s+certification\b", re.IGNORECASE),
        "motion_for_class_certification",
    ),
    (
        re.compile(r"\bmotion\s+to\s+reclassify\b", re.IGNORECASE),
        "motion_to_reclassify",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+(?:judicial\s+)?approval\b",
            re.IGNORECASE,
        ),
        "motion_for_judicial_approval",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+(?:appointment\s+of\s+)?receiver\b",
            re.IGNORECASE,
        ),
        "motion_for_appointment_of_receiver",
    ),
    (
        re.compile(
            r"\bright\s+to\s+attach\s+order\b",
            re.IGNORECASE,
        ),
        "right_to_attach_order",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+order\b.*\badmissions?\s+(?:to\s+be\s+)?(?:deemed\s+)?admitted\b",
            re.IGNORECASE,
        ),
        "deem_admissions_admitted",
    ),
    (
        re.compile(
            r"\bmotion\s+(?:for\s+)?(?:order\s+)?(?:for\s+|to\s+)?deem(?:ing)?\s+(?:matters?\s+)?admitted\b",
            re.IGNORECASE,
        ),
        "deem_admissions_admitted",
    ),
    (
        re.compile(
            r"\badmissions?\s+(?:to\s+be\s+)?deemed\s+admitted\b",
            re.IGNORECASE,
        ),
        "deem_admissions_admitted",
    ),
    (
        re.compile(r"\bmotion\s+to\s+consolidate\b", re.IGNORECASE),
        "motion_to_consolidate",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+(?:order\s+)?(?:authorizing\s+)?recordation\b",
            re.IGNORECASE,
        ),
        "lis_pendens",
    ),
    (
        re.compile(
            r"\b(?:notice\s+of\s+)?(?:pendency\s+of\s+action|lis\s+pendens)\b",
            re.IGNORECASE,
        ),
        "lis_pendens",
    ),
    (
        re.compile(r"\bmotion\s+(?:for|to)\s+(?:transfer|change)\s+venue\b", re.IGNORECASE),
        "motion_to_transfer_venue",
    ),
    (
        re.compile(r"\bmotion\s+to\s+seal\b", re.IGNORECASE),
        "motion_to_seal",
    ),
    (
        re.compile(
            r"\brequest\s+for\s+dismissal\b",
            re.IGNORECASE,
        ),
        "request_for_dismissal",
    ),
    (
        re.compile(
            r"\bwrit\s+of\s+attachment\b",
            re.IGNORECASE,
        ),
        "right_to_attach_order",
    ),
    # --- New patterns added for issue #2061 (motion_type extraction gaps) ---
    # "good faith settlement" / "determination of good faith settlement" — OC, SB
    (
        re.compile(
            r"\b(?:determination\s+of\s+)?good\s+faith\s+settlement\b",
            re.IGNORECASE,
        ),
        "good_faith_settlement",
    ),
    # "application for good faith settlement" — SB variant
    (
        re.compile(
            r"\bapplication\s+for\s+good\s+faith\s+settlement\b",
            re.IGNORECASE,
        ),
        "good_faith_settlement",
    ),
    # "motion for continuance" / "continue trial" / "continuance of trial" —
    # OC, SC, LA, Ventura
    (
        re.compile(
            r"\bmotion\s+(?:for\s+)?continuance\b",
            re.IGNORECASE,
        ),
        "motion_for_continuance",
    ),
    (
        re.compile(
            r"\bmotion\s+to\s+continue\s+(?:the\s+)?trial\b",
            re.IGNORECASE,
        ),
        "motion_for_continuance",
    ),
    (
        re.compile(
            r"\bcontinuance\s+of\s+trial\b",
            re.IGNORECASE,
        ),
        "motion_for_continuance",
    ),
    # "request for order" / "RFO" — SF family law
    (
        re.compile(
            r"\brequest\s+for\s+order\b",
            re.IGNORECASE,
        ),
        "request_for_order",
    ),
    (
        re.compile(r"\bRFO\b"),
        "request_for_order",
    ),
    # "claim of exemption" — Ventura
    (
        re.compile(
            r"\bclaim\s+of\s+exemption\b",
            re.IGNORECASE,
        ),
        "claim_of_exemption",
    ),
    # "vexatious litigant" — LA, SF
    (
        re.compile(
            r"\bvexatious\s+litigant\b",
            re.IGNORECASE,
        ),
        "motion_vexatious_litigant",
    ),
    # "trial preference" — LA
    (
        re.compile(
            r"\b(?:motion\s+for\s+)?trial\s+preference\b",
            re.IGNORECASE,
        ),
        "motion_for_trial_preference",
    ),
    # "motion to disqualify" / "disqualification" — Ventura
    (
        re.compile(
            r"\b(?:motion\s+to\s+)?disqualif(?:y|ication)\b",
            re.IGNORECASE,
        ),
        "motion_to_disqualify",
    ),
    # "motion for mental examination" / "motion for examination" — SC
    (
        re.compile(
            r"\bmotion\s+for\s+(?:a\s+)?(?:mental\s+)?examination\b",
            re.IGNORECASE,
        ),
        "motion_for_examination",
    ),
    # "interlocutory judgment" / "judgment of partition" — SB
    (
        re.compile(
            r"\binterlocutory\s+judgment\b",
            re.IGNORECASE,
        ),
        "motion_for_interlocutory_judgment",
    ),
    (
        re.compile(
            r"\bjudgment\s+of\s+partition\b",
            re.IGNORECASE,
        ),
        "motion_for_interlocutory_judgment",
    ),
    # "motion to reinstate" — LA
    (
        re.compile(
            r"\bmotion\s+to\s+reinstate\b",
            re.IGNORECASE,
        ),
        "motion_to_reinstate",
    ),
    # "notice of proposed action" / NOPA — Ventura probate
    (
        re.compile(
            r"\bnotice\s+of\s+proposed\s+action\b",
            re.IGNORECASE,
        ),
        "notice_of_proposed_action",
    ),
    (
        re.compile(r"\bNOPA\b"),
        "notice_of_proposed_action",
    ),
    # "common identity finding" — SC
    (
        re.compile(
            r"\bcommon\s+identity\s+finding\b",
            re.IGNORECASE,
        ),
        "motion_for_common_identity",
    ),
    # "application to appear pro hac vice" — OC variant (already have "motion
    # for pro hac vice" above)
    (
        re.compile(
            r"\bapplication\s+to\s+appear\s+pro\s+hac\s+vice\b",
            re.IGNORECASE,
        ),
        "motion_pro_hac_vice",
    ),
    # "motion to permit remote testimony" — OC
    (
        re.compile(
            r"\bmotion\s+to\s+permit\s+remote\s+testimony\b",
            re.IGNORECASE,
        ),
        "motion_to_permit_remote_testimony",
    ),
    # "moves to transfer venue" — catch verb form without "motion" prefix
    (
        re.compile(
            r"\bmoves?\s+to\s+transfer\s+venue\b",
            re.IGNORECASE,
        ),
        "motion_to_transfer_venue",
    ),
    # "moves to consolidate" — catch verb form without "motion" prefix
    (
        re.compile(
            r"\bmoves?\s+to\s+consolidate\b",
            re.IGNORECASE,
        ),
        "motion_to_consolidate",
    ),
    # "moves to compel" — catch verb form without "motion" prefix
    (
        re.compile(
            r"\bmoves?\s+(?:for\s+an\s+order\s+)?(?:to\s+)?compel(?:ling)?\b",
            re.IGNORECASE,
        ),
        "motion_to_compel",
    ),
    # Broad ex parte fallback — must come after specific ex_parte_application/motion
    (
        re.compile(r"\bex\s+parte\b", re.IGNORECASE),
        "ex_parte_application",
    ),
    # Broad "Hearing re:" pattern — extracts the motion type from Riverside's
    # "Hearing re: Motion to/for X" format.  Must come last as a catch-all.
    (
        re.compile(
            r"\bhearing\s+(?:re|on)[:\s]+(?:motion\s+(?:to|for)\s+)",
            re.IGNORECASE,
        ),
        "other",
    ),
]


# ---------------------------------------------------------------------------
# Outcome normalization
# ---------------------------------------------------------------------------

# Valid ruling_outcome enum values in PostgreSQL.
_VALID_OUTCOMES: frozenset[str] = frozenset(
    {
        "granted",
        "denied",
        "granted_in_part",
        "denied_in_part",
        "moot",
        "continued",
        "off_calendar",
        "submitted",
        "other",
    }
)

# Aliases for common non-standard outcome strings produced by scrapers.
_OUTCOME_ALIAS: dict[str, str] = {
    "no tentative ruling": "other",
    "no tentative": "other",
    "no tentative decision": "other",
    "no appearance required": "other",
    "hearing required": "other",
    "withdrawn": "off_calendar",
    "vacated": "off_calendar",
    "sustained": "granted",
    "sustain": "granted",
    "overruled": "denied",
    "overrule": "denied",
    "off calendar": "off_calendar",
    "granted in part": "granted_in_part",
    "denied in part": "denied_in_part",
    "denied without prejudice": "denied",
    "sustained without leave to amend": "granted",
    "sustained with leave to amend": "granted",
    "grant": "granted",
    "deny": "denied",
}

# Ordered so more specific outcomes match first (e.g. "granted in part"
# before "granted") when doing partial substring matching.
_OUTCOME_PARTIAL_ORDER: list[str] = [
    "granted_in_part",
    "denied_in_part",
    "granted",
    "denied",
    "moot",
    "continued",
    "off_calendar",
    "submitted",
]


def normalize_outcome(raw: str | None) -> str | None:
    """Normalize a raw outcome string to a valid ``ruling_outcome`` enum value.

    Handles title-case, uppercase, common aliases (e.g. ``"No Tentative
    Ruling"`` -> ``"other"``), and partial substring matching.  Returns
    ``None`` if the input is ``None``, empty, or cannot be mapped.

    This is the single canonical normalization function — used by both the
    live ingestion worker and the reingest pipeline.
    """
    if not raw:
        return None
    lower = raw.strip().lower()
    if not lower:
        return None
    # Direct match after lowercasing (e.g. "Granted" -> "granted").
    if lower in _VALID_OUTCOMES:
        return lower
    # Check aliases (e.g. "No Tentative Ruling" -> "other").
    if lower in _OUTCOME_ALIAS:
        return _OUTCOME_ALIAS[lower]
    # Partial match: e.g. "granted in part" within longer text.
    # Uses word boundaries to avoid false positives on substrings.
    for outcome in _OUTCOME_PARTIAL_ORDER:
        if re.search(r"\b" + re.escape(outcome.replace("_", " ")) + r"\b", lower):
            return outcome
    return None


# Set of all known normalized (snake_case) motion type values.
_KNOWN_MOTION_TYPES: frozenset[str] = frozenset(value for _, value in _MOTION_TYPE_PATTERNS)


# ---------------------------------------------------------------------------
# Prefix-less motion type patterns (#1783)
# ---------------------------------------------------------------------------
# Some scrapers (notably Riverside) produce motion type descriptions without
# the "motion to/for" prefix — e.g. "Attorney's Fees" instead of "Motion for
# Attorney's Fees", or "Compel Plaintiff's Responses" instead of "Motion to
# Compel Plaintiff's Responses".  These patterns are used ONLY by
# normalize_motion_type() as a fallback after the standard
# extract_motion_type() patterns fail.  They are intentionally broader than
# _MOTION_TYPE_PATTERNS because false positives are unlikely in scraper
# metadata (short strings) whereas they would be problematic in full ruling
# text.

_PREFIX_LESS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Order: more specific patterns before broader ones.
    (
        re.compile(r"\battorney['\u2018\u2019]?s?['\u2018\u2019]?\s*fees?\b", re.IGNORECASE),
        "motion_for_attorney_fees",
    ),
    (re.compile(r"\bnew\s+trial\b", re.IGNORECASE), "motion_for_new_trial"),
    (
        re.compile(r"\bjudgment\s+on\s+the\s+pleadings\b", re.IGNORECASE),
        "motion_for_judgment_on_the_pleadings",
    ),
    (
        re.compile(r"\bdeem\b.*\badmissions?\s+admitted\b", re.IGNORECASE),
        "deem_admissions_admitted",
    ),
    (re.compile(r"\bdeem\s+requests?\b", re.IGNORECASE), "deem_admissions_admitted"),
    (
        re.compile(r"\bterminating\s+sanctions?\b", re.IGNORECASE),
        "motion_for_sanctions",
    ),
    (re.compile(r"\bterminating\b", re.IGNORECASE), "motion_for_sanctions"),
    (
        re.compile(r"\bmonetary\s+sanctions?\b", re.IGNORECASE),
        "motion_for_sanctions",
    ),
    (re.compile(r"\bcompel\b", re.IGNORECASE), "motion_to_compel"),
    (
        re.compile(r"\bproduction\s+of\s+documents?\b", re.IGNORECASE),
        "motion_to_compel",
    ),
    (re.compile(r"\bprotective\s+order\b", re.IGNORECASE), "motion_for_protective_order"),
    (re.compile(r"\brelief\s+from\s+default\b", re.IGNORECASE), "motion_to_set_aside_default"),
    (re.compile(r"\bleave\s+to\s+amend\b", re.IGNORECASE), "motion_for_leave_to_amend"),
    (re.compile(r"\bstrike\b", re.IGNORECASE), "motion_to_strike"),
    (re.compile(r"\bquash\b", re.IGNORECASE), "motion_to_quash"),
    (re.compile(r"\breconsideration\b", re.IGNORECASE), "motion_for_reconsideration"),
    (re.compile(r"\bpro\s+hac\s+vice\b", re.IGNORECASE), "motion_pro_hac_vice"),
    (re.compile(r"\btax\s+costs\b", re.IGNORECASE), "motion_to_tax_costs"),
    (re.compile(r"\bvacate\b", re.IGNORECASE), "motion_to_vacate"),
    # Settlement patterns (#1815) — more specific before broader.
    (re.compile(r"\bPAGA\s+settlement\b", re.IGNORECASE), "paga_settlement"),
    (
        re.compile(r"\bsettlement\s+(?:agreement|approval|hearing)\b", re.IGNORECASE),
        "settlement_approval",
    ),
    # --- New patterns added for issue #2022 (Riverside enrichment gaps) ---
    (re.compile(r"\bstipulated\s+judgment\b", re.IGNORECASE), "motion_for_stipulated_judgment"),
    (re.compile(r"\bentry\s+of\s+judgment\b", re.IGNORECASE), "motion_for_entry_of_judgment"),
    (re.compile(r"\bbifurcate\b", re.IGNORECASE), "motion_to_bifurcate"),
    (re.compile(r"\bclass\s+certification\b", re.IGNORECASE), "motion_for_class_certification"),
    (re.compile(r"\breclassify\b", re.IGNORECASE), "motion_to_reclassify"),
    (re.compile(r"\bjudicial\s+approval\b", re.IGNORECASE), "motion_for_judicial_approval"),
    (
        re.compile(r"\bappointment\s+of\s+receiver\b", re.IGNORECASE),
        "motion_for_appointment_of_receiver",
    ),
    (
        re.compile(r"\breceiver\b", re.IGNORECASE),
        "motion_for_appointment_of_receiver",
    ),
    (re.compile(r"\bright\s+to\s+attach\b", re.IGNORECASE), "right_to_attach_order"),
    (re.compile(r"\bconsolidate\b", re.IGNORECASE), "motion_to_consolidate"),
    (re.compile(r"\blis\s+pendens\b", re.IGNORECASE), "lis_pendens"),
    (re.compile(r"\bpendency\s+of\s+action\b", re.IGNORECASE), "lis_pendens"),
    (re.compile(r"\btransfer\s+venue\b", re.IGNORECASE), "motion_to_transfer_venue"),
    (re.compile(r"\bchange\s+(?:of\s+)?venue\b", re.IGNORECASE), "motion_to_transfer_venue"),
    (re.compile(r"\bseal\b", re.IGNORECASE), "motion_to_seal"),
    # "set aside dismissal" must come before broad "dismissal" (#2061)
    (
        re.compile(r"\bset\s+aside\s+dismissals?\b", re.IGNORECASE),
        "motion_to_set_aside_dismissal",
    ),
    (re.compile(r"\bdismissal\b", re.IGNORECASE), "request_for_dismissal"),
    # --- New prefix-less patterns added for issue #2061 ---
    (re.compile(r"\bgood\s+faith\s+settlement\b", re.IGNORECASE), "good_faith_settlement"),
    (
        re.compile(r"\bcompel\s+arbitration\b", re.IGNORECASE),
        "motion_to_compel_arbitration",
    ),
    (re.compile(r"\bcontinuance\b", re.IGNORECASE), "motion_for_continuance"),
    (re.compile(r"\btrial\s+preference\b", re.IGNORECASE), "motion_for_trial_preference"),
    (re.compile(r"\bvexatious\s+litigant\b", re.IGNORECASE), "motion_vexatious_litigant"),
    (re.compile(r"\bclaim\s+of\s+exemption\b", re.IGNORECASE), "claim_of_exemption"),
    (re.compile(r"\bdisqualif(?:y|ication)\b", re.IGNORECASE), "motion_to_disqualify"),
    (
        re.compile(r"\b(?:mental\s+)?examination\b", re.IGNORECASE),
        "motion_for_examination",
    ),
    (
        re.compile(r"\binterlocutory\s+judgment\b", re.IGNORECASE),
        "motion_for_interlocutory_judgment",
    ),
    (re.compile(r"\breinstate\b", re.IGNORECASE), "motion_to_reinstate"),
    # Broad sanctions fallback — must come after more specific standalone patterns
    # to avoid shadowing "strike", "compel", "quash", etc. when the input
    # contains multiple keywords (e.g. "Strike and Sanctions").
    (re.compile(r"\bsanctions?\b", re.IGNORECASE), "motion_for_sanctions"),
]


def normalize_motion_type(motion_type: str) -> str | None:
    """Normalize a motion type string to its canonical snake_case form.

    Scrapers may produce motion types in various formats — title case
    (e.g. ``"Motion to Compel"``), calendar event names
    (e.g. ``"Motion Hearing"``), or composite types
    (e.g. ``"Demurrer/Motion to Strike"``).  Some scrapers (notably
    Riverside) also produce prefix-less descriptions like
    ``"Attorney's Fees"`` or ``"Compel Plaintiff's Responses"``.  This
    function converts any such value to the canonical snake_case form
    used by the enrichment pipeline and ``_MOTION_TYPE_CASE_TYPE_MAP``.

    Parameters
    ----------
    motion_type : str
        The raw motion type value from a scraper or event payload.

    Returns
    -------
    str | None
        The normalized snake_case value (e.g. ``"motion_to_compel"``),
        or ``None`` if the value cannot be mapped to a known type.
        Returning ``None`` allows the caller to fall back to regex
        extraction from ruling text.
    """
    if not motion_type:
        return None

    stripped = motion_type.strip()
    if not stripped:
        return None

    # Already normalized — return as-is.
    if stripped in _KNOWN_MOTION_TYPES:
        return stripped

    # Try matching against the standard motion type patterns.  This
    # handles title-case values like "Motion to Compel Further Responses"
    # or "Demurrer to Complaint" as well as composite calendar event
    # types like "Demurrer/Motion to Strike" (first match wins).
    for pattern, value in _MOTION_TYPE_PATTERNS:
        if pattern.search(stripped):
            return value

    # Try prefix-less patterns (#1783).  These are broader patterns that
    # handle scraper metadata missing the "motion to/for" prefix — e.g.
    # "Attorney's Fees", "Compel Plaintiff's Responses", "New Trial".
    for pattern, value in _PREFIX_LESS_PATTERNS:
        if pattern.search(stripped):
            return value

    # No known mapping — return None so the caller can fall through.
    return None


# ---------------------------------------------------------------------------
# Judge name extraction
# ---------------------------------------------------------------------------

# Patterns drawn from the California court scrapers.  Each targets a
# different court's formatting style so the backfill can recover judge
# names from ruling text that was already stored in the database.

_JUDGE_NAME_PATTERNS: list[re.Pattern[str]] = [
    # LA: "William A. Crowfoot Judge of the Superior Court" (now case-insensitive
    # to also match "JARED D. MOSES JUDGE OF THE SUPERIOR COURT")
    re.compile(
        r"([^\n]+?)\s+Judge of the Superior Court",
        re.IGNORECASE,
    ),
    # SB: "Department S22 - Judge Bobby P. Luna"
    re.compile(
        r"Department\s+\S+?\s*[-\u2013\u2014]\s*Judge\s+(?P<judge_name>[^\n]+)",
        re.IGNORECASE,
    ),
    # SB alternate: "BEFORE THE HONORABLE BOBBY P. LUNA"
    re.compile(r"BEFORE THE HONORABLE\s+(?P<judge_name>[^\n]+)", re.IGNORECASE),
    # SF: "Presiding: BOBBY P. LUNA"
    re.compile(r"Presiding:\s+(?P<judge_name>[A-Z][^\n]+)"),
    # Riverside / OC style: "Department 1 - Honorable John A. Smith"
    re.compile(
        r"Department\s+\S+\s*-\s*Honorable\s+(?P<judge_name>[^\n]+)",
        re.IGNORECASE,
    ),
    # "JUDICIAL OFFICER: John A. Smith" — used by some LA and inland courts.
    # Captures everything to end of line after the colon.
    re.compile(
        r"JUDICIAL\s+OFFICER\s*:\s*(?P<judge_name>[A-Za-z][^\n]+)",
        re.IGNORECASE,
    ),
    # "Hon. John A. Smith" or "Honorable John A. Smith" (standalone, many courts).
    # Requires first+last name minimum.  Uses literal spaces (not \s) so names
    # do not span across newlines.  Supports hyphenated surnames.
    re.compile(
        r"\bHon(?:orable)?\.?[ ]+"
        r"(?P<judge_name>"
        r"[A-Z][a-z]+"  # first name
        r"(?:[ ]+[A-Z][a-z.'\-]*)*"  # middle initials/names
        r"[ ]+[A-Z][a-z]+(?:-[A-Z][a-z]+)*"  # last name (optionally hyphenated)
        r")",
    ),
    # Standalone "Judge: Name" or "Judge Name" in headers.  Same name-shape
    # constraints as "Hon." pattern.  Uses literal spaces to stay on one line.
    re.compile(
        r"(?<![a-zA-Z])"  # not preceded by a letter
        r"Judge[:  ][ ]*"
        r"(?P<judge_name>"
        r"[A-Z][a-z]+"  # first name
        r"(?:[ ]+[A-Z][a-z.'\-]*)*"  # middle initials/names
        r"[ ]+[A-Z][a-z]+(?:-[A-Z][a-z]+)*"  # last name (optionally hyphenated)
        r")"
        r"(?![ ]+of\b)",  # exclude "Judge X of the Superior Court"
    ),
    # LA ALL-CAPS header: "DEPARTMENT 56 JUDGE STEVEN A. ELLIS" (#401)
    # Matches "JUDGE FIRST [M.] LAST" in ALL-CAPS, typically preceded by
    # "DEPARTMENT <number>".  Requires at least first + last name in uppercase.
    # Case-sensitive: only matches ALL-CAPS names to avoid false positives on
    # phrases like "judge decided the case".
    re.compile(
        r"(?:DEPARTMENT\s+\S+\s+)?"
        r"JUDGE\s+"
        r"(?P<judge_name>"
        r"[A-Z]{2,}"  # first name (all caps, 2+ chars)
        r"(?:\s+[A-Z]\.?)*"  # optional middle initials
        r"\s+[A-Z]{2,}"  # last name (all caps, 2+ chars)
        r"(?:-[A-Z]{2,})?"  # optional hyphenated surname
        r")"
        r"(?!\s+[Oo][Ff]\b)",  # exclude "JUDGE X OF the Superior Court"
    ),
]


# ---------------------------------------------------------------------------
# Case number extraction
# ---------------------------------------------------------------------------

# Patterns drawn from the California court scrapers.  Ordered so the most
# specific (and least likely to false-positive) patterns match first.
# Each pattern is compiled once at module load time.

_CASE_NUMBER_PATTERNS: list[re.Pattern[str]] = [
    # LA label pattern: "Case Number: 24NNCV02551" — most reliable when present.
    # The label anchors the match so the captured group can be broad.
    # Uses [\w-]+ to also capture hyphenated case numbers like FPT-25-378624.
    re.compile(r"Case\s+Number\s*:\s*([\w-]+)", re.IGNORECASE),
    # San Francisco: F + 2 letters + hyphen + 2-digit year + hyphen + 6 digits.
    # e.g. FPT-25-378624, FMS-20-387302, FDI-14-781786
    re.compile(r"\bF[A-Z]{2}-\d{2}-\d{6}\b"),
    # San Bernardino: CIV + 2 letters + optional space + 5-8 digits.
    # e.g. CIVRS2502080, CIVSB2416631, CIVSB 2600093 (Dept S36 uses a space).
    # Callers should normalise by removing any internal whitespace.
    re.compile(r"\bCIV[A-Z]{2}\s*\d{5,8}\b"),
    # Riverside: CV + 2-4 letters + 6-8 digits. e.g. CVPS2306157
    re.compile(r"\bCV[A-Z]{2,4}\d{6,8}\b"),
    # Santa Clara: 2-digit year + CV + 6 digits. e.g. 24CV443183
    re.compile(r"\b\d{2}CV\d{6}\b"),
    # LA standalone: 2-digit year + 2-letter area code + 2-letter case type + 5-6 digits.
    # Area codes: NN, ST, AV, SC, VE, BB, PD, GC, NE, WE, LC, CC, etc.
    # Case types: CV (civil), CP (complex/probate), LC (limited civil), etc.
    # e.g. 24NNCV02551, 26NNCP00062, 23STCV12345
    re.compile(r"\b\d{2}[A-Z]{2}(?:CV|CP|LC|CC|BB|PD|GC|NE|WE)\d{5,6}\b"),
    # OC Civil: 2-4 digit prefix + hyphen + 8 digits. e.g. 30-2024-01370288
    re.compile(r"\b\d{2,4}-\d{8}\b"),
    # OC Family Law: 2-digit year + D + 6 digits. e.g. 24D006789
    re.compile(r"\b\d{2}D\d{6}\b"),
]


def extract_case_number(ruling_text: str) -> str | None:
    """Extract a case number from ruling text using court-specific regex patterns.

    Tries multiple patterns used by California court scrapers (LA, SF, SB,
    Riverside, SC, OC).  Returns the first matched case number, or ``None``
    if no pattern matches.

    This is a fallback for when the scraper does not populate ``case_number``
    in the event payload.  The patterns are ordered so more specific formats
    match first to minimize false positives.
    """
    for pattern in _CASE_NUMBER_PATTERNS:
        m = pattern.search(ruling_text)
        if m:
            # Use group(1) if there's a capture group, otherwise group(0)
            if m.lastindex and m.lastindex >= 1:
                raw = m.group(1)
            else:
                raw = m.group(0)
            # Normalise: remove internal whitespace (e.g. "CIVSB 2600093" →
            # "CIVSB2600093" for SB Dept S36 format).
            return raw.replace(" ", "")
    return None


# ---------------------------------------------------------------------------
# Case type extraction from case number prefix
# ---------------------------------------------------------------------------

# Ordered so more specific prefixes match first.  Each tuple is
# (compiled regex, case_type value).  Patterns are anchored to the start
# of the case number (after optional leading digits for court/year codes).
_CASE_TYPE_PREFIX_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Civil prefixes — CV variants (Riverside, Santa Clara, SB, LA)
    # Matches: CVRI, CVME, CVPS, CVSW, CVCO, 24CV, 24NNCV, 23STCV, CIV*, etc.
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?CV", re.IGNORECASE), "civil"),
    (re.compile(r"^CIV", re.IGNORECASE), "civil"),
    # Ventura civil codes: CU (Civil Unlimited), CL (Civil Limited)
    # Standard format: 4-digit year + CU/CL + 2-letter subtype + 6 digits
    # e.g. 2025CUBC042098, 2024CLCL035410
    # Older format: longer digit prefix (e.g. 202200570068CUMM)
    (re.compile(r"^\d{4,}CU", re.IGNORECASE), "civil"),
    (re.compile(r"^\d{4,}CL[A-Z]", re.IGNORECASE), "civil"),
    # Family law prefixes
    (re.compile(r"^(?:\d{2,4})?FL", re.IGNORECASE), "family"),
    (re.compile(r"^(?:\d{2,4})?DV", re.IGNORECASE), "family"),
    (re.compile(r"^(?:\d{2,4})?D\d", re.IGNORECASE), "family"),
    # SF Unified Family Court: FDI (dissolution), FMS (family motion),
    # FPT (parentage), FDV (domestic violence), FCS (child support).
    # Must be ordered BEFORE any generic ^F patterns (e.g. ^F\d criminal)
    # so these more specific SF family prefixes win.
    (re.compile(r"^F(?:DI|MS|PT|DV|CS)-", re.IGNORECASE), "family"),
    # Probate prefixes
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?PR", re.IGNORECASE), "probate"),
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?BP", re.IGNORECASE), "probate"),
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?CP", re.IGNORECASE), "probate"),
    # Ventura old-format probate: long digit prefix + PR + 2-letter subtype
    # e.g. 200800332092PRCP, 201200427520PRCE, 202200572082PRGE, 202200570571PRLP
    (re.compile(r"^\d{8,}PR", re.IGNORECASE), "probate"),
    # Old Ventura P-prefix probate: P + 5-6 digits (e.g. P057837, P080266)
    (re.compile(r"^P\d{5,}$", re.IGNORECASE), "probate"),
    # Small claims
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?SC", re.IGNORECASE), "small_claims"),
    # Criminal prefixes
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?CR", re.IGNORECASE), "criminal"),
    # Felony docket format: F + digit (e.g. F2301234)
    (re.compile(r"^F\d", re.IGNORECASE), "criminal"),
    # Note: SF's F[A-Z]{2}- prefixes (FDI, FMS, FPT, FDV, FCS) are family
    # law, not criminal (see SF family patterns above). Do NOT add a generic
    # ^F[A-Z]{2}- -> criminal pattern here — it would misclassify those.
    # Juvenile
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?JV", re.IGNORECASE), "juvenile"),
    # Traffic
    (re.compile(r"^(?:\d{2,4})?(?:[A-Z]{2})?TR", re.IGNORECASE), "traffic"),
    # OC civil format: digits-digits (e.g. 30-2024-01370288 or 2024-01380242)
    (re.compile(r"^\d{2,4}-\d{4,8}"), "civil"),
]


def is_valid_case_number(value: str) -> bool:
    """Return True if *value* looks like a valid case number, not a case title.

    Rejects values that contain adversarial party indicators ("vs", "v."),
    legal citation patterns, or prose text.  These are common false positives
    from LLM extraction on documents that lack formal case numbers (e.g. OC
    North Justice Center PDFs).

    Examples of invalid "case numbers" this catches:
        - "Smith v. Kia" (case title)
        - "Catalan v. FCA" (case title)
        - "Cal. App. 4th 1, 20; DuPont Merck" (legal citation)
        - "Tuinenburg v. Before the Court is a demurrer" (case title + ruling text)
    """
    if not value or not value.strip():
        return False
    stripped = value.strip()
    # Reject if it contains "vs", "vs.", or "v." — this is a case title
    if re.search(r"\bvs?\.?\s", stripped, re.IGNORECASE):
        return False
    # Reject if it contains ruling text indicators
    if re.search(
        r"\b(?:the court|motion|demurrer|granted|denied|hearing|ruling|"
        r"tentative|calendar|petition|application)\b",
        stripped,
        re.IGNORECASE,
    ):
        return False
    # Reject legal citations (e.g. "Cal. App. 4th 1, 20")
    if re.search(r"\bCal\.\s*App\b|\bF\.\s*Supp\b|\bF\.\s*\d", stripped, re.IGNORECASE):
        return False
    # Reject if it's very long (case numbers are short, titles are long)
    if len(stripped) > 30:
        return False
    return True


# ---------------------------------------------------------------------------
# Case type extraction from scraper ID
# ---------------------------------------------------------------------------

# Maps scraper_id substrings to case_type values.  The scraper_id encodes
# the court and case category (e.g. "ca-oc-tentatives-civil" -> civil).
_SCRAPER_ID_CASE_TYPE_MAP: list[tuple[str, str]] = [
    ("civil", "civil"),
    ("family", "family"),
    ("probate", "probate"),
    ("criminal", "criminal"),
    ("small-claims", "small_claims"),
    ("juvenile", "juvenile"),
    ("traffic", "traffic"),
]


def extract_case_type_from_scraper_id(scraper_id: str) -> str | None:
    """Infer case type from a scraper_id string.

    This is a zero-cost fallback when neither the LLM nor the case number
    prefix yields a case_type.  Scraper IDs encode the case category in
    their suffix (e.g. ``ca-oc-tentatives-civil``).

    Returns one of the case type strings, or ``None`` if the scraper_id
    does not contain a recognized case type indicator.
    """
    if not scraper_id:
        return None
    scraper_lower = scraper_id.lower()
    for keyword, case_type in _SCRAPER_ID_CASE_TYPE_MAP:
        if keyword in scraper_lower:
            return case_type
    return None


# ---------------------------------------------------------------------------
# Case type extraction from motion type (#1702)
# ---------------------------------------------------------------------------

# Maps normalized motion_type values to case_type values.  This is a final
# fallback when case number prefix and scraper_id fail to determine the type.
# Only motion types that unambiguously identify a case type are included.
_MOTION_TYPE_CASE_TYPE_MAP: dict[str, str] = {
    # Civil motion types
    "msj": "civil",
    "msj_partial": "civil",
    "mtd": "civil",
    "demurrer": "civil",
    "motion_to_compel": "civil",
    "anti_slapp": "civil",
    "motion_to_strike": "civil",
    "preliminary_injunction": "civil",
    "motion_for_protective_order": "civil",
    "motion_for_attorney_fees": "civil",
    "motion_to_set_aside_default": "civil",
    "motion_to_vacate": "civil",
    "default_judgment": "civil",
    "motion_to_be_relieved_as_counsel": "civil",
    "motion_for_leave_to_amend": "civil",
    "motion_for_sanctions": "civil",
    "motion_for_relief": "civil",
    "motion_pro_hac_vice": "civil",
    "motion_to_substitute": "civil",
    "motion_to_tax_costs": "civil",
    "motion_for_new_trial": "civil",
    "motion_for_reconsideration": "civil",
    "motion_to_quash": "civil",
    "motion_for_judgment_on_the_pleadings": "civil",
    "deem_admissions_admitted": "civil",
    "class_action_settlement": "civil",
    "paga_settlement": "civil",
    "settlement_approval": "civil",
    "writ_of_possession": "civil",
    "mil": "civil",
    # --- New civil motion types (#2061) ---
    "good_faith_settlement": "civil",
    "motion_to_compel_arbitration": "civil",
    "motion_for_continuance": "civil",
    "motion_for_trial_preference": "civil",
    "motion_vexatious_litigant": "civil",
    "motion_to_disqualify": "civil",
    "motion_for_examination": "civil",
    "motion_for_interlocutory_judgment": "civil",
    "motion_to_reinstate": "civil",
    "motion_to_set_aside_dismissal": "civil",
    "motion_for_common_identity": "civil",
    "motion_to_permit_remote_testimony": "civil",
    "motion_to_consolidate": "civil",
    "motion_to_transfer_venue": "civil",
    "motion_to_seal": "civil",
    "motion_to_bifurcate": "civil",
    "motion_for_class_certification": "civil",
    "motion_to_reclassify": "civil",
    "motion_for_judicial_approval": "civil",
    "motion_for_appointment_of_receiver": "civil",
    "right_to_attach_order": "civil",
    "motion_for_stipulated_judgment": "civil",
    "motion_for_entry_of_judgment": "civil",
    "lis_pendens": "civil",
    "request_for_dismissal": "civil",
    # Probate-specific motion types
    "petition": "probate",
    "petition_for_probate": "probate",
    "guardianship_petition": "probate",
    "trust_petition": "probate",
    "notice_of_proposed_action": "probate",
    # Family law
    "request_for_order": "family",
    # Claim of exemption can appear in civil and family cases — excluded.
    # Accounting and show cause hearing can appear in multiple case types — excluded.
    # Ex parte and OSC can appear in multiple case types — excluded.
    # "petition_writ_of_mandate" and "petition_habeas_corpus" are civil/criminal
    # and ambiguous enough to exclude.
}


def extract_case_type_from_motion_type(motion_type: str) -> str | None:
    """Infer case type from a normalized motion type string.

    This is a final fallback when case number prefix, LLM extraction, and
    scraper_id all fail to determine the case type.  Only unambiguous motion
    types are mapped; motion types that can appear across multiple case types
    (e.g. ``ex_parte_application``, ``osc``) return ``None``.

    Parameters
    ----------
    motion_type : str
        The normalized motion type value (e.g. ``"motion_to_compel"``).

    Returns
    -------
    str | None
        One of the case type strings, or ``None`` if the motion type is
        ambiguous or not recognized.
    """
    if not motion_type:
        return None
    return _MOTION_TYPE_CASE_TYPE_MAP.get(motion_type.strip())


def extract_case_type_from_number(case_number: str) -> str | None:
    """Infer case type from a California case number prefix.

    California courts embed case type information in case number prefixes
    (e.g. ``CVRI2502741`` is civil, ``FL2301234`` is family).  This provides
    a zero-cost fallback when the LLM does not return a ``case_type``.

    Returns one of the ``CASE_TYPE_VALUES`` strings, or ``None`` if the
    prefix is not recognized.
    """
    if not case_number:
        return None
    case_number = case_number.strip()
    if not case_number:
        return None
    for pattern, case_type in _CASE_TYPE_PREFIX_PATTERNS:
        if pattern.match(case_number):
            return case_type
    return None


# ---------------------------------------------------------------------------
# Case type extraction from case title (#2062)
# ---------------------------------------------------------------------------

# Patterns that strongly indicate probate cases when found in case titles.
_PROBATE_TITLE_RE = re.compile(
    r"(?:"
    r"In\s+(?:the\s+)?(?:Matter|Re)\s+(?:of\b)"
    r"|Conservatorship\s+(?:of\b)"
    r"|Guardianship\s+(?:of\b)"
    r"|Estate\s+(?:of\b)"
    r"|Trust\s+(?:of\b)"
    r"|Petition\s+(?:for\s+)?(?:Probate|Letters)"
    r")",
    re.IGNORECASE,
)


def extract_case_type_from_title(case_title: str) -> str | None:
    """Infer case type from a case title.

    This is a final fallback when case number prefix, LLM extraction,
    scraper_id, and motion type all fail to determine the case type.
    Only unambiguous title patterns are matched — specifically, probate-style
    titles like "In the Matter of...", "Conservatorship of...", etc.

    Parameters
    ----------
    case_title : str
        The case title to inspect.

    Returns
    -------
    str | None
        One of the case type strings, or ``None`` if the title is
        ambiguous or not recognized.
    """
    if not case_title:
        return None
    case_title = case_title.strip()
    if not case_title:
        return None
    if _PROBATE_TITLE_RE.search(case_title):
        return "probate"
    return None


# Business / organization keywords used by _looks_like_person_name().
# Compiled once at module load time.  Uses word boundaries so that
# "COUNTY OF" matches at the start of the string.
_ORG_KEYWORD_RE = re.compile(
    r"\b(?:"
    r"LLC|INC|CORP|CORPORATION|LTD|L\.P\.|N\.A\."
    r"|GROUP|MEDICAL|HOSPITAL|CLINIC|INSURANCE|BANK"
    r"|ASSOCIATES|PARTNERS|FOUNDATION|UNIVERSITY|COLLEGE"
    r"|SCHOOL|DISTRICT|AUTHORITY|COMPANY|ENTERPRISES"
    r"|SERVICES|SOLUTIONS|INDUSTRIES|HOLDINGS|PROPERTIES"
    r"|MANAGEMENT|HEALTHCARE|FINANCIAL|INVESTMENTS"
    r"|TRUST|ESTATE|ASSOCIATION|SOCIETY|UNION"
    r"|COUNTY\s+OF|CITY\s+OF|STATE\s+OF|PEOPLE\s+OF|DEPARTMENT\s+OF"
    r"|D/B/A|DBA"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_person_name(name: str) -> bool:
    """Return True if *name* plausibly represents a person (judge) rather than an organization.

    This is a heuristic guard against false positives where party names like
    "Heritage Medical Group" or "Bank of America, N.A." are extracted as judge
    names by the broad regex patterns in ``_JUDGE_NAME_PATTERNS``.

    Checks applied (any failure → False):
    - Rejects names containing common business/organization suffixes.
    - Rejects names containing "vs" or "v." (case title leaked into the field).
    - Rejects names that are too long (> 60 chars) — real judge names are short.
    - Rejects names that are too short (< 3 chars) — need at least first + last.
    """
    if not name:
        return False

    # Too long or too short
    if len(name) > 60 or len(name) < 3:
        return False

    # Normalize for matching
    upper = name.upper()

    # Case title leaked into name: contains "VS" or "V." separator
    if re.search(r"\bVS\b", upper) or re.search(r"\bV\.", upper):
        return False

    if _ORG_KEYWORD_RE.search(upper):
        return False

    # "A CALIFORNIA CORPORATION" or similar entity descriptors
    if re.search(r"\bA\s+CALIFORNIA\b", upper):
        return False

    return True


# ---------------------------------------------------------------------------

# NOTE: The following functions were removed in #2178 when LLM enrichment
# replaced regex-based extraction for motion_type, outcome, case_title,
# and parties:
#   - extract_outcome()
#   - extract_motion_type()
#   - extract_case_title() and all its helper functions
#   - extract_parties_from_caption() and helpers

# ---------------------------------------------------------------------------
# Title validation — safety net between extraction and DB write (#1958)
# ---------------------------------------------------------------------------

# Common verb/preposition prefixes that indicate motion/procedural text,
# not actual case titles.  Checked case-insensitively against the first word.
_IMPLAUSIBLE_PREFIXES = (
    "to",
    "for",
    "by",
    "re:",
    "on",
)

# Phrases that should never appear in a valid case title.
_IMPLAUSIBLE_FRAGMENTS_RE = re.compile(
    r"\b(?:GRANTED|DENIED|CONTINUED|TENTATIVE RULING|TENTATIVE RULINGS"
    r"|OVERRULED|SUSTAINED|MOOT|OFF CALENDAR|HEARING|MOTION|DEMURRER"
    r"|ORDER|Department\s+[A-Z0-9]+|Timekeeper|paralegal)\b",
    re.IGNORECASE,
)

# Pattern to detect multiple case numbers in a title — strong indicator of
# contaminated/unsplit data (#2022).  Looks for Riverside (CV...), OC
# (30-...), SB (CIV...), and generic patterns.
_MULTIPLE_CASE_NUMBERS_RE = re.compile(
    r"(?:CV[A-Z]{2,4}\d{5,}|CIV[A-Z]{2}\d{5,}|\d{2,4}-\d{7,}|\d{2}[A-Z]{2}CV\d{5,})"
    r".*"
    r"(?:CV[A-Z]{2,4}\d{5,}|CIV[A-Z]{2}\d{5,}|\d{2,4}-\d{7,}|\d{2}[A-Z]{2}CV\d{5,})",
    re.IGNORECASE,
)

# Single embedded case number in a title — a tighter version of
# _EMBEDDED_CASE_NUMBER_RE (defined below) for use as a hard rejection
# gate in is_plausible_case_title().  The numeric-dash-numeric sub-pattern
# uses \d{7,} (not \d{5,}) to avoid false positives on non-case-number
# identifiers like parcel or contract numbers (#2242).
_PLAUSIBILITY_CASE_NUMBER_RE = re.compile(
    r"\b(?:"
    r"CV[A-Z]{2,4}\d{5,}"
    r"|CIV[A-Z]{2}\d{5,}"
    r"|\d{2,4}-\d{7,}"
    r"|\d{2}[A-Z]{2}CV\d{5,}"
    r"|[A-Z]{3}\d{2}-\d{4,}"
    r"|\d{2}[A-Z]{4}\d{4,}"
    r")\b",
)

# "In the" at the start — unless followed by "Matter of" (which is a
# legitimate non-adversarial case title pattern).
_IN_THE_NOT_MATTER_RE = re.compile(
    r"^In\s+the\b(?!\s+Matter\s+of)",
    re.IGNORECASE,
)

# Length bounds
_MIN_TITLE_LENGTH = 3
_MAX_TITLE_LENGTH = 120


def is_plausible_case_title(title: str) -> bool:
    """Return True if *title* looks like a real case title, not motion text.

    This guards against false positives from ``extract_case_title()`` where
    regex patterns match motion/procedural language (e.g.
    ``"To Respond, Without Objections"``) instead of party names (#1958).

    The function is intentionally conservative — it rejects obvious junk
    while allowing legitimate edge cases through.
    """
    if not title or not title.strip():
        return False

    stripped = title.strip()

    # Length check
    if len(stripped) < _MIN_TITLE_LENGTH or len(stripped) > _MAX_TITLE_LENGTH:
        return False

    # Check first word against implausible prefixes
    first_word = stripped.split()[0].rstrip(":,.")
    if first_word.lower() in _IMPLAUSIBLE_PREFIXES:
        # Allow "In the Matter of" — it's a legitimate case title pattern
        if first_word.lower() == "in" and re.match(
            r"^In\s+(?:re|the\s+Matter\s+of)\b",
            stripped,
            re.IGNORECASE,
        ):
            return True
        return False

    # Reject "In the <something>" unless it's "In the Matter of"
    if _IN_THE_NOT_MATTER_RE.match(stripped):
        return False

    # Reject titles containing ruling/procedural fragments
    if _IMPLAUSIBLE_FRAGMENTS_RE.search(stripped):
        return False

    # Reject titles containing multiple case numbers — strong indicator
    # of unsplit/contaminated data from LLM extraction (#2022).
    if _MULTIPLE_CASE_NUMBERS_RE.search(stripped):
        return False

    # Reject titles containing an embedded case number — legitimate case
    # titles are party names only, never case numbers.  Catches
    # contamination like "TAYLOR VS. AMAZON MSC21-02349 Romeo Cerina"
    # (#2242).
    if _PLAUSIBILITY_CASE_NUMBER_RE.search(stripped):
        return False

    return True


# ---------------------------------------------------------------------------
# Deterministic case_title cleanup (#2212)
# ---------------------------------------------------------------------------

# Case citation pattern: "(YYYY) NNN Cal.App..." or "(YYYY) NNN Cal...."
_CASE_CITATION_RE = re.compile(
    r"\s*\(?\d{4}\)?\s+\d+\s+Cal\.(?:App)?",
    re.IGNORECASE,
)

# Case number pattern embedded in a title — Riverside (CVPS..., CVME...),
# OC (30-2024-...), SB (CIVSB...), LA (24STCV...), generic (MSC21-...),
# Ventura new (2025CUBC040123 / 2025CUPR042345), Ventura old-format probate
# (202200570654PRLP, 201500471558PRCE — year+6digits+PR+2letters, 16 chars).
_EMBEDDED_CASE_NUMBER_RE = re.compile(
    r"\b(?:"
    r"CV[A-Z]{2,4}\d{5,}"
    r"|CIV[A-Z]{2}\d{5,}"
    r"|\d{2,4}-\d{5,}"
    r"|\d{2}[A-Z]{2}CV\d{5,}"
    r"|[A-Z]{3}\d{2}-\d{4,}"
    r"|\d{2}[A-Z]{4}\d{4,}"
    # Ventura new format: 4-digit year + 4-letter type + 6-digit sequence
    r"|\d{4}[A-Z]{4}\d{6}"
    # Ventura old-format probate: 4-digit year + 6-digit sequence + PR + 2 letters
    # Also match partial/truncated variants (PRL) that appear in some docs.
    r"|\d{10,12}PR[A-Z]{1,3}"
    r")\b",
)

# Motion / procedural keywords that should terminate a case title.
_TITLE_TERMINATOR_RE = re.compile(
    r"\b(?:"
    r"REQUEST\s+FOR|HEARING\s+ON|MOTION\s+(?:TO|FOR|IN|RE)"
    r"|PETITION\s+(?:TO|FOR|OF|RE)"
    r"|DEMURRER|ORDER\s+(?:TO|RE|ON)"
    r"|FORM\s+INTERROGATOR"
    r"|SPECIAL\s+INTERROGATOR"
    r"|REQUEST\s+FOR\s+(?:PRODUCTION|ADMISSION)"
    r"|NOTICE\s+OF"
    r"|by\s+and\s+through\s+(?:his|her|its|their)\s+Guardian"
    r")\b",
    re.IGNORECASE,
)


def clean_case_title(raw_title: str) -> str | None:
    """Clean a raw LLM-extracted case_title using extractive parsing (#2212).

    Takes a messy title that may contain motion descriptions, case citations,
    multiple parties from different cases, or embedded case numbers, and
    extracts just the first ``Plaintiff v. Defendant`` (or probate-style)
    title.

    Strategy:
      1. Find the first ``v./vs./V.`` separator.
      2. Walk backward from the separator for the plaintiff — stop at case
         numbers, start of string, or known delimiters.
      3. Walk forward from the separator for the defendant — stop at the
         next case number, second ``v./vs.``, motion keyword, case citation,
         or end of reasonable name.
      4. For probate titles (no ``v.``): extract
         ``IN THE MATTER OF: {name}``, ``ESTATE OF: {name}``,
         ``CONSERVATORSHIP OF: {name}``, ``GUARDIANSHIP OF: {name}``
         up to the next pattern or case number.
      5. Normalize casing and clean up artifacts.

    Returns the cleaned title, or ``None`` if the raw title cannot be
    meaningfully cleaned (e.g., empty or contains only case numbers).
    """
    if not raw_title or not raw_title.strip():
        return None

    # Collapse whitespace and newlines.
    cleaned = " ".join(raw_title.split())

    # Dedupe repeated title segments (#2370) — Ventura LLM sometimes
    # returns the same title 2-3x concatenated.
    deduped = dedupe_repeated_title(cleaned)
    if deduped:
        cleaned = deduped

    # Strip trailing case numbers appended to the title (#2370).
    without_cn = strip_trailing_case_number(cleaned)
    if without_cn:
        cleaned = without_cn

    # --- Probate / estate / conservatorship / guardianship titles ---
    probate_m = re.match(
        r"(?:IN\s+(?:THE\s+)?MATTER\s+OF"
        r"|ESTATE\s+OF"
        r"|CONSERVATORSHIP\s+OF"
        r"|GUARDIANSHIP\s+OF"
        r")\s*:?\s*",
        cleaned,
        re.IGNORECASE,
    )
    if probate_m:
        # Extract the name portion after the probate keyword.
        prefix = cleaned[: probate_m.end()]
        remainder = cleaned[probate_m.end() :]
        # Truncate at next case number, another probate keyword, or
        # motion keyword.
        end_pos = len(remainder)
        for pattern in [_EMBEDDED_CASE_NUMBER_RE, _TITLE_TERMINATOR_RE]:
            m = pattern.search(remainder)
            if m and m.start() < end_pos:
                end_pos = m.start()
        # Also truncate at a second probate keyword.  Skip matches that
        # look like they are part of the current title's subject
        # (e.g., "IN THE MATTER OF: The Estate of Williams" should not
        # truncate at "Estate of" because it's describing the subject).
        # A genuine second case starts after at least one name token.
        for second_probate in re.finditer(
            r"\b(?:CONSERVATORSHIP|ESTATE|GUARDIANSHIP|IN\s+THE\s+MATTER)\s+OF\b",
            remainder,
            re.IGNORECASE,
        ):
            # Only truncate if there's at least one name word before
            # this match (not just "The" or "of").
            before = remainder[: second_probate.start()].strip()
            # Require at least one word that is not a function word
            name_words = [
                w for w in before.split() if w.lower() not in ("the", "of", "a", "an", "and")
            ]
            if name_words and second_probate.start() < end_pos:
                end_pos = second_probate.start()
                break
        name = remainder[:end_pos].strip().rstrip(".,;: ")
        if name:
            result = prefix.strip() + " " + name
            result = result.strip()
            if len(result) <= _MAX_TITLE_LENGTH:
                return result
            # Hard-truncate at a word boundary if still too long.
            space_idx = result.rfind(" ", 0, _MAX_TITLE_LENGTH)
            if space_idx > 20:
                return result[:space_idx].rstrip(".,;: ")
        return None

    # --- Adversarial titles (v./vs.) ---
    vs_match = re.search(r"\s+[Vv][Ss]?\.?\s+", cleaned)
    if vs_match is None:
        # No "v." separator — return None (can't extract a proper title).
        return None

    # Walk backward for the plaintiff.
    before_vs = cleaned[: vs_match.start()]
    # Find where the plaintiff name starts — strip leading case numbers,
    # court names, and other non-name content.
    plaintiff_start = 0
    # Trim leading case numbers.
    leading_cn = _EMBEDDED_CASE_NUMBER_RE.match(before_vs)
    if leading_cn:
        plaintiff_start = leading_cn.end()
    plaintiff = before_vs[plaintiff_start:].strip()

    # Walk forward for the defendant.
    after_vs = cleaned[vs_match.end() :]

    # Find where the defendant name ends — truncate at:
    # 1. A second "v./vs." (multiple cases jammed together)
    # 2. An embedded case number
    # 3. A motion keyword / procedural text
    # 4. A case citation
    defendant_end = len(after_vs)

    # Check for second vs. separator (multiple cases jammed together).
    second_vs = re.search(r"\s+[Vv][Ss]?\.?\s+", after_vs)
    if second_vs:
        # The text before the second "vs." contains the first defendant
        # AND the second case's plaintiff.  We need to find where the
        # first defendant ends and the second plaintiff begins.
        pre_second_vs = after_vs[: second_vs.start()]
        # Strategy 1: entity ending (LLC, Inc., etc.)
        name_boundary = _find_second_case_boundary(pre_second_vs)
        if name_boundary is not None and name_boundary < defendant_end:
            defendant_end = name_boundary
        else:
            # Strategy 2: walk backward from the second "vs." to strip
            # the second plaintiff's name.  Find the last sequence of
            # capitalized words before the second "vs." that looks like
            # a person's name (2+ words starting with uppercase).
            second_plaintiff_start = _find_name_start_before_vs(pre_second_vs)
            if (
                second_plaintiff_start is not None
                and second_plaintiff_start < defendant_end
                and second_plaintiff_start > 3  # keep at least something
            ):
                defendant_end = second_plaintiff_start

    # Check for embedded case number.
    cn_match = _EMBEDDED_CASE_NUMBER_RE.search(after_vs)
    if cn_match and cn_match.start() < defendant_end:
        defendant_end = cn_match.start()

    # Check for motion/procedural keyword.
    term_match = _TITLE_TERMINATOR_RE.search(after_vs)
    if term_match and term_match.start() < defendant_end:
        defendant_end = term_match.start()

    # Check for case citation.
    cite_match = _CASE_CITATION_RE.search(after_vs)
    if cite_match and cite_match.start() < defendant_end:
        defendant_end = cite_match.start()

    defendant = after_vs[:defendant_end].strip().rstrip(".,;: ")

    if not plaintiff or not defendant:
        return None

    # Detect all-caps before normalizing.
    plaintiff_upper = plaintiff == plaintiff.upper() and plaintiff.strip()
    defendant_upper = defendant == defendant.upper() and defendant.strip()
    is_all_caps = plaintiff_upper and defendant_upper

    # Normalize the "v." separator.
    title = f"{plaintiff} v. {defendant}"

    # Title-case if all-caps.
    if is_all_caps:
        parts = title.split(" v. ")
        title = " v. ".join(p.title() for p in parts)

    # Clean trailing punctuation.
    title = title.rstrip(".,;: ")

    # Strip trailing "et al" that may have been left with trailing junk.
    title = re.sub(r"\s+et\s+al\.?\s*$", " et al.", title, flags=re.IGNORECASE)

    # Validate length.
    if len(title) > _MAX_TITLE_LENGTH:
        # Try truncating defendant at a word boundary.
        parts = title.split(" v. ", 1)
        if len(parts) == 2:
            max_def_len = _MAX_TITLE_LENGTH - len(parts[0]) - 4  # " v. "
            if max_def_len > 10:
                space_idx = parts[1].rfind(" ", 0, max_def_len)
                if space_idx > 5:
                    title = parts[0] + " v. " + parts[1][:space_idx].rstrip(".,;: ")
        if len(title) > _MAX_TITLE_LENGTH:
            return None

    if len(title) < _MIN_TITLE_LENGTH:
        return None

    return title


def _find_second_case_boundary(text: str) -> int | None:
    """Find where a second case's party name starts within defendant text.

    When multiple cases are jammed together like:
    ``GENERAL MOTORS LLC Dustin A Brazil``
    This finds the position where "Dustin A Brazil" starts (the second
    case's plaintiff).

    Heuristic: look for the last sequence of capitalized words that
    appears to be a person's name (2+ capitalized words in a row) before
    the text ends.  If we find such a sequence and there's preceding text
    that looks like a complete party name (LLC, Inc., etc. or just a
    name), return the start of the second name.
    """
    if not text.strip():
        return None

    # Split into words.
    words = text.split()
    if len(words) <= 2:
        return None

    # Look for a transition point where a new party name starts.
    # Indicators of a name boundary:
    # - Previous word ends an entity (LLC, Inc., Corp., etc.) or "al."
    # - There's a case number before the new name
    entity_endings = {
        "LLC",
        "INC",
        "INC.",
        "CORP",
        "CORP.",
        "CORPORATION",
        "LTD",
        "LTD.",
        "LP",
        "L.P.",
        "CO",
        "CO.",
        "AL.",
        "AL",
    }

    # Reconstruct positions for each word in the original text.
    positions: list[int] = []
    idx = 0
    for word in words:
        pos = text.index(word, idx)
        positions.append(pos)
        idx = pos + len(word)

    for i in range(1, len(words)):
        prev_word_upper = words[i - 1].upper().rstrip(",.")
        if prev_word_upper in entity_endings:
            return positions[i]

    return None


def _find_name_start_before_vs(text: str) -> int | None:
    """Find where the second case's plaintiff name starts in text before "vs.".

    Given text like ``LAKERIDGE ATHLETIC CLUB Nazir``, this finds the position
    where ``Nazir`` starts.  It works by walking backward from the end of the
    text to find where a new name begins — indicated by a transition from
    all-caps words to mixed-case, or by finding a name-like word sequence
    at the end that is clearly separate from the preceding entity name.

    Returns the character position where the second plaintiff's name starts,
    or ``None`` if no clear boundary is found.
    """
    if not text.strip():
        return None

    words = text.split()
    if len(words) <= 2:
        return None

    # Reconstruct positions for each word in the original text.
    positions: list[int] = []
    idx = 0
    for word in words:
        pos = text.index(word, idx)
        positions.append(pos)
        idx = pos + len(word)

    # Look for casing transition: preceding words all-caps, trailing words
    # mixed-case (e.g. "LAKERIDGE ATHLETIC CLUB Nazir").
    for i in range(len(words) - 1, 0, -1):
        word = words[i].rstrip(",.")
        prev_word = words[i - 1].rstrip(",.")
        # Transition: previous word is all-caps, current is not
        if prev_word == prev_word.upper() and word != word.upper() and word[0].isupper():
            # Verify that ALL preceding words are uppercase
            all_upper = all(w.rstrip(",.") == w.rstrip(",.").upper() for w in words[:i])
            if all_upper:
                return positions[i]

    return None


# ---------------------------------------------------------------------------
# Hearing date extraction
# ---------------------------------------------------------------------------

# Common date formats found in California tentative rulings.
# Ordered by specificity — "Month DD, YYYY" first (most common in court PDFs),
# then numeric formats.
_HEARING_DATE_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    # "February 24, 2026" or "February 24 2026" (with or without comma)
    (
        re.compile(
            r"(?:January|February|March|April|May|June|July|August"
            r"|September|October|November|December)"
            r"\s+\d{1,2},?\s+\d{4}",
            re.IGNORECASE,
        ),
        ["%B %d, %Y", "%B %d %Y"],
    ),
    # "Date: 03/04/26" or "Date: 03/04/2026"
    (
        re.compile(
            r"Date:\s*(\d{1,2}/\d{1,2}/\d{2,4})",
            re.IGNORECASE,
        ),
        ["%m/%d/%Y", "%m/%d/%y"],
    ),
    # Standalone MM/DD/YYYY or MM/DD/YY (less anchored — try last)
    (
        re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b"),
        ["%m/%d/%Y"],
    ),
]


def extract_hearing_date(ruling_text: str) -> date | None:
    """Extract a hearing date from ruling text using common date patterns.

    Returns the first successfully parsed date, or ``None`` if no pattern
    matches.  This is a fallback for when scrapers do not populate
    ``hearing_date`` in the event payload.
    """
    for pattern, formats in _HEARING_DATE_PATTERNS:
        m = pattern.search(ruling_text)
        if not m:
            continue
        # Use capture group if present, otherwise full match
        raw = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
        raw = " ".join(raw.split())  # normalize whitespace
        for fmt in formats:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


def extract_judge_name(ruling_text: str) -> str | None:
    """Extract a judge name from ruling text using court-specific regex patterns.

    Tries multiple patterns used by California court scrapers (LA, SB, SF,
    Riverside, OC).  Returns the first matched name stripped of whitespace,
    or ``None`` if no pattern matches.

    After a pattern match, the extracted name is validated with
    ``_looks_like_person_name`` to reject false positives where party or
    organization names are captured instead of judge names (see #326).

    The returned name is *raw* — callers should pass it through
    ``normalize_judge_name`` before using it as a canonical name.
    """
    for pattern in _JUDGE_NAME_PATTERNS:
        m = pattern.search(ruling_text)
        if m:
            # Use named group 'judge_name' if present, otherwise group 1
            try:
                name = m.group("judge_name")
            except IndexError:
                name = m.group(1)
            name = " ".join(name.strip().split())  # collapse whitespace
            if name and _looks_like_person_name(name):
                return name
    return None


# ---------------------------------------------------------------------------
# Ventura-specific extraction fixes (#2370)
# ---------------------------------------------------------------------------

# Trailing case number pattern — matches case numbers that appear at the
# end of a title string.  Used to strip case numbers that the LLM or
# scraper accidentally appends to the title.  Covers Ventura formats
# (new and old probate) plus common short fragments.
_TRAILING_CASE_NUMBER_RE = re.compile(
    r"\s+(?:"
    # Ventura new format: 4-digit year + 4-letter type + 6-digit sequence
    r"\d{4}[A-Z]{4}\d{6}"
    # Ventura old-format probate (plus truncated PRL variants).
    r"|\d{10,12}PR[A-Z]{1,3}"
    # Generic CV... / CIV... / dash-separated formats
    r"|CV[A-Z]{2,4}\d{5,}"
    r"|CIV[A-Z]{2}\d{5,}"
    r"|\d{2,4}-\d{5,}"
    r"|\d{2}[A-Z]{2}CV\d{5,}"
    r")\s*$",
)


def strip_trailing_case_number(title: str | None) -> str | None:
    """Strip a trailing case number from a case title (#2370).

    Observed bug: Ventura probate titles often end with the case number,
    e.g. ``"In the Matter of Denise Guadalupe Mejia 202200570654PRLP"``.
    This helper removes the trailing case number so the title contains
    only the party/decedent name.

    Parameters
    ----------
    title : str | None
        The raw title that may have a trailing case number.

    Returns
    -------
    str | None
        The title with the trailing case number stripped, or the original
        input if no trailing case number was detected.  Returns ``None``
        if the input is ``None``.  Returns an empty string unchanged.
    """
    if title is None:
        return None
    if not title:
        return title
    stripped = _TRAILING_CASE_NUMBER_RE.sub("", title).rstrip(" .,;:")
    return stripped if stripped else title


def dedupe_repeated_title(title: str | None) -> str | None:
    """Collapse exact repeated substrings in a case title (#2370).

    Observed bug: Ventura LLM returns titles like
    ``"CITY OF THOUSAND OAKS vs GLENN R PACKARD, et al. CITY OF THOUSAND
    OAKS vs GLENN R PACKARD, et al."`` where the full title is repeated
    2-3 times.

    The algorithm:
      - Tries increasing prefix lengths (>= 15 chars) and checks whether
        the rest of the string consists of one or more repetitions of
        the prefix (ignoring whitespace differences).
      - Returns the shortest prefix for which the rest matches.
      - If no repetition detected, returns the input (stripped of
        leading/trailing whitespace) unchanged.

    Short titles (< 15 chars of meaningful content) are returned unchanged
    to avoid false positives on legitimate short repeated tokens.

    Parameters
    ----------
    title : str | None
        The candidate title to deduplicate.

    Returns
    -------
    str | None
        The deduplicated title.  ``None`` input returns ``None``.  Empty
        strings return empty.
    """
    if title is None:
        return None
    if not title:
        return title

    # Collapse whitespace and strip
    normalized = " ".join(title.split())
    if not normalized:
        return title

    # Too short for dedup to be meaningful
    min_prefix_len = 10
    if len(normalized) < 2 * min_prefix_len:
        return normalized

    total_len = len(normalized)

    # Try each candidate prefix length (shortest to longest).
    # The prefix must end either at the end of the string, or immediately
    # before a space (word boundary).
    for prefix_len in range(min_prefix_len, total_len // 2 + 1):
        # Word boundary check: the char AT position prefix_len must be a
        # space (or we're at EOS).  This ensures we don't cut mid-word.
        if prefix_len < total_len and normalized[prefix_len] != " ":
            continue

        prefix = normalized[:prefix_len].strip()
        if len(prefix) < min_prefix_len:
            continue

        # Check whether the rest of the string is one or more repeats
        # of this prefix (whitespace-tolerant).
        rest = normalized[prefix_len:].lstrip()
        if not rest:
            continue

        # Walk through the rest matching repeated copies of prefix.
        # Each repetition is prefix followed by optional whitespace.
        cursor = 0
        rest_len = len(rest)
        all_match = True
        while cursor < rest_len:
            # The next candidate copy occupies exactly len(prefix) chars
            # (since we already stripped whitespace).
            candidate = rest[cursor : cursor + len(prefix)]
            if candidate != prefix:
                all_match = False
                break
            cursor += len(prefix)
            # Skip any whitespace between copies
            while cursor < rest_len and rest[cursor] == " ":
                cursor += 1
        if all_match and cursor >= rest_len:
            return prefix

    return normalized


# Regex that matches probate caption prefixes used to detect cases where
# the decedent/conservatee name is prominent in the title.
_PROBATE_PARTY_TITLE_RE = re.compile(
    r"(?:"
    r"In\s+(?:the\s+)?(?:Matter|Re)\s+of"
    r"|Estate\s+of"
    r"|Conservatorship\s+of"
    r"|Guardianship\s+of"
    r"|Petition\s+(?:for\s+)?(?:Probate|Letters)"
    # Also match "XYZ Trust" pattern (party name followed by "Trust")
    r"|\bTrust\b"
    r")",
    re.IGNORECASE,
)


def is_probate_decedent_name(
    candidate: str | None,
    case_title: str | None,
) -> bool:
    """Return True if *candidate* looks like the decedent/conservatee name
    from the probate case title, not a real judge name (#2370).

    Ventura probate cases (Dept J6) have captions like "Estate of Delbert
    L. Webb" where the decedent's name is very prominent.  The LLM can
    mistake this for the judge's name.

    This guard checks whether:
    1. The case title looks probate-shaped (contains "Estate of",
       "In the Matter of", "Conservatorship of", "Trust", etc.), AND
    2. The candidate name appears within the case title (case-insensitive
       substring match on normalized tokens).

    When both conditions hold, the candidate is almost certainly the
    decedent/conservatee/trustor name — not the judge.

    Parameters
    ----------
    candidate : str | None
        The candidate judge name (e.g. from LLM extraction).
    case_title : str | None
        The case title for this ruling.

    Returns
    -------
    bool
        True if the candidate is likely a probate party name (and should
        therefore be rejected as a judge name).  False if the candidate
        looks like a real judge name or the title is not probate-shaped.
    """
    if not candidate or not case_title:
        return False

    if not _PROBATE_PARTY_TITLE_RE.search(case_title):
        return False

    # Normalize both for comparison: lowercase, strip periods, collapse
    # whitespace.
    def _normalize(s: str) -> str:
        return re.sub(r"[.,]", "", s).lower().strip()

    norm_candidate = _normalize(candidate)
    norm_title = _normalize(case_title)

    if not norm_candidate or len(norm_candidate) < 3:
        return False

    # Direct substring match
    if norm_candidate in norm_title:
        return True

    # Token-level match — all candidate tokens must appear in the title
    # in order, allowing other tokens between them.
    candidate_tokens = [t for t in norm_candidate.split() if t]
    if len(candidate_tokens) < 2:
        return False

    title_tokens = norm_title.split()
    idx = 0
    for ct in candidate_tokens:
        # Find ct in title_tokens starting at idx
        found = -1
        for j in range(idx, len(title_tokens)):
            if title_tokens[j] == ct:
                found = j
                break
        if found < 0:
            return False
        idx = found + 1
    return True


def is_plausible_hearing_date(
    hearing_date: date | None,
    capture_timestamp: datetime | None,
    *,
    past_days: int = 14,
    future_days: int = 14,
) -> bool:
    """Return True if *hearing_date* is plausible given *capture_timestamp*
    (#2370).

    Tentative rulings are captured on or near the hearing day.  A value
    more than ``past_days`` before capture or ``future_days`` after capture
    is almost certainly a continuance or referenced minute-order date,
    not the actual hearing date.

    Parameters
    ----------
    hearing_date : date | None
        The candidate hearing date.
    capture_timestamp : datetime | None
        The document capture timestamp (when the scraper archived the
        ruling).  When ``None``, plausibility cannot be checked and the
        function returns ``True`` (permissive — don't drop data we can't
        verify).
    past_days : int
        Maximum number of days *before* capture allowed.  Default 14.
    future_days : int
        Maximum number of days *after* capture allowed.  Default 14.
        Tentative rulings are sometimes posted up to a week before the
        hearing; allow a bit of slack.

    Returns
    -------
    bool
        True if plausible, False otherwise.
    """
    if hearing_date is None:
        return False
    if capture_timestamp is None:
        # Can't verify — default to plausible.
        return True
    capture_date = capture_timestamp.date()
    delta = (hearing_date - capture_date).days
    return -past_days <= delta <= future_days
