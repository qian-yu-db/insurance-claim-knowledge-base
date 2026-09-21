"""Audio artifacts: reuse existing policy-narration MP3s + short TTS clips via macOS `say` + ffmpeg."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import claims


def _tts(text: str, dest: Path, voice: str | None = None) -> bool:
    """Render text to speech with `say` (AIFF) then transcode to dest format via ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tf:
        aiff = Path(tf.name)
    say_cmd = ["say", "-o", str(aiff)]
    if voice:
        say_cmd += ["-v", voice]
    say_cmd.append(text)
    if subprocess.run(say_cmd, capture_output=True).returncode != 0:
        subprocess.run(["say", "-o", str(aiff), text], capture_output=True)
    codec = ["-c:a", "aac"] if dest.suffix == ".m4a" else ["-c:a", "libmp3lame", "-q:a", "5"]
    r = subprocess.run(["ffmpeg", "-y", "-i", str(aiff), *codec, str(dest)], capture_output=True)
    aiff.unlink(missing_ok=True)
    return r.returncode == 0 and dest.exists()


def build_audio(cid: str, c: dict, audio_dir: Path) -> list[dict]:
    audio_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []

    # 1) reuse an existing policy-narration MP3 as a recorded coverage / policy-review call
    src = claims.EXISTING_MP3_DIR / c["reuse_mp3"]
    if src.exists():
        dest = audio_dir / f"{cid}_recorded_coverage_call.mp3"
        shutil.copyfile(src, dest)
        entries.append({"path": dest, "doc_type": "recorded_coverage_call", "format": "mp3", "source": "reused"})

    # 2) short TTS clip appropriate to the claim
    if c["complexity"] == "fast-track":
        txt = (f"Hi, this is {c['insured']['name']} calling about claim {cid}. I was rear-ended at a stop light. "
               f"The rear bumper is damaged but I'm okay. Please call me back at {c['insured']['phone']}. Thank you.")
        dest = audio_dir / f"{cid}_claimant_voicemail.mp3"
        if _tts(txt, dest, voice="Samantha"):
            entries.append({"path": dest, "doc_type": "claimant_voicemail", "format": "mp3", "source": "tts"})
    elif c["complexity"] == "litigation-bodily-injury":
        txt = (f"Recorded statement, claim {cid}. My name is {c['claimant']['name']}. On {c['loss']['date']} I was struck by "
               f"the insured vehicle while merging near Plano. I was taken by ambulance with neck and back injuries and I am "
               f"still receiving treatment. This statement is being recorded with my consent.")
        dest = audio_dir / f"{cid}_recorded_statement.m4a"
        if _tts(txt, dest, voice="Samantha"):
            entries.append({"path": dest, "doc_type": "recorded_statement", "format": "m4a", "source": "tts"})
    elif c["complexity"] == "investigation-disputed":
        txt = (f"This is investigator {c['siu']['investigator']}, reference {c['siu']['ref']}, claim {cid}. The reported deer "
               f"strike does not match the damage pattern. Recommending an examination under oath before any payment.")
        dest = audio_dir / f"{cid}_siu_field_note.m4a"
        if _tts(txt, dest, voice="Daniel"):
            entries.append({"path": dest, "doc_type": "siu_field_note", "format": "m4a", "source": "tts"})

    return entries
