"""TTS platform for the Fish Audio integration."""

import asyncio
from collections.abc import AsyncGenerator
import logging
from typing import Any, override

from fishaudio import TTSConfig
from fishaudio.exceptions import APIError, RateLimitError
from sentence_stream import SentenceBoundaryDetector

from homeassistant.components.tts import (
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
    TtsAudioType,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FishAudioConfigEntry
from .const import (
    CONF_BACKEND,
    CONF_LATENCY,
    CONF_FORMAT,
    CONF_MP3_BITRATE,
    CONF_OPUS_BITRATE,
    CONF_CHUNK_LENGTH,
    CONF_SPEED,
    CONF_VOICE_ID,
    AUDIO_FORMAT_OPTIONS,
    CHUNK_LENGTH_OPTIONS,
    DEFAULT_SPEED,
    DOMAIN,
    MAX_SPEED,
    MIN_SPEED,
    MP3_BITRATE_OPTIONS,
    OPUS_BITRATE_OPTIONS,
    TTS_SUPPORTED_LANGUAGES,
)
from .error import UnexpectedError

PARALLEL_UPDATES = 1
# How many sentences may be synthesised at once (current one + look-ahead).
MAX_CONCURRENT_SYNTHESIS = 2
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FishAudioConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Fish Audio TTS platform."""
    _LOGGER.debug("Setting up Fish Audio TTS platform")

    _LOGGER.debug("Entry: %s", entry)
    # Iterate over values
    for subentry in entry.subentries.values():
        _LOGGER.debug("Subentry: %s", subentry)
        if subentry.subentry_type != "tts":
            continue
        async_add_entities(
            [FishAudioTTSEntity(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class FishAudioTTSEntity(TextToSpeechEntity):
    """Fish Audio TTS entity."""

    _attr_has_entity_name = True
    _attr_supported_options = [CONF_VOICE_ID, CONF_BACKEND, CONF_LATENCY, CONF_SPEED]

    def __init__(self, entry: FishAudioConfigEntry, sub_entry: ConfigSubentry) -> None:
        """Initialize the TTS entity."""
        self.client = entry.runtime_data
        self.sub_entry = sub_entry
        self._attr_unique_id = sub_entry.subentry_id
        # Configured speed must be a default option so it is part of the TTS cache key.
        self._attr_default_options = {
            CONF_SPEED: sub_entry.data.get(CONF_SPEED, DEFAULT_SPEED)
        }
        title = sub_entry.title
        backend = sub_entry.data[CONF_BACKEND]
        self._attr_name = title

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sub_entry.subentry_id)},
            manufacturer="Fish Audio",
            model=backend,
            name=title,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    @override
    def default_language(self) -> str:
        """Return the default language."""
        return "en"

    @property
    @override
    def supported_languages(self) -> list[str]:
        """Return a list of supported languages."""
        return TTS_SUPPORTED_LANGUAGES

    def _resolve_params(self, options: dict[str, Any]) -> dict[str, Any]:
        """Validate options and return the keyword arguments for the Fish API."""
        voice_id = options.get(CONF_VOICE_ID, self.sub_entry.data.get(CONF_VOICE_ID))
        backend = options.get(CONF_BACKEND, self.sub_entry.data.get(CONF_BACKEND))
        latency = options.get(
            CONF_LATENCY, self.sub_entry.data.get(CONF_LATENCY, "balanced")
        )
        speed = options.get(
            CONF_SPEED, self.sub_entry.data.get(CONF_SPEED, DEFAULT_SPEED)
        )
        audio_format = options.get(
            CONF_FORMAT, self.sub_entry.data.get(CONF_FORMAT, "mp3")
        )
        mp3_bitrate = options.get(
            CONF_MP3_BITRATE, self.sub_entry.data.get(CONF_MP3_BITRATE, 64)
        )
        opus_bitrate = options.get(
            CONF_OPUS_BITRATE, self.sub_entry.data.get(CONF_OPUS_BITRATE, 32)
        )
        chunk_length = int(options.get(
            CONF_CHUNK_LENGTH, self.sub_entry.data.get(CONF_CHUNK_LENGTH, 100)
        ))
        mp3_bitrate = int(mp3_bitrate)
        opus_bitrate = int(opus_bitrate)

        if voice_id is None:
            raise ServiceValidationError("Voice ID not configured")
        if backend is None:
            raise ServiceValidationError("Backend model not configured")
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ServiceValidationError(
                f"Speed must be between {MIN_SPEED} and {MAX_SPEED}"
            )
        if audio_format not in AUDIO_FORMAT_OPTIONS:
            raise ServiceValidationError(f"Unsupported audio format: {audio_format}")
        if mp3_bitrate not in MP3_BITRATE_OPTIONS:
            raise ServiceValidationError(f"Unsupported MP3 bitrate: {mp3_bitrate}")
        if opus_bitrate not in OPUS_BITRATE_OPTIONS:
            raise ServiceValidationError(f"Unsupported Opus bitrate: {opus_bitrate}")
        if not min(CHUNK_LENGTH_OPTIONS) <= chunk_length <= max(CHUNK_LENGTH_OPTIONS):
            raise ServiceValidationError("Chunk length must be between 100 and 300")

        # The SDK only accepts format/latency/speed/reference_id/model as direct
        # keyword arguments. Bitrates and chunk length must go inside a TTSConfig.
        return {
            "reference_id": voice_id,
            "latency": latency,
            "speed": speed,
            "model": backend,
            "format": audio_format,
            "config": TTSConfig(
                format=audio_format,
                latency=latency,
                chunk_length=chunk_length,
                mp3_bitrate=mp3_bitrate,
                opus_bitrate=opus_bitrate,
            ),
        }

    @override
    async def async_get_tts_audio(
        self,
        message: str,
        language: str,
        options: dict[str, Any],
    ) -> TtsAudioType:
        """Load tts audio file from engine."""

        _LOGGER.debug("Getting TTS audio for %s", message)

        params = self._resolve_params(options)

        try:
            audio = await self.client.tts.convert(text=message, **params)
        except RateLimitError as err:
            _LOGGER.error("Fish Audio TTS rate limited: %s", err)
            raise HomeAssistantError(f"Rate limited: {err}") from err
        except APIError as err:
            _LOGGER.error("Fish Audio TTS request failed: %s", err)
            raise HomeAssistantError(f"TTS request failed: {err}") from err
        except Exception as err:
            raise UnexpectedError(err) from err

        return params["format"], audio

    @override
    async def async_stream_tts_audio(
        self, request: TTSAudioRequest
    ) -> TTSAudioResponse:
        """Generate speech from an incoming stream of text."""
        # Validate up front so configuration errors surface before streaming starts.
        params = self._resolve_params(request.options)
        return TTSAudioResponse(params["format"], self._stream_audio(request, params))

    async def _stream_audio(
        self, request: TTSAudioRequest, params: dict[str, Any]
    ) -> AsyncGenerator[bytes]:
        """Synthesize sentences concurrently, but yield their audio strictly in order.

        As soon as a sentence is complete it is sent to Fish Audio, even while an
        earlier sentence is still being streamed, so the next sentence's audio is
        already waiting when the previous one finishes.
        """
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_SYNTHESIS)
        order: asyncio.Queue[asyncio.Queue | Exception | None] = asyncio.Queue()
        tasks: list[asyncio.Task] = []

        feeder = asyncio.create_task(self._feed_sentences(request, params, order, semaphore, tasks))
        try:
            while True:
                item = await order.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                while True:
                    chunk = await item.get()
                    if chunk is None:
                        break
                    if isinstance(chunk, Exception):
                        raise chunk
                    yield chunk
        finally:
            feeder.cancel()
            for task in tasks:
                task.cancel()

    async def _feed_sentences(
        self,
        request: TTSAudioRequest,
        params: dict[str, Any],
        order: asyncio.Queue,
        semaphore: asyncio.Semaphore,
        tasks: list[asyncio.Task],
    ) -> None:
        """Split incoming text into sentences and start synthesising each at once."""
        boundary_detector = SentenceBoundaryDetector()

        def start(sentence: str) -> None:
            out: asyncio.Queue = asyncio.Queue()
            tasks.append(
                asyncio.create_task(self._fetch_sentence(sentence, params, out, semaphore))
            )
            order.put_nowait(out)

        try:
            async for text_chunk in request.message_gen:
                for sentence in boundary_detector.add_chunk(text_chunk):
                    if sentence.strip():
                        start(sentence)
            if (remaining := boundary_detector.finish()) and remaining.strip():
                start(remaining)
        except Exception as err:  # noqa: BLE001
            order.put_nowait(err)
        else:
            order.put_nowait(None)

    async def _fetch_sentence(
        self,
        text: str,
        params: dict[str, Any],
        out: asyncio.Queue,
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Fetch one sentence's audio into its queue (None = done, Exception = failed)."""
        try:
            async with semaphore:
                async for chunk in self._synthesize(text, params):
                    out.put_nowait(chunk)
        except Exception as err:  # noqa: BLE001
            out.put_nowait(err)
        else:
            out.put_nowait(None)

    async def _synthesize(
        self, text: str, params: dict[str, Any]
    ) -> AsyncGenerator[bytes]:
        """Stream the audio for a single piece of text from Fish Audio."""
        _LOGGER.debug("Streaming TTS audio for %s", text)
        try:
            async for chunk in await self.client.tts.stream(text=text, **params):
                yield chunk
        except RateLimitError as err:
            _LOGGER.error("Fish Audio TTS rate limited: %s", err)
            raise HomeAssistantError(f"Rate limited: {err}") from err
        except APIError as err:
            _LOGGER.error("Fish Audio TTS request failed: %s", err)
            raise HomeAssistantError(f"TTS request failed: {err}") from err
        except Exception as err:
            raise UnexpectedError(err) from err
