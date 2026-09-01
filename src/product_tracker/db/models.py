"""SQLAlchemy ORM models -- the persistence shape only.

Business rules live in ``services``; these classes just describe tables. Domain enums are
persisted as native PostgreSQL enum types using their *values*.

History tables (``price_history``, ``availability_history``) are append-only by contract:
nothing in the application updates or deletes a row once written, except the cascade when
its product is removed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..domain.enums import (
    Availability,
    CheckStatus,
    FetchMethod,
    NotificationStatus,
    RuleType,
    TrackingStatus,
)
from .base import Base

# Money: 12 digits total, 2 decimal places. Enough for any consumer price in any currency
# we are likely to see, and exact (never float).
Money = Numeric(12, 2)


def _pg_enum(enum_cls: type, name: str) -> SAEnum:
    """Native PostgreSQL enum keyed on member values rather than names."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Store(Base):
    """A supported e-commerce site.

    Rows mirror the adapter registry in code (``product-tracker stores sync``). The table
    exists so products can carry a stable foreign key and so stores can be disabled
    operationally without a deploy.
    """

    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    adapter_key: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    products: Mapped[list[Product]] = relationship(back_populates="store")


class User(Base):
    """An account.

    Only ever holds a SHA-256 of the API key, never the key itself: a database dump, a log
    line, or a support query can therefore never reveal a working credential. The key is
    shown once, at creation, and cannot be recovered afterwards -- only replaced.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        # Unique so one key can never resolve to two accounts.
        UniqueConstraint("api_key_hash", name="uq_users_api_key_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    api_key_hash: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


class Subscription(Base):
    """One user watching one listing.

    This is what makes listings shareable. The product row and its price history are global,
    so two users tracking the same URL cost the retailer one fetch rather than two, and a
    user joining later sees the whole history that already exists.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_subscriptions_user_product"),
        Index("ix_subscriptions_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    # Per subscriber, so one user pausing does not stop everyone else's updates for a
    # listing they all watch. The product is checked while any subscriber wants it.
    paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="subscriptions")
    product: Mapped[Product] = relationship(lazy="joined")


class ProductGroup(Base):
    """One real-world product, across every model, colour and shop that sells it.

    A group is what a person means by "iPhone 17". The rows underneath it are the actual
    listings: one per (variant, store). Without this, "iPhone 17 256GB Black on Flipkart"
    and the same phone on Reliance are two unrelated rows and nothing can compare them.
    """

    __tablename__ = "product_groups"
    __table_args__ = (
        # Per user, not global: two people may each keep an "iphone-17".
        UniqueConstraint("user_id", "slug", name="uq_product_groups_user_slug"),
        Index("ix_product_groups_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    variants: Mapped[list[ProductVariant]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ProductVariant.position, ProductVariant.label",
    )


class ProductVariant(Base):
    """One model or colour within a group -- "256GB / Black".

    This is the join point that makes cross-store comparison possible. Stores name the same
    variant differently ("256 GB", "256GB", "Black", "Midnight Black"), so matching on the
    listing title would silently split one model into several. A variant row is the single
    canonical identity that each store's listing attaches to.

    ``attributes`` is free-form on purpose: storage and colour suit phones, but capacity,
    size, or pack count suit other things, and none of them should need a migration.
    """

    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("group_id", "label", name="uq_product_variants_group_label"),
        Index("ix_product_variants_group_id", "group_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("product_groups.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Display order, so "128GB" sorts before "512GB" rather than alphabetically.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    group: Mapped[ProductGroup] = relationship(back_populates="variants")
    listings: Mapped[list[VariantListing]] = relationship(
        back_populates="variant", cascade="all, delete-orphan", passive_deletes=True
    )


class VariantListing(Base):
    """A listing that sells a given variant, in one user's grouping.

    A join table rather than a column on ``products``, because a product row is shared
    between users while grouping is private to each. With a single column, the second user
    to group a listing silently removed it from the first user's comparison -- their grid
    lost a column with nothing to explain it. The variant already belongs to a group, and
    the group to a user, so this link is per-user by construction.
    """

    __tablename__ = "variant_listings"
    __table_args__ = (
        UniqueConstraint("variant_id", "product_id", name="uq_variant_listings"),
        Index("ix_variant_listings_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    variant: Mapped[ProductVariant] = relationship(back_populates="listings")
    product: Mapped[Product] = relationship(lazy="joined")


class Product(Base):
    """A tracked product listing at one store.

    ``url_canonical`` is the normalised form of ``url`` and carries the uniqueness
    constraint, so the same listing submitted with different tracking parameters is
    recognised as a duplicate rather than tracked twice.
    """

    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_store_id", "store_id"),
        Index("ix_products_tracking_status", "tracking_status"),
        Index("ix_products_last_checked_at", "last_checked_at"),
        CheckConstraint(
            "check_interval_seconds IS NULL OR check_interval_seconds >= 60",
            name="check_interval_minimum",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_canonical: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False
    )

    name: Mapped[str | None] = mapped_column(Text)
    product_identifier: Mapped[str | None] = mapped_column(String(128))
    image_url: Mapped[str | None] = mapped_column(Text)

    current_price: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(3))
    availability: Mapped[Availability] = mapped_column(
        _pg_enum(Availability, "availability"),
        nullable=False,
        default=Availability.UNKNOWN,
        server_default=Availability.UNKNOWN.value,
    )

    # Free-form room for future per-store attributes (seller, coupon, delivery, ...)
    # without a migration. Named to avoid clashing with SQLAlchemy's ``Base.metadata``.
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    tracking_status: Mapped[TrackingStatus] = mapped_column(
        _pg_enum(TrackingStatus, "tracking_status"),
        nullable=False,
        default=TrackingStatus.ACTIVE,
        server_default=TrackingStatus.ACTIVE.value,
    )
    check_interval_seconds: Mapped[int | None] = mapped_column(Integer)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    store: Mapped[Store] = relationship(back_populates="products", lazy="joined")
    price_history: Mapped[list[PriceHistory]] = relationship(
        back_populates="product", cascade="all, delete-orphan", passive_deletes=True
    )
    availability_history: Mapped[list[AvailabilityHistory]] = relationship(
        back_populates="product", cascade="all, delete-orphan", passive_deletes=True
    )
    rules: Mapped[list[TrackingRule]] = relationship(
        back_populates="product", cascade="all, delete-orphan", passive_deletes=True
    )


class PriceHistory(Base):
    """An observed price. Append-only.

    A row is written on the first observation and whenever the extracted price differs
    from the most recent recorded price. Every check -- changed or not -- is still
    recorded in ``check_executions``.
    """

    __tablename__ = "price_history"
    __table_args__ = (
        Index("ix_price_history_product_id_observed_at", "product_id", "observed_at"),
        CheckConstraint("price >= 0", name="price_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    check_execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("check_executions.id", ondelete="SET NULL")
    )

    product: Mapped[Product] = relationship(back_populates="price_history")


class AvailabilityHistory(Base):
    """An observed availability transition. Append-only, written only on change."""

    __tablename__ = "availability_history"
    __table_args__ = (
        Index(
            "ix_availability_history_product_id_observed_at",
            "product_id",
            "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    availability: Mapped[Availability] = mapped_column(
        _pg_enum(Availability, "availability"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    check_execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("check_executions.id", ondelete="SET NULL")
    )

    product: Mapped[Product] = relationship(back_populates="availability_history")


class TrackingRule(Base):
    """A condition that turns an observation into a notification.

    ``params`` holds rule-type-specific settings (for example
    ``{"target_price": "69999.00"}``), which is what lets new rule types ship without a
    schema change.
    """

    __tablename__ = "tracking_rules"
    __table_args__ = (
        Index("ix_tracking_rules_product_id_enabled", "product_id", "enabled"),
        Index("ix_tracking_rules_user_id", "user_id"),
        CheckConstraint(
            "cooldown_seconds IS NULL OR cooldown_seconds >= 0", name="cooldown_non_negative"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    # Alerts express intent, so they belong to whoever set them. Two people watching the
    # same listing can hold different targets without seeing each other's.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rule_type: Mapped[RuleType] = mapped_column(_pg_enum(RuleType, "rule_type"), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    notify_provider: Mapped[str | None] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    cooldown_seconds: Mapped[int | None] = mapped_column(Integer)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    product: Mapped[Product] = relationship(back_populates="rules")


class Notification(Base):
    """A generated alert and its delivery outcome.

    ``dedupe_key`` is a deterministic digest of the event. Inserting with
    ``ON CONFLICT (dedupe_key) DO NOTHING`` is what makes notifications idempotent, so a
    retried job cannot alert the user twice for the same observation.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_notifications_dedupe_key"),
        Index("ix_notifications_product_id_created_at", "product_id", "created_at"),
        Index("ix_notifications_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    tracking_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracking_rules.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[NotificationStatus] = mapped_column(
        _pg_enum(NotificationStatus, "notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
    )
    provider: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CheckExecution(Base):
    """One attempt to check one product -- the diagnostic record.

    Written for every check, success or failure, so "why did this fail?" is answerable
    without reading logs.
    """

    __tablename__ = "check_executions"
    __table_args__ = (
        Index("ix_check_executions_product_id_started_at", "product_id", "started_at"),
        Index("ix_check_executions_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[CheckStatus] = mapped_column(
        _pg_enum(CheckStatus, "check_status"), nullable=False
    )
    fetch_method: Mapped[FetchMethod] = mapped_column(
        _pg_enum(FetchMethod, "fetch_method"),
        nullable=False,
        default=FetchMethod.NONE,
        server_default=FetchMethod.NONE.value,
    )
    http_status: Mapped[int | None] = mapped_column(Integer)

    extracted_price: Mapped[Decimal | None] = mapped_column(Money)
    extracted_currency: Mapped[str | None] = mapped_column(String(3))
    availability_result: Mapped[Availability | None] = mapped_column(
        _pg_enum(Availability, "availability")
    )

    price_changed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    availability_changed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    rules_evaluated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    rules_matched: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    notifications_created: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # ``error_detail`` is sanitised and truncated before it is stored; it never holds
    # credentials or full page bodies.
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)


class WorkerHeartbeat(Base):
    """Proof that a worker process is alive.

    Liveness used to be *inferred* from overdue jobs in the scheduler's store, which cannot
    tell "no worker running" from "a worker running but wedged mid-check". A row the worker
    touches on every reconcile answers it directly.

    One row per worker process, keyed by a per-process id, so a restart replaces its own
    row and a second worker is visible rather than hidden.
    """

    __tablename__ = "worker_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    hostname: Mapped[str | None] = mapped_column(String(255))
    pid: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
