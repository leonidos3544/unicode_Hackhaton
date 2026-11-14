from datetime import timedelta

# Six-hour refresh cycle; keep a limited cached window during temporary source outages.
SOURCE_FRESHNESS = timedelta(hours=36)
