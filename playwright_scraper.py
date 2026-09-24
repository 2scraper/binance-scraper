#!/usr/bin/env python3
"""
bbb-scraper — Playwright edition (primary engine)
=================================================

Scrapes bbb.org: keyword searches, category listings, and business profiles.

    --mode search     (default)  /search?find_text=…&find_loc=…
    --mode category              /us/category/{slug}, resolved to the tobId
                                 BBB actually filters on
    --mode profile               one /{cc}/{st}/{city}/profile/… page, read
                                 out of the page's own state: accreditation
                                 date, BBB file-opened date, years in
                                 business, entity type, and the review and
                                 complaint totals

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money; the shared decisions live
in output_writer.finish_run() and page_flow.py so they cannot drift apart.

What is different about BBB
---------------------------
* **The data is not in the DOM, and it is not JSON-LD either.** Every BBB
  page embeds `window.__PRELOADED_STATE__`, and on a listing page that object
  holds the site's own `/api/search` response verbatim — 15 complete business
  records with phone lists, letter grades, numeric scores, accreditation,
  categories and coordinates. So this engine navigates like a visitor and
  then reads the state, rather than scraping tiles. See product_parser.
* **That same endpoint answers without a browser at all.** `/api/search` is
  NOT behind Cloudflare: measured 2026-09-16, HTTP 200 and 57 KB of JSON from
  a datacenter address that gets 403 on every HTML page on the site. Which
  means `--mode search` and `--mode category` need no key and no proxy. Say
  so plainly rather than selling a product nobody needs here (§13).
* **A profile page is a different story, and it is the one that needs the
  paid path.** There is no profile endpoint — /api/businessprofile,
  /api/profile, /api/orgs, /api/reviews and /api/complaints all return BBB's
  404 page — so the only route to accreditation dates and complaint counts is
  the rendered page, and the rendered page is Cloudflare-gated.
* **BBB refuses in TWO shapes and both wear BBB's branding.** A Managed
  Challenge (`Just a moment...`, `cf_chl_opt`, Turnstile) and a hard
  "You have been blocked", both HTTP 403, both titled `… | Better Business
  Bureau®`. Only the first is solvable; the second is a refusal, not a test.
  page_flow.STATE_POLICY is where that distinction lives.
* **The site's own markers prove nothing.** `challenge-platform` and
  `cdn-cgi` appear on pages BBB serves normally — counted on its own 404 —
  so neither is a block marker. What discriminates is the challenge's own
  vocabulary and, positively, whether the page was built out of
  `assets.bbb.org` / `m.bbb.org`. Read product_parser's marker comment before
  adding anything to that list.
* **The default ordering is not a relevance ordering.** BBB's "Best Match"
  returned 15/15 accredited businesses on every page measured and, for
  `find_text=restaurants`, not one restaurant. `--sort` defaults to `a-z`
  here for that reason — this repo's one deliberate disagreement with the
  site — and `--sort best-match` reproduces what a visitor sees.
* **Pagination stops at 15 pages and page 16 is an HTTP 500.** Every listing
  response states `totalPages`, so pages are PLANNED from page 1's own
  arithmetic rather than discovered by walking off the end, which on this
  site manufactures a server error that reads like a bug in this code.

Usage
-----
    python playwright_scraper.py \\
        --text restaurants --location "New York, NY" \\
        --pages 3 --format both

    python playwright_scraper.py --mode category \\
        --url "https://www.bbb.org/us/category/restaurants"

    python playwright_scraper.py --mode profile \\
        --url "https://www.bbb.org/us/ny/bronx/profile/cleaning-services/proclean-maintenance-systems-inc-0121-134716"

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS,
                            RECAPTCHA_DISCOVERY_JS)
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
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


def _chrome_ua(chromium_version: str) -> str:
    """Build a desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded version number: that drifts the moment a newer Chromium
    ships, and a UA claiming an older Chrome than what the JS engine, WebGL
    strings and TLS ClientHello all actually report is itself a mismatch a
    fingerprinter can key on.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards rather than folded into shared
    state as the loop goes. Two reasons, and the second is the point:
    dedupe that mutates a running set inside the loop makes the OUTPUT depend
    on the order pages happen to arrive in — fine while that order is fixed,
    wrong the moment pages are fetched concurrently, because which page
    "claims" a duplicate sku (and so which `scraped_at` the row carries)
    would vary between runs of the same command. Merging afterwards in page
    order is deterministic regardless of arrival order.
    """
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The page_flow state this page came back as ("content", "blocked",
    # "challenge", "empty", "unknown"). Carried so the caller can tell an
    # EMPTY page — a /p/<slug> hub, a no-match query, or one page past the
    # end of a listing — from a page that failed. Both produce zero rows and
    # they mean opposite things.
    state: Optional[str] = None
    # What BBB itself said the result set was: `totalResults` and
    # `totalPages`, verbatim. Unlike the sibling repos, these are REAL
    # numbers rather than a header to be distrusted — they are the site's own
    # arithmetic, they are what pages are planned from, and they are the only
    # honest way to say that a complete 15-page run is a 1.2% sample of the
    # 19,016 businesses the query matched.
    total_available: Optional[int] = None
    pages_available: Optional[int] = None
    # Which ordering BBB says it applied, read back from the response's own
    # `sortTypes` rather than echoed from the request — so a sort the site
    # ignored shows up as a disagreement instead of as a column repeating
    # what we asked for.
    sort_applied: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


ITEM_LINK_SELECTOR = page_flow.READY_SELECTOR_LISTING

# The share of rows that must carry the columns BBB populates on every
# record, below which the read has broken rather than the data being
# unusual.
#
# There is no price on this site, so there is no price coverage; what stands
# in its place is the handful of fields BBB filled on 105 of 105 records
# across five listing captures — the name, the profile URL, the id, the
# category and the coordinates. Anything under this means the payload shape
# moved, not that the businesses are unusual.
#
# Deliberately NOT in this check: `rating_grade` (7 of 105 businesses are
# genuinely ungraded), `address` (13 of 15 — a service-area business has no
# street address) and `logo_url` (6 to 11 of 15). A floor on any of those
# would fire on healthy data.
CORE_FIELD_FLOOR = 99
CORE_FIELDS = ("title", "url", "sku", "category", "latitude")

# A page holding less than this share of BBB's own `pageSize` is reported as
# thin. The site's page size is FIXED at 15 and every captured page held
# exactly 15, so the only legitimately short page is the last one of a
# listing — which is why this can sit high without false alarms.
THIN_PAGE_SHARE = 0.6


# ---------------------------------------------------------------------------
# page_flow, bound to Playwright
# ---------------------------------------------------------------------------
# Every decision about WHAT to do with a page — how long to wait, when to
# scroll, when a fresh session is the only fix — lives in page_flow.py so all
# three engines make it identically. What lives here is only HOW to ask this
# particular driver. See page_flow's docstring for why that split exists.
def _driver(page):
    # Named OPERATIONS rather than JavaScript, and that is the point of the
    # split. Selenium's execute_script takes a function BODY with an explicit
    # `return` while Playwright and pyppeteer take `() => expr`, so a shared
    # module handing JS across this boundary would quietly acquire one
    # driver's dialect.
    #
    # There is no scroll primitive here, and its absence is measured rather
    # than forgotten: BBB paints its whole result set from
    # `__PRELOADED_STATE__` in the first response. All 15 records are in the
    # document before any scrolling could happen, on every capture, so a
    # scroll would be ceremony that looks load-bearing.
    return {
        "count": lambda selector: len(page.query_selector_all(selector)),
        "sleep": page.wait_for_timeout,
        "content": lambda: _content_when_settled(page),
        "current_url": lambda: page.url,
    }


def _ready_selector(args) -> str:
    return page_flow.ready_selector(args.mode)


def _min_matches(args) -> int:
    return page_flow.min_matches(args.mode)


def _classify(page, html: str, status=None) -> str:
    return page_flow.classify(html, status, page.url)

# Every readiness constant and every state policy lives in page_flow.py, with
# its measurement beside it. Nothing about WHAT to do with a page is
# duplicated here — this file only knows HOW to ask Playwright.


def _fetch_text(session, url: str, timeout_ms: int = 60000) -> str:
    """Navigate and return the document's text. Used for the endpoint.

    `innerText` rather than `content()`: the endpoint answers with JSON, and
    Chromium wraps a JSON document in its own viewer markup. Reading the body
    text gives back exactly what the server sent.
    """
    session.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    return _snapshot(session.page, url) or ""


def _resolve_category(session, args):
    """(tobId, label) for a `/{cc}/category/{slug}` URL, or exits 2.

    Costs one extra request, and the alternative is worse: BBB's category
    slugs are human addresses while the endpoint filters on a numeric tobId,
    and a hardcoded slug -> id table would be wrong the first time BBB
    renamed a category — silently, with the run reporting success on another
    category's businesses.

    The mapping is published by BBB itself, in every response's
    `filters.byId.filter_category.filterOptions`, so it is read from the site
    rather than stored here. Resolution REFUSES when nothing plausibly
    matches instead of taking BBB's first-ranked option: scraping the wrong
    category while reporting success is exactly the silent failure this
    family exists to avoid (§5, §8).
    """
    slug = category_from_url(args.url)
    probe = api_url(country=args.country or "USA",
                    text=slug.replace("-", " "), location=args.location or "",
                    page=1, sort=args.sort)
    logger.info("Resolving category %r to the id BBB filters on.", slug)
    options = category_options(_fetch_text(session, probe))
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
                       "match rather than an exact one — check that is the "
                       "category you meant.", slug, option["label"],
                       option["value"])
    else:
        logger.info("Category %r is %r (%s).", slug, option["label"],
                    option["value"])
    return option["value"], option["label"]


def _target_url(args, category_id=None, category_label=None) -> str:
    """The address this run actually fetches.

    For the two listing modes that is BBB's OWN endpoint rather than the
    rendered page, and the reason is the whole shape of this repo: the
    endpoint carries the identical object (see product_parser) at a seventh
    of the bytes, AND it is not behind Cloudflare — measured 2026-09-16,
    HTTP 200 from a datacenter address that gets 403 on every HTML page on
    the site. So `--mode search` and `--mode category` work with no key and
    no proxy, which is worth more than looking like a visitor.

    For `--mode profile` there is no endpoint at all — every
    /api/businessprofile-shaped URL tried returns BBB's 404 page — so the
    rendered page is fetched, and that one IS gated.
    """
    if args.mode == "profile":
        return args.url
    return api_url_from_listing_url(args.url, sort=args.sort, state=args.state,
                                    category_id=category_id,
                                    category_label=category_label)


def _plan_page_urls(args, page_one_url: str,
                    pages_available: Optional[int]) -> List[str]:
    """URLs for pages 2..N, decided once from what page 1 reported.

    On most sites in this family this function has to hedge: it compares the
    site's own next-link against what the URL convention would build, and
    falls back to chaining link-to-link when the two disagree, because a
    constructed URL that the site does not honour produces a complete-looking
    run holding page 1.

    BBB needs none of that hedging, and the reason is better than a
    selector: **every listing response states its own `totalPages`**. The end
    of the listing is a number the site gives us on page 1, not something
    discovered by walking off it — and walking off it is not free here, since
    `page=16` answers HTTP 500 rather than an empty page. So pages are
    planned arithmetically and the plan is CAPPED by the site's own figure.

    That the convention works at all was verified rather than assumed:
    `page=2` on a search returned a full second page of fifteen businesses
    sharing zero ids with page 1.
    """
    wanted = page_flow.pages_to_plan(args.pages, pages_available)
    if wanted < args.pages:
        logger.info("BBB reports %s page(s) for this query; %d were asked "
                    "for. Planning %d — asking past the site's own last page "
                    "returns HTTP 500, not an empty page.",
                    pages_available, args.pages, wanted)
    return [page_url(page_one_url, n) for n in range(2, wanted + 1)]


# Chromium's own names for "the proxy is the problem, not the site". Matched
# on the error text because Playwright surfaces them as a generic Error.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",     # nothing listening / refused
    "ERR_TUNNEL_CONNECTION_FAILED",    # CONNECT rejected by the proxy
    "ERR_PROXY_AUTH_UNSUPPORTED",      # auth scheme we cannot satisfy
    "ERR_PROXY_AUTH_REQUESTED",        # credentials missing or wrong
    "ERR_UNEXPECTED_PROXY_AUTH",
    "ERR_PROXY_CERTIFICATE_INVALID",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one.

    Distinguishing this from an ordinary timeout matters because the two want
    opposite responses: a timeout deserves a retry from the same exit, while
    an unusable exit deserves a different exit — retrying it unchanged just
    spends the retry budget on a proxy that is not going to answer.
    """
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    Factored out of scrape() so a proxy rotation can tear the whole browser
    down and call this again. Swapping the proxy under a live session would
    be cheaper and wrong: cookies a bot manager issued against one exit,
    replayed from another, are a stronger signal than either address alone.
    A rotation therefore means a genuinely fresh browser — new cookie jar,
    new storage — which is what an ordinary user on a different network
    looks like.
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    # Only override the UA when we launched our own bundled Chromium.
    # Forcing a UA on a page reached via --cdp-endpoint mismatches the remote
    # browser's real TLS/JS fingerprint on purpose-matched values.
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        # Only meaningful on this branch. Over --cdp-endpoint the Scraping
        # Browser already has its own fingerprint, and layering a second one
        # on top produces a mismatch rather than better cover.
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        # Must be installed on the context, before any page script runs.
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _BrowserSession:
    """One browser + context + page, relaunchable onto a different exit.

    Exists because a rotation replaces all three handles at once, and passing
    three mutable locals through every helper is how one of them ends up
    stale. It also gives a worker thread a single object to own: with
    Playwright's sync API, a browser and everything reachable from it belong
    to the thread that created them, so each worker builds its own.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    def relaunch(self):
        """Tear the browser down and come back on the pool's current exit.

        On a remote browser this is a no-op — its exit is not ours to change.
        """
        if self.remote:
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    # Explicit timeout. Playwright defaults to 30s here, but stating it makes
    # the contract visible next to the pyppeteer twin, which has no connect
    # timeout at all. A Scraping Browser session that is still held answers
    # with HTTP 500 rather than stalling, so this mostly guards against the
    # endpoint going quiet.
    try:
        browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
    except (PWError, PWTimeout) as e:
        # Playwright puts the endpoint it tried into the exception text, and
        # the endpoint is a URL with the password in it. Unmasked, that
        # password lands in the terminal, in CI output and in any log the run
        # is piped to — which is the one thing this project promises does not
        # happen ("credentials never reach argv or logs"). The message is
        # rewritten with the credentials masked and the host and port kept,
        # because WHICH endpoint failed is the useful half and is not the
        # secret.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"A Scraping Browser profile allows ONE live connection at a "
            f"time, so a 500 here usually means another run still holds this "
            f"`pid`. Wait for it to finish, or use a different pid."
        ) from None
    # Reuse the remote browser's existing context so its
    # fingerprint/session/proxy settings stay intact.
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    # The Scraping Browser API exposes a documented CDP domain
    # (`Captcha.setAutoSolve` / `Captcha.solve`) that clears supported
    # challenges inside the browser: https://2captcha.com/scraper/browser-api/api
    # Tried first when --cdp-endpoint is set; this script's own detect+solve
    # logic still runs as a fallback if the endpoint does not support it.
    # Note it does NOT cover BBB's HARD refusal, which is not a challenge:
    # "You have been blocked" carries no widget and no sitekey, so there is
    # nothing for any solver to do and a different exit is the only answer.
    # It DOES cover the Managed Challenge, which is a real Turnstile and is
    # what this path exists for.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.waitForSolve", lambda *_: logger.info("[Scraping Browser] CAPTCHA sent to 2captcha for solving."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled — supported "
                    "challenge types will be solved automatically if this "
                    "--cdp-endpoint is a Scraping Browser API session.")
    except Exception as e:
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


def _resolve_pagination_url(base_url: str, href: str) -> str:
    """Resolve a pagination link's raw href against the page it came from.

    Playwright's get_attribute("href") returns the raw HTML attribute,
    unresolved — unlike the DOM .href property Puppeteer/Selenium read for
    the same purpose in this project, which the browser resolves for you.
    urljoin handles every shape correctly — absolute, protocol-relative,
    absolute-path, and page-relative hrefs alike.
    """
    return urljoin(base_url, href)


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a Playwright connection
# error repeats the endpoint five times (the message plus a four-line call
# log), so a masker that handled only the first occurrence would print the
# password four times and look like it was working.
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


def _content_when_settled(page, attempts: int = 4, pause_ms: int = 700):
    """page.content() that tolerates a page mid-navigation.

    Playwright raises `Page.content: Unable to retrieve content because the
    page is navigating and changing the content` if the document swaps under
    it. BBB does not geo-redirect — the country is a path segment and a
    query parameter, not a function of the exit address — but a listing URL
    can canonicalise itself (`find_loc=New+York%2C+NY` comes back as
    `New York, NY`), so a snapshot taken right after goto() can land exactly
    on a swap.

    Retries briefly and returns None if the page won't hold still, so the
    caller can skip a check instead of failing the run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return page.content()
        except PWError as e:
            if "navigating" not in str(e).lower():
                raise
            if attempt == attempts:
                logger.warning("Page kept navigating through %d attempts — "
                               "continuing without a snapshot.", attempts)
                return None
            logger.info("Page is navigating (a URL canonicalisation?) — "
                        "retrying content() in %dms (%d/%d).",
                        pause_ms, attempt, attempts)
            page.wait_for_timeout(pause_ms)
    return None


def _is_endpoint(url: str) -> bool:
    """Whether this address is BBB's JSON endpoint rather than a page."""
    return urlparse(url or "").path == API_PATH


def _snapshot(page, url: str) -> Optional[str]:
    """What the parser is given for this address.

    Two shapes, because the two addresses answer with two things and
    Chromium does not hand them over the same way. A PAGE is read with
    `content()`. The ENDPOINT answers with JSON, which Chromium wraps in its
    own JSON-viewer markup — so `content()` there returns the viewer's HTML
    and the payload would be unreachable. `document.body.innerText` gives
    back exactly what the server sent.

    Getting this wrong is silent: the viewer markup parses as "not a listing
    payload", which reads as an empty result rather than as a bug.
    """
    if _is_endpoint(url):
        try:
            return page.evaluate("() => document.body.innerText") or ""
        except (PWError, PWTimeout) as e:
            logger.warning("Could not read the endpoint response: %s", e)
            return None
    return _content_when_settled(page)


def handle_captcha_if_present(page, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Runs after EVERY navigation, for ANY page — not scoped to one URL. The
    static-HTML and runtime reCAPTCHA detectors are run and reconciled
    against each other rather than short-circuited, because they can disagree
    about the variant and the parameters for one are rejected for the other.

    NOTE what this cannot help with. BBB's hard refusal is not a page at
    all — the HTTP/2 stream is reset and nothing arrives — so a 2Captcha key
    does nothing about the state a blocked run is most likely to meet, and
    `detect_page_state` reports that as "blocked" rather than "challenge"
    precisely so no solve is attempted or billed. No challenge has ever been
    observed on this site. This path exists because a bot manager can be
    switched on between deploys, and because the family's rule is that
    detection stays broad:
    different geos and scenarios surface different challenges.
    """
    html = _content_when_settled(page)
    if html is None:
        # Couldn't get a stable snapshot — skip detection for this navigation
        # rather than taking the whole run down. The next navigation gets
        # another chance, and the parse below reads its own copy of the DOM.
        return False

    # Detected is not the same as blocking. A challenge on a page whose
    # products are already rendered guards nothing, and counting the anchors
    # is instant — no wait_for_function, no 20s — which is why this check
    # sits here rather than after the readiness wait. Doing it the other way
    # round would cost 20 wasted seconds on a page the captcha genuinely
    # gates, where solving FIRST is what makes the content appear.
    already_rendered = len(page.query_selector_all(_ready_selector(args)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: page.evaluate(js), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False

    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it. Pass --solve-captcha always to "
                    "solve it anyway.", challenge.kind, challenge.source,
                    already_rendered)
        return False

    logger.warning("%s detected via %s (sitekey=%s, action=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey, challenge.action)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be "
                       "solved — continuing with whatever the page already "
                       "holds.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                               api_version=args.captcha_api,
                               min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("Solving the challenge failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    page.evaluate(INJECT_TOKEN_JS, token)
    logger.info("Token injected. Reloading page to continue.")
    page.wait_for_timeout(1500)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    return True


def _parse_for_mode(html: str, url: str, args, page_num: int = 1):
    """(rows, listing) for this mode. `listing` is None in --mode profile.

    `parse_profile` returns a single row or None; wrapping it here keeps
    every caller downstream — dedupe, merge, coverage logging, the writers —
    working on one shape instead of branching on the mode again.

    `page_num` is threaded through rather than defaulted, because `position`
    restarts at 1 on every page: without the page number beside it, a row
    from page 2 claims the same position as one from page 1 and the two are
    indistinguishable in the output. `smoke_test.py` asserts page+position
    is unique across a multi-page run for exactly that reason.
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


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Returns a PageOutcome and never raises for an EXPECTED failure — a
    timeout, a 403 refusal, a captcha page, a dead exit are all recorded on the
    outcome instead. What the run should do about them differs between the
    sequential and concurrent paths, so that decision belongs to the caller
    rather than to a raised exception unwinding through it.

    Always goes through `session.page`, never a captured local: a rotation
    replaces the browser, context and page together, and a stale handle is
    exactly the bug _BrowserSession exists to prevent.
    """
    outcome = PageOutcome(page_num=page_num, url=url)

    # How many times a blocked page may be retried.
    #
    # With a pool, each retry moves to a DIFFERENT exit and the budget is the
    # user's `--proxy-block-retries`. WITHOUT one — the ordinary case here,
    # because `--cdp-endpoint` brings its own exit — the retry re-fetches
    # through the same access path, and that is worth doing on this site
    # rather than giving up: a Scraping Browser profile was measured refusing
    # two requests and serving the third. Zero was the family default and it
    # made the first live run of this engine abandon page 1 on its first
    # block without retrying once.
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
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        # Retry a navigation timeout rather than ending the run on it. One
        # network flap on page 12 of 50 should not break the loop.
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                load_failed = False
                break
            except (PWTimeout, PWError) as e:
                # A dead or misconfigured proxy raises PWError
                # (net::ERR_PROXY_CONNECTION_FAILED), not PWTimeout —
                # catching only the latter lets it escape as a traceback,
                # which is the likeliest failure the first time anyone points
                # --proxy-file at a real list.
                reason = _proxy_failure(e)
                if reason:
                    exit_failed = reason
                    load_failed = True
                    break  # a different exit is the only thing that helps
                load_failed = True
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Timeout loading %s (attempt %d/%d) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            continue
        if load_failed:
            break

        if handle_captcha_if_present(session.page, args):
            # A solve navigated the page. Give the destination a moment
            # before judging what came back.
            session.page.wait_for_timeout(1000)

        html = _snapshot(session.page, url) or ""
        state = _classify(session.page, html)

        # BBB server-renders its data, so a listing is parseable in the
        # FIRST response and there is nothing to wait for on a healthy page.
        # Measured 2026-09-16 with no pause at all after
        # `wait_until="domcontentloaded"`: a search URL gave 15 rows and
        # `totalResults: 9470`, and a profile parsed in full. That is why
        # this engine has no scroll step and no readiness pause on the happy
        # path — either would be ceremony that looks load-bearing.
        #
        # The wait below is therefore only for the state that says BBB
        # served SOMETHING that is not the payload. That is the one case
        # where waiting can still help, and it is bounded.
        if state == "unknown":
            wait_timeout = page_flow.content_timeout_ms(args.mode)
            logger.info("Page %d is something BBB served (%d bytes, its own "
                        "assets referenced %d time(s)) but carries no listing "
                        "payload — waiting up to %.0fs rather than spending a "
                        "retry.", page_num, len(html),
                        references_own_assets(html), wait_timeout / 1000)
            found = page_flow.wait_for_count(
                lambda sel: len(session.page.query_selector_all(sel)),
                _ready_selector(args), _min_matches(args), wait_timeout,
                session.page.wait_for_timeout)
            if found < _min_matches(args):
                logger.info("Still nothing after %.0fs (%d match(es) for %s).",
                            wait_timeout / 1000, found, _ready_selector(args))
            html = _snapshot(session.page, url) or html
            state = _classify(session.page, html)

        # The paid path is reached only for state "challenge" — BBB's
        # Managed Challenge, which IS a test and can be solved. It is NOT
        # reached for "blocked": the hard "You have been blocked" page offers
        # no widget, no sitekey and no challenge of any kind, so a solve
        # there would be a charge for nothing. That distinction is the whole
        # reason page_flow separates the two states, and it is bounded by
        # SOLVES_PER_PAGE so a rotation loop cannot become a bill.
        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_captcha_if_present(session.page, args):
                session.page.wait_for_timeout(1000)
                html = _snapshot(session.page, url) or html
                state = _classify(session.page, html)
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the token works. This
                # line is what says whether the money bought anything.
                if state == "content":
                    logger.info("The solve was accepted — page %d is content "
                                "now.", page_num)
                else:
                    logger.warning(
                        "The solve was NOT accepted: page %d is still %s. The "
                        "purchase is spent.", page_num, state)

        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a hub category has no grid, and one page past
            # the end of a listing has no products — so retrying it would
            # spend the user's budget re-confirming the same right answer,
            # and rotating the exit would blame an address for the URL it was
            # given.
            break

        # Blocked or challenged. A different exit is the one thing that
        # plausibly changes the outcome: the ADDRESS is what was scored, not
        # the URL, so retrying it unchanged would only confirm it. Measured
        # 2026-09-09 — the same URL that answers 403 from a datacentre exit
        # answers 200 from a residential one.
        if block_attempt < block_retries:
            if has_pool:
                logger.warning("Page %d came back as %s from %s — retrying "
                               "from another exit (%d/%d).", page_num, state,
                               mask(pool.current), block_attempt + 1,
                               block_retries)
                pool.advance(f"{state} on page {page_num}")
                session.relaunch()
            else:
                # No pool, so nowhere else to go — but a plain re-fetch is
                # what clears this on a Scraping Browser profile. The browser
                # is NOT relaunched: over `--cdp-endpoint` a profile allows
                # one live connection, so tearing the session down and
                # reconnecting risks `profile_locked` and would lose the very
                # cookies the retry is meant to build on.
                pause = args.retry_delay * (block_attempt + 1)
                logger.warning("Page %d came back as %s — re-fetching through "
                               "the same access path in %.1fs (%d/%d). On this "
                               "site that is often what clears it.",
                               page_num, state, pause, block_attempt + 1,
                               block_retries)
                time.sleep(pause)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # What a caller needs here is WHICH refusal this is, because the two
        # want different answers and only one of them is solvable.
        #
        #   "You have been blocked | Better Business Bureau®"  — a refusal.
        #       No widget, no sitekey, nothing to solve. A different exit is
        #       the only move.
        #   "Just a moment..." with `cf_chl_opt`                — a test.
        #       That classifies as "challenge", not here, and IS solvable.
        #
        # Both are HTTP 403 and both wear BBB's own branding in the title, so
        # a reader who only sees "403" cannot tell them apart. Saying which
        # one arrived, and that this one has nothing to solve, is more use
        # than a captcha hint that would cost money.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html or "")
        assets = references_own_assets(html or "")
        logger.error(
            "BBB did not serve this request — %d bytes, its own asset hosts "
            "referenced %d time(s), saved to %s. This is the HARD refusal, "
            "not the Managed Challenge: there is no widget on it and no key "
            "would help. What clears it, measured 2026-09-16: an exit BBB "
            "does not score as a datacenter. A netcup VPS in Nuremberg was "
            "refused identically on six consecutive polls over 30 s, byte for "
            "byte, while the 2Captcha Scraping Browser was served normally. "
            "Use --cdp-endpoint, or a residential --proxy. This is exit 3, "
            "distinct from a genuinely empty result (exit 4).%s",
            len(html or ""), assets, debug_html,
            (f" Tried {block_retries + 1} exit(s)." if has_pool
             else f" Re-fetched {block_retries + 1} time(s)."))
        outcome.blocked_by = "cloudflare (hard block)" if html else "no-response"
        outcome.final_url = session.page.url
        return outcome

    # No readiness wait and no scroll on the content path, and their absence
    # is MEASURED rather than forgotten — see the "unknown" branch above.
    # BBB embeds the whole result set in the first response, so there is
    # nothing to wait for and nothing to scroll into view. Porting the
    # sibling repos' scroll loop here would be dead code that looks
    # load-bearing (CLAUDE.md §4).

    # Dumping on success, not only on failure: a run can return the right
    # NUMBER of rows with a field silently unpopulated, and then the only way
    # to tell a parsing bug from a too-early snapshot is to inspect the exact
    # bytes the parser was given.
    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only for a state page_flow already counts as BLOCKED, and that
    # narrowing was earned twice.
    #
    # A marker on a page whose products have rendered guards nothing — that
    # is the "detected is not blocking" rule the captcha default follows,
    # applied to the blocking decision instead of the spending one. But
    # `state != "content"` is still too wide: an EMPTY page is a correct
    # answer, and a live run of a /p/<slug> hub reported exit 3 on a 191 KB
    # page the site had plainly served, because the hub's own performance
    # script names `akamaihd.net` and "akamai" was in the marker list. Both
    # halves were wrong; the marker is gone (see
    # product_parser.BOT_CHALLENGE_MARKERS) and this now only refines the
    # REASON for a page the policy had already given up on.
    vendor = (detect_bot_challenge(html, url=session.page.url)
              if page_flow.counts_as_blocked(state) else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=f"{args.out}_page{page_num}_debug.png",
                                    full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s%s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html,
                     (f" (tried {block_retries + 1} exit(s))" if has_pool
                      else f" (re-fetched {block_retries + 1} time(s))"))
        outcome.blocked_by = vendor
        return outcome

    if not page_flow.should_parse(state):
        # Reached only for a state the policy says holds no rows — and it
        # says so in ONE place, so an engine cannot quietly decide to parse
        # something its twins would not.
        logger.info("Page %d came back as %s; nothing to parse.", page_num,
                    state)
        outcome.final_url = session.page.url
        return outcome

    products, listing = _parse_for_mode(html, session.page.url, args, page_num)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if listing is not None:
        # BBB states its own arithmetic on EVERY page, not just the first, so
        # it is recorded every time and page 1's copy is what the run plans
        # against. Unlike the sibling repos' result headers, these are
        # trustworthy: `totalPages` is exactly what the site will serve, and
        # asking for one more answers HTTP 500.
        outcome.total_available = listing.total_results
        outcome.pages_available = listing.pages_available
        outcome.sort_applied = listing.sort
        if page_num == 1:
            logger.info("BBB reports %s result(s) across %s page(s) of %s, "
                        "ordered by %r.", listing.total_results,
                        listing.pages_available, listing.page_size,
                        listing.sort)
            if (listing.total_results or 0) > (listing.pages_available or 0) * (listing.page_size or 0):
                reachable = (listing.pages_available or 0) * (listing.page_size or 0)
                logger.warning(
                    "This query matches %s businesses but BBB will only serve "
                    "%d of them (%d page(s) x %s). The run will be COMPLETE as "
                    "a request and a %.1f%% sample as a directory — the "
                    "sidecar records both numbers. Narrow the query (a "
                    "category, a smaller location, or --state) to reach more.",
                    listing.total_results, reachable, listing.pages_available,
                    listing.page_size, 100.0 * reachable / listing.total_results)
        if listing.sort and args.sort and listing.sort != args.sort:
            # Read back from the response rather than echoed from the
            # request, so an ordering the site quietly ignored shows up as a
            # disagreement instead of as a column repeating what we asked
            # for. It matters more here than elsewhere: the ordering decides
            # WHICH businesses are in the file, not just their order.
            logger.warning("Asked BBB for --sort %s and it applied %r. The "
                           "rows are ordered the site's way, and the `sort` "
                           "column records what it actually did.",
                           args.sort, listing.sort)

    if products:
        # No price on this site, so no price coverage. What stands in its
        # place is the handful of fields BBB filled on 105 of 105 records
        # across five listing captures — reported every time, so a consumer
        # gets the number rather than a threshold someone guessed.
        for field_name in CORE_FIELDS:
            filled = sum(1 for row in products if getattr(row, field_name, None) not in (None, "", []))
            share = 100.0 * filled / len(products)
            if share < CORE_FIELD_FLOOR:
                logger.warning(
                    "Only %.0f%% of page %d carries `%s`, against a measured "
                    "floor of %d%%. Every record of every capture had one, so "
                    "this is the payload shape moving rather than the "
                    "businesses being unusual — re-run with --dump-html.",
                    share, page_num, field_name, CORE_FIELD_FLOOR)
        graded = sum(1 for row in products if row.rating_grade)
        accredited = sum(1 for row in products if row.is_accredited)
        # Both reported, neither floored, and that is deliberate: 7 of 105
        # captured businesses are genuinely ungraded, and the ACCREDITED
        # share is a property of the ordering rather than of the data —
        # `--sort best-match` returned 15/15 accredited and `--sort a-z`
        # 0/15 from the identical query. A floor on either would fire on
        # perfectly healthy data.
        if args.mode == "profile":
            # One profile has no ordering, so the listing sentence would be
            # nonsense about a run of one row.
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
        debug_png = f"{args.out}_page{page_num}_debug.png"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.page.screenshot(path=debug_png, full_page=True)
        except Exception as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s and %s. Open the .png to see it.", debug_html, debug_png)

    outcome.products = products
    outcome.final_url = session.page.url
    return outcome


def _worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to a
    different offset. Two things fall out of that, both wanted:

      * Workers start on distinct exits, which is the point of running
        several — N workers all leaving from one address is just a faster way
        to burn that address.
      * No shared mutable state between threads, so rotation needs no lock.
        A worker that gets blocked can still walk the rest of the pool on its
        own.

    Its exit stays put for the worker's lifetime otherwise: a SESSION must
    not change address mid-flight, and a worker is one session.
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def _fetch_pages_concurrently(args, pool, specs, concurrency: int):
    """Fetch `specs` [(page_num, url), ...] across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one
    across threads is not an option even if it were desirable.
    """
    work = queue.Queue()
    for spec in specs:
        work.put(spec)

    results = []
    results_lock = threading.Lock()
    # Set when a page comes back with no rows at all — the end of the
    # listing. Without it, asking for 50 pages of a 5-page result would fetch
    # 45 empty ones. Workers check it before taking more work, so at most
    # (concurrency - 1) extra pages are in flight when it trips.
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                session = _BrowserSession(pw, args, _worker_pool(pool, index)).open()
                try:
                    first = True
                    while not exhausted.is_set():
                        try:
                            page_num, url = work.get_nowait()
                        except queue.Empty:
                            break
                        if not first:
                            time.sleep(args.delay)
                        first = False
                        outcome = _fetch_one_page(session, args, session.pool,
                                                  page_num, url)
                        with results_lock:
                            results.append(outcome)
                        if outcome.ok and not outcome.products:
                            logger.info("[%s] page %d returned no rows — "
                                        "treating that as the end of the listing "
                                        "and stopping dispatch.", name, page_num)
                            exhausted.set()
                finally:
                    session.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Anything still queued was never attempted (a worker died, or dispatch
    # stopped at the end of the listing). Not reported as failed pages: they
    # were not tried, and claiming otherwise would overstate the damage.
    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait()[0])
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    # One entry per page attempted, merged after the loop rather than folded
    # into shared state during it — see PageOutcome for why that ordering
    # matters more than it looks.
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # All three modes are one row per business-at-a-location, so `sku` is the
    # key for all of them.
    dedupe_key = "sku"
    # Why the loop ended. "completed" means every requested page was fetched;
    # "no_new_products" means the listing itself ran out (also a complete
    # result). "single_page_mode" is complete by construction — a detail page
    # has no page 2. Anything else is an early stop, and the run is only a
    # partial view.
    # "page_cap_reached" is the one that is specific to this site, and it is
    # a COMPLETE result: BBB caps `totalPages` at 15 however many businesses
    # match, so a run that fetched every page BBB offers fetched everything
    # the site will serve for that query.
    #
    # Only --mode profile is single-page. Both listing modes paginate
    # identically — the same `?page=N` on the same query — so neither may be
    # treated as single-page, which is the silent-success failure this family
    # exists to avoid.
    stop_reason = "single_page_mode" if args.mode == "profile" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None

    concurrency = max(1, args.concurrency)
    if concurrency > 1:
        if args.mode == "profile":
            logger.info("--concurrency is ignored in --mode profile: there is "
                        "one page to fetch.")
            concurrency = 1
        elif page_flow.concurrency_limit(args.cdp_endpoint) == 1:
            # The limit is page_flow's to state, not this engine's, so all
            # three refuse in the same place for the same reason.
            logger.warning("--concurrency is ignored with --cdp-endpoint: the "
                           "Scraping Browser API allows one live connection per "
                           "profile, and several workers would collide on it "
                           "(profile_locked). Use several pids instead, one run "
                           "each.")
            concurrency = 1
        elif not pool:
            logger.warning("--concurrency %d with no proxy pool: every worker "
                           "leaves from the SAME address, which is a faster way "
                           "to get that address scored than to gather data. "
                           "BBB refuses a scored address with a hard 403 that "
                           "no key can clear, so an address that works is one "
                           "worth not burning. Pass --proxy-file to spread "
                           "the load.\n"
                           "Note this warning does NOT apply to the endpoint "
                           "path: /api/search answered a datacenter address "
                           "normally, and it is the listing modes' data "
                           "source.", concurrency)
        if pool and pool.rotates_per_page():
            logger.info("--proxy-rotate per-page is redundant under "
                        "--concurrency: each worker already holds its own exit "
                        "for its lifetime, which is the same spread without a "
                        "browser relaunch per page.")
        if concurrency > 8:
            logger.warning("--concurrency %d means %d browsers at once "
                           "(~150-300MB each). Make sure the machine has the "
                           "memory for it.", concurrency, concurrency)

    with sync_playwright() as pw:
        session = _BrowserSession(pw, args, pool,
                                  remote=bool(args.cdp_endpoint)).open()
        try:
            category_id = category_label = None
            if args.mode == "category":
                category_id, category_label = _resolve_category(session, args)
            target = _target_url(args, category_id, category_label)
            if target != args.url:
                logger.info("Fetching BBB's own endpoint rather than the "
                            "rendered page: %s", target)

            # Page 1 is always fetched on its own: its content is what decides
            # how many pages 2..N there are to address at all.
            first = _fetch_one_page(session, args, pool, 1, target)
            outcomes.append(first)

            if not first.ok:
                stop_reason = ("page_load_timeout" if first.load_failed
                               else f"blocked_{first.blocked_by}")
                blocked = first.blocked_by is not None
            elif args.mode == "profile":
                pass  # one page is the whole run
            else:
                seen_keys.update(p.sku for p in first.products if p.sku is not None)
                planned = _plan_page_urls(args, first.final_url,
                                          first.pages_available)
                if len(planned) + 1 < args.pages:
                    # Capped by BBB's own `totalPages`, which is a complete
                    # answer rather than an early stop — see COMPLETE_STOP_REASONS.
                    stop_reason = "page_cap_reached"

                if args.pages > 1 and concurrency > 1 and not page_flow.pagination_is_addressable(first.final_url):
                    logger.warning("--concurrency %d requested, but this "
                                   "listing's pages cannot be addressed "
                                   "independently — falling back to one page "
                                   "at a time.", concurrency)
                    concurrency = 1

                if planned and concurrency > 1:
                    # Close the page-1 browser before starting workers: it has
                    # done its job, and holding it open would cost one more
                    # browser than asked for.
                    session.close()
                    specs = [(n, planned[n - 2]) for n in range(2, len(planned) + 2)]
                    logger.info("Fetching pages 2-%d across %d workers%s.",
                                len(planned) + 1, concurrency,
                                f" over {len(pool)} exit(s)" if pool else "")
                    rest, unattempted, exhausted = _fetch_pages_concurrently(
                        args, pool, specs, concurrency)
                    outcomes.extend(rest)

                    failed = [o for o in rest if not o.ok]
                    if failed:
                        worst = min(failed, key=lambda o: o.page_num)
                        stop_reason = ("page_load_timeout" if worst.load_failed
                                       else f"blocked_{worst.blocked_by}")
                        blocked = any(o.blocked_by for o in rest)
                    elif exhausted:
                        stop_reason = "no_new_products"
                    elif unattempted:
                        # Should not happen without a failure or exhaustion,
                        # but say so rather than reporting a complete run.
                        stop_reason = "pages_unattempted"
                    session = None  # already closed
                elif planned:
                    url = planned[0]
                    for page_num in range(2, len(planned) + 2):
                        # A new exit per page is what actually spreads a run's
                        # volume, and it costs a browser relaunch: carrying the
                        # session across exits would defeat the point.
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

                        # Whether this page contributed anything not already
                        # seen. Kept as a running check because the condition is
                        # inherently sequential — "new" only means anything
                        # relative to the pages before it. The authoritative
                        # dedupe happens once, after the loop, in page order.
                        fresh_count = sum(1 for p in outcome.products
                                          if p.sku is None or p.sku not in seen_keys)
                        seen_keys.update(p.sku for p in outcome.products
                                         if p.sku is not None)

                        # A page past the first that contributes nothing new
                        # means the end of the results — or that pagination is
                        # looping back on itself. Either way there is nothing
                        # further to fetch, and this is the honest terminating
                        # condition: a property of the DATA, not of a CSS
                        # selector that may have been renamed.
                        if not fresh_count:
                            logger.info("Page %d added no rows not already seen "
                                        "— treating that as the end of the "
                                        "listing.", page_num)
                            stop_reason = "no_new_products"
                            break

                        if page_num - 1 < len(planned):
                            url = planned[page_num - 1]
                            time.sleep(args.delay)
        finally:
            if session is not None:
                session.close()

    # Merge once, in PAGE order — not in the order pages happened to finish.
    # At one page at a time the two are identical, which is the point: this is
    # what keeps the output byte-for-byte the same while removing the
    # dependency on arrival order that concurrency would otherwise introduce.
    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            # Not necessarily "on an earlier page" — a duplicate can be on
            # this page. BBB's pagination WAS measured
            # repeating a little — page 1 and page 2 of one listing shared 3
            # products, all three from the "cheaper products" carousel — so a
            # small non-zero count here is expected and a large one is not.
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "pages x rows-per-page" as a hard expectation, even though BBB's
    # page size is fixed at 15: the LAST page of a listing is legitimately
    # short, and a threshold that fires on every healthy run teaches the
    # reader to ignore it. What is worth warning about is a page that came
    # back materially THIN against its siblings, which is what a truncated
    # response looks like.
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
                "page that is not the last one is a truncated response rather "
                "than a short listing — re-run with --dump-html.",
                ", ".join(str(p) for p, _ in thin), fullest,
                ", ".join("page %d: %d" % (p, n) for p, n in thin))
        if total_available:
            # The honest sentence, and the reason `totalResults` is worth
            # carrying: a complete run can still be a tiny sample, because
            # BBB will not serve past page 15 whatever the query matched.
            logger.info("BBB reports %d business(es) for this query; this run "
                        "holds %d (%.1f%%) across %d page(s) of the %s the "
                        "site offers.", total_available, len(all_rows),
                        100.0 * len(all_rows) / total_available,
                        len([o for o in outcomes if o.ok]), pages_available)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    # One-per-run context, in the sidecar rather than repeated down a column.
    #
    # For a listing run that is BBB's own arithmetic — `total_results`,
    # `pages_available`, and the ordering the site says it applied. Those
    # three together are what let a consumer tell a complete-but-capped run
    # (225 of 19,016, which is what a 15-page run of a broad query IS) from a
    # run that covered the whole result set, and no column can carry them
    # because they describe the QUERY rather than any business.
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
        description="BBB (Better Business Bureau) scraper (Playwright edition)")
    p.add_argument("--url", default=None,
                   help="A bbb.org URL: a search (/search?find_text=…&"
                        "find_loc=…), a category listing (/us/category/{slug}) "
                        "or one business profile "
                        "(/{cc}/{st}/{city}/profile/…) with --mode profile. "
                        "BBB serves the US and Canada from ONE host with the "
                        "country in the path, so there is no per-country "
                        "hostname. Optional for a search: --text and "
                        "--location build the URL instead. Also read from "
                        "BBB_URL in the environment or in .env.")
    p.add_argument("--text", default=None, metavar="QUERY",
                   help="What to search for, e.g. 'restaurants'. Builds a "
                        "/search URL together with --location, so a run needs "
                        "no hand-assembled URL. Ignored when --url is given.")
    p.add_argument("--location", default=None, metavar="PLACE",
                   help="Where to search, as BBB spells it: 'New York, NY', "
                        "'Toronto, ON'. Ignored when --url is given.")
    p.add_argument("--country", choices=["USA", "CAN"], default=None,
                   help="Which BBB directory --text/--location searches "
                        "(default USA). REFUSED together with --url, because "
                        "a search URL already carries find_country and a flag "
                        "that disagreed with it would silently scrape a "
                        "different directory than the one in the address. "
                        "This is the family's --country ban (CLAUDE.md §10) "
                        "applied where it actually bites: BBB has one host "
                        "for both countries, so the flag cannot contradict a "
                        "HOSTNAME — only a query string, which is what the "
                        "refusal covers.")
    p.add_argument("--sort", choices=sorted(SORTS), default=DEFAULT_SORT,
                   help="Which ordering to ask BBB for (default %(default)s). "
                        "This is not cosmetic: it decides WHICH businesses "
                        "are in the file. Measured 2026-09-16 on "
                        "find_text=restaurants near New York — 'best-match' "
                        "(the site's own default) returned 15/15 BBB "
                        "Accredited businesses and not one restaurant, while "
                        "'a-z' returned 0/15 accredited and real "
                        "name-matched restaurants. a-z is also the only "
                        "ordering that is STABLE between runs, which is what "
                        "a multi-page run needs. Pass 'best-match' to "
                        "reproduce what a visitor sees.")
    p.add_argument("--state", default=None, metavar="CODE",
                   help="Narrow a search to one state or province (NY, ON). "
                        "The documented way past BBB's 225-row ceiling: the "
                        "site caps every query at 15 pages of 15 however many "
                        "it matched, so slicing is the only way deeper — "
                        "filter_state=NY cut a 19,016-result query to 6,693, "
                        "and each slice gets its own 15 pages.")
    p.add_argument("--mode", choices=["search", "category", "profile"],
                   default="search",
                   help="search (default): /search?find_text=…&find_loc=…. "
                        "category: /us/category/{slug}, resolved to the tobId "
                        "BBB actually filters on — the slug is a human URL "
                        "and the API takes an id, so this costs one extra "
                        "request and refuses rather than guessing when no "
                        "category plausibly matches. profile: one business "
                        "profile page, which adds the accreditation date, the "
                        "BBB file-opened date, years in business, entity "
                        "type, the website, and the review and complaint "
                        "totals. --pages applies to the two listing modes; "
                        "there is one page to read in profile mode.")
    p.add_argument("--category", default=None,
                   help="Label to tag output rows with. Defaults to BBB's own "
                        "`tobText` for the business, so the column is "
                        "populated on every row without the flag. Pass one to "
                        "override it — useful when a run's rows should carry "
                        "the label you searched for rather than each "
                        "business's own primary category.")
    p.add_argument("--pages", type=int, default=1,
                   help="Number of listing pages to fetch. Applies to --mode "
                        "search and --mode category; ignored in --mode "
                        "profile. BBB caps every query at 15 pages of 15, and "
                        "asking for page 16 answers HTTP 500 rather than an "
                        "empty page — so a run PLANS against the `totalPages` "
                        "the site states on page 1 and never asks for the "
                        "page that errors. A request for more is honoured up "
                        "to that cap and the sidecar records both numbers.")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1 — "
                        "unchanged sequential behaviour). Each worker runs its "
                        "own browser and holds its own proxy exit, so N>1 "
                        "without --proxy-file just sends N times the traffic "
                        "from one address. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "The pause between attempts doubles each time. A page "
                        "that comes back EMPTY is not retried — see "
                        "page_flow.STATE_POLICY — because an empty hub "
                        "category is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first page-load retry, doubling "
                        "thereafter (default 2.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="bbb_businesses", help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). It does NOT decide "
                        "which directory is searched — that is find_country "
                        "in the URL, or --country — so this only affects what "
                        "the browser claims about itself.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line (# comments and blank "
                        "lines skipped) to rotate across. Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. per-page: "
                        "a new exit for every page — this is what spreads volume, "
                        "and it relaunches the browser each time so the session "
                        "does not follow the IP around.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup, so concurrent runs do not "
                        "all begin on the first exit in the file.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page comes back refused (HTTP 403) or behind "
                        "a captcha, retry it from this many OTHER exits before "
                        "giving up (default 2). Needs a pool of more than one; "
                        "ignored otherwise. This is the flag that matters most "
                        "on this site: the refusal is a property of the "
                        "ADDRESS, and a different exit is what clears it.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found. Off by "
                        "default so a failed run can't overwrite a good result "
                        "with an empty one; exit code is 4 either way.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a browser fingerprint from 2captcha's Fingerprint "
                        "API and apply it to the launched browser. Needs "
                        "--twocaptcha-key. Ignored with --cdp-endpoint, where the "
                        "Scraping Browser supplies its own.")
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
                        "your proxy's exit country — a US fingerprint on a "
                        "German IP is a contradiction.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use. v2 is the current "
                        "JSON API (api.2captcha.com/createTask); v1 is the "
                        "legacy in.php/res.php pair. Applies to both the image "
                        "captcha and reCAPTCHA.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected. Neither "
                        "setting touches BBB's hard refusal, which is not a "
                        "page at all — the HTTP/2 stream is reset and nothing "
                        "arrives — so no solve helps there and none is "
                        "attempted or billed. In fact NO challenge has ever "
                        "been observed on this site; the path is wired up "
                        "because a bot manager can be switched on between "
                        "deploys.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score to request (0.3, 0.7 or 0.9 "
                        "— the API only accepts these three). Ignored for v2 "
                        "widgets.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP instead "
                        "of launching Playwright's bundled Chromium, e.g. "
                        "ws://user:pass@host:port — the Scraping Browser API "
                        "endpoint, or any browser that exposes a CDP URL. "
                        "--proxy and --headless/--headful are ignored when this "
                        "is set.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success as "
                        "well as failure. Useful when the row count is right but "
                        "a column comes back empty — see TROUBLESHOOTING.md.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    # Fill --twocaptcha-key / --cdp-endpoint / --proxy / --url from the
    # environment or .env when the flag was not given. An explicit flag wins.
    env_config.apply(args)

    # --url and the query flags are two ways to say the same thing, and only
    # one of them may win. Refused rather than merged: a --country that
    # disagreed with the find_country already in a URL would silently search
    # a different directory than the address names, which is exactly the
    # hazard CLAUDE.md §10 bans a --country flag for. BBB has one host for
    # both countries, so the flag cannot contradict a HOSTNAME — only a query
    # string, and this is where that is caught.
    if args.url and (args.text or args.location or args.country or args.state):
        conflicting = [name for name, value in
                       (("--text", args.text), ("--location", args.location),
                        ("--country", args.country), ("--state", args.state))
                       if value]
        p.error("--url already carries the whole query; %s would have to "
                "agree with it and nothing here checks that they do. Pass "
                "either a URL or the query flags, not both."
                % ", ".join(conflicting))

    if not args.url and args.text is not None:
        if args.mode == "profile":
            p.error("--mode profile needs a --url: a profile is one page at "
                    "one address, and --text/--location describe a search.")
        args.url = listing_url(country=args.country or "USA", text=args.text,
                               location=args.location or "", page=1)
        logger.info("Built the listing URL from --text/--location: %s", args.url)

    if not args.url:
        p.error("no --url given and no --text: pass a bbb.org URL, or "
                "--text/--location to build a search. BBB_URL in the "
                "environment or in .env works too.")

    supported, why = is_supported_url(args.url)
    if not supported:
        # Refused rather than attempted. This parser reads BBB's own state
        # object and BBB's profile-path shape; pointing it at another site
        # would not fail loudly, it would return zero rows and look like an
        # empty result (§8). The REASON is given, because "is not a BBB site"
        # about a host that plainly is one sends the reader hunting a typo.
        p.error(f"{args.url!r} {why}.")

    if args.mode == "profile" and profile_parts(args.url) is None:
        p.error(f"--mode profile expects a business profile URL shaped "
                f"/{{cc}}/{{state}}/{{city}}/profile/{{category}}/{{name}}; "
                f"{args.url!r} is not one.")
    if args.mode != "profile" and profile_parts(args.url) is not None:
        p.error(f"{args.url!r} is a single business profile. Use --mode "
                f"profile for it, or pass a /search?find_text=… or "
                f"/{{cc}}/category/{{slug}} URL.")
    if args.mode == "category" and category_from_url(args.url) is None:
        p.error(f"--mode category expects a /{{cc}}/category/{{slug}} URL; "
                f"{args.url!r} is not one. A keyword search is --mode search.")

    if args.mode == "profile" and args.pages != 1:
        # Said out loud rather than silently ignored: a user who passed
        # --pages 5 expects five pages of something.
        logger.warning("--pages %d is ignored in --mode profile: there is one "
                       "page to read. The run status will say "
                       "single_page_mode.", args.pages)
        args.pages = 1
    if args.mode == "profile" and args.sort != DEFAULT_SORT:
        logger.warning("--sort is ignored in --mode profile: one profile has "
                       "no ordering.")
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second one on top creates a mismatch rather "
                       "than better cover.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        # Bad usage, not a crash: a typo in a proxy list would otherwise
        # surface as a connection failure on page 1 with nothing naming it.
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest one:
        # `profile_locked` means another run still holds this `pid`, and a
        # harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
