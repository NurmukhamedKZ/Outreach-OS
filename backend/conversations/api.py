from typing import Any

from fastapi import APIRouter, BackgroundTasks, status

import clock
import paths
from conversations.dependencies import ConversationServiceDep, ModelDep, run_in_background
from conversations.schemas import (
    ColdDrafts,
    ConversationSettings,
    Inbox,
    IncomingRequest,
    MoveRequest,
    OutcomeRequest,
    OutcomeView,
    OutreachRun,
    PromptView,
    ThreadView,
)

router = APIRouter(prefix="/api/threads", tags=["conversations"])

NO_KEY: dict[int | str, dict[str, Any]] = {
    503: {"description": "errors.modelKeyMissing: OPENROUTER_API_KEY is empty"}
}
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "errors.companyNotFound: research does not know the company"}
}

# Fixed paths go before /{company_id}: FastAPI matches in the order of declaration.


@router.get("", status_code=status.HTTP_200_OK, summary="Every thread, the most urgent first")
def inbox(service: ConversationServiceDep) -> Inbox:
    return service.inbox(clock.now())


@router.get(
    "/drafts",
    status_code=status.HTTP_200_OK,
    summary="First touches waiting for review, the strongest lead first",
)
def cold_drafts(service: ConversationServiceDep) -> ColdDrafts:
    return service.cold_drafts(clock.now())


@router.get(
    "/settings",
    status_code=status.HTTP_200_OK,
    summary="Model, run size, signals worth writing about, the offer and its variants",
)
def get_settings(service: ConversationServiceDep) -> ConversationSettings:
    return service.settings()


@router.put(
    "/settings",
    status_code=status.HTTP_200_OK,
    summary="Replace the conversation settings as a whole",
)
def save_settings(
    body: ConversationSettings, service: ConversationServiceDep
) -> ConversationSettings:
    return service.save_settings(body)


@router.post(
    "/outreach",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start «Черновики топ-N» in the background: first drafts to new leads",
    responses=NO_KEY | {409: {"description": "errors.outreachRunning: a run is going"}},
)
def start_outreach(
    background: BackgroundTasks, service: ConversationServiceDep, model: ModelDep
) -> OutreachRun:
    # The state is in outreach_runs: losing the task with the process loses one
    # run, the next start marks it failed and the next run takes the rest.
    run_id = service.start_outreach(clock.now())
    background.add_task(
        run_in_background, paths.state_db(), paths.PRODUCTION_STATE, model, run_id, clock.now
    )
    return service.outreach(run_id)


@router.get(
    "/outreach/current",
    status_code=status.HTTP_200_OK,
    summary="The latest «Черновики топ-N» run, null before the first",
)
def current_outreach(service: ConversationServiceDep) -> OutreachRun | None:
    return service.current_outreach()


@router.post(
    "/outreach/current/stop",
    status_code=status.HTTP_200_OK,
    summary="Stop the run; the lead in progress is finished",
    responses={409: {"description": "errors.outreachNotRunning: no run is going"}},
)
def stop_outreach(service: ConversationServiceDep) -> OutreachRun:
    return service.stop_outreach()


@router.get(
    "/{company_id}",
    status_code=status.HTTP_200_OK,
    summary="The company's thread, or the empty one its first draft would open",
    responses=NOT_FOUND | {409: {"description": "errors.noChannel: nowhere to write"}},
)
def thread(company_id: str, service: ConversationServiceDep) -> ThreadView:
    return service.view(company_id, clock.now())


@router.post(
    "/{company_id}/draft",
    status_code=status.HTTP_200_OK,
    summary="Draft the next message with the model; the first draft opens the thread",
    responses=NOT_FOUND
    | NO_KEY
    | {
        409: {
            "description": "errors.companyRefused, errors.noChannel, errors.notALead,"
            " errors.channelTaken, errors.noThread: no move;"
            " errors.agentClosed: the seller handed it on"
        },
        502: {"description": "errors.modelFailed: the model gave no valid answer"},
    },
)
def draft(
    company_id: str, body: MoveRequest, service: ConversationServiceDep, model: ModelDep
) -> ThreadView:
    return service.move(company_id, body.kind, model, clock.now())


@router.post(
    "/{company_id}/incoming",
    status_code=status.HTTP_200_OK,
    summary="Record a reply of the lead typed in by the operator",
    responses={409: {"description": "errors.noThread: nothing was drafted yet"}},
)
def add_incoming(
    company_id: str, body: IncomingRequest, service: ConversationServiceDep
) -> ThreadView:
    return service.add_incoming(company_id, body.text, clock.now())


@router.post(
    "/{company_id}/outcome",
    status_code=status.HTTP_200_OK,
    summary="Record how the thread ended; a meeting is set by a human only",
    responses={404: {"description": "errors.threadNotFound: the company has no thread"}},
)
def set_outcome(
    company_id: str, body: OutcomeRequest, service: ConversationServiceDep
) -> OutcomeView:
    return service.set_outcome(company_id, body.outcome, clock.now())


@router.get(
    "/{company_id}/messages/{message_id}/prompt",
    status_code=status.HTTP_200_OK,
    summary="The full request a draft was written from; apart, it weighs 2-4 KB",
    responses={404: {"description": "errors.messageNotFound: no such message in the thread"}},
)
def prompt(company_id: str, message_id: int, service: ConversationServiceDep) -> PromptView:
    return service.prompt(company_id, message_id)
