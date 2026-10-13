"""WebDriver session management and shared helpers.

A single process-wide :class:`Session` holds at most one live browser. Every
MCP tool goes through this module so that locator resolution, waiting and
error handling behave consistently.

Unless capture is switched off, starting a browser also starts the embedded
mitmproxy from :mod:`selenium_mcp.proxy` and launches the browser behind it, so
every request/response it makes is recorded for the traffic tools to inspect.
"""

from __future__ import annotations

from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .proxy import ProxyError, proxy

# Map the friendly strings an LLM will produce to Selenium's By constants.
BY_STRATEGIES = {
    "id": By.ID,
    "name": By.NAME,
    "css": By.CSS_SELECTOR,
    "css_selector": By.CSS_SELECTOR,
    "xpath": By.XPATH,
    "tag": By.TAG_NAME,
    "tag_name": By.TAG_NAME,
    "class": By.CLASS_NAME,
    "class_name": By.CLASS_NAME,
    "link_text": By.LINK_TEXT,
    "partial_link_text": By.PARTIAL_LINK_TEXT,
}


class BrowserError(RuntimeError):
    """Raised for user-facing problems (no browser, bad locator, timeout…)."""


def resolve_by(strategy: str) -> str:
    key = (strategy or "").strip().lower()
    if key not in BY_STRATEGIES:
        raise BrowserError(
            f"Unknown locator strategy {strategy!r}. "
            f"Use one of: {', '.join(sorted(set(BY_STRATEGIES)))}."
        )
    return BY_STRATEGIES[key]


class Session:
    """Owns the lifecycle of a single WebDriver instance."""

    def __init__(self) -> None:
        self._driver: Optional[WebDriver] = None
        self._default_timeout: float = 10.0
        # True when start() launched the proxy itself, so close() may stop it again.
        self._owns_proxy: bool = False

    # -- lifecycle ---------------------------------------------------------
    @property
    def driver(self) -> WebDriver:
        if self._driver is None:
            raise BrowserError(
                "No browser is open. Call start_browser first."
            )
        return self._driver

    @property
    def is_open(self) -> bool:
        return self._driver is not None

    def start(
        self,
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
        if self._driver is not None:
            raise BrowserError(
                "A browser is already open. Call close_browser first."
            )

        browser = (browser or "chrome").strip().lower()
        self._default_timeout = default_timeout

        # Bring the proxy up first: the browser must be launched already pointing
        # at it, and a capture failure should not leave a stray browser behind.
        started_proxy = False
        if capture_traffic and not proxy.is_running:
            try:
                proxy.start(port=proxy_port, capture_filter=capture_filter)
            except ProxyError as exc:
                raise BrowserError(f"Could not start traffic capture: {exc}") from exc
            started_proxy = True

        try:
            if browser in ("chrome", "chromium"):
                opts = ChromeOptions()
                if headless:
                    opts.add_argument("--headless=new")
                opts.add_argument(f"--window-size={window_width},{window_height}")
                opts.add_argument("--no-sandbox")
                opts.add_argument("--disable-dev-shm-usage")
                opts.add_argument("--disable-gpu")
                if browser == "chromium":
                    opts.binary_location = "/usr/bin/chromium"
                if user_agent:
                    opts.add_argument(f"--user-agent={user_agent}")
                if capture_traffic:
                    opts.add_argument(f"--proxy-server={proxy.proxy_url}")
                    # Chrome bypasses the proxy for localhost by default; <-loopback>
                    # turns that off so traffic to local apps is captured too.
                    opts.add_argument("--proxy-bypass-list=<-loopback>")
                    # Trust mitmproxy's on-the-fly certificates.
                    opts.set_capability("acceptInsecureCerts", True)
                self._driver = webdriver.Chrome(
                    service=ChromeService(), options=opts
                )
            elif browser == "firefox":
                opts = FirefoxOptions()
                if headless:
                    opts.add_argument("--headless")
                opts.add_argument(f"--width={window_width}")
                opts.add_argument(f"--height={window_height}")
                if user_agent:
                    opts.set_preference("general.useragent.override", user_agent)
                if capture_traffic:
                    opts.set_preference("network.proxy.type", 1)  # manual
                    opts.set_preference("network.proxy.http", proxy.host)
                    opts.set_preference("network.proxy.http_port", proxy.port)
                    opts.set_preference("network.proxy.ssl", proxy.host)
                    opts.set_preference("network.proxy.ssl_port", proxy.port)
                    # Firefox also skips the proxy for localhost unless told otherwise.
                    opts.set_preference("network.proxy.allow_hijacking_localhost", True)
                    opts.set_capability("acceptInsecureCerts", True)
                self._driver = webdriver.Firefox(
                    service=FirefoxService(), options=opts
                )
            else:
                raise BrowserError(
                    f"Unsupported browser {browser!r}. Use 'chrome', "
                    "'chromium' or 'firefox'."
                )

            self._driver.set_window_size(window_width, window_height)
        except Exception:
            # Don't leave an orphaned proxy behind if the browser won't start.
            if started_proxy:
                proxy.stop()
            raise

        self._owns_proxy = started_proxy
        if capture_traffic and proxy.is_running:
            capture = (
                f" Traffic capture is on via {proxy.proxy_url}"
                + (f" (filter: {proxy.capture_filter})" if proxy.capture_filter else "")
                + " — use list_requests to see what the browser requests."
            )
        else:
            capture = " Traffic capture is off."
        return (
            f"Started {browser} (headless={headless}) at "
            f"{window_width}x{window_height}.{capture}"
        )

    def close(self) -> str:
        if self._driver is None:
            return "No browser was open."
        try:
            self._driver.quit()
        finally:
            self._driver = None
        if self._owns_proxy:
            # Captured flows live on in the store, so history stays readable.
            self._owns_proxy = False
            proxy.stop()
            return "Browser closed and traffic capture stopped (history retained)."
        return "Browser closed."

    # -- element location --------------------------------------------------
    def find(
        self,
        strategy: str,
        selector: str,
        timeout: Optional[float] = None,
        clickable: bool = False,
    ) -> WebElement:
        """Locate a single element, waiting for it to appear."""
        by = resolve_by(strategy)
        wait = WebDriverWait(
            self.driver,
            self._default_timeout if timeout is None else timeout,
        )
        condition = (
            EC.element_to_be_clickable((by, selector))
            if clickable
            else EC.presence_of_element_located((by, selector))
        )
        try:
            return wait.until(condition)
        except Exception as exc:  # TimeoutException and friends
            raise BrowserError(
                f"No {'clickable ' if clickable else ''}element matched "
                f"{strategy}={selector!r} within {timeout or self._default_timeout}s."
            ) from exc

    def find_all(self, strategy: str, selector: str) -> list[WebElement]:
        by = resolve_by(strategy)
        return self.driver.find_elements(by, selector)


# Process-wide singleton used by all tools.
session = Session()
