"""Two-voice conversational commentary via the ElevenLabs dialogue API."""
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.types import DialogueInput
import os

load_dotenv()

elevenlabs = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

MALE = "JBFqnCBsd6RMkjVDRZzb"    # George
FEMALE = "FGY2WhTYpPnrIDTdsKH5"  # Laura

# After a long rally
turns = [# Knock knock
    (MALE, "Knock knock."),
    (FEMALE, "Who's there?"),
    (MALE, "Winner."),
    (FEMALE, "Winner who?"),
    (MALE, "[playful] Winner? Ask me again after this game."),
]

audio = elevenlabs.text_to_dialogue.convert(
    inputs=[DialogueInput(text=t, voice_id=v) for v, t in turns],
    model_id="eleven_v3",
    output_format="mp3_44100_128",
)

output_path = "output_convo_1.mp3"
with open(output_path, "wb") as f:
    f.write(b"".join(audio))

print(f"Saved {output_path}")
