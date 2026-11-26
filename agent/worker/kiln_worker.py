"""Worker do KILN: consome o stream, avanca as runs e executa jobs (agentes, verificacao).

Executar:  python -m worker.kiln_worker      (WORKDIR = pasta que contem app/ e worker/)
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kiln import observability as obs  # noqa: E402
from app.kiln.events import EventBus  # noqa: E402
from app.kiln.llm import OpenAICompatLLM  # noqa: E402
from app.kiln.orchestrator import Orchestrator  # noqa: E402
from app.kiln.sandbox import DockerSandbox  # noqa: E402
from app.kiln.settings import Settings  # noqa: E402
from app.kiln.store import Store  # noqa: E402

log = logging.getLogger("kiln.worker")
_stop = False


def _on_signal(signum, _frame):
    global _stop
    log.info("sinal %s: encerrando apos o ciclo atual", signum)
    _stop = True


def main() -> int:
    obs.setup_logging(os.environ.get("KILN_LOG_LEVEL", "INFO"))
    s = Settings.from_env()
    s.require_runtime()
    obs.start_metrics_server(s.metrics_port)
    store = Store(s)
    if s.auto_migrate and os.path.exists(s.schema_path):
        store.apply_schema(s.schema_path)
        log.info("schema kiln aplicado de %s", s.schema_path)
    bus = EventBus(s)
    bus.ensure_group()
    sandbox = DockerSandbox(s)
    sandbox.prepare()
    orch = Orchestrator(s, store, bus, OpenAICompatLLM(s), sandbox)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    log.info("kiln-worker %s pronto (parallel=%d, modelo=%s)", s.worker_id, s.max_parallel, s.llm_model)

    last_tick = 0.0
    reclaimed_at = 0.0
    while not _stop:
        try:
            now = time.time()
            if now - reclaimed_at > 60:
                reclaimed_at = now
                for mid, fields in bus.reclaim(s.worker_id):
                    _process(orch, bus, mid, fields)
            for mid, fields in bus.consume(s.worker_id, count=10, block_ms=2000):
                _process(orch, bus, mid, fields)
            if now - last_tick > s.tick_seconds:
                last_tick = now
                orch.tick()
        except Exception:
            log.exception("erro no ciclo principal; tentando de novo em 3s")
            time.sleep(3)
    orch.pool.shutdown(wait=True, cancel_futures=False)
    return 0


def _process(orch: Orchestrator, bus: EventBus, mid: str, fields: dict) -> None:
    try:
        orch.handle(fields)
    except Exception:
        log.exception("falha ao tratar evento %s (sera reentregue)", mid)
        return  # sem ack: reclaim() reentrega e, apos N tentativas, vai para dead-letter
    bus.ack([mid])


if __name__ == "__main__":
    sys.exit(main())
