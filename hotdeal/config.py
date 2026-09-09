"""JSON 설정 로드와 검증.

의존성을 0으로 유지하려고 YAML 대신 JSON 을 쓴다. 대신 `//` 로 시작하는
줄 주석은 허용해서 설정 파일에 설명을 남길 수 있게 했다.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """설정 파일이 잘못됐을 때. 메시지에 어느 키가 문제인지 담는다."""


@dataclass(slots=True)
class SourceConfig:
    name: str
    type: str
    url: str
    enabled: bool = True
    # html_list 전용
    link_pattern: str = ""
    base_url: str = ""
    # 공통
    category: str | None = None
    max_items: int = 50


@dataclass(slots=True)
class FilterConfig:
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    require_all_keywords: bool = False
    min_price_krw: int | None = None
    max_price_krw: int | None = None
    max_age_minutes: int | None = 180


@dataclass(slots=True)
class SenderConfig:
    type: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Config:
    sources: list[SourceConfig]
    senders: list[SenderConfig]
    filters: FilterConfig = field(default_factory=FilterConfig)
    db_path: str = "hotdeal.db"
    poll_interval_seconds: int = 300
    max_sends_per_run: int = 10
    send_gap_seconds: float = 1.5
    dedup_retention_days: int = 14
    seed_on_first_run: bool = True
    user_agent: str = ""
    respect_robots: bool = True
    min_interval_per_host: float = 2.0
    log_level: str = "INFO"

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

    @property
    def enabled_senders(self) -> list[SenderConfig]:
        return [s for s in self.senders if s.enabled]


def _expand_env(value: Any) -> Any:
    """문자열 안의 ${ENV_VAR} 를 환경변수로 치환한다.

    토큰을 설정 파일에 평문으로 적지 않게 하려는 것이다. 참조한 환경변수가
    없으면 즉시 실패시킨다 — 빈 토큰으로 조용히 돌다가 401 을 맞는 것보다 낫다.
    """
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"환경변수 {name} 가 설정되지 않았습니다 (설정 파일이 ${{{name}}} 를 참조함)")
            return os.environ[name]

        return _ENV_REF.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"설정 파일이 없습니다: {path}. config.example.json 을 복사해서 만드세요.")

    text = _LINE_COMMENT.sub("", path.read_text(encoding="utf-8"))
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} JSON 파싱 실패: {exc}") from exc

    return from_dict(_expand_env(raw))


def from_dict(raw: dict[str, Any]) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("설정 최상위는 객체(JSON object)여야 합니다")

    sources = [_source(item, index) for index, item in enumerate(raw.get("sources", []))]
    senders = [_sender(item, index) for index, item in enumerate(raw.get("senders", []))]

    if not sources:
        raise ConfigError("sources 가 비어 있습니다. 최소 한 개의 수집 소스가 필요합니다.")
    if not senders:
        raise ConfigError("senders 가 비어 있습니다. 최소 한 개의 전송 어댑터가 필요합니다.")

    filters_raw = raw.get("filters", {})
    if not isinstance(filters_raw, dict):
        raise ConfigError("filters 는 객체여야 합니다")

    known = {f.name for f in FilterConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(filters_raw) - known
    if unknown:
        raise ConfigError(f"filters 에 알 수 없는 키: {sorted(unknown)}")

    config = Config(
        sources=sources,
        senders=senders,
        filters=FilterConfig(**filters_raw),
        db_path=raw.get("db_path", "hotdeal.db"),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 300)),
        max_sends_per_run=int(raw.get("max_sends_per_run", 10)),
        send_gap_seconds=float(raw.get("send_gap_seconds", 1.5)),
        dedup_retention_days=int(raw.get("dedup_retention_days", 14)),
        seed_on_first_run=bool(raw.get("seed_on_first_run", True)),
        user_agent=raw.get("user_agent", ""),
        respect_robots=bool(raw.get("respect_robots", True)),
        min_interval_per_host=float(raw.get("min_interval_per_host", 2.0)),
        log_level=str(raw.get("log_level", "INFO")).upper(),
    )

    if config.poll_interval_seconds < 60:
        raise ConfigError("poll_interval_seconds 는 60 이상이어야 합니다. 그보다 잦으면 소스 사이트에서 차단됩니다.")
    if config.max_sends_per_run < 1:
        raise ConfigError("max_sends_per_run 은 1 이상이어야 합니다")
    return config


def _source(item: Any, index: int) -> SourceConfig:
    if not isinstance(item, dict):
        raise ConfigError(f"sources[{index}] 는 객체여야 합니다")
    for key in ("name", "type", "url"):
        if not item.get(key):
            raise ConfigError(f"sources[{index}] 에 '{key}' 가 필요합니다")
    if item["type"] not in ("rss", "html_list"):
        raise ConfigError(f"sources[{index}].type 은 'rss' 또는 'html_list' 여야 합니다 (받은 값: {item['type']})")
    if item["type"] == "html_list" and not item.get("link_pattern"):
        raise ConfigError(f"sources[{index}] 는 html_list 이므로 'link_pattern' 정규식이 필요합니다")
    return SourceConfig(
        name=item["name"],
        type=item["type"],
        url=item["url"],
        enabled=bool(item.get("enabled", True)),
        link_pattern=item.get("link_pattern", ""),
        base_url=item.get("base_url", ""),
        category=item.get("category"),
        max_items=int(item.get("max_items", 50)),
    )


def _sender(item: Any, index: int) -> SenderConfig:
    if not isinstance(item, dict):
        raise ConfigError(f"senders[{index}] 는 객체여야 합니다")
    if not item.get("type"):
        raise ConfigError(f"senders[{index}] 에 'type' 이 필요합니다")
    options = {k: v for k, v in item.items() if k not in ("type", "enabled")}
    return SenderConfig(type=item["type"], enabled=bool(item.get("enabled", True)), options=options)
