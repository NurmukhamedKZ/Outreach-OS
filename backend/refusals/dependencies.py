from typing import Annotated

from fastapi import Depends

from core.db import Db
from refusals.service import RefusalService


def get_refusal_service(db: Db) -> RefusalService:
    return RefusalService(db)


RefusalServiceDep = Annotated[RefusalService, Depends(get_refusal_service)]
