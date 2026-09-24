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
from typing import Optional

from product_parser import (MAX_PAGES, detect_bot_challenge,  # noqa: F401
                            detect_page_state)

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
