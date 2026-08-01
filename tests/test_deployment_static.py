#!/usr/bin/env python3
"""Static deployment checks that do not require Docker, a VPS, or credentials."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT = ROOT / "new-api-deployment"


def fail(message: str) -> None:
    raise AssertionError(message)


def service_block(compose: str, service: str) -> str:
    match = re.search(
        rf"^  {re.escape(service)}:\n(?P<body>(?:^    .*\n|^\s*$)*)",
        compose,
        re.MULTILINE,
    )
    if not match:
        fail(f"missing compose service: {service}")
    return match.group("body")


def main() -> None:
    compose = (DEPLOYMENT / "docker-compose.yml").read_text(encoding="utf-8")
    caddy = (DEPLOYMENT / "Caddyfile").read_text(encoding="utf-8")
    env_example = (DEPLOYMENT / ".env.example").read_text(encoding="utf-8")
    healthcheck = (DEPLOYMENT / "scripts" / "healthcheck.sh").read_text(encoding="utf-8")

    for route in (
        "/v1/friend/preflight",
        "/v1/friend/catalog",
        "/v1/friend/balance",
        "/v1/messages",
    ):
        if f"path {route}" not in caddy:
            fail(f"missing exact public route: {route}")
    if re.search(r"path /internal|path /v1/(models|responses)", caddy):
        fail("Caddyfile contains a forbidden public route")

    if caddy.count("reverse_proxy friend-gateway:{$FRIEND_GATEWAY_PORT}") != 4:
        fail("all four public Friend routes must terminate at friend-gateway")
    if "reverse_proxy new-api:" in caddy:
        fail("Caddy must not blindly proxy Friend routes to new-api")

    friend_gateway = service_block(compose, "friend-gateway")
    if "dockerfile: friend-gateway/Dockerfile" not in friend_gateway:
        fail("friend-gateway must use the reference gateway image source")
    if "FRIEND_GATEWAY_MODE:" not in friend_gateway:
        fail("friend-gateway mode is not wired")
    if "FRIEND_GATEWAY_CATALOG_FILE:" not in friend_gateway or "FRIEND_GATEWAY_KEY_BINDINGS_FILE:" not in friend_gateway:
        fail("friend-gateway server-side fixture paths are not wired")
    if re.search(r"^    ports:", friend_gateway, re.MULTILINE):
        fail("friend-gateway must not publish a host port")

    caddy_service = service_block(compose, "caddy")
    if "FRIEND_GATEWAY_PORT:" not in caddy_service or "condition: service_healthy" not in caddy_service:
        fail("Caddy must wait for the healthy Friend gateway")

    for required in (
        "FRIEND_GATEWAY_MODE=mock",
        "FRIEND_GATEWAY_PORT=3100",
        "FRIEND_GATEWAY_CATALOG_FILE=/app/config/catalog.json",
        "FRIEND_GATEWAY_KEY_BINDINGS_FILE=/app/config/key-bindings.json",
    ):
        if required not in env_example:
            fail(f"deployment template is missing {required.split('=', 1)[0]}")

    if "friend-gateway" not in healthcheck or "/healthz" not in healthcheck:
        fail("healthcheck must verify the gateway service and keep /healthz private")

    for service in ("new-api", "db"):
        if re.search(r"^    ports:", service_block(compose, service), re.MULTILINE):
            fail(f"{service} must not publish a host port")

    if "replace-with-runtime-secret" not in env_example:
        fail("deployment template must keep runtime secrets out of source")
    if ".invalid" not in env_example:
        fail("deployment template must use non-routable example values")
    print("deployment static validation: ok (template only; no VPS or credentials used)")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError) as error:
        print(f"deployment static validation: FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
