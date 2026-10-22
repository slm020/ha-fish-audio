"""Constants for the FishAudio integration."""

from typing import Literal

DOMAIN = "fish_audio"

CONF_USER_ID: Literal["user_id"] = "user_id"
CONF_VOICE_ID: Literal["voice_id"] = "voice_id"
CONF_BACKEND: Literal["backend"] = "backend"
CONF_SELF_ONLY: Literal["self_only"] = "self_only"
CONF_SORT_BY: Literal["sort_by"] = "sort_by"
CONF_LATENCY: Literal["latency"] = "latency"
CONF_SPEED: Literal["speed"] = "speed"
CONF_FORMAT: Literal["format"] = "format"
CONF_MP3_BITRATE: Literal["mp3_bitrate"] = "mp3_bitrate"
CONF_OPUS_BITRATE: Literal["opus_bitrate"] = "opus_bitrate"
CONF_CHUNK_LENGTH: Literal["chunk_length"] = "chunk_length"
CONF_TITLE: Literal["title"] = "title"

DEVELOPER_ID = "1e9f9baadce144f5b16dd94cbc0314c8"

TTS_SUPPORTED_LANGUAGES = [
    "Any",
    "en",
    "zh",
    "de",
    "ja",
    "ar",
    "fr",
    "es",
    "ko",
]


BACKEND_MODELS = [
    "s2.1-pro",
    "s2.1-pro-free",
    "s2-pro",
    "s1",
    "speech-1.5",
    "speech-1.6",
]
SORT_BY_OPTIONS = ["task_count", "score", "created_at"]
LATENCY_OPTIONS = ["normal", "balanced"]
# Fish Audio API-supported output and encoding choices. PCM is intentionally
# omitted because it has no container/header and is less compatible with HA players.
AUDIO_FORMAT_OPTIONS = ["mp3", "opus", "wav"]
MP3_BITRATE_OPTIONS = [64, 128, 192]
OPUS_BITRATE_OPTIONS = [24, 32, 48, 64]
CHUNK_LENGTH_OPTIONS = [100, 150, 200, 250, 300]

# Speech speed multiplier accepted by the Fish Audio API (1.0 = normal speed).
DEFAULT_SPEED = 1.0
MIN_SPEED = 0.5
MAX_SPEED = 2.0
SPEED_STEP = 0.05

SIGNUP_URL = "https://fish.audio/"
BILLING_URL = "https://fish.audio/app/billing/"
API_KEYS_URL = "https://fish.audio/app/api-keys/"
