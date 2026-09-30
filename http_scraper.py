#!/usr/bin/env python3
"""
binance-scraper — HTTP edition (no browser)
===========================================

The same three modes, the same query flags, the same output, sidecar and
exit codes as playwright_scraper.py, with no browser at all: each page is
one `requests` call to the endpoint the site's own front end calls.

Why it exists
-------------
The browser engines never render a page either. They land on an ungated
JSON endpoint and issue every request as a same-origin fetch(), and the
browser is there for one reason: if AWS WAF ever moves in front of the
endpoints, its challenge can run and its CAPTCHA can be solved. Until then
it costs a Chromium download, a launch per run and per rotation, and the
memory of a browser for what is a few JSON requests.

Measured 2026-09-30 from a datacentre VPS, this engine's own client:
all three endpoints answered HTTP 200 with complete JSON, on every page
their totals implied (P2P 12 of 12, copy-trading 304, announcements 46).

What it gives up, and says so rather than failing quietly
---------------------------------------------------------
* **No WAF.** If a response is AWS WAF's challenge or CAPTCHA, nothing here
  can run the one or solve the other. The page reports blocked (exit 3)
  with that reason, straight away rather than after the browser engines'
  15-second wait for a script to run, and names the engine that can.
* **No --cdp-endpoint, --fingerprint, --locale or --headless**: they
  describe a browser. `--proxy`, `--proxy-file` and rotation work: a
  rotation is a fresh session, so no cookie crosses exits (§8).
* **Its own identity.** It sends requests' own User-Agent rather than
  claiming to be Chrome over a TLS handshake that is not Chrome's, which is
  the contradiction a sibling site refused (§24). Measured here: P2P
  answered both identities 6 of 6.

Usage
-----
    python http_scraper.py --asset USDT --fiat EUR --pages 3
    python http_scraper.py --mode copytrading --pages 5 --concurrency 3
    python http_scraper.py --mode announcements --category delisting

Requires: pip install -r requirements.txt   (nothing else)
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time

import requests

from product_parser import (ANN_CATALOGS, COPY_SORTS, COPY_TIME_RANGES,
                            DEFAULT_ANN_CATALOG, DEFAULT_COPY_SORT, P2P_SIDES)
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("http_scraper")

CONNECT_TIMEOUT_S = 15.0

# The words requests and urllib3 use for a proxy that could not be used.
# Distinguished from a timeout because the two want opposite responses: a
# timeout deserves another try at the same exit, a dead proxy a different
# one (§8).
_PROXY_ERROR_MARKERS = (
    "ProxyError",
    "Unable to connect to proxy",
    "Tunnel connection failed",
    "407 Proxy Authentication Required",
)

# Every `scheme://user:pass@` in a string, however many times it occurs.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _proxy_failure(text: str) -> str:
    """The proxy-error marker in `text`, or "" if it is not one."""
    for marker in _PROXY_ERROR_MARKERS:
        if marker in (text or ""):
            return marker
    return ""


class _Ops:
    """One requests.Session, exposed as page_flow's named operations.

    `runs_scripts` is False, which tells the shared loop not to wait for an
    AWS WAF challenge to clear itself: that is a script, and there is no
    browser here to run it.
    """

    runs_scripts = False

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.session = None
        self.landed = False
        self._last_text = ""
        self._warned_waf = False

    def open(self):
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json, text/plain, */*"
        if self.pool:
            exit_url = self.pool.current
            self.session.proxies = {"http": exit_url, "https": exit_url}
            logger.info("Using proxy exit %s", mask(exit_url))
        self.landed = False
        return self

    def _request(self, method: str, url: str, body=None):
        headers = {"Content-Type": "application/json"} if body is not None else {}
        return self.session.request(
            method, url, data=body, headers=headers,
            timeout=(CONNECT_TIMEOUT_S, page_flow.FETCH_TIMEOUT_MS / 1000.0))

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            resp = self._request("GET", url)
        except requests.RequestException as e:
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        self._last_text = resp.text
        return resp.status_code, resp.headers.get("x-amzn-waf-action")

    def document_text(self) -> str:
        return self._last_text

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        if not self._warned_waf:
            logger.error("AWS WAF answered instead of the endpoint. This engine "
                         "has no browser, so its challenge cannot run and its "
                         "CAPTCHA cannot be solved here. playwright_scraper.py "
                         "can do both, with TWOCAPTCHA_KEY for the CAPTCHA.")
            self._warned_waf = True
        return False

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            resp = self._request(req.method, req.url, req.body_json)
        except requests.RequestException as e:
            return None, "", None, _mask_credentials("%s: %s" % (type(e).__name__, e))
        return resp.status_code, resp.text, resp.headers.get("x-amzn-waf-action"), None

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        """A fresh session on the pool's current exit, so no cookie issued
        against one exit is replayed from another (§8)."""
        self.close()
        self.open()

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers, each with its own
    session and its own exit. The page loop is page_flow.worker_loop, the
    same one the browser engines use."""
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        ops = None
        try:
            ops = _Ops(args, page_flow.worker_pool(pool, index)).open()
            page_flow.worker_loop(ops, args, query, work, results, results_lock,
                                  exhausted, name, _mask_credentials)
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)
        finally:
            if ops is not None:
                ops.close()

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait())
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    concurrency = page_flow.concurrency_for(args, pool)
    return page_flow.run_pages(
        lambda: _Ops(args, pool).open(),
        lambda ops: ops.close(),
        lambda pages: _fetch_pages_concurrently(args, pool, args.query, pages,
                                                concurrency),
        args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="binance.com scraper — P2P adverts, copy-trading lead "
                    "portfolios and announcements (HTTP edition: no browser)")
    p.add_argument("--mode", choices=["p2p", "copytrading", "announcements"],
                   default=None,
                   help="p2p (default): the P2P order book for --asset/--fiat. "
                        "copytrading: Futures copy-trading lead portfolios. "
                        "announcements: one announcement catalogue, newest "
                        "first. Inferred from --url when that is given.")
    p.add_argument("--url", default=None,
                   help="A binance.com page to read the query from instead of "
                        "the flags: a P2P trade page "
                        "(p2p.binance.com/en/trade/all-payments/USDT?fiat=EUR, "
                        "…/trade/sell/BTC?fiat=TRY), /en/copy-trading, or "
                        "/en/support/announcement/list/{id}. The page itself "
                        "is never fetched — it is behind AWS WAF — only the "
                        "query in it is read. Also read from BINANCE_URL.")
    g = p.add_argument_group("p2p")
    g.add_argument("--asset", default=None,
                   help="The crypto asset (default USDT).")
    g.add_argument("--fiat", default=None,
                   help="The fiat currency, ISO 4217 (default USD).")
    g.add_argument("--side", choices=P2P_SIDES, default=None,
                   help="buy (default): adverts you could BUY the asset from. "
                        "Those adverts are marked SELL by the site, because "
                        "an advert carries the maker's side; the row keeps "
                        "both as `side` and `advertiser_side`.")
    g.add_argument("--pay-type", action="append", default=None, metavar="ID",
                   help="Only adverts taking this payment method, by the "
                        "site's identifier (SEPAinstant, Wise, BANK, …). "
                        "Repeatable. Checked against the site's own list for "
                        "the fiat before the search runs: the search answers "
                        "an unknown one with an EMPTY result, not an error.")
    g.add_argument("--amount", type=float, default=None,
                   help="Only adverts whose limits admit an order of this much "
                        "fiat.")
    g = p.add_argument_group("copytrading")
    g.add_argument("--time-range", choices=COPY_TIME_RANGES, default=None,
                   help="The period ROI, PnL and drawdown cover (default 30D).")
    g.add_argument("--sort-by", choices=sorted(COPY_SORTS), default=None,
                   help="Ordering (default %s). Not cosmetic: a capped run "
                        "holds the first N portfolios by this key, so it "
                        "decides WHICH portfolios are in the file. `sharpe` "
                        "also filters: portfolios without a Sharpe ratio are "
                        "left out. Win rate is not offered: the API gave a "
                        "nonsense key the same answer." % DEFAULT_COPY_SORT)
    g.add_argument("--order", choices=["desc", "asc"], default=None,
                   help="desc (default) or asc.")
    g.add_argument("--hide-full", action="store_true",
                   help="Leave out portfolios with no copier seat free.")
    g = p.add_argument_group("announcements")
    g.add_argument("--category", default=None,
                   help="The announcement catalogue: %s, or a numeric "
                        "catalogue id (default %s)."
                        % (", ".join(ANN_CATALOGS), DEFAULT_ANN_CATALOG))
    p.add_argument("--pages", type=int, default=1,
                   help="Pages to fetch (20 adverts, 30 portfolios or 50 "
                        "announcements each). Planned against the total the "
                        "site states on page 1, so asking for more than exist "
                        "fetches all of them.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Delay between pages, seconds (default %(default)s)")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1). "
                        "Each worker holds its own session and proxy exit.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page on a transport failure (default 3). "
                        "The pause doubles each time. A request the endpoint "
                        "REFUSED is not retried: its parameters would be "
                        "refused again.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="binance_rows", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy)")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page: a new exit, and a fresh session, per page.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page is refused (403, 451, AWS WAF), retry it "
                        "from this many OTHER exits (default 2). Needs a pool "
                        "of more than one.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact response the parser is given, on "
                        "success as well as failure. It is JSON; the flag "
                        "keeps the family's name.")
    args = p.parse_args(argv)
    env_config.apply(args)
    # Read by the shared loop, which serves the browser engines too. There is
    # no remote browser here.
    args.cdp_endpoint = None
    args.query = page_flow.build_query(args, p.error)
    args.mode = args.query.mode
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
