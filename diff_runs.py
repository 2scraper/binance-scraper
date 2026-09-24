#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku`.

    python3 diff_runs.py --old restaurants.2026-09-01.json \\
                          --new restaurants.2026-09-07.json

Typical use is a scheduled re-run kept under a dated filename, diffed against
the previous one:

    python3 playwright_scraper.py --text restaurants --location "New York, NY" \\
        --out "restaurants_$(date +%F)"
    python3 diff_runs.py --old "restaurants_$(ls -t restaurants_*.json | sed -n 2p)" \\
                          --new "restaurants_$(date +%F).json" --out diff.json

Four buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new (closed or removed
                   from BBB, or just outside this particular query's 225-row
                   ceiling this time)
  changed        — sku present in both, with a different letter grade,
                   accreditation status, address, phone list, category,
                   website or complaint count. See TRACKED_FIELDS.
  source_changed — sku present in both, but one row came from a LISTING run
                   and the other from a PROFILE run, and they differ on a
                   column only a profile fills. Reported separately because
                   this says something about our own two snapshots rather
                   than about the business — and --fail-on-change
                   deliberately ignores it.

TWO THINGS TO KNOW BEFORE READING A DIFF OF THIS SITE
-----------------------------------------------------
**`removed` does not mean closed.** BBB caps every query at 15 pages of 15,
so a run holds at most 225 of a result set that was 19,016 on one measured
search. A business can leave the file because the ordering shifted rather
than because anything happened to it. Two runs are only comparable as a
CENSUS when both used the same `--sort` and the same query — which is why
`sort` is a column, and why this tool warns when the two files disagree on it.

**`position` and `page` are deliberately not tracked.** BBB's A-Z ordering
does not break a tie between two locations of one business deterministically:
measured 2026-09-16, three engines running the identical query returned the
identical 30 skus with two rows swapped. Diffing position would report churn
on every run.

A row this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

# What is worth watching on a business directory, and nothing else.
#
# A price monitor's fields are absent because BBB has no prices — porting
# them would be dead code that looks load-bearing (CLAUDE.md §4). What
# changes here is a business's STANDING and its CONTACT DETAILS, and those
# are the two things anyone diffs a directory for.
#
# `phone` and `service_areas` are lists and compare element-wise, which is
# what you want: a business adding a second number is a real change.
#
# Deliberately NOT tracked: `position` and `page`. BBB's A-Z ordering does
# not break a tie between two locations of one business deterministically —
# measured 2026-09-16, three engines running the identical query returned the
# identical 30 skus with two rows swapped — so a position diff would report
# churn on every run and teach the reader to ignore the output.
TRACKED_FIELDS = (
    # standing
    "rating_grade", "rating_score", "is_accredited", "out_of_business",
    # what it does and where
    "category", "categories", "address", "city", "state", "postal_code",
    "service_areas",
    # how to reach it
    "phone", "website",
    # only a profile run fills these; see PROFILE_ONLY_FIELDS
    "complaints_total", "complaints_3y", "complaints_12m", "reviews_total",
    "review_stars_avg", "years_in_business", "entity_type", "accredited_since",
)

# The subset that ONLY a profile run populates.
#
# A listing row leaves every one of these null, so diffing a listing run
# against a profile run would report each of them as a change on every row —
# and none of it would be about the business. When the two rows disagree on
# `data_source`, those fields are reported separately as `source_changed`
# rather than as changes, which is this family's rule (§8: a difference that
# comes with a provenance difference says something about our own two
# snapshots, not about the site).
PROFILE_ONLY_FIELDS = (
    "complaints_total", "complaints_3y", "complaints_12m", "reviews_total",
    "review_stars_avg", "years_in_business", "entity_type", "accredited_since",
    "website",
)


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        sku = p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def diff_products(old: List[dict], new: List[dict]) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed = [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # A row whose `data_source` differs between runs is not comparable on
        # the profile-only columns: a listing row leaves them null and a
        # profile row fills them, so every one of them would read as a change
        # and none of it would be about the business. Reporting it as a
        # change would be a false alarm about the site; the other columns
        # still compare fine.
        sources = (before.get("data_source"), after.get("data_source"))
        if sources[0] != sources[1] and any(f in field_changes
                                            for f in PROFILE_ONLY_FIELDS):
            profile_part = {f: v for f, v in field_changes.items()
                            if f in PROFILE_ONLY_FIELDS}
            other_part = {f: v for f, v in field_changes.items()
                          if f not in PROFILE_ONLY_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "data_source": {"old": sources[0], "new": sources[1]},
                "changes": profile_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable across run kinds.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  "
              f"{p.get('rating_grade') or 'not graded'}  {p.get('city')}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  "
              f"{p.get('rating_grade') or 'not graded'}  {p.get('city')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result["source_changed"]:
        src = c["data_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[data_source {src['old']!r} -> {src['new']!r}: a listing row "
              f"leaves these columns null and a profile row fills them, so "
              f"this is not a change in the business]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar — which
    is the normal case for output written before run metadata existed, or by
    `scraper_api_client.py` (single fetch, no pagination to cut short).
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    Prices of the SKUs both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            # This tool's whole premise is one row per `sku`, diffed on
            # price. A mode that produces many rows per sku would give a diff
            # whose every line is an artefact of two rows sharing an id, so
            # it is refused outright rather than answered. Both of this
            # repo's current modes qualify; the check is here so that adding
            # one that does not is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per sku. This tool diffs one row per sku on price, so there "
                f"is nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        problems.append(
            f"the two runs are different modes ({modes}). A listing row and a "
            f"detail row carry different fields, so `added`/`removed` would "
            f"describe the mode change rather than the catalogue.")

    # A SORT MISMATCH, which on this site is the one that really bites.
    #
    # The sibling repos guard a cross-storefront diff with `source`. BBB has
    # ONE host for both its countries, so `source` is "bbb.org" on both sides
    # and there is no storefront split for it to catch. What decides WHICH
    # businesses are in a file here is the ORDERING, because every query is
    # capped at 15 pages of 15 however many it matched: measured 2026-09-16,
    # `best-match` returned 15/15 BBB Accredited businesses and `a-z`
    # returned 0/15 from the identical query.
    #
    # So two runs that differ only in `--sort` hold two different SAMPLES of
    # the same result set, and every line of a diff between them is an
    # artefact of the ordering rather than a change in the directory.
    sorts = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        try:
            rows = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seen = {r.get("sort") for r in rows if r.get("sort")}
        if len(seen) == 1:
            sorts[label] = seen.pop()
        elif len(seen) > 1:
            problems.append(
                f"{label} ({path}) holds more than one sort ({sorted(seen)}) "
                f"— that file merges runs of different orderings, so it is "
                f"not one sample of anything.")
    if len(set(sorts.values())) > 1:
        problems.append(
            f"the two runs used different orderings ({sorts}). BBB serves at "
            f"most 225 rows of a result set that can run to tens of "
            f"thousands, and the ordering decides which 225 — 'best-match' "
            f"returned 15/15 BBB Accredited businesses where 'a-z' returned "
            f"0/15 from the same query. Every `added`/`removed` line would "
            f"describe the sort rather than the directory.")

    # And whether either run was CAPPED, which changes what `removed` means.
    for label, path in (("--old", args.old), ("--new", args.new)):
        _, meta = _run_status(path)
        if (meta or {}).get("capped_by_site"):
            print(f"[i] {label} ({path}) holds {meta.get('reachable_max')} of "
                  f"{meta.get('total_results')} businesses BBB reports for "
                  f"that query — a complete run, and a sample. A `removed` "
                  f"line may mean the ordering shifted rather than that "
                  f"anything closed.")

    if not problems:
        return True

    # A generic headline, because the reasons below are no longer only about
    # completeness: a mode mismatch and a reviews run are refused too, and a
    # message naming the wrong reason sends the reader looking in the wrong
    # place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include businesses that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two bbb-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # `source_changed` is not a reason to fail: it means one row came from a
    # listing run and the other from a profile run, so the columns only a
    # profile fills differ. That says something about our own two snapshots
    # rather than about the business, and alerting on it would train whoever
    # reads the alert to ignore it.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
