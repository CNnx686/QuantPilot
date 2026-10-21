"""Restricted research specifications and local template suggestions."""
import re
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(default="趋势基线研究", min_length=1, max_length=100)
    pairs: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"], min_length=1, max_length=2)
    start: date = date(2025, 1, 1)
    end: date = date(2025, 4, 1)
    wallet: float = Field(default=10000, gt=0, le=1e9)
    stake: float = Field(default=1000, gt=0, le=1e9)
    fee: float = Field(default=0.001, ge=0, le=0.1)
    strategy: str = "SmaTrend"
    dataset_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_research(self):
        if self.strategy not in {"SmaTrend", "FreqaiTrend"}:
            raise ValueError("Select SmaTrend or FreqaiTrend")
        if self.strategy == "FreqaiTrend" and (self.start < date(2025, 4, 1) or self.end > date(2025, 10, 1)):
            raise ValueError("FreqAI uses development/validation [2025-04-01, 2025-10-01); final test is reserved")
        if len(set(self.pairs)) != len(self.pairs) or any(p not in {"BTC/USDT", "ETH/USDT"} for p in self.pairs):
            raise ValueError("Select unique BTC/USDT and/or ETH/USDT pairs")
        if not 0 < (self.end - self.start).days <= 366:
            raise ValueError("end must follow start by at most 366 days (end is exclusive)")
        if self.stake > self.wallet:
            raise ValueError("stake must not exceed wallet")
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name must not be blank")
        return self




class ResearchSpec(ExperimentInput):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    schema_version: Literal["1"] = "1"
    timeframe: Literal["1h"] = "1h"
    hypothesis: str = Field(default="Evaluate the supported trend template on development data", min_length=1, max_length=2000)
    dataset_id: str = Field(min_length=1, max_length=100)
    start: date = Field(default=date(2025, 4, 1), strict=False)
    end: date = Field(default=date(2025, 7, 1), strict=False)

    @field_validator("start", "end", mode="before")
    @classmethod
    def iso_date(cls, value):
        if isinstance(value, date):
            return value
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Use an ISO date YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def development_only(self):
        if not date(2025, 4, 1) <= self.start < self.end <= date(2025, 10, 1):
            raise ValueError("Research is limited to development/validation [2025-04-01, 2025-10-01)")
        if not self.hypothesis.strip() or not self.dataset_id.strip():
            raise ValueError("hypothesis and dataset_id must not be blank")
        return self

    def experiment_config(self):
        return self.model_dump(mode="json", exclude={"name", "schema_version", "timeframe", "hypothesis"})


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    spec: ResearchSpec
    source: Literal["manual", "local_template", "llm"] = "manual"
    prompt: str = Field(default="", max_length=2000)
    suggestion_id: str | None = Field(default=None, max_length=100)


class EditInput(DraftInput):
    revision: int = Field(ge=1)


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=1)
    spec: ResearchSpec


class SuggestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=2000)
    provider: Literal["local_template", "llm"] = "local_template"


def suggest(prompt, dataset_id):
    """Conservative keyword grammar: unknown requirements are rejected, never ignored."""
    original = prompt.strip()
    remaining = original.lower()
    phrases = ["btc/usdt", "eth/usdt", "smatrend", "freqaitrend", "lightgbm", "baseline", "validation",
               "development", "research", "strategy", "trend", "hourly", "sma", "freqai", "btc", "eth",
               "usdt", "1h", "and", "with", "use", "test", "please", "ml", "a", "the",
               "比特币", "以太坊", "趋势", "基线", "策略", "研究", "开发期", "验证期", "小时", "一小时", "回测", "机器学习", "使用", "和", "请"]
    # Latin tokens use boundaries, so a supported word cannot hide an unsupported ask.
    for token in phrases:
        pattern = r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])" if token.isascii() else re.escape(token)
        remaining = re.sub(pattern, " ", remaining)
    if not original or re.sub(r"[\s,，。.!！、;；+/-]", "", remaining):
        raise ValueError("Unsupported request. Local templates understand only BTC/ETH, 1h, SMA/FreqAI trend and development/validation; edit exact dates and costs manually.")
    lowered = original.lower()
    ml = any(t in lowered for t in ("freqai", "lightgbm", "机器学习")) or bool(re.search(r"\bml\b", lowered))
    if ml and ("sma" in lowered):
        raise ValueError("Choose one strategy template")
    pairs = []
    if "btc" in lowered or "比特币" in lowered:
        pairs.append("BTC/USDT")
    if "eth" in lowered or "以太坊" in lowered:
        pairs.append("ETH/USDT")
    validation = "validation" in lowered or "验证期" in lowered
    return ResearchSpec(dataset_id=dataset_id, strategy="FreqaiTrend" if ml else "SmaTrend",
                        pairs=pairs or ["BTC/USDT", "ETH/USDT"],
                        start="2025-07-01" if validation else "2025-04-01",
                        end="2025-10-01" if validation else "2025-07-01")
