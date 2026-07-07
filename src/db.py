"""
Supabase client singleton. All modules import get_client() from here.

Uses SUPABASE_SERVICE_KEY (full access) for backend writes.
Dashboard JS uses the anon key directly — never import service key client-side.
"""

from functools import lru_cache
from typing import Any, cast

from src import config
from supabase import Client, create_client

# The Supabase SDK types APIResponse.data as List[JSON] — a broad recursive union
# that includes None, bool, int, float, etc. This alias + helper are the single
# authorised escape hatch: cast once here, get proper dict types everywhere else.
Rows = list[dict[str, Any]]


def rows(data: Any) -> Rows:
    """Cast Supabase .execute().data to a typed list of row dicts."""
    return cast(Rows, data or [])


@lru_cache(maxsize=1)
def get_client() -> Client:
    if not config.supabase.url or not config.supabase.service_key:
        raise EnvironmentError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env"
        )
    return create_client(config.supabase.url, config.supabase.service_key)


def get_anon_client() -> Client:
    """Read-only client using the anon key — mirrors what the dashboard JS uses."""
    return create_client(config.supabase.url, config.supabase.anon_key)
