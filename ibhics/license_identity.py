"""Dependency-free cluster identity normalization shared by Manager and vendor tools."""


def cluster_key(value: str) -> str:
    """Compare cluster IDs in either form: the UUID or IBHX-XXXX-... shown on the page."""
    text = str(value).strip().lower()
    if text.startswith("ibhx-"):
        text = text[5:]
    return text.replace("-", "")
