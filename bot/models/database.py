"""SQLAlchemy models and database session helpers.

SQLite is used by default. DATABASE_URL can later point at PostgreSQL
(e.g. postgresql+psycopg://...) without changing the repository API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from bot.models.report import ReportStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_verified: Mapped[bool] = mapped_column(
        "admin_verified",
        Boolean,
        default=False,
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )

    reports: Mapped[list[Report]] = relationship(back_populates="user")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    transcription: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(64),
        default=ReportStatus.RECEIVED.value,
        index=True,
    )
    excel_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )

    user: Mapped[User] = relationship(back_populates="reports")


class Database:
    """Thin persistence layer that can later target PostgreSQL via DATABASE_URL."""

    def __init__(self, database_url: str) -> None:
        connect_args: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        self.engine = create_engine(database_url, future=True, connect_args=connect_args)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    def create_tables(self) -> None:
        Base.metadata.create_all(self.engine)
        self._migrate_user_profile_columns()

    def _migrate_user_profile_columns(self) -> None:
        """Add profile and explicit admin-approval columns to existing databases."""
        inspector = inspect(self.engine)
        if "users" not in inspector.get_table_names():
            return
        existing = {column["name"] for column in inspector.get_columns("users")}
        statements: list[str] = []
        if "phone_number" not in existing:
            statements.append("ALTER TABLE users ADD COLUMN phone_number VARCHAR(32)")
        if "department" not in existing:
            statements.append("ALTER TABLE users ADD COLUMN department VARCHAR(255)")
        if "admin_verified" not in existing:
            statements.append(
                "ALTER TABLE users ADD COLUMN admin_verified BOOLEAN DEFAULT 0"
            )
        if not statements:
            return
        with self.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_or_create_user(
        self,
        telegram_id: int,
        username: str | None,
        display_name: str | None,
    ) -> User:
        with self.session() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                user = User(
                    telegram_id=telegram_id,
                    username=username,
                    display_name=display_name,
                )
                session.add(user)
                session.flush()
            else:
                user.username = username
                if display_name and not user.is_verified:
                    user.display_name = display_name
            session.refresh(user)
            # Detach a plain copy of identity fields for callers.
            session.expunge(user)
            return user

    def get_user_by_telegram_id(self, telegram_id: int) -> User | None:
        with self.session() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                return None
            session.expunge(user)
            return user

    def is_user_verified(self, telegram_id: int) -> bool:
        user = self.get_user_by_telegram_id(telegram_id)
        return bool(
            user
            and user.is_verified
            and user.display_name
            and user.department
        )

    def set_user_verification(self, telegram_id: int, verified: bool) -> User:
        with self.session() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                user = User(telegram_id=telegram_id)
                session.add(user)
            user.is_verified = verified
            session.flush()
            session.refresh(user)
            session.expunge(user)
            return user

    def list_users(self, limit: int = 20) -> list[User]:
        with self.session() as session:
            users = list(
                session.scalars(
                    select(User).order_by(User.id.desc()).limit(limit)
                )
            )
            for user in users:
                session.expunge(user)
            return users

    def complete_onboarding(
        self,
        telegram_id: int,
        *,
        full_name: str,
        department: str,
        username: str | None = None,
    ) -> User:
        with self.session() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                user = User(telegram_id=telegram_id, username=username)
                session.add(user)
                session.flush()
            user.username = username if username is not None else user.username
            user.display_name = full_name.strip()
            user.department = department.strip()
            session.flush()
            session.refresh(user)
            session.expunge(user)
            return user

    def create_report(self, user_db_id: int, status: ReportStatus = ReportStatus.RECEIVED) -> Report:
        with self.session() as session:
            report = Report(user_id=user_db_id, status=status.value)
            session.add(report)
            session.flush()
            session.refresh(report)
            session.expunge(report)
            return report

    def update_report(
        self,
        report_id: int,
        *,
        transcription: str | None = None,
        structured_json: str | None = None,
        status: ReportStatus | None = None,
        excel_path: str | None = None,
        error_message: str | None = None,
        confirmed_at: datetime | None = None,
    ) -> Report | None:
        with self.session() as session:
            report = session.get(Report, report_id)
            if report is None:
                return None
            if transcription is not None:
                report.transcription = transcription
            if structured_json is not None:
                report.structured_json = structured_json
            if status is not None:
                report.status = status.value
            if excel_path is not None:
                report.excel_path = excel_path
            if error_message is not None:
                report.error_message = error_message
            if confirmed_at is not None:
                report.confirmed_at = confirmed_at
            report.updated_at = utcnow()
            session.flush()
            session.refresh(report)
            session.expunge(report)
            return report

    def get_report(self, report_id: int) -> Report | None:
        with self.session() as session:
            report = session.get(Report, report_id)
            if report is None:
                return None
            session.expunge(report)
            return report

    def get_report_for_user(self, report_id: int, telegram_id: int) -> Report | None:
        with self.session() as session:
            stmt = (
                select(Report)
                .join(User)
                .where(Report.id == report_id, User.telegram_id == telegram_id)
            )
            report = session.scalar(stmt)
            if report is None:
                return None
            session.expunge(report)
            return report

    def get_reports_for_user(self, telegram_id: int, limit: int = 10) -> list[Report]:
        with self.session() as session:
            stmt = (
                select(Report)
                .join(User)
                .where(User.telegram_id == telegram_id)
                .order_by(Report.created_at.desc())
                .limit(limit)
            )
            reports = list(session.scalars(stmt))
            for report in reports:
                session.expunge(report)
            return reports

    def get_active_report_for_user(self, telegram_id: int) -> Report | None:
        active = {
            ReportStatus.RECEIVED.value,
            ReportStatus.TRANSCRIBED.value,
            ReportStatus.EXTRACTED.value,
            ReportStatus.AWAITING_CONFIRMATION.value,
            ReportStatus.CONFIRMED.value,
        }
        with self.session() as session:
            stmt = (
                select(Report)
                .join(User)
                .where(User.telegram_id == telegram_id, Report.status.in_(active))
                .order_by(Report.created_at.desc())
            )
            report = session.scalars(stmt).first()
            if report is None:
                return None
            session.expunge(report)
            return report


_db: Database | None = None


def init_database(database_url: str) -> Database:
    global _db
    _db = Database(database_url)
    _db.create_tables()
    return _db


def get_database() -> Database:
    if _db is None:
        raise RuntimeError("Database has not been initialized. Call init_database first.")
    return _db


@asynccontextmanager
async def lifespan_db(database_url: str) -> AsyncIterator[Database]:
    db = init_database(database_url)
    try:
        yield db
    finally:
        db.engine.dispose()
