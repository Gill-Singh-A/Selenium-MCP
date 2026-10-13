# Selenium MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes
[Selenium WebDriver](https://www.selenium.dev/) as a rich set of tools, so an
MCP client such as **Claude** or **Claude Code** can fully operate a real web
browser — navigate, click, fill forms, run JavaScript, manage cookies, switch
tabs and frames, handle alerts, drive the mouse/keyboard, scrape the DOM and
take screenshots.

It drives Chrome, Chromium or Firefox through their standard WebDriver, and
runs over stdio.

## Features

A single browser session is managed per server process. The following **46
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
7. `close_browser()`

## Notes & safety

- Only **one** browser is open at a time per server process; call
  `close_browser` before starting another.
- `start_browser` passes `--no-sandbox` and `--disable-dev-shm-usage` so Chrome
  runs in containers and minimal environments. Headless is the default.
- `execute_script` runs arbitrary JavaScript in the page and `upload_file`
  reads local files — only connect this server to clients you trust, and point
  it at sites you are authorised to automate.

## Project layout

```
selenium_mcp/
  __init__.py     package metadata
  __main__.py     `python -m selenium_mcp` entry point
  driver.py       WebDriver session management + locator helpers
  server.py       FastMCP server and all tool definitions
pyproject.toml    packaging / `selenium-mcp` script
requirements.txt  runtime dependencies
```

## License

MIT
