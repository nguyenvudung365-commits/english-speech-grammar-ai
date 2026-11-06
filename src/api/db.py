from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


class Database:
    def __init__(self, url: str, schema: str | None = None):
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("This application requires PostgreSQL (psycopg), not SQLite")
        self.engine = create_engine(url, pool_pre_ping=True)
        if schema:
            if not schema.startswith("test_") or not schema[5:].isalnum():
                raise ValueError("Invalid test schema")

            @event.listens_for(self.engine, "connect")
            def search_path(connection, _):
                # Only server-generated test schema identifiers; never user input.
                previous = connection.autocommit
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute(f'SET search_path TO "{schema}"')
                connection.autocommit = previous

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
