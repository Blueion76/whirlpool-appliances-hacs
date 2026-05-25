"""Constants for the Whirlpool Appliances-derived integration."""
from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_REGION, CONF_USERNAME

DOMAIN = "whirlpool_appliances"
CONF_BRAND = "brand"
CONF_ENVIRONMENT = "environment"
CONF_BASE_URL = "base_url"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXPOSE_RAW_SENSORS = "expose_raw_sensors"
CONF_ENABLE_CONTROL_ENTITIES = "enable_control_entities"

DEFAULT_REGION = "US"
DEFAULT_BRAND = "Whirlpool"
DEFAULT_ENVIRONMENT = "prod"
DEFAULT_SCAN_INTERVAL = 60

SUPPORTED_REGIONS = ["US", "EU"]
SUPPORTED_BRANDS = ["Whirlpool", "Maytag", "KitchenAid", "Consul", "JennAir", "Amana"]
SUPPORTED_ENVIRONMENTS = ["prod", "stage", "qa", "dev", "test", "custom"]

# URLs observed in Whirlpool Android 6.8.3 DEX strings. Only prod/custom should be used normally.
BASE_URLS: dict[tuple[str, str], str] = {
    ("US", "prod"): "https://api.whrcloud.com",
    ("EU", "prod"): "https://prod-api.whrcloud.eu",
    ("US", "stage"): "https://stage-api.whrcloud.com",
    ("EU", "stage"): "https://api-stg.whrcloud.eu",
    ("US", "qa"): "https://qa-api.whrcloud.com",
    ("EU", "qa"): "https://api-test.whrcloud.eu",
    ("US", "dev"): "https://dev-api.whrcloud.com",
    ("US", "test"): "https://api-test.whrcloud.com",
}

# OAuth clients from the Android app / decrypted Data.json. These are public mobile app client values.
CLIENT_CREDENTIALS: dict[tuple[str, str], tuple[str, str]] = {
    ("US", "Whirlpool"): ("whirlpool_android_v2", "rMVCgnKKhIjoorcRa7cpckh5irsomybd4tM9Ir3QxJxQZlzgWSeWpkkxmsRg1PL-"),
    ("US", "Maytag"): ("maytag_android_v2", "ULTqdvvqK0O9XcSLO3nA2tJDTLFKxdaaeKrimPYdXvnLX_yUtPhxovESldBId0Tf"),
    ("US", "KitchenAid"): ("kitchenaid_android_v2", "jd15ExiJdEt8UgLWBslwkzkQkmRGCR9lVSgeaqcPmFZQc9pgxtpjmaPSw3g-aRXG"),
    ("EU", "Whirlpool"): ("whirlpool_emea_android_v2", "90_3TBRfXfcdCYJj6L5BThEqOBZNkEchrTPT7loqm0gBS_tyeFIIEv47mmYTZkb6"),
    ("EU", "KitchenAid"): ("kitchenaid_android_stg", "Dn-ukFAFoSWOnB9nVm7Y2DDj4Gs9Bocm6aOkhy0mdNGBj5RcoLkRfCXujuxpKrqF2w15sl1tI45JXwK5Zi4saw"),
    ("US", "Consul"): ("consul_lar_android_v1", "xfPIj2fqhHXlK4bz2oPToDX5E0zHZ409ZLY6ZHiU3p_jh4wv_Ycg8haUhnB6yXuA"),
    ("EU", "Consul"): ("consul_lar_android_v1", "xfPIj2fqhHXlK4bz2oPToDX5E0zHZ409ZLY6ZHiU3p_jh4wv_Ycg8haUhnB6yXuA"),
}

IOT_ENDPOINTS = {
    "US": "wt.applianceconnect.net",
    "EU": "wt-eu.applianceconnect.net",
}

AWS_REGIONS = {
    "US": "us-east-2",
    # Whirlpool EMEA endpoint has historically still used us-east-2 identity flows in app builds.
    # Override this in code if future APK Data.json values prove otherwise.
    "EU": "us-east-2",
}

# The Android app uses okhttp for auth/API requests. Some endpoints are picky about this header.
USER_AGENT = "okhttp/3.12.0"

DATA_CLIENT = "client"
DATA_COORDINATOR = "coordinator"
