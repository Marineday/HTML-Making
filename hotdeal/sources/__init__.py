from .base import Source, SourceError
from .html_list import HtmlListSource
from .rss import RssSource

__all__ = ["Source", "SourceError", "HtmlListSource", "RssSource", "build"]


def build(config, client):
    """SourceConfig -> Source 인스턴스."""
    if config.type == "rss":
        return RssSource(config, client)
    if config.type == "html_list":
        return HtmlListSource(config, client)
    raise SourceError(f"알 수 없는 소스 타입: {config.type}")
