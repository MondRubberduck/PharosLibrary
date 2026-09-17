"""asset_service - local, non-destructive asset registry (Pharos).

Sprint scope:
    db.py            SQLite schema, FTS5 search, migrations
    uasset_parser.py binary Unreal .uasset/.umap header parser

Everything in this package treats the canonical asset roots as read-only.
"""

__version__ = "0.1.0"
