#!/usr/bin/env python3
"""
deploy-watcher: Post-deployment health monitor.
Watches services after deploy and alerts/rollbacks on failure.
"""

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import httpx
import yaml
from rich.console import Console
from rich.table import Table


console = Console()
logger = logging.getLogger("deploy-watcher")


class ServiceStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    service_name: str
    status: ServiceStatus
    response_time_ms: float
    status_code: Optional[int] = None
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "service": self.service_name,
            "status": self.status.value,
            "response_time_ms": round(self.response_time_ms, 2),
            "status_code": self.status_code,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class ServiceConfig:
    name: str
    url: str
    method: str = "GET"
    expected_status: int = 200
    expected_body: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)


class HealthChecker:
    """Performs HTTP health checks against configured services."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def check(self, service: ServiceConfig) -> CheckResult:
        start = time.monotonic()
        try:
            response = await self.client.request(
                method=service.method,
                url=service.url,
                headers=service.headers,
            )
            elapsed = (time.monotonic() - start) * 1000

            status = ServiceStatus.HEALTHY
            if response.status_code != service.expected_status:
                status = ServiceStatus.DEGRADED

            if service.expected_body and service.expected_body not in response.text:
                status = ServiceStatus.DEGRADED

            return CheckResult(
                service_name=service.name,
                status=status,
                response_time_ms=elapsed,
                status_code=response.status_code,
            )

        except httpx.TimeoutException:
            elapsed = (time.monotonic() - start) * 1000
            return CheckResult(
                service_name=service.name,
                status=ServiceStatus.DOWN,
                response_time_ms=elapsed,
                error="Timeout",
            )
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            return CheckResult(
                service_name=service.name,
                status=ServiceStatus.DOWN,
                response_time_ms=elapsed,
                error=str(e),
            )

    async def close(self):
        await self.client.aclose()


class Notifier:
    """Send notifications via webhooks."""

    def __init__(self, config: dict):
        self.slack_url = config.get("slack", {}).get("webhook_url", "")
        self.webhook_url = config.get("webhook", {}).get("url", "")
        self.client = httpx.AsyncClient(timeout=10)

    async def notify(self, message: str, results: List[CheckResult]):
        tasks = []
        if self.slack_url:
            tasks.append(self._send_slack(message, results))
        if self.webhook_url:
            tasks.append(self._send_webhook(message, results))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_slack(self, message: str, results: List[CheckResult]):
        payload = {
            "text": message,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{message}*"}},
                *[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{self._status_emoji(r.status)} *{r.service_name}*: {r.status.value} ({r.response_time_ms:.0f}ms)",
                        },
                    }
                    for r in results
                ],
            ],
        }
        await self.client.post(self.slack_url, json=payload)

    async def _send_webhook(self, message: str, results: List[CheckResult]):
        payload = {
            "message": message,
            "results": [r.to_dict() for r in results],
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.client.post(self.webhook_url, json=payload)

    @staticmethod
    def _status_emoji(status: ServiceStatus) -> str:
        return {
            ServiceStatus.HEALTHY: ":white_check_mark:",
            ServiceStatus.DEGRADED: ":warning:",
            ServiceStatus.DOWN: ":red_circle:",
            ServiceStatus.UNKNOWN: ":grey_question:",
        }.get(status, ":grey_question:")

    async def close(self):
        await self.client.aclose()


class RollbackEngine:
    """Execute rollback commands when services fail."""

    def __init__(self, config: dict):
        self.enabled = config.get("enabled", False)
        self.command = config.get("command", "")
        self.cooldown = config.get("cooldown", 300)
        self._last_rollback: Optional[float] = None

    async def execute(self) -> bool:
        if not self.enabled or not self.command:
            return False

        if self._last_rollback:
            elapsed = time.time() - self._last_rollback
            if elapsed < self.cooldown:
                logger.warning(f"Rollback cooldown: {self.cooldown - elapsed:.0f}s remaining")
                return False

        logger.warning(f"Executing rollback: {self.command}")
        try:
            proc = await asyncio.create_subprocess_shell(
                self.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            self._last_rollback = time.time()

            if proc.returncode == 0:
                logger.info("Rollback succeeded")
                return True
            else:
                logger.error(f"Rollback failed (exit {proc.returncode}): {stderr.decode()}")
                return False
        except Exception as e:
            logger.error(f"Rollback error: {e}")
            return False


class DeployWatcher:
    """Main orchestrator for deployment health monitoring."""

    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        self.services = self._parse_services()
        self.checker = HealthChecker(
            timeout=self.config.get("global", {}).get("timeout", 5)
        )
        self.notifier = Notifier(self.config.get("notifications", {}))
        self.rollback = RollbackEngine(self.config.get("rollback", {}))
        self.failure_counts: Dict[str, int] = {}
        self.threshold = self.config.get("global", {}).get("failure_threshold", 3)

    @staticmethod
    def _load_config(path: str) -> dict:
        with open(path) as f:
            return yaml.safe_load(f)

    def _parse_services(self) -> List[ServiceConfig]:
        services = []
        for svc in self.config.get("services", []):
            services.append(ServiceConfig(
                name=svc["name"],
                url=svc["url"],
                method=svc.get("method", "GET"),
                expected_status=svc.get("expected_status", 200),
                expected_body=svc.get("expected_body"),
                headers=svc.get("headers", {}),
            ))
        return services

    async def check_all(self) -> List[CheckResult]:
        tasks = [self.checker.check(svc) for svc in self.services]
        return await asyncio.gather(*tasks)

    async def run_once(self, verbose: bool = False) -> List[CheckResult]:
        results = await self.check_all()

        if verbose:
            self._print_results(results)

        for result in results:
            if result.status == ServiceStatus.DOWN:
                self.failure_counts[result.service_name] = (
                    self.failure_counts.get(result.service_name, 0) + 1
                )
                if self.failure_counts[result.service_name] >= self.threshold:
                    await self.notifier.notify(
                        f"Service DOWN: {result.service_name}", results
                    )
                    await self.rollback.execute()
            else:
                self.failure_counts[result.service_name] = 0

        return results

    async def run(self, verbose: bool = False):
        interval = self.config.get("global", {}).get("check_interval", 10)
        console.print(f"[bold green]deploy-watcher started[/] - checking {len(self.services)} services every {interval}s")

        try:
            while True:
                await self.run_once(verbose)
                await asyncio.sleep(interval)
        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/]")
        finally:
            await self.checker.close()
            await self.notifier.close()

    @staticmethod
    def _print_results(results: List[CheckResult]):
        table = Table(title="Health Check Results")
        table.add_column("Service", style="cyan")
        table.add_column("Status")
        table.add_column("Response Time", justify="right")
        table.add_column("Code", justify="center")
        table.add_column("Error")

        for r in results:
            status_style = {
                ServiceStatus.HEALTHY: "[green]HEALTHY[/]",
                ServiceStatus.DEGRADED: "[yellow]DEGRADED[/]",
                ServiceStatus.DOWN: "[red]DOWN[/]",
            }.get(r.status, "[dim]UNKNOWN[/]")

            table.add_row(
                r.service_name,
                status_style,
                f"{r.response_time_ms:.1f}ms",
                str(r.status_code or "-"),
                r.error or "-",
            )

        console.print(table)


@click.command()
@click.option("--config", "-c", required=True, help="Path to config YAML")
@click.option("--verbose", "-v", is_flag=True, help="Print results to console")
@click.option("--once", is_flag=True, help="Run single check and exit")
@click.option("--dry-run", is_flag=True, help="Validate config without running")
def main(config: str, verbose: bool, once: bool, dry_run: bool):
    """Post-deployment health monitor."""
    if not Path(config).exists():
        console.print(f"[red]Config file not found: {config}[/]")
        sys.exit(1)

    watcher = DeployWatcher(config)

    if dry_run:
        console.print(f"[green]Config valid[/] - {len(watcher.services)} services configured")
        for svc in watcher.services:
            console.print(f"  - {svc.name}: {svc.method} {svc.url}")
        return

    if once:
        results = asyncio.run(watcher.run_once(verbose=True))
        failed = [r for r in results if r.status != ServiceStatus.HEALTHY]
        sys.exit(1 if failed else 0)
    else:
        asyncio.run(watcher.run(verbose=verbose))


if __name__ == "__main__":
    main()