# Commentary API

One server. Start it once and leave it running.

```bash
cd ~/Desktop/elevenlabs/elevenlabs/eleven
python3 server.py                 # 127.0.0.1:8080
python3 server.py --dry           # choose and split, never call ElevenLabs
python3 server.py --host 0.0.0.0  # reachable from another machine
```

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | `0.0.0.0` to accept calls from other machines |
| `--port` | `8080` | |
| `--court` | from `live.py` | court to follow; `""` for all |
| `--key` | `655` | tournament key |
| `--dry` | off | never call ElevenLabs, produce no audio |
| `--debug` | off | log cache hits and per-piece timings |
| `--no-local-audio` | off | do not play here; the caller fetches `url` |
| `--city` | `sydney` | venue for weather lines |
| `--lat` / `--lon` | — | exact venue, overrides `--city` |
| `--no-weather` | off | do not poll for outdoor conditions |

## Speak a line

Two identical forms. GET or POST.

```bash
curl "localhost:8080/smash"                     # shortcut
curl "localhost:8080/say?category=smash"        # canonical
```

| Category | Blocks | Placeholders it uses |
|---|---|---|
| `match_start` | 14 | `player1` `player2` `country1` `country2` `court_no`* `match_no`* |
| `rally` | 20 | `player1` `player2` |
| `smash` | 20 | `player` |
| `highlights` | 20 | none |
| `winners` | 10 | `winner` `opponent` |
| `convo` | 10 | none — **two voices**, rendered whole |
| `weather` | 18 | `temperature` `condition` `wind` `high` `low` `rain_chance` `outlook` |
| `voting` | 10 | none |

\* `court_no` and `match_no` have no source, so blocks using them can never be selected.

## Parameters

Every placeholder above can be passed as a query parameter of the same name.
Anything you do not pass falls back to the live match, then to live weather,
then to empty.

```bash
curl "localhost:8080/say?category=smash&player=A"          # side A of the live match
curl "localhost:8080/say?category=smash&player=B"          # side B
curl "localhost:8080/say?category=smash&player=Heena"      # a literal name
curl "localhost:8080/say?category=rally&player1=X&player2=Y"
curl "localhost:8080/say?category=winners&winner=X&opponent=Y"
curl "localhost:8080/say?category=weather&temperature=31%20degrees"
```

`player=A|B|1|2` selects a side of the live match. Any other value is used as
the name itself.

`section=` forces a section on categories that have them:

```bash
curl "localhost:8080/say?category=smash&section=no-players"
curl "localhost:8080/say?category=rally&section=with-players"
```

Without it, `smash` and `rally` choose `with-players` when a name is known and
`no-players` when it is not.

## Speak your own words

```bash
curl "localhost:8080/play?text=[excited]%20What%20a%20smash%20from&name=Kannama"
curl "localhost:8080/play?text=[emphasis]%20{name}%20takes%20it&name=Akmal"
```

`text` is required. Put `{name}` in it to place the name; leave it out and the
name is spoken at the end.

## Everything else

| Endpoint | Method | Returns |
|---|---|---|
| `/` | GET | plain-text help |
| `/health` | GET | `{"ok": true, "queued": n, "dry_run": bool}` |
| `/state` | GET | the live match from the websocket |
| `/conditions` | GET | the outdoor weather being spoken from |
| `/pieces` | GET | how many audio pieces are cached |
| `/warm` | GET/POST | what pre-rendering every phrase would cost |
| `/warm?confirm=1` | GET/POST | actually pre-render (spends credits) |
| `/audio/<file>` | GET | a finished clip, by the name in `saved_as` |
| `/ui` | GET | the operator panel, open in a browser |

## Response

```json
{
  "category": "smash",
  "text": "[chuckle] Heena wasn't interested in a long rally...",
  "pieces": [
    {"kind": "name",   "text": "[chuckle] Heena", "chars": 15, "cached": true,  "seconds": 0.4},
    {"kind": "phrase", "text": " wasn't interested...", "chars": 64, "cached": false, "seconds": 1.9}
  ],
  "synthesized_chars": 64,
  "reused_chars": 15,
  "seconds": 1.4,
  "saved_as": "/.../configs/audio/clip_smash_a1b2c3d4.mp3",
  "url": "/audio/clip_smash_a1b2c3d4.mp3",
  "timings": {"render": 1.93, "total": 2.04}
}
```

`synthesized_chars` is what you were billed. `reused_chars` came free from
cache. `url` is where to fetch the clip if you play it yourself.

`convo` returns one piece of `"kind": "dialogue"` with a `turns` count instead,
because two-voice audio cannot be split.

## Errors, all JSON

| Code | When |
|---|---|
| `400` | no `category`, or `/play` with no `text` |
| `404` | unknown category, unknown route, missing clip |
| `405` | wrong method |
| `409` | no usable phrase — names not confirmed yet |
| `502` | ElevenLabs refused, e.g. `quota_exceeded` |
| `500` | a bug here; the JSON names the cause |

## From Windows

Use `curl.exe` — bare `curl` in PowerShell is an alias for `Invoke-WebRequest`.

```powershell
curl.exe "http://192.168.31.57:8080/smash"
Invoke-RestMethod "http://192.168.31.57:8080/state"
```

The server must have been started with `--host 0.0.0.0`.

## Cost and latency

eleven_v3 bills about one credit per character, and `synthesized_chars` tells
you what each call cost. A piece is billed once and cached forever, so the
phrase halves are shared across every player: renders scale as
(phrases + players), not (phrases x players).

`/warm` pre-renders every phrase piece. Check the estimate first — it reports
what it would cost before spending anything.
