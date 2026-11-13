import pytest

from backup import public
from backup.exceptions import BackupDisabledError, BackupFailedError, BackupRunningError


class Failing:
    def __init__(self, error):
        self.error = error
        self.runs = 0

    def run(self, now):
        self.runs += 1
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        BackupDisabledError("нет iCloud Drive"),
        BackupFailedError("не записан"),
        BackupRunningError("уже идёт"),
    ],
)
def test_back_up_takes_a_snapshot_and_swallows_what_the_journal_already_has(error):
    service = Failing(error)

    public.back_up(service)  # type: ignore[arg-type]  # a fake stands in for the service

    assert service.runs == 1


def test_back_up_lets_an_unexpected_error_through():
    with pytest.raises(RuntimeError):
        public.back_up(Failing(RuntimeError("bug")))  # type: ignore[arg-type]  # a fake service
