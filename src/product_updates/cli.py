import argparse
from apscheduler.schedulers.blocking import BlockingScheduler
from .config import load
from .config import Product
from .notify import render, send
from .scraper import scan
from .service import check
from .storage import Store
from .variants import group_candidates

def run_once(settings, store, notify=True):
    changes, issues = check(settings, store)
    for issue in issues: print(f"WARNING: {issue}")
    if not changes:
        print("No detectable listing changes."); return 0
    message = render(changes); print(message)
    if notify:
        try: print("Sent via:", ", ".join(send(message)) or "no configured destination")
        except Exception as exc: print(f"WARNING: notification failed: {exc}")
    return 0

def main():
    parser = argparse.ArgumentParser(description="Local product price monitor")
    parser.add_argument("command", choices=["check", "run", "history", "discover"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--product", help="Plain-language product, e.g. 'AirPods 4'")
    parser.add_argument("--pincode", help="Delivery PIN for the discovery result")
    args = parser.parse_args()
    settings, store = load(args.config), Store()
    if args.command == "discover":
        if not args.product or not args.pincode:
            parser.error("discover requires --product and --pincode")
        terms = [word.lower() for word in args.product.split() if len(word) > 1]
        product = Product(name=args.product, pincode=args.pincode, required_terms=terms, excluded_terms=[])
        retailers = [retailer.search_for(args.product) for retailer in settings.retailers]
        offers, issues = scan(retailers, [], product, settings.schedule.request_timeout_seconds, settings.scraper.browser_fallback)
        print(f"Candidates for {args.product} — PIN {args.pincode} (public cash price)")
        for candidate in group_candidates(offers):
            colours = f" ({', '.join(candidate.colours)})" if candidate.colours else ""
            stock = "in stock" if candidate.available else "out of stock"
            print(f"{candidate.retailer} | {candidate.description}{colours} | ₹{candidate.price} | {stock} | {candidate.urls[0]}")
        for issue in issues: print(f"WARNING: {issue}")
        return
    if args.command == "check": raise SystemExit(run_once(settings, store))
    if args.command == "history":
        for offer in store.history(args.limit): print(f"{offer.observed_at:%Y-%m-%d %H:%M} | {offer.retailer} | {offer.price} | {offer.title} | {offer.url}")
        return
    print(f"Monitoring every {settings.schedule.minutes} minutes. Press Ctrl+C to stop.")
    scheduler = BlockingScheduler(); scheduler.add_job(run_once, "interval", minutes=settings.schedule.minutes, args=[settings, store])
    run_once(settings, store); scheduler.start()

if __name__ == "__main__": main()
