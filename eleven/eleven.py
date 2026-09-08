from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.types import VoiceSettings
from elevenlabs.core.api_error import ApiError
import os
import random
import sys

load_dotenv()

elevenlabs = ElevenLabs(
  api_key=os.getenv("ELEVENLABS_API_KEY"),
)

# Phrases live in phrases/<category>.txt - one phrase per block, blank line
# between blocks, "#" lines are labels and are ignored. A "## <name>" line
# starts a section; pass section= to load only that part of the file, e.g.
# smash.txt has "no-players" and "with-players".
def load_phrases(category, section=None):
    text = open(f"phrases/{category}.txt", encoding="utf-8").read()
    blocks = []
    current = None
    for block in text.split("\n\n"):
        lines = []
        for l in block.strip().split("\n"):
            if l.startswith("##"):
                current = l.lstrip("#").strip()
            elif not l.startswith("#"):
                lines.append(l)
        if lines and (section is None or current == section):
            blocks.append("\n".join(lines))
    return blocks


CATEGORY = "smash"  # match_start, rally, smash, highlights, winners, convo
SECTION = None  # for smash: "no-players" or "with-players"
text = random.choice(load_phrases(CATEGORY, SECTION))
print(f"[{CATEGORY}] {text}")

output_path = f"output_{CATEGORY}.mp3"

try:
    audio = elevenlabs.text_to_speech.convert(
    text=text,

    voice_id="JBFqnCBsd6RMkjVDRZzb",  # George
    model_id="eleven_v3",
    output_format="mp3_44100_128",

    voice_settings=VoiceSettings(
        stability=0.0,
        similarity_boost=0.75,
        style=1.0,
        use_speaker_boost=True,
    ),
)
    # Buffer the stream first: the API can fail mid-response, and we do not
    # want to have already truncated a good file on disk.
    data = b"".join(audio)
except ApiError as e:
    detail = (e.body or {}).get("detail", {}) if isinstance(e.body, dict) else {}
    code = detail.get("code", "api_error")
    msg = detail.get("message", str(e.body))
    print(f"ElevenLabs request failed [{code}]: {msg}", file=sys.stderr)
    if code == "quota_exceeded":
        print(
            f"Text is {len(text)} characters; eleven_v3 bills ~1 credit per "
            f"character. Shorten the text or top up at "
            f"elevenlabs.io/app/subscription.",
            file=sys.stderr,
        )
    print(f"{output_path} left unchanged.", file=sys.stderr)
    sys.exit(1)

tmp_path = output_path + ".part"
with open(tmp_path, "wb") as f:
    f.write(data)
os.replace(tmp_path, output_path)

print(f"Saved {output_path} ({len(data)} bytes)")
