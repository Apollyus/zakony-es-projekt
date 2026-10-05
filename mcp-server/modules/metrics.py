"""
Observability module: metrics collection, Prometheus endpoint, and error tracking.

Provides:
- MetricsCollector class with labeled counters and histograms
- Prometheus-compatible /metrics HTTP endpoint
- Error ring buffer (last 100 errors) with /errors endpoint
- Structured JSON log file: logs/metrics.jsonl
- Singleton pattern for global access via get_metrics()
- Timing context manager and async decorator for per-tool instrumentation
"""

import json
import os
import time
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST


# ── Ring buffer for errors ─────────────────────────────────────────────


class ErrorRingBuffer:
    """
    Thread-safe, bounded ring buffer for storing the most recent errors.

    This buffer uses a ``collections.deque`` with ``maxlen`` set to enforce a
    fixed capacity. When the buffer is full, appending a new entry automatically
    evicts the oldest entry. This makes it ideal for maintaining a sliding window
    of the last N errors for the ``/errors`` endpoint.
        All public methods are protected by a ``threading.Lock`` to ensure
        thread-safe access. This is important because errors can be recorded
        from multiple concurrent request handlers while the ``/errors`` endpoint
        reads from a separate HTTP handler thread.

    Examples:
        >>> buffer = ErrorRingBuffer(capacity=5)
        >>> buffer.add("chart", "Connection failed")
        >>> len(buffer.get_all())  # Returns list of error dicts
        1
    """

    def __init__(self, capacity: int = 100):
        """
        Initialise the ring buffer with a fixed capacity.

        Args:
            capacity: Maximum number of error entries to retain. When exceeded,
                      the oldest entries are automatically evicted. Defaults to 100.
        """
        # deque with maxlen ensures automatic eviction of oldest entries
        self._buffer: deque[Dict[str, Any]] = deque(maxlen=capacity)
        # Lock to ensure thread-safe reads and writes
        self._lock = threading.Lock()

    def add(self, tool: str, error_message: str, status_code: int = 0,
            duration_ms: int = 0, **extra: Any) -> None:
        """
        Record an error entry in the ring buffer.

        A new entry dictionary is constructed with the current UTC timestamp,
        tool name, error message, status code, and duration. Any additional
        keyword arguments are merged into the entry for extensibility.
            The entry is constructed first, then written under lock to minimise
            the critical section. This avoids holding the lock while building
            the entry dict.

        Args:
            tool: The name of the tool / method that produced the error.
            error_message: A human-readable description of the error.
            status_code: An optional numeric status code (e.g. HTTP status). Defaults to 0.
            duration_ms: An optional duration of the failed operation in milliseconds.
                         Defaults to 0.
            **extra: Any additional key-value pairs to include in the error entry.
                     Useful for attaching context like ``database_id``, ``chart_id``, etc.
        """
        # Build the error entry dict with standard fields
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "error_message": error_message,
            "status_code": status_code,
            "duration_ms": duration_ms,
        }
        # Merge any extra context fields into the entry
        entry.update(extra)
        # Append under lock to ensure thread safety
        with self._lock:
            self._buffer.append(entry)

    def get_all(self) -> List[Dict[str, Any]]:
        """
        Return a copy of all error entries currently in the buffer.

        The returned list is a snapshot taken while holding the lock. Modifications
        to the returned list do not affect the internal buffer.

        Returns:
            A list of error entry dictionaries, ordered from oldest to newest.
            The list will never exceed ``capacity`` entries.
        """
        # Acquire lock to get a thread-safe snapshot of the buffer
        with self._lock:
            return list(self._buffer)


# ── JSON log writer ────────────────────────────────────────────────────


class JsonLogFile:
    """
    Append-only structured JSON log file writer.

    This class opens a file in append mode and writes one JSON object per line
    (JSONL / newline-delimited JSON format). Each line is a serialisable dictionary
    converted via ``json.dumps()`` with ``default=str`` to handle non-serialisable
    types gracefully.
        The directory containing the log file is created automatically if it doesn't
        exist. This allows the caller to specify any valid file path without
        pre-creating parent directories. The file is opened and closed on each
        ``write()`` call, which is less efficient than keeping the file open but
        is safer for crash recovery (each line is fully flushed to disk).

    Examples:
        >>> writer = JsonLogFile("logs/metrics.jsonl")
        >>> writer.write({"tool": "create_chart", "status": "success"})
    """

    def __init__(self, path: str) -> None:
        """
        Initialise the JSON log file writer.
            The parent directory is created using ``os.makedirs(..., exist_ok=True)``
            so that the writer can be given a nested path without error.

        Args:
            path: The full file path where JSON lines will be appended.
        """
        # Create parent directories if they don't exist (idempotent)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path

    def write(self, entry: Dict[str, Any]) -> None:
        """
        Append a single structured JSON entry to the log file.

        The entry dictionary is serialised to a JSON string with ``default=str``
        to handle non-standard types, then a newline is appended and the line is
        written to the file in append mode.

        Args:
            entry: A dictionary containing the structured log data to write.
                   All values must be JSON-serialisable or convertible to string.
        """
        # Serialize the dict to JSON, converting any non-serialisable values to str
        line = json.dumps(entry, default=str) + "\n"
        # Open in append mode so each call adds one line without overwriting
        with open(self._path, "a") as f:
            f.write(line)


# ── Prometheus metric factories ────────────────────────────────────────


def _make_counter(name: str, labelnames: list[str]) -> Counter:
    """
    Factory for creating a Prometheus Counter metric.

    A Counter is a cumulative metric that represents a monotonically increasing
    value (e.g., number of tool calls, errors, resources created). Once incremented,
    the value can never decrease.

    Args:
        name: The unique name of the metric (e.g. ``"tool_calls_total"``).
        labelnames: A list of label names to dimension the counter (e.g.
                    ``["tool", "status"]``). Labels allow filtering and grouping
                    in Prometheus queries.

    Returns:
        A new ``prometheus_client.Counter`` instance.
    """
    return Counter(name, "", labelnames=labelnames)


def _make_histogram(name: str, labelnames: Optional[list[str]] = None) -> Histogram:
    """
    Factory for creating a Prometheus Histogram metric.

    A Histogram samples observations (typically durations or sizes) and counts them
    in configurable buckets, providing an aggregate count and sum for percentile
    calculations. The default bucket boundaries are tuned for tool execution times.

    Args:
        name: The unique name of the metric (e.g. ``"tool_duration_seconds"``).
        labelnames: Optional list of label names to dimension the histogram.
                    Defaults to an empty list.

    Returns:
        A new ``prometheus_client.Histogram`` instance with predefined buckets
        ranging from 0.1s to 60s.
    """
    # Buckets are tuned for typical tool execution times (0.1s to 60s)
    return Histogram(name, "", buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0], labelnames=labelnames or [])


# ── MetricsCollector ───────────────────────────────────────────────────


class MetricsCollector:
    """
    In-process metrics collector that writes to Prometheus and a JSON log file.

    ``MetricsCollector`` is the central observability component. It maintains:
    - Multiple Prometheus Counters for tracking events (tool calls, resources created,
      transfers, queries, charts, dashboards, API requests, DB queries).
    - A Prometheus Histogram for tracking tool execution duration.
    - An ``ErrorRingBuffer`` for the last 100 errors.
    - A ``JsonLogFile`` for structured JSON log persistence.

    The collector can be used via:
    - The ``tool_timing()`` context manager for synchronous code.
    - The ``tool_wrapper()`` async decorator for async functions.
    - Direct counter increment helpers.
    - Error recording for the ring buffer and log file.
        This class is designed as a singleton (accessed via ``get_metrics()``). Only
        one instance exists per process to avoid duplicate metric registration with
        Prometheus, which would raise an ``AlreadyRegisteredError``.

    Examples:
        >>> mc = MetricsCollector()
        >>> with mc.tool_timing("superset_create_chart", status="success"):
        ...     create_chart()
        >>> mc.increment_counter("resources_created", {"resource_type": "chart"})
    """

    def __init__(self, log_path: Optional[str] = None) -> None:
        """
        Initialise all Prometheus counters, the error buffer, and the JSON log file.

        This constructor creates every Prometheus metric that the system will use.
        Each counter is dimensioned with appropriate labels for filtering and
        aggregation in Prometheus/Grafana dashboards.

        Args:
            log_path: The file path for the structured JSON log. Defaults to
                      ``"logs/metrics.jsonl"`` if ``None``.
        """
        # ── Prometheus counters ────────────────────────────────────────
        # Total tool call count, labelled by tool name and execution status
        self.tool_calls = _make_counter(
            "tool_calls_total", ["tool", "status"]
        )
        # Total resources created (charts, dashboards, datasets), labelled by type
        self.resources_created = _make_counter(
            "resources_created_total", ["resource_type"]
        )
        # Total transfer operations, labelled by overall status
        self.transfers_total = _make_counter(
            "transfers_total", ["status"]
        )
        # Total items within transfers, labelled by item status and type (chart, dashboard, etc.)
        self.transfers_items = _make_counter(
            "transfers_items_total", ["status", "item_type"]
        )
        # Total SQL queries executed, labelled by the source system
        self.queries_executed = _make_counter(
            "queries_executed_total", ["source"]
        )
        # Total charts generated, labelled by chart type (bar, line, pie, etc.)
        self.charts_generated = _make_counter(
            "charts_generated_total", ["chart_type"]
        )
        # Total dashboards generated, labelled by layout type
        self.dashboards_generated = _make_counter(
            "dashboards_generated_total", ["layout_type"]
        )
        # Total API requests, labelled by HTTP method, endpoint, and status code
        self.api_requests = _make_counter(
            "api_requests_total", ["method", "endpoint", "status_code"]
        )
        # Total database queries, labelled by database type and status
        self.db_queries = _make_counter(
            "db_queries_total", ["db_type", "status"]
        )

        # ── Prometheus histogram ───────────────────────────────────────
        # Tool execution duration in seconds, labelled by tool name
        self.tool_duration = _make_histogram("tool_duration_seconds", ["tool"])

        # ── Infrastructure ─────────────────────────────────────────────
        # Ring buffer for the last 100 errors (thread-safe)
        self._errors = ErrorRingBuffer(capacity=100)
        # Append-only JSON log file for persistent structured logging
        self._json_log = JsonLogFile(log_path or "logs/metrics.jsonl")

    # ── Context manager for tool timing ────────────────────────────────

    class _tool_timing:
        """
        Internal context manager that tracks tool execution duration and success status.

        This inner class is used by ``MetricsCollector.tool_timing()``. When used
        as a context manager, it records the elapsed time on exit (both for success
        and error paths) and increments the appropriate Prometheus counters.
            The timer starts in ``__init__`` (when entering the ``with`` block) and
            stops in ``__exit__``. Both success and exception paths are handled
            uniformly: the tool call counter and duration histogram are always
            updated, and the JSON log file always receives a write.

        Examples:
            >>> mc = MetricsCollector()
            >>> with mc.tool_timing("create_chart", status="success"):
            ...     do_work()
            # On exit: increments tool_calls, observes duration, writes to JSON log
        """

        def __init__(self, collector: "MetricsCollector", tool: str,
                     status: str = "success") -> None:
            """
            Initialise the timing context manager.

            Args:
                collector: The parent ``MetricsCollector`` instance that owns this
                           timing context. Provides access to counters and log file.
                tool: The name of the tool being timed (used as a label in Prometheus).
                status: The expected status to record (e.g. ``"success"`` or ``"error"``).
            """
            self._collector = collector
            self._tool = tool
            self._status = status
            # Start a monotonic clock timer (unaffected by system clock changes)
            self._start = time.monotonic()

        def __enter__(self) -> "MetricsCollector._tool_timing":
            """
            Enter the context manager.

            Returns:
                The ``_tool_timing`` instance itself, allowing use as
                ``with mc.tool_timing(...) as timer:``.
            """
            return self

        def __exit__(self, *exc: Any) -> None:
            """
            Exit the context manager and record metrics.

            This method is called when the ``with`` block exits, regardless of
            whether an exception was raised. It calculates the elapsed time,
            increments the tool call counter, observes the duration in the
            histogram, and appends a JSON log entry.

            Args:
                *exc: Exception info tuple (type, value, traceback) if an exception
                      was raised inside the ``with`` block. Unused but required by
                      the context manager protocol.

            Returns:
                None. Returning a truthy value would suppress exception propagation.
            """
            # Calculate elapsed time in seconds using monotonic clock
            elapsed = time.monotonic() - self._start
            elapsed_ms = int(elapsed * 1000)

            # Increment the tool calls counter for this tool and status
            self._collector.tool_calls.labels(tool=self._tool, status=self._status).inc()
            # Record the duration in the histogram for percentile analysis
            self._collector.tool_duration.labels(tool=self._tool).observe(elapsed)

            # Write a structured JSON log entry with timing details
            self._collector._json_log.write({
                "ts": datetime.now(timezone.utc).isoformat(),
                "tool": self._tool,
                "status": self._status,
                "duration_ms": elapsed_ms,
            })

    def tool_timing(self, tool: str, status: str = "success"):
        """
        Return a context manager that records tool execution metrics.

        This is the recommended way to instrument synchronous tool functions.
        Wrap the tool body in a ``with`` block and the duration, call count,
        and JSON log entry will be recorded automatically on exit.
            The status label defaults to ``"success"``. If the tool raises an
            exception, the context manager still records the timing with the
            specified status (callers should set ``status="error"`` explicitly
            for error paths).

        Args:
            tool: The name of the tool being timed. Used as a label in Prometheus
                  metrics. Should match the MCP tool name for consistency.
            status: The status label to record (e.g. ``"success"``, ``"error"``).
                    Defaults to ``"success"``.

        Returns:
            A ``_tool_timing`` context manager instance.

        Examples:
            >>> mc = MetricsCollector()
            >>> with mc.tool_timing("superset_create_chart"):
            ...     result = create_chart()
            # Metrics recorded automatically on exit
        """
        return self._tool_timing(self, tool, status)

    # ── Decorator for async tools ──────────────────────────────────────

    def tool_wrapper(self, tool: str):
        """
        Async decorator that wraps a coroutine with metrics instrumentation.

        This decorator should be applied to async tool functions. It tracks:
        - Tool call count (success/error)
        - Execution duration histogram
        - JSON log file entry
        - Error recording (via the ring buffer) when an exception is raised
            The decorator uses ``time.monotonic()`` for accurate timing. The status
            is inferred from the return value (checks for ``"Error"`` substring)
            or from exceptions raised during execution. The ``finally`` block
            ensures metrics are recorded even when an exception propagates.

        Args:
            tool: The name of the tool being decorated. Used as a label in Prometheus.

        Returns:
            A wrapper coroutine that, when awaited, executes the original function
            and records metrics around the execution.

        Examples:
            >>> mc = MetricsCollector()
            >>> @mc.tool_wrapper("superset_get_chart_info")
            >>> async def get_chart_info(chart_id: int):
            ...     return await fetch_chart(chart_id)
        """
        def decorator(fn):
            """
            Inner decorator that wraps the async function.

            Args:
                fn: The async function to wrap with metrics instrumentation.

            Returns:
                An async wrapper coroutine that executes ``fn`` and records metrics.
            """
            async def wrapper(*args, **kwargs):
                """
                Async wrapper that executes the original function and records metrics.

                The wrapper tracks execution start time, catches exceptions,
                determines success/error status, and records all metrics in the
                finally block to ensure they are always captured.
                """
                # Record the monotonic start time for duration calculation
                start = time.monotonic()
                status = "success"
                try:
                    # Await the original async function
                    result = await fn(*args, **kwargs)
                    # Heuristic: if the result string contains "error", mark as error
                    if isinstance(result, str) and ("Error" in result or "error" in result.lower()):
                        status = "error"
                    return result
                except Exception as e:
                    # An exception occurred: mark the status as error and re-raise
                    status = "error"
                    raise
                finally:
                    # Always record metrics, even if an exception was raised
                    elapsed = time.monotonic() - start
                    # Increment the tool calls counter
                    self.tool_calls.labels(tool=tool, status=status).inc()
                    # Record duration in the histogram
                    self.tool_duration.labels(tool=tool).observe(elapsed)
                    # Write structured JSON log entry
                    self._json_log.write({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "tool": tool,
                        "status": status,
                        "duration_ms": int(elapsed * 1000),
                    })
                    # If an error occurred, also record it in the ring buffer
                    if status == "error":
                        self.record_error(tool=tool, error_message=str(e), duration_ms=int(elapsed * 1000))
            return wrapper
        return decorator

    # ── Counter helpers ────────────────────────────────────────────────

    def increment_counter(self, metric_name: str, labels: Dict[str, str]) -> None:
        """
        Increment a registered counter by its logical name.

        This helper provides a convenient way to increment metrics without
        accessing the internal counter attributes directly. It maps logical
        names (e.g. ``"resources_created"``) to the actual Prometheus counter
        objects.
            The mapping from logical names to counter attributes is defined in
            the ``counters`` dict. If an unknown name is passed, a ``ValueError``
            is raised to catch typos early.

        Args:
            metric_name: The logical name of the counter to increment. Must be
                         one of: ``"resources_created"``, ``"transfers"``,
                         ``"transfer_items"``, ``"queries_executed"``,
                         ``"charts_generated"``, ``"dashboards_generated"``,
                         ``"api_requests"``, ``"db_queries"``.
            labels: A dictionary of label name -> value pairs to apply to the
                    counter before incrementing. Must match the label names
                    defined when the counter was created.

        Raises:
            ValueError: If ``metric_name`` is not a registered metric.
        """
        # Map logical names to the actual Prometheus counter attributes
        counters = {
            "resources_created": self.resources_created,
            "transfers": self.transfers_total,
            "transfer_items": self.transfers_items,
            "queries_executed": self.queries_executed,
            "charts_generated": self.charts_generated,
            "dashboards_generated": self.dashboards_generated,
            "api_requests": self.api_requests,
            "db_queries": self.db_queries,
        }
        # Look up the counter by name; raise if not found
        metric = counters.get(metric_name)
        if metric is None:
            raise ValueError(f"Unknown metric: {metric_name}")
        # Apply the labels and increment the counter
        metric.labels(**labels).inc()

    # ── Error tracking ─────────────────────────────────────────────────

    def record_error(self, tool: str, error_message: str,
                     status_code: int = 0, duration_ms: int = 0) -> None:
        """
        Record an error in both the ring buffer and the JSON log file.

        This method is called when a tool execution fails. It adds the error
        to the in-memory ring buffer (for the ``/errors`` endpoint) and writes
        a structured JSON log entry (for persistent analysis).

        Args:
            tool: The name of the tool that produced the error.
            error_message: A human-readable description of the error.
            status_code: An optional numeric status code. Defaults to 0.
            duration_ms: The duration of the failed operation in milliseconds.
                         Defaults to 0.
        """
        # Add the error to the thread-safe ring buffer
        self._errors.add(tool=tool, error_message=error_message,
                         status_code=status_code, duration_ms=duration_ms)
        # Write a structured JSON log entry for persistent analysis
        self._json_log.write({
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "error_message": error_message,
            "status_code": status_code,
            "duration_ms": duration_ms,
        })

    # ── Accessors ──────────────────────────────────────────────────────

    def get_errors(self) -> list[dict]:
        """
        Retrieve all error entries from the ring buffer.

        This method is called by the ``/errors`` FastAPI endpoint to expose
        the most recent errors to monitoring dashboards and clients.

        Returns:
            A list of error entry dictionaries, ordered from oldest to newest.
            Each entry contains: timestamp, tool, error_message, status_code,
            and duration_ms.
        """
        # Return a thread-safe snapshot of the ring buffer
        return self._errors.get_all()


# ── Singleton instance ────────────────────────────────────────────────

# Global singleton instance (initially None; created lazily on first call)
_instance: Optional[MetricsCollector] = None
# Lock to ensure thread-safe singleton creation
_instance_lock = threading.Lock()


def get_metrics() -> MetricsCollector:
    """
    Get or create the singleton ``MetricsCollector`` instance.

    This function implements a thread-safe lazy initialisation pattern (double-checked
    locking). The first call creates the singleton; subsequent calls return the
    existing instance. This ensures only one set of Prometheus metrics exists in
    the process, avoiding ``AlreadyRegisteredError`` exceptions.

    Returns:
        The singleton ``MetricsCollector`` instance. Created lazily on first call
        with the default log path ``"logs/metrics.jsonl"``.
    """
    global _instance
    # Acquire lock for thread-safe singleton creation
    with _instance_lock:
        # Check again inside the lock in case another thread created it first
        if _instance is None:
            _instance = MetricsCollector(log_path="logs/metrics.jsonl")
        return _instance


def reset_metrics_for_testing() -> None:
    """
    Reset the singleton to ``None`` (tests only).

    This function is intended exclusively for unit tests that need a clean state
    between test runs. It should never be called in production code.
        After calling this function, the next call to ``get_metrics()`` will create
        a fresh ``MetricsCollector`` instance with fresh Prometheus counters.
        Existing Prometheus metrics will persist (they are process-global), but
        the in-memory ring buffer and JSON log writer will be reset.
    """
    global _instance
    with _instance_lock:
        _instance = None
