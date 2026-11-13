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
    ],
)
def test_the_diagnosis_names_the_variable_to_fix_only_on_enough_replies(funnel, diagnosis):
    assert domain.diagnose(funnel) == diagnosis


def test_a_daemon_nobody_knows_is_silent_after_three_minutes():
    assert domain.silent_after("sender.tick") == 60
    assert domain.silent_after("someone") == 180
