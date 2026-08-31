import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from .models import Offer

class Store:
    def __init__(self, path: str | Path = "data/prices.sqlite3") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS offers (id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, retailer TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, price TEXT, available INTEGER NOT NULL, delivery_charge TEXT, UNIQUE(observed_at, retailer, url))""")
        self.db.commit()

    def last(self, key: str) -> Offer | None:
        retailer, url = key.split("|", 1)
        row = self.db.execute("SELECT * FROM offers WHERE retailer=? AND url=? ORDER BY id DESC LIMIT 1", (retailer, url)).fetchone()
        return self._offer(row) if row else None

    def save(self, offer: Offer) -> None:
        self.db.execute("INSERT OR IGNORE INTO offers (observed_at,retailer,title,url,price,available,delivery_charge) VALUES (?,?,?,?,?,?,?)", (offer.observed_at.isoformat(), offer.retailer, offer.title, offer.url, str(offer.price) if offer.price is not None else None, int(offer.available), str(offer.delivery_charge) if offer.delivery_charge is not None else None))
        self.db.commit()

    def has_observations(self) -> bool:
        return self.db.execute("SELECT 1 FROM offers LIMIT 1").fetchone() is not None

    def history(self, limit: int) -> list[Offer]:
        return [self._offer(r) for r in self.db.execute("SELECT * FROM offers ORDER BY id DESC LIMIT ?", (limit,))]

    @staticmethod
    def _offer(row: sqlite3.Row) -> Offer:
        return Offer(retailer=row["retailer"], title=row["title"], url=row["url"], price=Decimal(row["price"]) if row["price"] else None, available=bool(row["available"]), delivery_charge=Decimal(row["delivery_charge"]) if row["delivery_charge"] else None, observed_at=datetime.fromisoformat(row["observed_at"]))
