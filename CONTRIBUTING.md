# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

BBB changing its markup is the normal way this stops working, and it has its
own issue template. The detail that saves the most time is WHICH anchor
broke — and on this site that is not a CSS selector, because the parser does
not read the DOM.

BBB embeds its own state in every page, and on a listing page that state
holds the site's `/api/search` response verbatim:

```
window.__PRELOADED_STATE__ = {"user": …, "page": …, "searchResult": {…}}
```

So there are only four things that can break, and each fails loudly:

1. **The `__PRELOADED_STATE__` assignment.** If it is renamed,
   `extract_preloaded_state` returns None, `parse_listing` logs "no listing
   payload found" and the run reports 0 rows and exit 4. Loud.
2. **`searchResult` / `businessProfile`**, the two keys that say which kind
   of page this is. A rename makes a good page classify as `unknown` rather
   than as content — which waits and then reports honestly, rather than
   returning half a file.
3. **A record's own field names** — `businessName`, `rating`/`ratingScore`,
   `bbbMember`, `reportUrl`, `tobText`/`tobId`, `location`, `phone`. A rename
   here is the one that can be QUIET: the row still writes, with that column
   null. `CORE_FIELDS` in the engines is the guard — a coverage floor of 99%
   on the five columns BBB filled on 105 of 105 captured records.
4. **The `/api/search` endpoint itself.** If it starts requiring a key or
   goes behind Cloudflare, the README's central claim — that the listing
   modes need no account — stops being true, and the canary is what will say
   so, because it runs with no secrets from a datacenter runner.

If you are reporting a break, say which of those four it is, and attach the
`--dump-html` snapshot. The exact bytes are the only way to tell a parsing
bug from a page that had not arrived.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once —
   including its SKIP branch, which is what runs when the
   `BBB_CDP_ENDPOINT` secret is absent. Note that the canary's LISTING job
   runs daily with no secrets at all and is expected to be green — that half
   needs no credentials, and a green badge there is exactly the claim the
   README makes. Only the PROFILE job skips without a secret, because it
   fetches a Cloudflare-gated page and a GitHub runner is a datacenter
   address.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Six properties in this repo exist because they were once absent or were
measured against expectation, and cost real time. Tests pin all six, so a PR
that breaks one will fail rather than silently regress:

- **`sku` is `{bbbId}_{businessId}_{addressId}` and identifies a business AT
  A LOCATION.** "AV Brad Construction LLC" came back twice on one page of
  fifteen with the same `businessId` and two different address ids — two real
  locations, not a duplicate. Deduping on `businessId` would delete data the
  site published. A profile row REBUILDS the same sku from parts, because
  BBB's profile object states its own id as `0_209366` with a literal zero
  where the listing writes the bbbId.
- **An ungraded business is null, not zero.** `rating: ""` with
  `ratingScore: 0.0` means BBB has not graded it — 7 of 105 measured rows —
  and writing that zero through drags every average a consumer computes. The
  same trap exists one object deeper on a profile:
  `averageOfReviewStarRatings` is 0 on a business with no reviews, and BBB
  carries its own `displayAverageOfReviewStarRatings` flag for it.
- **`--sort` defaults to `a-z`, not to the site's own default**, and it is a
  COLUMN rather than only a sidecar field. Measured: `best-match` returned
  15/15 BBB Accredited businesses and not one match for the query, while
  `a-z` returned 0/15 accredited and real matches. The ordering decides
  WHICH businesses are in the file, so two runs that differ on it are not
  comparable and `diff_runs.py` refuses them.
- **A complete run can be a 1.2% sample.** BBB caps every query at 15 pages
  of 15 however many it matched, and page 16 answers HTTP 500 rather than an
  empty page. Runs PLAN against the `totalPages` the site states on page 1
  and the sidecar records `total_results`, `pages_available`,
  `capped_by_site` and `reachable_max`.
- **A block is not a challenge here, and only one of the two is solvable.**
  BBB answers a refused request either with a Managed Challenge (`cf_chl_opt`
  plus a Turnstile widget — a real test) or with a hard "You have been
  blocked" page carrying no widget at all. Both are HTTP 403 and both wear
  BBB's own branding in the `<title>`, so they are told apart structurally.
  `page_flow.STATE_POLICY` spends on the first and NEVER on the second.
- **A marker that matches every page is worse than no marker**, and this
  list has already been wrong once. `challenge-platform` and `cdn-cgi`
  appear on pages BBB serves normally — counted on its own 404 — so neither
  is in `BOT_CHALLENGE_MARKERS`. Neither is **`cf-turnstile`**, which is the
  obvious marker for a Turnstile and is measured useless here for a reason
  that has nothing to do with BBB: 2Captcha's own Scraping Browser
  auto-solve extension injects its hunters into every page it loads, so
  `cf-turnstile` fired on **five of five** pages fetched that way and on only
  one of the two real challenges. And `/turnstile/v0/api.js` fired on nothing
  at all, served or refused — dead weight, removed.

  What discriminates is the challenge's own vocabulary (`cf_chl_opt`,
  `__cf_chl`, `cf-chl-`, `challenges.cloudflare.com` — 0 on every served
  page) and, positively, whether the page was built out of `assets.bbb.org` /
  `m.bbb.org`.

  `smoke_test.py` pins all of it in both directions: no marker may appear on
  a served page (checked against a listing fetched THROUGH the Scraping
  Browser, which is the fixture that exposed the mistake), every marker must
  fire on a real challenge, and the three excluded strings must really be
  present on a served page — or excluding them would be a precaution against
  nothing.

- **BBB has its own captcha, and it is not the one above.** Every served page
  carries a reCAPTCHA **Enterprise** configuration
  (`NEXT_PUBLIC_GOOGLE_RECAPTCHA_SITE_KEY`, `recaptcha/enterprise.js?render=…`)
  for its review and complaint forms. `render=<sitekey>` means v3/Enterprise,
  not a v2 checkbox — worth knowing before anyone pays for the wrong task
  type. This scraper never touches those forms.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including a query that genuinely
  matched nothing, which is a correct answer — `5` remote API error, `6`
  partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A query
  that matched nothing was served exactly as asked.
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so concurrency cannot change
  the output.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
bbb.org, say in the PR what you ran, which mode and URL, from which exit,
and what you got — including the sidecar's `total_results`,
`pages_available` and `sort_applied`, and the coverage lines the run prints.

Two things about running this live that are specific to BBB:

* **The listing modes need no exit at all.** They read BBB's own endpoint,
  which answered a datacenter VPS normally, so "it worked from my laptop" is
  reproducible here in a way it is not on the sibling repos.
* **A profile run from a datacenter address gets HTTP 403**, every time — six
  consecutive polls over 30 seconds returned byte-identical markup. So "the
  profile mode is broken" from a VPS is not a finding; it is the documented
  behaviour, and a residential exit or the Scraping Browser is the answer.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification: the first live run of the pyppeteer engine crashed on its
FIRST fetch on a signature mismatch that four separate offline checks and 400
green assertions had not caught.

Do not add anything that submits a form. BBB's pages carry a "leave a
review" flow and a "file a complaint" flow, and this project must never
touch either — a review or a complaint filed by a scraper is a false record
about a real business.

## Scope

This repo scrapes **public pages** on BBB: search results, category listings
and business profiles, exactly as an anonymous visitor is served them.

Out of scope: anything behind a login, anything that submits a form
(including BBB's review and complaint flows), anything that defeats a
protection rather than passing it the way an ordinary browser does, and the
named individuals BBB lists as a business's officers — there is deliberately
no column for them, and adding one is a product decision rather than a bug
fix.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
