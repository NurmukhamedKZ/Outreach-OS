import pytest

from overview import domain
from overview.schemas import Step


def steps(sent, replied, held=0):
    counts = dict.fromkeys(domain.STEPS, 0) | {
        "sent": sent,
        "replied": replied,
        "meeting_held": held,
    }
    return [Step(step=step, count=count) for step, count in counts.items()]


@pytest.mark.parametrize(
    ("funnel", "diagnosis"),
    [
        (steps(0, 0), "ok"),
        (steps(100, 1), "reply_rate_low"),
        (steps(100, 2), "ok"),
        # A diagnosis on a few replies is noise the operator would learn to ignore.
        (steps(20, 9), "ok"),
        (steps(20, 10), "icp_mismatch"),
        (steps(20, 10, held=1), "ok"),
        # Too few replies for the volume is the text, even with enough replies for the ICP.
        (steps(1000, 10), "reply_rate_low"),
    ],
)
def test_the_diagnosis_names_the_variable_to_fix_only_on_enough_replies(funnel, diagnosis):
    assert domain.diagnose(funnel) == diagnosis


def test_a_daemon_nobody_knows_is_silent_after_three_minutes():
    assert domain.silent_after("sender.tick") == 60
    assert domain.silent_after("someone") == 180


def test_tasks_put_a_waiting_lead_first_then_the_backup_then_drafts_and_cut_at_the_limit():
    escalated = [
        {"company_id": "a", "channel": "+1", "status_reason": None, "our_number": None, "at": None}
    ]
    stuck = [{"company_id": "b", "channel": "+2", "our_number": "+9", "at": "t"}]
    drafts = [{"company_id": "c", "channel": "+3", "created_at": "t"}]

    found = domain.tasks(escalated, stuck, drafts, "бэкапа нет", {"b": "Бета"}, limit=10)

    assert [(t.kind, t.company_name, t.why) for t in found] == [
        ("escalated", "a", domain.NO_REASON),
        ("stuck", "Бета", domain.STUCK_WHY),
        ("backup", domain.BACKUP_NAME, "бэкапа нет"),
        ("draft", "c", domain.DRAFT_WHY),
    ]
    assert [t.kind for t in domain.tasks(escalated, stuck, drafts, None, {}, limit=2)] == [
        "escalated",
        "stuck",
    ]
