"""
Supabase client singleton. All modules import get_client() from here.

Uses SUPABASE_SERVICE_KEY (full access) for backend writes.
Dashboard JS uses the anon key directly — never import service key client-side.
"""

from functools import lru_cache

from supabase import Client, create_client

from src.config import SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY


@lru_cache(maxsize=1)
def get_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise EnvironmentError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env"
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def get_anon_client() -> Client:
    """Read-only client using the anon key — mirrors what the dashboard JS uses."""
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
