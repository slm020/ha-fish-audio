"""TTS platform for the Fish Audio integration."""

import asyncio
import hashlib
import os
import struct
import time
from collections.abc import AsyncGenerator
import logging
from typing import Any, override

from fishaudio import TTSConfig
from fishaudio.exceptions import APIError, RateLimitError
from fishaudio.types import FlushEvent
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
# Short opening sentences ("Sure.") are stored once and replayed from disk, so they are heard with no Fish delay.
ACK_MAX_CHARS = 30
# Streamed audio is raw 16-bit mono PCM at 44.1 kHz wrapped in a WAV header of unknown length. HA's ffmpeg
# probes WAV instantly, but waits for ~4 s of audio before it emits anything from a short mp3 stream.
PCM_RATE = 44100
PACE_LEAD = 4.0  # seconds of audio allowed ahead of real time
KEEPALIVE_MARGIN = 0.25  # send silence when less than this much audio is left queued in the speaker
SILENCE = bytes(2 * PCM_RATE // 10)  # 100 ms
WAV_HEADER = (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt "
              + struct.pack("<IHHIIHH", 16, 1, 1, PCM_RATE, PCM_RATE * 2, 2, 16)
              + b"data" + struct.pack("<I", 0xFFFFFFFF))
# If the free model errors or is retired, use this paid model for a while instead of going silent.
PAID_FALLBACK_MODEL = "s2.1-pro"
FREE_COOLDOWN_SECONDS = 300
ACK_PHRASES = ("Sure.", "On it.", "Right away.", "Sure, one moment.")
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
        self._ack_mem: dict[str, bytes] = {}
        self._ack_filling: set[str] = set()
        self._free_down_until = 0.0
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

    def _stream_params(self, options: dict[str, Any]) -> dict[str, Any]:
        """Parameters for the streaming path: same as the configured ones but raw PCM audio."""
        params = self._resolve_params(options)
        params["format"] = "pcm"
        params["config"] = params["config"].model_copy(update={"format": "pcm"})
        return params

    def _free_failed(self, err: Exception) -> None:
        """Remember that the free model failed, so the next requests go straight to the paid one."""
        self._free_down_until = time.monotonic() + FREE_COOLDOWN_SECONDS
        _LOGGER.warning("Fish free model failed (%s), using %s for %d s",
                        err, PAID_FALLBACK_MODEL, FREE_COOLDOWN_SECONDS)

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

        if (isinstance(backend, str) and backend.endswith("-free")
                and time.monotonic() < self._free_down_until):
            backend = PAID_FALLBACK_MODEL
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
            try:
                audio = await self.client.tts.convert(text=message, **params)
            except (RateLimitError, APIError) as err:
                if not str(params["model"]).endswith("-free"):
                    raise
                self._free_failed(err)
                audio = await self.client.tts.convert(
                    text=message, **{**params, "model": PAID_FALLBACK_MODEL})
        except RateLimitError as err:
            _LOGGER.error("Fish Audio TTS rate limited: %s", err)
            raise HomeAssistantError(f"Rate limited: {err}") from err
        except APIError as err:
            _LOGGER.error("Fish Audio TTS request failed: %s", err)
            raise HomeAssistantError(f"TTS request failed: {err}") from err
        except Exception as err:
            raise UnexpectedError(err) from err

        return params["format"], audio

    async def async_added_to_hass(self) -> None:
        """Pre-generate the standard acknowledgement phrases in the background."""
        await super().async_added_to_hass()

        async def pregen() -> None:
            try:
                params = self._stream_params({})
            except Exception:  # noqa: BLE001
                return
            for phrase in ACK_PHRASES:
                await self._ack_fill(phrase, params)

        self.hass.async_create_background_task(pregen(), "fish_audio_ack_pregen")

    def _ack_path(self, text: str, params: dict[str, Any]) -> tuple[str, str]:
        raw = "|".join(str(x) for x in (
            text.strip(), params["reference_id"], params["model"], params["speed"], params["format"],
            getattr(params["config"], "mp3_bitrate", None)))
        key = hashlib.sha1(raw.encode()).hexdigest()[:20]
        return key, os.path.join(self.hass.config.path("fish_ack_cache"), f"{key}.{params['format']}")

    async def _ack_load(self, text: str, params: dict[str, Any]) -> bytes | None:
        key, path = self._ack_path(text, params)
        if key in self._ack_mem:
            return self._ack_mem[key]
        def read() -> bytes | None:
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                return None
        if (data := await self.hass.async_add_executor_job(read)):
            self._ack_mem[key] = data
        return data

    async def _ack_fill(self, text: str, params: dict[str, Any]) -> None:
        """Synthesize a short phrase once and keep it on disk."""
        key, path = self._ack_path(text, params)
        if key in self._ack_mem or key in self._ack_filling:
            return
        if await self._ack_load(text, params):
            return
        self._ack_filling.add(key)
        try:
            audio = await self.client.tts.convert(text=text.strip(), **params)
            def write() -> None:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(audio)
            await self.hass.async_add_executor_job(write)
            self._ack_mem[key] = audio
            _LOGGER.debug("Cached Fish audio for %r", text)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not cache Fish audio for %r: %s", text, err)
        finally:
            self._ack_filling.discard(key)

    @override
    async def async_stream_tts_audio(
        self, request: TTSAudioRequest
    ) -> TTSAudioResponse:
        """Generate speech from an incoming stream of text."""
        # Validate up front so configuration errors surface before streaming starts.
        params = self._stream_params(request.options)
        return TTSAudioResponse("wav", self._wav_stream(request, params))

    async def _wav_stream(
        self, request: TTSAudioRequest, params: dict[str, Any]
    ) -> AsyncGenerator[bytes]:
        """Prefix the PCM stream with a WAV header."""
        yield WAV_HEADER
        # Keep-alive: once audio has started, a stall in the source (the answer still being written) would leave the
        # speaker with an empty buffer and ffmpeg holding back the tail of what it has. Whenever the audio sent so far
        # is about to be played out, send a short piece of silence instead; real audio is never delayed by more than one piece.
        queue: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            try:
                async for chunk in self._stream_audio(request, params):
                    queue.put_nowait(chunk)
                queue.put_nowait(None)
            except BaseException as err:  # noqa: BLE001
                queue.put_nowait(err)

        task = self.hass.async_create_background_task(pump(), "fish_audio_pump")
        t0 = time.monotonic(); sent = 0.0; started = None; silence = 0.0
        try:
            while True:
                if started is None:
                    item = await queue.get()
                else:
                    ahead = started + sent - time.monotonic()  # seconds of audio the speaker still has queued
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=max(ahead - KEEPALIVE_MARGIN, 0.01))
                    except TimeoutError:
                        yield SILENCE
                        sent += len(SILENCE) / 2 / PCM_RATE; silence += len(SILENCE) / 2 / PCM_RATE
                        continue
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                if started is None:
                    started = time.monotonic()
                elif (excess := sent - (time.monotonic() - started) - PACE_LEAD) > 0:
                    await asyncio.sleep(excess)  # source is faster than real time: feed the speaker steadily, not in bursts
                sent += len(item) / 2 / PCM_RATE
                yield item
        finally:
            task.cancel()
        _LOGGER.debug("TRACE stream complete t=%.2f audio=%.2fs silence_added=%.2fs", time.monotonic() - t0, sent, silence)

    async def _stream_audio(
        self, request: TTSAudioRequest, params: dict[str, Any]
    ) -> AsyncGenerator[bytes]:
        """Stream one reply over a single Fish WebSocket, flushing at each sentence.

        The HTTP endpoint only returns audio once the whole text is synthesized;
        the WebSocket sends audio as it is generated, so the first words are heard
        after the first sentence rather than after the whole reply. A short first
        sentence ("Sure.") is replayed from a local cache so it starts instantly.
        If the socket fails before any audio, fall back to the per-sentence HTTP method.
        """
        sentences: list[str] = []  # sentences still to be spoken (used for the HTTP fallback)
        detector = SentenceBoundaryDetector()
        source = request.message_gen.__aiter__()
        consumed_all = False

        async def next_sentences() -> list[str] | None:
            nonlocal consumed_all
            while not consumed_all:
                try:
                    text_chunk = await source.__anext__()
                except StopAsyncIteration:
                    consumed_all = True
                    rest = detector.finish()
                    return [rest] if rest and rest.strip() else None
                if got := [x for x in detector.add_chunk(text_chunk) if x.strip()]:
                    return got
            return None

        first = await next_sentences()
        to_send = first or []
        if first and len(first[0].strip()) <= ACK_MAX_CHARS:
            if (cached := await self._ack_load(first[0], params)) is not None:
                yield cached
                to_send = first[1:]
            else:
                self.hass.async_create_background_task(
                    self._ack_fill(first[0], params), "fish_audio_ack_fill")

        async def events():
            pending = list(to_send)
            while True:
                for sentence in pending:
                    sentences.append(sentence)
                    yield sentence.rstrip() + " "
                    yield FlushEvent()
                if not (pending := await next_sentences() or []):
                    return

        if not to_send and consumed_all:
            return
        got_audio = False
        try:
            async for chunk in self.client.tts.stream_websocket(
                events(),
                reference_id=params["reference_id"],
                latency=params["latency"],
                speed=params["speed"],
                model=params["model"],
                format=params["format"],
                config=params["config"],
            ):
                got_audio = True
                yield chunk
            return
        except Exception as err:  # noqa: BLE001
            if got_audio:
                _LOGGER.error("Fish Audio WebSocket stream failed mid-reply: %s", err)
                raise
            _LOGGER.warning("Fish Audio WebSocket failed (%s), falling back to HTTP", err)
            if str(params["model"]).endswith("-free"):
                self._free_failed(err)
                params = self._stream_params(request.options)

        async def replay():
            for sentence in sentences:
                yield sentence
            if not consumed_all:
                async for text_chunk in source:
                    yield text_chunk

        async for chunk in self._stream_audio_http(
            TTSAudioRequest(request.language, request.options, replay()), params
        ):
            yield chunk

    async def _stream_audio_http(
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
        sent = False
        try:
            try:
                async for chunk in await self.client.tts.stream(text=text, **params):
                    sent = True
                    yield chunk
            except (RateLimitError, APIError) as err:
                if sent or not str(params["model"]).endswith("-free"):
                    raise
                self._free_failed(err)
                async for chunk in await self.client.tts.stream(
                        text=text, **{**params, "model": PAID_FALLBACK_MODEL}):
                    yield chunk
        except RateLimitError as err:
            _LOGGER.error("Fish Audio TTS rate limited: %s", err)
            raise HomeAssistantError(f"Rate limited: {err}") from err
        except APIError as err:
            _LOGGER.error("Fish Audio TTS request failed: %s", err)
            raise HomeAssistantError(f"TTS request failed: {err}") from err
        except Exception as err:
            raise UnexpectedError(err) from err
