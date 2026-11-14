from typing import Annotated

from fastapi import Depends

from config import settings
from core.db import Db
from refusals.exceptions import SandboxRefusalError
from refusals.service import RefusalService


def get_refusal_service(db: Db) -> RefusalService:
    return RefusalService(db)


def outside_the_sandbox() -> None:
    """Refuse a refusal typed by hand while the sandbox is on.

    Raises:
        SandboxRefusalError: SANDBOX=1.
    """
    if settings.sandbox:
        raise SandboxRefusalError(
            "В песочнице отказ не оформляется: он лёг бы в базу прогона, а не в боевую."
            " Настоящий отказ оформляйте с выключенной песочницей"
        )


RefusalServiceDep = Annotated[RefusalService, Depends(get_refusal_service)]
