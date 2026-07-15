# HTTP/WebSocket gateway

`baresipy.server` exposes a `BareSIP` phone over a FastAPI HTTP + WebSocket API, so you can drive
calls, TTS, DTMF and audio from any language/process instead of embedding baresipy directly in
your python application.

## Install

```bash
pip install baresipy[server]
```

This pulls in `fastapi`, `uvicorn`, and `python-multipart`. Plain `import baresipy` never needs
them; only `baresipy.server` does, and it raises a clear `ImportError` naming this extra if it's
missing.

## Running

```bash
baresipy-gateway --user your_user --pwd your_password --gateway your_sip_gateway.example \
    --host 0.0.0.0 --port 8000
```

Useful flags:

| flag | default | meaning |
|---|---|---|
| `--user` / `--pwd` / `--gateway` / `--transport` | `None` / `None` / `None` / `udp` | SIP account, see [docs/configuration.md](configuration.md); omit all three for registrar-less mode |
| `--headless` | off | no sound card, see `BareSIP(headless=True)` |
| `--record-rx` / `--no-record-rx` | on | enable/disable `record_rx`, required for `/ws/audio` |
| `--auto-answer` / `--no-auto-answer` | on | auto-accept inbound calls vs. wait for `POST /accept` |
| `--token` | `$BARESIPY_TOKEN` | bearer token required on every route, see Auth below |

A phone only handles one call at a time. To run more than one line concurrently, start multiple
`baresipy-gateway` processes with distinct `--config-path`/ports (pass `BARESIPY_TOKEN` per
process, or reuse the same config directory only if the phones never overlap).

## Auth

If a token is configured (`--token` or `BARESIPY_TOKEN` env var), every HTTP route requires:

```
Authorization: Bearer <token>
```

and both WebSocket routes require the same header on the connection handshake, or the server
closes the socket with code `4001`. With no token configured, the gateway is open — put it behind
your own auth/network boundary in that case.

## Endpoints

All request/response bodies are JSON unless noted.

| method | path | body | responses |
|---|---|---|---|
| GET | `/status` | — | `200 {status, current_call, ready, running}` |
| POST | `/call` | `{"uri": "sip:..."}` | `200 {"ok": true}` · `409` already in a call |
| POST | `/accept` | — | `200` · `409` no incoming call |
| POST | `/hangup` | — | `200` · `409` no active call |
| POST | `/hold` | — | `200` · `409` no active call |
| POST | `/resume` | — | `200` · `409` no active call |
| POST | `/speak` | `{"text": "..."}` | `200` · `409` no established call · `503` no TTS configured |
| POST | `/dtmf` | `{"digits": "123", "mode": "keys"}` | `200` · `409` no call (mode=`keys`) · `422` invalid mode |
| POST | `/audio` | multipart file upload, field `file` | `200` · `409` no established call |
| WS | `/ws/events` | — | streams call-lifecycle events as JSON |
| WS | `/ws/audio` | — | streams resampled caller audio as PCM16 frames |

`mode` for `/dtmf` is `"keys"` (real RTP telephone-events, needs an established call) or
`"audio"` (synthesized in-band tones, mirrors `BareSIP.send_dtmf`).

`/audio` accepts wav/mp3/anything `pydub`+`ffmpeg` can decode — the upload is saved to a temp
file and passed through `BareSIP.convert_audio`.

## curl examples

```bash
curl -s http://localhost:8000/status

curl -s -X POST http://localhost:8000/call -H 'content-type: application/json' \
    -d '{"uri": "sip:someone@your_sip_gateway.example"}'

curl -s -X POST http://localhost:8000/speak -H 'content-type: application/json' \
    -d '{"text": "hello, this is a test"}'

curl -s -X POST http://localhost:8000/dtmf -H 'content-type: application/json' \
    -d '{"digits": "123#", "mode": "keys"}'

curl -s -X POST http://localhost:8000/audio -F 'file=@/path/to/clip.wav'

curl -s -X POST http://localhost:8000/hangup

# with a token configured
curl -s http://localhost:8000/status -H 'Authorization: Bearer your_token_here'
```

The examples below use the `websockets` package (`pip install websockets`) for the client side —
it is not a baresipy dependency, just a convenient async WebSocket client.

## `/ws/events`

On connect, the server sends the last 50 buffered events, then streams new ones live as they
happen. Each message is `{"event": str, "data": dict, "ts": float}`. Emitted events:
`incoming_call`, `call_established`, `call_ended`, `dtmf_received`, `login_success`,
`login_failure`.

```bash
python -c "
import asyncio, websockets, json

async def main():
    async with websockets.connect('ws://localhost:8000/ws/events') as ws:
        async for message in ws:
            print(json.loads(message))

asyncio.run(main())
"
```

## `/ws/audio`

Streams the caller's audio while a call is established, resampled to 16kHz mono PCM16 (same
format as `baresipy.audio.resample_pcm16`/`BareSIPMicrophone`). Requires the gateway to have been
started with `--record-rx` (the default). If recording wasn't enabled, or no rx recording ever
appeared, the server closes the socket with code `4003`.

Message sequence:

1. One JSON text message: `{"sample_rate": 16000, "sample_width": 2, "channels": 1}`
2. Binary frames of raw PCM16 little-endian audio, as they arrive
3. One JSON text message `{"event": "eof"}` when the call ends, then the socket closes

Python client example, writing the stream to your own processing pipeline (e.g. feeding an STT
engine):

```python
import asyncio
import json
import websockets


async def consume_audio(url="ws://localhost:8000/ws/audio"):
    async with websockets.connect(url) as ws:
        header = json.loads(await ws.recv())
        print("stream format:", header)  # {"sample_rate": 16000, "sample_width": 2, "channels": 1}

        async for message in ws:
            if isinstance(message, bytes):
                # raw PCM16 mono frame - hand it to your STT/VAD/recorder here
                process_pcm_frame(message)
            else:
                event = json.loads(message)
                if event.get("event") == "eof":
                    break


def process_pcm_frame(frame: bytes) -> None:
    ...


asyncio.run(consume_audio())
```

See also [examples/gateway_client.py](../examples/gateway_client.py) for a runnable script that
places a call over REST and prints `/ws/events` as they arrive.
