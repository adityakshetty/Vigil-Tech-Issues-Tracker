from vigil_tracker.classifier import IssueClassifier, VerdictDetector
from vigil_tracker.models import IssueCategory, Verdict

NO_ISSUE_PHRASES = ["no issue", "not stuck", "working as expected"]


class FakeLLM:
    """Deterministic stand-in for the Anthropic client."""

    def __init__(self, verdict="issue", match=True, confidence=0.9):
        self.verdict = verdict
        self.match = match
        self.confidence = confidence

    def complete_json(self, system, user, schema):
        props = schema["properties"]
        if "verdict" in props:
            return {"verdict": self.verdict, "diagnosis_summary": "summary"}
        if self.match:
            return {
                "matched_existing": True,
                "matched_id": "stuck_autoqc",
                "confidence": self.confidence,
                "diagnosis_summary": "stuck autoqc",
            }
        return {
            "matched_existing": False,
            "confidence": self.confidence,
            "new_name": "Brand New Issue",
            "new_description": "A new kind of problem.",
            "diagnosis_summary": "new issue",
        }


def test_no_issue_phrase_short_circuits_without_llm():
    det = VerdictDetector(NO_ISSUE_PHRASES, llm=None)
    verdict, _ = det.detect("user text", "The task is not stuck, all good")
    assert verdict == Verdict.NO_ISSUE


def test_phrase_detection_is_case_insensitive():
    det = VerdictDetector(NO_ISSUE_PHRASES)
    assert det.matches_no_issue_phrase("There is NO ISSUE here") is True
    assert det.matches_no_issue_phrase("AutoQC is stuck") is False


def test_llm_used_when_no_phrase_match():
    det = VerdictDetector(NO_ISSUE_PHRASES, llm=FakeLLM(verdict="ambiguous"))
    verdict, summary = det.detect("user", "hmm, hard to say")
    assert verdict == Verdict.AMBIGUOUS
    assert summary == "summary"


def test_heuristic_fallback_without_llm():
    det = VerdictDetector(NO_ISSUE_PHRASES, llm=None)
    # Affirmative hint -> issue.
    assert det.detect("u", "the run is stuck")[0] == Verdict.ISSUE
    # Nothing recognizable -> ambiguous.
    assert det.detect("u", "thanks for the ping")[0] == Verdict.AMBIGUOUS


def registry():
    return [IssueCategory("stuck_autoqc", "Stuck AutoQC", "desc")]


def test_classify_matches_existing_above_threshold():
    clf = IssueClassifier(FakeLLM(match=True, confidence=0.9), 0.75)
    result = clf.classify("AutoQC stuck", "url", "stuck autoqc", registry())
    assert result.is_new is False
    assert result.category.id == "stuck_autoqc"


def test_low_confidence_creates_new_category():
    # Matched id returned, but confidence below threshold -> treat as new.
    clf = IssueClassifier(FakeLLM(match=True, confidence=0.4), 0.75)
    result = clf.classify("AutoQC stuck", "url", "stuck autoqc", registry())
    assert result.is_new is True
    assert result.category.id != "stuck_autoqc"


def test_new_category_gets_stable_id():
    clf = IssueClassifier(FakeLLM(match=False, confidence=0.9), 0.75)
    result = clf.classify("weird new thing", "url", "new issue", registry())
    assert result.is_new is True
    assert result.category.id == "brand_new_issue"
    assert result.category.name == "Brand New Issue"
