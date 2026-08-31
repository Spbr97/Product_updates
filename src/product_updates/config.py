from pathlib import Path
import yaml
from pydantic import BaseModel, Field, HttpUrl

class Product(BaseModel):
    name: str
    pincode: str
    required_terms: list[str]
    excluded_terms: list[str] = Field(default_factory=list)
    currency: str = "INR"

class Schedule(BaseModel):
    minutes: int = Field(default=60, ge=5)
    request_timeout_seconds: int = Field(default=25, ge=5, le=90)

class Scraper(BaseModel):
    browser_fallback: bool = True

class Retailer(BaseModel):
    name: str
    search_url: HttpUrl
    search_url_template: str | None = None

    def search_for(self, query: str) -> "Retailer":
        from urllib.parse import quote_plus
        if not self.search_url_template:
            return self
        return self.model_copy(update={"search_url": self.search_url_template.format(query=quote_plus(query))})

class DirectListing(BaseModel):
    name: str
    url: HttpUrl

class Settings(BaseModel):
    product: Product
    schedule: Schedule = Field(default_factory=Schedule)
    scraper: Scraper = Field(default_factory=Scraper)
    retailers: list[Retailer]
    listing_urls: list[HttpUrl] = Field(default_factory=list)
    direct_listings: list[DirectListing] = Field(default_factory=list)

def load(path: str | Path = "config.yaml") -> Settings:
    return Settings.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
