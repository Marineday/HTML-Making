from .base import SendError, Sender, SendOutcome
from .console import ConsoleSender
from .discord import DiscordSender
from .kakao_bridge import KakaoBridgeSender
from .telegram import TelegramSender

__all__ = [
    "Sender",
    "SendError",
    "SendOutcome",
    "ConsoleSender",
    "DiscordSender",
    "KakaoBridgeSender",
    "TelegramSender",
    "build",
]

_REGISTRY = {
    "console": ConsoleSender,
    "telegram": TelegramSender,
    "discord": DiscordSender,
    "kakao_bridge": KakaoBridgeSender,
}


def build(config, client):
    """SenderConfig -> Sender 인스턴스."""
    try:
        cls = _REGISTRY[config.type]
    except KeyError:
        raise SendError(f"알 수 없는 sender 타입: {config.type}. 사용 가능: {sorted(_REGISTRY)}") from None
    return cls(config.options, client)
