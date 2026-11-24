"""Barramento Redis Streams (grupo de consumidores, reentrega e dead-letter)."""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("kiln.events")
MAX_DELIVERIES = 5


class EventBus:
    def __init__(self, settings, client=None):
        self.s = settings
        if client is None:
            import redis  # import tardio

            client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        self.r = client

    def ensure_group(self) -> None:
        try:
            self.r.xgroup_create(self.s.stream_events, self.s.group, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP = ja existe
            if "BUSYGROUP" not in str(e):
                raise

    def publish(self, type_: str, run_id: str, **fields) -> str:
        payload = {"type": type_, "run_id": str(run_id), "ts": str(time.time())}
        payload.update({k: (v if isinstance(v, str) else json.dumps(v)) for k, v in fields.items()})
        return self.r.xadd(self.s.stream_events, payload, maxlen=50_000, approximate=True)

    def notify_approval(self, run_id: str, approval_id: str, kind: str, summary: str) -> None:
        """Stream de saida (WhatsApp/painel/Alertmanager podem consumir)."""
        self.r.xadd(self.s.stream_approvals, {"run_id": str(run_id), "approval_id": str(approval_id),
                                              "kind": kind, "summary": summary[:500]},
                    maxlen=10_000, approximate=True)

    def consume(self, consumer: str, count: int = 10, block_ms: int = 5000):
        res = self.r.xreadgroup(self.s.group, consumer, {self.s.stream_events: ">"},
                                count=count, block=block_ms)
        return [(mid, f) for _, msgs in (res or []) for mid, f in msgs]

    def reclaim(self, consumer: str, min_idle_ms: int = 60_000, count: int = 50):
        """Reassume mensagens de consumidores mortos; move para dead-letter as que excederam tentativas."""
        out = []
        pend = self.r.xpending_range(self.s.stream_events, self.s.group, min="-", max="+",
                                     count=count, idle=min_idle_ms)
        for p in pend:
            mid = p["message_id"]
            if p.get("times_delivered", 0) > MAX_DELIVERIES:
                for _id, fields in self.r.xrange(self.s.stream_events, mid, mid):
                    self.r.xadd(self.s.stream_dead, {**fields, "dead_reason": "max_deliveries"},
                                maxlen=10_000, approximate=True)
                self.ack([mid])
                log.warning("evento %s movido para dead-letter", mid)
                continue
            out += self.r.xclaim(self.s.stream_events, self.s.group, consumer, min_idle_ms, [mid])
        return out

    def ack(self, ids) -> None:
        if ids:
            self.r.xack(self.s.stream_events, self.s.group, *ids)
