def matches(segment: dict | None, recipient_attrs: dict, vertical: str | None = None) -> bool:
    """Check whether a recipient matches a workflow's entry_segment.

    For v1, segment supports a tiny DSL:
      - `null` or `{}` -> matches everything (no filter)
      - `{"vertical": "auto"}` -> recipient.vertical == "auto"
      - `{"attributes.state": "TX"}` -> recipient.attributes["state"] == "TX"
      - `{"attributes.age_band": ["35-44", "45-54"]}` -> list membership
      - Multiple keys -> all must match (implicit AND)

    Top-level keys can be:
      - "vertical" -> match against the `vertical` arg
      - "attributes.<key>" -> walk recipient_attrs by the dotted suffix

    Returns True iff every key matches; False otherwise.
    """
    if not segment:
        return True
    for key, expected in segment.items():
        if key == "vertical":
            actual = vertical
        elif key.startswith("attributes."):
            attr_key = key.split(".", 1)[1]
            actual = recipient_attrs.get(attr_key) if isinstance(recipient_attrs, dict) else None
        else:
            return False
        if isinstance(expected, list):
            if actual not in expected:
                return False
        else:
            if actual != expected:
                return False
    return True
