"""Selenium MCP server.

Exposes a broad surface of Selenium WebDriver functionality as MCP tools so an
LLM client (Claude / Claude Code) can fully drive a browser: navigation, element
interaction, forms, waits, JavaScript execution, cookies, windows/tabs/frames,
alerts, mouse/keyboard action chains, screenshots and scraping.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from .driver import BrowserError, resolve_by, session

mcp = FastMCP("selenium-mcp")


def _ok(message: str) -> str:
    return message


def _wrap(fn):
    """Decorator: turn BrowserError into a clean string result for the model."""

    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BrowserError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            return f"Error: {type(exc).__name__}: {exc}"

    inner.__name__ = fn.__name__
    inner.__doc__ = fn.__doc__
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
) -> str:
    """Launch a browser.

    Args:
        browser: 'chrome', 'chromium' or 'firefox'.
        headless: Run without a visible window (default True).
        window_width: Initial viewport width in pixels.
        window_height: Initial viewport height in pixels.
        user_agent: Optional custom User-Agent string.
        default_timeout: Default seconds to wait when locating elements.
    """
    return _ok(
        session.start(
            browser=browser,
            headless=headless,
            window_width=window_width,
            window_height=window_height,
            user_agent=user_agent,
            default_timeout=default_timeout,
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
@mcp.tool()
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


@mcp.tool()
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


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
