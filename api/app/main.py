"""Employee PTO API: GET /pto/{employee_id}. Also serves the web front-end."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

from common import db
from common.embeddings import Embedder, openai_client
from common.pii import PiiCipher

from .auth import require_pto_reader
from .config import settings
from .employees import EmployeeRepository
from .pto import load_policy
from .pto_explainer import PtoExplainer
from .pto_service import EmployeeNotFound, PtoService
from .rag import HandbookRetriever
from .schemas import PtoResponse

logging.basicConfig(level=logging.INFO)
WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def _build_pto_service() -> tuple[PtoService, ConnectionPool]:
    pool = ConnectionPool(
        db.conninfo(settings.pg_host, settings.pg_database, settings.pg_user,
                    settings.pg_sslmode, settings.pg_sslrootcert),
        connection_class=db.connection_class(settings.pg_password),
        min_size=1, max_size=10, max_lifetime=1800, open=True,
    )
    client = None
    if settings.azure_openai_endpoint:
        client = openai_client(settings.azure_openai_endpoint, settings.azure_openai_api_version)
    retriever = None
    if client:
        embedder = Embedder(client, settings.embedding_deployment, settings.embedding_dimensions)
        retriever = HandbookRetriever(pool, embedder, settings.rag_top_k, settings.rag_min_score)
    service = PtoService(
        repo=EmployeeRepository(pool, PiiCipher(settings.pii_encryption_key, settings.pii_encryption_key_previous)),
        retriever=retriever,
        explainer=PtoExplainer(client if settings.pto_llm_explanation else None, settings.chat_deployment),
        policy=load_policy(),
        timezone=settings.company_timezone,
        id_pattern=settings.employee_id_pattern,
    )
    return service, pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pto, pool = await run_in_threadpool(_build_pto_service)
    yield
    pool.close()


app = FastAPI(title="Employee PTO API", version="1.0.0", lifespan=lifespan)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.allowed_origins.split(",")],
        allow_methods=["GET"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=True,
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/pto/"):
        response.headers["Cache-Control"] = "no-store"  # employee data: never cache
    return response


def get_pto_service(request: Request) -> PtoService:
    service = getattr(request.app.state, "pto", None)
    if service is None:
        raise HTTPException(503, "PTO lookup is not configured.")
    return service


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/pto/{employee_id}", response_model=PtoResponse, dependencies=[Depends(require_pto_reader)])
def pto(employee_id: str, service: PtoService = Depends(get_pto_service)):
    """Available PTO for one employee, assuming no leave taken. Unknown IDs -> 404 "Employee not found"."""
    try:
        return service.get(employee_id)
    except EmployeeNotFound:
        raise HTTPException(404, "Employee not found")


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
