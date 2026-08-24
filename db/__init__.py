"""Package DB — tránh import vòng (db.init ↔ db_utils). Import trực tiếp: ``from db.init import ...``."""

__all__ = ['init_db', 'init_db_columns', 'migrate_database', 'migrate_all_databases']


def __getattr__(name: str):
    if name in __all__:
        from db import init as _init
        return getattr(_init, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
