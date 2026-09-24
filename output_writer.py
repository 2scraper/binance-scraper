"""
output_writer.py
-----------------
Row models + JSON/CSV writers shared by all three engines.

Three modes, three row classes
------------------------------
    --mode p2p            P2PAd         one P2P advert
    --mode copytrading    LeadTrader    one copy-trading lead portfolio
    --mode announcements  Announcement  one announcement

These are three different things with nothing in common beyond an id and a
name, so they are three dataclasses rather than one wide row that is two-
thirds null on every line (CLAUDE.md §9: a column that is null on every row
of a mode should not exist in that mode's file).

What they DO share, byte-identical and in order, is the family prefix:
`source`, `scraped_at`, `url`, `sku`, `title`. One column name then works
across the whole family, and a consumer reading several of these repos reads
the same first five columns in the same order. The run-describing tail —
`page`, `position`, `mode`, `data_source` — is shared too.

None of the three is a shop row, so there is no `price`/`currency`/`brand`
triple to keep null. `P2PAd.price` IS a price, but it is the price of an
asset in a fiat, which the row states in its own `asset` and `fiat` columns
rather than in the family's `currency`.

Everything below the dataclasses is row-class-agnostic: pass `row_cls` so
an empty CSV still gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# Every row comes from www.binance.com. P2P has its own host
# (p2p.binance.com) for its PAGES, but the data is read from the www host's
# endpoints, and the value is kept constant so it cannot vary with which
# address the run happened to land on.
SOURCE_DEFAULT = "binance.com"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class P2PAd:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    # The advertiser's public P2P profile. An advert has no page of its own
    # that the list links to, while the advertiser does.
    url: str = ""
    # `adv.advNo`, the advert's own id: a 19-20 digit number, kept as a
    # string because it is past 2**53 and a JSON consumer in JavaScript would
    # round it.
    sku: Optional[str] = None
    # The advertiser's public nickname: the name a P2P tile shows.
    title: Optional[str] = None

    # ---- the two sides of one trade -------------------------------------
    # `side` is what the RUN asked for, from the taker's side: "buy" means
    # "adverts I could buy from". `advertiser_side` is what the advert
    # itself says, from the maker's side, and it is always the opposite:
    # 20 of 20 "sell" on a buy query, 20 of 20 "buy" on a sell query
    # (2026-09-24). Both are kept, because a consumer who reads the API's
    # `tradeType` alone gets every row backwards.
    side: Optional[str] = None
    advertiser_side: Optional[str] = None
    asset: Optional[str] = None
    fiat: Optional[str] = None
    fiat_symbol: Optional[str] = None
    # Fiat per unit of asset. The endpoint returns a STRING ("0.869"), parsed
    # here to a float.
    price: Optional[float] = None
    # How much of `asset` the advert still has on offer.
    available: Optional[float] = None
    # Per-order limits in FIAT. `max_order_fiat` is the site's DYNAMIC
    # maximum, capped by what is left on offer. That is the limit a taker
    # actually meets, not the static one the advertiser set.
    min_order_fiat: Optional[float] = None
    max_order_fiat: Optional[float] = None
    # Payment methods: `identifier` is what --pay-type filters on
    # ("SEPAinstant"), and the name is what the page shows ("SEPA Instant").
    # A LIST: one advert often takes several.
    pay_methods: Optional[List[str]] = None
    pay_method_names: Optional[List[str]] = None
    # Minutes the taker has to pay before the order is cancelled.
    pay_time_limit_min: Optional[int] = None
    # `adv.classify`: "mass", "profession" and similar. The site's own
    # category for the advert, written through unchanged.
    ad_class: Optional[str] = None
    extra_kyc_required: Optional[bool] = None
    # `privilegeType`, written through UNINTERPRETED. Exactly one advert per
    # page carried `1` on every page measured, always at position 1 and
    # always in price order, so it does not displace anything. What it means
    # is not stated anywhere in the payload, and this column does not guess.
    privilege_type: Optional[int] = None

    # ---- who is behind it -----------------------------------------------
    # `advertiser.userNo`, the site's public advertiser id.
    advertiser_id: Optional[str] = None
    # "user" or "merchant".
    advertiser_type: Optional[str] = None
    # Completed orders in the last 30 days, and the share completed. The
    # share is a FRACTION (1.0 = 100%) because that is how the site
    # publishes it.
    month_orders: Optional[int] = None
    month_finish_rate: Optional[float] = None
    # The share of positive feedback, also a fraction.
    positive_rate: Optional[float] = None

    # ---- about the RUN --------------------------------------------------
    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    # "bapi": the site's own JSON endpoint. Provenance in a column (§8).
    data_source: Optional[str] = None


@dataclass
class LeadTrader:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    url: str = ""
    # `leadPortfolioId`: a 19-digit number, a string for the same reason as
    # P2PAd.sku.
    sku: Optional[str] = None
    title: Optional[str] = None

    # The period every figure below covers. The same portfolio has a
    # different ROI under 7D and 30D, so this is part of what a figure
    # MEANS, and two runs over different periods are not comparable.
    time_range: Optional[str] = None
    # Percentages as the site publishes them: 4557.37 is 4,557.37%, not a
    # fraction. The `_pct` suffix says so, because `month_finish_rate` in the
    # P2P row is a fraction and a reader of both would otherwise have to
    # guess.
    roi_pct: Optional[float] = None
    # Profit and assets under management, in the portfolio's margin asset.
    # The list payload does not name that asset, so no currency column
    # pretends to.
    pnl: Optional[float] = None
    aum: Optional[float] = None
    # Maximum drawdown over the period.
    mdd_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    # What the portfolio's copiers made in total.
    copier_pnl: Optional[float] = None
    # Null on most rows, as the site publishes it (`sharpRatio: null`).
    sharpe_ratio: Optional[float] = None
    copiers: Optional[int] = None
    max_copiers: Optional[int] = None
    # copiers >= max_copiers: no seat left for a new copier.
    is_full: Optional[bool] = None
    # The site's tier badge ("CHAMPION", "EXPERT", "MASTER"), null for a
    # portfolio without one.
    badge: Optional[str] = None
    # `apiKeyTag == "API_KEY_TRADE"`: the lead trades through the API rather
    # than by hand. 16 of 30 on the first page measured.
    api_trading: Optional[bool] = None
    # `tradFiTag` present: the portfolio trades the TradFi perpetuals
    # (tokenised stocks and similar) as well as crypto.
    tradfi: Optional[bool] = None
    portfolio_type: Optional[str] = None
    # When the lead portfolio was opened.
    started_at: Optional[str] = None

    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    # The ordering that was asked for (`roi-desc`, `pnl-desc`, ...). It is a
    # column and not only a sidecar field because it decides WHICH
    # portfolios are in a capped run at all: the first 300 by ROI and the
    # first 300 by AUM are different samples. `diff_runs.py` refuses to
    # compare two runs that differ here (§21).
    sort: Optional[str] = None
    data_source: Optional[str] = None


@dataclass
class Announcement:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    # The address the site's own articles use to link each other.
    url: str = ""
    # The article's numeric `id`. It is the key rather than the article's
    # `code` because the code is 32 hex characters, the exact shape of an
    # API key, and a key column in that shape would teach every credential
    # scanner to ignore it. The code is still in `url`.
    sku: Optional[str] = None
    title: Optional[str] = None
    catalog_id: Optional[int] = None
    catalog_name: Optional[str] = None
    # `releaseDate`, epoch milliseconds, as ISO-8601 UTC.
    released_at: Optional[str] = None

    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    data_source: Optional[str] = None


# Row classes by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {"p2p": P2PAd, "copytrading": LeadTrader,
                     "announcements": Announcement}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py. All three qualify: an advert, a portfolio and
# an article each appear once per listing.
UNIQUE_BY_SKU_MODES = ("p2p", "copytrading", "announcements")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages.

    On this site the drop count is NOT expected to be zero on a long run,
    and that is a property of the data rather than a fault. All three
    listings are LIVE: the P2P `total` went from 186 to 187 between two
    requests a minute apart. A listing that gains an entry at the top
    between page 1 and page 2 pushes one row from page 1 onto page 2, where
    it is fetched a second time. The duplicate is dropped here. The mirror
    case, an entry REMOVED above the cut, pushes one row from page 2 onto
    page 1 after page 1 was fetched, and no scraper can see that row. The
    README says so.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = P2PAd) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked before any data arrived: AWS WAF's CAPTCHA or
# challenge, a 403, or a 451 refusing the exit's country. Distinct from
# EXIT_NO_PRODUCTS so a caller can tell "the listing genuinely has nothing in
# it" from "something stood between us and the listing".
#
# An empty listing is NOT this code. A P2P market with no adverts answers
# HTTP 200, code 000000, `data: []`, and that is EXIT_NO_PRODUCTS: the
# request was served exactly as asked.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: one output prefix can hold a P2P run, a copy-trading run or an
    announcements run, and those have different row classes. diff_runs.py
    refuses a pair whose modes or sources differ.

    `extra` carries facts about the run that are not about any single row:
    the query that was sent (asset/fiat/side, period and ordering,
    catalogue) and the site's OWN count of what matched (`total_results`,
    `pages_available`). The count is the only honest way to say how much of
    a listing a run holds. A 3-page copy-trading run is complete as a
    REQUEST and a 90-of-8,920 sample as a LISTING, and only the sidecar
    can say so.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        # Named "products" even though these are adverts, portfolios or
        # articles, and kept that way deliberately: every repo in this family
        # writes this key, and a consumer reading several of them reads one
        # sidecar shape. The row TYPE is `mode`, right beside it.
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = P2PAd) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 rows -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue.
#
# On this site there is a third and stronger signal, the site's own
# arithmetic. Every listing states its total on page 1, so the number of
# pages is PLANNED rather than discovered, and a run that fetched them all
# ends "completed". "end_of_listing" is the data-side stop: a page came back
# empty, meaning the live listing shrank below the plan during the run.
# That is complete too, because there was nothing more to get.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "end_of_listing")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, P2PAd)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
