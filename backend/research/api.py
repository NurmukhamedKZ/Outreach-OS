from fastapi import APIRouter, BackgroundTasks, status

import clock
import paths
from research.dependencies import ModelDep, PagesDep, ResearchServiceDep, run_in_background
from research.schemas import BatchView, CompanyResearch, Lead, ResearchSettings

router = APIRouter(prefix="/api/research", tags=["research"])

ALREADY_RUNNING = {409: {"description": "errors.batchRunning: a batch is already running"}}
NO_KEY = {503: {"description": "errors.modelKeyMissing: OPENROUTER_API_KEY is empty"}}
NOT_FOUND = {404: {"description": "errors.companyNotFound: no such company"}}


@router.post("/batches", status_code=status.HTTP_202_ACCEPTED,
             summary="Start a batch in the background, or resume the interrupted one",
             responses=ALREADY_RUNNING | NO_KEY)
def start_batch(background: BackgroundTasks, service: ResearchServiceDep, pages: PagesDep,
                model: ModelDep) -> BatchView:
    # The state is in the batch tables: losing the task with the process loses
    # nothing, the next start marks the batch interrupted and resumes it.
    batch_id = service.start(clock.now())
    background.add_task(run_in_background, paths.state_db(), pages, model, batch_id, clock.now)
    return service.batch(batch_id)


@router.get("/batches/current", status_code=status.HTTP_200_OK,
            summary="The latest batch with its companies, null before the first")
def current_batch(service: ResearchServiceDep) -> BatchView | None:
    return service.current()


@router.post("/batches/current/stop", status_code=status.HTTP_200_OK,
             summary="Stop the running batch; research already started is finished",
             responses={409: {"description": "errors.batchNotRunning: no batch is running"}})
def stop_batch(service: ResearchServiceDep) -> BatchView:
    return service.stop()


@router.get("/leads", status_code=status.HTTP_200_OK,
            summary="Leads with their dossier, channel and score, the highest first")
def leads(service: ResearchServiceDep) -> list[Lead]:
    return service.leads(clock.now())


@router.get("/companies/{company_id}", status_code=status.HTTP_200_OK,
            summary="What research knows of a company: dossier, score, attempts",
            responses=NOT_FOUND)
def company(company_id: str, service: ResearchServiceDep) -> CompanyResearch:
    return service.company(company_id, clock.now())


@router.post("/companies/{company_id}", status_code=status.HTTP_202_ACCEPTED,
             summary="Research the company again in a batch of one, in the background",
             responses=NOT_FOUND | NO_KEY | {
                 409: {"description": "errors.batchRunning: a batch is already running;"
                                      " errors.companyRefused: the company is refused"}})
def research_again(company_id: str, background: BackgroundTasks, service: ResearchServiceDep,
                   pages: PagesDep, model: ModelDep) -> BatchView:
    batch_id = service.start_one(company_id, clock.now())
    background.add_task(run_in_background, paths.state_db(), pages, model, batch_id, clock.now)
    return service.batch(batch_id)


@router.get("/settings", status_code=status.HTTP_200_OK,
            summary="Batch size, model, signal weights and the half-life of a signal")
def get_settings(service: ResearchServiceDep) -> ResearchSettings:
    return service.settings()


@router.put("/settings", status_code=status.HTTP_200_OK,
            summary="Replace the research settings as a whole")
def save_settings(body: ResearchSettings, service: ResearchServiceDep) -> ResearchSettings:
    return service.save_settings(body)
