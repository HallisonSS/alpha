"""Parser tolerante e estrito da saida do LLM (JSON de acoes)."""
from __future__ import annotations

import json
import re

from .models import ACTION_TYPES, Action

MAX_ACTIONS_PER_STEP = 8


class ActionParseError(ValueError):
    pass


def extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ActionParseError("resposta sem objeto JSON")
    try:
        d = json.loads(t[start:end + 1])
    except ValueError as e:
        raise ActionParseError(f"JSON invalido: {e}") from e
    if not isinstance(d, dict):
        raise ActionParseError("JSON raiz deve ser objeto")
    return d


def parse_actions(text: str) -> list[Action]:
    d = extract_json(text)
    raw = d.get("actions")
    if not isinstance(raw, list) or not raw:
        raise ActionParseError("campo 'actions' deve ser lista nao vazia")
    if len(raw) > MAX_ACTIONS_PER_STEP:
        raise ActionParseError(f"maximo de {MAX_ACTIONS_PER_STEP} acoes por passo")
    out: list[Action] = []
    for i, a in enumerate(raw):
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            raise ActionParseError(f"actions[{i}].type invalido; use {list(ACTION_TYPES)}")
        t = a["type"]
        act = Action(type=t)
        if t in ("write_file", "delete_file", "read_file", "list_files", "propose_contract_change"):
            if not isinstance(a.get("path"), str) or not a["path"]:
                raise ActionParseError(f"actions[{i}] ({t}) exige 'path'")
            act.path = a["path"]
        if t in ("write_file", "propose_contract_change"):
            if not isinstance(a.get("content"), str):
                raise ActionParseError(f"actions[{i}] ({t}) exige 'content' string")
            act.content = a["content"]
        if t == "propose_contract_change":
            act.reason = str(a.get("reason", ""))[:500]
        if t == "run":
            argv = a.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
                raise ActionParseError(f"actions[{i}] (run) exige 'argv' como lista de strings (sem shell)")
            act.argv = argv
        if t == "propose_lesson":
            act.text = str(a.get("text", ""))[:300]
            if not act.text:
                raise ActionParseError(f"actions[{i}] (propose_lesson) exige 'text'")
        if t == "done":
            act.summary = str(a.get("summary", ""))[:1000]
        out.append(act)
    return out
