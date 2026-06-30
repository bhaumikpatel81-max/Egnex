# Step 3 — OpenAI for the bot brain + Whisper for candidate speech

## 3A. Point the bot brain at OpenAI — ENV ONLY, no code change

`backend/app/services/interviewer_llm.py` already uses the **OpenAI Python SDK**, just pointed at Groq via `GROQ_BASE_URL`. To use OpenAI instead, set in `.env.prod`:

```
# Use OpenAI for NexAI's conversation + scoring
GROQ_API_KEY=sk-YOUR_OPENAI_KEY          # the client reads this var name
GROQ_BASE_URL=https://api.openai.com/v1   # OpenAI endpoint
LLM_MODEL=gpt-4o-mini                      # or gpt-4o / gpt-4.1-mini etc.
```

That's it — the SDK calls `chat.completions.create` which is identical on OpenAI.

### OPTIONAL cleanup (so the var names aren't confusing)
If you'd rather use `OPENAI_API_KEY`, make these two edits in `interviewer_llm.py`:

#### FIND:
```python
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Add it to the backend .env file."
            )
        base_url = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
```
#### REPLACE WITH:
```python
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env.prod."
            )
        base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get(
            "GROQ_BASE_URL", "https://api.openai.com/v1"
        )
```
Then in `.env.prod`:
```
OPENAI_API_KEY=sk-YOUR_OPENAI_KEY
OPENAI_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

> Also check `requirements.txt` already has `openai>=1.40.0` — it does. No new dependency.

## 3B. Add Whisper transcription (server-side candidate speech)

Today the candidate's spoken answers are transcribed **in the browser** by the free Web `SpeechRecognition` API (Chrome-only, variable quality). Whisper moves transcription to the server for better accuracy and cross-browser support.

### New service file: `backend/app/services/stt.py`
```python
"""
Speech-to-text via OpenAI Whisper API.
Used by the conversational NexAI interview to transcribe candidate audio.
Env: OPENAI_API_KEY (reuses the same key as the LLM brain).
"""
import os
import tempfile
from typing import Optional

import openai

_client: Optional[openai.OpenAI] = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set — required for Whisper STT.")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        _client = openai.OpenAI(api_key=api_key, base_url=base_url)
    return _client


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm",
                     model: str = None) -> str:
    """
    Transcribe a single audio blob with Whisper. Returns plain text.
    model defaults to env WHISPER_MODEL or 'whisper-1'.
    """
    model = model or os.environ.get("WHISPER_MODEL", "whisper-1")
    suffix = "." + filename.rsplit(".", 1)[-1] if "." in filename else ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tf:
        tf.write(audio_bytes)
        tf.flush()
        tf.seek(0)
        with open(tf.name, "rb") as fh:
            resp = _get_client().audio.transcriptions.create(
                model=model,
                file=fh,
                response_format="text",
            )
    # SDK returns a str when response_format="text"
    return (resp if isinstance(resp, str) else getattr(resp, "text", "")).strip()
```

### New endpoint in `backend/app/routers/nexai_api.py`
Add near the other public invite endpoints (public — candidate has no JWT). It accepts an audio file and returns text; the frontend then passes that text into the existing `/invite/converse` turn.

```python
@router.post("/invite/transcribe")
async def transcribe_candidate_audio(file: UploadFile = File(...)):
    """
    Public — transcribe one candidate audio blob via Whisper.
    Returns {"text": "..."}. The frontend sends this text to /invite/converse.
    """
    from ..services.stt import transcribe_audio
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "Empty audio")
    try:
        text = transcribe_audio(audio, file.filename or "audio.webm")
    except Exception as exc:
        print(f"[stt] transcription failed: {exc}")
        raise HTTPException(502, "Transcription failed")
    return {"text": text}
```
At the top of `nexai_api.py` ensure these imports exist:
```python
from fastapi import UploadFile, File
```
(add `UploadFile, File` to the existing fastapi import line).

### Make the endpoint public — `backend/app/main.py`
The auth middleware already lets through anything starting with `/api/nexai/invite`. Confirm this line is present (it is):
```python
    if path.startswith("/api/nexai/invite") or path == "/nexai-interview":
```
`/api/nexai/invite/transcribe` matches that prefix → already public. No change needed.

### Frontend — `frontend/interview.html` (conversational mode)
Today `_recog` (SpeechRecognition) fills `_listenText`, and on silence the code sends `_listenText` to `/invite/converse`. To use Whisper instead, record the mic with `MediaRecorder`, and on turn-end POST the blob to `/invite/transcribe`, then send the returned text to `/invite/converse`.

Minimal approach (let Claude Code implement against the existing `_convMicStream`):
1. When a candidate turn starts, create `const rec = new MediaRecorder(_convMicStream, {mimeType:'audio/webm'})`, collect chunks.
2. On silence/turn-end, `rec.stop()`, build `new Blob(chunks,{type:'audio/webm'})`.
3. `POST` it as `FormData` (`file`) to `/api/nexai/invite/transcribe?...`, read `{text}`.
4. Pass `text` as `candidate_text` to the existing `/invite/converse` call (replacing `_listenText`).
5. Keep the SpeechRecognition path as a fallback if `MediaRecorder` or the network call fails, so the interview never dead-ends.

> Recommendation: **ship Whisper as an enhancement with the browser STT kept as fallback.** That way a Whisper/API hiccup never blocks a live candidate.

### requirements.txt
No change — `openai>=1.40.0` covers Whisper too.

## 3C. (Optional) Reuse Whisper for panel-interview transcripts
This is the realistic answer to "transcript of the panel interview without Google": add a recruiter-facing "Upload interview recording" button that POSTs an audio/video file to a new authenticated endpoint, runs `transcribe_audio`, stores it in `interview_notes.transcript_text`, optionally summarizes with the LLM, and emails the recruiter. This avoids Google entirely but requires someone to record + upload the call. Scope it as a follow-up if needed.

## VERIFY Step 3
1. Set OpenAI env vars, restart. Run a NexAI conversational interview → bot replies are coherent (now from OpenAI).
2. If Whisper wired: speak an answer → network tab shows `/invite/transcribe` returning text → conversation continues.
3. Completion email to the recruiter still arrives with transcript + score.
