"""SQLAlchemy 2.0 declarative: string-based relationship() and a
ForeignKey() table-name string (not a Python identifier -> heuristic can't
resolve it to the AddressTable class)."""
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AddressTable(Base):
    __tablename__ = "addresses"
    id: Mapped[int] = mapped_column(primary_key=True)


class UserTable(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    address_id: Mapped[int] = mapped_column(ForeignKey("addresses.id"))
    address = relationship("AddressTable")
