"""Costanti dell'integrazione ZKTeco Access."""

DOMAIN = "zkteco_access"
DEFAULT_PORT = 8099
CONF_UNLOCK_SECONDS = "unlock_seconds"
CONF_AUX_SECONDS = "aux_seconds"
DEFAULT_AUX_SECONDS = 5

# categorie degli eventi inviate dall'add-on
EVENT_TYPES = ["granted", "denied", "unknown_card", "remote", "exit", "door", "alarm", "aux", "info"]
SIGNAL_EVENT = f"{DOMAIN}_event"
