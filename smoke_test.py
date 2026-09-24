#!/usr/bin/env python3
"""
smoke_test.py — the offline suite for bbb-scraper.

One file of plain functions with inline fixtures. No pytest, no conftest, no
fixtures directory (CLAUDE.md §10); `tests/test_smoke.py` wraps this as a
single pytest test so `pytest` works as an entry point without a second copy
of the checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and fails if that engine's group reports a skip.

The fixtures below are cut from real captures taken 2026-09-16 and trimmed to
the fields the parser reads. One field is NOT verbatim: BBB's profile object
names the business's officers as individuals, and those names are replaced
with REDACTED placeholders. The parser deliberately reads no such column;
republishing a named person is a separate act from the site showing them on
its own page (§10). Everything BBB generates around them is untouched, and
`check_fixtures_carry_no_personal_names` guards the SHAPE so a future capture
is caught too.
"""

import argparse
import ast
import csv
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, fields

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))
LISTING_PAYLOAD_JSON = r'''{"page":1,"pageSize":15,"totalPages":15,"totalResults":19016,"results":[{"id":"0121_100402_93156","businessName":"'1849' Bar and Grill","address":"183 Bleecker Street","city":"New York ","state":"NY","postalcode":"10012-1406","tobText":"Restaurants","rating":"","bbbMember":false,"reportUrl":"/us/ny/new-york-/profile/restaurants/1849-bar-and-grill-0121-100402","phone":["(212) 505-3200"],"location":"40.7290153503418,-74.0008773803711","businessId":"100402","ratingScore":0.0,"logoUri":null,"tobId":"50544-000","bbbId":"0121","bbbName":"BBB Serving Metropolitan New York","localReportUrl":null,"leaveReviewUrl":"/us/ny/new-york-/profile/restaurants/1849-bar-and-grill-0121-100402/leave-a-review","requestAQuoteUrl":null,"categories":[{"id":"50544-000","name":"Restaurants"}],"serviceAreasSummary":null,"hasServiceArea":false,"isCharity":0,"charitySeal":false,"accreditedCharity":false,"outOfBusinessStatus":null},{"id":"0021_191924_354991","businessName":"10 Rocks Tapas Bar & <em>Restaurant</em>","address":"1091 Main St","city":"Pawtucket","state":"RI","postalcode":"02860","tobText":"Restaurants","rating":"A+","bbbMember":false,"reportUrl":"/us/ri/pawtucket/profile/restaurants/10-rocks-lounge-tapas-llc-0021-191924","phone":["(401) 728-0800"],"location":"41.860408782958984,-71.39939880371094","businessId":"191924","ratingScore":100.0,"logoUri":null,"tobId":"50544-000","bbbId":"0021","bbbName":"Better Business Bureau in Eastern MA, ME, RI & VT","localReportUrl":null,"leaveReviewUrl":"/us/ri/pawtucket/profile/restaurants/10-rocks-lounge-tapas-llc-0021-191924/leave-a-review","requestAQuoteUrl":null,"categories":[{"id":"50544-000","name":"Restaurants"}],"serviceAreasSummary":["01504","01529","01569","01747","01756","02019","02031","02035","02038","02048","02053","02056","02067","02070","02071","02093","02334","02356","02375","02702","02703","02712","02715","02718","02720","02721","02722","02723","02724","02725","02726","02760","02761","02762","02763","02764","02766","02767","02768","02769","02771","02777","02779","02780","02802","02806","02809","02814","02815","02816","02818","02823","02824","02825","02826","02828","02829","02830","02831","02838","02839","02857","02858","02859","02860","02861","02862","02863","02864","02865","02872","02876","02885","02886","02887","02888","02889","02893","02895","02896","02901","02902","02903","02904","02905","02906","02907","02908","02909","02910","02911","02912","02914","02915","02916","02917","02918","02919","02920","02921","02940","MA","RI","USA"],"hasServiceArea":true,"isCharity":0,"charitySeal":false,"accreditedCharity":false,"outOfBusinessStatus":null},{"id":"0111_87055124_46262","businessName":"1-800 Water Damage","address":"2435 Bedford St Unit 15C","city":"Stamford","state":"CT","postalcode":"06905-3990","tobText":"Fire and Water Damage Restoration","rating":"","bbbMember":false,"reportUrl":"/us/ct/stamford/profile/fire-water-damage-restoration/1-800-water-damage-0111-87055124","phone":["(203) 406-9962","(914) 372-1319","(800) 928-3732"],"location":"41.07012939453125,-73.54624938964844","businessId":"87055124","ratingScore":0.0,"logoUri":null,"tobId":"60367-000","bbbId":"0111","bbbName":"BBB, Connecticut","localReportUrl":null,"leaveReviewUrl":"/us/ct/stamford/profile/fire-water-damage-restoration/1-800-water-damage-0111-87055124/leave-a-review","requestAQuoteUrl":null,"categories":[{"id":"60367-000","name":"Fire and Water Damage Restoration"},{"id":"66002-000","name":"Cleaning Services"},{"id":"60184-000","name":"Carpet and Rug Cleaners"},{"id":"60367-101","name":"Water Damage Restoration"}],"serviceAreasSummary":null,"hasServiceArea":false,"isCharity":0,"charitySeal":false,"accreditedCharity":false,"outOfBusinessStatus":null},{"id":"0121_87159665_265670","businessName":"Smart Cleaning - Cleaning Services Inc.","address":"","city":"New York","state":"NY","postalcode":"10036","tobText":"Cleaning Services","rating":"A+","bbbMember":true,"reportUrl":"/us/ny/new-york/profile/cleaning-services/smart-cleaning-cleaning-services-inc-0121-87159665","phone":["(501) 557-6278"],"location":"40.76026153564453,-73.9932861328125","businessId":"87159665","ratingScore":100.0,"logoUri":"https://m.bbb.org/prod/ProfileImages/2025/8894e6b2-3d6a-4654-b8aa-37616af0cab1.png","tobId":"66002-000","bbbId":"0121","bbbName":"BBB Serving Metropolitan New York","localReportUrl":null,"leaveReviewUrl":"/us/ny/new-york/profile/cleaning-services/smart-cleaning-cleaning-services-inc-0121-87159665/leave-a-review","requestAQuoteUrl":"/new-york-city/quote/request-smart-cleaning-cleaning-services-inc-87159665","categories":[{"id":"66002-000","name":"Cleaning Services"},{"id":"60449-000","name":"House Cleaning"},{"id":"85306-100","name":"Upholstery Cleaning"},{"id":"85306-000","name":"Rug Cleaning"}],"serviceAreasSummary":["NY"],"hasServiceArea":true,"isCharity":0,"charitySeal":false,"accreditedCharity":true,"outOfBusinessStatus":null},{"id":"0107_1400923_118755","businessName":"Premier Plumbing","address":"196 Divadale Dr","city":"East York","state":"ON","postalcode":"M4G 2P7","tobText":"Plumber","rating":"A+","bbbMember":true,"reportUrl":"/ca/on/east-york/profile/plumber/premier-plumbing-0107-1400923","phone":["(647) 868-4217"],"location":"43.71614074707031,-79.36264038085938","businessId":"1400923","ratingScore":100.0,"logoUri":"https://m.bbb.org/prod/ProfileImages/2025/9f44bc4e-a07f-4ab4-b5bb-0458439bb9d0.png","tobId":"10113-000","bbbId":"0107","bbbName":"BBB Serving Central Ontario","localReportUrl":null,"leaveReviewUrl":"/ca/on/east-york/profile/plumber/premier-plumbing-0107-1400923/leave-a-review","requestAQuoteUrl":"/kitchener/quote/request-premier-plumbing-1400923","categories":[{"id":"10113-000","name":"Plumber"},{"id":"10043-000","name":"Drainage Contractors"},{"id":"10171-100","name":"Basement Waterproofing"},{"id":"10113-300","name":"Plumbing Renovation"}],"serviceAreasSummary":null,"hasServiceArea":false,"isCharity":0,"charitySeal":false,"accreditedCharity":true,"outOfBusinessStatus":null},{"id":"0121_120583_128632","businessName":"AV Brad Construction LLC","address":"80 Nassau St Apt 102","city":"New York","state":"NY","postalcode":"10038-3795","tobText":"Home Improvement","rating":"A+","bbbMember":true,"reportUrl":"/us/ny/new-york/profile/home-improvement/av-brad-construction-llc-0121-120583","phone":["(917) 710-2622"],"location":"40.709922790527344,-74.00806427001953","businessId":"120583","ratingScore":100.0,"logoUri":null,"tobId":"10079-000","bbbId":"0121","bbbName":"BBB Serving Metropolitan New York","localReportUrl":"/us/ny/new-york/profile/home-improvement/av-brad-construction-llc-0121-120583/addressId/128632","leaveReviewUrl":"/us/ny/new-york/profile/home-improvement/av-brad-construction-llc-0121-120583/leave-a-review","requestAQuoteUrl":"/new-york-city/quote/request-av-brad-construction-llc-120583","categories":[{"id":"10079-000","name":"Home Improvement"},{"id":"10177-000","name":"Construction Services"},{"id":"66002-000","name":"Cleaning Services"},{"id":"10184-100","name":"Kitchen Cabinet Refacing"}],"serviceAreasSummary":["Arverne, NY","Astoria, NY","Bayside, NY","Bellerose, NY","Breezy Point, NY","Bronx, NY","Brooklyn, NY","Cambria Heights, NY","College Point, NY","Corona, NY","East Elmhurst, NY","Elmhurst, NY","Far Rockaway, NY","Floral Park, NY","Flushing, NY","Forest Hills, NY","Fresh Meadows, NY","Glen Oaks, NY","Hollis, NY","Howard Beach, NY","Jackson Heights, NY","Jamaica, NY","Kew Gardens, NY","Little Neck, NY","Long Island City, NY","Manhattan, NY","Maspeth, NY","Middle Village, NY","New York, NY","Oakland Gardens, NY","Ozone Park, NY","Queens Village, NY","Rego Park, NY","Richmond Hill, NY","Ridgewood, NY","Rockaway Park, NY","Rosedale, NY","Saint Albans, NY","South Ozone Park, NY","South Richmond Hill, NY","Springfield Gardens, NY","Staten Island, NY","Sunnyside, NY","Whitestone, NY","Woodhaven, NY","Woodside, NY","NY"],"hasServiceArea":true,"isCharity":0,"charitySeal":false,"accreditedCharity":true,"outOfBusinessStatus":null},{"id":"0121_120583_225724","businessName":"AV Brad Construction LLC","address":"64 Fulton St Rm 406","city":"New York","state":"NY","postalcode":"10038-2754","tobText":"Home Improvement","rating":"A+","bbbMember":true,"reportUrl":"/us/ny/new-york/profile/home-improvement/av-brad-construction-llc-0121-120583","phone":["(917) 710-2622","(212) 962-3524"],"location":"40.70873260498047,-74.00536346435547","businessId":"120583","ratingScore":100.0,"logoUri":null,"tobId":"10079-000","bbbId":"0121","bbbName":"BBB Serving Metropolitan New York","localReportUrl":null,"leaveReviewUrl":"/us/ny/new-york/profile/home-improvement/av-brad-construction-llc-0121-120583/leave-a-review","requestAQuoteUrl":"/new-york-city/quote/request-av-brad-construction-llc-120583","categories":[{"id":"10079-000","name":"Home Improvement"},{"id":"10177-000","name":"Construction Services"},{"id":"66002-000","name":"Cleaning Services"},{"id":"10184-100","name":"Kitchen Cabinet Refacing"}],"serviceAreasSummary":["Arverne, NY","Astoria, NY","Bayside, NY","Bellerose, NY","Breezy Point, NY","Bronx, NY","Brooklyn, NY","Cambria Heights, NY","College Point, NY","Corona, NY","East Elmhurst, NY","Elmhurst, NY","Far Rockaway, NY","Floral Park, NY","Flushing, NY","Forest Hills, NY","Fresh Meadows, NY","Glen Oaks, NY","Hollis, NY","Howard Beach, NY","Jackson Heights, NY","Jamaica, NY","Kew Gardens, NY","Little Neck, NY","Long Island City, NY","Manhattan, NY","Maspeth, NY","Middle Village, NY","New York, NY","Oakland Gardens, NY","Ozone Park, NY","Queens Village, NY","Rego Park, NY","Richmond Hill, NY","Ridgewood, NY","Rockaway Park, NY","Rosedale, NY","Saint Albans, NY","South Ozone Park, NY","South Richmond Hill, NY","Springfield Gardens, NY","Staten Island, NY","Sunnyside, NY","Whitestone, NY","Woodhaven, NY","Woodside, NY","NY"],"hasServiceArea":true,"isCharity":0,"charitySeal":false,"accreditedCharity":true,"outOfBusinessStatus":null}],"sortTypes":[{"label":"Best Match","value":"Relevance","isActive":false},{"label":"Distance","value":"Distance","isActive":false},{"label":"Rating","value":"Rating","isActive":false},{"label":"A-Z","value":"AToZ","isActive":true},{"label":"Z-A","value":"ZToA","isActive":false}],"heading":{"searchInputText":"restaurants","searchLocationText":"New York, NY"},"filters":{"byId":{"filter_category":{"id":"filter_category","filterOptions":[{"value":"50544-000","label":"Restaurants"},{"value":"66002-000","label":"Cleaning Services"},{"value":"66002-100","label":"Commercial Cleaning Services"},{"value":"60449-000","label":"House Cleaning"},{"value":"50164-020","label":"Fast Food Restaurants"}]}}}}'''

EMPTY_PAYLOAD_JSON = r'''{"page":1,"pageSize":15,"totalPages":0,"totalResults":0,"results":[],"sortTypes":[{"label":"Best Match","value":"Relevance","isActive":true},{"label":"Distance","value":"Distance","isActive":false},{"label":"Rating","value":"Rating","isActive":false},{"label":"A-Z","value":"AToZ","isActive":false},{"label":"Z-A","value":"ZToA","isActive":false}],"heading":{"searchInputText":"zzzqqxnonexistentbiz","searchLocationText":"New York, NY"}}'''

PROFILE_STATE_JSON = r'''{"user":{},"page":{},"businessProfile":{"id":"0_209366","bbbId":"0121","businessId":"134716","isMultiLocation":true,"names":{"primary":"Proclean Maintenance Systems, Inc."},"rating":{"bbbRating":"A+","ratingReasonNotRated":null,"ratingReasons":[]},"accreditationInformation":{"isAccredited":true},"dates":{"accreditationRevoked":null,"accredited":"2018-10-22T00:00:00","bbbFileOpened":"2012-05-29T00:00:00","businessStart":"2010-01-26T00:00:00","businessLocalStart":null,"incorporated":"2010-01-26T00:00:00","newOwnerDate":null},"localBbbData":{"name":"BBB Serving Metropolitan New York"},"location":{"latitude":40.806404,"longitude":-73.928108,"postalAddress":{"addressLine1":"79 Alexander Ave Ste B16","addressLine2":null,"city":"Bronx","stateCode":"NY","zipCode":"10454-4409"},"servingArea":null,"servingAreas":null},"urls":{"profile":"/us/ny/bronx/profile/cleaning-services/proclean-maintenance-systems-inc-0121-134716","primary":"https://www.pc-ms.com","submitReview":"/us/ny/bronx/profile/cleaning-services/proclean-maintenance-systems-inc-0121-134716/leave-a-review","requestQuote":"/new-york-city/quote/request-proclean-maintenance-systems-inc-134716"},"orgDetails":{"isOutOfBusiness":false,"organizationDescription":"Proclean\nMaintenance Systems, Inc. provides janitorial services, floor care, carpet cleaning, window washing, and construction cleanup for both large and commercial clients.","typeOfEntity":{"legalOrgType":1003,"name":"Corporation","canDisplay":true},"yearsInBusiness":16},"reviewsComplaintsSummary":{"suppressReviews":false,"averageOfReviewStarRatings":0,"displayReviewStarRating":true,"reviewsTotal":0,"complaintsTotal":0,"displayAverageOfReviewStarRatings":false,"submitReviewErrorMessage":"Unable to save review. Please try submitting the review again.","totalClosedComplaintsPastThreeYears":0,"totalClosedComplaintsPastTwelveMonths":0},"categories":{"links":[{"title":"Cleaning Services","url":"/us/ny/bronx/category/cleaning-services","entityType":null,"entityId":null,"id":null},{"title":"Commercial Cleaning Services","url":"/us/ny/bronx/category/commercial-cleaning-services","entityType":null,"entityId":null,"id":null}]},"media":{"logo":null},"contactInformation":{"emailAddress":"","phoneNumber":"(212) 618-6387","additionalPhoneNumbers":[],"additionalFaxNumbers":[{"name":null,"value":"(877) 784-2471","labels":[]}],"contacts":[{"isPrincipal":true,"title":"President","name":{"prefix":"REDACTED","first":"REDACTED","middle":null,"last":"REDACTED","suffix":null}}]}}}'''

CHALLENGE_HTML = "<html><head><title>Just a moment...</title></head><body>pt and cookies to continue</span></div></noscript></div></div><script>(function(){window._cf_chl_opt = {cFPWv: 'g',cH: 'D_0_uMlpTlCJDowK3ZLIVeJniyMUVaa_9KFQUPd0AcA-1789556207-1.2.1.1-NGA0bT5PiVPrj9KihzBFW4gPPS2gMqfoRyp3GPIQqeeDJc2 … Tk:\"/search?find_country=USA\\u0026find_text=restaurants\\u0026find_loc=New+York%2C+NY\\u0026__cf_chl_tk=M35_sw6MOEfJn5420KpbyV7lFLfDRv8fpRAmUuaQc70-1789556207-1.0.1.1-pCKVU1Wd8nodmdl6Aw7k9mNgfqUWqug_PhXrPvdu.pE\",cvId: '3',cZone: 'ww … isplay: grid;\"><div><div><div></div><input type=\"hidden\" name=\"cf-turnstile-response\" id=\"cf-chl-widget-tmfx1_response\"></div></div></div><div id=\"CJPCL0\" style=\"display: none;\"><div>Verification successful. Waiting for www.bbb.or … -platform/h/g/orchestrate/chl_page/v1?ray=a3bf58b98f99cee6\"></script><script src=\"https://challenges.cloudflare.com/turnstile/v0/g/330e41bb475c/api.js?onload=khCN8&amp;render=explicit\" async=\"\" defer=\"\" crossorigin=\"anonymous\"></s … chl_page/v1?ray=a3bf58b98f99cee6\"></script><script src=\"https://challenges.cloudflare.com/turnstile/v0/g/330e41bb475c/api.js?onload=khCN8&amp;render=explicit\" async=\"\" defer=\"\" crossorigin=\"anonymous\"></script></head>\n  <body>\n   </body></html>"

HARD_BLOCK_HTML = "<html><head><title>You have been blocked | Better Business Bureau®</title></head><body>ta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n\n    <title>You have been blocked | Better Business Bureau®</title>\n\n    <style id=\"custom-props\">\n      :root {\n        --bds-color-black: #2d2926;\n    … 3',t:'MTc4OTU1NjMyMw=='};var a=document.createElement('script');a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';document.getElementsByTagName('head')[0].appendChild(a);\";b.getElementsByTagName('head')[0].appendCh</body></html>"

SERVED_404_HTML = "<html><head><title>Page not found | Better Business Bureau&#174;</title></head><body>n>\n    <div id=\"iabbb-footer-placeholder\"></div>\n    <script async src=\"https://assets.bbb.org/bbb-web/universal/dist/hf.min.js\"></script>\n  <script>(function(){function c(){var b=a.contentDocument||(a.contentWindow&&a.c … t\"\n            height=\"133\"\n            loading=\"lazy\"\n            src=\"https://m.bbb.org/terminuscontent/dist/img/404-icon__254w.png?tx=w_109\"\n            width=\"109\"\n          />\n        </div>\n        <img\n          a … a',t:'MTc4OTU1NzQwMg=='};var a=document.createElement('script');a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';document.getElementsByTagName('head')[0].appendChild(a);\";b.getElementsByTagName('head')[0].appendCh</body></html>"


# A page BBB SERVED, fetched THROUGH the 2Captcha Scraping Browser.
# Trimmed around the things that made it dangerous: the auto-solve
# extension's injected hunters, its `cf-turnstile-response` input, BBB's
# own reCAPTCHA Enterprise loader, and a real BBB asset reference.
SERVED_VIA_SCRAPING_BROWSER_HTML = "<html><head><title>Search results for Restaurants near New York, NY | Better Business Bureau®</title></head><body>oogletagmanager.com/gtag/js?id=G-QWV3Q1HBDG&amp;cx=c&amp;gtm=4e69e1\"></script><script src=\"chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/captchafox/interceptor.js\"></script><script src=\"chrome-exten …  src=\"chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/turnstile/hunter.js\" data-ts-input=\"cf-turnstile-response\"></script><script src=\"chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captc … aptcha(){if(loaded){return;}\nloaded=true;var s=document.createElement('script');s.src=\"https://www.google.com/recaptcha/enterprise.js?render=6Lfm-HorAAAAANvcLwOHwVYwoAkSuRb_sWokbfq3\";s.async=true;s.onload=function(){remo … r Restaurants near New York, NY | Search | Better Business Bureau</title><link rel=\"preconnect\" href=\"https://assets.bbb.org\"><link rel=\"preconnect\" href=\"https://www.googletagmanager.com\"><link rel=\"preconnect\" href=\"ht</body></html>"


# ---------------------------------------------------------------------------
# The parser, asserted on VALUES rather than on coverage
# ---------------------------------------------------------------------------
# A column can be 100% populated and entirely wrong (CLAUDE.md §10), so every
# check below pins a figure from the real capture rather than counting
# non-nulls.

def check_listing_parses():
    import product_parser as P
    page = P.parse_listing(LISTING_PAYLOAD_JSON, page=1, mode="search",
                           sort="a-z")
    equal("listing: row count", len(page.rows), 7)
    equal("listing: totalResults read from the site", page.total_results, 19016)
    equal("listing: totalPages read from the site", page.pages_available, 15)
    equal("listing: pageSize", page.page_size, 15)
    equal("listing: sort read BACK from the response", page.sort, "a-z")
    equal("listing: query echoed in heading", page.query_text, "restaurants")
    check("listing: not an empty result set", not page.is_empty_result_set)
    equal("listing: every row carries the page it came from",
          sorted({r.page for r in page.rows}), [1])
    equal("listing: positions are 1..n in the site's order",
          [r.position for r in page.rows], [1, 2, 3, 4, 5, 6, 7])
    equal("listing: mode is recorded on the row",
          sorted({r.mode for r in page.rows}), ["search"])
    equal("listing: sort is recorded on the row",
          sorted({r.sort for r in page.rows}), ["a-z"])
    equal("listing: data_source is recorded",
          sorted({r.data_source for r in page.rows}), ["api"])
    equal("listing: source column", sorted({r.source for r in page.rows}),
          ["bbb.org"])


def check_ungraded_business_is_null_not_zero():
    """The trap: `rating: ""` with `ratingScore: 0.0` means NOT GRADED.

    Written through as 0.0 that zero drags every average a consumer computes.
    7 of 105 captured businesses are in this state.
    """
    import product_parser as P
    rows = P.parse_listing(LISTING_PAYLOAD_JSON).rows
    ungraded = [r for r in rows if r.title == "'1849' Bar and Grill"]
    equal("ungraded: the fixture still carries one", len(ungraded), 1)
    row = ungraded[0]
    equal("ungraded: rating_grade is None", row.rating_grade, None)
    equal("ungraded: rating_score is None, NOT 0.0", row.rating_score, None)
    check("ungraded: no row anywhere scores exactly 0.0",
          not any(r.rating_score == 0.0 for r in rows))
    graded = [r for r in rows if r.rating_grade]
    check("graded rows still carry a score",
          all(r.rating_score is not None for r in graded),
          "%r" % [(r.rating_grade, r.rating_score) for r in graded])


def check_search_highlighting_is_stripped():
    """`businessName` carries `<em>` when the query matched the name."""
    import product_parser as P
    rows = P.parse_listing(LISTING_PAYLOAD_JSON).rows
    titles = [r.title for r in rows]
    check("titles: no markup survives", not any("<" in t for t in titles),
          "%r" % [t for t in titles if "<" in t])
    equal("titles: the highlighted fixture row reads cleanly",
          [t for t in titles if t.startswith("10 Rocks")],
          ["10 Rocks Tapas Bar & Restaurant"])
    equal("strip_highlight is exact", P.strip_highlight("ADA <em>Restaurant</em>"),
          "ADA Restaurant")
    equal("strip_highlight keeps None as None", P.strip_highlight(None), None)


def check_phone_is_a_list_not_a_first_element():
    """78 of 105 rows carry one number, 19 carry two, and one carries TWELVE."""
    import product_parser as P
    rows = P.parse_listing(LISTING_PAYLOAD_JSON).rows
    longest = max(rows, key=lambda r: len(r.phone or []))
    check("phone: the many-numbered fixture row keeps them all",
          len(longest.phone or []) >= 3,
          "longest is %r" % (longest.phone,))
    check("phone: every entry is a non-empty string",
          all(isinstance(n, str) and n.strip()
              for r in rows for n in (r.phone or [])))


def check_absent_fields_are_none_not_empty_string():
    import product_parser as P
    rows = P.parse_listing(LISTING_PAYLOAD_JSON).rows
    no_address = [r for r in rows if r.address is None]
    check("address: the storefront-less fixture row is None, not ''",
          len(no_address) >= 1)
    check("no row carries an empty string in any text column",
          not any(v == "" for r in rows for v in asdict(r).values()))


def check_country_comes_from_the_row_not_the_query():
    import product_parser as P
    rows = P.parse_listing(LISTING_PAYLOAD_JSON).rows
    equal("country: both directories appear in one fixture",
          sorted({r.country for r in rows}), ["CAN", "USA"])
    canadian = [r for r in rows if r.country == "CAN"]
    check("country: the Canadian row's URL says /ca/",
          all("/ca/" in r.url for r in canadian))
    equal("country_from_path reads the segment",
          product_parser_country("/us/ny/bronx/profile/x/y"), "USA")


def product_parser_country(path):
    import product_parser as P
    return P.country_from_path(path)


def check_sku_identifies_a_location_not_a_business():
    """Same business, two address ids — two real locations, not a duplicate."""
    import product_parser as P
    from output_writer import dedupe_by_key
    rows = P.parse_listing(LISTING_PAYLOAD_JSON).rows
    by_business = {}
    for r in rows:
        by_business.setdefault(r.business_id, []).append(r)
    repeated = [v for v in by_business.values() if len(v) > 1]
    equal("sku: the fixture carries one business at two locations",
          len(repeated), 1)
    pair = repeated[0]
    equal("sku: the two share a businessId", len({r.business_id for r in pair}), 1)
    equal("sku: the two have DIFFERENT skus", len({r.sku for r in pair}), 2)
    equal("sku: and different address ids", len({r.address_id for r in pair}), 2)
    kept = dedupe_by_key(rows, set(), key="sku")
    equal("dedupe on sku keeps both locations", len(kept), len(rows))
    dropped = dedupe_by_key(rows, {r.sku for r in rows}, key="sku")
    equal("dedupe drops a genuinely repeated page", len(dropped), 0)
    equal("sku is {bbbId}_{businessId}_{addressId}",
          [p for p in pair[0].sku.split("_")],
          [pair[0].bbb_id, pair[0].business_id, pair[0].address_id])


def check_coordinates_are_split_into_two_floats():
    import product_parser as P
    rows = P.parse_listing(LISTING_PAYLOAD_JSON).rows
    check("location: every fixture row got both coordinates",
          all(r.latitude is not None and r.longitude is not None for r in rows))
    equal("split_location splits a real value",
          P.split_location("40.75806427001953,-73.97885131835938"),
          (40.75806427001953, -73.97885131835938))
    equal("split_location refuses half a coordinate",
          P.split_location("40.758"), (None, None))
    equal("split_location refuses a non-string", P.split_location(None),
          (None, None))
    equal("split_location refuses three parts",
          P.split_location("1,2,3"), (None, None))


def check_empty_result_set_is_an_answer_not_a_block():
    import product_parser as P
    import page_flow
    page = P.parse_listing(EMPTY_PAYLOAD_JSON, page=1, mode="search")
    equal("empty: no rows", len(page.rows), 0)
    equal("empty: the site's own total is 0", page.total_results, 0)
    check("empty: recognised as an empty RESULT SET", page.is_empty_result_set)
    equal("empty: classified as 'empty', not 'blocked'",
          P.detect_page_state(EMPTY_PAYLOAD_JSON, 200), "empty")
    check("empty: policy parses it rather than retrying",
          page_flow.should_parse("empty") and not page_flow.should_retry("empty"))
    check("empty: policy does NOT count it as blocked",
          not page_flow.counts_as_blocked("empty"))


def check_profile_parses_and_joins_a_listing_row():
    """A profile row must JOIN a listing row on the family's own key.

    It only does because `sku` is REBUILT from parts: BBB's profile object
    states its own id as "0_209366", with a literal zero where the listing
    writes the bbbId, so a row keyed on it would never have matched anything.
    """
    import product_parser as P
    row = P.parse_profile(PROFILE_STATE_JSON)
    check("profile: parsed", row is not None)
    equal("profile: title", row.title, "Proclean Maintenance Systems, Inc.")
    equal("profile: sku is rebuilt, not the page's own '0_...' id",
          row.sku, "0121_134716_209366")
    check("profile: the page's raw id is NOT the sku",
          not row.sku.startswith("0_"))
    equal("profile: bbb_id", row.bbb_id, "0121")
    equal("profile: business_id", row.business_id, "134716")
    equal("profile: address_id", row.address_id, "209366")
    equal("profile: grade", row.rating_grade, "A+")
    equal("profile: accredited", row.is_accredited, True)
    equal("profile: accreditation date", row.accredited_since,
          "2018-10-22T00:00:00")
    equal("profile: BBB file opened", row.bbb_file_opened, "2012-05-29T00:00:00")
    equal("profile: years in business", row.years_in_business, 16)
    equal("profile: entity type", row.entity_type, "Corporation")
    equal("profile: website", row.website, "https://www.pc-ms.com")
    equal("profile: complaints, all three windows",
          (row.complaints_total, row.complaints_3y, row.complaints_12m),
          (0, 0, 0))
    equal("profile: multi-location flag", row.is_multi_location, True)
    equal("profile: out of business is a BOOL on this side too",
          row.out_of_business, False)
    equal("profile: country from its own path", row.country, "USA")
    equal("profile: mode and provenance", (row.mode, row.data_source),
          ("profile", "profile"))
    equal("profile: categories from its own links", row.categories,
          ["Cleaning Services", "Commercial Cleaning Services"])


def check_profile_zero_stars_means_no_reviews():
    """`averageOfReviewStarRatings` is 0 on a business with no reviews.

    The same trap as the 0.0 rating score, one object deeper. BBB carries its
    own `displayAverageOfReviewStarRatings` flag for it, so the site's answer
    is used rather than a guess.
    """
    import product_parser as P
    row = P.parse_profile(PROFILE_STATE_JSON)
    equal("profile: reviews_total is a real 0", row.reviews_total, 0)
    equal("profile: review_stars_avg is None, NOT 0.0",
          row.review_stars_avg, None)

    state = json.loads(PROFILE_STATE_JSON)
    summary = state["businessProfile"]["reviewsComplaintsSummary"]
    summary["displayAverageOfReviewStarRatings"] = True
    summary["averageOfReviewStarRatings"] = 4.5
    summary["reviewsTotal"] = 12
    reviewed = P.parse_profile(json.dumps(state))
    equal("profile: a displayable average IS read", reviewed.review_stars_avg, 4.5)


def check_profile_does_not_invent_a_rating_score():
    """A+ spans 97.0-100.0 on the listing measurements, so inverting the
    letter would invent a precision the page does not have."""
    import product_parser as P
    row = P.parse_profile(PROFILE_STATE_JSON)
    equal("profile: rating_score stays None beside a real grade",
          (row.rating_grade, row.rating_score), ("A+", None))


def check_profile_publishes_no_personal_names():
    """The parser must not lift BBB's named officers into a column.

    BBB publishes them; republishing a named person is a separate act from
    the site showing them on its own page (§10). This asserts the CURRENT
    behaviour so that adding such a column becomes a decision rather than an
    accident.
    """
    import product_parser as P
    from output_writer import Business
    row = P.parse_profile(PROFILE_STATE_JSON)
    columns = set(asdict(Business()).keys())
    for banned in ("contact", "contacts", "principal", "officer", "owner_name",
                   "person"):
        check("schema has no %r column" % banned,
              not any(banned in c for c in columns),
              "found %r" % sorted(c for c in columns if banned in c))
    values = " ".join(str(v) for v in asdict(row).values())
    check("no parsed value carries the fixture's placeholder name",
          "REDACTED" not in values)


def check_fixtures_carry_no_personal_names():
    """Guard the SHAPE, not the old literals, so a future capture is caught.

    A re-captured profile would bring a real officer's name back with it;
    this fails the build if one is ever committed un-scrubbed.
    """
    state = json.loads(PROFILE_STATE_JSON)
    contacts = state["businessProfile"]["contactInformation"]["contacts"]
    check("fixture: profile contacts are present but scrubbed",
          contacts and all(
              c["name"]["first"] == "REDACTED" and c["name"]["last"] == "REDACTED"
              for c in contacts),
          "%r" % contacts)
    # The same guard over the whole file: a person-shaped name object with
    # anything other than the placeholder in it.
    source = open(os.path.join(HERE, "smoke_test.py"), encoding="utf-8").read()
    # Only the fixture literals, not this file's own pattern text.
    literals = "\n".join(line for line in source.splitlines()
                         if line.startswith(("LISTING_PAYLOAD_JSON",
                                             "EMPTY_PAYLOAD_JSON",
                                             "PROFILE_STATE_JSON")))
    real_names = re.findall(r'"first":"(?!REDACTED)([^"]{2,})"', literals)
    check("fixture: no real given name anywhere in this file",
          not real_names, "found %r" % real_names[:5])


# ---------------------------------------------------------------------------
# URLs, pagination and category resolution
# ---------------------------------------------------------------------------

def check_url_building():
    import product_parser as P
    equal("page_url REPLACES ?page rather than appending",
          P.page_url("https://www.bbb.org/search?find_text=x&page=1", 4),
          "https://www.bbb.org/search?find_text=x&page=4")
    check("page_url preserves the other query parameters",
          "find_text=x" in P.page_url("https://www.bbb.org/search?find_text=x", 2))
    equal("page_url keeps ?page on page 1 too",
          P.page_url("https://www.bbb.org/search?find_text=x", 1),
          "https://www.bbb.org/search?find_text=x&page=1")

    built = P.api_url(text="restaurants", location="New York, NY", page=3,
                      sort="a-z")
    check("api_url points at the endpoint", "/api/search?" in built)
    check("api_url translates our sort spelling to BBB's", "sort=AToZ" in built)
    check("api_url carries the page", "page=3" in built)
    try:
        P.api_url(text="x", sort="nonsense")
        check("api_url refuses an unknown sort", False)
    except ValueError:
        check("api_url refuses an unknown sort", True)
    try:
        P.api_url(text="x", country="DEU")
        check("api_url refuses a country BBB does not serve", False)
    except ValueError:
        check("api_url refuses a country BBB does not serve", True)


def check_category_filter_refuses_empty_text():
    """Measured: find_type=Category with an empty find_text answers 200 with
    totalResults 0, while the same filter with the label answers 234,844."""
    import product_parser as P
    try:
        P.api_url(category_id="50544-000", text="")
        check("api_url refuses a category filter with no text", False)
    except ValueError as e:
        check("api_url refuses a category filter with no text",
              "find_text" in str(e))
    built = P.api_url(text="Restaurants", category_id="50544-000")
    check("api_url sends find_type AND find_id together",
          "find_type=Category" in built and "find_id=50544-000" in built)


def check_category_resolution_refuses_rather_than_guesses():
    import product_parser as P
    options = P.category_options(LISTING_PAYLOAD_JSON)
    check("category options are read from the site's own response",
          len(options) >= 3, "%r" % options)
    labels = {o["label"] for o in options}
    check("the fixture's option list names Restaurants",
          "Restaurants" in labels, "%r" % sorted(labels))

    exact, was_exact = P.pick_category_option(options, "restaurants")
    equal("category: exact slug match", (exact["label"], was_exact),
          ("Restaurants", True))
    equal("category: hyphenated slug matches its label",
          P.pick_category_option(options, "cleaning-services")[0]["label"],
          "Cleaning Services")
    plural, was_exact = P.pick_category_option(
        [{"value": "10113-000", "label": "Plumber"}], "plumbers")
    equal("category: a plural slug matches on the prefix rule",
          (plural["label"], was_exact), ("Plumber", False))
    nothing, _ = P.pick_category_option(options, "nonexistent-widget")
    equal("category: nothing plausible REFUSES rather than taking the first",
          nothing, None)
    equal("category: an empty option list refuses too",
          P.pick_category_option([], "restaurants"), (None, False))


def check_pagination_is_planned_from_the_sites_own_number():
    """BBB caps totalPages at 15 and answers page 16 with HTTP 500, so a run
    must never ask for the page that errors."""
    import product_parser as P
    import page_flow
    equal("50 pages requested, 15 available -> 15", P.pages_to_fetch(50, 15), 15)
    equal("3 requested, 15 available -> 3", P.pages_to_fetch(3, 15), 3)
    equal("50 requested, site said nothing -> the MAX_PAGES backstop",
          P.pages_to_fetch(50, None), P.MAX_PAGES)
    equal("a short listing caps at its own length", P.pages_to_fetch(10, 2), 2)
    equal("never below 1", P.pages_to_fetch(0, 15), 1)
    equal("page_flow agrees with the parser, on every input",
          [page_flow.pages_to_plan(a, b) for a, b in
           ((50, 15), (3, 15), (50, None), (10, 2), (0, 15))],
          [P.pages_to_fetch(a, b) for a, b in
           ((50, 15), (3, 15), (50, None), (10, 2), (0, 15))])
    equal("MAX_PAGES is the measured 15", P.MAX_PAGES, 15)
    equal("PAGE_SIZE is the measured 15", P.PAGE_SIZE, 15)


def check_supported_urls_are_refused_with_a_reason():
    import product_parser as P
    ok, why = P.is_supported_url("https://www.bbb.org/us/category/restaurants")
    equal("a real BBB URL is supported", (ok, why), (True, ""))
    check("bbb.org without www. is supported too",
          P.is_supported_url("https://bbb.org/search?find_text=x")[0])
    ok, why = P.is_supported_url("https://www.example.com/x")
    check("another host is refused", not ok)
    check("and the refusal EXPLAINS rather than saying 'not a BBB site'",
          "country in the path" in why, why)
    check("a non-URL is refused", not P.is_supported_url("not a url")[0])
    check("a non-http scheme is refused",
          not P.is_supported_url("ftp://www.bbb.org/x")[0])


def check_path_shapes():
    import product_parser as P
    parts = P.profile_parts(
        "https://www.bbb.org/us/ny/bronx/profile/cleaning-services/"
        "proclean-maintenance-systems-inc-0121-134716")
    check("profile_parts recognises a profile URL", parts is not None)
    equal("profile_parts reads the country", parts["country"], "USA")
    equal("profile_parts reads the city", parts["city"], "bronx")
    equal("a category URL is NOT a profile",
          P.profile_parts("https://www.bbb.org/us/category/restaurants"), None)
    equal("category_from_url reads the slug",
          P.category_from_url("https://www.bbb.org/us/category/restaurants"),
          "restaurants")
    equal("a search URL has no category slug",
          P.category_from_url("https://www.bbb.org/search?find_text=x"), None)


def check_listing_url_translation():
    import product_parser as P
    translated = P.api_url_from_listing_url(
        "https://www.bbb.org/search?find_country=USA&find_text=restaurants"
        "&find_loc=New+York%2C+NY&page=2", sort="a-z")
    check("a search URL translates to the endpoint, query intact",
          "find_text=restaurants" in translated and "page=2" in translated,
          translated)
    try:
        P.api_url_from_listing_url("https://www.bbb.org/us/category/restaurants")
        check("a category URL refuses to translate without its tobId", False)
    except ValueError as e:
        check("a category URL refuses to translate without its tobId",
              "tobId" in str(e))
    with_id = P.api_url_from_listing_url(
        "https://www.bbb.org/us/category/restaurants", sort="a-z",
        category_id="50544-000", category_label="Restaurants")
    check("with the tobId it builds the filtered query",
          "find_id=50544-000" in with_id and "find_text=Restaurants" in with_id)


# ---------------------------------------------------------------------------
# What BBB answered with
# ---------------------------------------------------------------------------

def check_page_states_on_real_captures():
    import product_parser as P
    equal("a Managed Challenge is a CHALLENGE",
          P.detect_page_state(CHALLENGE_HTML, 403), "challenge")
    equal("the hard refusal is BLOCKED",
          P.detect_page_state(HARD_BLOCK_HTML, 403), "blocked")
    equal("a page BBB served but that is not a listing is UNKNOWN",
          P.detect_page_state(SERVED_404_HTML, 404), "unknown")
    equal("the listing payload is CONTENT",
          P.detect_page_state(LISTING_PAYLOAD_JSON, 200), "content")
    equal("a profile page is CONTENT too",
          P.detect_page_state(PROFILE_STATE_JSON, 200), "content")
    equal("an empty result set is EMPTY",
          P.detect_page_state(EMPTY_PAYLOAD_JSON, 200), "empty")
    equal("a bare 403 with no markers is blocked",
          P.detect_page_state("", 403), "blocked")


def check_markers_do_not_match_a_page_bbb_serves():
    """CLAUDE.md §18: a marker that matches every page is WORSE than none.

    This check was once passing for the WRONG REASON, and that is why it now
    runs against two different kinds of served page.

    It originally used BBB's 404 only — fetched through plain curl — which
    carries `challenge-platform` and `cdn-cgi` (so those two are correctly
    excluded) but nothing else. Meanwhile `cf-turnstile` was in the marker
    set and fired on **five of five** pages fetched through the 2Captcha
    Scraping Browser, because that product's auto-solve extension injects its
    own hunters into every page it loads. A good page therefore named a
    vendor, and only the signal ORDERING in detect_page_state (payload first)
    kept it from being reported as a challenge.

    So the fixture that matters is a page BBB served THROUGH the Scraping
    Browser, extension injections and all.
    """
    import product_parser as P
    for label, page in (("404 (plain curl)", SERVED_404_HTML),
                        ("listing (via Scraping Browser)",
                         SERVED_VIA_SCRAPING_BROWSER_HTML)):
        for marker in P.BOT_CHALLENGE_MARKERS:
            check("marker %r does not appear on a served %s" % (marker, label),
                  marker not in page)
        check("no vendor is named on a served %s" % label,
              P.detect_bot_challenge(page) is None,
              "got %r" % P.detect_bot_challenge(page))

    # The fixture has to actually CARRY the dangerous content, or this check
    # proves nothing. Pinned so a future re-capture cannot quietly drop it.
    check("the Scraping Browser fixture carries the auto-solve extension",
          "kjmkgkdkpedkejedfhmfcenooemhbpbo" in SERVED_VIA_SCRAPING_BROWSER_HTML)
    check("...including its cf-turnstile-response input",
          "cf-turnstile" in SERVED_VIA_SCRAPING_BROWSER_HTML)

    for banned in ("challenge-platform", "cdn-cgi", "cf-turnstile"):
        check("%r is NOT in the marker set" % banned,
              not any(banned in m for m in P.BOT_CHALLENGE_MARKERS))
    # ...and each one really does appear on a page BBB served, or excluding it
    # would be a precaution against nothing.
    for banned, page in (("challenge-platform", SERVED_404_HTML),
                         ("cdn-cgi", SERVED_404_HTML),
                         ("cf-turnstile", SERVED_VIA_SCRAPING_BROWSER_HTML)):
        check("...and %r really does appear on a served page" % banned,
              banned in page)

    # Every marker must fire on at least one real refusal, or it is dead
    # weight. `/turnstile/v0/api.js` was in this list and matched nothing at
    # all across every capture (§17).
    for marker in P.BOT_CHALLENGE_MARKERS:
        check("marker %r fires on a real challenge" % marker,
              marker in CHALLENGE_HTML,
              "matches nothing — dead weight")

    equal("the challenge fixture names its vendor",
          P.detect_bot_challenge(CHALLENGE_HTML), "cloudflare (cf_chl_opt)")
    equal("the HARD block names no vendor — it carries no widget",
          P.detect_bot_challenge(HARD_BLOCK_HTML), None)


def check_positive_asset_detection():
    """A page BBB served is built out of BBB's own assets; an interstitial is
    not. §8's assets.mmsrg.com trick, on a third site."""
    import product_parser as P
    check("the served page references BBB's asset hosts",
          P.references_own_assets(SERVED_404_HTML) >= 2)
    equal("the challenge references none",
          P.references_own_assets(CHALLENGE_HTML), 0)
    equal("the hard block references none",
          P.references_own_assets(HARD_BLOCK_HTML), 0)
    check("both titles wear BBB's branding, which is why a title check fails",
          "Better Business Bureau" in HARD_BLOCK_HTML)


def check_state_policy():
    import page_flow
    equal("every state has a policy",
          sorted(page_flow.STATE_POLICY),
          ["blocked", "challenge", "content", "empty", "unknown"])
    check("content: parsed, not retried, not blocked",
          page_flow.should_parse("content")
          and not page_flow.should_retry("content")
          and not page_flow.counts_as_blocked("content"))
    check("challenge: retried, SOLVED, counts as blocked",
          page_flow.should_retry("challenge")
          and page_flow.should_solve("challenge")
          and page_flow.counts_as_blocked("challenge"))
    check("blocked: retried, NEVER solved — there is no widget to solve",
          page_flow.should_retry("blocked")
          and not page_flow.should_solve("blocked")
          and page_flow.counts_as_blocked("blocked"))
    check("unknown: retried, not solved, NOT blocked",
          page_flow.should_retry("unknown")
          and not page_flow.should_solve("unknown")
          and not page_flow.counts_as_blocked("unknown"))
    check("an unrecognised state falls back to unknown's policy",
          page_flow.should_retry("something-new")
          and not page_flow.should_solve("something-new"))
    equal("at most one solve per page", page_flow.SOLVES_PER_PAGE, 1)


def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code.

    `RETRY_ON_BLOCKED` carried a paragraph of justification in a sibling repo
    and no engine consulted it, so setting it False changed nothing.
    """
    import page_flow
    sources = []
    for name in ("playwright_scraper.py", "selenium_scraper.py",
                 "puppeteer_scraper.py", "scraper_api_client.py"):
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            sources.append(open(path, encoding="utf-8").read())
    joined = "\n".join(sources)
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                     "SOLVES_PER_PAGE"):
        check("page_flow.%s is CONSULTED by an engine" % constant,
              constant in joined,
              "defined in page_flow and read by nothing")
    for fn in ("pages_to_plan", "ready_selector", "min_matches",
               "content_timeout_ms", "wait_for_count", "classify",
               "should_retry", "should_solve", "counts_as_blocked",
               "should_parse", "concurrency_limit",
               "pagination_is_addressable"):
        check("page_flow.%s has a caller outside its own module" % fn,
              fn in joined, "unused policy")


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

def check_row_schema():
    from output_writer import Business, ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES
    names = [f.name for f in fields(Business)]
    equal("the family prefix is byte-identical and in order (§9)",
          names[:5], ["source", "scraped_at", "url", "sku", "title"])
    for gone in ("price", "currency", "original_price", "discount_pct",
                 "in_stock", "brand"):
        check("the commerce column %r is absent, not null-forever" % gone,
              gone not in names)
    check("`rating` is absent — BBB's is a letter plus a 0-100 score",
          "rating" not in names)
    check("...and both of its real names are present",
          "rating_grade" in names and "rating_score" in names)
    equal("every mode maps to a row class",
          sorted(ROW_CLASS_BY_MODE), ["category", "profile", "search"])
    equal("every mode is one row per sku",
          sorted(UNIQUE_BY_SKU_MODES), ["category", "profile", "search"])
    equal("all three modes share one class",
          len({c for c in ROW_CLASS_BY_MODE.values()}), 1)


def check_csv_and_json_writers():
    from output_writer import Business, write_csv, write_json
    import product_parser as P
    rows = P.parse_listing(LISTING_PAYLOAD_JSON).rows
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(rows, csv_path, row_cls=Business)
        with open(csv_path, encoding="utf-8") as f:
            reader = list(csv.reader(f))
        equal("CSV header matches the dataclass, in order",
              reader[0], [f.name for f in fields(Business)])
        equal("CSV holds every row", len(reader) - 1, len(rows))
        phone_col = reader[0].index("phone")
        check("a list column is joined readably rather than repr()'d",
              " | " in reader[1][phone_col] or reader[1][phone_col].count("(") <= 1,
              reader[1][phone_col])
        check("no Python list repr leaked into the CSV",
              not any(cell.startswith("[") for row in reader[1:] for cell in row))

        empty_csv = os.path.join(tmp, "empty.csv")
        write_csv([], empty_csv, row_cls=Business)
        with open(empty_csv, encoding="utf-8") as f:
            header = list(csv.reader(f))
        equal("an EMPTY csv still carries its header", len(header), 1)
        equal("...and it is the right one", header[0],
              [f.name for f in fields(Business)])

        json_path = os.path.join(tmp, "out.json")
        write_json(rows, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        equal("JSON holds every row", len(loaded), len(rows))
        equal("JSON keys are the dataclass fields, in order",
              list(loaded[0].keys()), [f.name for f in fields(Business)])
        check("a list column stays a real list in JSON",
              isinstance(loaded[0]["phone"], list))


def check_exit_codes():
    import output_writer as O
    equal("0 ok / 1 crash / 2 usage / 3 blocked / 4 empty / 5 api / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_API_ERROR, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    check("page_cap_reached is a COMPLETE stop reason",
          "page_cap_reached" in O.COMPLETE_STOP_REASONS)
    check("single_page_mode is complete by construction",
          "single_page_mode" in O.COMPLETE_STOP_REASONS)
    check("no_new_products is complete",
          "no_new_products" in O.COMPLETE_STOP_REASONS)


def check_a_run_that_finds_nothing_writes_nothing():
    """Never replace last night's good output with []."""
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        code = save([], prefix, "json", allow_empty=False)
        equal("an empty run exits 4", code, 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(),
              '[{"sku": "yesterday"}]')
        code = save([], prefix, "json", allow_empty=True)
        equal("--allow-empty WRITES the empty file...", 
              json.load(open(prefix + ".json", encoding="utf-8")), [])
        # ...and still reports exit 4. Pinned deliberately (§10: pin a known
        # behaviour rather than half-guarding it): "zero businesses" is true
        # whether or not the file was written, and a caller that wanted the
        # file still wants to know the result was empty.
        equal("...and still reports exit 4, because it IS empty", code, 4)


def check_page_and_position_are_unique_across_pages():
    """One line, and the column is worthless without it: `position` restarts
    at 1 on every page."""
    import product_parser as P
    page1 = P.parse_listing(LISTING_PAYLOAD_JSON, page=1, mode="search").rows
    page2 = P.parse_listing(LISTING_PAYLOAD_JSON, page=2, mode="search").rows
    pairs = [(r.page, r.position) for r in page1 + page2]
    equal("page+position is unique across a multi-page run",
          len(set(pairs)), len(pairs))
    equal("page 2's rows really say page 2",
          sorted({r.page for r in page2}), [2])


def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="page_cap_reached",
                    pages_requested=50, pages_completed=15, pages_failed=[],
                    products=225, mode="search", source="bbb.org",
                    start_url="https://www.bbb.org/search?find_text=x",
                    final_url="https://www.bbb.org/api/search?find_text=x",
                    extra={"total_results": 19016, "pages_available": 15,
                           "capped_by_site": True, "reachable_max": 225})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source"):
        check("the sidecar records %r" % key, key in meta)
    equal("the sidecar carries BBB's own total", meta["total_results"], 19016)
    check("...and says the run was capped BY THE SITE", meta["capped_by_site"])
    equal("pages_failed is a LIST of numbers, not a count",
          isinstance(meta["pages_failed"], list), True)


# ---------------------------------------------------------------------------
# The engines — the five checks CLAUDE.md §17 says to steal
# ---------------------------------------------------------------------------

ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
DRIVER_IMPORTS = {
    "playwright_scraper": "playwright",
    "selenium_scraper": "selenium",
    "puppeteer_scraper": "pyppeteer",
}


def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None


def check_engines_import_their_driver_at_module_level():
    """For the guarded imports above to MEAN anything.

    A sibling repo imported `launch`/`connect` inside the launch path, so the
    module imported cleanly with no pyppeteer installed: the group never
    skipped, and the CI job that exists to fail on unexpected skips could not
    have caught a broken import. It also let CI run against a stub version
    for a while without anything noticing. This drifts back silently, so it
    is asserted with an `ast` walk rather than trusted.
    """
    for module, driver in DRIVER_IMPORTS.items():
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            check("%s exists" % module, False)
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:          # module level ONLY
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver),
              driver in top_level,
              "top-level imports: %s" % sorted(top_level))


def check_shared_calls_bind_against_the_real_signature():
    """§17's check #1, and the one that earns its keep.

    A sibling repo shipped `classify(html, url=…)` in two of three engines
    against a callee taking `status` second, and BOTH crashed on their first
    fetch — invisible to import, --help, compileall, the undefined-name walk
    and 400+ green assertions, because none of those calls a function the way
    a live run does.

    This walks every engine's AST for calls into the shared modules and binds
    each one against the callee's real signature.
    """
    import page_flow
    import product_parser
    import output_writer
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer}
    bound = 0
    for module in ENGINES + ("scraper_api_client",):
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        # Which shared names this file imported directly (`from x import y`).
        direct = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (
                        targets[node.module], alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            owner = attr = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in targets:
                    owner, attr = targets[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in direct:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            # A name that is NOT THERE is the loudest possible failure and
            # this check used to swallow it: `getattr(..., None)` returned
            # None, `not callable(None)` was true, and the call was skipped.
            # Three calls into a page_flow API that does not exist in this
            # repo -- comparable(), next_page_selector(),
            # next_page_candidates(), all of them Tokopedia's, all arriving
            # with copied code -- sat in two engines under a green run of
            # this very function. Absent is not "nothing to bind".
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)"
                      % (getattr(owner, "__name__", owner), attr,
                         module + ".py", node.lineno),
                      False,
                      "the engine calls a name the shared module does not "
                      "define; a live run reaches this as AttributeError")
                continue
            callee = getattr(owner, attr)
            if not callable(callee) or inspect.isclass(callee):
                continue
            try:
                signature = inspect.signature(callee)
            except (TypeError, ValueError):
                continue
            positional = [inspect.Parameter.empty] * len(node.args)
            keywords = {}
            for kw in node.keywords:
                if kw.arg is None:          # **kwargs — cannot be checked here
                    keywords = None
                    break
                keywords[kw.arg] = inspect.Parameter.empty
            if keywords is None:
                continue
            try:
                signature.bind(*positional, **keywords)
                bound += 1
            except TypeError as e:
                check("%s:%d %s.%s(...) binds against its real signature"
                      % (module, node.lineno, owner.__name__, attr),
                      False, "%s; signature is %s" % (e, signature))
    check("every shared-module call in every engine binds (%d checked)" % bound,
          bound > 40, "only %d calls were checked — is the walk finding them?"
          % bound)


def _argparse_flags(module_name):
    """Every --flag a module's parser defines, without running the CLI."""
    path = os.path.join(HERE, module_name + ".py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    # Only calls on the argparse parser itself. A browser's option object
    # also has `add_argument`, and counting Chrome's own switches
    # (`--no-sandbox`, `--window-size=…`) as CLI flags made this check
    # compare nonsense.
    parsers = {"p"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("add_argument_group",
                                             "add_mutually_exclusive_group")):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    parsers.add(target.id)
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parsers):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.startswith("--"):
                    flags.add(arg.value)
    return flags


# The family's flag contract (CLAUDE.md §9), plus this repo's own additions.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html",
}
BBB_FLAGS = {"--mode", "--text", "--location", "--country", "--sort", "--state",
             "--locale"}


def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both ways.

    A missing flag fails; so does closing a difference the README documents.
    """
    sets = {}
    for module in ENGINES:
        if not os.path.exists(os.path.join(HERE, module + ".py")):
            continue
        sets[module] = _argparse_flags(module)
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | BBB_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
    # The ONE documented difference: pyppeteer downloads its own Chromium
    # and could not launch it on the development machine, so it needs a way
    # to point at another one. Its twins have no equivalent because they do
    # not ship a browser. Listed here so that closing the difference — or
    # growing a second one — fails the build (§17).
    DOCUMENTED_DIFFERENCES = {"puppeteer_scraper": {"--chromium-path"}}
    names = sorted(sets)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
        only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
        check("%s and %s define the same flags" % (a, b),
              not only_a and not only_b,
              "only in %s: %s; only in %s: %s"
              % (a, sorted(only_a), b, sorted(only_b)))


BANNED_FLAGS = ("--antidetect", "--country-code")


def check_banned_and_removed_flags():
    """Scoped to the ENGINES.

    `--country` is legitimate here and NOT banned — BBB serves both its
    countries from one host, so the flag cannot contradict a hostname the way
    CLAUDE.md §10's rule is about. What the rule is really about is a flag
    disagreeing with the URL, and that is enforced instead: passing --country
    together with --url is refused, which the next check asserts.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        for flag in BANNED_FLAGS:
            check("%s does not define %s" % (module, flag),
                  '"%s"' % flag not in source)
        check("%s refuses --country alongside --url" % module,
              "--url already carries the whole query" in source,
              "the refusal that makes --country safe here is missing")


def check_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE.

    A live run of a sibling repo's pyppeteer engine died with NameError on a
    line reached only while fetching, after an import had been removed — the
    module imported cleanly, --help worked, compileall passed and CI was
    green. Kept COARSE (pooled bindings, no scope tracking) so it
    under-reports rather than inventing problems.
    """
    import builtins
    modules = [f for f in sorted(os.listdir(HERE))
               if f.endswith(".py") and f != "smoke_test.py"]
    for filename in modules:
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        # Module-level dunders exist without being assigned anywhere.
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.alias) and node.asname:
                defined.add(node.asname)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % filename, not unresolved,
              "%s" % unresolved)


def _import_graph(entrypoint):
    """Every local module an entrypoint reaches, transitively."""
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, queue = set(), [entrypoint]
    while queue:
        name = queue.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(open(os.path.join(HERE, name + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                queue.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                queue.append(node.module.split(".")[0])
    return seen


def check_dockerfile_copies_everything_the_entrypoint_imports():
    """§10: all three repos in this family shipped an image that died with
    ModuleNotFoundError on every invocation, --help included, because
    proxy_pool.py was missing from the COPY list. CI never built the image;
    this check needs no Docker."""
    path = os.path.join(HERE, "Dockerfile")
    if not os.path.exists(path):
        check("Dockerfile exists", False)
        return
    dockerfile = open(path, encoding="utf-8").read()
    # Only the COPY instructions, continuations included — a comment above
    # them naming a file is not a file the image carries.
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    entry = re.search(r'(?:CMD|ENTRYPOINT)\s*\[?\s*"?(?:python3?"?,\s*"?)?'
                      r'([A-Za-z_][A-Za-z0-9_]*)\.py', dockerfile)
    entrypoint = entry.group(1) if entry else "playwright_scraper"
    needed = _import_graph(entrypoint)
    missing = sorted(needed - copied)
    check("the Dockerfile COPYs every module %s.py imports" % entrypoint,
          not missing, "missing %s" % missing)
    for unwanted in ("smoke_test", "test_smoke"):
        check("the image does not carry %s.py" % unwanted,
              unwanted not in copied)


def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    path = os.path.join(HERE, ".env.example")
    if not os.path.exists(path):
        check(".env.example exists", False)
        return
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", 
                                open(path, encoding="utf-8").read(), re.M))
    read = set(env_config.ENV_KEYS)
    check("every variable the loader reads is documented",
          not (read - documented), "undocumented: %s" % sorted(read - documented))
    check("every documented variable is actually read",
          not (documented - read), "unread: %s" % sorted(documented - read))


def check_a_copied_env_example_reads_as_UNSET():
    """§17: `cp .env.example .env` followed by a run must not connect.

    The placeholder check was a literal set in a sibling repo, and the two
    credentialled URLs are documented the way the vendor documents them —
    `ws://{login}-zone-…:{password}@cb.2captcha.com:9222` — so neither
    literal matched, the run connected with the string `{login}-zone-…` as
    its username, and got a 401 a long way from its cause.
    """
    import env_config
    example = os.path.join(HERE, ".env.example")
    if not os.path.exists(example):
        check(".env.example exists", False)
        return
    text = open(example, encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    check("the example actually sets every variable",
          set(values) == set(env_config.ENV_KEYS),
          "example has %s, loader reads %s"
          % (sorted(values), sorted(env_config.ENV_KEYS)))
    # Every CREDENTIAL must read as unset. The default TARGET must not: it is
    # a real, usable URL, and blanking it would remove the one setting this
    # file exists to make convenient (§17's check #3 says exactly this — the
    # credentials unset, the non-credential default still usable).
    CREDENTIALS = {"TWOCAPTCHA_KEY", "BBB_CDP_ENDPOINT", "BBB_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in CREDENTIALS:
                check("a copied .env.example leaves %s unset" % name,
                      got is None, "got %r" % got)
            else:
                check("...while %s stays a usable default" % name,
                      got == raw.strip(), "got %r" % got)
    finally:
        os.environ.clear()
        os.environ.update(before)
    # And the counter-check: a real credential must still come through, or
    # the placeholder rule would have made the loader useless. Deliberately
    # NOT 32 hex characters — that is the shape of a real 2captcha key, and
    # this repo's own credential scan (rightly) fails on one.
    try:
        os.environ["TWOCAPTCHA_KEY"] = "not-a-real-key-but-a-real-value"
        equal("a real value is still read",
              env_config.env_value("TWOCAPTCHA_KEY"),
              "not-a-real-key-but-a-real-value")
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_credential_scan_is_one_implementation_invoked_from_both():
    """§17: two sources of truth, one dead and one holed.

    `.github/ci_checks.py` sat in three repos invoked by NOTHING, while
    tests.yml carried an inline grep doing a narrower version of the same job
    — one that matched only ws:// and wss://, so an http://user:pass@
    credential would have sailed past CI.
    """
    script = os.path.join(HERE, ".github", "ci_checks.py")
    check("the credential scan exists as a script", os.path.exists(script))
    if not os.path.exists(script):
        return
    workflow = os.path.join(HERE, ".github", "workflows", "tests.yml")
    if os.path.exists(workflow):
        text = open(workflow, encoding="utf-8").read()
        check("CI INVOKES the script rather than reimplementing it",
              "ci_checks.py" in text)
    result = subprocess.run([sys.executable, script, "--all"], cwd=HERE,
                            capture_output=True, text=True)
    check("the credential scan passes on this repo's own tree",
          result.returncode == 0,
          (result.stdout + result.stderr)[-600:])


BANNED_WORDING = (
    "cloud browser", "antidetect browser", "2scraper Antidetect Browser",
    "gate.2prx.com", "ANTIDETECT_LOCAL_API",
)


def check_banned_wording():
    """§12: enforced by this test rather than by review."""
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", ".pytest_cache", "node_modules")]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            for phrase in BANNED_WORDING:
                if phrase.lower() in text and filename != "smoke_test.py":
                    check("%s contains no %r" % (
                        os.path.relpath(path, HERE), phrase), False)
    check("banned-wording scan ran", True)


def check_concurrency_with_the_browser_stubbed():
    """§10: a live run cannot always reach this machinery.

    Page 1 is fetched alone and decides how many pages there are, so a
    blocked page 1 means the workers never start. Driven directly instead,
    with the browser replaced.
    """
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return

    class Args:
        delay = 0
        retries = 1
        retry_delay = 0
        out = "unused"
        mode = "search"
        sort = "a-z"
        pages = 50

    fetched = []
    import threading
    lock = threading.Lock()

    def fake_fetch(session, args, pool, page_num, url):
        with lock:
            fetched.append(page_num)
        outcome = engine.PageOutcome(page_num=page_num, url=url)
        # Page 6 is the end of this listing: no rows, but a served page.
        outcome.products = [] if page_num >= 6 else [object()] * 15
        outcome.state = "empty" if page_num >= 6 else "content"
        return outcome

    class FakeSession:
        def __init__(self, *a, **k):
            self.pool = None
        def open(self):
            return self
        def close(self):
            pass

    class FakePlaywright:
        def __enter__(self):
            return None
        def __exit__(self, *a):
            return False

    real_fetch = engine._fetch_one_page
    real_session = engine._BrowserSession
    real_pw = engine.sync_playwright
    engine._fetch_one_page = fake_fetch
    engine._BrowserSession = FakeSession
    engine.sync_playwright = lambda: FakePlaywright()
    try:
        specs = [(n, "u%d" % n) for n in range(2, 51)]
        results, unattempted, exhausted = engine._fetch_pages_concurrently(
            Args(), None, specs, 4)
    finally:
        engine._fetch_one_page = real_fetch
        engine._BrowserSession = real_session
        engine.sync_playwright = real_pw

    check("every page fetched was fetched exactly once",
          len(fetched) == len(set(fetched)), "%r" % sorted(fetched))
    check("dispatch STOPPED at the end of the listing", exhausted)
    check("...so the 49 queued pages cost far fewer fetches",
          len(fetched) < 15, "fetched %d of 49" % len(fetched))
    check("unattempted pages are REPORTED, not counted as failed",
          len(unattempted) > 0 and all(isinstance(n, int) for n in unattempted))
    equal("attempted + unattempted covers the whole queue",
          len(set(fetched)) + len(unattempted), 49)
    equal("outcomes are restorable to page order",
          [o.page_num for o in sorted(results, key=lambda o: o.page_num)],
          sorted(o.page_num for o in results))


def check_a_dead_worker_neither_hangs_nor_loses_its_siblings():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return

    class Args:
        delay = 0
        retries = 1
        retry_delay = 0
        out = "unused"
        mode = "search"
        sort = "a-z"
        pages = 10

    def exploding_fetch(session, args, pool, page_num, url):
        if page_num == 3:
            raise RuntimeError("worker died")
        outcome = engine.PageOutcome(page_num=page_num, url=url)
        outcome.products = [object()] * 15
        outcome.state = "content"
        return outcome

    class FakeSession:
        def __init__(self, *a, **k):
            self.pool = None
        def open(self):
            return self
        def close(self):
            pass

    class FakePlaywright:
        def __enter__(self):
            return None
        def __exit__(self, *a):
            return False

    real_fetch, real_session, real_pw = (engine._fetch_one_page,
                                         engine._BrowserSession,
                                         engine.sync_playwright)
    engine._fetch_one_page = exploding_fetch
    engine._BrowserSession = FakeSession
    engine.sync_playwright = lambda: FakePlaywright()
    try:
        specs = [(n, "u%d" % n) for n in range(2, 8)]
        results, unattempted, exhausted = engine._fetch_pages_concurrently(
            Args(), None, specs, 3)
    finally:
        engine._fetch_one_page = real_fetch
        engine._BrowserSession = real_session
        engine.sync_playwright = real_pw

    check("the run returned rather than hanging", True)
    check("the dead worker's siblings still delivered their pages",
          len(results) >= 3, "%d results" % len(results))
    check("page 3 is not reported as a success",
          3 not in [o.page_num for o in results])


def check_worker_pools_start_on_different_exits():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    firsts = [engine._worker_pool(pool, i).current for i in range(3)]
    equal("three workers start on three different exits",
          len(set(firsts)), 3)
    equal("a missing pool stays missing", engine._worker_pool(None, 0), None)


def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    """§10: an unknown key in new_context(**kwargs) is a TypeError at launch,
    on the PAID path, at runtime."""
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    try:
        from fingerprint_client import playwright_context_kwargs
    except ImportError as e:
        skip("fingerprint", str(e))
        return
    sample = {"id": "x", "country": "US",
              "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US",
              "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    from playwright.sync_api import sync_playwright  # noqa: F401
    import playwright.sync_api as pw_api
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown,
          "unknown: %s" % unknown)


def check_every_engine_exposes_the_same_public_surface():
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        for name in ("scrape", "parse_args", "PageOutcome", "_fetch_one_page",
                     "_parse_for_mode", "_target_url"):
            check("%s.%s exists" % (module, name), hasattr(engine, name))
        outcome = engine.PageOutcome(page_num=1, url="u")
        for field_name in ("state", "total_available", "pages_available",
                           "sort_applied", "products", "blocked_by",
                           "load_failed", "final_url"):
            check("%s.PageOutcome carries %r" % (module, field_name),
                  hasattr(outcome, field_name))
        check("%s.PageOutcome.ok is True for a fresh outcome" % module,
              outcome.ok)
        equal("%s shares CORE_FIELDS with its twins" % module,
              tuple(engine.CORE_FIELDS),
              ("title", "url", "sku", "category", "latitude"))
        equal("%s shares CORE_FIELD_FLOOR with its twins" % module,
              engine.CORE_FIELD_FLOOR, 99)


def check_engines_do_not_evaluate_a_string_in_the_browser():
    """§18: a site whose CSP omits `unsafe-eval` kills wait_for_function with
    an EvalError and takes the run down with exit 1. BBB has not been
    measured for that, and the cheap habit costs nothing where it would have
    been allowed."""
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor"):
            check("%s never CALLS %s" % (module, banned), banned not in called,
                  "poll through page_flow.wait_for_count instead")


def check_credentials_never_reach_a_log():
    """§8: an EXCEPTION MESSAGE is a log, and the masker must be GLOBAL.

    A Playwright connection error repeats the endpoint five times (the
    message plus a four-line call log), so a masker handling only the first
    occurrence prints the password four times and looks like it is working.
    """
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module,
              "supersecret" not in masked, masked)
        check("%s keeps the host and port, which are the useful half" % module,
              "h1:9222" in masked and "h2:9222" in masked, masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)


def check_sample_output_matches_the_schema():
    from output_writer import Business
    expected = [f.name for f in fields(Business)]
    json_path = os.path.join(HERE, "sample_output.json")
    csv_path = os.path.join(HERE, "sample_output.csv")
    if not os.path.exists(json_path):
        check("sample_output.json exists", False)
        return
    rows = json.load(open(json_path, encoding="utf-8"))
    check("sample_output.json holds rows", bool(rows))
    equal("sample_output.json keys match the schema, in order",
          list(rows[0].keys()), expected)
    check("sample_output.json is from a real run (bbb.org rows)",
          all(r["source"] == "bbb.org" for r in rows))
    check("...and carries no fabrication markers",
          not any("example" in (r.get("url") or "").lower() or
                  "lorem" in (r.get("title") or "").lower() for r in rows))
    if os.path.exists(csv_path):
        header = next(csv.reader(open(csv_path, encoding="utf-8")))
        equal("sample_output.csv header matches the schema", header, expected)


def check_readme_numbers_are_not_stale():
    """§17's check #4: diff every numeric claim against what is on disk.

    Only the figures that MUST hold are pinned: a number that legitimately
    varies between runs is written as a range in the README and not checked
    here.
    """
    path = os.path.join(HERE, "README.md")
    if not os.path.exists(path):
        check("README.md exists", False)
        return
    readme = open(path, encoding="utf-8").read()
    import product_parser as P
    if "15 pages" in readme or "15 page" in readme:
        equal("the README's page cap matches MAX_PAGES", P.MAX_PAGES, 15)
    if "225" in readme:
        equal("the README's 225 is pages x pageSize",
              P.MAX_PAGES * P.PAGE_SIZE, 225)
    from output_writer import Business
    column_count = len(fields(Business))
    claimed = re.findall(r"(\d+)\s+columns", readme)
    for number in claimed:
        equal("the README's column count matches the schema",
              int(number), column_count)


_TREE_BEFORE = None


def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines()
                  if not line.endswith(".pyc"))


def check_no_test_mutates_the_working_tree():
    """§10: one suite used its own file as a fake chromedriver and chmod'd it
    to 755, leaving a mode change in git status.

    Compares the tree against how it looked when the suite STARTED, not
    against a clean checkout — otherwise this is permanently red while
    anyone is editing, and a check that is always red teaches everyone to
    ignore checks.
    """
    if _TREE_BEFORE is None:
        skip("git status", "not a git repository")
        return
    after = _tree_state()
    changed = sorted(set(after) - set(_TREE_BEFORE))
    check("the suite itself changed nothing in the working tree",
          not changed, "%s" % changed)


def check_captcha_capability_claims_match_the_code():
    """§19: the most expensive bug this family can ship is a SENTENCE.

    It fails in both directions and this family has shipped both:

      * saying a captcha CANNOT be solved, when the true statement is that
        THIS REPO does not implement the task type. 2Captcha solves
        enterprise reCAPTCHA and Cloudflare Turnstile and has for years, so
        such a sentence tells a reader not to buy something that works.
      * saying this repo DOES solve something it builds no task type for --
        which is what the README said here: it billed the Managed Challenge
        solve to `--twocaptcha-key`, while the only thing that clears one is
        `Captcha.setAutoSolve` over `--cdp-endpoint`.

    Neither is visible to any other check: nothing fails, nothing crashes,
    and the output is correct.
    """
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    solver = open(os.path.join(HERE, "captcha_solver.py"), encoding="utf-8").read()
    low = readme.lower()

    # Conclusions about the PRODUCT. Phrases about a page carrying no widget
    # are deliberately absent -- BBB's hard block really is one, and calling
    # THAT unsolvable is honest.
    for phrase in ("cannot be solved", "can't be solved", "neither is solvable",
                   "is not solvable", "solver is inapplicable", "no solver can"):
        check("README: no %r -- write 'this repo does not implement X'" % phrase,
              phrase not in low)

    # The positive direction, stated as a PAIRING rather than a keyword
    # search so it cannot go quiet by accident: if the task type is absent,
    # the README has to say so in those words.
    if "TurnstileTaskProxyless" not in solver:
        check("README says plainly that TurnstileTaskProxyless is not built here",
              "does not implement `turnstiletaskproxyless`" in low,
              "the solver builds no Turnstile task, so the README must not let "
              "a reader believe --twocaptcha-key clears a Managed Challenge")
        check("...and the Managed Challenge is not billed to --twocaptcha-key",
              "(`--twocaptcha-key`) - for the managed challenge" not in low
              and "(`--twocaptcha-key`) \u2014 for the managed challenge" not in low)
    else:
        check("a built Turnstile task needs the render interception too",
              "TURNSTILE_INTERCEPT_JS" in solver)

    # Whatever the README credits with clearing the challenge must be a thing
    # the engines actually do.
    if "setautosolve" in low:
        srcs = ""
        for name in ("playwright_scraper.py", "selenium_scraper.py",
                     "puppeteer_scraper.py"):
            path = os.path.join(HERE, name)
            if os.path.exists(path):
                srcs += open(path, encoding="utf-8").read()
        check("README credits Captcha.setAutoSolve, and an engine calls it",
              "Captcha.setAutoSolve" in srcs)
def check_no_statement_is_unreachable():
    """A statement sitting after a return/raise/break/continue in the SAME
    block, which therefore can never run.

    Narrow on purpose: it makes no claim about reachability in general, only
    about a block whose control flow has already left. Measured across the
    eighteen repos of this family on 2026-09-16 it reported six problems and
    zero false positives.

    `check_undefined_names_in_every_module` cannot see this class at all, by
    design -- it pools every binding in the file rather than tracking scopes,
    so a name used inside dead code passes as long as anything else in the
    module binds it. What was hiding in that blind spot here, and in five
    sibling repos, byte for byte: a function whose `def` line had been lost,
    leaving its docstring and body absorbed into the end of the function
    above it. Present since this repo's first commit, invisible to import,
    `--help`, `compileall`, and every green run of this suite.
    """
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename),
                              encoding="utf-8").read())
        dead = []
        for node in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise,
                                         ast.Continue, ast.Break)):
                        dead.append(block[i + 1].lineno)
                        break
        check("%s: no statement the control flow can never reach" % filename,
              not dead, "first at line %d" % min(dead) if dead else "")


def check_x_debug_header_is_redacted():
    """SECURITY.md names the Scraper API's x-debug header as a place
    credentials reach a log unmasked. It was then logged verbatim.

    The fixtures are assembled from pieces rather than written out whole,
    because this file is scanned by the credential check like every other
    and a fixture that LOOKS like a live key fails it. They are the SHAPES a
    credential takes, not the literals this repo happens to contain today.
    """
    try:
        import scraper_api_client as sac
    except ImportError:
        return

    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    check("x-debug: the credential and the key are gone",
                 pw not in out and key not in out)
    check("x-debug: the cost, host and status survive",
                 "cost=0.00145" in out and "cb.2captcha.com:9222" in out
                 and "status=200" in out)

    s1, s2 = "secret" + "one", "secret" + "two"
    two = sac._redact_debug_header(
        "a=http://u1:" + s1 + "@h1:1 b=http://u2:" + s2 + "@h2:2")
    check("x-debug: both credentials are masked, not just the first",
                 s1 not in two and s2 not in two)

    src = inspect.getsource(sac)
    check("x-debug: the log line calls the redactor",
                 'logger.info("x-debug: %s", _redact_debug_header(debug))' in src)



CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")]


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="bbb-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose

    global _TREE_BEFORE
    _TREE_BEFORE = _tree_state()

    for fn in CHECKS:
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()

    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
