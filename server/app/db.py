from collections.abc import Iterator
from os import environ

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = environ.get(
    "GATHERLOOM_DATABASE_URL",
    "postgresql+psycopg://gatherloom:gatherloom-local@127.0.0.1:54329/gatherloom",
)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, hide_parameters=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db
