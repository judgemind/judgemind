"""Test case definitions for validation prompt eval.

Each test case represents an ingested ruling with extracted fields.
Known-bad cases are derived from real production bugs (#1716, #1401).
Known-good cases represent correct ingestions that should pass validation.
Edge cases cover legitimate short/cross-reference rulings that should NOT
be flagged as errors.

Test case categories:
  - known_good: correct ingestions, validator should return "pass"
  - known_bad: production bugs, validator should return "flag" or "fail"
  - edge_case: unusual but correct, validator should return "pass"
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TestCase:
    """A single validation test case."""

    id: str
    category: str  # "known_good", "known_bad", "edge_case"
    description: str
    expected_verdict: str  # "pass", "flag", "fail"
    # Extracted fields (what the validator will see)
    case_number: str | None = None
    case_title: str | None = None
    ruling_text: str | None = None
    motion_type: str | None = None
    judge_name: str | None = None
    hearing_date: str | None = None
    department: str | None = None
    outcome: str | None = None
    county: str | None = None
    # Bug pattern tag for analysis
    bug_pattern: str | None = None


# ---------------------------------------------------------------------------
# Known-good test cases
# ---------------------------------------------------------------------------

KNOWN_GOOD: list[TestCase] = [
    TestCase(
        id="good_la_demurrer",
        category="known_good",
        description="LA County demurrer ruling, typical length and structure",
        expected_verdict="pass",
        case_number="23STCV12345",
        case_title="Smith v. Jones",
        ruling_text=(
            "The demurrer to the first amended complaint is SUSTAINED with "
            "leave to amend as to the first cause of action for negligence. "
            "Defendant argues that plaintiff fails to allege facts sufficient "
            "to state a cause of action. The court agrees. Plaintiff's "
            "allegation that defendant 'acted negligently' is a legal "
            "conclusion, not a factual allegation. However, the defect "
            "appears curable. Plaintiff is granted 20 days leave to amend. "
            "The demurrer is OVERRULED as to the second cause of action for "
            "breach of contract. Defendant argues the contract is void for "
            "lack of consideration. The court disagrees. The complaint "
            "adequately alleges mutual promises constituting valid "
            "consideration. Moving party to give notice."
        ),
        motion_type="demurrer",
        judge_name="David J. Cowan",
        hearing_date="2026-03-15",
        department="31",
        outcome="granted_in_part",
        county="Los Angeles",
    ),
    TestCase(
        id="good_oc_msj",
        category="known_good",
        description="OC summary judgment ruling, multi-paragraph analysis",
        expected_verdict="pass",
        case_number="30-2024-01393434",
        case_title="Garcia v. ABC Corporation",
        ruling_text=(
            "Defendant ABC Corporation's motion for summary judgment is "
            "DENIED. Defendant contends there are no triable issues of "
            "material fact as to plaintiff's claims for wrongful termination "
            "and retaliation under Labor Code section 1102.5. The court "
            "finds triable issues exist.\n\n"
            "As to the wrongful termination claim, defendant presents "
            "evidence that plaintiff was terminated for documented "
            "performance deficiencies. However, plaintiff's declaration "
            "and the deposition testimony of former supervisor Martinez "
            "create a triable issue as to whether the stated reason was "
            "pretextual. Martinez testified that she was instructed to "
            "'build a paper trail' against plaintiff shortly after "
            "plaintiff reported safety violations to OSHA.\n\n"
            "As to the retaliation claim, the temporal proximity between "
            "plaintiff's protected activity (OSHA report on January 15, "
            "2024) and the adverse action (termination on February 28, "
            "2024) is sufficient at summary judgment to raise an inference "
            "of retaliatory motive. See Loggins v. Kaiser Permanente "
            "International (2007) 151 Cal.App.4th 1102, 1110.\n\n"
            "The motion for summary adjudication of the punitive damages "
            "claim is GRANTED. Plaintiff fails to present clear and "
            "convincing evidence of malice, oppression, or fraud by an "
            "officer, director, or managing agent of defendant. The "
            "declaration of plaintiff's attorney presenting hearsay "
            "statements from unnamed employees is inadmissible.\n\n"
            "Moving party to give notice."
        ),
        motion_type="motion for summary judgment",
        judge_name="Randall J. Sherman",
        hearing_date="2026-03-20",
        department="N14",
        outcome="denied",
        county="Orange",
    ),
    TestCase(
        id="good_riverside_compel",
        category="known_good",
        description="Riverside motion to compel, detailed ruling",
        expected_verdict="pass",
        case_number="CVRI2403055",
        case_title="Yeldell v. Henss",
        ruling_text=(
            "Summary of Ruling: The Court grants all three motions to compel "
            "further responses. Defendant Henss is ordered to provide "
            "verified, code-compliant responses to Form Interrogatories "
            "(Set One), Special Interrogatories (Set One), and Request for "
            "Production of Documents (Set One) within 30 days of this "
            "ruling. Monetary sanctions in the amount of $1,560 are imposed "
            "against defendant and defendant's counsel of record, jointly "
            "and severally, payable to plaintiff's counsel within 30 days.\n\n"
            "Discussion: Plaintiff served the discovery requests on "
            "October 15, 2024. Defendant served objection-only responses on "
            "November 20, 2024. The objections consist entirely of "
            "boilerplate language without any substantive basis. Defendant's "
            "opposition argues the requests are overbroad and seek "
            "privileged information but fails to provide a privilege log or "
            "identify any specific privilege. The court finds the objections "
            "meritless under CCP section 2030.300.\n\n"
            "Sanctions are warranted because defendant's failure to provide "
            "substantive responses was not substantially justified. The "
            "court calculates sanctions based on 4 hours of attorney time at "
            "$350/hour plus the $60 filing fee."
        ),
        motion_type="motion to compel",
        judge_name="John Smith",
        hearing_date="2026-03-23",
        department="2",
        outcome="granted",
        county="Riverside",
    ),
    TestCase(
        id="good_sb_motion_to_strike",
        category="known_good",
        description="Santa Barbara motion to strike, standard length",
        expected_verdict="pass",
        case_number="24CV04567",
        case_title="Brown v. Williams",
        ruling_text=(
            "Defendant's motion to strike portions of the complaint is "
            "GRANTED in part and DENIED in part. The motion to strike the "
            "prayer for punitive damages (paragraphs 45-47) is GRANTED with "
            "leave to amend. Plaintiff has not alleged specific facts showing "
            "malice, oppression, or fraud. Conclusory allegations that "
            "defendant 'acted with conscious disregard' are insufficient.\n\n"
            "The motion to strike the prayer for attorney fees (paragraph "
            "48) is DENIED. Plaintiff's cause of action under the Song-"
            "Beverly Act provides a statutory basis for attorney fees.\n\n"
            "Plaintiff may file an amended complaint within 20 days."
        ),
        motion_type="motion to strike",
        judge_name="Thomas P. Anderle",
        hearing_date="2026-03-18",
        department="3",
        outcome="granted_in_part",
        county="Santa Barbara",
    ),
    TestCase(
        id="good_sf_continuance",
        category="known_good",
        description="SF ruling continuing the hearing, brief but legitimate",
        expected_verdict="pass",
        case_number="CGC-25-614320",
        case_title="Pacific Heights HOA v. Tran",
        ruling_text=(
            "Defendant's motion to continue the trial date is GRANTED. "
            "Good cause appearing, the trial currently set for April 15, "
            "2026 is continued to June 10, 2026 at 9:30 a.m. in Department "
            "302. All discovery and motion cutoff dates will follow the "
            "new trial date. The parties are ordered to attend a mandatory "
            "settlement conference on May 20, 2026."
        ),
        motion_type="motion to continue",
        judge_name="Suzanne R. Bolanos",
        hearing_date="2026-03-12",
        department="302",
        outcome="granted",
        county="San Francisco",
    ),
]


# ---------------------------------------------------------------------------
# Known-bad test cases (from production bugs)
# ---------------------------------------------------------------------------

KNOWN_BAD: list[TestCase] = [
    # --- #1716 pattern: wrong ruling text assigned ---
    TestCase(
        id="bad_1716_wrong_text",
        category="known_bad",
        description=(
            "#1716: 6-page ruling for CVRI2403055 replaced by one-liner "
            "from CVRI2506271. Ruling text is for wrong case."
        ),
        expected_verdict="flag",
        case_number="CVRI2403055",
        case_title="Yeldell v. Henss",
        ruling_text=(
            "Motion Granted. No opposition filed. Court order to be signed at hearing."
        ),
        motion_type="motion to compel",
        judge_name="John Smith",
        hearing_date="2026-03-23",
        department="2",
        outcome="granted",
        county="Riverside",
        bug_pattern="wrong_text_assigned",
    ),
    TestCase(
        id="bad_1716_see_above_as_full_ruling",
        category="known_bad",
        description=(
            "#1716 variant: 'See #1 Above' stored as the entire ruling text "
            "for a case that should have substantive content."
        ),
        expected_verdict="flag",
        case_number="CVRI2403055",
        case_title="Yeldell v. Henss",
        ruling_text="See #1 Above",
        motion_type="motion to compel further responses",
        judge_name="John Smith",
        hearing_date="2026-03-23",
        department="2",
        outcome="granted",
        county="Riverside",
        bug_pattern="cross_reference_as_full_ruling",
    ),
    # --- #1401 pattern: motion type in case title ---
    TestCase(
        id="bad_1401_motion_in_title_msj",
        category="known_bad",
        description=(
            "#1401: case_title contains motion type text. "
            "'Rambo v. Ford Motor Motion For Summary' should be "
            "'Rambo v. Ford Motor'."
        ),
        expected_verdict="flag",
        case_number="CVPS2400123",
        case_title="Rambo v. Ford Motor Motion For Summary",
        ruling_text=(
            "Defendant's motion for summary judgment is DENIED. The court "
            "finds triable issues of material fact exist regarding "
            "plaintiff's claims for products liability and negligence. "
            "Defendant's evidence that the vehicle was modified after "
            "purchase does not negate all theories of liability. Plaintiff's "
            "expert declaration establishes a triable issue as to the design "
            "defect claim."
        ),
        motion_type="motion for summary judgment",
        judge_name="Kathleen Jacobs",
        hearing_date="2026-02-15",
        department="PS1",
        outcome="denied",
        county="Riverside",
        bug_pattern="motion_in_title",
    ),
    TestCase(
        id="bad_1401_motion_in_title_paga",
        category="known_bad",
        description=(
            "#1401: case_title contains 'Motion For Approval Of Paga'. "
            "Should be 'Hunt v. Abs Facility'."
        ),
        expected_verdict="flag",
        case_number="CVMV2500456",
        case_title="Hunt v. Abs Facility Motion For Approval Of Paga",
        ruling_text=(
            "The motion for approval of the PAGA settlement is GRANTED. "
            "The court finds the settlement is fair, reasonable, and "
            "adequate. The settlement amount of $75,000, with 75% allocated "
            "to the LWDA and 25% to aggrieved employees, is within the "
            "range approved by California courts. Attorney fees of "
            "$25,000 (one-third of the settlement) are reasonable."
        ),
        motion_type="motion for approval",
        judge_name="Michelle Williams",
        hearing_date="2026-02-20",
        department="MV2",
        outcome="granted",
        county="Riverside",
        bug_pattern="motion_in_title",
    ),
    TestCase(
        id="bad_1401_motion_in_title_dismiss",
        category="known_bad",
        description=(
            "#1401: case_title 'Sanzone v. Dch Korean Motion To Dismiss "
            "For Failure'. Should be 'Sanzone v. Dch Korean'."
        ),
        expected_verdict="flag",
        case_number="CVRI2401789",
        case_title="Sanzone v. Dch Korean Motion To Dismiss For Failure",
        ruling_text=(
            "The motion to dismiss for failure to prosecute is GRANTED. "
            "Plaintiff has not served the summons and complaint within "
            "three years as required by CCP section 583.210. No extensions "
            "or tolling provisions apply. The action is dismissed without "
            "prejudice."
        ),
        motion_type="motion to dismiss",
        judge_name="Mark Singerton",
        hearing_date="2026-01-10",
        department="1",
        outcome="granted",
        county="Riverside",
        bug_pattern="motion_in_title",
    ),
    TestCase(
        id="bad_1401_responses_in_title",
        category="known_bad",
        description=(
            "#1401: case_title 'Malik v. City Of Responses To Special'. "
            "Should be 'Malik v. City Of [something]'."
        ),
        expected_verdict="flag",
        case_number="CVRI2402345",
        case_title="Malik v. City Of Responses To Special",
        ruling_text=(
            "Plaintiff's motion to compel further responses to special "
            "interrogatories is GRANTED. Defendant's objections are "
            "overruled. Defendant shall serve verified responses within "
            "30 days. Sanctions of $1,200 are imposed against defendant."
        ),
        motion_type="motion to compel",
        judge_name="Harold Hopp",
        hearing_date="2026-01-25",
        department="3",
        outcome="granted",
        county="Riverside",
        bug_pattern="motion_in_title",
    ),
    # --- Suspiciously short ruling for complex motion ---
    TestCase(
        id="bad_short_ruling_complex_motion",
        category="known_bad",
        description=(
            "A motion for summary judgment ruling should have substantial "
            "analysis. A one-line ruling suggests text was truncated or "
            "from a different entry."
        ),
        expected_verdict="flag",
        case_number="30-2024-01234567",
        case_title="Anderson v. Pacific Gas & Electric",
        ruling_text="Granted.",
        motion_type="motion for summary judgment",
        judge_name="David Belz",
        hearing_date="2026-03-10",
        department="C11",
        outcome="granted",
        county="Orange",
        bug_pattern="suspiciously_short",
    ),
    # --- Missing ruling text entirely ---
    TestCase(
        id="bad_empty_ruling_text",
        category="known_bad",
        description="Ruling text is empty/null but other fields present.",
        expected_verdict="fail",
        case_number="CVRI2405678",
        case_title="Davis v. Metropolitan Corp",
        ruling_text="",
        motion_type="demurrer",
        judge_name="Craig Riemer",
        hearing_date="2026-03-01",
        department="6",
        outcome="granted",
        county="Riverside",
        bug_pattern="empty_ruling_text",
    ),
]

# ---------------------------------------------------------------------------
# Edge cases (unusual but correct)
# ---------------------------------------------------------------------------

EDGE_CASES: list[TestCase] = [
    TestCase(
        id="edge_withdrawn",
        category="edge_case",
        description="Motion withdrawn — legitimately short ruling text.",
        expected_verdict="pass",
        case_number="CVRI2406848",
        case_title="Lopez v. Valley Industries",
        ruling_text="Motion withdrawn.",
        motion_type="motion to compel",
        judge_name="John Smith",
        hearing_date="2026-03-23",
        department="2",
        outcome="off_calendar",
        county="Riverside",
    ),
    TestCase(
        id="edge_off_calendar",
        category="edge_case",
        description="Hearing taken off calendar — short but correct.",
        expected_verdict="pass",
        case_number="23STCV99876",
        case_title="Nguyen v. First American Title",
        ruling_text=(
            "This matter is ordered off calendar. The parties have "
            "stipulated to continue the hearing to April 20, 2026."
        ),
        motion_type="motion for summary judgment",
        judge_name="Robert Broadbelt",
        hearing_date="2026-03-14",
        department="53",
        outcome="off_calendar",
        county="Los Angeles",
    ),
    TestCase(
        id="edge_see_above_cross_ref",
        category="edge_case",
        description=(
            "Cross-reference 'See #1 Above' is valid when it IS the entire "
            "ruling — for a companion case. The case_title differs from the "
            "referenced case."
        ),
        expected_verdict="pass",
        case_number="CVRI2403056",
        case_title="Henss v. Yeldell",
        ruling_text="See #1 Above.",
        motion_type="motion to compel",
        judge_name="John Smith",
        hearing_date="2026-03-23",
        department="2",
        outcome="other",
        county="Riverside",
    ),
    TestCase(
        id="edge_multi_motion",
        category="edge_case",
        description="Multiple motions heard together — longer ruling text.",
        expected_verdict="pass",
        case_number="CVMV2500789",
        case_title="Rodriguez v. Sunrise Senior Living",
        ruling_text=(
            "1. Plaintiff's motion to compel further responses to form "
            "interrogatories is GRANTED. Defendant shall provide verified "
            "responses within 20 days.\n\n"
            "2. Plaintiff's motion to compel further responses to request "
            "for production of documents is GRANTED in part. Defendant "
            "shall produce responsive documents for categories 1-8 and "
            "12-15. The motion is denied as to categories 9-11 as these "
            "seek privileged attorney-client communications.\n\n"
            "3. Plaintiff's motion to deem requests for admission admitted "
            "is DENIED. Although responses were late, defendant served "
            "substantially compliant responses before the hearing.\n\n"
            "Sanctions of $2,100 are imposed against defendant's counsel, "
            "payable within 30 days."
        ),
        motion_type="motion to compel",
        judge_name="Michelle Williams",
        hearing_date="2026-03-20",
        department="MV2",
        outcome="granted_in_part",
        county="Riverside",
    ),
    TestCase(
        id="edge_no_tentative",
        category="edge_case",
        description=(
            "Court has no tentative ruling — hearing will be conducted. "
            "This is a valid 'other' outcome."
        ),
        expected_verdict="pass",
        case_number="CVPS2401234",
        case_title="Chen v. Desert Regional Medical Center",
        ruling_text=(
            "No tentative ruling. The matter will be heard as scheduled. "
            "Parties should be prepared to argue."
        ),
        motion_type="motion for summary judgment",
        judge_name="Kathleen Jacobs",
        hearing_date="2026-03-22",
        department="PS1",
        outcome="other",
        county="Riverside",
    ),
]


def get_all_test_cases() -> list[TestCase]:
    """Return all test cases in order: known_good, known_bad, edge_case."""
    return KNOWN_GOOD + KNOWN_BAD + EDGE_CASES


def get_test_cases_by_category(category: str) -> list[TestCase]:
    """Return test cases for a specific category."""
    mapping = {
        "known_good": KNOWN_GOOD,
        "known_bad": KNOWN_BAD,
        "edge_case": EDGE_CASES,
    }
    return mapping.get(category, [])
