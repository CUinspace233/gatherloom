from alembic import context
from sqlalchemy import Connection

from app.db import DATABASE_URL, engine
from app.models import Base


def include_object(
    _object: object, name: str | None, type_: str, _reflected: bool, _compare: object
) -> bool:
    # LangGraph owns its checkpoint tables (PostgresSaver.setup at worker start).
    return not (type_ in ("table", "index") and (name or "").startswith("checkpoint"))


def migrate(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=DATABASE_URL, target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied_connection = context.config.attributes.get("connection")
    if supplied_connection is not None:
        migrate(supplied_connection)
    else:
        with engine.connect() as connection:
            migrate(connection)
