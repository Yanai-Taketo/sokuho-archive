"""sokuho-archive -- NHKニュース速報を保存・記録するアーカイバ.

An archiver for NHK's Japanese breaking-news ("ニュース速報") XML feed.
It polls the feed, keeps every distinct document verbatim, and records each
flash's appearance, revision and disappearance in an append-only log.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
