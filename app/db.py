import os, datetime
from sqlalchemy import (create_engine, String, Integer, Float, Boolean, DateTime,
                        ForeignKey, Text)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, relationship,
                            sessionmaker)

# DATABASE_URL fourni par Coolify (PostgreSQL). Repli SQLite en local.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./suivi.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    bon_commande: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    proforma: Mapped[str] = mapped_column(String(64), default="")
    facture: Mapped[str] = mapped_column(String(64), default="")
    date_commande: Mapped[str] = mapped_column(String(20), default="")
    fournisseur: Mapped[str] = mapped_column(String(128), default="")
    pays: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    nb_machines: Mapped[int] = mapped_column(Integer, default=0)
    montant_achat: Mapped[float] = mapped_column(Float, default=0.0)
    devise: Mapped[str] = mapped_column(String(8), default="")
    tva_regime: Mapped[str] = mapped_column(String(32), default="")
    tva_montant: Mapped[float] = mapped_column(Float, default=0.0)
    montant_total: Mapped[float] = mapped_column(Float, default=0.0)
    paiement: Mapped[str] = mapped_column(String(16), default="A payer")
    date_paiement: Mapped[str] = mapped_column(String(20), default="")
    etiquette_ups: Mapped[str] = mapped_column(String(16), default="A creer")
    tracking_ups: Mapped[str] = mapped_column(String(64), default="")
    expedition: Mapped[str] = mapped_column(String(16), default="En attente")
    date_expedition: Mapped[str] = mapped_column(String(20), default="")
    reception: Mapped[str] = mapped_column(String(16), default="Non recu")
    date_reception: Mapped[str] = mapped_column(String(20), default="")
    stock_odoo: Mapped[bool] = mapped_column(Boolean, default=False)
    date_stock: Mapped[str] = mapped_column(String(20), default="")
    odoo_po_id: Mapped[str] = mapped_column(String(32), default="")
    odoo_po_name: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    machines: Mapped[list["Machine"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class Machine(Base):
    __tablename__ = "machines"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    bon_commande: Mapped[str] = mapped_column(String(64), index=True)
    item_id: Mapped[str] = mapped_column(String(64), default="")
    product_name: Mapped[str] = mapped_column(Text, default="")
    sku_requested: Mapped[str] = mapped_column(String(128), default="")
    sku_scanned: Mapped[str] = mapped_column(String(128), default="")
    imei: Mapped[str] = mapped_column(String(64), default="")
    serial: Mapped[str] = mapped_column(String(64), default="", index=True)
    physical_status: Mapped[str] = mapped_column(String(32), default="")
    location: Mapped[str] = mapped_column(String(32), default="")
    carton: Mapped[str] = mapped_column(String(32), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    manufacturer: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    capacity: Mapped[str] = mapped_column(String(32), default="")
    colour: Mapped[str] = mapped_column(String(32), default="")
    network: Mapped[str] = mapped_column(String(32), default="")
    variant: Mapped[str] = mapped_column(String(64), default="")
    grade: Mapped[str] = mapped_column(String(8), default="")
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    recu: Mapped[bool] = mapped_column(Boolean, default=False)
    probleme: Mapped[str] = mapped_column(String(32), default="RAS")
    commentaire: Mapped[str] = mapped_column(Text, default="")
    order: Mapped["Order"] = relationship(back_populates="machines")


def init_db():
    Base.metadata.create_all(engine)
    # Migrations idempotentes : ajoute les colonnes récentes aux bases existantes (PostgreSQL).
    if engine.dialect.name == "postgresql":
        from sqlalchemy import text
        alters = [
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS stock_odoo BOOLEAN DEFAULT FALSE",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS date_stock VARCHAR(20) DEFAULT ''",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS odoo_po_id VARCHAR(32) DEFAULT ''",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS odoo_po_name VARCHAR(64) DEFAULT ''",
        ]
        with engine.begin() as conn:
            for stmt in alters:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
