"""Command line. Run from the project folder:

    python -m mpf ingest              # fetch all enabled sources (polite, cached)
    python -m mpf ingest --source nl_ea
    python -m mpf score               # score everything in the database
    python -m mpf show --min-score 40 # print flagged items with evidence
    python -m mpf label-template      # make labels/to_label.csv for hand labelling
    python -m mpf eval                # precision/recall against your labels
    python -m mpf llm --limit 5       # optional summaries (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys

import requests

from mpf import db
from mpf.config import ROOT, data_dir, drop_dir, load_scoring, load_settings
from mpf.evaluate import read_labels, run_dfo_recall, run_hand_label_eval
from mpf.http import PoliteClient
from mpf.score import reason_lines, score_text
from mpf.sources import SOURCES
from mpf.sources.drop_folder import DropFolderSource
from mpf.textutil import today
from mpf.timing import compute


def _open(settings):
    return db.connect(data_dir(settings) / "mpf.sqlite")


def cmd_ingest(args, settings):
    conn = _open(settings)
    client = PoliteClient(settings, data_dir(settings) / "cache")
    from mpf.sources import LIVE_SOURCES
    if args.source:
        wanted = args.source
    elif args.live:
        wanted = [n for n in LIVE_SOURCES if settings["sources"].get(n, {}).get("enabled")]
    else:
        wanted = [n for n, c in settings["sources"].items() if c.get("enabled")]
    problems = []
    for name in wanted:
        conf = settings["sources"].get(name, {})
        if name == "drop_folder":
            source = DropFolderSource(client, settings, conf, drop_dir(settings))
        elif name in SOURCES:
            source = SOURCES[name](client, settings, conf)
        else:
            print(f"Unknown source '{name}'. Known: {', '.join(list(SOURCES) + ['drop_folder'])}")
            continue
        print(f"== {name}")
        new = total = 0
        try:
            for item in source.fetch():
                total += 1
                new += db.upsert_item(conn, item)
                if total % 25 == 0:
                    conn.commit()
                    print(f"   ...{total} items", flush=True)
        except requests.RequestException as e:
            # Usually the internet connection dropped. Pages already saved stay in the
            # cache, so running ingest again only fetches what is missing.
            print(f"   Network problem, stopped this source early: {type(e).__name__}. Run ingest again later.")
            problems.append(name)
        except Exception as e:      # a broken source must never stop the others
            print(f"   ERROR in this source, skipped: {type(e).__name__}: {str(e)[:200]}")
            problems.append(name)
        conn.commit()
        for line in source.log:
            print("   " + line)
            if line.startswith("STOPPED"):
                problems.append(name)
        print(f"   {total} items ({new} new)")
    print(f"Network requests this run: {client.network_requests} (everything else came from the cache)")
    if problems:
        print(f"Sources that stopped early or failed: {', '.join(sorted(set(problems)))}. The others ran normally.")


def cmd_score(args, settings):
    conn = _open(settings)
    rules = load_scoring()
    t = settings.get("timing", {})
    now = today()
    rows = conn.execute("SELECT * FROM items").fetchall()
    for r in rows:
        row = dict(r)
        row["extra"] = json.loads(row["extra"] or "{}")
        s = score_text(row["full_text"] or "", rules)
        tm = compute(row, now, t.get("closing_soon_days", 14), t.get("too_late_days", 7))
        conn.execute(
            "UPDATE items SET score=?, flagged=?, project_types=?, size_summary=?, reasons=?, facts=?, "
            "stage=?, timing=?, days_to_deadline=?, too_late=? WHERE key=?",
            (s["score"], int(s["flagged"]), json.dumps(s["project_types"]), s["size_summary"],
             json.dumps(s["reasons"], ensure_ascii=False), json.dumps(s["facts"], ensure_ascii=False),
             tm["stage"], tm["timing"], tm["days_to_deadline"], int(tm["too_late"]), row["key"]))
    conn.commit()
    live = conn.execute("SELECT COUNT(*), SUM(flagged) FROM items WHERE is_test_set=0").fetchone()
    print(f"Scored {len(rows)} items. Live sources: {live[0]} items, {live[1] or 0} flagged "
          f"(score >= {rules['flag_threshold']}).")


def _print_item(r):
    reasons = json.loads(r["reasons"] or "[]")
    print(f"[{r['score']:>3}] {r['title']}")
    print(f"      {r['key']} | {r['province']} | proponent: {r['proponent']}")
    print(f"      type: {', '.join(json.loads(r['project_types'] or '[]')) or 'unknown'} | size: {r['size_summary']}"
          f" | water body: {r['water_body']}")
    print(f"      stage: {r['stage']} | {r['timing']}")
    print(f"      {r['url']}")
    for line in reason_lines(reasons):
        print(f"        {line}")
    if r["summary_en"]:
        print(f"      LLM ({r['llm_confidence']}{', translated' if r['llm_translated'] else ''}): {r['summary_en']}")
    print()


def cmd_show(args, settings):
    conn = _open(settings)
    q = "SELECT * FROM items WHERE score >= ? AND is_test_set = ?"
    params = [args.min_score, 1 if args.test_set else 0]
    if args.source:
        q += " AND source = ?"
        params.append(args.source)
    q += " ORDER BY score DESC, comment_deadline LIMIT ?"
    params.append(args.limit)
    for r in conn.execute(q, params).fetchall():
        _print_item(r)


def cmd_label_template(args, settings):
    conn = _open(settings)
    out = ROOT / "labels" / "to_label.csv"
    done = read_labels(ROOT / settings.get("labels_file", "labels/labels.csv"))
    rows = conn.execute("SELECT key, source, title, province, proponent, url FROM items "
                        "WHERE is_test_set=0 ORDER BY source, key").fetchall()
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "label", "notes", "source", "title", "province", "proponent", "url"])
        for r in rows:
            if r["key"] not in done:
                w.writerow([r["key"], "", "", r["source"], r["title"], r["province"], r["proponent"], r["url"]])
    print(f"Wrote {out}. Scores are left out on purpose, so they do not sway your judgement.\n"
          f"Fill 'label' with 1 (lead) or 0 (not a lead) for about 30 rows, then save as labels/labels.csv.")


def cmd_eval(args, settings):
    conn = _open(settings)
    rules = load_scoring()
    threshold = args.threshold if args.threshold is not None else rules["flag_threshold"]
    print(run_hand_label_eval(conn, ROOT / settings.get("labels_file", "labels/labels.csv"), threshold))
    print()
    print(run_dfo_recall(conn, threshold))


def cmd_llm(args, settings):
    from mpf.llm import summarize
    conn = _open(settings)
    conf = settings.get("llm", {})
    min_score = args.min_score if args.min_score is not None else conf.get("min_score", 40)
    rows = conn.execute("SELECT key, full_text FROM items WHERE is_test_set=0 AND score >= ? "
                        "AND summary_en IS NULL ORDER BY score DESC LIMIT ?", (min_score, args.limit)).fetchall()
    for r in rows:
        try:
            res = summarize(r["full_text"], conf.get("model", "claude-opus-5-5"))
        except Exception as e:
            print(f"{r['key']}: skipped ({e})")
            continue
        conn.execute("UPDATE items SET summary_en=?, llm_confidence=?, llm_quotes=?, llm_translated=?, "
                     "llm_model=? WHERE key=?",
                     (res["summary_en"], res["confidence"], json.dumps(res["quotes"], ensure_ascii=False),
                      int(res["translated"]), res.get("model"), r["key"]))
        conn.commit()
        print(f"{r['key']}: {res['confidence']}, {len(res['quotes'])} verified quotes, {res['dropped']} dropped")


def _review_path(settings):
    return ROOT / settings.get("review", {}).get("file", "review/review.csv")


def cmd_review(args, settings):
    """Write review/review.csv with every scored item. Mark yes (checked) or no (hide)."""
    from mpf import review
    conn = _open(settings)
    from mpf.site import enabled_sources
    counts = review.write_review(conn, _review_path(settings), settings.get("tiers", {}), enabled_sources(settings))
    print(f"Review file: {_review_path(settings)} ({counts['rows']} items, {counts['new']} new, "
          f"{counts['yes']} marked yes, {counts['no']} marked no)")


def cmd_publish(args, settings):
    """Build site/ from the Strong match, Possible and Weak tiers."""
    from collections import Counter

    from mpf import review, site
    from mpf.tiers import TIER_LABELS
    conn = _open(settings)
    rows = review.read_review(_review_path(settings))
    out = ROOT / "site"
    records, skipped = site.build_public_records(conn, rows, settings, today(), previous=site.load_previous(out))
    site.write_site(out, records, settings, today())
    tiers = Counter(r["tier"] for r in records)
    print(f"Published {len(records)} items to {out / 'index.html'}: " +
          ", ".join(f"{TIER_LABELS[t]} {tiers.get(t, 0)}" for t in site.PUBLISHED_TIERS))
    print(f"Reviewed by a person: {sum(r['reviewed'] for r in records)}. Not published: "
          f"{skipped['not_relevant']} probably not relevant (review file only), {skipped['hidden_by_you']} marked no, "
          f"{skipped['bad_link']} bad links, {skipped['merged_duplicates']} duplicates merged.")


def cmd_approved_count(args, settings):
    from mpf import review
    print(len(review.approved_ids(_review_path(settings))))


def cmd_zip_site(args, settings):
    """Zip site/ for Netlify Drop (app.netlify.com/drop)."""
    import zipfile
    src = ROOT / "site"
    if not (src / "index.html").exists():
        print("site/ is empty. Run publish first.")
        return
    out = ROOT / "site.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(src))
    print(f"Wrote {out}. Drag it onto https://app.netlify.com/drop to get a link.")


def cmd_check_access(args, settings):
    """Check each live source still allows us, without the cache. One blocked source does not
    stop the others; the command only fails if every source is unreachable."""
    import tempfile
    from pathlib import Path as _P

    from mpf.http import Blocked
    from mpf.sources import CHECK_URLS, LIVE_SOURCES
    ok, problems = [], []
    with tempfile.TemporaryDirectory() as tmp:
        client = PoliteClient(settings, _P(tmp))
        for name in LIVE_SOURCES:
            if not settings["sources"].get(name, {}).get("enabled"):
                continue
            url = CHECK_URLS[name].replace("{date}", today().strftime("%Y%m%d"))
            headers = {"Accept": "application/json"} if name == "es_boe" else None
            try:
                body = client.get(url, headers=headers)
                ok.append(name)
                print(f"OK      {name}: {len(body):,} bytes, robots.txt allows it")
            except Blocked as e:
                problems.append(name)
                print(f"BLOCKED {name}: {e}")
            except requests.RequestException as e:
                problems.append(name)
                print(f"FAILED  {name}: {str(e)[:160]}")
    if problems:
        print(f"Not reachable: {', '.join(problems)}. Those sources will stop safely; nothing is worked around.")
    if not ok:
        print("No source is reachable. Stopping.")
        sys.exit(1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m mpf", description="Marine Project Finder")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest", help="fetch sources")
    s.add_argument("--source", action="append", help="only this source (repeatable)")
    s.add_argument("--live", action="store_true", help="all enabled live web sources (used by the scheduled run)")
    sub.add_parser("score", help="score all items")
    s = sub.add_parser("show", help="print top items with evidence")
    s.add_argument("--min-score", type=int, default=40)
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--source")
    s.add_argument("--test-set", action="store_true", help="show DFO test-set rows instead")
    sub.add_parser("label-template", help="write labels/to_label.csv")
    s = sub.add_parser("eval", help="precision and recall against hand labels")
    s.add_argument("--threshold", type=int)
    s = sub.add_parser("llm", help="optional LLM summaries")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--min-score", type=int)
    sub.add_parser("review", help="write review/review.csv (every scored item)")
    sub.add_parser("publish", help="build site/ with every item, in tiers")
    sub.add_parser("approved-count", help="print how many items are marked yes")
    sub.add_parser("zip-site", help="zip site/ for Netlify Drop")
    sub.add_parser("check-access", help="check the sources still allow us (no cache)")
    args = p.parse_args(argv)
    settings = load_settings()
    {"ingest": cmd_ingest, "score": cmd_score, "show": cmd_show, "label-template": cmd_label_template,
     "eval": cmd_eval, "llm": cmd_llm, "review": cmd_review, "publish": cmd_publish,
     "approved-count": cmd_approved_count, "zip-site": cmd_zip_site,
     "check-access": cmd_check_access}[args.cmd](args, settings)


if __name__ == "__main__":
    sys.exit(main())
