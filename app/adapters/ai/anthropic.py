import json
from pathlib import Path

from anthropic import AsyncAnthropic

from app.adapters.ai.base import (
    AIClient,
    GeneratedVariant,
    GenerationBrief,
    GenerationResult,
)


class AnthropicClient(AIClient):
    name = "anthropic"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._client: AsyncAnthropic | None = None

    @property
    def client(self) -> AsyncAnthropic:
        if self._client is None:
            self._client = AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        return self._client

    async def generate_variants(self, brief: GenerationBrief) -> GenerationResult:
        prompt_path = (
            Path(__file__).parents[3]
            / "prompts"
            / brief.prompt_version
            / "variant_generation.md"
        )
        template = prompt_path.read_text() if prompt_path.exists() else _DEFAULT_PROMPT

        user_prompt = template.format(
            tenant=brief.tenant,
            campaign_name=brief.campaign_name,
            campaign_brief=brief.campaign_brief,
            vertical=brief.vertical,
            cohort_key=brief.cohort.cohort_key,
            dimensions=json.dumps(brief.cohort.dimensions),
            recipient_count=brief.cohort.recipient_count,
            variants_requested=brief.variants_requested,
            brand_voice=brief.brand_voice_guide or "(no guide provided)",
            merge_tags=", ".join(brief.merge_tags_available) or "{{first_name}}, {{unsub_url}}",
            drivers=", ".join(brief.top_performing_drivers),
        )

        resp = await self.client.messages.create(
            model=self._settings.anthropic_generation_model,
            max_tokens=4096,
            system="You are a marketing email copywriter. Output ONLY a JSON array of variant objects — no prose.",
            messages=[{"role": "user", "content": user_prompt}],
        )

        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json\n"):
                text = text[5:]
            text = text.rstrip("`").strip()

        data = json.loads(text)
        variants = [
            GeneratedVariant(
                subject=v["subject"],
                preheader=v.get("preheader", ""),
                body_html=v["body_html"],
                body_text=v["body_text"],
                emotional_driver=v.get("emotional_driver", ""),
                variant_label=chr(ord("A") + i),
            )
            for i, v in enumerate(data[: brief.variants_requested])
        ]
        return GenerationResult(
            variants=variants,
            prompt_version=brief.prompt_version,
            model=self._settings.anthropic_generation_model,
            raw_usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
        )


_DEFAULT_PROMPT = """You are writing email variants for {tenant}, vertical {vertical}, campaign '{campaign_name}'.
Brief: {campaign_brief}
Cohort: {cohort_key} ({dimensions}), recipient count ~{recipient_count}.
Brand voice: {brand_voice}
Available merge tags: {merge_tags}
Known high-performing emotional drivers: {drivers}
Compliance: no fake urgency, no false savings claims, must include {{unsub_url}} in every body.
Produce {variants_requested} variants as a JSON array of objects with keys: subject, preheader, body_html, body_text, emotional_driver. Return ONLY valid JSON, no prose."""
