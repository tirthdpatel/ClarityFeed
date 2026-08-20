# Migration Strategy — Alembic for Future Phases

## Current Approach (Phase 1)

Phase 1 uses `Base.metadata.create_all()` to create tables on startup. This is appropriate for the initial deployment where no existing data needs to be preserved.

## Future Approach (Phase 2+)

Starting in Phase 2, database schema changes should use [Alembic](https://alembic.sqlalchemy.org/) for versioned migrations.

### Setup Steps

1. Add `alembic` to `requirements.txt`:
   ```
   alembic==1.13.1
   ```

2. Initialize Alembic:
   ```bash
   alembic init alembic
   ```

3. Configure `alembic.ini` to use `DATABASE_URL` from environment:
   ```python
   # In alembic/env.py
   from config.settings import settings
   config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
   ```

4. Point Alembic at the ORM models:
   ```python
   # In alembic/env.py
   from backend.database.orm_models import Base
   target_metadata = Base.metadata
   ```

### Migration Workflow

```bash
# Generate a migration after changing ORM models
alembic revision --autogenerate -m "add new_column to raw_articles"

# Apply migrations
alembic upgrade head

# Rollback one step
alembic downgrade -1
```

### Neon Compatibility

Alembic works with Neon PostgreSQL without modification. The same `DATABASE_URL` connection string (with `?sslmode=require`) works for both the application and Alembic. Ensure the Neon database is awake before running migrations (send a simple query first if needed).

### Deployment

On Render, the build command can be extended to run migrations:
```
pip install -r requirements.txt && alembic upgrade head
```

This ensures migrations run before the application starts.
