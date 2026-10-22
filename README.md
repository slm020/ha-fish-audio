# Fish Audio TTS (streaming) for Home Assistant

A custom version of Home Assistant's built-in [Fish Audio](https://www.home-assistant.io/integrations/fish_audio)
integration, which has the same `fish_audio` domain and overrides the core one when installed in
`custom_components`.

## Changes from the core integration

- **Streaming TTS.** It implements `async_stream_tts_audio`. Text from the conversation agent is split
  into sentences as it arrives. Each sentence is sent to Fish Audio straight away, several at a time,
  and the audio is played back strictly in order, so speech starts on the first sentence instead of
  after the whole reply.
- **More options.** Speed, output format (mp3/opus/wav), MP3/Opus bitrate and chunk length, as well as
  backend model and latency mode.
- **Extra requirement.** `sentence-stream`.

## Latency modes

Measured on `s2.1-pro-free` with a custom voice, from HA, for one short sentence:

| latency | first audio byte |
|---|---|
| normal | ~2.6 s |
| balanced | ~0.6 s |
| low (API only, rejected by fish-audio-sdk 1.1.0) | ~0.6 s |

`balanced` is the one to use. `low` gives no further gain.

## Install

Copy `custom_components/fish_audio` into your HA config's `custom_components` folder and restart Home Assistant.

## Credits

This is based on the Home Assistant core `fish_audio` integration (Apache-2.0) by @noambav.
