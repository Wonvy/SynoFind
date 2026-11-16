#!/usr/bin/env python3
"""Generate the checked-in OpenAPI contract without third-party tooling."""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def ref(name):
    return {"$ref": "#/components/schemas/" + name}


def obj(properties, required=()):
    result = {"type": "object", "properties": properties}
    if required:
        result["required"] = list(required)
    return result


STR = {"type": "string"}
INT = {"type": "integer", "format": "int64"}
TIME = {"type": "string", "description": "UTC RFC3339; empty string if not yet available."}
schemas = {
    "Error": obj({"error": obj({"code": STR, "message": STR}, ("code", "message"))}, ("error",)),
    "Session": obj({"authenticated": {"type": "boolean"}, "csrf_token": STR}, ("authenticated", "csrf_token")),
    "File": obj({
        "id": {"type": "string", "description": "Opaque file identifier; obtain it from search."},
        "name": STR, "path": {"type": "string", "description": "Root-relative path with forward slashes."},
        "size": {"type": "integer", "format": "int64", "minimum": 0},
        "modified_at": {"type": "string", "format": "date-time"}, "extension": STR,
        "preview_type": {"type": "string", "enum": ["image", "pdf", "text", ""]}, "smb_path": STR,
    }, ("id", "name", "path", "size", "modified_at", "extension", "preview_type", "smb_path")),
    "SearchResult": obj({"items": {"type": "array", "items": ref("File")}, "total": INT, "page": INT, "limit": INT}, ("items", "total", "page", "limit")),
    "IndexStatus": obj({"connected": {"type": "boolean"}, "last_search_at": TIME, "last_error": STR, "root_path": STR}, ("connected", "last_error", "root_path")),
    "Settings": obj({"root_path": STR, "smb_root": STR, "dsm_url": STR, "dsm_username": STR, "has_dsm_password": {"type": "boolean"}, "port": {"type": "integer", "readOnly": True, "minimum": 1024, "maximum": 65535}}, ("root_path", "smb_root", "dsm_url", "dsm_username", "has_dsm_password", "port")),
    "SettingsUpdate": obj({"root_path": STR, "smb_root": STR, "dsm_url": STR, "dsm_username": STR, "dsm_password": {"type": "string", "format": "password", "writeOnly": True}}, ("root_path", "smb_root", "dsm_url", "dsm_username")),
    "Token": obj({"id": STR, "name": STR, "created_at": TIME, "last_used_at": TIME}, ("id", "name", "created_at")),
    "TokenCreated": obj({"id": STR, "name": STR, "created_at": TIME, "token": {"type": "string", "description": "Only revealed in this response; store securely."}}, ("id", "name", "created_at", "token")),
    "TokenList": obj({"items": {"type": "array", "items": ref("Token")}}, ("items",)),
}


def response(schema, description="Success"):
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def operation(summary, schema=None, *, security=None, status="200", errors=("400", "401", "403", "500")):
    responses = {status: response(schema) if schema is not None else {"description": "Success; no response body."}}
    responses.update({code: response(ref("Error"), {"400": "Invalid parameters", "401": "Authentication required", "403": "Access or CSRF denied", "404": "Not found", "409": "Conflicting state", "429": "Rate limited", "500": "Internal error"}.get(code, "Error")) for code in errors})
    out = {"summary": summary, "responses": responses}
    if security is not None:
        out["security"] = security
    return out


def body(schema):
    return {"required": True, "content": {"application/json": {"schema": schema}}}


def query(name, schema, description):
    return {"name": name, "in": "query", "required": False, "description": description, "schema": schema}


FILE_ID = {"name": "id", "in": "path", "required": True, "schema": STR, "description": "Opaque ID returned by the API."}
CSRF = {"name": "X-CSRF-Token", "in": "header", "required": False, "schema": STR, "description": "Required for session-cookie mutations. Obtain from login/session; not used with a Bearer token."}
SESSION_ONLY = [{"SessionCookie": []}]
paths = {}
login = operation("Log in to the personal web account", ref("Session"), security=[], errors=("400", "401", "403", "429", "500"))
login["requestBody"] = body(obj({"password": {"type": "string", "format": "password"}}, ("password",)))
paths["/auth/login"] = {"post": login}
paths["/auth/session"] = {"get": operation("Inspect web session and obtain CSRF token", ref("Session"), security=SESSION_ONLY, errors=("401", "403", "500"))}
logout = operation("End web session", security=SESSION_ONLY, status="204")
logout["parameters"] = [CSRF]
paths["/auth/logout"] = {"post": logout}
search = operation("Search filenames through the configured DSM File Station API", ref("SearchResult"))
search["parameters"] = [
	query("q", STR, "Filename pattern accepted by DSM File Station.Search; parent paths and file contents are not searched."),
    query("ext", STR, "Comma-separated extensions without dots, e.g. pdf,docx."),
    query("min_size", {**INT, "minimum": 0}, "Minimum bytes."), query("max_size", {**INT, "minimum": 0}, "Maximum bytes."),
    query("modified_after", {"type": "string", "format": "date-time"}, "Modification lower bound (RFC3339)."),
    query("modified_before", {"type": "string", "format": "date-time"}, "Modification upper bound (RFC3339)."),
    query("page", {"type": "integer", "minimum": 1, "default": 1}, "One-based page."),
    query("limit", {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}, "Maximum page size."),
    query("sort", {"type": "string", "enum": ["name", "modified", "size"], "default": "name"}, "Sort key."),
    query("order", {"type": "string", "enum": ["asc", "desc"], "default": "asc"}, "Sort direction."),
]
paths["/search"] = {"get": search}
detail = operation("Get file metadata", ref("File"), errors=("400", "401", "403", "404", "500"))
detail["parameters"] = [FILE_ID]
paths["/files/{id}"] = {"get": detail}
content = operation("Stream original file bytes for download", errors=("400", "401", "403", "404", "500"))
content["parameters"] = [FILE_ID]
content["responses"]["200"] = {"description": "Complete file", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}
paths["/files/{id}/content"] = {"get": content}
preview = operation("Preview safe supported file types", errors=("400", "401", "403", "404", "500"))
preview["parameters"] = [FILE_ID]
preview["responses"]["200"] = {"description": "Image, PDF or plain text; active HTML/SVG is not rendered inline.", "content": {mime: {"schema": {"type": "string", "format": "binary"}} for mime in ("image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain")}}
preview["responses"]["415"] = response(ref("Error"), "Preview not supported for this file type")
preview["responses"]["413"] = response(ref("Error"), "Plain text preview exceeds the 2 MiB limit; download the file instead")
paths["/files/{id}/preview"] = {"get": preview}
paths["/index/status"] = {"get": operation("Read the latest DSM search connection status", ref("IndexStatus"))}
settings = operation("Update the DSM connection and File Station search scope", ref("Settings"), security=SESSION_ONLY, errors=("400", "401", "403", "500"))
settings["requestBody"] = body(ref("SettingsUpdate"))
settings["parameters"] = [CSRF]
paths["/settings"] = {"get": operation("Read personal server settings", ref("Settings"), security=SESSION_ONLY), "put": settings}
token = operation("Create an API token (shown once)", ref("TokenCreated"), security=SESSION_ONLY, status="201")
token["requestBody"] = body(obj({"name": {"type": "string", "minLength": 1}}, ("name",)))
token["parameters"] = [CSRF]
paths["/tokens"] = {"get": operation("List token metadata without secrets", ref("TokenList"), security=SESSION_ONLY), "post": token}
revoke = operation("Revoke an API token", security=SESSION_ONLY, status="204", errors=("400", "401", "403", "404", "500"))
revoke["parameters"] = [FILE_ID, CSRF]
paths["/tokens/{id}"] = {"delete": revoke}
paths["/health"] = {"get": operation("Public liveness check without personal data", obj({"status": STR}), security=[], errors=("500",))}

document = {
    "openapi": "3.0.3",
    "info": {"title": "SynoFind API", "version": "0.2.0", "description": "Personal, read-only NAS filename search and retrieval through Synology File Station APIs. Bearer tokens can search and read files; settings and token administration require a web session and CSRF protection. DSM credentials are write-only configuration values and are never returned."},
    "servers": [{"url": "/api/v1", "description": "Same SynoFind origin"}],
    "security": [{"ApiToken": []}, {"SessionCookie": []}],
    "paths": paths,
    "components": {"securitySchemes": {"ApiToken": {"type": "http", "scheme": "bearer", "description": "Token created in the SynoFind settings page."}, "SessionCookie": {"type": "apiKey", "in": "cookie", "name": "synofind_session"}}, "schemas": schemas},
}
OUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(OUT)
