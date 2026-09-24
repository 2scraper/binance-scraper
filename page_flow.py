"""
page_flow.py
------------
The retry / solve / blocked decision, as DATA rather than as three copies of
an if-chain (CLAUDE.md §1).

binance.com answers one of this repo's requests in eight ways, and they want
six different responses:

    the endpoint's JSON with rows in it                 -> parse
    the same JSON with no rows                          -> parse, it is an answer
    the JSON with a non-success `code`, or HTTP 400     -> stop: the PARAMETERS
                                                           were refused, and a
                                                           retry sends them again
    AWS WAF: 202 + x-amzn-waf-action, or its CAPTCHA    -> solve, or rotate
    429 / 418                                           -> wait, same exit
    451                                                 -> rotate: the COUNTRY
                                                           is refused
    403                                                 -> rotate
    anything else                                       -> retry

Three copies of that triage across three engines would drift, and the drift
would be silent: one engine reporting exit 3 where its twin reports exit 0
on the same response.

Nothing here imports a browser, and **no JavaScript crosses this boundary**
(§1). Each engine spells its fetch() in its own driver's dialect.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
from product_parser import (MAX_PAGES, DEFAULT_ANN_CATALOG,  # noqa: F401
                            DEFAULT_COPY_SORT, ORIGIN_URL, Query, api_error,
                            detect_bot_challenge, detect_page_state,
                            pages_available, parse_page, pay_type_identifiers,
                            pay_type_request, query_from_url, request_for,
                            total_results)
from product_parser import check_pay_types as check_pay_types_against

log = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# How long one fetch() may take before the engine gives up on it. The
# largest response measured was 76 KB of P2P JSON, which arrived in well
# under a second. The bound exists because a browser fetch() has no timeout
# of its own, and CLAUDE.md §8 requires every remote call to have one.
FETCH_TIMEOUT_MS = 30_000

# How long to wait at the SAME exit after a 429/418 before trying again.
# The site's public API documentation describes 429 as a warning and 418 as
# an IP ban that follows ignoring it, so the response to either is to slow
# down rather than to rotate.
THROTTLE_WAIT_S = 10.0
THROTTLE_RETRIES = 2

# How long to let AWS WAF's own challenge script run on a landing before
# judging it, and how often to look. The challenge action computes a token
# and reloads the page by itself, so a real browser passes it with nothing
# but time. Polled rather than slept, so a landing that was never
# challenged costs nothing.
CHALLENGE_SETTLE_MS = 15_000
CHALLENGE_POLL_MS = 1_000

# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the site answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every engine calls
    `classify(html, status, url, waf_action)`. A sibling repo shipped
    `classify(html, url=...)` in two of three engines against a callee that
    took `status` second, and both crashed on their first fetch (§17).
    `smoke_test.py` binds every engine's call against this signature for
    that reason.
    """
    return detect_page_state(html or "", status, url, waf_action)


STATE_POLICY = {
    "content":    {"retry": False, "solve": False, "blocked": False, "parse": True},
    # A listing with nothing in it, e.g. a P2P market with no adverts. The
    # site served exactly what was asked for, so this is EXIT_NO_PRODUCTS
    # rather than EXIT_BLOCKED.
    "empty":      {"retry": False, "solve": False, "blocked": False, "parse": True},
    # The endpoint refused the PARAMETERS: code 000002 "illegal parameter",
    # 11012004 "Invalid input", or an HTTP 400 with an empty body. The same
    # request sent again gets the same answer, and no exit or solve changes
    # it. So nothing retries and nothing counts as blocked. The engine stops
    # and names the site's own complaint.
    "rejected":   {"retry": False, "solve": False, "blocked": False, "parse": False},
    # AWS WAF. A CAPTCHA page is solvable (AmazonTask). A bare 202 challenge
    # is not, since there is no widget to buy an answer to, but a browser that
    # runs its script can pass it. A fresh exit clears either, hence retry.
    "challenge":  {"retry": True,  "solve": True,  "blocked": True,  "parse": False},
    # Rate limited. The retry happens at the same exit after a wait
    # (THROTTLE_*). It is NOT counted as blocked: calling a throttle a block
    # reports exit 3 for a page that was about to come back, and sends a
    # reader to buy a proxy they do not need (§24).
    "throttled":  {"retry": True,  "solve": False, "blocked": False, "parse": False},
    # HTTP 451: the site refuses the exit's country. Only a different exit
    # changes that.
    "restricted": {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    "blocked":    {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    # Not JSON and not an interstitial. Worth one more try.
    "unknown":    {"retry": True,  "solve": False, "blocked": False, "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by every
# engine, so setting it False really does stop the retry loop (§17).
RETRY_ON_BLOCKED = True

# How many times to re-fetch a blocked page when there is no proxy pool to
# rotate into. One: a WAF decision is about the address and the session, and
# a second request from both unchanged is a second identical answer. WITH a
# pool the engines retry once per remaining exit instead, because there the
# retry changes the variable the refusal depends on.
BLOCK_RETRIES_WITHOUT_POOL = 1

# At most one solve per page. A challenge that survives a solved token is not
# a challenge this run can pass, and a second solve is a second charge for
# the same answer.
SOLVES_PER_PAGE = 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given the total page 1 reported.

    Every listing here states its total on page 1, so the end is known up
    front rather than discovered by walking off it. Walking off it would be
    harmless here, since all three endpoints answer a page past the end with
    an empty list, but it costs a request per worker. And P2P's
    empty page reports `total: 0`, which would read as "the listing emptied".
    """
    ceiling = MAX_PAGES if pages_available is None else min(pages_available, MAX_PAGES)
    return max(1, min(int(pages_requested), ceiling))


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (§7).
    """
    return 1 if cdp_endpoint else None


# ---------------------------------------------------------------------------
# Refusals: how they are named and what the reader is told
# ---------------------------------------------------------------------------

def refusal_name(state: str) -> str:
    """The name a refusal is reported by, in logs and in `stop_reason`."""
    return {"challenge": "aws-waf", "restricted": "geo-451",
            "blocked": "http-403"}.get(state, state)


def refusal_advice(state: str) -> str:
    """One sentence on what changes the answer, per refusal. Kept here so
    the three engines cannot give three different pieces of advice."""
    if state == "restricted":
        return ("binance.com refuses this exit's COUNTRY (HTTP 451). Binance "
                "does not serve some jurisdictions, the United States among "
                "them. Use an exit elsewhere: --proxy with a non-US exit, or "
                "a Scraping Browser country- segment.")
    if state == "challenge":
        return ("AWS WAF challenged this session. A residential exit "
                "(--proxy) may not be challenged at all; otherwise set "
                "TWOCAPTCHA_KEY so its CAPTCHA can be solved (AmazonTask).")
    return ("The site refused this address (HTTP 403). A different exit is "
            "what changes that: --proxy / --proxy-file, or --cdp-endpoint.")


def stop_reason_for(outcome) -> str:
    """The run's stop_reason when `outcome` is the page that ended it."""
    if getattr(outcome, "rejected", None):
        return "api_rejected"
    if getattr(outcome, "blocked_by", None):
        return "blocked_%s" % outcome.blocked_by
    if getattr(outcome, "state", None) == "throttled":
        return "throttled"
    return "page_load_timeout"


# ---------------------------------------------------------------------------
# The query, and the end of a run
# ---------------------------------------------------------------------------

# The query flags, by the argparse dest they land in. A flag left at None was
# not typed, which is how build_query tells a user's value from a default.
QUERY_FLAGS = (("asset", "--asset"), ("fiat", "--fiat"), ("side", "--side"),
               ("pay_type", "--pay-type"), ("category", "--category"),
               ("time_range", "--time-range"), ("sort_by", "--sort-by"),
               ("order", "--order"))


def build_query(args, error: Callable[[str], None]) -> Query:
    """The Query a run sends, from --url or from the flags, validated.

    --url and the query flags are two ways to say the same thing. A flag the
    user typed that disagrees with the URL would silently scrape something
    neither of them named, so the combination is refused rather than merged
    (the family's --country rule, §10). `error` is argparse's `p.error`, so
    a refusal is exit 2 with the usage line, as in every engine.
    """
    if args.url:
        query, why = query_from_url(args.url)
        if query is None:
            error(why)
        if args.mode and args.mode != query.mode:
            error("--mode %s disagrees with --url, which is a %s page."
                  % (args.mode, query.mode))
        typed = [flag for dest, flag in QUERY_FLAGS
                 if getattr(args, dest, None) is not None]
        if typed:
            error("--url already carries the query; %s would have to agree "
                  "with it and nothing checks that they do. Pass a URL or "
                  "the flags, not both." % ", ".join(typed))
        if args.amount is not None:
            query.amount = args.amount
        query.hide_full = bool(args.hide_full)
    else:
        query = Query(mode=args.mode or "p2p",
                      asset=(args.asset or "USDT").upper(),
                      fiat=(args.fiat or "USD").upper(),
                      side=args.side or "buy",
                      pay_types=tuple(args.pay_type or ()),
                      amount=args.amount,
                      time_range=args.time_range or "30D",
                      sort_by=args.sort_by or DEFAULT_COPY_SORT,
                      order=args.order or "desc",
                      hide_full=bool(args.hide_full),
                      catalog=args.category or DEFAULT_ANN_CATALOG)
    why = query.validate()
    if why:
        error(why)
    if args.pages < 1:
        error("--pages must be at least 1")
    return query


def query_summary(query: Query) -> dict:
    """The query as the sidecar records it: only the fields its mode uses."""
    if query.mode == "p2p":
        return {"asset": query.asset, "fiat": query.fiat, "side": query.side,
                "pay_types": list(query.pay_types), "amount": query.amount}
    if query.mode == "copytrading":
        return {"time_range": query.time_range, "sort_by": query.sort_by,
                "order": query.order, "hide_full": query.hide_full}
    return {"catalog": query.catalog}


def finish(args, query: Query, outcomes: List, stop_reason: str,
           blocked: bool) -> int:
    """Merge the pages in PAGE order, write the output, return the exit code.

    One implementation for the three engines, so the merge order, the
    dedupe and the sidecar cannot differ between them (§6).
    """
    rows, seen = [], set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, seen, key="sku")
        if len(fresh) < len(oc.products):
            log.info("Page %d: dropped %d duplicate row(s) — the live listing "
                     "moved between page fetches.", oc.page_num,
                     len(oc.products) - len(fresh))
        rows.extend(fresh)

    first = next((o for o in outcomes if o.page_num == 1), None)
    total = getattr(first, "total_available", None)
    available = getattr(first, "pages_available", None)
    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = sorted(o.page_num for o in outcomes if not o.ok)
    if rows and total:
        log.info("The site reports %d match(es); this run holds %d (%.1f%%).",
                 total, len(rows), 100.0 * len(rows) / total)
    last_ok = max([o.page_num for o in ok_pages] or [1])
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=blocked, stop_reason=stop_reason,
        pages_requested=args.pages, pages_completed=len(ok_pages),
        pages_failed=failed_pages, mode=query.mode, source=SOURCE_DEFAULT,
        start_url=args.url or request_for(query, 1).url,
        final_url=request_for(query, last_ok).url,
        extra={"total_results": total, "pages_available": available,
               "query": query_summary(query)})


def cookie_domain(host: Optional[str]) -> str:
    """The domain an `aws-waf-token` cookie is set on: the registrable one.

    `.binance.com` for www.binance.com and p2p.binance.com alike. The site's
    own WAF integration keeps the token there, and a copy scoped to
    www.binance.com alone did not clear the CAPTCHA (2026-09-24).
    """
    parts = (host or "www.binance.com").lower().split(".")
    return "." + ".".join(parts[-2:]) if len(parts) >= 2 else (host or "")


# ---------------------------------------------------------------------------
# The fetch loop, driven through named operations
# ---------------------------------------------------------------------------
#
# Everything about fetching one page lives here, once: landing on the
# origin, letting the WAF's script run, paying for a CAPTCHA, retrying a
# transport failure, waiting out a throttle, rotating on a refusal, and
# parsing what came back. The three engines differ only in HOW they ask
# their driver, so each passes in an object with these operations and no
# JavaScript crosses this boundary (§1):
#
#     ops.goto(url)        -> (status, waf_header). Raises TransportError.
#     ops.document_text()  -> the landing document, JSON text if it is JSON
#     ops.wait_ms(ms)
#     ops.solve_captcha()  -> True if a CAPTCHA was solved and the page reloaded
#     ops.fetch(req)       -> (status, text, waf_header, error_or_None)
#     ops.relaunch()       -> a fresh browser (on the pool's current exit)
#     ops.landed           -> bool attribute, owned by the loop
#     ops.proxy_failure(text) -> the driver's proxy-error name in text, or ""
#
# Before this, each engine carried its own copy of the loop, and a family
# rule (§6) says the three must agree on exit codes and on whether a run
# spends money. One copy is how that holds by construction.


class TransportError(Exception):
    """A navigation that did not complete: a timeout, a dead proxy."""


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards, in page order, rather than
    folded into shared state as the loop goes, so the output cannot depend
    on which page happened to finish first (§8).
    """
    page_num: int
    url: str
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The state the page came back as. Carried so the caller can tell an
    # EMPTY page (the end of a live listing) from a failed one: both hold
    # zero rows and they mean opposite things.
    state: Optional[str] = None
    # The endpoint's own complaint when it refused the parameters.
    rejected: Optional[str] = None
    # The site's own count of what matched, from this page's response.
    total_available: Optional[int] = None
    pages_available: Optional[int] = None

    @property
    def ok(self) -> bool:
        return (not self.load_failed and self.blocked_by is None
                and self.rejected is None)


# The columns the site filled on EVERY record of every capture, per mode.
# Below this share, the payload shape has moved rather than the data being
# unusual. Deliberately NOT here: `sharpe_ratio` (null on most portfolios,
# as published) and `badge` (14 of 30 had none).
CORE_FIELD_FLOOR = 99
CORE_FIELDS = {
    "p2p": ("sku", "title", "price", "min_order_fiat", "pay_methods"),
    "copytrading": ("sku", "title", "roi_pct", "pnl", "copiers"),
    "announcements": ("sku", "title", "released_at", "url"),
}


def land(ops, args) -> Tuple[str, Optional[str]]:
    """Put the page on a www.binance.com document that fetch() can use.

    Returns (state, error): state is a policy state for the landing, and
    error is a transport failure's text or None. Solves at most
    SOLVES_PER_PAGE CAPTCHAs, because a CAPTCHA that survives a solved token
    is not one this run can pass, and a second solve is a second charge for
    the same answer.
    """
    try:
        status, waf = ops.goto(ORIGIN_URL)
    except TransportError as e:
        return "load_failed", str(e)
    state = classify(ops.document_text(), status, ORIGIN_URL, waf)
    if state == "challenge":
        # The WAF's challenge action computes a token and reloads the page
        # by itself, so let it run before paying for anything.
        waited = 0
        while waited < CHALLENGE_SETTLE_MS and state == "challenge":
            ops.wait_ms(CHALLENGE_POLL_MS)
            waited += CHALLENGE_POLL_MS
            state = classify(ops.document_text(), None, ORIGIN_URL, None)
        if state == "challenge" and should_solve(state):
            for _ in range(SOLVES_PER_PAGE):
                if not ops.solve_captcha():
                    break
                state = classify(ops.document_text(), None, ORIGIN_URL, None)
                if state != "challenge":
                    break
    ops.landed = state in ("content", "empty")
    return state, None


def _core_field_warnings(rows: List, mode: str, page_num: int) -> None:
    for name in CORE_FIELDS.get(mode, ()):
        if not rows:
            return
        filled = sum(1 for r in rows if getattr(r, name, None) not in (None, "", []))
        share = 100.0 * filled / len(rows)
        if share < CORE_FIELD_FLOOR:
            log.warning("Only %.0f%% of page %d carries `%s`, against a "
                        "measured floor of %d%%. Every record of every capture "
                        "had one, so the payload shape has moved — re-run with "
                        "--dump-html.", share, page_num, name, CORE_FIELD_FLOOR)


def _dump(args, page_num: int, text: str) -> None:
    """Write the exact response the parser was given, on success too (§9)."""
    if not args.dump_html:
        return
    path = args.dump_html if args.pages == 1 else f"{args.dump_html}.page{page_num}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("Saved the response the parser sees to %s (%d bytes).",
             path, len(text))


def _save_debug(args, page_num: int, text: str) -> str:
    path = f"{args.out}_page{page_num}_debug.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")
    return path


def fetch_one_page(ops, args, pool, query: Query, page_num: int,
                   mask: Callable[[str], str] = lambda s: s) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Never raises for an EXPECTED failure. A timeout, a refusal, a WAF
    challenge and a dead exit are all recorded on the outcome, because what
    the run should do about them differs between the sequential and the
    concurrent paths.
    """
    req = request_for(query, page_num)
    outcome = PageOutcome(page_num=page_num, url=req.label)

    has_pool = bool(pool and len(pool) > 1)
    block_retries = 0 if not RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool else BLOCK_RETRIES_WITHOUT_POOL)
    throttles = 0
    state, text, last_error, exit_failed = "unknown", "", None, None

    for block_attempt in range(block_retries + 1):
        log.info("Fetching page %d/%d: %s", page_num, args.pages, req.label)
        exit_failed = None
        attempt = 0
        while attempt < args.retries:
            attempt += 1
            text, last_error = "", None
            if not ops.landed:
                state, last_error = land(ops, args)
                if last_error:
                    exit_failed = ops.proxy_failure(last_error) or None
                    if exit_failed:
                        break  # a different exit is the only thing that helps
                elif not ops.landed:
                    text = ops.document_text()
            if ops.landed:
                status, text, waf, last_error = ops.fetch(req)
                state = ("load_failed" if last_error
                         else classify(text, status, req.url, waf))
                if state == "challenge":
                    # The WAF challenged this session after the landing. A
                    # fresh navigation is where its page can run or be
                    # solved, so the next attempt lands again.
                    ops.landed = False
            if state == "throttled" and throttles < THROTTLE_RETRIES:
                throttles += 1
                attempt -= 1  # a throttle wait spends its own budget (§24)
                pause = THROTTLE_WAIT_S * throttles
                log.warning("Rate-limited on page %d (HTTP 429/418) — waiting "
                            "%.0fs at the same exit (%d/%d).", page_num, pause,
                            throttles, THROTTLE_RETRIES)
                ops.wait_ms(int(pause * 1000))
                continue
            if state in ("load_failed", "unknown") and attempt < args.retries:
                pause = args.retry_delay * (2 ** (attempt - 1))
                log.warning("Page %d came back %s (attempt %d/%d)%s — retrying "
                            "in %.1fs.", page_num, state, attempt, args.retries,
                            f": {mask(last_error)}" if last_error else "", pause)
                ops.wait_ms(int(pause * 1000))
                continue
            break

        if exit_failed and has_pool and block_attempt < block_retries:
            log.warning("Exit %s is unusable (%s) — rotating to another one "
                        "(%d/%d).", mask(pool.current), exit_failed,
                        block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            ops.relaunch()
            continue
        if (counts_as_blocked(state) and should_retry(state)
                and block_attempt < block_retries):
            if has_pool:
                log.warning("Page %d refused (%s) at %s — rotating to another "
                            "exit (%d/%d).", page_num, refusal_name(state),
                            mask(pool.current), block_attempt + 1, block_retries)
                pool.advance(f"refused: {refusal_name(state)}")
            else:
                log.warning("Page %d refused (%s) — re-fetching once from a "
                            "fresh browser.", page_num, refusal_name(state))
            ops.relaunch()
            continue
        break

    outcome.state = state
    if state == "load_failed" or exit_failed:
        outcome.load_failed = True
        log.error("Gave up on page %d: %s", page_num,
                  mask(last_error or "the request never completed"))
        return outcome
    if state == "rejected":
        outcome.rejected = api_error(text) or "HTTP 400, empty body"
        log.error("The endpoint refused this request (%s). That is a statement "
                  "about the PARAMETERS, and the same request sent again gets "
                  "the same answer, so it is not retried. If the query looks "
                  "right, the site's API has changed — open an issue with "
                  "--dump-html.", outcome.rejected)
        _dump(args, page_num, text)
        return outcome
    if counts_as_blocked(state):
        outcome.blocked_by = refusal_name(state)
        debug = _save_debug(args, page_num, text)
        log.error("Blocked by %s on page %d — saved to %s. This is exit 3, "
                  "distinct from an empty listing (exit 4). %s",
                  outcome.blocked_by, page_num, debug, refusal_advice(state))
        return outcome
    if not should_parse(state):
        # throttled past its budget, or never the endpoint's JSON at all
        outcome.load_failed = True
        debug = _save_debug(args, page_num, text)
        log.error("Page %d never came back as the endpoint's JSON (%s) — saved "
                  "to %s. %s", page_num, state, debug,
                  "Raise --delay, or spread the run over --proxy-file."
                  if state == "throttled" else "")
        return outcome

    _dump(args, page_num, text)
    rows = parse_page(text, query, page_num)
    outcome.products = rows
    outcome.total_available = total_results(text, query.mode)
    outcome.pages_available = pages_available(outcome.total_available, query.mode)
    log.info("Parsed %d row(s) from page %d.", len(rows), page_num)
    if page_num == 1 and outcome.total_available is not None:
        log.info("The site reports %d match(es) — %s page(s) at this page "
                 "size.", outcome.total_available, outcome.pages_available)
    _core_field_warnings(rows, query.mode, page_num)
    return outcome


def check_pay_types(ops, args, query: Query) -> Optional[str]:
    """Refuse an unknown --pay-type before the search, with the reason.

    The search answers an unknown identifier with an EMPTY result, not an
    error, so without this a typo is an exit-4 run reporting that nobody
    trades on a market that is full of adverts. If the list itself cannot be
    read, the run goes ahead unchecked with a warning rather than refusing
    a correct query over a response it could not parse.
    """
    if not query.pay_types:
        return None
    if not ops.landed:
        land(ops, args)
    offered = None
    if ops.landed:
        _status, text, _waf, err = ops.fetch(pay_type_request(query.fiat))
        offered = None if err else pay_type_identifiers(text)
    if offered is None:
        log.warning("Could not read the payment methods P2P offers for %s — "
                    "--pay-type is sent unchecked. A typo in it will come back "
                    "as an empty listing.", query.fiat)
        return None
    return check_pay_types_against(query.pay_types, offered)


def run_pages(open_ops, close_ops, run_concurrently, args, pool,
              query: Query, concurrency: int,
              mask: Callable[[str], str] = lambda s: s) -> int:
    """The whole run after argument handling, shared by the three engines.

    `open_ops()` returns a ready ops object on `pool`, `close_ops(ops)`
    tears it down, and `run_concurrently(page_nums)` returns
    (outcomes, unattempted, exhausted) for pages 2..N fetched by workers. The
    engines supply those three because a browser's lifecycle (and on
    Playwright, its thread) is the one thing that cannot be shared.
    """
    outcomes: List[PageOutcome] = []
    blocked, stop_reason = False, "completed"
    ops = open_ops()
    try:
        refusal = check_pay_types(ops, args, query)
        if refusal:
            log.error("%s", refusal)
            return 2

        # Page 1 is always fetched alone: its total decides how many pages
        # there are to address (§7).
        first = fetch_one_page(ops, args, pool, query, 1, mask)
        outcomes.append(first)
        if not first.ok:
            stop_reason = stop_reason_for(first)
            blocked = first.blocked_by is not None
        else:
            plan = pages_to_plan(args.pages, first.pages_available)
            if plan < args.pages:
                log.info("Asked for %d page(s); the listing has %s. Fetching "
                         "all of them.", args.pages, first.pages_available)
            rest = list(range(2, plan + 1)) if first.products else []
            if rest and concurrency > 1:
                close_ops(ops)
                ops = None
                log.info("Fetching pages 2-%d across %d workers%s.", plan,
                         concurrency, f" over {len(pool)} exit(s)" if pool else "")
                more, unattempted, exhausted = run_concurrently(rest)
                outcomes.extend(more)
                failed = [o for o in more if not o.ok]
                if failed:
                    stop_reason = stop_reason_for(min(failed, key=lambda o: o.page_num))
                    blocked = any(o.blocked_by for o in more)
                elif exhausted:
                    stop_reason = "end_of_listing"
                elif unattempted:
                    stop_reason = "pages_unattempted"
            else:
                for page_num in rest:
                    ops.wait_ms(int(args.delay * 1000))
                    if pool and pool.rotates_per_page():
                        pool.advance(f"per-page rotation, page {page_num}")
                        ops.relaunch()
                    outcome = fetch_one_page(ops, args, pool, query, page_num, mask)
                    outcomes.append(outcome)
                    if not outcome.ok:
                        stop_reason = stop_reason_for(outcome)
                        blocked = outcome.blocked_by is not None
                        break
                    if not outcome.products:
                        # The live listing shrank below the plan made from
                        # page 1. A property of the DATA, and complete.
                        log.info("Page %d came back empty — the listing ended "
                                 "before the plan did.", page_num)
                        stop_reason = "end_of_listing"
                        break
    finally:
        if ops is not None:
            close_ops(ops)
    return finish(args, query, outcomes, stop_reason, blocked)


def worker_loop(ops, args, query: Query, work, results, results_lock,
                exhausted, name: str, mask: Callable[[str], str] = lambda s: s):
    """One concurrent worker's page loop, after its engine opened `ops`.

    Takes pages until the queue is empty or a page comes back empty (the
    live listing ended before the plan did), which sets `exhausted` so the
    other workers stop taking work too.
    """
    first = True
    while not exhausted.is_set():
        try:
            page_num = work.get_nowait()
        except Exception:  # queue.Empty
            break
        if not first:
            ops.wait_ms(int(args.delay * 1000))
        first = False
        outcome = fetch_one_page(ops, args, ops.pool, query, page_num, mask)
        with results_lock:
            results.append(outcome)
        if outcome.ok and not outcome.products:
            log.info("[%s] page %d returned no rows — the listing ended; "
                     "stopping dispatch.", name, page_num)
            exhausted.set()


def concurrency_for(args, pool) -> int:
    """How many workers this run may use, with the warnings said once for
    all three engines."""
    concurrency = max(1, args.concurrency)
    if concurrency <= 1:
        return 1
    if concurrency_limit(args.cdp_endpoint) == 1:
        log.warning("--concurrency is ignored with --cdp-endpoint: the Scraping "
                    "Browser API allows one live connection per profile, and "
                    "several workers would collide on it (profile_locked). Use "
                    "several pids instead.")
        return 1
    if not pool:
        log.warning("--concurrency %d with no proxy pool: every worker leaves "
                    "from the SAME address. The endpoints answered a datacentre "
                    "address normally, but N workers are N times the request "
                    "rate from it, and the site answers a rate it dislikes with "
                    "429 and then 418. Pass --proxy-file to spread the load.",
                    concurrency)
    if concurrency > 8:
        log.warning("--concurrency %d means %d browsers at once (~150-300MB "
                    "each).", concurrency, concurrency)
    return concurrency


def worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Workers start on distinct exits and share no mutable state, so rotation
    needs no lock (§7).
    """
    if not pool:
        return None
    from proxy_pool import ProxyPool
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")
