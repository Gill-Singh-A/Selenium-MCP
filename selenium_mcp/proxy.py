"""Embedded mitmproxy capture — a Burp-style proxy history for the browser session.

Rather than shelling out to ``mitmdump``, a mitmproxy ``DumpMaster`` runs inside this
process on its own asyncio event loop in a daemon thread. ``start_browser`` points the
browser at it, so every request/response the browser makes — including HTTPS, which is
decrypted using mitmproxy's own CA — is recorded into an in-memory ring buffer that the
traffic tools list, inspect, search, replay and export as HAR.

Nothing here writes to stdout: the MCP server speaks its protocol over stdio, so stray
output would corrupt it. mitmproxy's terminal log and flow dumper are both disabled and
its warnings are collected into a buffer that ``proxy_status`` reports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_MAX_BODY_BYTES = 200_000
DEFAULT_MAX_FLOWS = 2000
DEFAULT_HOST = "127.0.0.1"

# Marker used to tag replayed ("Repeater") requests. The addon strips it before the
# request leaves the proxy, so the target never sees it.
REPLAY_HEADER = "x-selenium-mcp-replay"

# Hop-by-hop / recomputed headers that must not be copied verbatim on replay.
_REPLAY_SKIP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
}


class ProxyError(RuntimeError):
    """Raised for user-facing proxy problems (not running, bad filter, bind failure…)."""


# ---------------------------------------------------------------------------
# Flow records
# ---------------------------------------------------------------------------
def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _headers_to_pairs(headers) -> list[list[str]]:
    """Preserve repeated headers (Set-Cookie, …) that a dict would collapse."""
    return [[k, v] for k, v in headers.items(multi=True)]


def _header_value(pairs: list[list[str]], name: str) -> Optional[str]:
    lowered = name.lower()
    for k, v in pairs:
        if k.lower() == lowered:
            return v
    return None


def _body_info(message, limit: int) -> dict:
    """Decode a request/response body, recording its true size and truncation.

    ``message.content`` is the body after any content-encoding (gzip/br) is undone.
    Bodies that are not valid text for their charset are flagged binary and dropped
    rather than stored as mojibake; their size is still reported.
    """
    try:
        content = message.content
    except (ValueError, TypeError):
        content = None
    if content is None:
        content = message.raw_content
    if not content:
        return {"body": None, "size": 0, "truncated": False, "binary": False}

    size = len(content)
    try:
        text = message.get_text(strict=True)
    except (ValueError, TypeError, UnicodeDecodeError):
        text = None
    if text is None:
        return {"body": None, "size": size, "truncated": False, "binary": True}
    if len(text) > limit:
        return {"body": text[:limit], "size": size, "truncated": True, "binary": False}
    return {"body": text, "size": size, "truncated": False, "binary": False}


@dataclass
class Flow:
    """One captured HTTP transaction."""

    id: int
    started_at: float
    method: str
    url: str
    host: str
    path: str
    scheme: str
    http_version: str
    request: dict
    response: Optional[dict] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    replayed: bool = False
    replay_token: Optional[str] = None

    # -- derived views -----------------------------------------------------
    @property
    def status_code(self) -> Optional[int]:
        return self.response["status"] if self.response else None

    @property
    def content_type(self) -> str:
        if not self.response:
            return ""
        return (_header_value(self.response["headers"], "content-type") or "").split(";")[0]

    @property
    def response_size(self) -> int:
        return self.response["size"] if self.response else 0

    def matches_text(self, needle: str, where: str = "any") -> bool:
        """Case-insensitive substring search across the parts named by `where`."""
        needle = needle.lower()
        req, resp = self.request, self.response

        def hdr_text(pairs) -> str:
            return "\n".join(f"{k}: {v}" for k, v in pairs)

        haystacks: list[str] = []
        if where in ("any", "url"):
            haystacks.append(self.url)
        if where in ("any", "headers"):
            haystacks.append(hdr_text(req["headers"]))
            if resp:
                haystacks.append(hdr_text(resp["headers"]))
        if where in ("any", "body", "request_body"):
            haystacks.append(req["body"] or "")
        if where in ("any", "body", "response_body"):
            if resp:
                haystacks.append(resp["body"] or "")
        return any(needle in h.lower() for h in haystacks)

    def to_dict(self, include_bodies: bool = True, max_body_chars: Optional[int] = None) -> dict:
        def body_of(part: dict) -> dict:
            out = {k: v for k, v in part.items() if k != "body"}
            if include_bodies:
                body = part["body"]
                if body is not None and max_body_chars is not None and len(body) > max_body_chars:
                    body = body[:max_body_chars]
                    out["truncated"] = True
                out["body"] = body
            return out

        data: dict[str, Any] = {
            "id": self.id,
            "started_at": _iso(self.started_at),
            "method": self.method,
            "url": self.url,
            "host": self.host,
            "path": self.path,
            "duration_ms": self.duration_ms,
            "request": body_of(self.request),
        }
        if self.response:
            data["response"] = body_of(self.response)
        if self.error:
            data["error"] = self.error
        if self.replayed:
            data["replayed"] = True
        return data


# ---------------------------------------------------------------------------
# Flow store
# ---------------------------------------------------------------------------
class FlowStore:
    """Thread-safe ring buffer of captured flows.

    The mitmproxy addon writes from the proxy thread while MCP tools read from the
    server thread, so every access takes the lock; the same lock is a Condition so
    ``wait_for`` can block until matching traffic arrives instead of polling.
    """

    def __init__(self, max_flows: int = DEFAULT_MAX_FLOWS) -> None:
        self._cond = threading.Condition()
        self._flows: deque[Flow] = deque(maxlen=max_flows)
        self._next_id = 1
        self._dropped = 0
        self._total = 0

    def set_max_flows(self, max_flows: int) -> None:
        with self._cond:
            kept = deque(self._flows, maxlen=max_flows)
            self._dropped += len(self._flows) - len(kept)
            self._flows = kept

    @property
    def max_flows(self) -> int:
        return self._flows.maxlen or 0

    def add(self, flow: Flow) -> Flow:
        with self._cond:
            flow.id = self._next_id
            self._next_id += 1
            self._total += 1
            if self._flows.maxlen and len(self._flows) == self._flows.maxlen:
                self._dropped += 1
            self._flows.append(flow)
            self._cond.notify_all()
            return flow

    def all(self) -> list[Flow]:
        with self._cond:
            return list(self._flows)

    def last_id(self) -> int:
        with self._cond:
            return self._next_id - 1

    def get(self, flow_id: int) -> Optional[Flow]:
        with self._cond:
            for f in self._flows:
                if f.id == flow_id:
                    return f
            return None

    def by_replay_token(self, token: str) -> Optional[Flow]:
        with self._cond:
            for f in reversed(self._flows):
                if f.replay_token == token:
                    return f
            return None

    def select(
        self,
        *,
        host: Optional[str] = None,
        method: Optional[str] = None,
        status: Optional[int] = None,
        url_contains: Optional[str] = None,
        content_type: Optional[str] = None,
        since_id: Optional[int] = None,
        errors_only: bool = False,
        predicate: Optional[Callable[[Flow], bool]] = None,
    ) -> list[Flow]:
        with self._cond:
            flows = list(self._flows)
        out = []
        for f in flows:
            if host and host.lower() not in f.host.lower():
                continue
            if method and f.method.upper() != method.upper():
                continue
            if status is not None and f.status_code != status:
                continue
            if url_contains and url_contains.lower() not in f.url.lower():
                continue
            if content_type and content_type.lower() not in f.content_type.lower():
                continue
            if since_id is not None and f.id <= since_id:
                continue
            if errors_only and not f.error:
                continue
            if predicate and not predicate(f):
                continue
            out.append(f)
        return out

    def wait_for(
        self,
        predicate: Callable[[Flow], bool],
        timeout: float,
        after_id: int = 0,
    ) -> Optional[Flow]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for f in self._flows:
                    if f.id > after_id and predicate(f):
                        return f
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def clear(self) -> int:
        with self._cond:
            n = len(self._flows)
            self._flows.clear()
            self._dropped = 0
            return n

    def stats(self) -> dict:
        flows = self.all()
        by_host: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        bytes_down = 0
        for f in flows:
            by_host[f.host] = by_host.get(f.host, 0) + 1
            key = str(f.status_code) if f.status_code else ("error" if f.error else "pending")
            by_status[key] = by_status.get(key, 0) + 1
            if f.content_type:
                by_type[f.content_type] = by_type.get(f.content_type, 0) + 1
            bytes_down += f.response_size
        return {
            "stored": len(flows),
            "captured_total": self._total,
            "dropped_oldest": self._dropped,
            "capacity": self.max_flows,
            "response_bytes": bytes_down,
            "by_host": dict(sorted(by_host.items(), key=lambda kv: -kv[1])),
            "by_status": dict(sorted(by_status.items())),
            "by_content_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        }


# ---------------------------------------------------------------------------
# mitmproxy addon
# ---------------------------------------------------------------------------
class CaptureAddon:
    """Records each flow into the store; optionally narrowed by a mitmproxy filter."""

    def __init__(
        self,
        store: FlowStore,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        capture_filter: Optional[str] = None,
        ready: Optional[threading.Event] = None,
    ) -> None:
        self.store = store
        self.max_body_bytes = max_body_bytes
        self.filter_expr = capture_filter
        self._matcher = None
        self._ready = ready
        if capture_filter:
            from mitmproxy import flowfilter

            self._matcher = flowfilter.parse(capture_filter)  # ValueError if invalid

    # -- mitmproxy hooks ---------------------------------------------------
    def running(self) -> None:
        if self._ready is not None:
            self._ready.set()

    def request(self, flow) -> None:
        """Strip the replay marker so the target never sees it, remembering the token."""
        token = flow.request.headers.pop(REPLAY_HEADER, None)
        if token:
            flow.metadata["selenium_mcp_replay"] = token

    def response(self, flow) -> None:
        self._record(flow)

    def error(self, flow) -> None:
        # Connection reset, TLS failure, DNS error… Burp shows these too.
        self._record(flow)

    # -- recording ---------------------------------------------------------
    def _record(self, flow) -> None:
        try:
            if self._matcher is not None:
                from mitmproxy import flowfilter

                if not flowfilter.match(self._matcher, flow):
                    return
            if getattr(flow, "request", None) is None:
                return
            self.store.add(self._build(flow))
        except Exception as exc:  # noqa: BLE001 - logging must never break proxying
            logging.getLogger("selenium_mcp.proxy").warning("capture failed: %s", exc)

    def _build(self, flow) -> Flow:
        req = flow.request
        resp = getattr(flow, "response", None)

        duration_ms = None
        if resp is not None and resp.timestamp_end and req.timestamp_start:
            duration_ms = int((resp.timestamp_end - req.timestamp_start) * 1000)

        token = flow.metadata.get("selenium_mcp_replay")
        record = Flow(
            id=0,  # assigned by the store
            started_at=req.timestamp_start or time.time(),
            method=req.method,
            url=req.pretty_url,
            host=req.pretty_host,
            path=req.path,
            scheme=req.scheme,
            http_version=req.http_version,
            request={
                "headers": _headers_to_pairs(req.headers),
                "query": [[k, v] for k, v in req.query.items(multi=True)],
                **_body_info(req, self.max_body_bytes),
            },
            duration_ms=duration_ms,
            error=str(flow.error) if getattr(flow, "error", None) else None,
            replayed=bool(token),
            replay_token=token,
        )
        if resp is not None:
            record.response = {
                "status": resp.status_code,
                "reason": resp.reason or "",
                "http_version": resp.http_version,
                "headers": _headers_to_pairs(resp.headers),
                **_body_info(resp, self.max_body_bytes),
            }
        return record


class _LogCollector(logging.Handler):
    """Keeps mitmproxy's warnings/errors so proxy_status can surface them."""

    def __init__(self, maxlen: int = 50) -> None:
        super().__init__(level=logging.WARNING)
        self.records: deque[str] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(f"{record.levelname}: {record.getMessage()}")
        except Exception:  # noqa: BLE001 - a logging handler must not raise
            pass


# ---------------------------------------------------------------------------
# Proxy controller
# ---------------------------------------------------------------------------
def _free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _default_confdir() -> Path:
    env = os.environ.get("SELENIUM_MCP_MITM_CONFDIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".selenium-mcp" / "mitmproxy"


class ProxyController:
    """Owns the embedded mitmproxy instance and the captured flow history.

    The flow store outlives any single proxy run, so traffic stays readable after
    the browser and proxy have been shut down.
    """

    def __init__(self) -> None:
        self.store = FlowStore()
        self.host = DEFAULT_HOST
        self.port: Optional[int] = None
        self.confdir: Path = _default_confdir()
        self.capture_filter: Optional[str] = None
        self.max_body_bytes = DEFAULT_MAX_BODY_BYTES
        self.started_at: Optional[float] = None
        self._master = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None
        self._logs = _LogCollector()
        self._prev_mitm_level: Optional[int] = None

    # -- state -------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._master is not None

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ca_cert_path(self) -> Path:
        return self.confdir / "mitmproxy-ca-cert.pem"

    def recent_logs(self) -> list[str]:
        return list(self._logs.records)

    # -- lifecycle ---------------------------------------------------------
    def start(
        self,
        port: Optional[int] = None,
        host: str = DEFAULT_HOST,
        capture_filter: Optional[str] = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_flows: int = DEFAULT_MAX_FLOWS,
        verify_upstream_certs: bool = False,
        startup_timeout: float = 30.0,
    ) -> dict:
        if self.is_running:
            raise ProxyError(
                f"Proxy already running on {self.proxy_url}. Call stop_proxy first."
            )

        try:
            from mitmproxy import options as mitm_options
            from mitmproxy.tools.dump import DumpMaster
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise ProxyError(
                "mitmproxy is not installed. Install it with: pip install mitmproxy"
            ) from exc

        self.host = host
        self.port = port or _free_port(host)
        self.capture_filter = capture_filter
        self.max_body_bytes = max_body_bytes
        self.store.set_max_flows(max_flows)
        self.confdir.mkdir(parents=True, exist_ok=True)
        self._error = None

        # Validate the filter here so a bad expression fails the call, not the thread.
        ready = threading.Event()
        try:
            addon = CaptureAddon(
                self.store,
                max_body_bytes=max_body_bytes,
                capture_filter=capture_filter,
                ready=ready,
            )
        except ValueError as exc:
            raise ProxyError(
                f"Invalid capture_filter {capture_filter!r}: {exc}. Examples: "
                "'~d example.com' (domain), '~u /api/' (URL), '~m POST' (method), "
                "'~d example.com & ~m POST' (both), '!~d ads.example' (exclude)."
            ) from exc

        logging.getLogger().addHandler(self._logs)
        # mitmproxy logs every connect/disconnect at INFO. The MCP client would see
        # all of it on stderr, so keep mitmproxy's own loggers at WARNING; genuine
        # problems still reach the collector and `proxy_status`.
        mitm_log = logging.getLogger("mitmproxy")
        self._prev_mitm_level = mitm_log.level
        mitm_log.setLevel(logging.WARNING)

        def run() -> None:
            async def amain() -> None:
                opts = mitm_options.Options(listen_host=host, listen_port=self.port)
                master = DumpMaster(opts, with_termlog=False, with_dumper=False)
                master.options.update(
                    confdir=str(self.confdir),
                    ssl_insecure=not verify_upstream_certs,
                )
                master.addons.add(addon)
                self._master = master
                self._loop = asyncio.get_running_loop()
                await master.run()

            try:
                asyncio.run(amain())
            except BaseException as exc:  # noqa: BLE001 - reported via start()
                self._error = exc
            finally:
                self._master = None
                self._loop = None
                ready.set()  # never leave start() waiting

        self._thread = threading.Thread(
            target=run, name="selenium-mcp-mitmproxy", daemon=True
        )
        self._thread.start()

        if not ready.wait(startup_timeout) or self._error or not self._wait_until_listening(
            startup_timeout
        ):
            # mitmproxy reports a bind failure through the log before exiting, so
            # prefer that over the bare SystemExit the thread sees.
            logged = [r for r in self._logs.records if r.startswith(("ERROR", "CRITICAL"))]
            self.stop()
            # First line only: mitmproxy's follow-up hint is mitmdump CLI advice.
            detail = (
                logged[-1].split(": ", 1)[-1].splitlines()[0]
                if logged
                else str(self._error or "")
            )
            raise ProxyError(
                f"Proxy failed to start on {host}:{self.port}."
                + (f" {detail}" if detail else " Is the port already in use?")
            )

        self.started_at = time.time()
        return {
            "proxy": self.proxy_url,
            "ca_cert": str(self.ca_cert_path),
            "capture_filter": capture_filter,
            "max_body_bytes": max_body_bytes,
            "max_flows": max_flows,
        }

    def _wait_until_listening(self, timeout: float) -> bool:
        """Confirm the listener really bound — the `running` hook fires either way."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._error:
                return False
            try:
                with socket.create_connection((self.host, self.port), timeout=1):
                    return True
            except OSError:
                time.sleep(0.1)
        return False

    def stop(self) -> str:
        master, loop, thread = self._master, self._loop, self._thread
        if master is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(master.shutdown)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(timeout=10)
        self._master = None
        self._loop = None
        self._thread = None
        self.started_at = None
        logging.getLogger().removeHandler(self._logs)
        if self._prev_mitm_level is not None:
            logging.getLogger("mitmproxy").setLevel(self._prev_mitm_level)
            self._prev_mitm_level = None
        if thread is None:
            return "No proxy was running."
        return "Proxy stopped (captured traffic is retained)."

    # -- replay ("Repeater") ----------------------------------------------
    def replay(
        self,
        flow: Flow,
        *,
        method: Optional[str] = None,
        url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        body: Optional[str] = None,
        remove_headers: Optional[list[str]] = None,
        follow_redirects: bool = False,
        timeout: float = 30.0,
    ) -> dict:
        """Re-send a captured request, optionally modified, through the proxy.

        Routing it through the proxy means the replay lands in the history too, so it
        can be inspected like any other flow.
        """
        if not self.is_running:
            raise ProxyError("Proxy is not running. Start it (or the browser) first.")

        target_url = url or flow.url
        target_method = (method or flow.method).upper()

        drop = {h.lower() for h in (remove_headers or [])} | _REPLAY_SKIP_HEADERS
        out_headers: dict[str, str] = {}
        for name, value in flow.request["headers"]:
            if name.lower() in drop:
                continue
            out_headers[name] = value
        for name, value in (headers or {}).items():
            if value is None:
                out_headers.pop(name, None)
            else:
                # Replace case-insensitively so an override does not duplicate.
                for existing in [k for k in out_headers if k.lower() == name.lower()]:
                    del out_headers[existing]
                out_headers[name] = value

        payload: Optional[bytes] = None
        if body is not None:
            payload = body.encode("utf-8")
        elif flow.request.get("body") and target_method not in ("GET", "HEAD"):
            payload = flow.request["body"].encode("utf-8")

        token = uuid.uuid4().hex
        out_headers[REPLAY_HEADER] = token

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}),
            urllib.request.HTTPSHandler(context=self._ssl_context()),
            *([] if follow_redirects else [_NoRedirect()]),
        )
        request = urllib.request.Request(
            target_url, data=payload, headers=out_headers, method=target_method
        )

        started = time.monotonic()
        try:
            resp = opener.open(request, timeout=timeout)
            status, reason, raw_headers, content = (
                resp.status,
                resp.reason,
                resp.headers,
                resp.read(),
            )
        except urllib.error.HTTPError as exc:  # a 4xx/5xx is still a response
            status, reason, raw_headers, content = (
                exc.code,
                exc.reason,
                exc.headers,
                exc.read(),
            )
        except urllib.error.URLError as exc:
            raise ProxyError(f"Replay failed: {exc.reason}") from exc
        duration_ms = int((time.monotonic() - started) * 1000)

        # The replay travelled through our own proxy, which already undid any
        # content-encoding (gzip/br/zstd). Prefer that decoded copy — urllib hands
        # back the still-compressed bytes, which would read as binary.
        # Matching on the status urllib ended up with picks the *final* hop: when
        # redirects are followed, every hop carries the same replay token.
        captured = self.store.wait_for(
            lambda f: (
                f.replay_token == token
                and f.response is not None
                and f.response["status"] == status
            ),
            timeout=2.0,
        )
        if captured is not None and captured.response is not None:
            resp_part = captured.response
            text, binary, size = resp_part["body"], resp_part["binary"], resp_part["size"]
            headers_out = [list(h) for h in resp_part["headers"]]
            status, reason = resp_part["status"], resp_part["reason"]
        else:
            text, binary, size = _decode_response_body(content, raw_headers)
            headers_out = [[k, v] for k, v in raw_headers.items()]

        return {
            "request": {
                "method": target_method,
                "url": target_url,
                "headers": [[k, v] for k, v in out_headers.items() if k != REPLAY_HEADER],
                "body": payload.decode("utf-8", "replace") if payload else None,
            },
            "response": {
                "status": status,
                "reason": reason or "",
                "headers": headers_out,
                "body": None if binary else text,
                "size": size,
                "binary": binary,
            },
            "duration_ms": duration_ms,
            "history_flow_id": captured.id if captured else None,
        }

    def _ssl_context(self) -> ssl.SSLContext:
        """Trust the mitmproxy CA so replayed HTTPS traffic can be intercepted."""
        if self.ca_cert_path.exists():
            try:
                return ssl.create_default_context(cafile=str(self.ca_cert_path))
            except (ssl.SSLError, OSError):
                pass
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def _decode_response_body(content: bytes, headers) -> tuple[Optional[str], bool, int]:
    """Decode a replayed response when the proxy's own decoded copy is unavailable.

    urllib does not undo content-encoding, so compressed bodies are inflated here
    before being decoded as text.
    """
    size = len(content)
    encoding = (headers.get("content-encoding") or "").lower().strip()
    raw = content
    try:
        if encoding == "gzip":
            import gzip

            raw = gzip.decompress(content)
        elif encoding == "deflate":
            import zlib

            try:
                raw = zlib.decompress(content)
            except zlib.error:
                raw = zlib.decompress(content, -zlib.MAX_WBITS)
        elif encoding in ("br", "zstd"):
            module = "brotli" if encoding == "br" else "zstandard"
            mod = __import__(module)
            raw = mod.decompress(content) if encoding == "br" else mod.ZstdDecompressor().decompress(content)
    except Exception:  # noqa: BLE001 - fall back to showing it as binary
        return None, True, size
    try:
        return raw.decode(headers.get_content_charset() or "utf-8"), False, size
    except (UnicodeDecodeError, LookupError):
        return None, True, size


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep 3xx responses intact, the way Burp's Repeater does."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


# ---------------------------------------------------------------------------
# HAR export
# ---------------------------------------------------------------------------
def flows_to_har(flows: list[Flow]) -> dict:
    """Build a HAR 1.2 log, loadable by Burp, Chrome DevTools or any HAR viewer."""
    entries = []
    for f in flows:
        req, resp = f.request, f.response
        post_data = None
        if req.get("body"):
            post_data = {
                "mimeType": _header_value(req["headers"], "content-type") or "",
                "text": req["body"],
                "params": [],
            }
        entry: dict[str, Any] = {
            "startedDateTime": _iso(f.started_at),
            "time": f.duration_ms or 0,
            "request": {
                "method": f.method,
                "url": f.url,
                "httpVersion": f.http_version,
                "headers": [{"name": k, "value": v} for k, v in req["headers"]],
                "queryString": [{"name": k, "value": v} for k, v in req.get("query", [])],
                "cookies": [],
                "headersSize": -1,
                "bodySize": req["size"],
            },
            "cache": {},
            "timings": {"send": 0, "wait": f.duration_ms or 0, "receive": 0},
        }
        if post_data:
            entry["request"]["postData"] = post_data
        if resp:
            entry["response"] = {
                "status": resp["status"],
                "statusText": resp["reason"],
                "httpVersion": resp["http_version"],
                "headers": [{"name": k, "value": v} for k, v in resp["headers"]],
                "cookies": [],
                "content": {
                    "size": resp["size"],
                    "mimeType": _header_value(resp["headers"], "content-type") or "",
                    "text": resp["body"] or "",
                },
                "redirectURL": _header_value(resp["headers"], "location") or "",
                "headersSize": -1,
                "bodySize": resp["size"],
            }
        else:
            entry["response"] = {
                "status": 0,
                "statusText": f.error or "no response",
                "httpVersion": "",
                "headers": [],
                "cookies": [],
                "content": {"size": 0, "mimeType": "", "text": ""},
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": 0,
            }
        entries.append(entry)
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "selenium-mcp", "version": "0.2.0"},
            "entries": entries,
        }
    }


def write_har(flows: list[Flow], path: str) -> str:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(flows_to_har(flows), indent=2))
    return str(target)


# ---------------------------------------------------------------------------
# Rendering for the model (Burp-style views)
# ---------------------------------------------------------------------------
def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}kB"
    return f"{n / (1024 * 1024):.1f}MB"


def render_table(flows: list[Flow], url_width: int = 88) -> str:
    """A proxy-history table: one line per flow, newest last."""
    if not flows:
        return "No matching requests captured."
    rows = [f"{'#':>5}  {'METHOD':<7} {'ST':>3}  {'TYPE':<24} {'SIZE':>8} {'TIME':>7}  URL"]
    for f in flows:
        status = str(f.status_code) if f.status_code else ("ERR" if f.error else "---")
        url = f.url if len(f.url) <= url_width else f.url[: url_width - 1] + "…"
        marker = "R" if f.replayed else " "
        rows.append(
            f"{f.id:>5}{marker} {f.method:<7} {status:>3}  {f.content_type[:24]:<24} "
            f"{_human_size(f.response_size):>8} "
            f"{(str(f.duration_ms) + 'ms') if f.duration_ms is not None else '-':>7}  {url}"
        )
    rows.append(f"\n{len(flows)} request(s). Use get_request(flow_id=#) for full detail.")
    return "\n".join(rows)


def render_flow(flow: Flow, max_body_chars: int = 4000, headers: bool = True) -> str:
    """One flow as raw HTTP, the way Burp's message editor shows it."""
    out: list[str] = []
    req = flow.request
    out.append(f"=== #{flow.id}  {_iso(flow.started_at)}"
               f"{'  [replayed]' if flow.replayed else ''} ===")
    out.append("")
    out.append("--- REQUEST ---")
    target = flow.path or "/"  # mitmproxy's path already includes the query string
    out.append(f"{flow.method} {target} {flow.http_version}")
    if headers:
        out.extend(f"{k}: {v}" for k, v in req["headers"])
    out.append("")
    out.append(_render_body(req, max_body_chars))

    out.append("")
    if flow.response:
        resp = flow.response
        out.append("--- RESPONSE ---")
        out.append(
            f"{resp['http_version']} {resp['status']} {resp['reason']}"
            + (f"   ({flow.duration_ms}ms)" if flow.duration_ms is not None else "")
        )
        if headers:
            out.extend(f"{k}: {v}" for k, v in resp["headers"])
        out.append("")
        out.append(_render_body(resp, max_body_chars))
    else:
        out.append(f"--- NO RESPONSE --- ({flow.error or 'unknown error'})")
    return "\n".join(out)


def _render_body(part: dict, max_body_chars: int) -> str:
    if part["binary"]:
        return f"[binary body, {_human_size(part['size'])} — not shown]"
    body = part["body"]
    if not body:
        return "[no body]"
    note = ""
    if len(body) > max_body_chars:
        note = (
            f"\n[... body shown truncated to {max_body_chars} of {len(body)} chars; "
            "raise max_body_chars for more]"
        )
        body = body[:max_body_chars]
    elif part["truncated"]:
        note = (
            f"\n[... capture truncated: {_human_size(part['size'])} on the wire; "
            "raise max_body_bytes in start_proxy to store more]"
        )
    return body + note


# Process-wide singleton shared by the browser session and the traffic tools.
proxy = ProxyController()
