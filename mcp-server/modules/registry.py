"""
Module Registry and Dependency Resolution

This module provides:
- ``ModuleInfo``: A lightweight container holding a module's name and its exposed API dict.
- ``ModuleRegistry``: A thread-agnostic registry that stores and retrieves module API
  information for inter-module communication.
- ``resolve_dependency_order()``: Implements Kahn's algorithm for topological sorting of
  module dependencies, detecting circular dependencies and raising ``ValueError`` when
  they are found.

The dependency resolver is used at startup to determine the correct order in which
modules must be loaded so that dependencies are always available before dependent modules.
"""

import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ModuleInfo:
    """
    Lightweight container for a module's metadata and API interface.

    Each loaded module gets a ``ModuleInfo`` instance that holds:
    - ``name``: The module's directory name (e.g. ``"superset"``).
    - ``api``: A dictionary mapping method names to callable objects, representing
      the public interface that other modules can use.
        This class is primarily used internally by ``ModuleRegistry`` and is not
        typically instantiated directly by module authors.
    """

    def __init__(self, name: str, api: Dict[str, Any] = None):
        """
        Create a new ``ModuleInfo`` instance.

        Args:
            name: The unique name of the module (used as a key in the registry).
            api: An optional dictionary mapping API method names to callables.
                 Defaults to an empty dict if ``None`` is passed.
        """
        self.name = name
        # Default to empty dict so callers don't need to pass one explicitly
        self.api = api or {}

    def get_api(self) -> Dict[str, Any]:
        """
        Return the API dictionary for this module.

        Returns:
            A copy-safe reference to the ``api`` dict. Callers should not modify
            this dict in-place; use ``ModuleRegistry.register_module_api()`` instead.
        """
        return self.api


class ModuleRegistry:
    """
    Registry for tracking module APIs and their inter-module dependencies.

    The ``ModuleRegistry`` is a singleton-like object (instantiated once in ``main.py``)
    that stores ``ModuleInfo`` instances keyed by module name. It supports:
    - Registering or updating a module's API dict.
    - Querying whether a module is registered.
    - Retrieving a module's API dict.
    - Listing all registered module names.
        This registry enables modules to discover and use the APIs of other modules
        at runtime. For example, a module that generates charts can look up a database
        module's connection API via ``registry.get_module_api("database")``.
    """

    def __init__(self):
        """
        Initialise an empty ``ModuleRegistry``.

        The internal ``_modules`` dict maps module name strings to ``ModuleInfo``
        instances. It starts empty and is populated during the module-loading phase.
        """
        self._modules: Dict[str, ModuleInfo] = {}

    def register_module_api(self, module_name: str, api: Dict[str, Any]) -> None:
        """
        Register or update a module's API in the registry.

        If the module is already registered, this method merges the new API dict into
        the existing one (allowing incremental API registration). If the module is
        not yet registered, a new ``ModuleInfo`` instance is created.
            This method is called by each module's ``register()`` function to announce
            its public API. The update-merging behaviour allows multiple calls to
            ``register_module_api()`` for the same module without overwriting existing
            entries.

        Args:
            module_name: The unique name of the module (must match the directory name).
            api: A dictionary mapping public method names to callable objects.
        """
        if module_name in self._modules:
            # Module already exists: merge the new API entries into the existing dict
            self._modules[module_name].api.update(api)
        else:
            # New module: create a ModuleInfo container with the provided API
            self._modules[module_name] = ModuleInfo(module_name, api)
        logger.info(f"Registered API for module: {module_name}")

    def get_module_api(self, module_name: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve the API dict for a registered module.

        Args:
            module_name: The name of the module to look up.

        Returns:
            The API dictionary if the module is registered, or ``None`` if the
            module is not found in the registry.
        """
        # Look up the ModuleInfo; return its api dict if found, None otherwise
        info = self._modules.get(module_name)
        return info.api if info else None

    def list_modules(self) -> List[str]:
        """
        Return a list of all registered module names.

        Returns:
            A list of module name strings, one for each module registered in the
            registry. The order is insertion order (Python 3.7+ dict guarantee).
        """
        return list(self._modules.keys())

    def has_module(self, module_name: str) -> bool:
        """
        Check whether a module is registered in the registry.

        Args:
            module_name: The name of the module to check.

        Returns:
            ``True`` if the module has been registered, ``False`` otherwise.
        """
        return module_name in self._modules


def resolve_dependency_order(
    modules: Dict[str, List[str]]
) -> List[str]:
    """
    Resolve the load order for modules using Kahn's topological sort algorithm.

    This function implements Kahn's algorithm to compute a linear ordering of modules
    such that every module appears after all of its dependencies. The algorithm:
    1. Builds an in-degree count for each module (number of unresolved dependencies).
    2. Seeds a queue with all modules that have zero in-degree (no dependencies).
    3. Repeatedly removes a module from the queue, adds it to the sorted output,
       and decrements the in-degree of all modules that depend on it.
    4. If the sorted output contains fewer modules than were input, a circular
       dependency exists and a ``ValueError`` is raised.
        This is the dependency resolution mechanism used at startup to determine the
        order in which dynamically-discovered modules should be imported. Without a
        correct ordering, a module might be loaded before its dependencies, causing
        import-time failures.

    Args:
        modules: A dictionary mapping each module name to a list of its dependency
                 module names. For example:
                 ``{"charts": ["database", "sql"], "database": []}`` means ``charts``
                 depends on ``database`` and ``sql``, and ``database`` has no deps.

    Returns:
        A list of module name strings in valid topological (dependency-resolved) order.
        Modules with no dependencies appear first.

    Raises:
        ValueError: If a circular dependency is detected among the modules. The error
            message includes the names of the modules involved in the cycle.
    """
    # Step 1: Convert each dependency list to a set for O(1) membership checking
    deps = {}
    for mod_name, dep_list in modules.items():
        deps[mod_name] = set(dep_list)

    # Step 2: Calculate in-degree for each module (number of dependencies it has)
    # In a directed graph, in-degree = number of edges pointing TO the node
    in_degree = {m: 0 for m in deps}
    for m, dep_set in deps.items():
        in_degree[m] = len(dep_set)

    # Step 3: Build the adjacency list (dependency -> list of modules that depend on it)
    # This is the reverse of the dependency direction: for each dependency d of module m,
    # we add an edge d -> m meaning "m becomes available once d is resolved"
    dep_graph = defaultdict(list)
    for m, dep_set in deps.items():
        for d in dep_set:
            dep_graph[d].append(m)

    # Step 4: Seed the queue with all modules that have zero in-degree (no dependencies)
    # These are the modules that can be loaded immediately
    queue = deque([m for m, deg in in_degree.items() if deg == 0])
    sorted_order = []

    # Step 5: Process the queue using Kahn's algorithm
    while queue:
        # Dequeue the next module with all dependencies satisfied
        node = queue.popleft()
        # Add it to the resolved order
        sorted_order.append(node)
        # For each module that depends on this one, decrement its in-degree
        for dependent in dep_graph[node]:
            in_degree[dependent] -= 1
            # If the dependent now has all its dependencies satisfied, enqueue it
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Step 6: Check for circular dependencies
    # If any module still has a non-zero in-degree, it was part of a cycle
    unresolved = [m for m, deg in in_degree.items() if deg > 0]
    if unresolved:
        raise ValueError(
            f"Circular dependency detected among modules: {unresolved}"
        )

    # Return the topologically sorted list of module names
    return sorted_order
