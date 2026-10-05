"""
FastMCP + FastAPI Integration Server

This module bootstraps a combined MCP (Model Context Protocol) server and FastAPI application.
It dynamically discovers and loads plugins from the ``modules/`` directory, resolves their
dependency graph via topological sort, and registers every tool as both an MCP endpoint and
a FastAPI route so that Swagger/OpenAPI docs are auto-generated.

Module-level responsibilities:
- Define and configure the ``FastMCP`` server instance
- Dynamically load modules from ``modules/`` at import time
- Resolve inter-module dependencies using Kahn's algorithm
- Expose all MCP tools as FastAPI routes for Swagger documentation
- Mount the MCP SSE transport at ``/mcp``
- Provide Prometheus metrics and recent-error observability endpoints
"""

import os
import sys
import inspect
import importlib.util
import logging
from fastmcp import FastMCP
from fastapi import FastAPI, Request
from typing import Optional
from fastapi.responses import RedirectResponse, Response
import uvicorn
from modules.registry import ModuleRegistry, resolve_dependency_order
from modules.metrics import get_metrics
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from modules.openapi_utils import docstring_to_openapi, _resolve_wrapper_impl

# --- Logging Setup ---
# Configure the root logger so every module can emit structured log lines with timestamps
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Core Application Setup ---
# Create the MCP server instance; this is the primary object that holds all registered tools
mcp = FastMCP("FastMCP Server")
# The ModuleRegistry tracks API interfaces exposed by each loaded module
module_registry = ModuleRegistry()

# --- Create MCP Transport Apps BEFORE FastAPI to get proper lifespan ---
# The Streamable HTTP transport requires its StarletteWithLifespan's lifespan
# to be passed to the parent FastAPI app for proper task group initialization
mcp_streamable_app = mcp.http_app(transport="streamable-http", path="/")
mcp_sse_app = mcp.http_app(transport="sse")

# --- Dynamic Module Loading ---


def _read_module_metadata(module_name: str) -> dict:
    """
    Parse a module's ``main.py`` to extract its ``depends_on`` list.

    This function uses Python's AST (Abstract Syntax Tree) parser to inspect the source
    code of a module's ``main.py`` file. It looks for a top-level assignment of the form
    ``depends_on = ["module_a", "module_b"]`` and extracts the dependency names into
    a list. This metadata is later used to build the dependency graph.

    Description:
        Reads the AST of ``modules/<module_name>/main.py`` and extracts any ``depends_on``
        list declaration. If the file does not exist or contains a syntax error, a minimal
        metadata dict with an empty dependency list is returned.

    Args:
        module_name: The name of the module directory under ``modules/`` to inspect.

    Returns:
        A dictionary with keys ``"name"`` (the module name) and ``"depends_on"``
        (a list of dependency module name strings).
    """
    # Build the expected path to the module's main.py file
    module_dir = os.path.join("modules", module_name)
    main_file = os.path.join(module_dir, "main.py")
    # Start with a minimal metadata structure (empty deps, name always present)
    metadata = {"depends_on": [], "name": module_name}

    # If the module doesn't have a main.py, return the empty metadata as-is
    if not os.path.isfile(main_file):
        return metadata

    # Read the raw source code from the module's main.py
    with open(main_file, "r") as f:
        source = f.read()

    # Import AST inside the function to avoid circular imports at module level
    import ast
    try:
        # Parse the source into an AST tree for static analysis
        tree = ast.parse(source)
        # Walk through every top-level node in the AST
        for node in ast.iter_child_nodes(tree):
            # Look for assignment statements (e.g. depends_on = [...])
            if isinstance(node, ast.Assign):
                # Check each target of the assignment (handles multi-target assignments)
                for target in node.targets:
                    # We are only interested in assignments to a simple name variable
                    if isinstance(target, ast.Name) and target.id == "depends_on":
                        # Extract string literal values from the list if present
                        if isinstance(node.value, ast.List):
                            metadata["depends_on"] = [
                                elt.value for elt in node.value.elts
                                if isinstance(elt, ast.Constant)
                            ]
    except SyntaxError:
        # If the source file has invalid Python, skip metadata extraction
        pass

    return metadata


def load_modules(mcp_server: FastMCP) -> list:
    """
    Discover, load, and initialise all modules from the ``modules/`` directory.

    This is the core module-loading mechanism. The function:
    1. Scans ``modules/`` for subdirectories containing a ``main.py``.
    2. Reads each module's metadata (dependency declarations) via AST parsing.
    3. Resolves the full dependency order using Kahn's topological sort algorithm.
    4. Imports each module in dependency order and calls its ``register()`` function.

    Description:
        Modules are loaded sequentially in topological order so that dependencies are
        always initialised before the modules that depend on them. The ``register()``
        function in each module is invoked with the MCP server instance (and optionally
        the registry), allowing it to register tools and register its API with the registry.

    Args:
        mcp_server: The ``FastMCP`` instance to which each module's tools will be registered.

    Returns:
        A list of successfully loaded module name strings. If dependency resolution fails,
        an empty list is returned.
    """
    # Path to the modules directory; relative to the project root
    modules_dir = "modules"
    loaded_modules = []

    # If the modules directory doesn't exist, return empty (graceful no-op)
    if not os.path.exists(modules_dir):
        return loaded_modules

    # Phase 1: Discover all modules and collect their metadata (dependency info)
    module_metas = {}
    for module_name in os.listdir(modules_dir):
        module_path = os.path.join(modules_dir, module_name)
        # Only consider actual directories (not files like __pycache__)
        if os.path.isdir(module_path):
            main_file_path = os.path.join(module_path, "main.py")
            # Each loadable module must have a main.py
            if os.path.isfile(main_file_path):
                # Parse the module's source to extract its depends_on list
                meta = _read_module_metadata(module_name)
                module_metas[module_name] = meta

    # Build a dependency map: module_name -> list of dependency module names
    dep_map = {name: meta["depends_on"] for name, meta in module_metas.items()}
    try:
        # Phase 2: Resolve the topological ordering of modules
        load_order = resolve_dependency_order(dep_map)
        logger.info(f"Module load order (resolved dependencies): {load_order}")
    except ValueError as e:
        # If a circular dependency is detected, log the error and abort loading
        logger.error(f"Dependency resolution failed: {e}")
        return loaded_modules

    # Phase 3: Load each module in the resolved dependency order
    for module_name in load_order:
        main_file_path = os.path.join(modules_dir, module_name, "main.py")
        try:
            # Dynamically import the module using its fully qualified package path
            module_pkg = f"modules.{module_name}.main"
            module = importlib.import_module(module_pkg)

            # Check if the module has a callable ``register`` function
            if hasattr(module, "register") and callable(module.register):
                # Inspect the register function signature to determine if it accepts registry
                sig = inspect.signature(module.register)
                if "registry" in sig.parameters:
                    # If register() accepts a registry parameter, pass both mcp and registry
                    module.register(mcp_server, registry=module_registry)
                else:
                    # Otherwise, pass only the mcp_server for backwards compatibility
                    module.register(mcp_server)

                # Track the successfully loaded module
                loaded_modules.append(module_name)
                # Log the module and its declared dependencies for observability
                deps = module_metas[module_name]["depends_on"]
                logger.info(f"Loaded module: {module_name} (depends_on: {deps})")
            else:
                # Warn if a module lacks a register function (it won't register any tools)
                logger.warning(f"Module '{module_name}' has no 'register(mcp)' function.")

        except Exception as e:
            # Log any exception during import or registration with full traceback
            logger.error(f"Error loading module '{module_name}': {e}", exc_info=True)

    return loaded_modules


# --- Application Startup ---
# Load all modules at import time so tools are available immediately
loaded_modules = load_modules(mcp)

# --- Core Tools ---

@mcp.tool()
def list_loaded_modules() -> dict:
    """
    Return a list of all modules that were successfully loaded at startup.

    This is a built-in MCP tool that queries the ``loaded_modules`` list populated
    during the module-loading phase. It is useful for verifying which modules are
    active and for debugging module discovery issues.

    Returns:
        A dictionary with ``"message"`` (a status message) and ``"loaded_modules"``
        (a list of module name strings).
    """
    return {"message": "Modules loaded", "loaded_modules": loaded_modules}

@mcp.tool()
def list_available_tools() -> list:
    """
    Enumerate all tools currently registered with the MCP server.

    This tool iterates over the internal tool manager's registry and returns
    a structured list of tool names, descriptions, and the module they came from.
    It is primarily useful for debugging and for clients that want to discover
    available tools at runtime.

    Returns:
        A list of dictionaries, each with keys ``"name"``, ``"description"``,
        and ``"module"`` (the Python module the tool was defined in).
    """
    tools = []
    # Iterate over the internal _tools dictionary of the MCP tool manager
    for name, tool in mcp._tool_manager._tools.items():
        tools.append({
            "name": name,
            "description": tool.description,
            "module": tool.fn.__module__
        })
    return tools

@mcp.tool()
def health_check() -> str:
    """
    Perform a basic health check for the MCP server.

    This is a minimal tool that always returns ``"OK"``. It can be used by
    external health probes, load balancers, or monitoring systems to verify
    that the server is alive and responding.

    Returns:
        The string ``"OK"`` when the server is running.
    """
    return "OK"

# --- FastAPI Integration for Documentation ---

# Create a FastAPI app instance that wraps the MCP tools for Swagger/OpenAPI docs
# Pass the Streamable HTTP app's lifespan to ensure proper task group initialization
app = FastAPI(
    title="FastMCP Server",
    description="Auto-generated documentation for FastMCP tools",
    version="1.1.0",
    lifespan=mcp_streamable_app.lifespan
)

# Enable CORS for trusted origins only
from fastapi.middleware.cors import CORSMiddleware
import os


class BodyToQueryMiddleware:
    """ASGI middleware that merges JSON body parameters into query string for POST endpoints."""
    
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        
        path = scope.get("path", "")
        
        # Skip MCP routes - they need body intact for JSON-RPC
        if path.startswith("/sse") or path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        
        import json
        import urllib.parse
        
        # Read the body
        body_parts = []
        while True:
            message = await receive()
            body_parts.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        
        body_bytes = b"".join(body_parts)
        
        if body_bytes:
            try:
                body = json.loads(body_bytes)
                if isinstance(body, dict) and body:
                    existing_query = scope.get("query_string", b"").decode()
                    
                    # Extract param names already in query string
                    existing_keys = set()
                    new_params = []
                    for param in existing_query.split("&"):
                        if "=" in param:
                            key = param.split("=")[0]
                            existing_keys.add(key)
                            new_params.append(param)
                        elif param:
                            new_params.append(param)
                    
                    # Add body params only if not already in query string
                    for key, val in body.items():
                        if val is not None and key not in existing_keys:
                            if isinstance(val, list):
                                # Send list items as repeated params for FastAPI to parse
                                for item in val:
                                    if isinstance(item, (dict, list)):
                                        new_params.append(f"{key}={urllib.parse.quote(json.dumps(item), safe='')}")
                                    elif not isinstance(item, str):
                                        new_params.append(f"{key}={urllib.parse.quote(str(item), safe='')}")
                                    else:
                                        new_params.append(f"{key}={urllib.parse.quote(item, safe='')}")
                            elif isinstance(val, dict):
                                new_params.append(f"{key}={urllib.parse.quote(json.dumps(val), safe='')}")
                            elif not isinstance(val, str):
                                new_params.append(f"{key}={urllib.parse.quote(str(val), safe='')}")
                            else:
                                new_params.append(f"{key}={urllib.parse.quote(val, safe='')}")
                    
                    scope["query_string"] = "&".join(new_params).encode()
                    import logging
                    logging.getLogger(__name__).info(f"BodyToQuery: query_string={scope["query_string"].decode()[:200]}")
            except (json.JSONDecodeError, Exception) as e:
                import logging
                logging.getLogger(__name__).error(f"BodyToQuery error: {e}")
                pass
        
        # Clear Content-Length and body so FastAPI reads from query string
        headers = list(scope.get("headers", []))
        scope["headers"] = [(k, v) for k, v in headers if k.lower() != b"content-length"]
        cleared_body = b""
        async def new_receive():
            return {"type": "http.request", "body": cleared_body, "more_body": False}
        await self.app(scope, new_receive, send)


app.add_middleware(BodyToQueryMiddleware)

# Read allowed origins from environment variable, with a sensible localhost default
allowed_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",")

# Configure the CORSMiddleware to allow cross-origin requests from whitelisted origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Force OpenAPI 3.0.0 for compatibility and strip 422 errors
from fastapi.openapi.utils import get_openapi

def custom_openapi():
    """
    Generate a custom OpenAPI schema with OpenAPI 3.0.0 and enriched documentation.

    FastAPI's default ``get_openapi`` generates an OpenAPI 3.1 schema which some
    MCP clients and third-party parsers do not handle well. This function intercepts
    the schema generation, downgrades the version to 3.0.0, removes 422 responses,
    and enriches the schema with:

    - Parameter descriptions from Google-style docstrings
    - Request body schemas for POST endpoints (derived from function signatures)
    - Response descriptions from docstring Returns sections

    Description:
        Caches the result after first generation to avoid redundant computation on
        every request. The function follows a lazy-initialisation pattern: if the
        schema has already been generated and stored on ``app.openapi_schema``, it
        is returned immediately.

    Returns:
        The OpenAPI schema dictionary (str-compatible via FastAPI's rendering).
    """
    # If we already generated the schema, return the cached version
    if app.openapi_schema:
        return app.openapi_schema
    # Generate the standard OpenAPI schema from the app's routes and metadata
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        servers=[{"url": "/"}]
    )
    # Downgrade to OpenAPI 3.0.0 for broader client compatibility
    openapi_schema["openapi"] = "3.0.0"

    # Remove 422 validation errors which clutter the spec and confuse some parsers
    for path, methods in openapi_schema.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            if isinstance(details, dict) and "responses" in details and "422" in details.get("responses", {}):
                del details["responses"]["422"]

    # ── Enrich schema with docstring metadata ────────────────────────────
    for path, methods in openapi_schema.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if not isinstance(op, dict):
                continue
            operation_id = op.get("operationId", "")
            meta = _tool_openapi_meta.get(operation_id)
            if not meta:
                continue

            # Inject parameter descriptions from docstrings
            for param in (op.get("parameters") or []):
                if not isinstance(param, dict):
                    continue
                name = param.get("name", "")
                if name in meta.get("param_descriptions", {}):
                    param["description"] = meta["param_descriptions"][name]

            # Inject requestBody for POST endpoints if missing
            if method == "post" and "requestBody" not in op:
                rb = meta.get("requestBody")
                if rb:
                    op["requestBody"] = rb

            # Enrich response descriptions
            tool_responses = meta.get("responses", {})
            op_responses = op.get("responses")
            if not isinstance(op_responses, dict):
                op["responses"] = {}
                op_responses = op["responses"]
            for status_code, resp_info in tool_responses.items():
                if status_code not in op_responses:
                    op_responses[status_code] = {}
                if "description" in resp_info:
                    op_responses[status_code]["description"] = resp_info["description"]

    # Inject requestBody for endpoints that accept JSON body with large params
    body_schemas = {
        "/generate_dashboard": {
            "schema": {
                "type": "object",
                "required": ["server_id", "dashboard_title", "chart_ids"],
                "properties": {
                    "server_id": {"type": "string", "description": "Superset server identifier"},
                    "dashboard_title": {"type": "string", "description": "Title of the dashboard"},
                    "chart_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of chart IDs"},
                    "layout_type": {"type": "string", "default": "robust"},
                    "published": {"type": "boolean", "default": False},
                    "slug": {"type": "string", "nullable": True},
                    "json_metadata": {"type": "string", "nullable": True},
                },
            },
            "example": {
                "server_id": "superset_dev",
                "dashboard_title": "Sales Overview",
                "chart_ids": [195, 196, 197],
            },
        },
        "/superset_generate_simple_dashboard_layout": {
            "schema": {
                "type": "object",
                "required": ["server_id", "chart_ids"],
                "properties": {
                    "server_id": {"type": "string", "description": "Superset server identifier"},
                    "chart_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of chart IDs"},
                    "layout_type": {"type": "string", "default": "row_of_charts"},
                    "chart_width": {"type": "integer", "default": 6},
                    "chart_height": {"type": "integer", "default": 50},
                },
            },
            "example": {
                "server_id": "superset_dev",
                "chart_ids": [195, 196],
            },
        },
        "/superset_generate_robust_dashboard_layout": {
            "schema": {
                "type": "object",
                "required": ["server_id", "chart_ids"],
                "properties": {
                    "server_id": {"type": "string", "description": "Superset server identifier"},
                    "chart_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of chart IDs"},
                },
            },
            "example": {
                "server_id": "superset_dev",
                "chart_ids": [195, 196, 197],
            },
        },
        "/superset_bulk_link_charts_to_dashboard": {
            "schema": {
                "type": "object",
                "required": ["server_id", "dashboard_id", "chart_ids"],
                "properties": {
                    "server_id": {"type": "string", "description": "Superset server identifier"},
                    "dashboard_id": {"type": "integer", "description": "Target dashboard ID"},
                    "chart_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of chart IDs to link"},
                },
            },
            "example": {
                "server_id": "superset_dev",
                "dashboard_id": 13,
                "chart_ids": [195, 196],
            },
        },
    }

    for path, body_config in body_schemas.items():
        if path in openapi_schema.get("paths", {}):
            openapi_schema["paths"][path]["post"]["requestBody"] = {
                "content": {
                    "application/json": {
                        "schema": body_config["schema"],
                        "example": body_config["example"],
                    }
                }
            }

    # Cache the modified schema on the app object for subsequent calls
    app.openapi_schema = openapi_schema
    return app.openapi_schema

# Register the custom OpenAPI generator on the FastAPI app
app.openapi = custom_openapi

# Tools that only read data and should be GET in Swagger (NOT exhaustive - add as needed)
# These tool names are mapped to HTTP GET methods instead of POST for better REST semantics
READ_ONLY_TOOLS = {
    "list_superset_servers", "list_loaded_modules", "list_available_tools",
    "health_check", "superset_server_status", "superset_available_resource_types",
    "superset_list_resources", "list_dashboards", "list_charts", "list_databases",
    "list_datasets", "superset_get_resource", "get_chart_info", "get_dashboard_info",
    "get_database_info", "get_dataset_info", "superset_get_chart_api_info",
    "superset_sqllab_get_query_results", "superset_get_database_schema",
    "superset_get_dashboard_api_info", "superset_get_standard_chart_params",
    "superset_get_chart_creation_rules", "superset_get_dashboard_creation_rules",
    "get_schema", "get_instance_info", "get_chart_preview", "get_chart_data",
    "get_chart_type_schema",
    "get_mermaid_doc_content", "get_mermaid_file_descriptions",
    "generate_all_mermaid_diagrams",
    "analyze_database_schema", "get_normalization_rules", "get_table_data",
    "run_read_only_sql",
    "get_server_configs_json", "get_server_configs_full",
    "get_dashboard_expert_info", "check_dashboard_import_ready",
}

# 1. Register all MCP tools as FastAPI endpoints to generate Swagger docs
# This loop iterates over every tool registered in the MCP server and creates a
# corresponding FastAPI route so that Swagger UI displays all tools.
# Also stores metadata per operation_id for schema enrichment in custom_openapi().
_tool_openapi_meta = {}  # operation_id → {param_descriptions, parameters, requestBody, responses}

for tool_name, tool in mcp._tool_manager._tools.items():
    try:
        # Extract the original Python function from the tool wrapper
        endpoint = tool.fn

        # Determine the Swagger tag for this tool based on its module path
        module_path = tool.fn.__module__
        if module_path.startswith("modules."):
            # Extract the first segment of the module path (e.g., "superset", "mermaid")
            tag = module_path.split(".")[1]
        else:
            # Tools not under modules/ get tagged as "Core"
            tag = "Core"

        # Read-only tools use GET; mutating tools use POST for Swagger clarity
        methods = ["GET"] if tool_name in READ_ONLY_TOOLS else ["POST"]

        # Parse the function's Google-style docstring into OpenAPI metadata
        # If the tool is a thin wrapper, auto-resolve the implementation function
        # to inherit its richer docstring (Args, Returns sections)
        impl_fn = _resolve_wrapper_impl(endpoint)
        meta = docstring_to_openapi(endpoint, impl_fn=impl_fn)

        # Store metadata for later schema enrichment in custom_openapi()
        _tool_openapi_meta[tool_name] = meta

        # Register the route on the FastAPI app with the parsed docstring metadata
        app.add_api_route(
            path=f"/{tool_name}",
            endpoint=endpoint,
            methods=methods,
            tags=[tag],
            summary=meta["summary"],
            description=meta["description"],
            name=tool_name,
            operation_id=tool_name
        )
        logging.info(f"Registered FastAPI route for tool: {tool_name} [{', '.join(methods)}] with tag: {tag}")
    except Exception as e:
        # If route registration fails (e.g., name collision), log and continue
        logging.error(f"Failed to register route for tool '{tool_name}': {e}")

# 3. Redirect /sse to /sse/sse for SSE stream, /messages to /sse/messages
# These must be defined BEFORE the mount so they take priority
@app.get("/sse")
async def sse_stream_redirect():
    """Redirect /sse to /sse/sse (SSE stream endpoint)."""
    return RedirectResponse(url="/sse/sse")

@app.get("/messages")
@app.post("/messages")
async def messages_redirect(request: Request):
    """Redirect /messages to /sse/messages (SSE messages endpoint)."""
    return RedirectResponse(url="/sse/messages")

# 2. Mount both MCP transports (SSE + Streamable HTTP) to support multiple clients
# Streamable HTTP transport for Open WebUI and modern MCP clients
# SSE transport for legacy clients (e.g. MCP CLI, other SSE-based clients)
try:
    # Streamable HTTP at /mcp (internal route is /, so full path is /mcp)
    app.mount("/mcp", mcp_streamable_app)
    logging.info("Mounted FastMCP Streamable HTTP server at /mcp")

    # SSE transport mounted at /sse so its routes become /sse/sse and /sse/messages
    app.mount("/sse", mcp_sse_app)
    logging.info("Mounted FastMCP SSE server at /sse (stream: /sse/sse, messages: /sse/messages)")
except Exception as e:
    logging.error(f"Failed to mount FastMCP app: {e}")

# 4. Add observability endpoints
@app.get("/metrics")
async def prometheus_metrics():
    """
    Expose Prometheus metrics in the wire format expected by Prometheus scrapers.

    This endpoint is served at ``GET /metrics`` and returns the latest Prometheus
    scrape format (text/plain; version=0.0.4). It accesses the singleton
    ``MetricsCollector`` and uses ``prometheus_client.generate_latest()`` to produce
    the formatted output.

    Returns:
        A ``Response`` object with the Prometheus metrics body and the correct
        ``Content-Type`` header for Prometheus scraping.

    Example:
        >>> # GET /metrics returns Prometheus text format:
        >>> # # HELP http_requests_total Total HTTP requests
        >>> # # TYPE http_requests_total counter
        >>> # http_requests_total{method="GET",status="200"} 1234
    """
    from modules.metrics import get_metrics
    # Get the singleton MetricsCollector to access all registered metrics
    mc = get_metrics()
    # Generate the Prometheus scrape format and return with proper content type
    return Response(content=generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

@app.get("/errors")
async def get_recent_errors():
    """
    Return the most recent errors recorded by the ``MetricsCollector``.

    Errors are stored in an in-memory ring buffer (capacity 100) so this endpoint
    provides quick access to the last N errors without any disk I/O.

    Returns:
        A JSON response containing a list of the most recent error entries, each
        with a timestamp, tool name, error message, status code, and duration.

    Example:
        >>> # GET /errors returns:
        >>> # [
        >>> #   {
        >>> #     "timestamp": "2025-01-15T10:30:00",
        >>> #     "tool": "execute_query",
        >>> #     "error": "Connection timeout",
        >>> #     "status": "error",
        >>> #     "duration_ms": 30100
        >>> #   }
        >>> # ]
    """
    mc = get_metrics()
    # Retrieve the ring buffer contents (thread-safe via internal locking)
    return mc.get_errors()

# --- Entry Point ---
if __name__ == "__main__":
    # Log startup information including where to find Swagger docs and MCP endpoints
    logging.info("Starting FastMCP Server with FastAPI integration...")
    logging.info("Documentation available at: http://localhost:8000/docs")
    logging.info("File Upload Endpoint at: http://localhost:8000/upload_file (POST multipart/form-data)")
    logging.info("MCP Streamable HTTP Endpoint at: http://localhost:8000/mcp")
    logging.info("MCP SSE Endpoint at: http://localhost:8000/sse/sse")
    logging.info("MCP SSE Messages Endpoint at: http://localhost:8000/sse/messages")

    # Start the uvicorn ASGI server listening on all interfaces, port 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
