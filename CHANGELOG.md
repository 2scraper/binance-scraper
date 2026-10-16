# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [Unreleased]

### Changed

- CI only: the canary now runs weekly (Mondays 05:40 UTC) and on a manual dispatch, with its optional secret `BINANCE_PROXY` passed on every run; the daily schedule is removed.

## [0.3.0] — 2026-10-15

> **Behaviour change:** `--retries 0` is now a usage error (exit 2). It used
> to be accepted and made the page-load loop unreachable, so nothing was
> fetched and the engine reported the browser's own blank page as a result:
> exit 3 or 4 with a 39-byte "capture", in tens of milliseconds. If a
> script of yours passes 0, pass **1**: the flag counts ATTEMPTS, so 1 means
> "try once and do not retry".
>
> **Behaviour change:** a page now buys at most one CAPTCHA solve in total,
> across every block-retry and exit rotation. It used to be one per
> LANDING, so a refused page could buy 2 AWS WAF solves with no proxy pool
> and 4 with a 3-exit pool. A page whose CAPTCHA survives that one solve is
> reported blocked (exit 3) sooner and for less.
>
> **Behaviour change:** CSV cells that start with `=`, `+`, `-`, `@`, a
> tab or a line break now carry a leading apostrophe, so a spreadsheet
> reads them as text. JSON is unchanged; the sidecar's `csv_cells_escaped`
> says how many cells differ.
>
> **Behaviour change:** `BINANCE_URL` from `.env` or the environment is a
> default only. A `--mode` or a query flag typed on the command line now
> wins over it instead of being refused as a conflict. `.env.example`
> ships it commented out.

### Fixed

- **One refused page could buy several CAPTCHA solves.** The allowance
  lived inside each landing, and every block-retry or rotation landed
  again. With the solver stubbed: 2 solves with no pool, 4 with a 3-exit
  pool, against `SOLVES_PER_PAGE = 1`. The allowance is now one
  `page_flow.SolveBudget` per page, checked before each purchase and never
  reset by a retry or a rotation. The `--pay-type` pre-check no longer
  solves at all. A suite check drives the shared loop with a solver that
  always "succeeds" and counts purchases.
- **A failed write destroyed the previous good output.** `out.json`,
  `out.csv` and `out.meta.json` were opened with truncation, so a failure
  inside `json.dump` left 13 bytes of invalid JSON where a good file had
  been. Each is now written to a temporary file in the same directory,
  fsynced and renamed over the target. A new file gets the mode `open()`
  would have given it (`0o666 & ~umask`), and an existing file keeps its
  own. Each file is replaced atomically on its own; the three are not one
  transaction.
- **CSV cells could run as spreadsheet formulas.** Advertiser nicknames,
  portfolio names and announcement titles are third-party text. See the
  behaviour change above.
- **A copied `.env.example` broke every flag-built query.** It set
  `BINANCE_URL` to a P2P page, so `--mode announcements`,
  `--mode copytrading` and `--asset/--fiat` all exited 2 with a conflict
  against a URL nobody typed. The engines now record the typed `--url`
  before `.env` is read.
- **Advice for HTTP 451 named a country nobody had measured.** The exit-3
  message, README, TROUBLESHOOTING, CONTRIBUTING and the bug template said
  a US exit is refused. 451 has not been observed on binance.com, and the
  daily canary is served from a GitHub-hosted runner with no proxy. The
  message now says so and asks for the exit's country in an issue.
- **A Scraping Browser 401 was explained as credentials that "last about
  a day".** That figure was not measured (an endpoint has been seen working
  after ten days). A 401 is now reported as refused credentials, expired or
  mistyped.
- A capture saved at the repo root (`--dump-html dump.html`) is ignored by
  git, and `.dockerignore` excludes every `.env*` file.
- Comments that pointed at private notes now state their reasons; a suite
  check keeps it that way.

- **`--retries 0` fetched nothing and called it a result.** The load loop is
  `range(1, retries + 1)`, so 0 never runs it and the engine judged
  `about:blank`. Three third-party audits of this family ran exactly that and
  reported a working scraper as broken. `--retries` now goes through
  `page_flow.attempts` in every entry point; a check reads the source of each
  one and calls the validator, so a new engine declaring the flag with a
  plain int fails the suite.

## [0.2.0] — 2026-09-30

### Added

- `http_scraper.py`: the three modes with no browser, through the same
  fetch loop, output and exit codes. Same flags as the browser engines
  minus the ones that describe a browser. It sends requests' own
  User-Agent rather than a Chrome one over a non-Chrome TLS handshake.
  Measured 2026-09-30: identical rows to Playwright in all three modes, in
  about a third to a half of the time. If AWS WAF answers instead of an
  endpoint it reports exit 3 straight away, without waiting for a
  challenge script that nothing here can run.
- The canary runs P2P through it daily, with no browser installed.

## [0.1.1] — 2026-09-30

> **A moved payload shape is no longer reported as an empty listing.** A
> page the endpoint served, whose own total says it holds rows, and from
> which no row could be read, used to end the run as exit 4 ("the listing
> has nothing in it"). It is now exit 5 on page 1 and exit 6 on a later
> page, with `stop_reason: parser_found_nothing`. Measured by renaming the
> row container in a real P2P capture that still counted 187 adverts.

### Changed

- The sidecar carries `core_field_shortfall`: for each page, the core
  columns filled on fewer than 99% of its rows. `{}` on a healthy run. A
  renamed field used to write a complete-looking file with the column null
  on every row and one warning in the log; the daily canary now fails on it.
- The canary names a moved shape on exit 5 and 6.
- CI tests Python 3.9 and 3.14 (was 3.9 and 3.12). 3.9 stays as the floor.
- The AWS WAF token cookie is set on the domain the challenge page lists in
  `awsWafCookieDomainList`, or on the page's own host when it lists none,
  rather than always on the registrable domain.
- `engine-smoke` checks that the engine imports, rather than grepping the
  suite's output for a phrase it never printed.

## [0.1.0] — 2026-09-24

First release. Three modes over binance.com's own JSON endpoints, three
browser engines over one shared fetch loop, and the 2Captcha Scraper API for
the one mode it can reach.

### Added

- `--mode p2p`: the P2P order book for an asset/fiat pair. One row per
  advert: price, per-order limits, payment methods, time limit, and the
  advertiser's 30-day orders, completion and feedback. Both sides of the
  trade are kept (`side` is what was asked, `advertiser_side` is what the
  advert says, and they are always opposite).
- `--mode copytrading`: Futures copy-trading lead portfolios, one row per
  portfolio: ROI, PnL, max drawdown, win rate, AUM, copier PnL, Sharpe,
  copiers and seats, badge, the period and the ordering.
- `--mode announcements`: one announcement catalogue (new listings,
  delistings, news, activities, maintenance, API updates, airdrops), one row
  per article, with the site's canonical `/detail/{code}` address.
- `--url` reads the query from a P2P trade page, the copy-trading page or an
  announcement catalogue address; the page itself is never fetched.
- Every query parameter is allowlisted, and `--pay-type` is checked against
  the site's own list for the fiat, because the API answers several wrong
  values with a plausible response instead of an error.
- Pages are planned from the total page 1 states; the sidecar records
  `total_results`, `pages_available` and the query.
- AWS WAF: its CAPTCHA is solved with AmazonTask / AmazonTaskProxyless, and
  the solution's `existing_token` is set as `aws-waf-token` on the
  registrable domain, the arrangement measured to clear it.
- `diff_runs.py` diffs two runs of one mode by `sku`, over columns derived
  from the row class rather than listed by hand.
- An offline suite over real, scrubbed API responses, including an
  end-to-end run of the shared fetch loop with a fake browser, and a daily
  canary of all three modes with no secrets.
