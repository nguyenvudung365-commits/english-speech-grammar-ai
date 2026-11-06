from alembic import context

from src.api.config import Settings
from src.api.db import Database
from src.api.models import Base


def run_online(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url, target_metadata=Base.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = context.config.attributes.get("connection")
    if supplied is not None:
        run_online(supplied)
    else:
        database = Database(Settings().database_url)
        try:
            with database.engine.connect() as connection:
                run_online(connection)
        finally:
            database.engine.dispose()
