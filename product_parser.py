"""
product_parser.py
-----------------
Everything this repo knows about binance.com lives here (CLAUDE.md §1).

Three modes, three of the site's own JSON endpoints
---------------------------------------------------
    --mode p2p            POST /bapi/c2c/v2/friendly/c2c/adv/search
                          the P2P order book: one row per advert
    --mode copytrading    POST /bapi/futures/v1/friendly/future/copy-trade/home-page/query-list
                          Futures copy-trading lead portfolios: ROI, PnL,
                          drawdown, AUM, copier counts
    --mode announcements  GET  /bapi/composite/v1/public/cms/article/list/query
                          one announcement catalogue (new listings,
                          delistings, ...), newest first

Why JSON endpoints and not pages
--------------------------------
Measured 2026-09-24 from a datacentre address (netcup, AS197540):

    every HTML page on www.binance.com and p2p.binance.com
        -> HTTP 202, empty body, `x-amzn-waf-action: challenge`
    the same pages in real Chromium, headless AND headful
        -> "Human Verification", HTTP 405: an AWS WAF CAPTCHA
    each of the three endpoints above, plain curl, no cookies
        -> HTTP 200, the full JSON the site's own front end renders

So the gate is per ROUTE, not per site (CLAUDE.md §21): the pages are
behind AWS WAF and the front end's own data calls are not. There is no HTML
parser in this file because no HTML page is needed for any mode. The
engines drive a real browser anyway, and the browser earns its keep in two
places. It lands on an endpoint that answers GET, which gives it a
www.binance.com origin, and then issues each request as a same-origin
`fetch()` with the real TLS stack and cookies. And if the WAF ever does gate
the endpoints, the landing shows the WAF's own CAPTCHA page, which
`captcha_solver` answers with 2Captcha's AmazonTask.

Every parameter is allowlisted, because the API does not validate them
----------------------------------------------------------------------
The worst thing this site does is accept a wrong value and answer with
something plausible. All of the following were measured on 2026-09-24:

    copy-trading  dataType=BOGUS        -> HTTP 200, a full list under SOME
                                           ordering (the same one WIN_RATE
                                           gives, so WIN_RATE is not
                                           provably a real key either)
    copy-trading  pageSize=50 or 100    -> HTTP 200 with 30 rows: silently capped
    p2p           payTypes=["Revolut"]  -> HTTP 200, total 0, for a method
                                           with no EUR adverts, and for a typo
    p2p           rows=50               -> code 000002 "illegal parameter"
    announcements pageSize=12/25/30/100 -> HTTP 400, EMPTY body
    copy-trading  timeRange=1Y          -> code 11012004 "Invalid input"

The first three turn a user's mistake into a run that looks healthy, so the
values go through the allowlists below and anything else is refused before a
request is sent. The last three are loud already, and `detect_page_state`
calls them `rejected` rather than blocked, so nobody goes looking for a
proxy problem that is not there.
"""

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

from output_writer import Announcement, LeadTrader, P2PAd, SOURCE_DEFAULT

BASE = "https://www.binance.com"

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

P2P_PATH = "/bapi/c2c/v2/friendly/c2c/adv/search"
# The list of payment methods P2P actually offers for a fiat. Used to check
# `--pay-type` BEFORE the search, because the search answers an unknown
# identifier with an empty result instead of an error (module docstring).
P2P_FILTER_PATH = "/bapi/c2c/v2/public/c2c/adv/filter-conditions"
COPY_PATH = "/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list"
ANN_PATH = "/bapi/composite/v1/public/cms/article/list/query"

# Where a browser engine lands before its first request. It needs to be:
#   * on www.binance.com, so that every later fetch() is same-origin;
#   * a GET, since a browser cannot navigate to a POST;
#   * small and ungated, since it happens once per browser.
# The announcements list with one row satisfies all three: 1.3 KB of JSON,
# HTTP 200 to plain curl and to headless Chromium (2026-09-24). If AWS WAF
# ever does gate the endpoints, this is also where its CAPTCHA page appears,
# which is where the solver can see it.
ORIGIN_URL = BASE + ANN_PATH + "?type=1&pageNo=1&pageSize=1&catalogId=48"

# ---------------------------------------------------------------------------
# Page sizes, measured per endpoint
# ---------------------------------------------------------------------------

# The largest `rows` the P2P search accepts. 50 and 100 both return code
# 000002 "illegal parameter".
P2P_ROWS = 20
# The copy-trading list CAPS rather than refusing: 18 gives 18 rows, while
# 50 and 100 each give 30. So 30 is what a page holds. Asking for more would
# make page N start where the server thinks page N starts (N x 30), and
# planning pages from an asked-for 50 would skip 20 portfolios on every page
# in silence.
COPY_ROWS = 30
# The announcements list takes a page size from a FIXED set. Measured:
# 1, 2, 5, 10, 15, 20 and 50 answer 200, while 12, 25, 30, 40, 51, 60 and 100
# answer HTTP 400 with an empty body.
ANN_ROWS = 50

ROWS_PER_PAGE = {"p2p": P2P_ROWS, "copytrading": COPY_ROWS,
                 "announcements": ANN_ROWS}

# A ceiling on --pages. The largest listing measured was copy-trading's
# 8,920 portfolios, which is 298 pages of 30. The cap sits well above that
# and exists so a typo in --pages cannot start a ten-thousand-request run.
MAX_PAGES = 1000

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

P2P_SIDES = ("buy", "sell")

# The periods the copy-trading list accepts. Each returned code 000000 with
# its own figures. "1Y" is refused with code 11012004.
COPY_TIME_RANGES = ("7D", "30D", "90D", "180D", "365D")

# --sort-by -> the API's `dataType`. Only the keys PROVEN to change the
# ordering are listed. Each gave a different top three on 2026-09-24.
# WIN_RATE is deliberately absent: it returned the same list as a nonsense
# key, which means the API ignored it, not that it sorted by win rate.
COPY_SORTS = {
    "roi": "ROI",
    "pnl": "PNL",
    "mdd": "MDD",
    "aum": "AUM",
    "copier-pnl": "COPIER_PNL",
    "copiers": "COPY_COUNT",
    # This one also FILTERS: it returned 5,568 portfolios against 8,920
    # under every other key, because a portfolio without a Sharpe ratio is
    # left out. `total_results` in the sidecar records that.
    "sharpe": "SHARP_RATIO",
}
DEFAULT_COPY_SORT = "roi"

# Announcement catalogues, by the ids and names the list endpoint itself
# returns when asked with no catalogId (2026-09-24). The alias is what a
# user types; the id is what the API filters on.
ANN_CATALOGS = {
    "new-listings": 48,
    "news": 49,
    "activities": 93,
    "delisting": 161,
    "maintenance": 157,
    "api-updates": 51,
    "airdrop": 128,
}
DEFAULT_ANN_CATALOG = "new-listings"

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


@dataclass
class ApiRequest:
    """One call to one of the site's endpoints: what an engine sends.

    `body` is None for a GET. `label` is what a log line and a --dump-html
    file name use, because a POST has no address that tells two pages apart.
    """
    method: str
    path: str
    page: int
    params: Dict[str, Any] = field(default_factory=dict)
    body: Optional[Dict[str, Any]] = None

    @property
    def url(self) -> str:
        q = ("?" + urlencode(self.params)) if self.params else ""
        return BASE + self.path + q

    @property
    def body_json(self) -> Optional[str]:
        return None if self.body is None else json.dumps(self.body)

    @property
    def label(self) -> str:
        return "%s %s page=%d" % (self.method, self.path.rsplit("/", 1)[-1],
                                  self.page)


@dataclass
class Query:
    """What a run asks for, independent of the page number.

    Built once from the CLI (or from --url) and validated before anything is
    sent. See `validate()` for why an unknown value is refused rather than
    passed through.
    """
    mode: str
    # p2p
    asset: str = "USDT"
    fiat: str = "USD"
    side: str = "buy"
    pay_types: Tuple[str, ...] = ()
    amount: Optional[float] = None
    # copytrading
    time_range: str = "30D"
    sort_by: str = DEFAULT_COPY_SORT
    order: str = "desc"
    hide_full: bool = False
    # announcements
    catalog: str = DEFAULT_ANN_CATALOG

    def validate(self) -> Optional[str]:
        """None when the query can be sent, else the reason it cannot."""
        if self.mode == "p2p":
            if self.side not in P2P_SIDES:
                return "--side must be one of %s" % ", ".join(P2P_SIDES)
            if not re.fullmatch(r"[A-Z0-9]{2,10}", self.asset or ""):
                return "--asset %r is not an asset symbol (USDT, BTC, ...)" % self.asset
            if not re.fullmatch(r"[A-Z]{3}", self.fiat or ""):
                return "--fiat %r is not a 3-letter currency code" % self.fiat
            if self.amount is not None and self.amount <= 0:
                return "--amount must be positive"
        elif self.mode == "copytrading":
            if self.time_range not in COPY_TIME_RANGES:
                return "--time-range must be one of %s" % ", ".join(COPY_TIME_RANGES)
            if self.sort_by not in COPY_SORTS:
                return "--sort-by must be one of %s" % ", ".join(sorted(COPY_SORTS))
            if self.order not in ("asc", "desc"):
                return "--order must be asc or desc"
        elif self.mode == "announcements":
            if catalog_id(self.catalog) is None:
                return ("--catalog must be one of %s, or a numeric catalogue id"
                        % ", ".join(ANN_CATALOGS))
        else:
            return "unknown mode %r" % self.mode
        return None

    @property
    def sort_label(self) -> Optional[str]:
        """The ordering, as a value for the rows' `sort` column."""
        if self.mode == "copytrading":
            return "%s-%s" % (self.sort_by, self.order)
        return None


def catalog_id(catalog: Any) -> Optional[int]:
    """An announcement catalogue alias or id -> the numeric id, else None."""
    if catalog is None:
        return None
    text = str(catalog).strip().lower()
    if text in ANN_CATALOGS:
        return ANN_CATALOGS[text]
    if text.isdigit() and int(text) > 0:
        return int(text)
    return None


def catalog_alias(cid: Optional[int]) -> Optional[str]:
    for alias, value in ANN_CATALOGS.items():
        if value == cid:
            return alias
    return None


def request_for(query: Query, page: int) -> ApiRequest:
    """The API call that fetches page `page` (1-based) of `query`."""
    if query.mode == "p2p":
        body = {
            "asset": query.asset,
            "fiat": query.fiat,
            # The request carries the TAKER's side: what the user wants to
            # do. Every advert that comes back carries the MAKER's side,
            # which is the opposite one. BUY returned 20 of 20 adverts
            # marked SELL on 2026-09-24, and BTC/TRY SELL returned 20 of 20
            # marked BUY. The row keeps both (see output_writer.P2PAd).
            "tradeType": query.side.upper(),
            "page": page,
            "rows": P2P_ROWS,
            "payTypes": list(query.pay_types),
            "publisherType": None,
        }
        if query.amount is not None:
            body["transAmount"] = query.amount
        return ApiRequest("POST", P2P_PATH, page, body=body)
    if query.mode == "copytrading":
        body = {
            "pageNumber": page,
            "pageSize": COPY_ROWS,
            "timeRange": query.time_range,
            "dataType": COPY_SORTS[query.sort_by],
            "favoriteOnly": False,
            "hideFull": bool(query.hide_full),
            "nickname": "",
            "order": query.order.upper(),
            "userAsset": 0,
            "portfolioType": "PUBLIC",
        }
        return ApiRequest("POST", COPY_PATH, page, body=body)
    if query.mode == "announcements":
        params = {"type": 1, "pageNo": page, "pageSize": ANN_ROWS,
                  "catalogId": catalog_id(query.catalog)}
        return ApiRequest("GET", ANN_PATH, page, params=params)
    raise ValueError("unknown mode %r" % query.mode)


def pay_type_request(fiat: str) -> ApiRequest:
    return ApiRequest("POST", P2P_FILTER_PATH, 0, body={"fiat": fiat})


def pay_type_identifiers(payload: Any) -> Optional[List[str]]:
    """The `identifier`s P2P offers for a fiat, or None if unreadable.

    None rather than [] on a response that does not have the shape: a caller
    that got [] would conclude EVERY --pay-type is invalid and refuse a
    correct run because of a response it could not read.
    """
    data = _as_dict(payload).get("data")
    if not isinstance(data, dict) or not isinstance(data.get("tradeMethods"), list):
        return None
    out = []
    for m in data["tradeMethods"]:
        ident = m.get("identifier") if isinstance(m, dict) else None
        if isinstance(ident, str) and ident:
            out.append(ident)
    return out


def check_pay_types(wanted: Sequence[str], offered: Optional[Sequence[str]]
                    ) -> Optional[str]:
    """None when every --pay-type is one P2P offers, else a refusal naming
    the bad ones and the closest real identifiers.

    Matching is exact because the API's matching is: `SEPAinstant` works and
    `SEPA Instant` would return nothing at all.
    """
    if not wanted or offered is None:
        return None
    bad = [w for w in wanted if w not in offered]
    if not bad:
        return None
    def norm(x: str) -> str:
        # "SEPA Instant" (the name the page shows) against "SEPAinstant"
        # (the identifier the API filters on): only spacing and case differ.
        return re.sub(r"[^a-z0-9]", "", x.lower())

    hints = []
    for b in bad:
        exact = [o for o in offered if norm(o) == norm(b)]
        near = exact or [o for o in offered
                         if norm(b) in norm(o) or norm(o) in norm(b)]
        if near:
            hints.append("%s -> %s" % (b, ", ".join(near[:4])))
    msg = ("--pay-type %s is not a payment method P2P offers for this fiat. "
           "The search would answer with an empty result rather than an "
           "error, so the run is refused before it starts."
           % ", ".join(bad))
    if hints:
        msg += " Did you mean: " + "; ".join(hints) + "?"
    return msg


# ---------------------------------------------------------------------------
# --url
# ---------------------------------------------------------------------------

SUPPORTED_HOSTS = ("www.binance.com", "binance.com", "p2p.binance.com",
                   "c2c.binance.com")

_LANG = r"(?:/[a-z]{2}(?:-[A-Za-z]{2,4})?)?"


def query_from_url(url: str) -> Tuple[Optional[Query], Optional[str]]:
    """Map a binance.com address onto a Query. Returns (query, None), or
    (None, reason) when the address is not one of the shapes this repo reads.

    Accepted shapes. The P2P forms are the ones the site uses for its own
    market pages:

        p2p.binance.com/{lang}/trade/{payment|all-payments}/{ASSET}?fiat=EUR   buy
        p2p.binance.com/{lang}/trade/sell/{ASSET}?fiat=EUR&payment=Wise        sell
        www.binance.com/{lang}/p2p/...                                        same
        www.binance.com/{lang}/copy-trading
        www.binance.com/{lang}/support/announcement/list/{catalogId}
        www.binance.com/{lang}/support/announcement/c-{catalogId}
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if host not in SUPPORTED_HOSTS:
        return None, ("%r is not a binance.com address. binance.us is a "
                      "separate exchange with its own site and is not "
                      "supported." % (parsed.hostname or url))
    path = parsed.path or "/"
    qs = {k: v[-1] for k, v in parse_qs(parsed.query).items()}

    m = re.fullmatch(_LANG + r"(?:/p2p)?/trade/([^/]+)/([A-Za-z0-9]+)/?", path)
    if m and (host != "www.binance.com" or "/p2p/" in path):
        first, asset = m.group(1), m.group(2).upper()
        side = "sell" if first.lower() == "sell" else "buy"
        payment = qs.get("payment") if side == "sell" else first
        pay_types: Tuple[str, ...] = ()
        if payment and payment.lower() not in ("all-payments", "buy", "sell"):
            pay_types = (payment,)
        fiat = (qs.get("fiat") or "USD").upper()
        return Query(mode="p2p", asset=asset, fiat=fiat, side=side,
                     pay_types=pay_types), None

    if re.fullmatch(_LANG + r"/copy-trading/?", path):
        return Query(mode="copytrading"), None

    m = re.fullmatch(_LANG + r"/support/announcement/(?:list/|c-)(\d+)/?", path)
    if m:
        return Query(mode="announcements", catalog=m.group(1)), None
    if re.fullmatch(_LANG + r"/support/announcement/?", path):
        return Query(mode="announcements"), None

    return None, ("%s is not a page this repo reads. Supported: a P2P trade "
                  "page, /copy-trading, or an announcement catalogue "
                  "(/support/announcement/list/{id})." % url)


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------

# The site's success code, on every endpoint.
OK_CODE = "000000"

# Markers of the AWS WAF interstitials, counted on 2026-09-24:
#   the CAPTCHA page real Chromium gets ("Human Verification", HTTP 405)
#       window.gokuProps  1    *.token.awswaf.com  1    challenge.js  1
#   the three endpoints' JSON responses: 0 of each
# `gokuProps` and the token host are the WAF's own integration vocabulary,
# not text a page would carry. The bare word `captcha` is NOT a marker: the
# Scraping Browser's auto-solve extension injects hunter scripts that say it
# on every page (CLAUDE.md §24).
AWS_WAF_MARKERS = ("gokuProps", "token.awswaf.com", "awswaf.com/")

# The HTTP statuses AWS WAF uses for an interstitial. 202 comes with an empty
# body and `x-amzn-waf-action: challenge`, a silent JS challenge that a real
# browser runs by itself. 405 is the CAPTCHA action.
WAF_STATUSES = (202, 405)


def detect_bot_challenge(html: Optional[str], url: str = "") -> Optional[str]:
    """The vendor whose interstitial this is, or None."""
    head = (html or "")[:200_000]
    if any(m in head for m in AWS_WAF_MARKERS):
        return "aws-waf"
    return None


def _json_or_none(text: Optional[str]) -> Any:
    if not text:
        return None
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def _as_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (str, bytes)):
        got = _json_or_none(payload if isinstance(payload, str)
                            else payload.decode("utf-8", "replace"))
        return got if isinstance(got, dict) else {}
    return {}


def detect_page_state(text: Optional[str], status: Optional[int] = None,
                      url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the site answered with. See page_flow.STATE_POLICY.

        content     the endpoint's JSON, code 000000, rows in it
        empty       the same, no rows: an answer, not a failure
        rejected    the endpoint refused the PARAMETERS: code != 000000, or
                    an HTTP 400. Nothing to retry and nothing to solve.
        challenge   AWS WAF: status 202/405, `x-amzn-waf-action`, or its
                    markers on the page
        throttled   429, or 418 (the site's own "you kept going after
                    429" answer)
        restricted  451, the status's own meaning: the exit's COUNTRY is
                    refused. Not observed on this site (page_flow).
        blocked     403, or any other refusal with no widget
        unknown     anything else: not JSON, not an interstitial

    Signals are ordered by what they PROVE, not by what they cost (§17):
    parseable JSON with the site's own envelope is checked first, because
    no interstitial carries it.
    """
    payload = _json_or_none(text)
    if isinstance(payload, dict) and "code" in payload:
        code = str(payload.get("code"))
        if code != OK_CODE:
            return "rejected"
        return "content" if count_rows(payload) > 0 else "empty"
    if waf_action or status in WAF_STATUSES or detect_bot_challenge(text):
        return "challenge"
    if status == 400:
        return "rejected"
    if status in (429, 418):
        return "throttled"
    if status == 451:
        return "restricted"
    if status == 403:
        return "blocked"
    return "unknown"


def api_error(text: Optional[str]) -> Optional[str]:
    """The endpoint's own complaint, for a `rejected` response."""
    payload = _as_dict(text)
    if not payload:
        return None
    code, msg = payload.get("code"), payload.get("message")
    if code is None or str(code) == OK_CODE:
        return None
    return "code %s: %s" % (code, msg or "(no message)")


# ---------------------------------------------------------------------------
# Payload access
# ---------------------------------------------------------------------------

def _row_list(payload: Dict[str, Any]) -> List[Any]:
    """The list of records in any of the three envelopes, or []."""
    data = payload.get("data")
    if isinstance(data, list):                      # p2p
        return data
    if isinstance(data, dict):
        if isinstance(data.get("list"), list):      # copy-trading
            return data["list"]
        cats = data.get("catalogs")                 # announcements
        if isinstance(cats, list):
            out: List[Any] = []
            for c in cats:
                if isinstance(c, dict) and isinstance(c.get("articles"), list):
                    out.extend(c["articles"])
            return out
    return []


def count_rows(payload: Any) -> int:
    return len(_row_list(_as_dict(payload)))


def total_results(payload: Any, mode: str) -> Optional[int]:
    """The site's own count of everything that matches, from page 1.

    Read from page 1 ONLY. The P2P endpoint reports `total: 0` on a page
    past the end, beside an empty `data`, so a later page's total states
    nothing about the listing.
    """
    p = _as_dict(payload)
    data = p.get("data")
    if mode == "p2p":
        t = p.get("total")
    elif mode == "copytrading":
        t = data.get("total") if isinstance(data, dict) else None
    elif mode == "announcements":
        cats = data.get("catalogs") if isinstance(data, dict) else None
        t = cats[0].get("total") if cats and isinstance(cats[0], dict) else None
    else:
        t = None
    return t if isinstance(t, int) and t >= 0 else None


def pages_available(total: Optional[int], mode: str) -> Optional[int]:
    if total is None:
        return None
    return max(1, math.ceil(total / ROWS_PER_PAGE[mode])) if total else 0


def pages_to_fetch(pages_requested: int, available: Optional[int]) -> int:
    ceiling = MAX_PAGES if available is None else min(available, MAX_PAGES)
    return max(1, min(int(pages_requested), ceiling))


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _iso_ms(v: Any) -> Optional[str]:
    """Epoch milliseconds -> ISO-8601 UTC. None for anything implausible."""
    n = _int(v)
    # 2009-01-01 .. 2100-01-01 in ms: anything outside is not a timestamp.
    if n is None or not (1_230_768_000_000 <= n <= 4_102_444_800_000):
        return None
    return datetime.fromtimestamp(n / 1000, tz=timezone.utc).isoformat()


def _round(v: Optional[float], places: int) -> Optional[float]:
    return None if v is None else round(v, places)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def advertiser_url(user_no: Optional[str]) -> str:
    return ("https://p2p.binance.com/en/advertiserDetail?advertiserNo=%s" % user_no
            if user_no else "")


def lead_url(portfolio_id: Optional[str]) -> str:
    return ("https://www.binance.com/en/copy-trading/lead-details/%s" % portfolio_id
            if portfolio_id else "")


def announcement_url(code: Optional[str]) -> str:
    """The announcement's canonical address.

    The site's own article bodies link announcements as
    `/en/support/announcement/{slug}-{code}`, and opening one of those in a
    real browser redirects to `/en/support/announcement/detail/{code}`
    (measured on two articles, 2026-09-24, past the WAF's CAPTCHA). The
    redirect target is what is written here, since it needs no slug built
    from a title.
    """
    return "%s/en/support/announcement/detail/%s" % (BASE, code) if code else ""


def parse_p2p_ad(rec: Dict[str, Any], query: Query, *, page: int,
                 position: int) -> Optional[P2PAd]:
    adv = rec.get("adv") if isinstance(rec.get("adv"), dict) else None
    who = rec.get("advertiser") if isinstance(rec.get("advertiser"), dict) else {}
    if not adv:
        return None
    adv_no = _str(adv.get("advNo"))
    if not adv_no:
        return None
    methods = [m for m in (adv.get("tradeMethods") or []) if isinstance(m, dict)]
    user_no = _str(who.get("userNo"))
    return P2PAd(
        url=advertiser_url(user_no),
        sku=adv_no,
        title=_str(who.get("nickName")),
        side=query.side,
        advertiser_side=(_str(adv.get("tradeType")) or "").lower() or None,
        asset=_str(adv.get("asset")),
        fiat=_str(adv.get("fiatUnit")),
        fiat_symbol=_str(adv.get("fiatSymbol")),
        price=_float(adv.get("price")),
        available=_float(adv.get("tradableQuantity") or adv.get("surplusAmount")),
        min_order_fiat=_float(adv.get("minSingleTransAmount")),
        max_order_fiat=_float(adv.get("dynamicMaxSingleTransAmount")
                              or adv.get("maxSingleTransAmount")),
        pay_methods=[m.get("identifier") for m in methods
                     if isinstance(m.get("identifier"), str)] or None,
        pay_method_names=[m.get("tradeMethodName") for m in methods
                          if isinstance(m.get("tradeMethodName"), str)] or None,
        pay_time_limit_min=_int(adv.get("payTimeLimit")),
        ad_class=_str(adv.get("classify")),
        extra_kyc_required=(bool(adv.get("takerAdditionalKycRequired"))
                            if adv.get("takerAdditionalKycRequired") is not None
                            else None),
        privilege_type=_int(rec.get("privilegeType")),
        advertiser_id=user_no,
        advertiser_type=_str(who.get("userType")),
        month_orders=_int(who.get("monthOrderCount")),
        month_finish_rate=_float(who.get("monthFinishRate")),
        positive_rate=_float(who.get("positiveRate")),
        page=page,
        position=position,
        mode="p2p",
        data_source="bapi",
    )


def parse_lead(rec: Dict[str, Any], query: Query, *, page: int,
               position: int) -> Optional[LeadTrader]:
    pid = _str(rec.get("leadPortfolioId"))
    if not pid:
        return None
    current, cap = _int(rec.get("currentCopyCount")), _int(rec.get("maxCopyCount"))
    return LeadTrader(
        url=lead_url(pid),
        sku=pid,
        title=_str(rec.get("nickname")),
        time_range=query.time_range,
        roi_pct=_round(_float(rec.get("roi")), 4),
        pnl=_round(_float(rec.get("pnl")), 4),
        aum=_round(_float(rec.get("aum")), 4),
        mdd_pct=_round(_float(rec.get("mdd")), 4),
        win_rate_pct=_round(_float(rec.get("winRate")), 4),
        copier_pnl=_round(_float(rec.get("copierPnl")), 4),
        sharpe_ratio=_round(_float(rec.get("sharpRatio")), 4),
        copiers=current,
        max_copiers=cap,
        is_full=(current >= cap) if (current is not None and cap) else None,
        badge=_str(rec.get("badgeName")),
        api_trading=(rec.get("apiKeyTag") == "API_KEY_TRADE"),
        tradfi=(rec.get("tradFiTag") is not None),
        portfolio_type=_str(rec.get("portfolioType")),
        started_at=_iso_ms(rec.get("startTime")),
        page=page,
        position=position,
        mode="copytrading",
        sort=query.sort_label,
        data_source="bapi",
    )


def parse_announcement(rec: Dict[str, Any], catalog: Dict[str, Any], *,
                       page: int, position: int) -> Optional[Announcement]:
    aid = _int(rec.get("id"))
    title = _str(rec.get("title"))
    if aid is None or not title:
        return None
    return Announcement(
        url=announcement_url(_str(rec.get("code"))),
        sku=str(aid),
        title=title,
        catalog_id=_int(catalog.get("catalogId")),
        catalog_name=_str(catalog.get("catalogName")),
        released_at=_iso_ms(rec.get("releaseDate")),
        page=page,
        position=position,
        mode="announcements",
        data_source="bapi",
    )


def parse_page(payload: Any, query: Query, page: int = 1) -> List[Any]:
    """Every row on one page of one mode's response, in the site's order.

    `position` counts the rows EMITTED, not the slots in the payload, so a
    record the parser drops cannot shift every later position (§24).
    """
    p = _as_dict(payload)
    if str(p.get("code")) != OK_CODE:
        return []
    rows: List[Any] = []
    if query.mode == "announcements":
        data = p.get("data") if isinstance(p.get("data"), dict) else {}
        for cat in data.get("catalogs") or []:
            if not isinstance(cat, dict):
                continue
            for rec in cat.get("articles") or []:
                if isinstance(rec, dict):
                    row = parse_announcement(rec, cat, page=page,
                                             position=len(rows) + 1)
                    if row:
                        rows.append(row)
        return rows
    parse = parse_p2p_ad if query.mode == "p2p" else parse_lead
    for rec in _row_list(p):
        if isinstance(rec, dict):
            row = parse(rec, query, page=page, position=len(rows) + 1)
            if row:
                rows.append(row)
    return rows

