"""Selenium MCP server.

Exposes a broad surface of Selenium WebDriver functionality as MCP tools so an
LLM client (Claude / Claude Code) can fully drive a browser: navigation, element
interaction, forms, waits, JavaScript execution, cookies, windows/tabs/frames,
alerts, mouse/keyboard action chains, screenshots and scraping.

The browser is launched behind an embedded mitmproxy (see :mod:`selenium_mcp.proxy`),
so the model can also read every request and response the page made — a Burp-style
proxy history, searchable, replayable and exportable as HAR.
"""

from __future__ import annotations

import functools
import json
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from . import proxy as traffic
from .driver import BrowserError, resolve_by, session
from .proxy import ProxyError, proxy

mcp = FastMCP("selenium-mcp")


def _ok(message: str) -> str:
    return message


def _wrap(fn):
    """Decorator: turn BrowserError into a clean string result for the model.

    ``functools.wraps`` matters beyond cosmetics here: FastMCP derives each
    tool's parameter schema from the wrapped function's signature, so without
    it every tool would advertise only ``*args``/``**kwargs``.
    """

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (BrowserError, ProxyError) as exc:
            return f"Error: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            return f"Error: {type(exc).__name__}: {exc}"

    return inner


# ---------------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def start_browser(
    browser: str = "chrome",
    headless: bool = True,
    window_width: int = 1280,
    window_height: int = 800,
    user_agent: Optional[str] = None,
    default_timeout: float = 10.0,
    capture_traffic: bool = True,
    proxy_port: Optional[int] = None,
    capture_filter: Optional[str] = None,
) -> str:
    """Launch a browser, by default behind the traffic-capturing proxy.

    With `capture_traffic` on, every request and response the browser makes —
    including decrypted HTTPS — is recorded, so after navigating you can call
    `list_requests`, `get_request`, `search_traffic` or `replay_request` to see
    exactly what went over the wire.

    Args:
        browser: 'chrome', 'chromium' or 'firefox'.
        headless: Run without a visible window (default True).
        window_width: Initial viewport width in pixels.
        window_height: Initial viewport height in pixels.
        user_agent: Optional custom User-Agent string.
        default_timeout: Default seconds to wait when locating elements.
        capture_traffic: Route the browser through the capture proxy (default True).
        proxy_port: Port for the proxy; a free one is chosen when omitted.
        capture_filter: mitmproxy filter limiting what is recorded, e.g.
            '~d example.com' for one domain. See `start_proxy` for the syntax.
    """
    return _ok(
        session.start(
            browser=browser,
            headless=headless,
            window_width=window_width,
            window_height=window_height,
            user_agent=user_agent,
            default_timeout=default_timeout,
            capture_traffic=capture_traffic,
            proxy_port=proxy_port,
            capture_filter=capture_filter,
        )
    )


@mcp.tool()
@_wrap
def close_browser() -> str:
    """Close the browser and end the session."""
    return _ok(session.close())


@mcp.tool()
@_wrap
def browser_status() -> str:
    """Report whether a browser is currently open, with its URL and title."""
    if not session.is_open:
        return "No browser is open."
    d = session.driver
    return f"Open. url={d.current_url!r} title={d.title!r}"


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def navigate(url: str) -> str:
    """Navigate the current tab to a URL."""
    session.driver.get(url)
    return _ok(f"Navigated to {session.driver.current_url}")


@mcp.tool()
@_wrap
def go_back() -> str:
    """Go back one entry in browser history."""
    session.driver.back()
    return _ok(f"Back to {session.driver.current_url}")


@mcp.tool()
@_wrap
def go_forward() -> str:
    """Go forward one entry in browser history."""
    session.driver.forward()
    return _ok(f"Forward to {session.driver.current_url}")


@mcp.tool()
@_wrap
def refresh() -> str:
    """Reload the current page."""
    session.driver.refresh()
    return _ok(f"Reloaded {session.driver.current_url}")


@mcp.tool()
@_wrap
def get_current_url() -> str:
    """Return the current page URL."""
    return session.driver.current_url


@mcp.tool()
@_wrap
def get_title() -> str:
    """Return the current page title."""
    return session.driver.title


# ---------------------------------------------------------------------------
# Element interaction
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def click(strategy: str, selector: str, timeout: Optional[float] = None) -> str:
    """Click the first element matching a locator.

    Args:
        strategy: Locator type: id, name, css, xpath, tag, class, link_text,
            partial_link_text.
        selector: The locator value.
        timeout: Seconds to wait for the element to be clickable.
    """
    el = session.find(strategy, selector, timeout=timeout, clickable=True)
    el.click()
    return _ok(f"Clicked {strategy}={selector!r}.")


@mcp.tool()
@_wrap
def type_text(
    strategy: str,
    selector: str,
    text: str,
    clear_first: bool = True,
    press_enter: bool = False,
    timeout: Optional[float] = None,
) -> str:
    """Type text into an input or textarea.

    Args:
        clear_first: Clear existing content before typing.
        press_enter: Append a RETURN key after the text (e.g. to submit).
    """
    el = session.find(strategy, selector, timeout=timeout)
    if clear_first:
        el.clear()
    el.send_keys(text)
    if press_enter:
        el.send_keys(Keys.RETURN)
    return _ok(f"Typed into {strategy}={selector!r}.")


@mcp.tool()
@_wrap
def clear(strategy: str, selector: str, timeout: Optional[float] = None) -> str:
    """Clear the value of an input or textarea."""
    el = session.find(strategy, selector, timeout=timeout)
    el.clear()
    return _ok(f"Cleared {strategy}={selector!r}.")


@mcp.tool()
@_wrap
def submit(strategy: str, selector: str, timeout: Optional[float] = None) -> str:
    """Submit the form containing the matched element."""
    el = session.find(strategy, selector, timeout=timeout)
    el.submit()
    return _ok(f"Submitted form for {strategy}={selector!r}.")


@mcp.tool()
@_wrap
def send_keys(
    strategy: str,
    selector: str,
    keys: str,
    timeout: Optional[float] = None,
) -> str:
    """Send special keys to an element.

    `keys` is a comma-separated list of key names (e.g. "CONTROL,a" or
    "TAB" or "ESCAPE"). Names map to selenium.webdriver.common.keys.Keys;
    an unknown single-character token is sent literally.
    """
    el = session.find(strategy, selector, timeout=timeout)
    tokens = [k.strip() for k in keys.split(",") if k.strip()]
    resolved = []
    for tok in tokens:
        resolved.append(getattr(Keys, tok.upper(), tok))
    el.send_keys(*resolved)
    return _ok(f"Sent keys {tokens} to {strategy}={selector!r}.")


# ---------------------------------------------------------------------------
# Reading / scraping
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def get_text(strategy: str, selector: str, timeout: Optional[float] = None) -> str:
    """Return the visible text of the first matching element."""
    el = session.find(strategy, selector, timeout=timeout)
    return el.text


@mcp.tool()
@_wrap
def get_attribute(
    strategy: str,
    selector: str,
    attribute: str,
    timeout: Optional[float] = None,
) -> str:
    """Return a DOM attribute (e.g. 'href', 'value', 'class') of an element."""
    el = session.find(strategy, selector, timeout=timeout)
    value = el.get_attribute(attribute)
    return "" if value is None else value


@mcp.tool()
@_wrap
def get_property(
    strategy: str,
    selector: str,
    name: str,
    timeout: Optional[float] = None,
) -> str:
    """Return a JS DOM property of an element."""
    el = session.find(strategy, selector, timeout=timeout)
    return json.dumps(el.get_property(name), default=str)


@mcp.tool()
@_wrap
def is_displayed(
    strategy: str, selector: str, timeout: Optional[float] = None
) -> str:
    """Report whether an element is displayed, enabled and selected."""
    el = session.find(strategy, selector, timeout=timeout)
    return json.dumps(
        {
            "displayed": el.is_displayed(),
            "enabled": el.is_enabled(),
            "selected": el.is_selected(),
        }
    )


@mcp.tool()
@_wrap
def find_elements(
    strategy: str, selector: str, limit: int = 50
) -> str:
    """List elements matching a locator.

    Returns a JSON array of objects with tag, text (trimmed), and the
    href/value attributes when present. Useful for discovering page structure.
    """
    els = session.find_all(strategy, selector)
    out = []
    for el in els[: max(0, limit)]:
        try:
            out.append(
                {
                    "tag": el.tag_name,
                    "text": (el.text or "")[:200],
                    "href": el.get_attribute("href"),
                    "value": el.get_attribute("value"),
                }
            )
        except Exception:
            continue
    return json.dumps({"count": len(els), "shown": len(out), "elements": out})


@mcp.tool()
@_wrap
def get_page_source() -> str:
    """Return the full HTML source of the current page."""
    return session.driver.page_source


@mcp.tool()
@_wrap
def get_page_text() -> str:
    """Return the visible text of the whole page body."""
    return session.driver.find_element("tag name", "body").text


# ---------------------------------------------------------------------------
# Dropdowns (<select>)
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def select_option(
    strategy: str,
    selector: str,
    by: str = "visible_text",
    value: str = "",
    timeout: Optional[float] = None,
) -> str:
    """Choose an option in a <select> element.

    Args:
        by: 'visible_text', 'value' or 'index'.
        value: The text, value attribute, or integer index to select.
    """
    el = session.find(strategy, selector, timeout=timeout)
    sel = Select(el)
    by = by.lower()
    if by == "visible_text":
        sel.select_by_visible_text(value)
    elif by == "value":
        sel.select_by_value(value)
    elif by == "index":
        sel.select_by_index(int(value))
    else:
        raise BrowserError("by must be 'visible_text', 'value' or 'index'.")
    return _ok(f"Selected option ({by}={value!r}).")


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def wait_for_element(
    strategy: str,
    selector: str,
    timeout: float = 10.0,
    clickable: bool = False,
) -> str:
    """Wait until an element is present (or clickable) on the page."""
    session.find(strategy, selector, timeout=timeout, clickable=clickable)
    return _ok(f"Element {strategy}={selector!r} is ready.")


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def execute_script(script: str) -> str:
    """Run JavaScript in the page and return its result as JSON.

    The script runs in the page context. Use `return ...;` to get a value.
    """
    result = session.driver.execute_script(script)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def scroll_to(
    strategy: str, selector: str, timeout: Optional[float] = None
) -> str:
    """Scroll an element into view."""
    el = session.find(strategy, selector, timeout=timeout)
    session.driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});", el
    )
    return _ok(f"Scrolled to {strategy}={selector!r}.")


@mcp.tool()
@_wrap
def scroll_by(x: int = 0, y: int = 600) -> str:
    """Scroll the window by a pixel offset."""
    session.driver.execute_script(f"window.scrollBy({x},{y});")
    return _ok(f"Scrolled by ({x},{y}).")


# ---------------------------------------------------------------------------
# Mouse / keyboard action chains
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def hover(strategy: str, selector: str, timeout: Optional[float] = None) -> str:
    """Move the mouse over an element."""
    el = session.find(strategy, selector, timeout=timeout)
    ActionChains(session.driver).move_to_element(el).perform()
    return _ok(f"Hovered {strategy}={selector!r}.")


@mcp.tool()
@_wrap
def double_click(
    strategy: str, selector: str, timeout: Optional[float] = None
) -> str:
    """Double-click an element."""
    el = session.find(strategy, selector, timeout=timeout)
    ActionChains(session.driver).double_click(el).perform()
    return _ok(f"Double-clicked {strategy}={selector!r}.")


@mcp.tool()
@_wrap
def right_click(
    strategy: str, selector: str, timeout: Optional[float] = None
) -> str:
    """Right-click (context click) an element."""
    el = session.find(strategy, selector, timeout=timeout)
    ActionChains(session.driver).context_click(el).perform()
    return _ok(f"Right-clicked {strategy}={selector!r}.")


@mcp.tool()
@_wrap
def drag_and_drop(
    source_strategy: str,
    source_selector: str,
    target_strategy: str,
    target_selector: str,
    timeout: Optional[float] = None,
) -> str:
    """Drag one element onto another."""
    src = session.find(source_strategy, source_selector, timeout=timeout)
    dst = session.find(target_strategy, target_selector, timeout=timeout)
    ActionChains(session.driver).drag_and_drop(src, dst).perform()
    return _ok("Dragged source onto target.")


# ---------------------------------------------------------------------------
# Windows, tabs and frames
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def open_new_tab(url: str = "") -> str:
    """Open a new tab (optionally navigating to a URL) and switch to it."""
    session.driver.switch_to.new_window("tab")
    if url:
        session.driver.get(url)
    return _ok(
        f"Opened new tab (handle index "
        f"{session.driver.window_handles.index(session.driver.current_window_handle)})."
    )


@mcp.tool()
@_wrap
def list_tabs() -> str:
    """List open tab/window handles and the index of the active one."""
    d = session.driver
    return json.dumps(
        {
            "handles": d.window_handles,
            "active": d.current_window_handle,
            "active_index": d.window_handles.index(d.current_window_handle),
        }
    )


@mcp.tool()
@_wrap
def switch_tab(index: int) -> str:
    """Switch to a tab/window by its index in the handle list."""
    handles = session.driver.window_handles
    if index < 0 or index >= len(handles):
        raise BrowserError(
            f"Tab index {index} out of range (0..{len(handles) - 1})."
        )
    session.driver.switch_to.window(handles[index])
    return _ok(f"Switched to tab {index}: {session.driver.current_url}")


@mcp.tool()
@_wrap
def close_tab() -> str:
    """Close the active tab and switch to the first remaining one."""
    d = session.driver
    d.close()
    if d.window_handles:
        d.switch_to.window(d.window_handles[0])
        return _ok(f"Closed tab. Now on {d.current_url}")
    return _ok("Closed last tab.")


@mcp.tool()
@_wrap
def switch_to_frame(
    strategy: str, selector: str, timeout: Optional[float] = None
) -> str:
    """Switch into an iframe identified by a locator."""
    el = session.find(strategy, selector, timeout=timeout)
    session.driver.switch_to.frame(el)
    return _ok(f"Switched into frame {strategy}={selector!r}.")


@mcp.tool()
@_wrap
def switch_to_default_content() -> str:
    """Switch back to the top-level document from an iframe."""
    session.driver.switch_to.default_content()
    return _ok("Switched to default content.")


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def handle_alert(action: str = "accept", text: Optional[str] = None) -> str:
    """Interact with a JavaScript alert/confirm/prompt dialog.

    Args:
        action: 'accept', 'dismiss' or 'get_text'.
        text: Optional text to type into a prompt before accepting.
    """
    alert = session.driver.switch_to.alert
    body = alert.text
    action = action.lower()
    if action == "get_text":
        return body
    if text is not None:
        alert.send_keys(text)
    if action == "accept":
        alert.accept()
        return _ok(f"Accepted alert. Text was: {body!r}")
    if action == "dismiss":
        alert.dismiss()
        return _ok(f"Dismissed alert. Text was: {body!r}")
    raise BrowserError("action must be 'accept', 'dismiss' or 'get_text'.")


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def get_cookies() -> str:
    """Return all cookies for the current domain as JSON."""
    return json.dumps(session.driver.get_cookies(), default=str)


@mcp.tool()
@_wrap
def add_cookie(
    name: str,
    value: str,
    domain: Optional[str] = None,
    path: str = "/",
) -> str:
    """Add a cookie to the current session."""
    cookie = {"name": name, "value": value, "path": path}
    if domain:
        cookie["domain"] = domain
    session.driver.add_cookie(cookie)
    return _ok(f"Added cookie {name!r}.")


@mcp.tool()
@_wrap
def delete_cookie(name: str) -> str:
    """Delete a single cookie by name."""
    session.driver.delete_cookie(name)
    return _ok(f"Deleted cookie {name!r}.")


@mcp.tool()
@_wrap
def delete_all_cookies() -> str:
    """Delete all cookies for the current session."""
    session.driver.delete_all_cookies()
    return _ok("Deleted all cookies.")


# ---------------------------------------------------------------------------
# Window sizing
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def set_window_size(width: int, height: int) -> str:
    """Resize the browser window."""
    session.driver.set_window_size(width, height)
    return _ok(f"Window set to {width}x{height}.")


@mcp.tool()
@_wrap
def maximize_window() -> str:
    """Maximize the browser window."""
    session.driver.maximize_window()
    return _ok("Window maximized.")


# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def upload_file(
    strategy: str,
    selector: str,
    file_path: str,
    timeout: Optional[float] = None,
) -> str:
    """Set the file for a `<input type=file>` element by sending its path."""
    if not os.path.isfile(file_path):
        raise BrowserError(f"File not found: {file_path}")
    el = session.find(strategy, selector, timeout=timeout)
    el.send_keys(os.path.abspath(file_path))
    return _ok(f"Set upload file for {strategy}={selector!r}.")


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
@_wrap
def screenshot(save_path: Optional[str] = None) -> Image | str:
    """Capture a screenshot of the current page.

    If `save_path` is given, the PNG is written there and the path is
    returned. Otherwise the image is returned inline to the client.
    """
    png = session.driver.get_screenshot_as_png()
    if save_path:
        with open(save_path, "wb") as fh:
            fh.write(png)
        return _ok(f"Saved screenshot to {os.path.abspath(save_path)}")
    return Image(data=png, format="png")


@mcp.tool(structured_output=False)
@_wrap
def element_screenshot(
    strategy: str,
    selector: str,
    save_path: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Image | str:
    """Capture a screenshot of a single element."""
    el = session.find(strategy, selector, timeout=timeout)
    png = el.screenshot_as_png
    if save_path:
        with open(save_path, "wb") as fh:
            fh.write(png)
        return _ok(f"Saved element screenshot to {os.path.abspath(save_path)}")
    return Image(data=png, format="png")


# ---------------------------------------------------------------------------
# Traffic capture (embedded mitmproxy) — a Burp-style proxy history
# ---------------------------------------------------------------------------
@mcp.tool()
@_wrap
def start_proxy(
    port: Optional[int] = None,
    capture_filter: Optional[str] = None,
    max_body_bytes: int = 200_000,
    max_flows: int = 2000,
    verify_upstream_certs: bool = False,
) -> str:
    """Start the capture proxy on its own (start_browser does this for you).

    Useful to capture traffic from something other than this server's browser —
    point any client at the returned proxy address — or to raise the capture
    limits before browsing.

    Args:
        port: Listen port on 127.0.0.1; a free one is chosen when omitted.
        capture_filter: mitmproxy filter expression; only matching flows are
            recorded. Examples: '~d example.com' (domain), '~u /api/' (URL),
            '~m POST' (method), '~d example.com & ~m POST' (both),
            '!~d analytics.google.com' (exclude). Omit to record everything.
        max_body_bytes: Per-body cap on what is stored (bodies above it are
            truncated; the true size is still reported).
        max_flows: Ring-buffer size; the oldest flows are dropped beyond it.
        verify_upstream_certs: Reject invalid certificates on the server side
            instead of tolerating them (default False, like Burp).
    """
    info = proxy.start(
        port=port,
        capture_filter=capture_filter,
        max_body_bytes=max_body_bytes,
        max_flows=max_flows,
        verify_upstream_certs=verify_upstream_certs,
    )
    return _ok(
        f"Capture proxy listening on {info['proxy']}.\n"
        f"CA certificate: {info['ca_cert']}\n"
        "Browsers started by start_browser trust it automatically; an external "
        "client must either trust that CA file or ignore certificate errors."
    )


@mcp.tool()
@_wrap
def stop_proxy() -> str:
    """Stop the capture proxy. Already-captured traffic stays readable."""
    return _ok(proxy.stop())


@mcp.tool()
@_wrap
def proxy_status() -> str:
    """Report whether capture is running, where, and how much it has recorded."""
    stats = proxy.store.stats()
    lines = []
    if proxy.is_running:
        lines.append(f"Running on {proxy.proxy_url}")
        lines.append(f"CA certificate: {proxy.ca_cert_path}")
        lines.append(f"Capture filter: {proxy.capture_filter or '(none — recording everything)'}")
        lines.append(f"Body cap: {proxy.max_body_bytes} bytes per message")
    else:
        lines.append("Not running. Call start_browser or start_proxy.")
    lines.append(
        f"History: {stats['stored']} stored of {stats['captured_total']} captured "
        f"(capacity {stats['capacity']}, {stats['dropped_oldest']} oldest dropped)"
    )
    warnings = proxy.recent_logs()
    if warnings:
        lines.append("Recent proxy warnings:")
        lines.extend(f"  {w}" for w in warnings[-5:])
    return "\n".join(lines)


@mcp.tool()
@_wrap
def list_requests(
    limit: int = 50,
    host: Optional[str] = None,
    method: Optional[str] = None,
    status: Optional[int] = None,
    url_contains: Optional[str] = None,
    content_type: Optional[str] = None,
    since_id: Optional[int] = None,
    errors_only: bool = False,
) -> str:
    """List captured requests, newest last — the proxy history table.

    Each row is `#id METHOD status content-type size time URL`; pass an id to
    `get_request` for the full request and response. A trailing `R` on the id
    marks a replayed request.

    Args:
        limit: Return at most this many of the most recent matches.
        host: Only flows whose host contains this substring.
        method: Exact HTTP method, e.g. 'POST'.
        status: Exact response status code, e.g. 404.
        url_contains: Only flows whose URL contains this substring.
        content_type: Only flows whose response content-type contains this,
            e.g. 'json' for API calls or 'javascript' for scripts.
        since_id: Only flows newer than this id — pass the largest id you have
            already seen to get just what an action generated.
        errors_only: Only flows that failed before a response arrived.
    """
    flows = proxy.store.select(
        host=host,
        method=method,
        status=status,
        url_contains=url_contains,
        content_type=content_type,
        since_id=since_id,
        errors_only=errors_only,
    )
    return traffic.render_table(flows[-limit:] if limit > 0 else flows)


@mcp.tool()
@_wrap
def get_request(
    flow_id: int,
    max_body_chars: int = 4000,
    headers: bool = True,
) -> str:
    """Show one captured transaction as raw HTTP: request, then response.

    Args:
        flow_id: The `#id` from `list_requests`.
        max_body_chars: Per-body display cap; raise it to read a long response
            (a whole JS bundle, say), lower it to save context.
        headers: Include headers (set False for bodies only).
    """
    flow = proxy.store.get(flow_id)
    if flow is None:
        raise ProxyError(
            f"No captured request #{flow_id}. Call list_requests to see what is stored "
            "(the oldest flows are dropped once the buffer is full)."
        )
    return traffic.render_flow(flow, max_body_chars=max_body_chars, headers=headers)


@mcp.tool()
@_wrap
def search_traffic(
    query: str,
    where: str = "any",
    limit: int = 50,
) -> str:
    """Case-insensitively search captured traffic for a string.

    Good for tracing where a value came from or went: an API key, a session
    token, a CSRF nonce, a reflected parameter.

    Args:
        query: Substring to look for.
        where: 'any' (default), 'url', 'headers', 'body', 'request_body' or
            'response_body'.
        limit: Return at most this many of the most recent matches.
    """
    allowed = {"any", "url", "headers", "body", "request_body", "response_body"}
    if where not in allowed:
        raise ProxyError(f"Unknown `where` {where!r}. Use one of: {', '.join(sorted(allowed))}.")
    flows = proxy.store.select(predicate=lambda f: f.matches_text(query, where))
    header = f"Searched {where} for {query!r}:\n"
    return header + traffic.render_table(flows[-limit:] if limit > 0 else flows)


@mcp.tool()
@_wrap
def wait_for_request(
    url_contains: str,
    timeout: float = 15.0,
    method: Optional[str] = None,
    status: Optional[int] = None,
    include_existing: bool = True,
    max_body_chars: int = 2000,
) -> str:
    """Block until a matching request is captured, then show it.

    Use this after an action that fires a background request — a click that
    triggers an XHR, a form submit, a lazy-loaded page — instead of guessing
    how long to wait.

    Args:
        url_contains: Substring the request URL must contain.
        timeout: Seconds to wait before giving up.
        method: Also require this exact HTTP method.
        status: Also require this exact response status code.
        include_existing: Match a request already in the history (default True);
            set False to wait only for new traffic.
        max_body_chars: Per-body display cap for the result.
    """
    after_id = 0 if include_existing else proxy.store.last_id()

    def matches(f) -> bool:
        if url_contains.lower() not in f.url.lower():
            return False
        if method and f.method.upper() != method.upper():
            return False
        if status is not None and f.status_code != status:
            return False
        return True

    flow = proxy.store.wait_for(matches, timeout=timeout, after_id=after_id)
    if flow is None:
        return (
            f"No request matching {url_contains!r} was captured within {timeout}s. "
            "Check that capture is running with proxy_status."
        )
    return traffic.render_flow(flow, max_body_chars=max_body_chars)


@mcp.tool()
@_wrap
def replay_request(
    flow_id: int,
    method: Optional[str] = None,
    url: Optional[str] = None,
    headers: Optional[dict] = None,
    body: Optional[str] = None,
    remove_headers: Optional[list] = None,
    follow_redirects: bool = False,
    max_body_chars: int = 4000,
) -> str:
    """Re-send a captured request, optionally modified — Burp's Repeater.

    The original request's headers and cookies are reused, so this replays an
    authenticated call as the browser made it. Only what you override changes.
    The replay goes through the proxy, so it is added to the history too (marked
    `R`). Redirects are not followed by default, so you see the real response.

    Args:
        flow_id: The `#id` of the request to replay.
        method: Override the HTTP method.
        url: Override the full URL — the usual way to change a path or a
            query parameter.
        headers: Headers to set or replace, e.g. {"X-Role": "admin"}.
        body: Replace the request body.
        remove_headers: Header names to drop, e.g. ["Cookie"] to check whether
            an endpoint is reachable unauthenticated.
        follow_redirects: Follow 3xx responses instead of returning them.
        max_body_chars: Per-body display cap for the result.
    """
    flow = proxy.store.get(flow_id)
    if flow is None:
        raise ProxyError(f"No captured request #{flow_id}. Call list_requests to see what is stored.")
    result = proxy.replay(
        flow,
        method=method,
        url=url,
        headers=headers,
        body=body,
        remove_headers=remove_headers,
        follow_redirects=follow_redirects,
    )
    req, resp = result["request"], result["response"]
    out = [f"=== replay of #{flow_id} ===", "", "--- REQUEST ---", f"{req['method']} {req['url']}"]
    out.extend(f"{k}: {v}" for k, v in req["headers"])
    out.append("")
    out.append(req["body"] or "[no body]")
    out.append("")
    out.append("--- RESPONSE ---")
    out.append(f"{resp['status']} {resp['reason']}   ({result['duration_ms']}ms)")
    out.extend(f"{k}: {v}" for k, v in resp["headers"])
    out.append("")
    if resp["binary"]:
        out.append(f"[binary body, {resp['size']} bytes — not shown]")
    else:
        text = resp["body"] or "[no body]"
        if len(text) > max_body_chars:
            text = text[:max_body_chars] + f"\n[... truncated to {max_body_chars} chars]"
        out.append(text)
    if result["history_flow_id"]:
        out.append(f"\nAlso captured in the history as #{result['history_flow_id']}.")
    return "\n".join(out)


@mcp.tool()
@_wrap
def traffic_stats() -> str:
    """Summarise captured traffic by host, status code and content type."""
    stats = proxy.store.stats()
    if not stats["stored"]:
        return "No traffic captured yet."
    lines = [
        f"{stats['stored']} flows stored ({stats['captured_total']} captured, "
        f"{stats['dropped_oldest']} dropped), "
        f"{stats['response_bytes']} response bytes.",
        "",
        "By host:",
    ]
    lines.extend(f"  {n:>5}  {h}" for h, n in list(stats["by_host"].items())[:25])
    lines.append("By status:")
    lines.extend(f"  {n:>5}  {k}" for k, n in stats["by_status"].items())
    lines.append("By content type:")
    lines.extend(f"  {n:>5}  {t}" for t, n in list(stats["by_content_type"].items())[:25])
    return "\n".join(lines)


@mcp.tool()
@_wrap
def clear_traffic() -> str:
    """Discard the captured history, so the next action's traffic stands alone."""
    return _ok(f"Cleared {proxy.store.clear()} captured flow(s).")


@mcp.tool()
@_wrap
def export_har(
    save_path: str,
    host: Optional[str] = None,
    url_contains: Optional[str] = None,
    since_id: Optional[int] = None,
) -> str:
    """Write captured traffic to a HAR 1.2 file (Burp, DevTools, any HAR viewer).

    Unlike the display tools this keeps full fidelity — all headers and whole
    stored bodies — so it is the right way to hand traffic to another tool or
    keep it as evidence.

    Args:
        save_path: Where to write the .har file.
        host: Only flows whose host contains this substring.
        url_contains: Only flows whose URL contains this substring.
        since_id: Only flows newer than this id.
    """
    flows = proxy.store.select(host=host, url_contains=url_contains, since_id=since_id)
    if not flows:
        return "No matching traffic to export."
    path = traffic.write_har(flows, save_path)
    return _ok(f"Wrote {len(flows)} flow(s) to {path}")


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
