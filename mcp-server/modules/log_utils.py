"""
Log collection utility for merging structured logs into tool results.

Provides:
- ``LogCollector``: A class that records log messages alongside actual logging,
  then merges them into JSON tool results via the ``collect()`` method.

This module is useful for tools that need to include their internal log trail
in the result returned to the MCP client, enabling debugging and audit trails
without requiring clients to access server-side log files.
"""

import json
import logging
from typing import List


class LogCollector:
    """
    Captures log messages and merges them into JSON tool results.

    ``LogCollector`` acts as a bridge between Python's ``logging`` module and
    tool result formatting. Every log message recorded via its ``info()``,
    ``warning()``, and ``error()`` methods is stored in an internal list.
    When ``collect()`` is called with a JSON string result, the log entries
    are injected into the result dict under the ``"_logs"`` key.
        The ``collect()`` method attempts to parse the input string as JSON.
        If it succeeds and the result is a dictionary, the logs are added.
        If parsing fails (e.g., the result is a plain string or malformed JSON),
        the original string is returned unchanged so that non-JSON results
        pass through without modification.

    Examples:
        >>> lc = LogCollector("my_tool")
        >>> lc.info("Starting operation")
        >>> lc.info("Processing item 1")
        >>> result = json.dumps({"status": "ok", "items": 5})
        >>> lc.collect(result)
        # Returns: {"status": "ok", "items": 5, "_logs": ["Starting operation", "Processing item 1"]}
    """

    def __init__(self, name: str):
        """
        Initialise the log collector with a logger instance.

        Args:
            name: The name used to create a ``logging.Logger`` via
                  ``logging.getLogger(name)``. This determines the logger's
                  identity in the logging hierarchy.
        """
        # Get or create a named logger from the logging hierarchy
        self._logger = logging.getLogger(name)
        # Internal list to store all captured log message strings
        self._logs: List[str] = []

    def info(self, msg: str) -> None:
        """
        Record an info-level log message.

        The message is forwarded to the underlying ``logging.Logger.info()`` method
        for console/file output and also stored in the internal log list.

        Args:
            msg: The log message string to record.
        """
        # Forward to the standard logger for console/file output
        self._logger.info(msg)
        # Also capture the message in our internal list for result merging
        self._logs.append(msg)

    def warning(self, msg: str) -> None:
        """
        Record a warning-level log message.

        The message is forwarded to ``logging.Logger.warning()`` and stored
        in the internal list with a ``"WARN: "`` prefix to distinguish it
        from info messages when merged into results.

        Args:
            msg: The warning message string to record.
        """
        # Forward to the standard logger
        self._logger.warning(msg)
        # Capture with a WARN prefix for easy identification in merged results
        self._logs.append(f"WARN: {msg}")

    def error(self, msg: str) -> None:
        """
        Record an error-level log message.

        The message is forwarded to ``logging.Logger.error()`` and stored
        in the internal list with an ``"ERROR: "`` prefix.

        Args:
            msg: The error message string to record.
        """
        # Forward to the standard logger
        self._logger.error(msg)
        # Capture with an ERROR prefix for easy identification in merged results
        self._logs.append(f"ERROR: {msg}")

    def collect(self, result_str: str) -> str:
        """
        Merge captured log entries into a JSON result string.

        This method parses the input string as JSON. If it is a valid JSON
        dictionary, the captured log entries are added under the ``"_logs"``
        key and the enriched dictionary is returned as a formatted JSON string.
        If parsing fails, the original string is returned unchanged.
            The ``_logs`` key is chosen with a leading underscore to avoid
            collision with existing keys in the result dictionary. The result
            is re-serialised with indentation and ``default=str`` for readability
            and safety with non-standard types.

        Args:
            result_str: A JSON string representation of the tool result.
                        If not valid JSON or not a dictionary, it is returned
                        unchanged.

        Returns:
            The enriched JSON string with ``"_logs"`` appended if parsing
            succeeded, or the original string if parsing failed.
        """
        try:
            # Attempt to parse the result string as a JSON dictionary
            d = json.loads(result_str)
            if isinstance(d, dict):
                # Inject the captured log entries under a _logs key
                d["_logs"] = list(self._logs)
                # Re-serialize with indentation for readability
                return json.dumps(d, indent=2, default=str)
        except (json.JSONDecodeError, TypeError, ValueError):
            # If parsing fails, fall through and return the original string
            pass
        # Return the original string for non-JSON or non-dict results
        return result_str
