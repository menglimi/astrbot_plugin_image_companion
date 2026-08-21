# -*- coding: utf-8 -*-
"""Unified debug recorder for image generation.

The recorder deliberately has no dependency on the generation engine.  It is
safe to instantiate in legacy, shadow, and active modes and can therefore be
introduced before the large legacy runtime is migrated.  Events are appended
as JSONL records and large request/response bodies can be stored as per-trace
payload files.

Capture modes
-------------
``off``
    Do not create files and return ``None`` from recording methods.
``redacted``
    Keep diagnostic data while masking credential fields and common inline
    credential formats.
``full``
    Keep prompts, paths, session identifiers and request/response details;
    credential fields are still masked as a guard against accidental leaks.
``full_with_secrets``
    Preserve credential fields as well.  This mode requires
    ``include_secrets=True`` (or the equivalent mapping setting) so a caller
    must opt in twice before secrets are persisted.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CAPTURE_MODES = frozenset({"off", "redacted", "full", "full_with_secrets"})
_CREDENTIAL_KEY_PARTS = (
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "private_key",
    "secret",
    "token",
)
_CREDENTIAL_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:bearer|basic)\s+|(?:api[_-]?key|access[_-]?token|auth(?:orization)?|token|secret|password)\s*[:=]\s*)"
    r"(?P<value>[^\s,;\"']+)"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>\bauthorization\s*:\s*)(?:bearer|basic)\s+[^\s,;\"']+"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[?&](?:api[_-]?key|access[_-]?token|auth(?:orization)?|token|secret|password)=)"
    r"(?P<value>[^&\s]+)"
)
_JSON_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:api[_-]?key|apikey|access[_-]?token|auth(?:orization)?|token|secret|password)[\"']?\s*:\s*[\"'])"
    r"(?P<value>.*?)(?P<suffix>[\"'])"
)
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class GenerationDebugConfig:
    """Runtime options for :class:`GenerationDebugRecorder`.

    ``max_file_size_kb`` controls the main JSONL file and its backups.  A
    non-positive value disables file rotation but does not disable recording;
    use ``enabled=False`` or ``capture_mode='off'`` to disable recording.
    """

    enabled: bool = False
    capture_mode: str = "redacted"
    include_secrets: bool = False
    retention_days: int = 14
    max_file_size_kb: int = 10 * 1024
    backup_count: int = 5
    capture_payloads: bool = False
    max_body_kb: int = 256
    max_depth: int = 10
    max_items: int = 200
    max_string_chars: int = 12_000

    def normalized(self) -> "GenerationDebugConfig":
        mode = str(self.capture_mode or "redacted").strip().lower()
        if mode not in CAPTURE_MODES:
            mode = "redacted"
        return GenerationDebugConfig(
            enabled=bool(self.enabled) and mode != "off",
            capture_mode=mode,
            include_secrets=bool(self.include_secrets),
            retention_days=max(0, min(3650, int(self.retention_days or 0))),
            max_file_size_kb=max(0, min(1024 * 1024, int(self.max_file_size_kb or 0))),
            backup_count=max(0, min(100, int(self.backup_count or 0))),
            capture_payloads=bool(self.capture_payloads),
            max_body_kb=max(1, min(1024 * 1024, int(self.max_body_kb or 1))),
            max_depth=max(1, min(32, int(self.max_depth or 1))),
            max_items=max(1, min(10_000, int(self.max_items or 1))),
            max_string_chars=max(256, min(10_000_000, int(self.max_string_chars or 256))),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        defaults: "GenerationDebugConfig | None" = None,
    ) -> "GenerationDebugConfig":
        """Parse a config section without raising on malformed user values."""

        base = defaults or cls()
        raw = value if isinstance(value, Mapping) else {}

        def _int(name: str, fallback: int) -> int:
            try:
                return int(raw.get(name, fallback) or 0)
            except (TypeError, ValueError):
                return fallback

        def _bool(name: str, fallback: bool) -> bool:
            value = raw.get(name, fallback)
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"1", "true", "yes", "on", "enabled"}:
                    return True
                if lowered in {"0", "false", "no", "off", "disabled", ""}:
                    return False
            return fallback if value is None else bool(value)

        mode = raw.get("capture_mode", raw.get("mode", base.capture_mode))
        return cls(
            enabled=_bool("enabled", base.enabled),
            capture_mode=str(mode or base.capture_mode),
            include_secrets=_bool("include_secrets", base.include_secrets),
            retention_days=_int("retention_days", base.retention_days),
            max_file_size_kb=_int("max_file_size_kb", base.max_file_size_kb),
            backup_count=_int("backup_count", base.backup_count),
            capture_payloads=_bool("capture_payloads", base.capture_payloads),
            max_body_kb=_int("max_body_kb", base.max_body_kb),
            max_depth=_int("max_depth", base.max_depth),
            max_items=_int("max_items", base.max_items),
            max_string_chars=_int("max_string_chars", base.max_string_chars),
        ).normalized()


@dataclass(slots=True)
class _TraceState:
    trace_id: str
    request_id: str = ""
    started_at: float = field(default_factory=time.time)
    seq: int = 0
    event_count: int = 0
    last_status: str = "running"
    context: dict[str, Any] = field(default_factory=dict)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    finished_at: float | None = None


class GenerationDebugRecorder:
    """Thread-safe JSONL recorder shared by generation engine and adapters.

    A recorder instance may be kept on the plugin/runtime object.  Recording
    failures are intentionally swallowed and sent to the normal logger so a
    full disk or malformed diagnostic value cannot fail image generation.
    """

    _file_lock = threading.RLock()
    _MAX_STATE_COUNT = 512

    def __init__(
        self,
        data_dir: str | os.PathLike[str] | None = None,
        config: GenerationDebugConfig | Mapping[str, Any] | None = None,
        *,
        filename: str = "generation.jsonl",
        clock: Any = time.time,
    ) -> None:
        if isinstance(config, GenerationDebugConfig):
            normalized = config.normalized()
        else:
            normalized = GenerationDebugConfig.from_mapping(config)
        self.config = normalized
        self.data_dir = Path(data_dir or ".")
        self.root_dir = self.data_dir / "photo_debug"
        safe_name = _SAFE_FILENAME_RE.sub("_", str(filename or "generation.jsonl"))
        self.filename = safe_name if safe_name.endswith(".jsonl") else safe_name + ".jsonl"
        self.events_path = self.root_dir / self.filename
        self.traces_dir = self.root_dir / "traces"
        self._clock = clock
        self._states: dict[str, _TraceState] = {}
        self._state_lock = threading.RLock()
        self._pruned = False

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.capture_mode != "off")

    @property
    def effective_capture_mode(self) -> str:
        """Return the mode after applying the explicit secret opt-in gate."""

        if self.config.capture_mode == "full_with_secrets" and self.config.include_secrets:
            return "full_with_secrets"
        if self.config.capture_mode == "full_with_secrets":
            return "full"
        return self.config.capture_mode

    def start_trace(
        self,
        trace_id: str | None = None,
        *,
        request_id: str = "",
        context: Mapping[str, Any] | None = None,
        reset: bool = False,
        **metadata: Any,
    ) -> str:
        """Create or resume a trace and return its normalized identifier.

        A legacy bridge and the unified engine can both initialize the same
        request.  Starting an existing trace is therefore intentionally
        idempotent: its sequence, event count and original context are kept,
        while newly supplied context is merged. Callers that truly need a new
        trace with a reused identifier must opt in with ``reset=True``.
        """

        normalized = self._normalize_id(trace_id) or uuid.uuid4().hex
        if not self.enabled:
            return normalized
        now = self._now()
        initial_context: dict[str, Any] = {}
        if isinstance(context, Mapping):
            initial_context.update(context)
        initial_context.update(metadata)
        with self._state_lock:
            state = self._states.get(normalized)
            if state is not None and not reset:
                normalized_request = self._normalize_id(request_id)
                if normalized_request and not state.request_id:
                    state.request_id = normalized_request
                if initial_context:
                    state.context.update(self._sanitize(initial_context))
                # A resumed trace may have received a terminal engine event
                # before the legacy fallback continued. Keep it running until
                # the outermost caller explicitly finishes the request.
                if state.finished_at is not None:
                    state.finished_at = None
                    state.last_status = "running"
            else:
                self._prune_states_locked()
                self._states[normalized] = _TraceState(
                    trace_id=normalized,
                    request_id=self._normalize_id(request_id),
                    started_at=now,
                    context=self._sanitize(initial_context),
                )
        try:
            self._ensure_trace_dir(normalized)
            self._write_manifest(normalized)
            self._prune_once()
        except Exception as exc:  # pragma: no cover - defensive filesystem guard
            logger.debug("生图 debug trace 初始化失败: %s", exc)
        return normalized

    # Friendly aliases used by different call sites.
    begin_trace = start_trace

    def update_context(self, trace_id: str, **context: Any) -> bool:
        """Merge context into a trace for subsequent events."""

        if not self.enabled:
            return False
        normalized = self._normalize_id(trace_id)
        with self._state_lock:
            state = self._states.get(normalized)
            if state is None:
                self.start_trace(normalized, context=context)
                return True
            state.context.update(self._sanitize(context))
            return True

    def emit(
        self,
        trace_id: str,
        stage: str,
        *,
        status: str = "ok",
        severity: str | None = None,
        request_id: str = "",
        operation: str = "",
        workflow: str = "",
        backend: str = "",
        route: str = "",
        attempt: int | str | None = None,
        error_code: str = "",
        failure_stage: str = "",
        data: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        error: BaseException | Mapping[str, Any] | str | None = None,
        payloads: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any] | None:
        """Append one event and return the serialized envelope.

        ``data`` is intended for structured diagnostic values.  ``payloads``
        is intended for request/response bodies or workflow JSON that may be
        too large for the event line; each item is persisted separately and
        represented by a compact ``payloads`` map in the event.
        """

        if not self.enabled:
            return None
        normalized_trace = self._normalize_id(trace_id) or uuid.uuid4().hex
        normalized_stage = self._normalize_text(stage, 120) or "event"
        now = self._now()
        with self._state_lock:
            state = self._states.get(normalized_trace)
            if state is None:
                self._prune_states_locked()
                state = _TraceState(
                    trace_id=normalized_trace,
                    request_id=self._normalize_id(request_id),
                    started_at=now,
                )
                self._states[normalized_trace] = state
            if request_id and not state.request_id:
                state.request_id = self._normalize_id(request_id)
            if context:
                state.context.update(self._sanitize(context))
            state.seq += 1
            state.event_count += 1
            sequence = state.seq
            elapsed_ms = max(0, int((now - state.started_at) * 1000))
            event_context = copy.deepcopy(state.context)

        event_data: dict[str, Any] = {}
        if isinstance(data, Mapping):
            event_data.update(data)
        elif data is not None:
            event_data["value"] = data
        if extra:
            event_data.update(extra)
        sanitized_data = self._sanitize(event_data)
        event_payloads: dict[str, Any] = {}
        if isinstance(payloads, Mapping):
            for name, value in payloads.items():
                payload_meta = self.record_payload(
                    normalized_trace,
                    str(name),
                    value,
                    seq=sequence,
                )
                if payload_meta:
                    event_payloads[str(name)] = payload_meta
            if event_payloads:
                sanitized_data = dict(sanitized_data) if isinstance(sanitized_data, Mapping) else {"value": sanitized_data}
                sanitized_data["payloads"] = event_payloads

        error_data = self._error_details(error)
        if error_data:
            sanitized_data = dict(sanitized_data) if isinstance(sanitized_data, Mapping) else {"value": sanitized_data}
            sanitized_data["error"] = error_data
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "trace_id": normalized_trace,
            "request_id": self._normalize_id(request_id) or state.request_id,
            "seq": sequence,
            "ts": now,
            "time": datetime.fromtimestamp(now, tz=timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "elapsed_ms": elapsed_ms,
            "stage": normalized_stage,
            "status": self._normalize_text(status, 40) or "ok",
            "severity": self._normalize_text(severity or self._default_severity(status), 20),
            "operation": self._normalize_text(operation, 120),
            "workflow": self._normalize_text(workflow, 240),
            "backend": self._normalize_text(backend, 80),
            "route": self._normalize_text(route, 240),
            "attempt": self._normalize_attempt(attempt),
            "error_code": self._normalize_text(error_code, 120),
            "failure_stage": self._normalize_text(failure_stage, 120),
            "context": event_context,
            "data": sanitized_data,
        }
        # Empty optional fields make the JSONL easier to inspect programmatically
        # while avoiding a different schema for every stage.
        line = self._encode_event(event)
        self._append_line(line, event)
        terminal = event["status"] in {"completed", "failed", "error", "cancelled"} or normalized_stage in {
            "completed", "failed", "delivery_completed", "delivery_failed",
        }
        with self._state_lock:
            state.last_status = str(event["status"])
            if terminal:
                state.finished_at = now
                self._prune_states_locked(keep_trace_id=normalized_trace)
        if terminal:
            try:
                self._write_manifest(normalized_trace)
            except Exception as exc:  # pragma: no cover
                logger.debug("生图 debug manifest 写入失败: %s", exc)
        return event

    # Common names that make adapters easy to instrument.
    record = emit
    event = emit

    def record_payload(
        self,
        trace_id: str,
        name: str,
        value: Any,
        *,
        mime_type: str | None = None,
        sensitive: bool = False,
        seq: int | None = None,
    ) -> dict[str, Any] | None:
        """Persist a large body and return a metadata reference.

        In ``redacted``/``full`` modes raw credential-bearing payloads are not
        written.  Binary values are represented by hashes unless payload
        capture is explicitly enabled and the effective mode permits it.
        """

        if not self.enabled:
            return None
        normalized_trace = self._normalize_id(trace_id) or uuid.uuid4().hex
        safe_name = _SAFE_FILENAME_RE.sub("_", str(name or "payload"))[:100] or "payload"
        is_binary = isinstance(value, (bytes, bytearray, memoryview))
        # Sidecar bodies follow the same policy as event data.  Without this
        # step a captured JSON request could bypass key-based redaction even
        # though its event envelope was sanitized correctly.
        value_for_payload = value if self.effective_capture_mode == "full_with_secrets" or is_binary else self._sanitize(value)
        raw_bytes, encoding, value_for_text = self._payload_bytes(value_for_payload)
        if mime_type is None and not is_binary:
            mime_type = "application/json" if isinstance(value, (Mapping, list, tuple, set, frozenset)) else "text/plain"
        effective = self.effective_capture_mode
        reveal = effective == "full_with_secrets"
        should_write = bool(self.config.capture_payloads)
        if sensitive and not reveal:
            should_write = False
        if is_binary and not should_write:
            return self._payload_reference_only(
                normalized_trace,
                safe_name,
                raw_bytes,
                mime_type=mime_type,
                seq=seq,
                encoding=encoding,
                captured=False,
            )
        if not is_binary and not should_write:
            # Keep a useful, bounded preview in the event without creating a
            # sidecar file for every normal prompt.
            preview = self._sanitize(value_for_text)
            preview_bytes = self._encode_json_bytes(preview)
            return self._payload_reference_only(
                normalized_trace,
                safe_name,
                preview_bytes,
                mime_type=mime_type or "application/json",
                seq=seq,
                encoding="utf-8",
                captured=False,
                preview=preview,
            )
        if len(raw_bytes) > self.config.max_body_kb * 1024:
            # The hash remains stable and the metadata explicitly explains why
            # no body was captured.
            return self._payload_reference_only(
                normalized_trace,
                safe_name,
                raw_bytes,
                mime_type=mime_type,
                seq=seq,
                encoding=encoding,
                captured=False,
                truncated=True,
            )
        try:
            trace_dir = self._ensure_trace_dir(normalized_trace)
            payload_dir = trace_dir / "payloads"
            payload_dir.mkdir(parents=True, exist_ok=True)
            extension = self._payload_extension(mime_type, encoding, is_binary)
            suffix = f"_{seq}" if seq is not None else ""
            path = payload_dir / f"{safe_name}{suffix}{extension}"
            # Avoid overwriting a same-named payload produced by a separate
            # event when the caller does not supply a sequence.
            if path.exists():
                path = payload_dir / f"{safe_name}{suffix}_{uuid.uuid4().hex[:8]}{extension}"
            path.write_bytes(raw_bytes)
            metadata = self._payload_metadata(
                normalized_trace,
                safe_name,
                raw_bytes,
                mime_type=mime_type,
                seq=seq,
                encoding=encoding,
                captured=True,
                path=path,
            )
            with self._state_lock:
                state = self._states.setdefault(normalized_trace, _TraceState(normalized_trace))
                state.payloads.append(metadata)
            return metadata
        except Exception as exc:  # pragma: no cover - defensive filesystem guard
            logger.debug("生图 debug payload 写入失败: %s", exc)
            return self._payload_reference_only(
                normalized_trace,
                safe_name,
                raw_bytes,
                mime_type=mime_type,
                seq=seq,
                encoding=encoding,
                captured=False,
            )

    def finish_trace(
        self,
        trace_id: str,
        *,
        status: str = "completed",
        stage: str = "completed",
        data: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Record a terminal event and retain the manifest for UI readers."""

        event = self.emit(trace_id, stage, status=status, data=data, **kwargs)
        normalized = self._normalize_id(trace_id)
        if self.enabled:
            with self._state_lock:
                state = self._states.get(normalized)
                if state is not None and state.finished_at is None:
                    state.finished_at = self._now()
            try:
                self._write_manifest(normalized)
            except Exception as exc:  # pragma: no cover
                logger.debug("生图 debug manifest 更新失败: %s", exc)
        return event

    end_trace = finish_trace

    def read_events(
        self,
        *,
        limit: int = 100,
        trace_id: str | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """Read recent valid event envelopes from the current and rotated files."""

        if limit <= 0:
            return []
        paths = [self.events_path]
        for index in range(1, self.config.backup_count + 1):
            paths.append(self.events_path.with_name(f"{self.events_path.stem}.{index}{self.events_path.suffix}"))
        rows: list[dict[str, Any]] = []
        normalized_trace = self._normalize_id(trace_id)
        for path in paths:
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(item, dict):
                            continue
                        if normalized_trace and str(item.get("trace_id") or "") != normalized_trace:
                            continue
                        rows.append(item)
            except (OSError, UnicodeError):
                continue
        rows.sort(key=lambda item: (float(item.get("ts") or 0), int(item.get("seq") or 0)))
        if newest_first:
            rows.reverse()
        return rows[:limit]

    def read_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.read_events(limit=limit, newest_first=True)

    def get_trace_state(self, trace_id: str) -> dict[str, Any] | None:
        normalized = self._normalize_id(trace_id)
        with self._state_lock:
            state = self._states.get(normalized)
            if state is None:
                return None
            return {
                "trace_id": state.trace_id,
                "request_id": state.request_id,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "seq": state.seq,
                "event_count": state.event_count,
                "last_status": state.last_status,
                "context": copy.deepcopy(state.context),
                "payloads": copy.deepcopy(state.payloads),
            }

    def _append_line(self, line: str, event: Mapping[str, Any]) -> None:
        encoded = line.encode("utf-8")
        max_bytes = self.config.max_file_size_kb * 1024
        if max_bytes > 0 and len(encoded) > max_bytes:
            compact = dict(event)
            compact["context"] = {"truncated": True, "reason": "event_exceeds_max_size"}
            compact["data"] = {"truncated": True, "reason": "event_exceeds_max_size", "original_bytes": len(encoded)}
            line = self._encode_event(compact)
            encoded = line.encode("utf-8")
        try:
            with self._file_lock:
                self.root_dir.mkdir(parents=True, exist_ok=True)
                if max_bytes > 0:
                    current_size = self.events_path.stat().st_size if self.events_path.exists() else 0
                    if current_size and current_size + len(encoded) > max_bytes:
                        self._rotate_files()
                with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
        except Exception as exc:  # pragma: no cover - logging must not break generation
            logger.debug("生图 debug event 写入失败: %s", exc)

    def _prune_states_locked(self, *, keep_trace_id: str = "") -> None:
        """Bound in-memory trace metadata without dropping active requests."""
        if len(self._states) <= self._MAX_STATE_COUNT:
            return
        finished = sorted(
            (
                state.finished_at or float("inf"),
                state.started_at,
                trace_id,
            )
            for trace_id, state in self._states.items()
            if state.finished_at is not None and trace_id != keep_trace_id
        )
        remove_count = len(self._states) - self._MAX_STATE_COUNT
        for _finished_at, _started_at, trace_id in finished[:remove_count]:
            self._states.pop(trace_id, None)

    def _rotate_files(self) -> None:
        backup_count = self.config.backup_count
        if backup_count <= 0:
            try:
                self.events_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        for index in range(backup_count, 0, -1):
            source = self.events_path if index == 1 else self.events_path.with_name(
                f"{self.events_path.stem}.{index - 1}{self.events_path.suffix}"
            )
            target = self.events_path.with_name(f"{self.events_path.stem}.{index}{self.events_path.suffix}")
            try:
                if source.exists():
                    os.replace(source, target)
            except OSError:
                logger.debug("生图 debug 日志轮转失败: %s", source)

    def _ensure_trace_dir(self, trace_id: str) -> Path:
        path = self.traces_dir / self._safe_trace_id(trace_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_manifest(self, trace_id: str) -> None:
        if not self.enabled:
            return
        normalized = self._normalize_id(trace_id)
        with self._state_lock:
            state = self._states.get(normalized)
            if state is None:
                return
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "trace_id": state.trace_id,
                "request_id": state.request_id,
                "started_at": state.started_at,
                "started_time": self._iso(state.started_at),
                "finished_at": state.finished_at,
                "finished_time": self._iso(state.finished_at) if state.finished_at else "",
                "event_count": state.event_count,
                "seq": state.seq,
                "last_status": state.last_status,
                "context": copy.deepcopy(state.context),
                "payloads": copy.deepcopy(state.payloads),
            }
        path = self._ensure_trace_dir(normalized) / "manifest.json"
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _prune_once(self) -> None:
        if self._pruned or self.config.retention_days <= 0:
            self._pruned = True
            return
        self._pruned = True
        cutoff = self._now() - self.config.retention_days * 86400
        if not self.traces_dir.exists():
            return
        try:
            for path in self.traces_dir.iterdir():
                try:
                    if path.stat().st_mtime < cutoff:
                        if path.is_dir():
                            for child in sorted(path.rglob("*"), reverse=True):
                                if child.is_file() or child.is_symlink():
                                    child.unlink(missing_ok=True)
                                elif child.is_dir():
                                    child.rmdir()
                            path.rmdir()
                        else:
                            path.unlink(missing_ok=True)
                except OSError:
                    continue
        except OSError:
            return

    def _error_details(self, error: BaseException | Mapping[str, Any] | str | None) -> dict[str, Any]:
        if error is None:
            return {}
        if isinstance(error, BaseException):
            details: dict[str, Any] = {
                "type": type(error).__name__,
                "message": str(error),
                "args": list(error.args),
            }
            formatted = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            if formatted:
                details["traceback"] = formatted
            return self._sanitize(details)
        if isinstance(error, Mapping):
            return self._sanitize(dict(error))
        return {"type": "Error", "message": self._sanitize(str(error))}

    def _sanitize(self, value: Any, *, key: str = "", depth: int = 0) -> Any:
        if depth > self.config.max_depth:
            return "[truncated: max_depth]"
        normalized_key = str(key or "").strip().lower().replace("-", "_")
        if self._is_credential_key(normalized_key) and self.effective_capture_mode != "full_with_secrets":
            return "***"
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for index, (item_key, item_value) in enumerate(value.items()):
                if index >= self.config.max_items:
                    result["__truncated__"] = f"max_items:{self.config.max_items}"
                    break
                safe_key = self._normalize_text(item_key, 200) or "key"
                result[safe_key] = self._sanitize(item_value, key=safe_key, depth=depth + 1)
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            result = [self._sanitize(item, depth=depth + 1) for item in items[: self.config.max_items]]
            if len(items) > self.config.max_items:
                result.append(f"[truncated: max_items:{self.config.max_items}]")
            return result
        if isinstance(value, bytes):
            return {
                "type": "bytes",
                "size": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        if isinstance(value, (bytearray, memoryview)):
            raw = bytes(value)
            return {"type": "bytes", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if isinstance(value, str):
            text = value
            if self.effective_capture_mode != "full_with_secrets":
                text = _redact_inline_secrets(text)
            if len(text) > self.config.max_string_chars:
                return text[: self.config.max_string_chars] + "...[truncated]"
            return text
        if value is None or isinstance(value, (bool, int, float)):
            return value
        try:
            return self._sanitize(str(value), key=key, depth=depth + 1)
        except Exception:
            return "[unserializable]"

    @staticmethod
    def _is_credential_key(key: str) -> bool:
        compact = key.replace("_", "").replace("-", "")
        return any(part.replace("_", "") in compact for part in _CREDENTIAL_KEY_PARTS)

    @staticmethod
    def _normalize_text(value: Any, limit: int) -> str:
        if value is None:
            return ""
        text = str(value).replace("\x00", "")
        text = " ".join(text.splitlines())
        return text[:limit]

    @staticmethod
    def _normalize_id(value: Any) -> str:
        return GenerationDebugRecorder._normalize_text(value, 240).strip()

    @staticmethod
    def _safe_trace_id(value: str) -> str:
        return _SAFE_FILENAME_RE.sub("_", value)[:180] or "trace"

    @staticmethod
    def _normalize_attempt(value: Any) -> int | str | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return GenerationDebugRecorder._normalize_text(value, 80)

    @staticmethod
    def _default_severity(status: Any) -> str:
        normalized = str(status or "ok").lower()
        if normalized in {"failed", "error", "cancelled"}:
            return "error"
        if normalized in {"warning", "degraded", "fallback"}:
            return "warning"
        return "info"

    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            return time.time()

    @staticmethod
    def _iso(value: float | None) -> str:
        return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().isoformat(timespec="milliseconds") if value else ""

    @staticmethod
    def _encode_event(event: Mapping[str, Any]) -> str:
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"

    @staticmethod
    def _payload_bytes(value: Any) -> tuple[bytes, str, Any]:
        if isinstance(value, bytes):
            return value, "binary", value
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value), "binary", bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8"), "utf-8", value
        try:
            return GenerationDebugRecorder._encode_json_bytes(value), "utf-8", value
        except (TypeError, ValueError):
            return str(value).encode("utf-8", "replace"), "utf-8", str(value)

    @staticmethod
    def _encode_json_bytes(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")

    @staticmethod
    def _payload_extension(mime_type: str | None, encoding: str, is_binary: bool) -> str:
        if is_binary:
            extension = mimetypes.guess_extension(mime_type or "") or ".bin"
            return extension if extension.startswith(".") else "." + extension
        return ".json" if mime_type == "application/json" else ".txt"

    def _payload_metadata(
        self,
        trace_id: str,
        name: str,
        raw_bytes: bytes,
        *,
        mime_type: str | None,
        seq: int | None,
        encoding: str,
        captured: bool,
        path: Path | None = None,
        truncated: bool = False,
        preview: Any = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "name": name,
            "trace_id": trace_id,
            "seq": seq,
            "mime_type": mime_type or "application/octet-stream",
            "encoding": encoding,
            "size": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "captured": captured,
        }
        if path is not None:
            try:
                metadata["path"] = str(path.relative_to(self.root_dir)).replace("\\", "/")
            except ValueError:
                metadata["path"] = str(path)
        if truncated:
            metadata["truncated"] = True
        if preview is not None:
            metadata["preview"] = preview
        return metadata

    def _payload_reference_only(
        self,
        trace_id: str,
        name: str,
        raw_bytes: bytes,
        *,
        mime_type: str | None,
        seq: int | None,
        encoding: str,
        captured: bool,
        truncated: bool = False,
        preview: Any = None,
    ) -> dict[str, Any]:
        metadata = self._payload_metadata(
            trace_id,
            name,
            raw_bytes,
            mime_type=mime_type,
            seq=seq,
            encoding=encoding,
            captured=captured,
            truncated=truncated,
            preview=preview,
        )
        with self._state_lock:
            state = self._states.setdefault(trace_id, _TraceState(trace_id))
            state.payloads.append(metadata)
        return metadata


def _redact_inline_secrets(value: str) -> str:
    """Mask common credential forms while preserving surrounding diagnostics."""

    value = _AUTH_HEADER_RE.sub(lambda match: match.group("prefix") + "***", value)
    value = _CREDENTIAL_RE.sub(lambda match: match.group("prefix") + "***", value)
    value = _QUERY_SECRET_RE.sub(lambda match: match.group("prefix") + "***", value)
    return _JSON_SECRET_RE.sub(
        lambda match: match.group("prefix") + "***" + match.group("suffix"),
        value,
    )


def redact(value: Any) -> Any:
    """Small public helper for callers that need one-off redaction."""

    config = GenerationDebugConfig(enabled=True, capture_mode="redacted")
    recorder = GenerationDebugRecorder(config=config)
    return recorder._sanitize(value)


__all__ = [
    "CAPTURE_MODES",
    "SCHEMA_VERSION",
    "GenerationDebugConfig",
    "GenerationDebugRecorder",
    "redact",
]
