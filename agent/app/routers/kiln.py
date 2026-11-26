
"""API do KILN: /kiln/runs, /kiln/approvals, /kiln/lessons.

Registrar no app/main.py:
    from app.routers import kiln as kiln_router
    app.include_router(kiln_router.router)

Autenticacao: cabecalho `X-Kiln-Token` (= KILN_ADMIN_TOKEN) e `X-Kiln-User` (quem decidiu).
Troque `require_admin` pelo RBAC do auth.py quando quiser (a assinatura e so uma Depends).
"""
from __future__ import annotations

import datetime
import decimal
import hmac
import uuid
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.kiln.events import EventBus
from app.kiln.models import ApprovalKind, RunStatus
from app.kiln.settings import Settings
from app.kiln.store import Store

router = APIRouter(prefix="/kiln", tags=["kiln"])


@lru_cache
def _settings() -> Settings:
    return Settings.from_env()


@lru_cache
def _store() -> Store:
    return Store(_settings())


@lru_cache
def _bus() -> EventBus:
    return EventBus(_settings())


def require_admin(x_kiln_token: str = Header(default=""), x_kiln_user: str = Header(default="admin")) -> str:
    expected = _settings().admin_token
    if not expected:
        raise HTTPException(503, "KILN_ADMIN_TOKEN nao configurado")
    if not hmac.compare_digest(x_kiln_token.encode(), expected.encode()):
        raise HTTPException(401, "token invalido")
    return x_kiln_user[:64] or "admin"


class BudgetIn(BaseModel):
    tokens: int | None = Field(None, ge=1000, le=50_000_000)
    usd: float | None = Field(None, ge=0, le=1000)
    seconds: int | None = Field(None, ge=60, le=86_400)
    iterations: int | None = Field(None, ge=1, le=200)
    commands: int | None = Field(None, ge=1, le=1000)


class RunIn(BaseModel):
    goal: str = Field(..., min_length=20, max_length=8000)
    budget: BudgetIn | None = None


class DecisionIn(BaseModel):
    decision: Literal["approved", "denied"]
    note: str = Field("", max_length=1000)


def _plain(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def _pub(row: dict | None) -> dict:
    if row is None:
        raise HTTPException(404, "nao encontrado")
    return {k: _plain(v) for k, v in row.items()}


@router.post("/runs", status_code=201)
def create_run(body: RunIn, user: str = Depends(require_admin)):
    budget = {k: v for k, v in (body.budget.model_dump() if body.budget else {}).items() if v is not None}
    run = _store().create_run(body.goal.strip(), budget, {}, user)
    _store().add_event(str(run["id"]), user, "run.created", {"goal": body.goal[:500]})
    _bus().publish("run.created", str(run["id"]))
    return _pub(run)


@router.get("/runs")
def list_runs(limit: int = Query(50, ge=1, le=200), _: str = Depends(require_admin)):
    return [_pub(r) for r in _store().list_runs(limit)]


@router.get("/runs/{run_id}")
def get_run(run_id: str, _: str = Depends(require_admin)):
    run = _store().get_run(run_id)
    if run is None:
        raise HTTPException(404, "run nao encontrada")
    tasks = _store().get_tasks(run_id)
    return {"run": _pub(run), "tasks": [_pub(t) for t in tasks], "usage": _store().usage_totals(run_id),
            "pending_approvals": [_pub(a) for a in _store().pending_approvals(run_id)]}


@router.get("/runs/{run_id}/events")
def run_events(run_id: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000),
               _: str = Depends(require_admin)):
    return [{**e, "run_id": str(e["run_id"]), "task_id": str(e["task_id"]) if e["task_id"] else None,
             "ts": e["ts"].isoformat()} for e in _store().list_events(run_id, after, limit)]


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, user: str = Depends(require_admin)):
    st = _store()
    run = st.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run nao encontrada")
    if run["status"] in RunStatus.TERMINAL:
        raise HTTPException(409, f"run ja esta {run['status']}")
    open_states = [RunStatus.CREATED, RunStatus.PLANNING, RunStatus.AWAITING_PLAN, RunStatus.STARTING,
                   RunStatus.RUNNING, RunStatus.INTEGRATING]
    if not st.transition_run(run_id, open_states, RunStatus.CANCELLED, error=f"cancelada por {user}"):
        raise HTTPException(409, "run mudou de estado; tente novamente")
    st.add_event(run_id, user, "run.cancelled", {})
    _bus().publish("run.cancelled", run_id)
    return {"ok": True}


@router.get("/approvals")
def list_approvals(status: Literal["pending", "approved", "denied"] | None = "pending",
                   run_id: str | None = None, _: str = Depends(require_admin)):
    return [_pub(a) for a in _store().list_approvals(status, run_id)]


@router.post("/approvals/{approval_id}/decide")
def decide_approval(approval_id: str, body: DecisionIn, user: str = Depends(require_admin)):
    st = _store()
    ap = st.decide_approval(approval_id, body.decision == "approved", user, body.note)
    if ap is None:
        raise HTTPException(409, "aprovacao inexistente ou ja decidida")
    st.add_event(str(ap["run_id"]), user, f"approval.{body.decision}",
                 {"approval": str(ap["id"]), "kind": ap["kind"], "note": body.note})
    _bus().publish("approval.decided", str(ap["run_id"]), approval_id=str(ap["id"]))
    return _pub(ap)


@router.get("/lessons")
def list_lessons(status: Literal["proposed", "approved", "rejected"] | None = "proposed",
                 _: str = Depends(require_admin)):
    return [_pub(x) for x in _store().list_lessons(status)]


@router.post("/lessons/{lesson_id}/decide")
def decide_lesson(lesson_id: str, body: DecisionIn, user: str = Depends(require_admin)):
    row = _store().decide_lesson(lesson_id, body.decision == "approved", user)
    if row is None:
        raise HTTPException(409, "licao inexistente ou ja decidida")
    return _pub(row)


@router.get("/health")
def health():
    return {"status": "ok", "kinds": [ApprovalKind.PLAN, ApprovalKind.CONTRACT_CHANGE, ApprovalKind.CONFIG_CHANGE]}
