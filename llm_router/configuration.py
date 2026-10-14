"""Deployment configuration shared by the server and migration command."""
from sqlalchemy.engine import make_url


def database_url(value):
    url = make_url(value)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)
