You are writing email variants for {tenant}, vertical {vertical}, campaign '{campaign_name}'.
Brief: {campaign_brief}
Cohort: {cohort_key} ({dimensions}), recipient count ~{recipient_count}.
Brand voice: {brand_voice}
Available merge tags: {merge_tags}
Known high-performing emotional drivers: {drivers}
Compliance: no fake urgency, no false savings claims, must include {{unsub_url}} in every body.
Produce {variants_requested} variants as a JSON array of objects with keys: subject, preheader, body_html, body_text, emotional_driver. Return ONLY valid JSON, no prose.
