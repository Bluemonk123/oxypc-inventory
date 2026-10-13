"""Clean-up helpers for dealer fields that arrive from outside (bulk uploads,
Google Maps exports): State and Google CID.

State is a Master Dropdown value, so free text from a file is mapped onto the
configured option list. Google Maps exports regularly carry junk in the State
column ("7+ years in business", "Open 24 hours", a street name), so when the
cell is not a real state the PIN code decides instead.
"""
import re

# First digits of an Indian PIN code -> state/UT. Longer prefixes are checked
# first, so the 3-digit exceptions (Goa, Uttarakhand, ...) win over their
# surrounding 2-digit region; a None entry means the prefix spans two states
# and the PIN code cannot decide.
_PIN_PREFIX_STATE = {
    "11": "Delhi",
    "12": "Haryana", "13": "Haryana",
    "14": "Punjab", "15": "Punjab", "160": None, "16": "Punjab",  # 160xxx: Chandigarh or Mohali
    "17": "Himachal Pradesh",
    "18": "Jammu and Kashmir", "190": "Jammu and Kashmir", "191": "Jammu and Kashmir",
    "192": "Jammu and Kashmir", "193": "Jammu and Kashmir", "194": "Ladakh",
    "248": "Uttarakhand", "249": "Uttarakhand", "263": "Uttarakhand",
    "20": "Uttar Pradesh", "21": "Uttar Pradesh", "22": "Uttar Pradesh",
    "23": "Uttar Pradesh", "24": "Uttar Pradesh", "25": "Uttar Pradesh",
    "26": "Uttar Pradesh", "27": "Uttar Pradesh",
    "28": "Uttar Pradesh",
    "30": "Rajasthan", "31": "Rajasthan", "32": "Rajasthan", "33": "Rajasthan", "34": "Rajasthan",
    "36": "Gujarat", "37": "Gujarat", "38": "Gujarat", "39": "Gujarat",
    "403": "Goa", "40": "Maharashtra", "41": "Maharashtra", "42": "Maharashtra",
    "43": "Maharashtra", "44": "Maharashtra",
    "45": "Madhya Pradesh", "46": "Madhya Pradesh", "47": "Madhya Pradesh", "48": "Madhya Pradesh",
    "49": "Chhattisgarh",
    "50": "Telangana",
    "51": "Andhra Pradesh", "52": "Andhra Pradesh", "53": "Andhra Pradesh",
    "56": "Karnataka", "57": "Karnataka", "58": "Karnataka", "59": "Karnataka",
    "60": "Tamil Nadu", "61": "Tamil Nadu", "62": "Tamil Nadu",
    "63": "Tamil Nadu", "64": "Tamil Nadu",
    "67": "Kerala", "68": "Kerala", "69": "Kerala",
    "70": "West Bengal", "71": "West Bengal", "72": "West Bengal", "73": "West Bengal",
    "74": "West Bengal",
    "75": "Odisha", "76": "Odisha", "77": "Odisha",
    "78": "Assam",
    "80": "Bihar", "81": "Bihar", "82": "Jharkhand", "83": "Jharkhand", "84": "Bihar", "85": "Bihar",
}

# Common spellings that are not the option text itself.
_STATE_ALIASES = {
    "new delhi": "Delhi", "nct of delhi": "Delhi", "delhi ncr": "Delhi",
    "orissa": "Odisha", "pondicherry": "Puducherry", "uttaranchal": "Uttarakhand",
    "j&k": "Jammu and Kashmir", "jammu & kashmir": "Jammu and Kashmir",
    "up": "Uttar Pradesh", "mp": "Madhya Pradesh", "hr": "Haryana",
}


def state_from_pincode(pincode) -> str | None:
    digits = re.sub(r"\D", "", str(pincode or ""))
    if len(digits) != 6:
        return None
    for n in (3, 2):
        if digits[:n] in _PIN_PREFIX_STATE:
            return _PIN_PREFIX_STATE[digits[:n]]  # None = prefix spans two states
    return None


def normalize_state(raw, pincode=None, options=()) -> str | None:
    """Map a free-text state onto the configured dropdown option (matched
    case-insensitively, with a few common aliases). When the text is not a
    state, fall back to the PIN code. Returns None when neither decides."""
    by_lower = {o.strip().lower(): o for o in options if o}
    text = re.sub(r"\s+", " ", str(raw or "")).strip().strip("-,. ").lower()
    if text:
        if text in by_lower:
            return by_lower[text]
        alias = _STATE_ALIASES.get(text)
        if alias:
            return by_lower.get(alias.lower(), alias)
    from_pin = state_from_pincode(pincode)
    if from_pin:
        return by_lower.get(from_pin.lower(), from_pin)
    return None


def parse_google_cid(raw) -> str | None:
    """Digits of a Google Maps CID from either the bare number or a
    https://maps.google.com/?cid=... link. An empty "?cid=" gives None."""
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.search(r"cid=(\d+)", s)
    if m:
        return m.group(1)
    if "cid=" in s:
        return None
    digits = s if s.isdigit() else ""
    return digits or None


def safe_external_url(raw) -> str | None:
    """An http(s) link for a stored website. Anything else (no scheme, or a
    javascript:/data: prefix from an imported file) becomes https://<rest>, so
    a dealer record can never turn into a script link on the page."""
    s = str(raw or "").strip()
    if not s:
        return None
    if re.match(r"(?i)^https?://", s):
        return s
    return "https://" + re.sub(r"(?i)^[a-z][a-z0-9+.-]*:/*", "", s)
