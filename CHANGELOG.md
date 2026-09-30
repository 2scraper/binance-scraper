# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [Unreleased]

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
