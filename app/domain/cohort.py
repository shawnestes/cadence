def compute_cohort_key(vertical: str, dimensions: dict[str, str]) -> str:
    parts = [vertical]
    for k in sorted(dimensions.keys()):
        parts.append(dimensions[k] or "unknown")
    return ":".join(parts)


def derive_recipient_cohort(
    recipient_vertical: str,
    recipient_attrs: dict,
    dimensions: list[str],
) -> str:
    selected = {d: recipient_attrs.get(d, "unknown") for d in dimensions}
    return compute_cohort_key(recipient_vertical, selected)
