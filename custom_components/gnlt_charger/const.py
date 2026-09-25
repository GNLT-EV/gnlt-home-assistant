"""Constants of the GNLT charger integration."""

from __future__ import annotations

DOMAIN = "gnlt_charger"

CONF_IDENTITY = "identity"
CONF_PORT = "port"
CONF_PRESET = "preset"  # "1_7" = 1 phase 7 kW, see protocol.AC_PRESETS
CONF_AUTO_START = "auto_start"
CONF_RESUME = "resume_after_power_loss"
CONF_METER_INTERVAL = "meter_interval"

# Schedule - one window, changed from the device page (time/select entities).
CONF_SCHEDULE = "schedule"
CONF_SCHEDULE_START = "schedule_start"  # "HH:MM"
CONF_SCHEDULE_END = "schedule_end"
CONF_SCHEDULE_DAYS = "schedule_days"  # all | weekdays | weekends
CONF_SCHEDULE_STOP = "schedule_stop_at_end"

# Tariff - set once in the settings of the charger.
CONF_PRICE = "price"
CONF_NIGHT_PRICE = "night_price"
CONF_NIGHT_START = "night_start"
CONF_NIGHT_END = "night_end"

DEFAULT_PORT = 9000
DEFAULT_PRESET = "1_7"
DEFAULT_METER_INTERVAL = 10
DEFAULT_SCHEDULE_START = "23:00"
DEFAULT_SCHEDULE_END = "07:00"
DEFAULT_NIGHT_START = "23:00"
DEFAULT_NIGHT_END = "07:00"

# The path does not matter to the server; "/ocpp/" keeps the address familiar
# to people who connected chargers to the GNLT platform before.
OCPP_PATH = "/ocpp/"

STORAGE_KEY = f"{DOMAIN}.chargers"
STORAGE_VERSION = 1

PLATFORMS = ["binary_sensor", "button", "number", "select", "sensor", "switch", "time"]

OCPP_STATUSES = [
    "available",
    "preparing",
    "charging",
    "suspendedev",
    "suspendedevse",
    "finishing",
    "reserved",
    "unavailable",
    "faulted",
]

OCPP_ERRORS = [
    "noerror",
    "connectorlockfailure",
    "evcommunicationerror",
    "groundfailure",
    "hightemperature",
    "internalerror",
    "locallistconflict",
    "othererror",
    "overcurrentfailure",
    "overvoltage",
    "powermeterfailure",
    "powerswitchfailure",
    "readerfailure",
    "resetfailure",
    "undervoltage",
    "weaksignal",
]
