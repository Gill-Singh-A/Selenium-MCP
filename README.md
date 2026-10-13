# Selenium MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
[Selenium WebDriver](https://www.selenium.dev/) as a rich set of tools, so an
MCP client such as **Claude** or **Claude Code** can fully operate a real web
browser — navigate, click, fill forms, run JavaScript, manage cookies, switch
tabs and frames, handle alerts, drive the mouse/keyboard, scrape the DOM and
take screenshots.

The browser also runs behind an **embedded [mitmproxy](https://mitmproxy.org/)**,
so the model can read *every* request and response the page made — including
decrypted HTTPS. That gives it a Burp Suite-style proxy history it can list,
inspect as raw HTTP, search, replay with modifications and export as HAR.

It drives Chrome, Chromium or Firefox through their standard WebDriver, and
runs over stdio.

## Features

A single browser session is managed per server process. The following **57
tools** are available:

| Group | Tools |
| --- | --- |
| **Lifecycle** | `start_browser`, `close_browser`, `browser_status` |
| **Navigation** | `navigate`, `go_back`, `go_forward`, `refresh`, `get_current_url`, `get_title` |
| **Interaction** | `click`, `type_text`, `clear`, `submit`, `send_keys` |
| **Reading / scraping** | `get_text`, `get_attribute`, `get_property`, `is_displayed`, `find_elements`, `get_page_source`, `get_page_text` |
| **Dropdowns** | `select_option` |
| **Waiting** | `wait_for_element` |
| **JavaScript** | `execute_script` |
| **Scrolling** | `scroll_to`, `scroll_by` |
| **Mouse / keyboard** | `hover`, `double_click`, `right_click`, `drag_and_drop` |
| **Tabs / windows / frames** | `open_new_tab`, `list_tabs`, `switch_tab`, `close_tab`, `switch_to_frame`, `switch_to_default_content` |
| **Alerts** | `handle_alert` |
| **Cookies** | `get_cookies`, `add_cookie`, `delete_cookie`, `delete_all_cookies` |
| **Window** | `set_window_size`, `maximize_window` |
| **Files** | `upload_file` |
| **Screenshots** | `screenshot`, `element_screenshot` |
| **Traffic capture** | `proxy_status`, `list_requests`, `get_request`, `search_traffic`, `wait_for_request`, `replay_request`, `traffic_stats`, `clear_traffic`, `export_har`, `start_proxy`, `stop_proxy` |

### Seeing the traffic (the Burp-style part)

`start_browser` starts an in-process mitmproxy and launches the browser behind
it, trusting its CA automatically — there is no separate `mitmdump` to run and
no certificate to install. Every transaction is then readable:

```
start_browser()                      # capture is on by default
navigate(url="https://example.com")
list_requests()
```

```
    #  METHOD   ST  TYPE                         SIZE    TIME  URL
    1  GET     200  text/html                    577B    14ms  https://example.com/
    2  GET     200  text/javascript             1.9kB    15ms  https://example.com/s.js
```

`get_request(flow_id=1)` then prints that transaction as raw HTTP — request
line, every header (repeated ones like `Set-Cookie` kept intact), and the
decoded body, with gzip/brotli/zstd already undone:

```
--- REQUEST ---
GET / HTTP/2.0
user-agent: Mozilla/5.0 ...
...
--- RESPONSE ---
HTTP/2.0 200    (14ms)
content-type: text/html
...
```

What the other tools are for:

| Need | Tool |
| --- | --- |
| Find where a token, key or parameter appears | `search_traffic(query="...")` — searches URLs, headers and bodies |
| See what one action triggered | note the last id, act, then `list_requests(since_id=...)` |
| Wait for a background XHR instead of guessing a sleep | `wait_for_request(url_contains="/api/")` |
| Re-send a request, modified — Burp's Repeater | `replay_request(flow_id=8, headers={...})` |
| Check an endpoint without the session cookie | `replay_request(flow_id=8, remove_headers=["Cookie"])` |
| Overview of hosts, status codes, content types | `traffic_stats()` |
| Hand the traffic to another tool, or keep evidence | `export_har(save_path="run.har")` |
| Isolate the next action's traffic | `clear_traffic()` |

`replay_request` reuses the original request's headers and cookies, so it
replays an authenticated call exactly as the browser made it and only changes
what you override. It travels through the proxy too, so the replay joins the
history marked `R`. Redirects are not followed by default, so you see the real
response rather than its destination.

#### Cutting out the noise

Chrome chatters to its own update and telemetry endpoints. A `capture_filter`
records only what you care about, using
[mitmproxy's filter syntax](https://docs.mitmproxy.org/stable/concepts-filters/):

```
start_browser(capture_filter="~d example.com")          # one domain
start_browser(capture_filter="~d example.com & ~m POST") # and only POSTs
start_browser(capture_filter="~u /api/")                 # URL contains /api/
```

#### Limits and knobs

- Flows live **in memory** in a ring buffer (2000 by default, oldest dropped);
  the history survives `close_browser`, but not restarting the server. Use
  `export_har` to keep anything.
- Bodies are stored up to 200 kB each; above that they are truncated and the
  real size is reported. `get_request(max_body_chars=...)` controls how much is
  *displayed* (4000 by default), so raise it to read a whole JS bundle.
- Binary bodies (images, fonts) are reported with their size rather than dumped.
- `start_proxy(...)` raises those limits, or runs the proxy on a fixed port for
  an **external** client to use — anything you point at the returned address is
  captured too. Such a client must trust the CA file that `proxy_status` prints
  (browsers started here already do).
- `start_browser(capture_traffic=False)` turns capture off entirely.
- Traffic to `localhost` is captured as well; the usual proxy bypass for
  loopback is explicitly disabled.

### Locator strategies

Every element-targeting tool takes a `strategy` and a `selector`. Supported
strategies:

`id`, `name`, `css` (`css_selector`), `xpath`, `tag` (`tag_name`),
`class` (`class_name`), `link_text`, `partial_link_text`.

Example: `click(strategy="css", selector="button.submit")`.

Element lookups wait up to a configurable timeout (default 10s) for the element
to appear, so you rarely need an explicit `wait_for_element` first.

## Requirements

- Python 3.10+
- `mitmproxy` (installed with the dependencies below) for traffic capture
- A browser and its matching WebDriver on `PATH`:
  - Chrome/Chromium with `chromedriver`, **or**
  - Firefox with `geckodriver`

On Debian/Kali these come from the `chromium-driver` / `firefox-esr` +
`geckodriver` packages (Selenium 4.6+ can also fetch drivers automatically via
Selenium Manager).

## Installation

```bash
cd Selenium-MCP
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# or install as a package (provides the `selenium-mcp` command):
pip install -e .
```

## Running

```bash
# as a module
python -m selenium_mcp

# or, if installed as a package
selenium-mcp
```

The server speaks MCP over stdio and is normally launched by your client rather
than by hand.

## Connecting to Claude Code

Register it with the Claude Code CLI:

```bash
claude mcp add selenium -- python -m selenium_mcp
```

Or add it manually to your MCP client config (e.g. `claude_desktop_config.json`
or a project `.mcp.json`):

```json
{
  "mcpServers": {
    "selenium": {
      "command": "python",
      "args": ["-m", "selenium_mcp"],
      "cwd": "/home/kaptaan/Tools/Selenium-MCP"
    }
  }
}
```

If you use a virtualenv, point `command` at that interpreter, e.g.
`/home/kaptaan/Tools/Selenium-MCP/.venv/bin/python`.

## Example session

A typical flow the model can perform once connected:

1. `start_browser(browser="chrome", headless=true)`
2. `navigate(url="https://example.com")`
3. `get_title()` → `"Example Domain"`
4. `click(strategy="link_text", selector="More information...")`
5. `type_text(strategy="name", selector="q", text="hello", press_enter=true)`
6. `screenshot()` → image returned inline
7. `list_requests()` → every request the page made
8. `get_request(flow_id=3)` → that transaction as raw HTTP
9. `close_browser()`

Or, hunting for an API: navigate, click the thing, then
`list_requests(content_type="json")` to find the endpoint,
`get_request` to read its response, and `replay_request` to poke at it with a
changed parameter.

## Notes & safety

- Only **one** browser is open at a time per server process; call
  `close_browser` before starting another.
- `start_browser` passes `--no-sandbox` and `--disable-dev-shm-usage` so Chrome
  runs in containers and minimal environments. Headless is the default.
- `execute_script` runs arbitrary JavaScript in the page and `upload_file`
  reads local files — only connect this server to clients you trust, and point
  it at sites you are authorised to automate.
- Traffic capture is **MITM interception**: HTTPS is decrypted, so captured
  flows and exported HARs contain session cookies, tokens and anything else the
  page sent. Treat them as secrets, and only capture traffic you are authorised
  to intercept. `replay_request` re-sends real authenticated requests, so it
  can change state on the target — the same caution as Burp's Repeater applies.
- The proxy listens on `127.0.0.1` only, and its CA (in
  `~/.selenium-mcp/mitmproxy/`, override with `SELENIUM_MCP_MITM_CONFDIR`) is
  generated locally on first use. It is not installed into any system trust
  store: browsers started here are told to accept it for their session only.

## Project layout

```
selenium_mcp/
  __init__.py     package metadata
  __main__.py     `python -m selenium_mcp` entry point
  driver.py       WebDriver session management + locator helpers
  proxy.py        embedded mitmproxy, flow store, replay, HAR export
  server.py       FastMCP server and all tool definitions
pyproject.toml    packaging / `selenium-mcp` script
requirements.txt  runtime dependencies
```

## License

MIT
