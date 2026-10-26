"""Calls back into Home Assistant (through the Supervisor proxy).

The add-on runs the theater "Listen" script before playing. That keeps the
receiver/TV logic in Home Assistant, where the Phase 0 dashboard builds it.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


class HAClient:
    def __init__(self, base_url: str = "http://supervisor/core/api", token: str | None = None):
        self.base_url = base_url
        self.token = token or os.environ.get("SUPERVISOR_TOKEN")

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def call_service(self, domain: str, service: str, data: dict) -> bool:
        if not self.available:
            log.info("No Supervisor token; skipping %s.%s", domain, service)
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{self.base_url}/services/{domain}/{service}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json=data,
                )
                r.raise_for_status()
                return True
        except httpx.HTTPError as exc:
            log.warning("Service call %s.%s failed: %s", domain, service, exc)
            return False

    async def run_script(self, entity_id: str, variables: dict | None = None) -> bool:
        data: dict = {"entity_id": entity_id}
        if variables:
            data["variables"] = variables
        return await self.call_service("script", "turn_on", data)
