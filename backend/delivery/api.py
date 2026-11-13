from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

import clock
from delivery.dependencies import DeliveryServiceDep, NumberDep, signed
from delivery.schemas import (
    Author,
    DeliverySettings,
    NewNumber,
    NewNumberStatus,
    NumberCard,
    PairingCode,
    PairingQr,
    QueueRequest,
    QueueRow,
    QueueView,
    SenderStatus,
    WebhookAnswer,
    WebhookEvent,
)

router = APIRouter(prefix="/api/sender", tags=["delivery"])

NO_NUMBER: dict[int | str, dict[str, Any]] = {
    404: {"description": "errors.numberNotFound: the number is not in the pool"},
    422: {"description": "errors.phoneInvalid: not a phone"},
}
NODE: dict[int | str, dict[str, Any]] = {
    500: {"description": "Node did not answer"},
}
NO_ROW: dict[int | str, dict[str, Any]] = {
    404: {"description": "errors.rowNotFound: no such row"},
    409: {"description": "errors.rowNotWaiting: the row is not waiting any more"},
}


@router.get(
    "", status_code=status.HTTP_200_OK, summary="The pool with its sockets, the queue, the warmup"
)
def sender_status(service: DeliveryServiceDep) -> SenderStatus:
    return service.status(clock.now())


@router.get("/settings", status_code=status.HTTP_200_OK, summary="The settings of delivery")
def get_settings(service: DeliveryServiceDep) -> DeliverySettings:
    return service.settings()


@router.put(
    "/settings",
    status_code=status.HTTP_200_OK,
    summary="Replace the settings of delivery as a whole; the next tick reads them",
)
def save_settings(body: DeliverySettings, service: DeliveryServiceDep) -> DeliverySettings:
    return service.save_settings(body)


@router.post(
    "/numbers",
    status_code=status.HTTP_201_CREATED,
    summary="Add our number to the pool",
    responses={409: {"description": "errors.numberExists: the number is in the pool"}},
)
def register(body: NewNumber, service: DeliveryServiceDep) -> NumberCard:
    return service.register(body.number, body.skip_warmup, clock.now())


@router.post(
    "/numbers/{number}/pair",
    status_code=status.HTTP_200_OK,
    summary="A pairing code to link the number; an unlinked session is put aside first",
    responses=NO_NUMBER | NODE,
)
def pair(number: NumberDep, service: DeliveryServiceDep) -> PairingCode:
    return PairingCode(code=service.pair(number))


@router.post(
    "/numbers/{number}/qr",
    status_code=status.HTTP_200_OK,
    summary="A QR to link the number; an unlinked session is put aside first",
    responses=NO_NUMBER | NODE,
)
def qr(number: NumberDep, service: DeliveryServiceDep) -> PairingQr:
    return PairingQr(qr=service.qr(number))


@router.post(
    "/numbers/{number}/warmed",
    status_code=status.HTTP_200_OK,
    summary="Mark a number warmed outside our system: active at once",
    responses=NO_NUMBER,
)
def warmed(number: NumberDep, service: DeliveryServiceDep) -> NumberCard:
    return service.mark_warmed(number, clock.now())


@router.post(
    "/numbers/{number}/release",
    status_code=status.HTTP_200_OK,
    summary="Lift the quarantine: active or warming, as the warmup calendar says today",
    responses=NO_NUMBER
    | {409: {"description": "errors.numberNotQuarantined: the number is not in quarantine"}},
)
def release(number: NumberDep, service: DeliveryServiceDep) -> NumberCard:
    return service.release(number, clock.now())


@router.post(
    "/numbers/{number}/status",
    status_code=status.HTTP_200_OK,
    summary="Set the number's status by hand; a ban moves its threads",
    responses=NO_NUMBER,
)
def set_number_status(
    number: NumberDep, body: NewNumberStatus, service: DeliveryServiceDep
) -> NumberCard:
    return service.set_number_status(number, body.status, body.note, clock.now())


@router.post(
    "/queue",
    status_code=status.HTTP_201_CREATED,
    summary="Queue the thread's draft as the operator, in any mode but off",
    responses={
        404: {"description": "errors.threadNotFound: the company has no thread"},
        409: {
            "description": "errors.threadClosed | errors.nothingToQueue | errors.alreadyQueued"
            " | errors.sessionLoggedOut"
        },
        422: {"description": "errors.notReachable | errors.emptyText"},
        503: {"description": "errors.noNumberAvailable: no number has room today"},
    },
)
def enqueue(body: QueueRequest, service: DeliveryServiceDep) -> QueueRow:
    outbox_id = service.enqueue(body.thread_id, clock.now(), Author.OPERATOR, text=body.text)
    return service.queue_row(outbox_id)


@router.get(
    "/queue",
    status_code=status.HTTP_200_OK,
    summary="Every waiting row and the latest rows, of one thread or of all",
)
def show_queue(service: DeliveryServiceDep, thread_id: str | None = None) -> QueueView:
    return service.queue(thread_id.strip() if thread_id else None)


@router.post(
    "/queue/{outbox_id}/confirm",
    status_code=status.HTTP_200_OK,
    summary="Let a waiting row of the automaton go as the operator's",
    responses=NO_ROW,
)
def confirm(outbox_id: int, service: DeliveryServiceDep) -> QueueRow:
    return service.confirm(outbox_id, clock.now())


@router.post(
    "/queue/{outbox_id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Cancel a waiting row; its draft stays a draft",
    responses=NO_ROW,
)
def cancel(outbox_id: int, service: DeliveryServiceDep) -> QueueRow:
    return service.cancel(outbox_id, clock.now())


@router.post(
    "/webhook",
    status_code=status.HTTP_200_OK,
    summary="An event of Node: a delivery receipt or the lead's reply",
    responses={401: {"description": "errors.webhookNotSigned: X-Sender-Secret is wrong"}},
)
def webhook(
    event: WebhookEvent,
    service: DeliveryServiceDep,
    _signed: Annotated[None, Depends(signed)],
) -> WebhookAnswer:
    # 200 even for what is not handled: Node retries anything else forever.
    now = clock.now()
    if event.kind == "incoming":
        return WebhookAnswer(
            handled=service.record_incoming(event.sender, event.text, event.provider_id, now)
        )
    if event.kind == "status" and event.provider_id:
        return WebhookAnswer(handled=service.record_status(event.provider_id, event.status, now))
    return WebhookAnswer(handled=False)
