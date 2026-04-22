from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class CohortDescription:
    tenant: str
    vertical: str
    cohort_key: str
    dimensions: dict[str, str]
    recipient_count: int


@dataclass(slots=True)
class GenerationBrief:
    tenant: str
    campaign_name: str
    campaign_brief: str
    vertical: str
    cohort: CohortDescription
    variants_requested: int = 5
    brand_voice_guide: str = ""
    merge_tags_available: list[str] = field(default_factory=list)
    top_performing_drivers: list[str] = field(default_factory=lambda: [
        "Loss Aversion",
        "Comparison Empowerment",
        "FOMO",
        "Curiosity",
        "Deal Seeking",
    ])
    prompt_version: str = "v1"


@dataclass(slots=True)
class GeneratedVariant:
    subject: str
    preheader: str
    body_html: str
    body_text: str
    emotional_driver: str
    variant_label: str


@dataclass(slots=True)
class GenerationResult:
    variants: list[GeneratedVariant]
    prompt_version: str
    model: str
    raw_usage: dict = field(default_factory=dict)


class AIClient(ABC):
    name: str

    @abstractmethod
    async def generate_variants(self, brief: GenerationBrief) -> GenerationResult: ...
