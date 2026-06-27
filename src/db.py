"""
Supabase client singleton. All modules import get_client() from here.

Uses SUPABASE_SERVICE_KEY (full access) for backend writes.
Dashboard JS uses the anon key directly — never import service key client-side.

Loads .env automatically so scripts work without manual export.
"""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

# Load .env from project root regardless of where script is invoked from
load_dotenv(Path(__file__).parent.parent / ".env")


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

    if not url or not key:
        raise EnvironmentError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env"
        )

    return create_client(url, key)


def get_anon_client() -> Client:
    """Read-only client using the anon key — mirrors what the dashboard JS uses."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    return create_client(url, key)
