"""HTTP API + background worker. Run:  uvicorn app.main:app --reload"""
import contextlib
import logging
import os
import threading

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import clock, config, db, samples, security, states, workflow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("api")
STATIC = config.ROOT / "app" / "static"


def _worker(stop: threading.Event):
    while not stop.is_set():
        try:
            workflow.tick()
        except Exception:
            log.exception("worker tick failed")
        stop.wait(0.5)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    stop = threading.Event()
    if os.environ.get("DISABLE_WORKER") != "1":
        threading.Thread(target=_worker, args=(stop,), daemon=True, name="worker").start()
    yield
    stop.set()


app = FastAPI(title="Autonomous Merchant Onboarding", version="0.1.0", lifespan=lifespan)


def operator(x_operator_key: str = Header("")):
    if x_operator_key != config.OPERATOR_API_KEY:
        raise HTTPException(401, "operator key required (X-Operator-Key)")
    return "operator"


@app.exception_handler(workflow.NotFound)
async def _nf(_, e): return JSONResponse({"detail": str(e)}, 404)
@app.exception_handler(workflow.Conflict)
async def _cf(_, e): return JSONResponse({"detail": str(e)}, 409)
@app.exception_handler(states.IllegalTransition)
async def _it(_, e): return JSONResponse({"detail": str(e)}, 409)
@app.exception_handler(workflow.BadRequest)
async def _br(_, e): return JSONResponse({"detail": str(e)}, 422)


# ---------------- merchant-facing -------------------------------------------
@app.post("/applications", status_code=201, summary="Submit an application with documents")
async def submit(payload: dict = Body(...)):
    app_id = await run_in_threadpool(workflow.submit_application, payload)
    return await run_in_threadpool(workflow.applicant_status, app_id)


@app.patch("/applications/{app_id}", summary="Provide missing info / corrected data / new documents")
async def update(app_id: str, payload: dict = Body(...)):
    docs = payload.pop("documents", [])
    await run_in_threadpool(workflow.resubmit, app_id, payload, docs)
    return await run_in_threadpool(workflow.applicant_status, app_id)


@app.get("/applications/{app_id}/status", summary="Applicant-safe status")
def status(app_id: str):
    return workflow.applicant_status(app_id)


# ---------------- provider webhooks ----------------------------------------
@app.post("/callbacks/{provider}", summary="Asynchronous verification result (HMAC-signed)")
async def callback(provider: str, request: Request, x_signature: str = Header("")):
    raw = await request.body()
    try:
        return await run_in_threadpool(workflow.handle_callback, provider, raw, x_signature)
    except workflow.BadSignature:
        raise HTTPException(401, "invalid signature")


# ---------------- operator view ---------------------------------------------
@app.get("/applications", dependencies=[Depends(operator)])
def list_apps(): return workflow.list_applications()


@app.get("/applications/{app_id}", dependencies=[Depends(operator)])
def detail(app_id: str): return workflow.application_view(app_id)


@app.post("/applications/{app_id}/reject", dependencies=[Depends(operator)])
def reject(app_id: str, body: dict = Body(default={})):
    workflow.reject_application(app_id, "operator", body.get("notes", ""))
    return workflow.applicant_status(app_id)


@app.get("/review-queue", dependencies=[Depends(operator)])
def queue(): return workflow.review_queue()


@app.post("/review-queue/{task_id}/resolve", dependencies=[Depends(operator)])
def resolve(task_id: str, body: dict = Body(...)):
    return workflow.resolve_review(task_id, body.get("decision", ""), body.get("reviewer", "operator"), body.get("notes", ""))


@app.get("/dead-letters", dependencies=[Depends(operator)])
def dlq(): return workflow.dead_letters()


@app.post("/dead-letters/{dl_id}/replay", dependencies=[Depends(operator)])
def replay(dl_id: int): return workflow.replay_dead_letter(dl_id)


@app.get("/metrics", dependencies=[Depends(operator)])
def metrics(): return workflow.metrics()


# ---------------- demo / simulation controls -------------------------------------
@app.get("/admin/samples", dependencies=[Depends(operator)])
def sample_list(): return samples.SCENARIOS


@app.post("/admin/samples/{name}", dependencies=[Depends(operator)])
def sample_submit(name: str):
    if name not in samples.SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    base = None
    if name == "duplicate_application":
        last = db.one("SELECT id FROM applications ORDER BY created_at DESC LIMIT 1")
        if not last:
            raise HTTPException(409, "submit another application first")
        base = workflow.get_app(last["id"])["form"]
    return workflow.applicant_status(workflow.submit_application(samples.build(name, base_form=base)))


@app.post("/admin/tick", dependencies=[Depends(operator)])
def tick(): return workflow.tick()


@app.post("/admin/clock/advance", dependencies=[Depends(operator)])
def advance_clock(seconds: float = 30):
    clock.advance(seconds)
    return {"advanced_s": seconds, "tick": workflow.tick()}


@app.get("/", include_in_schema=False)
def index(): return FileResponse(STATIC / "index.html")
