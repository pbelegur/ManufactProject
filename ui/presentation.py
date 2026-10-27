"""Human labels; runtime names and UUIDs remain the stable execution identity."""
BACKEND_LABELS = {"always-on": "Always on", "knative": "Autoscaling · scale to zero"}


def clean_display_name(value):
    value = " ".join(value.split())
    if not value or len(value) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Display name must contain 1–80 readable characters")
    return value


def validation_message(error):
    if hasattr(error, "errors"):
        return "; ".join(" ".join(str(p).replace("_", " ") for p in item["loc"]) + ": " + item["msg"]
                         for item in error.errors(include_input=False))
    return str(error)
