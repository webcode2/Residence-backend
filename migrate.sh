#!/bin/bash

# Default message
MESSAGE="auto migration"

# If an argument is provided, use it as the migration message
if [ ! -z "$1" ]; then
    MESSAGE="$1"
fi

echo "Generating migration: $MESSAGE"
./.venv/bin/alembic revision --autogenerate -m "$MESSAGE"

echo "Applying migration..."
./.venv/bin/alembic upgrade head

echo "Done!"
