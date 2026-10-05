"""
Utility to map Google-style docstrings + function signatures to FastAPI OpenAPI metadata.

This module bridges the gap between the Google-style docstrings (Description/Args/Returns)
added throughout the codebase and FastAPI's Swagger/OpenAPI documentation. FastAPI natively
uses the first line of a docstring as the ``summary`` but does not parse structured sections
like ``Args:`` and ``Returns:``. The ``docstring_to_openapi()`` function in this module
extracts those sections, builds parameter schemas from type annotations via
``inspect.signature()``, and formats everything as OpenAPI-compatible metadata.

Output includes:
- ``summary`` / ``description`` — Markdown for Swagger UI display
- ``parameters`` — OpenAPI-compatible parameter definitions with types and descriptions
- ``requestBody`` — JSON request body schema for POST endpoints
- ``responses`` — response descriptions from docstring Returns sections
"""

import inspect
import dis
import typing
import docstring_parser

# Map Python type annotations to OpenAPI type strings
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}


def _py_type_to_openapi_type(param) -> str:
    """Map a Python type annotation to an OpenAPI type string.

    Args:
        param: An ``inspect.Parameter`` object.

    Returns:
        OpenAPI type string (e.g. ``"string"``, ``"integer"``, ``"array"``).
    """
    if param.annotation is inspect.Parameter.empty:
        return "string"

    ann = param.annotation
    origin = typing.get_origin(ann)
    if origin is not None:
        # Handle Optional[T] → the underlying type, mark as nullable
        if origin is typing.Union:
            args = typing.get_args(ann)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                return _type_from_class(non_none[0])
        # Handle List[T] → array
        if origin is list or origin is typing.List:
            return "array"
    return _type_from_class(ann)


def _type_from_class(cls) -> str:
    """Map a Python class to OpenAPI type string.

    Args:
        cls: A Python type (str, int, dict, etc.).

    Returns:
        OpenAPI type string.
    """
    return _TYPE_MAP.get(cls, "string")


def _is_required(param) -> bool:
    """Determine if a parameter is required (has no default value).

    Args:
        param: An ``inspect.Parameter`` object.

    Returns:
        True if the parameter has no default value and is not ``**kwargs``.
    """
    return param.default is inspect.Parameter.empty


def _extract_default(param) -> object:
    """Extract the default value from a function parameter for OpenAPI schema.
        Converts Python default values (including ``None`` and sentinel objects)
        to JSON-serializable forms. Returns ``None`` if the parameter has no default.
        ``Optional`` parameters with ``None`` default get ``None``, not ``""``.

    Args:
        param: An ``inspect.Parameter`` object.

    Returns:
        The JSON-serializable default value, or ``None`` if no default.
    """
    if param.default is inspect.Parameter.empty:
        return None
    default = param.default
    if default is None:
        return None
    if isinstance(default, (str, int, float, bool)):
        return default
    if isinstance(default, (list, tuple)):
        return list(default)
    # Unrepresentable defaults (e.g., mutable dicts) → skip
    return None


def _format_default_for_desc(param) -> str:
    """Format a parameter's default value for inclusion in the description.

    Args:
        param: An ``inspect.Parameter`` object.

    Returns:
        String like `` Defaults to "robust".`` or `` Defaults to False.``
        or empty string if the parameter has no default.
    """
    if param.default is inspect.Parameter.empty:
        return ""
    default = param.default
    if default is None:
        return " Defaults to None."
    if isinstance(default, str):
        return f' Defaults to "{default}".'
    if isinstance(default, bool):
        return f" Defaults to {default}."
    if isinstance(default, (int, float)):
        return f" Defaults to {default}."
    return ""


def _build_openapi_parameters(
    fn, doc_params: list, is_get: bool
) -> list:
    """Build OpenAPI-compatible parameter definitions from function signature + docstrings.
        Introspects the function's signature to extract parameter names, types, and
        required flags, then merges descriptions from the parsed docstring Args section.
        For GET endpoints, parameters are ``query`` type; for POST they are ``body``
        references (not included here — see ``_build_request_body()``).

    Args:
        fn: The callable to introspect.
        doc_params: List of ``docstring_parser.DocstringParam`` objects.
        is_get: True if this is a GET endpoint (parameters → query), False for POST.

    Returns:
        List of OpenAPI parameter dicts, each with ``name``, ``in``, ``required``,
        ``description``, and ``schema`` keys.
    """
    if not is_get:
        return []

    sig = inspect.signature(fn)
    doc_desc_map = {p.arg_name: p.description for p in doc_params if p.description}

    parameters = []
    for name, param in sig.parameters.items():
        # Skip self/cls, **kwargs, and return annotation
        if name in ("self", "cls"):
            continue
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            continue

        required = _is_required(param)
        op_type = _py_type_to_openapi_type(param)
        desc = doc_desc_map.get(name, "")
        default_val = _extract_default(param)
        default_desc = _format_default_for_desc(param)

        if default_desc and default_desc not in desc:
            desc = (desc + default_desc).strip()

        op_param = {
            "name": name,
            "in": "query",
            "required": required,
            "description": desc,
            "schema": {"type": op_type},
        }
        if default_val is not None:
            op_param["schema"]["default"] = default_val

        # Handle Optional: if Union with None, mark nullable
        ann = param.annotation
        if ann is not inspect.Parameter.empty:
            origin = typing.get_origin(ann)
            if origin is typing.Union:
                args = typing.get_args(ann)
                if type(None) in args:
                    op_param["schema"]["nullable"] = True
                    op_param["required"] = False

        parameters.append(op_param)

    return parameters


def _build_request_body(
    fn, doc_params: list, summary: str
) -> dict:
    """Build an OpenAPI requestBody definition for POST endpoints.
        Generates a JSON request body schema from the function's signature parameters.
        Each parameter becomes a property of the JSON object, with type and description
        from docstrings. Optional parameters are listed but not marked required.

    Args:
        fn: The callable to introspect.
        doc_params: List of ``docstring_parser.DocstringParam`` objects.
        summary: Short description used as the request body description.

    Returns:
        OpenAPI-compatible requestBody dict with ``application/json`` content schema.
    """
    sig = inspect.signature(fn)
    doc_desc_map = {p.arg_name: p.description for p in doc_params if p.description}

    properties = {}
    required_list = []

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            continue

        op_type = _py_type_to_openapi_type(param)
        desc = doc_desc_map.get(name, "")
        is_req = _is_required(param)
        default_val = _extract_default(param)
        default_desc = _format_default_for_desc(param)

        if default_desc and default_desc not in desc:
            desc = (desc + default_desc).strip()

        prop = {
            "type": op_type,
            "description": desc,
        }
        if default_val is not None:
            prop["default"] = default_val

        # Handle Optional[T] → nullable
        ann = param.annotation
        if ann is not inspect.Parameter.empty:
            origin = typing.get_origin(ann)
            if origin is typing.Union:
                args = typing.get_args(ann)
                if type(None) in args:
                    prop["nullable"] = True
                    is_req = False
            if origin is list or origin is typing.List:
                args = typing.get_args(ann)
                if args:
                    item_type = args[0]
                    prop["items"] = {"type": _type_from_class(item_type)}

        properties[name] = prop
        if is_req:
            required_list.append(name)

    schema = {
        "type": "object",
        "properties": properties,
    }
    if required_list:
        schema["required"] = required_list

    return {
        "description": summary,
        "content": {
            "application/json": {
                "schema": schema,
            }
        },
    }


def _resolve_wrapper_impl(fn):
    """Resolve the implementation function called by a thin tool wrapper.
        Tool wrappers in SupersetModule/main.py follow a consistent pattern:
        ``return [await] MODULE_VAR.FUNC_NAME(manager, ...)``. This function
        inspects the wrapper's bytecode to find the ``LOAD_GLOBAL`` +
        ``LOAD_ATTR`` call pattern, then uses ``__globals__`` to retrieve
        the actual implementation function.

        The implementation function typically has a rich Google-style
        docstring (Description, Args, Returns) that the wrapper lacks.

    Args:
        fn: The thin wrapper function to analyze.

    Returns:
        The resolved implementation callable if found, or None if the
        wrapper doesn't follow the expected pattern.
    """
    try:
        instructions = list(dis.get_instructions(fn))

        for i, instr in enumerate(instructions):
            if instr.opname == "LOAD_GLOBAL" and i + 1 < len(instructions):
                next_instr = instructions[i + 1]
                if next_instr.opname == "LOAD_ATTR":
                    module_var = instr.argval
                    func_name = next_instr.argval
                    if module_var and func_name:
                        mod = fn.__globals__.get(module_var)
                        if mod and hasattr(mod, func_name):
                            impl = getattr(mod, func_name)
                            if callable(impl) and impl.__doc__:
                                return impl
    except (OSError, TypeError, AttributeError):
        pass

    return None


def docstring_to_openapi(fn, impl_fn=None):
    """
    Parse a function's Google-style docstring and return OpenAPI-compatible metadata.

    Extracts the short description as the Swagger ``summary`` and builds a rich
    Markdown ``description`` from the long description, parameter list, and return
    value documentation. Also builds OpenAPI-compatible ``parameters`` (for GET)
    or ``requestBody`` (for POST) and response descriptions from function signatures.
        When ``impl_fn`` is provided and has a richer docstring (contains ``Args:``
        while the wrapper doesn't), it is used for docstring content, parameter
        descriptions, and return value documentation. Parameter signatures are
        always taken from the wrapper (since the implementation may have extra
        params like ``manager``).

    Args:
        fn: A callable whose ``__doc__`` follows the Google-style convention
            (sections: Description, Args, Returns).
        impl_fn: Optional. The implementation function that the wrapper delegates to.
            If provided and has a richer docstring (contains ``Args:``), it is
            used as the source for description, parameter details, and responses.

    Returns:
        A dictionary with keys:
        - ``summary``: The first line of the docstring, used as the Swagger operation summary.
        - ``description``: A Markdown string combining the long description, parameter
          table, and return value documentation.
        - ``param_descriptions``: A dict mapping parameter names to their descriptions.
        - ``parameters``: OpenAPI-compatible parameter definitions (for GET endpoints).
        - ``requestBody``: OpenAPI request body schema (for POST endpoints, None for GET).
        - ``responses``: Dict with response descriptions.
    """
    # Use impl_fn's docstring if it's richer than the wrapper's (has Args: section)
    doc_fn = fn
    if impl_fn and impl_fn.__doc__:
        impl_doc = impl_fn.__doc__.strip()
        fn_doc = (fn.__doc__ or "").strip()
        if "Args:" in impl_doc and "Args:" not in fn_doc:
            doc_fn = impl_fn

    # Step 1: Parse the full docstring into structured components
    doc = docstring_parser.parse_from_object(doc_fn)

    # Step 2: Collect all content sections in order for the Markdown description
    sections = []

    # Add the short description (first sentence/line) if present
    if doc.short_description:
        sections.append(doc.short_description)

    # Append the long description (everything between short description and Args:) if present
    if doc.long_description:
        sections.append("")
        sections.append(doc.long_description)

    # Step 3: Build a parameter table from the Args section
    if doc.params:
        sections.append("")
        sections.append("**Parameters:**")
        sections.append("")
        # Use a Markdown table for parameters within the description
        sections.append("| Parameter | Type | Required | Description |")
        sections.append("|-----------|------|----------|-------------|")

        sig = inspect.signature(fn)
        sig_params = {
            name: p for name, p in sig.parameters.items()
            if name not in ("self", "cls") and p.kind != inspect.Parameter.VAR_KEYWORD
        }

        for p in doc.params:
            type_name = p.type_name or "any"
            desc = (p.description or "").replace("\n", " ")
            sig_param = sig_params.get(p.arg_name)
            if sig_param and sig_param.default is not inspect.Parameter.empty:
                required = "No"
            else:
                required = "Yes"
            sections.append(f"| `{p.arg_name}` | `{type_name}` | {required} | {desc} |")

    # Step 4: Add the Returns section if documented
    if doc.returns:
        sections.append("")
        sections.append("**Returns:**")
        sections.append("")
        # Include the return type if specified
        if doc.returns.type_name:
            sections.append(f"Type: `{doc.returns.type_name}`")
        if doc.returns.description:
            sections.append(doc.returns.description)

    # Step 5: Build param_descriptions dict
    param_descriptions = {p.arg_name: p.description for p in doc.params if p.description}

    # Step 6: Determine endpoint type from function name convention
    is_get = _is_get_endpoint(fn)

    # Step 7: Build OpenAPI parameters (GET) or requestBody (POST)
    parameters = _build_openapi_parameters(fn, doc.params, is_get)
    summary = doc.short_description or fn.__name__.replace("_", " ").title()
    request_body = None if is_get else _build_request_body(fn, doc.params, summary)

    # Step 8: Build response descriptions
    responses = {"200": {"description": "Successful response"}}
    if doc.returns and doc.returns.description:
        responses["200"]["description"] = doc.returns.description

    # Step 9: Build the result dict for FastAPI route registration
    return {
        "summary": summary,
        "description": "\n".join(sections).strip(),
        "param_descriptions": param_descriptions,
        "parameters": parameters,
        "requestBody": request_body,
        "responses": responses,
    }


def _is_get_endpoint(fn) -> bool:
    """Heuristically determine if a tool endpoint should be GET based on naming.
        Read-only tool names typically start with ``list_``, ``get_``, ``superset_get_``,
        or ``health_check``. This function detects those patterns. The definitive
        mapping is in ``main.py``'s ``READ_ONLY_TOOLS``, but this is a fallback
        for when the full set isn't available.

    Args:
        fn: The callable to check.

    Returns:
        True if the function name suggests GET semantics.
    """
    name = fn.__name__.lower()
    # Also check the tool name as registered (some MCP tool wrappers preserve this)
    if hasattr(fn, "__wrapped__"):
        name = getattr(fn.__wrapped__, "__name__", name).lower()
    get_prefixes = (
        "list_", "get_", "superset_get_", "health_check",
        "superset_list_", "superset_available_",
        "superset_sqllab_get_", "superset_server_status",
        "generate_all_", "analyze_", "get_mermaid_",
    )
    return name.startswith(get_prefixes)
