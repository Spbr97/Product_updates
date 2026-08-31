from .config import Settings
from .models import Change, Offer
from .scraper import scan
from .storage import Store

def detect(previous: Offer | None, current: Offer) -> Change | None:
    if previous is None: return Change("new", current)
    if previous.price != current.price: return Change("price", current, previous.price, previous.available)
    if previous.available != current.available: return Change("availability", current, previous.price, previous.available)
    return None

def check(settings: Settings, store: Store) -> tuple[list[Change], list[str]]:
    direct_listings = [(listing.name, str(listing.url)) for listing in settings.direct_listings]
    direct_listings.extend(("Direct listing", str(url)) for url in settings.listing_urls)
    direct_retailers = {name.casefold() for name, _ in direct_listings}
    search_retailers = [retailer for retailer in settings.retailers if retailer.name.casefold() not in direct_retailers]
    offers, issues = scan(search_retailers, direct_listings, settings.product, settings.schedule.request_timeout_seconds, settings.scraper.browser_fallback)
    changes = []
    is_baseline = not store.has_observations()
    for offer in offers:
        change = detect(store.last(offer.key), offer)
        store.save(offer)
        if change and not is_baseline: changes.append(change)
    return changes, issues
