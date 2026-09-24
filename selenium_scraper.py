#!/usr/bin/env python3
"""
bbb-scraper — Selenium edition (secondary engine)
=================================================

The same scrape as playwright_scraper.py, driven through Selenium. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money — the decisions that determine all three live in page_flow.py
and output_writer.finish_run(), so this file is browser plumbing and nothing
else.

    --mode search     (default)  /search?find_text=…&find_loc=…
    --mode category              /us/category/{slug}
    --mode profile               one business profile page

Like its twins, this engine fetches BBB's OWN endpoint for the two listing
modes and the rendered page for profiles — see playwright_scraper.py's
header for why, and product_parser.py for what the endpoint carries.

Two limits of this engine, stated here rather than left to be discovered.
Neither is a bug in this code and neither can be fixed from here:

  * **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
    `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put a password. So --cdp-endpoint here works only for an endpoint that
    needs no credentials; a credentialed one is refused with exit 2 rather
    than connected to and silently failing.
  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=` accepts
    no credentials, and there is no equivalent of pyppeteer's
    `page.authenticate`. Credentials are stripped and a warning says so, so
    nobody believes a `user:pass` URL is doing something.

On BBB those two limits bite in exactly one place, and it is worth knowing
which: the two LISTING modes do not need a credentialled exit at all — BBB's
endpoint answered a datacenter address normally — so this engine reads them
as well as its twins do. `--mode profile` fetches a Cloudflare-gated page,
and that is where an engine that cannot authenticate an exit is the wrong
tool.

There is no --concurrency here either: parallel page fetching lives in the
Playwright engine.

Usage
-----
    python selenium_scraper.py --text restaurants --location "New York, NY" \\
        --pages 3

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          Selenium 4 fetches a matching chromedriver itself; a local Chrome
          or Chromium must be installed.
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urlsplit

from selenium import webdriver
from selenium.common.exceptions import (TimeoutException, WebDriverException,
                                        JavascriptException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS)
from product_parser import (API_PATH, DEFAULT_SORT, SORTS, api_url,
                            api_url_from_listing_url, category_from_url,
                            category_options, detect_bot_challenge,
                            is_supported_url, listing_url, page_url,
                            parse_listing, parse_profile, pick_category_option,
                            profile_parts, references_own_assets)
from output_writer import (dedupe_by_key, finish_run, EXIT_API_ERROR,
                           SOURCE_DEFAULT)
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The share of rows that must carry the columns BBB populates on every
# record. There is no price on this site; what stands in its place is the
# handful of fields BBB filled on 105 of 105 records across five listing
# captures. Kept byte-identical to the Playwright engine's — the two must not
# disagree about what a healthy page looks like.
CORE_FIELD_FLOOR = 99
CORE_FIELDS = ("title", "url", "sku", "category", "latitude")

# A page holding less than this share of the fullest page in the same run is
# reported as thin. BBB's page size is FIXED at 15, so the only legitimately
# short page is the last one of a listing.
THIN_PAGE_SHARE = 0.6

PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Chromium's own names for "the proxy is the problem, not the site". A dead
# proxy and a slow page want opposite responses — a different exit versus
# another try at the same one — so they are told apart by the error text.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    # BBB's OWN arithmetic, verbatim: how many businesses the query matched
    # and how many pages the site will serve. Real numbers here, unlike the
    # sibling repos' result headers — they are what pages are planned from.
    total_available: Optional[int] = None
    pages_available: Optional[int] = None
    # Which ordering BBB says it applied, read back from the response rather
    # than echoed from the request.
    sort_applied: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a driver's connection
# error can repeat the endpoint several times (the message plus a call log),
# so a masker that handled only the first occurrence would print the password
# the other times and look like it was working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Takes arbitrary text, not just a URL, because the strings that most need
    this are exception messages with a URL inside them. The host and port are
    KEPT — which endpoint or exit a run used is the useful half of the line
    and is not the secret.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version.

    `driver.capabilities["browserVersion"]` is the installed Chrome's version,
    so the claim matches what the JS engine and the TLS handshake report. A
    hardcoded number drifts the moment Chrome updates, and claiming an older
    Chrome than everything else reports is itself a signal.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason.

    chromedriver takes a bare address here and cannot send credentials, so an
    endpoint that carries them cannot work through this engine. Refused up
    front: connecting anyway would fail somewhere further in with an error
    that names none of this.
    """
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser — and a
    fresh browser is also the only thing that re-rolls the served page
    fresh cookie jar is what an ordinary user on another network looks like.
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover.
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        # Not a fingerprint measure, a correctness one: without it Chrome
        # advertises "HeadlessChrome", which is a giveaway on any site with
        # a bot manager in front of it.
        options.add_argument("--disable-blink-features=AutomationControlled")
        # Flag parity with the Playwright engine, and really applied rather
        # than accepted and ignored: Chrome takes the locale as --lang. It
        # does NOT decide which BBB directory is searched — that is
        # find_country in the URL — so this only affects what the browser
        # claims about itself.
        options.add_argument(f"--lang={self.args.locale}")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            # Set over CDP rather than as a launch switch, so it can use the
            # version the driver actually reports.
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # Explicit, because a driver that stops answering otherwise hangs the
        # run: "every remote call is bounded" applies to this engine too.
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            # The same patch script the Playwright engine installs on its
            # context. Shared deliberately: two engines applying different
            # halves of one fingerprint would be a contradiction of exactly
            # the kind a fingerprint is meant to avoid.
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is why page_flow names
# operations instead of passing JavaScript.
def _driver(session):
    driver = session.driver

    def count(selector):
        try:
            return len(driver.find_elements(By.CSS_SELECTOR, selector))
        except WebDriverException as e:
            logger.debug("count(%s) failed: %s", selector, e)
            return 0

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return driver.page_source
        except WebDriverException as e:
            # A URL canonicalisation can navigate, so a snapshot can land on
            # the document swap. None tells the caller to skip a check rather
            # than fail the run.
            logger.debug("page_source unavailable (page navigating?): %s", e)
            return None

    def current_url():
        try:
            return driver.current_url
        except WebDriverException:
            return ""

    # No scroll primitive, and its absence is measured rather than
    # forgotten: BBB serves its whole result set in the first response, so
    # there is nothing to scroll into view. Mirrors the Playwright engine.
    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url}


def _parse_for_mode(html: str, url: str, args, page_num: int = 1):
    """(rows, listing). Mirrors playwright_scraper._parse_for_mode exactly.

    `page_num` is threaded through rather than defaulted: `position` restarts
    at 1 on every page, so without the page number beside it a row from page
    2 claims the same position as one from page 1.
    """
    if args.mode == "profile":
        row = parse_profile(html, url)
        if row is not None and args.category:
            row.category = args.category
        return ([row] if row is not None else []), None
    listing = parse_listing(html, page=page_num, mode=args.mode, sort=args.sort)
    if args.category:
        for row in listing.rows:
            row.category = args.category
    return listing.rows, listing


def _is_endpoint(url: str) -> bool:
    """Whether this address is BBB's JSON endpoint rather than a page."""
    return urlparse(url or "").path == API_PATH


def _snapshot(session, url: str):
    """What the parser is given for this address.

    A PAGE is read with `page_source`. The ENDPOINT answers with JSON, which
    Chromium wraps in its own JSON-viewer markup — so `page_source` there
    returns the viewer's HTML and the payload would be unreachable. Reading
    the body text gives back exactly what the server sent.

    Note the JS dialect: a function BODY with an explicit `return`, not the
    arrow expression the other two engines pass. That difference is exactly
    why no JavaScript crosses the page_flow boundary.
    """
    if _is_endpoint(url):
        try:
            return session.driver.execute_script(
                "return document.body.innerText;") or ""
        except WebDriverException as e:
            logger.warning("Could not read the endpoint response: %s", e)
            return None
    return _driver(session)["content"]()


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same detectors, same reconciliation and the same "detected is not
    blocking" rule as the Playwright engine — the three must agree about
    when a run spends money.

    NOTE what this cannot help with: BBB's hard refusal is not an HTTP
    403 carrying its own error page with no challenge on it, so no solve
    applies there and none is attempted. See product_parser.detect_page_state.
    """
    driver = session.driver
    d = _driver(session)
    html = d["content"]()
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = d["count"](selector)
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, d["current_url"]())
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=d["current_url"]())
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False
    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it.", challenge.kind, challenge.source,
                    already_rendered)
        return False
    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s).", e)
        return False
    try:
        driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
    except WebDriverException as e:
        logger.error("Could not inject the token (%s).", e)
        return False
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    driver.refresh()
    return True


def _resolve_category(session, args):
    """(tobId, label) for a `/{cc}/category/{slug}` URL, or exits 2.

    Mirrors playwright_scraper._resolve_category exactly, including the
    refusal: BBB's category slugs are human addresses while the endpoint
    filters on a numeric tobId, and scraping whichever category the site
    ranked first while reporting success is the silent failure this family
    exists to avoid.
    """
    slug = category_from_url(args.url)
    probe = api_url(country=args.country or "USA",
                    text=slug.replace("-", " "), location=args.location or "",
                    page=1, sort=args.sort)
    logger.info("Resolving category %r to the id BBB filters on.", slug)
    session.driver.get(probe)
    options = category_options(_snapshot(session, probe) or "")
    option, exact = pick_category_option(options, slug)
    if option is None:
        offered = ", ".join("%s (%s)" % (o["label"], o["value"])
                            for o in options[:8]) or "nothing"
        logger.error("BBB has no category matching %r. For that term it "
                     "offers: %s. Refusing rather than scraping whichever "
                     "category it ranked first.", slug, offered)
        sys.exit(2)
    if not exact:
        logger.warning("Category %r resolved to %r (%s) on a plural/prefix "
                       "match rather than an exact one.", slug,
                       option["label"], option["value"])
    else:
        logger.info("Category %r is %r (%s).", slug, option["label"],
                    option["value"])
    return option["value"], option["label"]


def _target_url(args, category_id=None, category_label=None) -> str:
    """The address this run actually fetches. Mirrors playwright_scraper.

    BBB's own endpoint for the two listing modes — it carries the identical
    object and is not behind Cloudflare — and the rendered page for profiles,
    which have no endpoint at all.
    """
    if args.mode == "profile":
        return args.url
    return api_url_from_listing_url(args.url, sort=args.sort, state=args.state,
                                    category_id=category_id,
                                    category_label=category_label)


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    Kept structurally parallel to its twins on purpose — "all three engines
    agree" is checked by reading them side by side as well as by the smoke
    suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    d = _driver(session)
    html, state, load_failed = None, "ok", False

    # See the Playwright engine for the measurement: without a pool there is
    # no exit to rotate to, but a plain re-fetch is what clears a block on a
    # Scraping Browser profile, so the budget is not zero.
    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not just documented. It was a
    # constant with a paragraph of justification that no engine read — a
    # policy statement nothing enforced, which is the same defect as dead
    # code that looks load-bearing. Setting it False now really does stop
    # the retry loop.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.driver.get(url)
                load_failed = False
                break
            except (TimeoutException, WebDriverException) as e:
                text = str(e)
                reason = next((m for m in _PROXY_ERROR_MARKERS if m in text), "")
                load_failed = True
                if reason:
                    exit_failed = reason
                    break  # a different exit is the only thing that helps
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            d = _driver(session)
            continue
        if load_failed:
            break


        if handle_captcha_if_present(session, args):
            time.sleep(1)

        html = _snapshot(session, url) or ""
        state = page_flow.classify(html, None, d["current_url"]())

        # BBB server-renders its data, so a listing is parseable in the
        # FIRST response and there is nothing to wait for on a healthy page.
        # Measured with no pause at all after the load. The wait below is
        # only for the state that says BBB served SOMETHING that is not the
        # payload. Mirrors playwright_scraper exactly.
        if state == "unknown":
            wait_ms = page_flow.content_timeout_ms(args.mode)
            sel = page_flow.ready_selector(args.mode)
            need = page_flow.min_matches(args.mode)
            logger.info("Page %d is something BBB served (%d bytes, its own "
                        "assets referenced %d time(s)) but carries no listing "
                        "payload — waiting up to %.0fs rather than spending a "
                        "retry.", page_num, len(html),
                        references_own_assets(html), wait_ms / 1000.0)
            found = page_flow.wait_for_count(d["count"], sel, need, wait_ms,
                                             d["sleep"])
            if found < need:
                logger.info("Still nothing after %.0fs (%d match(es) for %s).",
                            wait_ms / 1000.0, found, sel)
            html = _snapshot(session, url) or html
            state = page_flow.classify(html, None, d["current_url"]())

        # The paid path is reached only for state "challenge" — BBB's
        # Managed Challenge, which IS a test. It is NOT reached for
        # "blocked": the hard "You have been blocked" page carries no widget
        # and no sitekey, so a solve there would be a charge for nothing.
        # Bounded by SOLVES_PER_PAGE. Mirrors playwright_scraper.
        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session, args):
                time.sleep(1)
                html = d["content"]() or html
                state = page_flow.classify(html, url=d["current_url"]())
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning("The solve was NOT accepted: page %d is "
                                   "still %s. The purchase is spent.",
                                   page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a hub category has no grid — so retrying it
            # would re-confirm the same right answer, and rotating the exit
            # would blame an address for the URL it was given.
            break

        # Blocked or challenged. The ADDRESS is what was scored, not the URL,
        # so a different exit is the only thing that plausibly changes the
        # outcome.
        if block_attempt < block_retries:
            logger.warning("Page %d came back as %s from %s — retrying from "
                           "another exit (%d/%d).", page_num, state,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"{state} on page {page_num}")
            session.relaunch()
            d = _driver(session)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # BBB refuses in TWO shapes and only one is solvable — see
        # playwright_scraper's twin of this block. This is the HARD refusal:
        # no widget, no sitekey, nothing a key could buy.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        logger.error(
            "BBB did not serve this request — %d bytes, its own asset hosts "
            "referenced %d time(s), saved to %s. There is no widget on this "
            "page and no key would help. What clears it, measured "
            "2026-09-16: an exit BBB does not score as a datacenter. Note "
            "this engine cannot use an authenticated remote CDP endpoint or "
            "an authenticated proxy; see the README's engine limits — which "
            "is why --mode profile is the one mode that needs a different "
            "engine here. This is exit 3, distinct from a genuinely empty "
            "result (exit 4).",
            len(html or ""), references_own_assets(html or ""), debug_html)
        outcome.blocked_by = "cloudflare (hard block)" if html else "no-response"
        outcome.final_url = d["current_url"]()
        return outcome

    # No readiness wait and no scroll on the content path, and their absence
    # is MEASURED rather than forgotten — see the "unknown" branch above.
    # Mirrors playwright_scraper.

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only when the page is NOT already content. A challenge marker on a
    # page whose products have rendered guards nothing — and over
    # --cdp-endpoint the Scraping Browser's own auto-solve extension injects
    # such markers into every page it loads.
    # Only for a state page_flow already counts as BLOCKED. An EMPTY page is
    # a correct answer, and a live run of a /p/<slug> hub reported exit 3 on
    # a page the site had plainly served because the hub's own performance
    # script names `akamaihd.net`. Mirrors playwright_scraper exactly.
    vendor = (detect_bot_challenge(html, url=d["current_url"]())
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    final_url = d["current_url"]() or url
    if not page_flow.should_parse(state):
        logger.info("Page %d came back as %s; nothing to parse.", page_num,
                    state)
        outcome.final_url = final_url
        return outcome

    products, listing = _parse_for_mode(html, final_url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if listing is not None:
        # BBB's own arithmetic, recorded on every page. Mirrors
        # playwright_scraper — the two engines must not disagree about what a
        # run holds.
        outcome.total_available = listing.total_results
        outcome.pages_available = listing.pages_available
        outcome.sort_applied = listing.sort
        if page_num == 1:
            logger.info("BBB reports %s result(s) across %s page(s) of %s, "
                        "ordered by %r.", listing.total_results,
                        listing.pages_available, listing.page_size,
                        listing.sort)
            reachable = (listing.pages_available or 0) * (listing.page_size or 0)
            if (listing.total_results or 0) > reachable:
                logger.warning(
                    "This query matches %s businesses but BBB will only serve "
                    "%d of them (%d page(s) x %s). The run will be COMPLETE as "
                    "a request and a %.1f%% sample as a directory — the "
                    "sidecar records both numbers.",
                    listing.total_results, reachable, listing.pages_available,
                    listing.page_size, 100.0 * reachable / listing.total_results)
        if listing.sort and args.sort and listing.sort != args.sort:
            logger.warning("Asked BBB for --sort %s and it applied %r. The "
                           "`sort` column records what it actually did.",
                           args.sort, listing.sort)

    if products:
        for field_name in CORE_FIELDS:
            filled = sum(1 for row in products
                         if getattr(row, field_name, None) not in (None, "", []))
            share = 100.0 * filled / len(products)
            if share < CORE_FIELD_FLOOR:
                logger.warning(
                    "Only %.0f%% of page %d carries `%s`, against a measured "
                    "floor of %d%%. Every record of every capture had one, so "
                    "this is the payload shape moving rather than the "
                    "businesses being unusual.", share, page_num, field_name,
                    CORE_FIELD_FLOOR)
        graded = sum(1 for row in products if row.rating_grade)
        accredited = sum(1 for row in products if row.is_accredited)
        if args.mode == "profile":
            logger.info("Profile: BBB grade %s, BBB Accredited: %s.",
                        products[0].rating_grade or "not graded",
                        "yes" if products[0].is_accredited else "no")
        else:
            logger.info("Page %d: %d/%d graded by BBB, %d/%d BBB Accredited "
                        "(ordering %r decides that second number, not the "
                        "data).", page_num, graded, len(products), accredited,
                        len(products), args.sort)

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = final_url
    return outcome


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # All three modes are one row per business-at-a-location, so `sku` is the
    # key for all of them.
    dedupe_key = "sku"
    # Only --mode profile is single-page. Both listing modes paginate
    # identically, so neither may be treated as single-page — that is the
    # silent-success failure this family exists to avoid.
    stop_reason = "single_page_mode" if args.mode == "profile" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    if args.concurrency > 1:
        logger.warning("--concurrency is ignored in this engine: parallel page "
                       "fetching is implemented in playwright_scraper.py, "
                       "which is the primary engine. Running one page at a "
                       "time.")

    session = None
    try:
        session = _Session(args, pool).open()

        category_id = category_label = None
        if args.mode == "category":
            category_id, category_label = _resolve_category(session, args)
        target = _target_url(args, category_id, category_label)
        if target != args.url:
            logger.info("Fetching BBB's own endpoint rather than the rendered "
                        "page: %s", target)

        first = _fetch_one_page(session, args, pool, 1, target)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif args.mode != "profile":
            seen_keys.update(p.sku for p in first.products if p.sku is not None)

            # Planned from BBB's OWN `totalPages` rather than chased through
            # next-links: the site states the number on page 1, and asking for
            # page 16 answers HTTP 500 rather than an empty page. Mirrors
            # playwright_scraper._plan_page_urls.
            page_one = first.final_url or target
            wanted = page_flow.pages_to_plan(args.pages, first.pages_available)
            if wanted < args.pages:
                logger.info("BBB reports %s page(s) for this query; %d were "
                            "asked for. Fetching %d — asking past the site's "
                            "own last page returns HTTP 500.",
                            first.pages_available, args.pages, wanted)
                stop_reason = "page_cap_reached"
            planned = [page_url(page_one, n) for n in range(2, wanted + 1)]

            for index, url in enumerate(planned):
                page_num = index + 2
                if pool and pool.rotates_per_page():
                    pool.advance(f"per-page rotation, page {page_num}")
                    session.relaunch()

                outcome = _fetch_one_page(session, args, pool, page_num, url)
                outcomes.append(outcome)
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break

                fresh_count = sum(1 for p in outcome.products
                                  if p.sku is None or p.sku not in seen_keys)
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                if not fresh_count:
                    logger.info("Page %d added no rows not already seen — "
                                "treating that as the end of the listing.",
                                page_num)
                    stop_reason = "no_new_products"
                    break

                if index + 1 < len(planned):
                    time.sleep(args.delay)
    finally:
        if session is not None:
            session.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "pages x rows-per-page" as a hard expectation, even though BBB's
    # page size is fixed at 15: the LAST page of a listing is legitimately
    # short. Mirrors playwright_scraper.
    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    pages_available = next((o.pages_available for o in outcomes
                            if o.pages_available is not None), None)
    if args.mode != "profile" and all_rows:
        counts = [(o.page_num, len(o.products)) for o in outcomes if o.ok]
        fullest = max((n for _, n in counts), default=0)
        thin = [(p, n) for p, n in counts
                if fullest and n < THIN_PAGE_SHARE * fullest]
        last_page = max((p for p, _ in counts), default=0)
        thin = [(p, n) for p, n in thin if p != last_page]
        if thin:
            logger.warning(
                "Page(s) %s came back much thinner than the fullest page "
                "(%d rows): %s. BBB serves a FIXED 15 per page, so a short "
                "page that is not the last one is a truncated response.",
                ", ".join(str(p) for p, _ in thin), fullest,
                ", ".join("page %d: %d" % (p, n) for p, n in thin))
        if total_available:
            logger.info("BBB reports %d business(es) for this query; this run "
                        "holds %d (%.1f%%).", total_available, len(all_rows),
                        100.0 * len(all_rows) / total_available)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    # Byte-identical in shape to the other two engines: BBB's own arithmetic,
    # which is what lets a consumer tell a complete-but-capped run from one
    # that covered the whole result set.
    extra = None
    if args.mode != "profile":
        sorts_applied = sorted({o.sort_applied for o in outcomes
                                if o.sort_applied})
        extra = {"total_results": total_available,
                 "pages_available": pages_available,
                 "sort_requested": args.sort,
                 "sort_applied": sorts_applied[0] if len(sorts_applied) == 1
                                 else sorts_applied}
        if total_available and pages_available:
            reachable = pages_available * 15
            if total_available > reachable:
                extra["capped_by_site"] = True
                extra["reachable_max"] = reachable

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=SOURCE_DEFAULT,
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="BBB (Better Business Bureau) scraper (Selenium edition). "
                    "Cannot authenticate a proxy or a remote CDP endpoint — "
                    "see the module docstring; playwright_scraper.py is the "
                    "primary engine.")
    p.add_argument("--url", default=None,
                   help="A bbb.org URL: /search?find_text=…&find_loc=…, "
                        "/{cc}/category/{slug}, or a business profile with "
                        "--mode profile. Optional for a search: --text and "
                        "--location build the URL instead. Also read from "
                        "BBB_URL in the environment or in .env.")
    p.add_argument("--text", default=None, metavar="QUERY",
                   help="What to search for. Builds a /search URL together "
                        "with --location. Ignored when --url is given.")
    p.add_argument("--location", default=None, metavar="PLACE",
                   help="Where to search, as BBB spells it: 'New York, NY'. "
                        "Ignored when --url is given.")
    p.add_argument("--country", choices=["USA", "CAN"], default=None,
                   help="Which BBB directory --text/--location searches "
                        "(default USA). REFUSED together with --url, because "
                        "a search URL already carries find_country.")
    p.add_argument("--sort", choices=sorted(SORTS), default=DEFAULT_SORT,
                   help="Which ordering to ask BBB for (default %(default)s). "
                        "Not cosmetic: 'best-match' returned 15/15 BBB "
                        "Accredited businesses and not one match for the "
                        "query, while 'a-z' returned 0/15 accredited and real "
                        "matches. See playwright_scraper for the measurement.")
    p.add_argument("--state", default=None, metavar="CODE",
                   help="Narrow a search to one state or province. The "
                        "documented way past BBB's 225-row ceiling.")
    p.add_argument("--mode", choices=["search", "category", "profile"],
                   default="search",
                   help="search (default), category, or profile. profile "
                        "reads one business profile page and adds the "
                        "accreditation date, years in business, entity type, "
                        "the website and the review and complaint totals. No "
                        "--pages in profile mode. NOTE: profile is the one "
                        "mode that fetches a Cloudflare-gated page, and this "
                        "engine cannot authenticate an exit — use the "
                        "Playwright or pyppeteer engine for it.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to BBB's own "
                        "category for each business.")
    p.add_argument("--pages", type=int, default=1,
                   help="Listing pages to fetch. BBB caps every query at 15 "
                        "pages of 15 and answers page 16 with HTTP 500, so a "
                        "run plans against the totalPages the site states on "
                        "page 1.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for flag parity and IGNORED here: parallel "
                        "page fetching lives in playwright_scraper.py.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "A page that comes back EMPTY is not retried: an empty "
                        "hub category is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="bbb_businesses", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US), passed to Chrome as "
                        "--lang. It does NOT decide which directory is "
                        "searched; that is find_country in the URL.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL. NOTE: Selenium cannot authenticate a "
                        "proxy; credentials are stripped and a warning says "
                        "so. Use the Playwright or pyppeteer engine for an "
                        "authenticated exit.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from 2captcha's Fingerprint API "
                        "and apply it over CDP. Needs --twocaptcha-key. "
                        "Ignored with --cdp-endpoint.")
    # ONE OS-family tag, not a list — and the default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop" in
    # this family, which the API rejects with HTTP 400 ("Request parameters
    # are invalid"), so --fingerprint failed on every invocation. Measured
    # 2026-09-10: `Windows` succeeds, and `Windows,Chrome,Desktop`, `Chrome`
    # and `Desktop` each 400. fingerprint_client.py's own --tags help has
    # said so all along; the engines' default contradicted it.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400, and no combination is accepted. Use "
                        "--fp-country to narrow further. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Note that "
                        "NO challenge has ever been observed on this site — a "
                        "refused request gets no page at all — so neither "
                        "setting has anything to act on today, and neither "
                        "helps with a refusal.")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Attach to a running browser at host:port. Must NOT "
                        "carry credentials — chromedriver's debuggerAddress "
                        "cannot send them, so a credentialed endpoint is "
                        "refused with exit 2 rather than silently failing.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    env_config.apply(args)

    # Spelled identically to playwright_scraper: --url and the query flags are
    # two ways to say the same thing, and only one may win.
    if args.url and (args.text or args.location or args.country or args.state):
        conflicting = [name for name, value in
                       (("--text", args.text), ("--location", args.location),
                        ("--country", args.country), ("--state", args.state))
                       if value]
        p.error("--url already carries the whole query; %s would have to "
                "agree with it and nothing here checks that they do."
                % ", ".join(conflicting))

    if not args.url and args.text is not None:
        if args.mode == "profile":
            p.error("--mode profile needs a --url.")
        args.url = listing_url(country=args.country or "USA", text=args.text,
                               location=args.location or "", page=1)
        logger.info("Built the listing URL from --text/--location: %s", args.url)

    if not args.url:
        p.error("no --url given and no --text: pass a bbb.org URL, or "
                "--text/--location to build a search. BBB_URL in the "
                "environment or in .env works too.")

    supported, why = is_supported_url(args.url)
    if not supported:
        p.error(f"{args.url!r} {why}.")

    if args.mode == "profile" and profile_parts(args.url) is None:
        p.error(f"--mode profile expects a business profile URL shaped "
                f"/{{cc}}/{{state}}/{{city}}/profile/{{category}}/{{name}}; "
                f"{args.url!r} is not one.")
    if args.mode != "profile" and profile_parts(args.url) is not None:
        p.error(f"{args.url!r} is a single business profile. Use --mode "
                f"profile for it.")
    if args.mode == "category" and category_from_url(args.url) is None:
        p.error(f"--mode category expects a /{{cc}}/category/{{slug}} URL; "
                f"{args.url!r} is not one.")

    if args.mode == "profile" and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode profile: there is one "
                       "page to read.", args.pages)
        args.pages = 1
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "remote browser supplies its own.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except Exception as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest
        # one: `profile_locked` means another run still holds this `pid`, and
        # a harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
