from app.adapters.ai.base import (
    AIClient,
    GeneratedVariant,
    GenerationBrief,
    GenerationResult,
)


class MockAIClient(AIClient):
    name = "mock"

    async def generate_variants(self, brief: GenerationBrief) -> GenerationResult:
        drivers = brief.top_performing_drivers or ["Curiosity"]
        variants: list[GeneratedVariant] = []
        for i in range(brief.variants_requested):
            label = chr(ord("A") + i)
            driver = drivers[i % len(drivers)]
            variants.append(
                GeneratedVariant(
                    subject=f"[mock] {brief.campaign_name} - variant {label}",
                    preheader=f"Preheader {label}",
                    body_html=(
                        f"<p>Hi {{{{first_name}}}}, variant {label} from "
                        f"{brief.campaign_name}</p><p>{{{{unsub_url}}}}</p>"
                    ),
                    body_text=(
                        f"Hi {{{{first_name}}}}, variant {label} from "
                        f"{brief.campaign_name}\n\n{{{{unsub_url}}}}"
                    ),
                    emotional_driver=driver,
                    variant_label=label,
                )
            )
        return GenerationResult(
            variants=variants,
            prompt_version=brief.prompt_version,
            model="mock",
            raw_usage={},
        )
