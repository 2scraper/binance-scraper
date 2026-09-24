# bbb-scraper

[![release](https://img.shields.io/github/v/release/2scraper/bbb-scraper)](https://github.com/2scraper/bbb-scraper/releases)
[![tests](https://github.com/2scraper/bbb-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/bbb-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/bbb-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/bbb-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%20%7C%203.12-blue)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20pyppeteer-lightgrey)
![runs without an account](https://img.shields.io/badge/search%20%26%20category-no%20account%20needed-brightgreen)

Scrapes business listings and business profiles from the **Better Business
Bureau** ([bbb.org](https://www.bbb.org)) — name, address, phone numbers, BBB
letter grade, accreditation, categories, coordinates, and on a profile the
accreditation date, years in business and complaint counts.

Three engines (Playwright, Selenium, pyppeteer) plus a browserless client for
2Captcha's Scraper API. One row schema, one exit-code contract, one sidecar.

---

## Start with the part most scrapers bury

**You do not need an account, a key or a proxy to read BBB's listings.**

BBB's own front end renders search and category pages out of a JSON endpoint,
and that endpoint is not behind Cloudflare. Measured **2026-09-16** from a
datacenter VPS in Nuremberg (netcup, AS197540) — an address that gets HTTP
**403 on every HTML page on the site**:

```
GET https://www.bbb.org/api/search?find_country=USA&find_text=restaurants
    &find_loc=New%20York%2C%20NY&page=1     ->  HTTP 200, 57 KB of JSON
```

Fifteen complete business records, richer than the rendered tile. A two-page
run from that machine with no `.env` at all returned **30 rows,
`status: complete`**.

What the 2Captcha products actually buy here is **`--mode profile`**. A
business profile page IS Cloudflare-gated, and there is no endpoint for it —
`/api/businessprofile`, `/api/profile`, `/api/orgs`, `/api/reviews` and
`/api/complaints` all return BBB's 404 page — so the rendered page is the only
route to accreditation dates, complaint counts and the rest.

| | listings (`search`, `category`) | profiles (`profile`) |
|---|---|---|
| Cloudflare | **not gated** | gated, HTTP 403 |
| needs a key | no | no |
| needs a residential exit | **no** | **yes** |
| measured from a datacenter IP | 200, 30 rows, complete | 403 |

---

## Install

```bash
git clone https://github.com/2scraper/bbb-scraper.git
cd bbb-scraper
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium
```

**Install exactly one engine.** The three declare mutually unsatisfiable pins
— playwright and pyppeteer disagree on `pyee`, pyppeteer and selenium on
`urllib3` — so `pip check` reports a conflict if you install more than one.
Use a virtualenv per engine if you need several.

## Run

```bash
# a keyword search, three pages
python playwright_scraper.py --text restaurants --location "New York, NY" --pages 3

# a category listing
python playwright_scraper.py --mode category \
    --url "https://www.bbb.org/us/category/restaurants" --pages 3

# one business profile (this is the one that needs a residential exit)
python playwright_scraper.py --mode profile \
    --url "https://www.bbb.org/us/ny/bronx/profile/cleaning-services/proclean-maintenance-systems-inc-0121-134716"

# Canada
python playwright_scraper.py --country CAN --text plumbers --location "Toronto, ON"
```

Output: `bbb_businesses.json`, `bbb_businesses.csv` and a
`bbb_businesses.meta.json` sidecar describing the run. See
[`sample_output.json`](sample_output.json) — 10 rows, 53 columns, cut from a
real run.

---

## Four things about BBB that will look like bugs

Each of these is the site, not the scraper, and each is measured.

### 1. "Best Match" is not a relevance ordering — it is an accredited placement

BBB's own default sort returned **15 of 15 BBB Accredited businesses** on
every page tried, and for `find_text=restaurants` returned cleaning services,
hotel management and home improvement — **not one restaurant**. The same query
under A-Z returned **0 of 15 accredited** and real name-matched restaurants.

So **this scraper defaults to `--sort a-z`**, which is the one place it
deliberately disagrees with the site. A-Z is also the only ordering that is
stable between runs, which is what a multi-page run needs. Pass
`--sort best-match` to reproduce what a visitor sees.

`sort` is a **column**, not just a sidecar field, because it changes *which*
businesses are in the file rather than only their order.

### 2. A complete run can still be a 1.2% sample

BBB caps **every** query at **15 pages of 15** — 225 rows — however many it
matched. One measured search reported `totalResults: 19016` against
`totalPages: 15`. A category listing reported 234,844.

The run is genuinely `status: complete`: it fetched everything BBB will serve
for that query. The sidecar says both numbers and sets `capped_by_site`, so a
consumer can tell "complete" from "exhaustive":

```json
{"status": "complete", "total_results": 19016, "pages_available": 15,
 "capped_by_site": true, "reachable_max": 225}
```

To go deeper, narrow the query: `--state NY` cut a 19,016-result search to
6,693, and each slice gets its own 15 pages.

Asking for page 16 answers **HTTP 500**, not an empty page — so the scraper
plans against `totalPages` and never asks for it.

### 3. An unrated business is not rated zero

BBB returns `rating: ""` with `ratingScore: 0.0` for a business it has not
graded — **7 of 105** measured. Written through, that zero drags every average
a consumer computes, so both `rating_grade` and `rating_score` are **null**.

The two columns are one rating in two notations. Measured over those 105 rows:

```
A+  >= 97.0      A  94.0-95.9      A-  92.9-93.8      B-  80.0
```

There is no `rating` column, because the rest of this repo family fills that
with a 0-to-5 float off a star widget and 100.0 there would mean something
else entirely.

### 4. One business can appear twice, and both rows are real

`sku` is `{bbbId}_{businessId}_{addressId}` and identifies a business **at a
location**. "AV Brad Construction LLC" came back twice on one page of fifteen:
same `businessId`, two `addressId`s, two real locations. Dedupe on `sku`;
`businessId` legitimately repeats.

A profile row rebuilds the same `sku`, so a profile run **joins** a listing
run. (BBB's profile object states its own id as `0_209366`, with a literal
zero where the listing writes the bbbId — a row keyed on that would never have
matched anything.)

---

## Engines

| | |
|---|---|
| `playwright_scraper.py` | **Primary.** The only one with `--concurrency`. Authenticates a proxy and a remote CDP endpoint. |
| `selenium_scraper.py` | Drives the Chrome you already have. **Cannot authenticate a proxy or a remote CDP endpoint** (`debuggerAddress` is a bare `host:port`), so it refuses a credentialled `--cdp-endpoint` with exit 2 — which makes it the wrong engine for `--mode profile` and a perfectly good one for the two listing modes, which need no credentials. |
| `puppeteer_scraper.py` | pyppeteer is effectively unmaintained and its own README points at Playwright. It downloads its **own Chromium**, which failed to launch on the development machine (`Browser closed unexpectedly`) — pass `--chromium-path` to point at another one. |
| `scraper_api_client.py` | One HTTP request per page via the 2Captcha Scraper API, no local browser. Built for `--mode profile`. **`--cdp-url` is required**: measured 2026-09-16, the same profile URL came back 403 plain and **200 with the profile parsing in full** when routed through a Scraping Browser session, at $0.0005. |

**All three browser engines produce the same rows.** Measured on three live
runs of the identical query on 2026-09-16: Playwright and Selenium were
**byte-identical** ignoring `scraped_at`; pyppeteer returned the identical 30
`sku`s with two of them swapped — both "11400 Inc", one business at two
addresses. BBB's A-Z ordering does not break a tie between two locations of
one business deterministically, so **`position` describes one fetch, not the
directory**. The `sku` set is stable, which is what `diff_runs.py` keys on.

---

## What the 2Captcha products buy, and when

One key, four separately-billed products
([2captcha.com](https://2captcha.com)):

* **A residential proxy** (`--proxy`, `--proxy-file`) — the thing that makes
  `--mode profile` work. BBB refuses a datacenter address: measured on the
  development VPS, the same profile URL was refused **identically on six
  consecutive polls over 30 seconds, byte for byte**, headless and headful,
  bundled Chromium and real Chrome alike. No amount of browser patching
  changes an ASN.
* **The Scraping Browser API** (`--cdp-endpoint`) — a remote browser, so you
  run none. This is what every live profile measurement in this README was
  taken through. One live connection per `pid`, which is why `--concurrency`
  is refused with it.
* **Captcha solving** — and it matters *which* product clears BBB's gate,
  because the two are billed separately. BBB refuses in two shapes:

  | | title | what clears it |
  |---|---|---|
  | Managed Challenge | `Just a moment...` | a real Turnstile widget — cleared by `Captcha.setAutoSolve` over `--cdp-endpoint` |
  | Hard block | `You have been blocked \| Better Business Bureau®` | nothing: no widget, no sitekey. A different exit is the only answer |

  Both are HTTP 403 and both wear BBB's own branding, so the scrapers tell
  them apart structurally. `--solve-captcha when-blocked` is the default and
  the `blocked` state never spends.

  **`--twocaptcha-key` alone does not clear the Managed Challenge here.** The
  local solver in `captcha_solver.py` builds `RecaptchaV2Task`,
  `RecaptchaV2TaskProxyless` and `RecaptchaV3TaskProxyless`, and **this repo
  does not implement `TurnstileTaskProxyless`** — nor the init script that
  captures `sitekey`, `action`, `cData` and `chlPageData` from Cloudflare's
  one call to `turnstile.render()`, which is the only way to obtain them
  (they appear nowhere in the served HTML). That is a gap in this repo and
  not in the product: 2Captcha solves Turnstile, and `foodpanda-scraper` in
  this family does exactly this. It is not implemented here because of where
  the gate actually is: the listing path — which is what this scraper is
  mostly for — is not behind Cloudflare at all, and the profile path, which
  is, already needs a Scraping Browser session to be reachable (measured
  2026-09-16: the same profile URL returned 403 direct and 200 in full
  through one). On that path `Captcha.setAutoSolve` clears the challenge
  inside the browser before a local solver would get a turn. The reCAPTCHA machinery is kept as a DETECTOR:
  which challenge a visitor meets depends on the exit and on what the address
  has been doing, and a narrow detector is how a challenge gets reported as
  an empty page months later.
* **Fingerprints** (`--fingerprint`) — a consistent device identity for a
  local browser. Ignored with `--cdp-endpoint`, which brings its own.

Nothing here integrates a competitor.

---

## Exit codes

| | |
|---|---|
| 0 | rows written |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked — Cloudflare, distinct from an empty result |
| 4 | zero businesses — the query matched nothing |
| 5 | remote API error (the Scraping Browser or Scraper API) |
| 6 | partial — some pages came back and some did not |

**A run that finds nothing writes nothing**, so a failure never replaces last
night's good output with `[]`. `--allow-empty` is the opt-out.

---

## Configuration

Credentials live in `.env` next to the scripts, never on a command line — a
secret in `argv` is readable by anything that can run `ps`. Copy
[`.env.example`](.env.example) and fill in what you use; `python3 env_config.py`
prints what was picked up **without printing secrets**.

Precedence: explicit flag → exported environment variable → `.env` → default.

---

## Tests

```bash
python3 smoke_test.py          # 337 offline checks, no network, no engine needed
python3 smoke_test.py -v       # every check as it passes
pytest                          # the same suite, one test
```

The suite runs with no engine library installed and records every skip. CI
installs each engine in its own virtualenv and fails if that engine's group
reports one, because "skipped, engine absent" reads identically to a real
import error.

The [canary](.github/workflows/canary.yml) runs a real 3-page listing daily —
**with no secrets**, because the listing path needs none, which is also what
keeps that claim honest. The profile half skips with a notice when no
`BBB_CDP_ENDPOINT` secret is set.

---

## Legal

This reads **public pages** on bbb.org: search results, category listings and
business profiles — the same pages a visitor sees, at a visitor's pace.

It does not read anything behind a login, and it deliberately does **not**
collect the named individuals BBB lists as a business's officers. There is no
column for them: republishing a named person's details is a separate act from
the site showing them on its own page.

Rate limits, terms of service and the legality of scraping in your
jurisdiction are your responsibility as the operator. `--delay` defaults to 2
seconds; leave it there unless you have a reason.

MIT licensed. Not affiliated with or endorsed by the Better Business Bureau.
