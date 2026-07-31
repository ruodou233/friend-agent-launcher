#!/usr/bin/env python3
"""Dependency-free structural checks for the V1A OpenAPI contract."""

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONTRACT = ROOT / "friend-api.openapi.json"


def fail(message):
    raise AssertionError(message)


def ref_name(ref):
    prefix = "#/components/schemas/"
    if not isinstance(ref, str) or not ref.startswith(prefix):
        fail("expected schema reference, got {!r}".format(ref))
    return ref[len(prefix):]


def main():
    with CONTRACT.open("r", encoding="utf-8") as handle:
        document = json.load(handle)

    fail_if = lambda condition, message: fail(message) if condition else None
    fail_if(document.get("openapi") != "3.1.0", "OpenAPI version must be 3.1.0")
    fail_if(document.get("x-template-status") != "not-deployed-not-production-verified", "template status must remain explicit")

    expected_paths = {
        "/v1/friend/preflight",
        "/v1/friend/catalog",
        "/v1/friend/balance",
        "/v1/messages",
        "/internal/keys",
        "/internal/keys/{token_id}/revoke",
        "/internal/manual-recharges",
        "/internal/manual-recharges/{business_ref}",
    }
    fail_if(set(document.get("paths", {})) != expected_paths, "paths must exactly match the V1A route set")
    expected_methods = {
        "/v1/friend/preflight": {"get"},
        "/v1/friend/catalog": {"post"},
        "/v1/friend/balance": {"get"},
        "/v1/messages": {"post"},
        "/internal/keys": {"post"},
        "/internal/keys/{token_id}/revoke": {"post"},
        "/internal/manual-recharges": {"post"},
        "/internal/manual-recharges/{business_ref}": {"get"},
    }
    for path, methods in expected_methods.items():
        actual_methods = {method for method in document["paths"][path] if not method.startswith("x-")}
        fail_if(actual_methods != methods, "unexpected method set for {}".format(path))

    schemes = document["components"]["securitySchemes"]
    fail_if(schemes["BearerFriendKey"]["type"] != "http", "Friend key scheme must be HTTP auth")
    fail_if(schemes["BearerFriendKey"]["scheme"] != "bearer", "Friend key scheme must be bearer")
    fail_if(schemes["InternalMTLS"]["type"] != "mutualTLS", "internal scheme must be mutual TLS")

    public = {
        ("/v1/friend/preflight", "get"): [],
        ("/v1/friend/catalog", "post"): [{"BearerFriendKey": []}],
        ("/v1/friend/balance", "get"): [{"BearerFriendKey": []}],
        ("/v1/messages", "post"): [{"BearerFriendKey": []}],
    }
    for (path, method), security in public.items():
        operation = document["paths"][path][method]
        fail_if(operation.get("security") != security, "wrong bearer boundary for {} {}".format(method.upper(), path))
        fail_if(operation.get("x-public") is not True, "public marker missing for {}".format(path))

    for path, methods in document["paths"].items():
        if path.startswith("/internal/"):
            for method, operation in methods.items():
                fail_if(operation.get("security") != [{"InternalMTLS": []}], "internal path is not mTLS-only: {} {}".format(method, path))
                fail_if(operation.get("x-public") is not False, "internal path is marked public: {}".format(path))
                fail_if(operation.get("x-network-scope") != "private-or-loopback", "internal path network scope is not private")

    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            if method.startswith("x-"):
                continue
            parameter_refs = {parameter.get("$ref") for parameter in operation.get("parameters", [])}
            fail_if("#/components/parameters/RequestId" not in parameter_refs, "X-Request-Id missing from {} {}".format(method.upper(), path))

    schemas = document["components"]["schemas"]
    catalog_required = {
        "product", "protocol", "canonical_id", "model_ref", "gateway_ref",
        "display_name", "capabilities", "default", "catalog_version",
        "expires_at", "billing_label", "signature",
    }
    catalog = schemas["CatalogEntry"]
    fail_if(set(catalog["required"]) != catalog_required, "catalog schema required fields drifted")
    fail_if(catalog.get("additionalProperties") is not False, "catalog entries must reject extra fields")
    fail_if(catalog["properties"]["gateway_ref"].get("const") != "friend-fixed-gateway", "gateway must be fixed")
    fail_if("signature" not in catalog["properties"], "catalog signature is required")

    public_request_schemas = ["CatalogRequest", "MessagesRequest"]
    for schema_name in public_request_schemas:
        schema = schemas[schema_name]
        fail_if(schema.get("additionalProperties") is not False, "public request {} must be closed".format(schema_name))
        fail_if("account_id" in schema.get("properties", {}), "public request {} accepts account_id".format(schema_name))
        property_names = set(schema.get("properties", {}))
        fail_if(bool(property_names.intersection({"endpoint", "provider"})), "public request {} exposes endpoint/provider".format(schema_name))

    error_codes = set(schemas["ErrorCode"]["enum"])
    required_error_codes = {
        "AUTH_REQUIRED", "KEY_EXPIRED", "KEY_REVOKED", "PRODUCT_PROTOCOL_MISMATCH",
        "CATALOG_EXPIRED", "CATALOG_UNTRUSTED", "UPSTREAM_UNAVAILABLE", "RECOVERY_REQUIRED",
    }
    fail_if(not required_error_codes.issubset(error_codes), "required error code missing")
    fail_if(set(schemas["ErrorResponse"]["required"]) != {"request_id", "code", "message", "retryable"}, "error response fields drifted")

    request_id_pattern = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    fail_if(schemas["RequestIdValue"]["pattern"] != request_id_pattern, "request_id pattern drifted")
    fail_if(schemas["RechargeRequestId"]["pattern"] != request_id_pattern, "recharge request_id pattern drifted")
    fail_if(schemas["ManualRechargeCreateRequest"]["properties"].get("business_ref") is not None, "business_ref must be server-derived")
    fail_if("manual-wechat:" not in json.dumps(schemas["ManualRechargeResponse"]), "business_ref derivation is not documented")

    # A cheap source-level secret guard for this public contract.
    raw = CONTRACT.read_text(encoding="utf-8")
    fail_if(bool(re.search(r"(?i)(sk-[A-Za-z0-9]{12,}|bearer\s+[A-Za-z0-9_-]{20,})", raw)), "possible credential found in contract")
    print("contract validation: ok ({})".format(CONTRACT))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, KeyError, json.JSONDecodeError) as error:
        print("contract validation: FAILED: {}".format(error), file=sys.stderr)
        sys.exit(1)
