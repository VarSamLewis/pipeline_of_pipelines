"""Run versioned PostgreSQL migrations using the application's engine."""

from __future__ import annotations

import models  # noqa: F401 - register SQLModel tables
from alembic import context
from db_ops import create_application_schema, get_engine
from sqlmodel import SQLModel


def run_migrations() -> None:
    create_application_schema(get_engine())
    connection = context.config.attributes.get("connection")
    if connection is None:
        with get_engine().connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=SQLModel.metadata,
                version_table_schema="pipeline",
            )
            with context.begin_transaction():
                context.run_migrations()
    else:
        context.configure(
            connection=connection,
            target_metadata=SQLModel.metadata,
            version_table_schema="pipeline",
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
