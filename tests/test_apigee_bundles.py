"""K2: bundles de Apigee, su empaquetado y la carpeta de Postman (INTEGRACION_KIZUNA_OAUTH2.spec.md, 3.1–3.4 y Anexo A).

Sin red: se verifica el XML versionado. Lo que solo Apigee puede confirmar se prueba en la organización de evaluación
(criterio K2.3, lista de `deploy/apigee/README.md`).
"""

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts.package_apigee import APIGEE_DIR, bundle_dirs, package_all

BUNDLES = ("kizuna-consents", "kizuna-oauth")
SCOPE = "kizuna.consents"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Anexo A del spec: cuerpo exacto de cada respuesta de error, por política.
ERROR_BODIES = {
    ("kizuna-oauth", "AM-InvalidClient"): (
        401,
        {"error": "invalid_client", "error_description": "Credenciales del cliente inválidas"},
    ),
    ("kizuna-oauth", "AM-InvalidRequest"): (
        400,
        {"error": "invalid_request", "error_description": "Petición de token inválida"},
    ),
    ("kizuna-oauth", "RF-MissingGrantType"): (
        400,
        {"error": "invalid_request", "error_description": "Petición de token inválida"},
    ),
    ("kizuna-oauth", "RF-UnsupportedGrantType"): (
        400,
        {
            "error": "unsupported_grant_type",
            "error_description": "Solo se admite grant_type=client_credentials",
        },
    ),
    ("kizuna-oauth", "RF-NotFound"): (404, {"detail": "Recurso no encontrado"}),
    ("kizuna-consents", "RF-NotFound"): (404, {"detail": "Recurso no encontrado"}),
    ("kizuna-consents", "AM-MissingToken"): (401, {"detail": "Falta el token de acceso"}),
    ("kizuna-consents", "AM-InvalidToken"): (401, {"detail": "Token de acceso inválido o vencido"}),
    ("kizuna-consents", "AM-InsufficientScope"): (
        403,
        {"detail": "El token no tiene permiso para esta operación"},
    ),
}


def apiproxy(bundle: str) -> Path:
    return APIGEE_DIR / bundle / "apiproxy"


def xml(bundle: str, relative: str) -> ET.Element:
    return ET.parse(apiproxy(bundle) / relative).getroot()


def policy(bundle: str, name: str) -> ET.Element:
    return xml(bundle, f"policies/{name}.xml")


def text(element: ET.Element, path: str) -> str:
    found = element.find(path)
    assert found is not None, path
    return (found.text or "").strip()


def step_names(container: ET.Element | None) -> list[str]:
    return [] if container is None else [text(step, "Name") for step in container.findall("Step")]


def flows(bundle: str) -> dict[str, ET.Element]:
    return {flow.get("name"): flow for flow in xml(bundle, "proxies/default.xml").find("Flows")}


def payload_json(element: ET.Element) -> Any:
    """El Payload con cada {variable} reemplazada por un valor JSON válido (Apigee la resuelve en tiempo de ejecución)."""
    raw = text(element, ".//Set/Payload")
    return json.loads(re.sub(r"\{[A-Za-z0-9_.\-]+\}", "1", raw))


def headers(element: ET.Element, section: str = "Set") -> dict[str, str]:
    return {h.get("name"): (h.text or "").strip() for h in element.findall(f".//{section}/Headers/Header")}


# --- Estructura ---


def test_the_bundles_are_the_expected_ones() -> None:
    assert [path.name for path in bundle_dirs()] == sorted(BUNDLES)


@pytest.mark.parametrize("bundle", BUNDLES)
def test_descriptor_and_endpoints_exist(bundle: str) -> None:
    assert xml(bundle, f"{bundle}.xml").get("name") == bundle
    assert xml(bundle, "proxies/default.xml").get("name") == "default"


@pytest.mark.parametrize("bundle", BUNDLES)
def test_every_step_has_its_policy_and_every_policy_is_used(bundle: str) -> None:
    endpoints = [
        apiproxy(bundle) / "proxies" / "default.xml",
        *sorted((apiproxy(bundle) / "targets").glob("*.xml")),
    ]
    used = {text(step, "Name") for path in endpoints for step in ET.parse(path).getroot().iter("Step")}
    files = {path.stem: path for path in (apiproxy(bundle) / "policies").glob("*.xml")}
    assert used == set(files)
    for name, path in files.items():
        assert ET.parse(path).getroot().get("name") == name


@pytest.mark.parametrize("bundle", BUNDLES)
def test_bundles_have_no_hosts_urls_or_secrets(bundle: str) -> None:
    for path in apiproxy(bundle).rglob("*.xml"):
        content = path.read_text(encoding="utf-8")
        for forbidden in ("http://", "https://", "run.app", "bcscode", "nip.io", "<URL>"):
            assert forbidden not in content, (path.name, forbidden)
        # La API Key solo sale del KVM: ningún header con un valor fijo.
        assert not re.search(r'name="X-API-Key">(?!\{private\.)', content), path.name


@pytest.mark.parametrize(("key", "expected"), ERROR_BODIES.items(), ids=lambda v: str(v))
def test_error_bodies_follow_annex_a(key: tuple[str, str], expected: tuple[int, dict[str, str]]) -> None:
    element = policy(*key)
    status_code, body = expected
    assert text(element, ".//Set/StatusCode") == str(status_code)
    assert payload_json(element) == body
    assert element.find(".//Set/Payload").get("contentType") == "application/json"


# --- kizuna-oauth (3.1) ---


def test_token_proxy_has_no_backend() -> None:
    endpoint = xml("kizuna-oauth", "proxies/default.xml")
    assert text(endpoint, "HTTPProxyConnection/BasePath") == "/oauth2"
    (route,) = endpoint.findall("RouteRule")
    assert route.find("TargetEndpoint") is None
    assert not (apiproxy("kizuna-oauth") / "targets").exists()


def test_token_flow_validates_grant_type_before_oauthv2() -> None:
    token = flows("kizuna-oauth")["token"]
    assert text(token, "Condition") == '(proxy.pathsuffix MatchesPath "/token") and (request.verb = "POST")'
    assert step_names(token.find("Request")) == [
        "RF-MissingGrantType",
        "RF-UnsupportedGrantType",
        "OA-GenerateToken",
    ]
    assert step_names(token.find("Response")) == ["AM-TokenResponse"]
    conditions = [text(step, "Condition") for step in token.find("Request").findall("Step")[:2]]
    assert conditions == [
        '(request.formparam.grant_type = null) or (request.formparam.grant_type = "")',
        'request.formparam.grant_type != "client_credentials"',
    ]


@pytest.mark.parametrize("bundle", BUNDLES)
def test_unknown_routes_end_in_a_404_flow(bundle: str) -> None:
    *_, last = flows(bundle).values()
    assert last.find("Condition") is None
    assert step_names(last.find("Request")) == ["RF-NotFound"]


def test_generate_token_issues_client_credentials_tokens_for_3600_seconds() -> None:
    generate = policy("kizuna-oauth", "OA-GenerateToken")
    assert text(generate, "Operation") == "GenerateAccessToken"
    assert text(generate, "ExpiresIn") == "3600000"
    assert [g.text for g in generate.findall("SupportedGrantTypes/GrantType")] == ["client_credentials"]
    assert text(generate, "GrantType") == "request.formparam.grant_type"
    assert text(generate, "Scope") == "request.formparam.scope"
    assert generate.find("GenerateResponse").get("enabled") == "false"


def test_token_response_is_rfc_6749_without_developer_data() -> None:
    response = policy("kizuna-oauth", "AM-TokenResponse")
    assert response.find("AssignTo").get("type") == "response"
    raw = text(response, ".//Set/Payload")
    assert raw == (
        '{"access_token":"{oauthv2accesstoken.OA-GenerateToken.access_token}","token_type":"Bearer",'
        '"expires_in":{oauthv2accesstoken.OA-GenerateToken.expires_in},'
        '"scope":"{oauthv2accesstoken.OA-GenerateToken.scope}"}'
    )
    assert payload_json(response) == {
        "access_token": "1",
        "token_type": "Bearer",
        "expires_in": 1,
        "scope": "1",
    }
    assert "developer" not in raw.lower()
    assert headers(response) == {"Cache-Control": "no-store", "Pragma": "no-cache"}
    assert text(response, ".//Set/StatusCode") == "200"


def test_token_fault_rules_catch_both_invalid_client_names() -> None:
    rules = {
        rule.get("name"): (step_names(rule), text(rule, "Condition"))
        for rule in xml("kizuna-oauth", "proxies/default.xml").find("FaultRules")
    }
    assert rules == {
        "invalid-client": (
            ["AM-InvalidClient"],
            '(fault.name Matches "*InvalidClientIdentifier") or (fault.name Matches "*invalid_client")',
        ),
        "invalid-request": (["AM-InvalidRequest"], 'fault.name Matches "*InvalidRequest"'),
    }
    assert headers(policy("kizuna-oauth", "AM-InvalidClient"))["WWW-Authenticate"].startswith("Basic ")


# --- kizuna-consents (3.2–3.3) ---


def test_consents_proxy_verifies_then_injects_the_api_key() -> None:
    endpoint = xml("kizuna-consents", "proxies/default.xml")
    assert text(endpoint, "HTTPProxyConnection/BasePath") == "/api/v1/consents"
    assert step_names(endpoint.find("PreFlow/Request")) == [
        "OA-VerifyToken",
        "KVM-GetBackendKey",
        "AM-BackendHeaders",
    ]
    assert text(endpoint, "RouteRule/TargetEndpoint") == "default"


def test_verify_token_requires_the_scope() -> None:
    verify = policy("kizuna-consents", "OA-VerifyToken")
    assert text(verify, "Operation") == "VerifyAccessToken"
    assert text(verify, "Scope") == SCOPE


def test_api_key_comes_from_the_encrypted_environment_kvm() -> None:
    kvm = policy("kizuna-consents", "KVM-GetBackendKey")
    assert kvm.get("mapIdentifier") == "consent-backend"
    assert text(kvm, "Scope") == "environment"
    get = kvm.find("Get")
    assert get.get("assignTo") == "private.backend_api_key"
    assert text(get, "Key/Parameter") == "api_key"


def test_backend_headers_remove_client_credentials_before_setting_the_key() -> None:
    assign = policy("kizuna-consents", "AM-BackendHeaders")
    actions = [child.tag for child in assign if child.tag in ("Remove", "Set", "Add", "Copy")]
    assert actions == ["Remove", "Set"]
    assert [h.get("name") for h in assign.findall("Remove/Headers/Header")] == ["Authorization", "X-API-Key"]
    assert headers(assign) == {"X-API-Key": "{private.backend_api_key}"}
    assert text(assign, "IgnoreUnresolvedVariables") == "false"
    assert assign.find("AssignTo").get("type") == "request"


def test_target_is_the_consent_api_target_server() -> None:
    connection = xml("kizuna-consents", "targets/default.xml").find("HTTPTargetConnection")
    assert [server.get("name") for server in connection.findall("LoadBalancer/Server")] == ["consent-api"]
    assert text(connection, "Path") == "/api/v1/consents"
    assert connection.find("URL") is None


def test_only_the_contract_routes_are_allowed() -> None:
    allowed = {
        name: text(flow, "Condition")
        for name, flow in flows("kizuna-consents").items()
        if name != "not-found"
    }
    # Sin barra final: con ella la API respondería 307 con un Location hacia el host del backend.
    base = '((proxy.pathsuffix = null) or (proxy.pathsuffix = ""))'
    assert allowed == {
        "create": f'(request.verb = "POST") and {base}',
        "list": f'(request.verb = "GET") and {base}',
        "detail": '(request.verb = "GET") and (proxy.pathsuffix MatchesPath "/*") and (proxy.pathsuffix != "/")',
    }


def test_token_faults_map_to_401_and_403() -> None:
    rules = list(xml("kizuna-consents", "proxies/default.xml").find("FaultRules"))
    # De abajo hacia arriba: InsufficientScope (que también marca la verificación como fallida) se evalúa primero.
    assert [rule.get("name") for rule in rules] == ["invalid-token", "insufficient-scope"]
    invalid, scope = rules
    assert text(invalid, "Condition") == "oauthV2.OA-VerifyToken.failed = true"
    assert [(text(s, "Name"), text(s, "Condition")) for s in invalid.findall("Step")] == [
        ("AM-MissingToken", "request.header.Authorization = null"),
        ("AM-InvalidToken", "request.header.Authorization != null"),
    ]
    assert text(scope, "Condition") == 'fault.name Matches "*InsufficientScope"'
    assert headers(policy("kizuna-consents", "AM-MissingToken")) == {"WWW-Authenticate": "Bearer"}
    assert headers(policy("kizuna-consents", "AM-InvalidToken")) == {
        "WWW-Authenticate": 'Bearer error="invalid_token"'
    }


# --- Empaquetado ---


def test_package_builds_one_zip_per_bundle_with_apiproxy_at_the_root(tmp_path: Path) -> None:
    zips = package_all(tmp_path)
    assert [path.name for path in zips] == [f"{name}.zip" for name in sorted(BUNDLES)]
    for path in zips:
        bundle = path.stem
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        expected = sorted(
            p.relative_to(APIGEE_DIR / bundle).as_posix() for p in apiproxy(bundle).rglob("*") if p.is_file()
        )
        assert names == expected
        assert f"apiproxy/{bundle}.xml" in names


def test_package_is_reproducible(tmp_path: Path) -> None:
    first = [p.read_bytes() for p in package_all(tmp_path / "a")]
    second = [p.read_bytes() for p in package_all(tmp_path / "b")]
    assert first == second


# --- Postman (K2, tarea 5) ---


def apigee_folder() -> dict[str, Any]:
    collection = json.loads((PROJECT_ROOT / "postman" / "collection.json").read_text(encoding="utf-8"))
    (folder,) = [item for item in collection["item"] if item["name"] == "KIZUNA vía Apigee (OAuth 2.0)"]
    return folder


def test_postman_apigee_folder_covers_the_kizuna_operations() -> None:
    from app.main import app

    operations = {
        (method.upper(), re.sub(r"\{[^}]+\}", "{}", path))
        for path, ops in app.openapi()["paths"].items()
        for method, op in ops.items()
        if "kizuna" in op.get("tags", [])
    }
    covered = set()
    for item in apigee_folder()["item"]:
        request = item["request"]
        path = "/" + "/".join(request["url"]["path"])
        covered.add((request["method"], re.sub(r"\{\{[^}]+\}\}", "{}", path)))
    assert operations <= covered
    assert ("POST", "/oauth2/token") in covered


def test_postman_apigee_folder_uses_the_bearer_and_never_the_api_key() -> None:
    folder = apigee_folder()
    assert folder["auth"] == {
        "type": "bearer",
        "bearer": [{"key": "token", "value": "{{apigee_access_token}}", "type": "string"}],
    }
    dumped = json.dumps(folder)
    assert "api_key" not in dumped and "X-API-Key" not in dumped
    token = next(item for item in folder["item"] if item["request"]["url"]["path"] == ["oauth2", "token"])
    assert token["request"]["auth"] == {"type": "noauth"}
    assert {(p["key"], p["value"]) for p in token["request"]["body"]["urlencoded"]} == {
        ("grant_type", "client_credentials"),
        ("client_id", "{{apigee_client_id}}"),
        ("client_secret", "{{apigee_client_secret}}"),
        ("scope", SCOPE),
    }
