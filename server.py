from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect
from mutagen.mp3 import MP3
import json
import base64
import asyncio
from fastapi.responses import Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv
from llm import chat
from database import (
    start_call, end_call, get_all_calls,
    get_call_transcript, save_recording,
    set_call_state, get_call_state, update_call_state, delete_call_state,
    get_cached_response, store_llm_response,
    get_order_context_cached,
    verify_customer_phone,
    redis_client
)
import os
import tempfile
import re
import time

def spoken_to_order_id(text: str) -> str:
    """Convert spoken order ID to numeric string."""
    text = text.lower().strip()

    multiplier_map = {
        'double': 2, 'twice': 2,
        'triple': 3, 'thrice': 3,
        'quadruple': 4, 'quad': 4,
        'quintuple': 5,
    }

    for word, times in multiplier_map.items():
        pattern = rf'\b{word}\s+(zero|one|two|three|four|five|six|seven|eight|nine|oh)\b'
        def expand(m, t=times):
            return ' '.join([m.group(1)] * t)
        text = re.sub(pattern, expand, text)

    word_to_digit = {
        'zero': '0', 'oh': '0',
        'one': '1',
        'two': '2', 'to': '2', 'too': '2',
        'three': '3',
        'four': '4', 'for': '4', 'fore': '4',
        'five': '5',
        'six': '6',
        'seven': '7',
        'eight': '8', 'ate': '8',
        'nine': '9',
    }

    for word, digit in sorted(word_to_digit.items(), key=lambda x: -len(x[0])):
        text = re.sub(r'\b' + word + r'\b', digit, text)

    digits_only = re.sub(r'[^0-9]', '', text)
    return digits_only

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

current_audio_files = {}

# ── Per-call active process_transcript tasks ──
# Used to cancel in-progress responses when customer interrupts
active_tasks = {}  # call_sid → asyncio.Task


# ════════════════════════════════════════════════════
# Dead call monitor
# ════════════════════════════════════════════════════

async def monitor_dead_calls():
    """Background task — detects and cleans up silent/dead calls."""
    while True:
        await asyncio.sleep(30)
        try:
            keys = redis_client.keys("call:*")
            for key in keys:
                call_sid = key.split(":")[1]
                last_activity = float(redis_client.hget(key, "last_activity_at") or 0)
                if last_activity == 0:
                    continue
                elapsed = time.time() - last_activity
                if elapsed > 120:
                    print(f"[{call_sid}] Dead call detected ({elapsed:.0f}s inactive) — cleaning up")
                    end_call(call_sid)
                    delete_call_state(call_sid)
        except Exception as e:
            print(f"Dead call monitor error: {e}")


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(monitor_dead_calls())


# ════════════════════════════════════════════════════
# TTS helpers
# ════════════════════════════════════════════════════

def format_numbers_for_speech(text):
    """Space out any number that is 4 or more digits long"""
    def space_digits(match):
        return ' '.join(list(match.group()))
    return re.sub(r'\b\d{4,}\b', space_digits, text)


def generate_elevenlabs_audio(text, call_sid):
    """Generate audio using ElevenLabs. Returns None if quota exceeded."""
    from elevenlabs.client import ElevenLabs

    text = format_numbers_for_speech(text)

    try:
        client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

        audio = client.text_to_speech.convert(
            text=text,
            voice_id="EXAVITQu4vr4xnSDxMaL",
            model_id="eleven_turbo_v2_5",
            output_format="mp3_44100_128"
        )

        old_file = current_audio_files.get(call_sid)
        if old_file and os.path.exists(old_file):
            os.unlink(old_file)

        temp = tempfile.NamedTemporaryFile(
            suffix=".mp3",
            delete=False,
            dir=".",
            prefix=f"response_{call_sid}_"
        )

        for chunk in audio:
            if chunk:
                temp.write(chunk)

        temp.close()
        current_audio_files[call_sid] = temp.name
        return temp.name

    except Exception as e:
        print(f"ElevenLabs failed, falling back to Twilio TTS: {e}")
        return None


def get_play_block(text, host, call_sid):
    """Try ElevenLabs first, fall back to Twilio Polly TTS."""
    audio_path = generate_elevenlabs_audio(text, call_sid)

    if audio_path:
        audio_filename = os.path.basename(audio_path)
        print(f"Using ElevenLabs audio")
        return f"<Play>https://{host}/audio/{audio_filename}</Play>"
    else:
        safe_text = text.replace("'", "").replace('"', "").replace("&", "and")
        print(f"Using Twilio Polly TTS fallback")
        return f'<Say voice="Polly.Joanna">{safe_text}</Say>'


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    safe_filename = os.path.basename(filename)
    return FileResponse(path=safe_filename, media_type="audio/mpeg")


# ════════════════════════════════════════════════════
# TwiML builders
# ════════════════════════════════════════════════════

def build_transfer_twiml(host, call_sid):
    transfer_number = os.getenv("HUMAN_AGENT_NUMBER")
    if transfer_number:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">Please hold while we connect you to a human agent.</Say>
    <Dial>{transfer_number}</Dial>
</Response>"""
    else:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">We are sorry, all agents are unavailable. Please call back later.</Say>
    <Hangup/>
</Response>"""


def build_response_twiml(text, host, call_sid, verified=False):
    play_block = get_play_block(text, host, call_sid)

    if verified:
        gather = f"""<Gather input="speech"
                action="https://{host}/handle-speech?call_sid={call_sid}"
                method="POST"
                speechTimeout="auto"
                timeout="15"
                language="en-IN"
                speechModel="phone_call">
            {play_block}
        </Gather>"""
    else:
        gather = f"""<Gather input="dtmf"
                action="https://{host}/handle-order-id?call_sid={call_sid}"
                method="POST"
                timeout="10"
                finishOnKey="#">
            {play_block}
        </Gather>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {gather}
</Response>"""


# ════════════════════════════════════════════════════
# Core transcript processor
# ════════════════════════════════════════════════════

async def process_transcript(call_sid: str, transcript: str, host: str):
    """
    Process a Deepgram transcript:
    1. Update Redis activity timestamp
    2. Check Redis cache for keyword match → instant response
    3. Cache miss → call Groq LLM → store response in Redis
    4. Play response back via Twilio REST API
    5. Unmute after TTS finishes (unless interrupted)
    """
    from twilio.rest import Client as TwilioClient

    print(f"[{call_sid}] Deepgram STT: '{transcript}'")

    # Update last activity timestamp
    update_call_state(call_sid, is_speaking=True, last_activity_at=time.time())

    # Clear any previous interrupt flag
    redis_client.hset(f"call:{call_sid}", "is_interrupted", "0")

    # Goodbye detection
    goodbye_words = ["goodbye", "bye", "thank you", "thanks", "that's all"]
    if any(word in transcript.lower() for word in goodbye_words):
        end_call(call_sid)

    # ── Step 1: Check Redis cache ──
    response_text = get_cached_response(call_sid, transcript)

    if response_text:
        from database import save_message
        save_message(call_sid, "user", transcript)
        save_message(call_sid, "assistant", response_text)
    else:
        print(f"[{call_sid}] Cache miss — sending to LLM")
        response_text = chat(transcript, call_sid=call_sid)

        # ── NEW: LLM requested human escalation ──
        if "TRANSFER_TO_HUMAN" in str(response_text):
            print(f"[{call_sid}] HUMAN ESCALATION TRIGGERED")

            transfer_number = os.getenv("HUMAN_AGENT_NUMBER")
            print(f"[{call_sid}] Target number: {transfer_number}")

            twiml = build_transfer_twiml(host, call_sid)

            print(f"[{call_sid}] Transfer TwiML:")
            print(twiml)

            try:
                twilio_client = TwilioClient(
                    os.getenv("TWILIO_ACCOUNT_SID"),
                    os.getenv("TWILIO_AUTH_TOKEN")
                )

                result = twilio_client.calls(call_sid).update(
                    twiml=twiml
                )

                print(f"[{call_sid}] Transfer successful: {result.sid}")

            except Exception as e:
                print(f"[{call_sid}] Transfer failed: {e}")

            return

        if response_text:
            store_llm_response(call_sid, transcript, response_text)

    # ── Step 3: LLM failed — transfer to human ──
    # ── Step 3: LLM failed — transfer to human ──
    if not response_text:
        print(f"[{call_sid}] TRANSFER TRIGGERED")

        transfer_number = os.getenv("HUMAN_AGENT_NUMBER")
        print(f"[{call_sid}] Target number: {transfer_number}")

        twiml = build_transfer_twiml(host, call_sid)
        print(f"[{call_sid}] Transfer TwiML:\n{twiml}")

        try:
            twilio_client = TwilioClient(
                os.getenv("TWILIO_ACCOUNT_SID"),
                os.getenv("TWILIO_AUTH_TOKEN")
            )

            result = twilio_client.calls(call_sid).update(
                twiml=twiml
            )

            print(f"[{call_sid}] Twilio update successful: {result.sid}")

        except Exception as e:
            print(f"[{call_sid}] TRANSFER FAILED: {e}")

        return

    # ── Check if interrupted before even playing ──
    interrupted = redis_client.hget(f"call:{call_sid}", "is_interrupted") == "1"
    if interrupted:
        print(f"[{call_sid}] Interrupted before playback — discarding response")
        update_call_state(call_sid, is_speaking=False, resumed_at=time.time())
        return

    print(f"[{call_sid}] Agent: {response_text}")
    play_block = get_play_block(response_text, host, call_sid)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {play_block}
    <Pause length="60"/>
</Response>"""

    try:
        twilio_client = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN")
        )
        twilio_client.calls(call_sid).update(twiml=twiml)
        print(f"[{call_sid}] Response playing")
    except Exception as e:
        print(f"[{call_sid}] Failed to update call TwiML: {e}")
        return

    # ── Wait for TTS to finish — checking for interrupts ──
    audio_path = current_audio_files.get(call_sid)
    if audio_path and os.path.exists(audio_path):
        try:
            audio = MP3(audio_path)
            actual_duration = audio.info.length
            print(f"[{call_sid}] TTS duration: {actual_duration:.1f}s")
            total_duration = actual_duration + 0.2
        except Exception:
            total_duration = max(2, len(response_text.split()) * 0.4)
    else:
        word_count = len(response_text.split())
        total_duration = max(1.5, (word_count / 150) * 60)
        print(f"[{call_sid}] Estimated TTS duration: {total_duration:.1f}s ({word_count} words)")

    # Check for interrupt every 0.3 seconds instead of sleeping full duration
    elapsed = 0.0
    check_interval = 0.3
    while elapsed < total_duration:
        await asyncio.sleep(check_interval)
        elapsed += check_interval

        # Check if customer interrupted
        interrupted = redis_client.hget(f"call:{call_sid}", "is_interrupted") == "1"
        if interrupted:
            print(f"[{call_sid}] Interrupt detected at {elapsed:.1f}s — stopping TTS")
            # Send silence TwiML to stop audio immediately
            try:
                stop_twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="60"/>
</Response>"""
                twilio_client = TwilioClient(
                    os.getenv("TWILIO_ACCOUNT_SID"),
                    os.getenv("TWILIO_AUTH_TOKEN")
                )
                twilio_client.calls(call_sid).update(twiml=stop_twiml)
                print(f"[{call_sid}] TTS stopped — ready for customer input")
            except Exception as e:
                print(f"[{call_sid}] Failed to stop TTS: {e}")

            # Unmute immediately so customer's speech is processed
            update_call_state(
                call_sid,
                is_speaking=False,
                resumed_at=time.time(),
                last_activity_at=time.time()
            )
            redis_client.hset(f"call:{call_sid}", "is_interrupted", "0")
            print(f"[{call_sid}] Listening resumed after interrupt")
            return

    # TTS finished naturally — unmute
    update_call_state(
        call_sid,
        is_speaking=False,
        resumed_at=time.time(),
        last_activity_at=time.time()
    )
    print(f"[{call_sid}] Listening resumed")


# ════════════════════════════════════════════════════
# Call routes
# ════════════════════════════════════════════════════

async def unmute_after_greeting(call_sid: str, duration: float):
    """Wait for greeting to finish playing then unmute Deepgram."""
    total_wait = 2.0 + duration + 0.3
    print(f"[{call_sid}] Unmuting in {total_wait:.1f}s")
    await asyncio.sleep(total_wait)

    state = get_call_state(call_sid)
    if not state:
        print(f"[{call_sid}] Call ended before unmute")
        return

    update_call_state(
        call_sid,
        is_speaking=False,
        resumed_at=time.time(),
        last_activity_at=time.time()
    )
    print(f"[{call_sid}] Greeting finished — listening started")


@app.post("/incoming-call")
async def incoming_call(request: Request):
    host = request.headers.get("host")
    form_data = await request.form()

    call_sid = form_data.get("CallSid", "unknown")
    caller_number = form_data.get("From", "unknown")

    print(f"Incoming call! SID: {call_sid} From: {caller_number}")
    start_call(call_sid, caller_number)

    redis_client.setex(f"caller:{call_sid}", 900, caller_number)

    greeting = (
        "Hello! Thank you for calling. "
        "I'm Maya, your customer support agent. "
        "How can I help you today?"
    )
    play_block = get_play_block(greeting, host, call_sid)

    try:
        from twilio.rest import Client as TwilioClient
        twilio_client = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN")
        )
        twilio_client.calls(call_sid).recordings.create(
            recording_status_callback=f"https://{host}/recording-status",
            recording_status_callback_method="POST"
        )
        print(f"[{call_sid}] Background recording started")
    except Exception as e:
        print(f"[{call_sid}] Could not start recording: {e}")

    set_call_state(call_sid, is_speaking=True, host=host)

    word_count = len(greeting.split())
    greeting_audio = current_audio_files.get(call_sid)

    if greeting_audio and os.path.exists(greeting_audio):
        try:
            audio = MP3(greeting_audio)
            greeting_duration = audio.info.length + 0.5
        except Exception:
            greeting_duration = max(2.0, word_count / 3.5)
    else:
        greeting_duration = max(2.0, word_count / 3.5)

    print(f"[{call_sid}] Greeting duration: {greeting_duration:.1f}s — will unmute after")
    asyncio.create_task(unmute_after_greeting(call_sid, greeting_duration))

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="2"/>
    {play_block}
    <Start>
        <Stream url="wss://{host}/media-stream">
            <Parameter name="call_sid" value="{call_sid}"/>
        </Stream>
    </Start>
    <Pause length="60"/>
</Response>"""

    return Response(content=twiml, media_type="text/xml")


@app.post("/handle-order-id")
async def handle_order_id(request: Request, call_sid: str = ""):
    """Handle order ID via speech or DTMF with confirmation and retry logic"""
    host = request.headers.get("host")
    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")

    attempt = int(request.query_params.get("attempt", "1"))

    form_data = await request.form()
    digits = form_data.get("Digits", "").strip()
    speech = form_data.get("SpeechResult", "").strip()

    if digits:
        print(f"[{call_sid}] Order ID via keypad: {digits}")
        return await process_order_id(digits, call_sid, host)

    if speech:
        order_id_str = spoken_to_order_id(speech)

        if not order_id_str or len(order_id_str) != 4:
            return await ask_again(call_sid, host, attempt, f"I heard {order_id_str or 'nothing'} which doesnt look like a valid 4 digit order ID")
        print(f"[{call_sid}] Speech: '{speech}' → Order ID: '{order_id_str}'")

        redis_client.setex(f"pending_order:{call_sid}", 300, order_id_str)

        spaced = ' '.join(list(order_id_str))
        confirm_text = f"I heard order ID {spaced}. Is that correct? Say yes to confirm or no to try again."
        play_block = get_play_block(confirm_text, host, call_sid)

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            speechModel="phone_call"
            hints="yes:5, no:5, correct:3, wrong:3, right:3, yeah:5, nope:5, yep:3">
        {play_block}
    </Gather>
    <Redirect method="POST">https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}&amp;timeout=true</Redirect>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    return await ask_again(call_sid, host, attempt, "didn't receive any input")


async def ask_again(call_sid: str, host: str, attempt: int, reason: str):
    """Ask caller to retry or switch to keypad after 2 failed speech attempts."""
    if attempt >= 3:
        text = "No problem. Please type your Order ID on the keypad and press the hash key."
        play_block = get_play_block(text, host, call_sid)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="dtmf"
            action="https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={attempt}"
            method="POST"
            timeout="20"
            finishOnKey="#">
        {play_block}
    </Gather>
    <Redirect method="POST">https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={attempt}</Redirect>
</Response>"""
    else:
        next_attempt = attempt + 1
        text = f"Sorry, I {reason}. Please say your Order ID again, digit by digit."
        play_block = get_play_block(text, host, call_sid)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech dtmf"
            action="https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={next_attempt}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            finishOnKey="#"
            hints="zero:5, oh:5, one:5, two:5, three:5, four:5, five:5, six:5, seven:5, eight:5, nine:5, double:3, triple:3">
        {play_block}
    </Gather>
    <Redirect method="POST">https://{host}/handle-order-id?call_sid={call_sid}&amp;attempt={next_attempt}</Redirect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")


async def process_order_id(order_id_str: str, call_sid: str, host: str):
    """Process a confirmed order ID — verify, seed Redis, open Deepgram stream."""
    caller_number = redis_client.get(f"caller:{call_sid}")

    if not verify_customer_phone(int(order_id_str), caller_number):
        print(f"[{call_sid}] Phone verification failed")
        twiml = build_transfer_twiml(host, call_sid)
        return Response(content=twiml, media_type="text/xml")

    response_text = chat(order_id_str, call_sid=call_sid)
    print(f"[{call_sid}] Agent: {response_text}")

    from database import get_verified_order
    verified = get_verified_order(call_sid) is not None

    if verified:
        set_call_state(call_sid, is_speaking=False, host=host)
        order_id = get_verified_order(call_sid)
        get_order_context_cached(int(order_id), call_sid)

        play_block = get_play_block(response_text, host, call_sid)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {play_block}
    <Start>
        <Stream url="wss://{host}/media-stream">
            <Parameter name="call_sid" value="{call_sid}"/>
        </Stream>
    </Start>
    <Pause length="60"/>
</Response>"""
    else:
        twiml = build_response_twiml(response_text, host, call_sid, verified=False)

    return Response(content=twiml, media_type="text/xml")


@app.post("/confirm-order-id")
async def confirm_order_id(request: Request, call_sid: str = ""):
    """Handle yes/no confirmation of spoken order ID."""
    host = request.headers.get("host")
    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")

    attempt = int(request.query_params.get("attempt", "1"))
    timeout = request.query_params.get("timeout", "false")

    if timeout == "true":
        order_id_str = redis_client.get(f"pending_order:{call_sid}") or "unknown"
        spaced = ' '.join(list(order_id_str))
        confirm_text = f"I didn't hear a response. Did you say order ID {spaced}? Please say yes or no."
        play_block = get_play_block(confirm_text, host, call_sid)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            speechModel="phone_call"
            hints="yes, no, yeah, nope">
        {play_block}
    </Gather>
    <Redirect method="POST">https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}&amp;timeout=true</Redirect>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    form_data = await request.form()
    speech = form_data.get("SpeechResult", "").strip().lower()

    yes_words = ["yes", "yeah", "yep", "correct", "right", "sure", "confirm", "affirmative"]
    no_words  = ["no", "nope", "wrong", "incorrect", "negative", "retry", "again"]

    confirmed = any(word in speech for word in yes_words)
    denied    = any(word in speech for word in no_words)

    if confirmed:
        order_id_str = redis_client.get(f"pending_order:{call_sid}")
        redis_client.delete(f"pending_order:{call_sid}")
        if order_id_str:
            print(f"[{call_sid}] Order ID confirmed: {order_id_str}")
            return await process_order_id(order_id_str, call_sid, host)
        else:
            return await ask_again(call_sid, host, attempt, "something went wrong")

    elif denied:
        redis_client.delete(f"pending_order:{call_sid}")
        print(f"[{call_sid}] Order ID rejected — attempt {attempt}")
        return await ask_again(call_sid, host, attempt, "let's try again")

    else:
        order_id_str = redis_client.get(f"pending_order:{call_sid}") or "unknown"
        spaced = ' '.join(list(order_id_str))
        confirm_text = f"Sorry, I didn't catch that. Did you say order ID {spaced}? Please say yes or no."
        play_block = get_play_block(confirm_text, host, call_sid)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/confirm-order-id?call_sid={call_sid}&amp;attempt={attempt}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            hints="yes, no, correct, wrong, right, yeah, nope">
        {play_block}
    </Gather>
</Response>"""
        return Response(content=twiml, media_type="text/xml")


# ════════════════════════════════════════════════════
# Deepgram WebSocket
# ════════════════════════════════════════════════════

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """
    Twilio Media Stream → Deepgram live STT with interrupt support.
    Falls back to Twilio STT if Deepgram fails.
    """
    await websocket.accept()

    call_sid = "unknown"
    host = ""
    print(f"[unknown] Media stream connected — waiting for start event")

    async for message in websocket.iter_text():
        data = json.loads(message)
        if data.get("event") == "start":
            call_sid = data["start"].get("callSid", "unknown")
            state = get_call_state(call_sid)
            host = state.get("host", "")
            print(f"[{call_sid}] Media stream identified")
            break

    deepgram_ws = None
    deepgram_connected = False

    try:
        deepgram_client = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))
        deepgram_ws = deepgram_client.listen.asyncwebsocket.v("1")

        async def on_transcript(self, result, **kwargs):
            try:
                sentence = result.channel.alternatives[0].transcript
                is_final = result.is_final

                if not sentence:
                    return

                state = get_call_state(call_sid)
                is_speaking = state.get("is_speaking", False)

                # ── Interrupt detection ──
                # If agent is speaking and we get an interim result
                # with at least 2 words → treat as interrupt
                if is_speaking and not is_final:
                    word_count = len(sentence.strip().split())
                    if word_count >= 2:
                        print(f"[{call_sid}] Interrupt detected: '{sentence}'")

                        # Set interrupt flag in Redis
                        redis_client.hset(f"call:{call_sid}", "is_interrupted", "1")

                        # Cancel active process_transcript task if running
                        task = active_tasks.get(call_sid)
                        if task and not task.done():
                            task.cancel()
                            active_tasks.pop(call_sid, None)
                            print(f"[{call_sid}] Active task cancelled")

                        # Immediately unmute
                        update_call_state(
                            call_sid,
                            is_speaking=False,
                            resumed_at=time.time(),
                            last_activity_at=time.time()
                        )
                    return  # Always return on interim — wait for final

                # ── Final transcript handling ──
                if not is_final:
                    return

                # Ignore if still muted (non-interrupt case)
                if is_speaking:
                    print(f"[{call_sid}] Muted — ignoring: '{sentence}'")
                    return

                # Cooldown — discard buffered audio just after unmute
                resumed_at = state.get("resumed_at", 0)
                if time.time() - resumed_at < 1.0:
                    print(f"[{call_sid}] Cooldown — discarding buffered audio: '{sentence}'")
                    return

                # Cancel any existing task before starting new one
                existing_task = active_tasks.get(call_sid)
                if existing_task and not existing_task.done():
                    existing_task.cancel()

                # Start new process_transcript task and track it
                task = asyncio.create_task(
                    process_transcript(call_sid, sentence, host)
                )
                active_tasks[call_sid] = task

            except Exception as e:
                print(f"[{call_sid}] Transcript error: {e}")

        async def on_error(self, error, **kwargs):
            print(f"[{call_sid}] Deepgram error: {error}")

        deepgram_ws.on(LiveTranscriptionEvents.Transcript, on_transcript)
        deepgram_ws.on(LiveTranscriptionEvents.Error, on_error)

        options = LiveOptions(
            model="nova-2",
            language="en-IN",
            encoding="mulaw",
            sample_rate=8000,
            endpointing=300,
            interim_results=True,   # ← CHANGED: needed for interrupt detection
        )

        result = await deepgram_ws.start(options)
        if result is False:
            raise Exception("Deepgram connection rejected — check API key")

        deepgram_connected = True
        print(f"[{call_sid}] Deepgram live connection opened")

        async for message in websocket.iter_text():
            data = json.loads(message)
            if data.get("event") == "media":
                audio_chunk = base64.b64decode(data["media"]["payload"])
                await deepgram_ws.send(audio_chunk)
            elif data.get("event") == "stop":
                print(f"[{call_sid}] Stream stopped")
                break

    except WebSocketDisconnect:
        print(f"[{call_sid}] WebSocket disconnected")

    except Exception as e:
        print(f"[{call_sid}] Deepgram failed: {e} — switching to Twilio STT")
        if host and call_sid != "unknown":
            try:
                from twilio.rest import Client as TwilioClient
                twilio_client = TwilioClient(
                    os.getenv("TWILIO_ACCOUNT_SID"),
                    os.getenv("TWILIO_AUTH_TOKEN")
                )
                fallback_twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/handle-speech?call_sid={call_sid}"
            method="POST"
            speechTimeout="auto"
            timeout="15"
            language="en-IN"
            speechModel="phone_call">
        <Say voice="Polly.Joanna">Sorry, please say your query.</Say>
    </Gather>
</Response>"""
                twilio_client.calls(call_sid).update(twiml=fallback_twiml)
                print(f"[{call_sid}] Switched to Twilio STT fallback")
            except Exception as fe:
                print(f"[{call_sid}] Fallback failed: {fe}")

    finally:
        if deepgram_connected and deepgram_ws:
            await deepgram_ws.finish()

        # Cancel any active task
        task = active_tasks.pop(call_sid, None)
        if task and not task.done():
            task.cancel()

        audio_path = current_audio_files.pop(call_sid, None)
        if audio_path and os.path.exists(audio_path):
            os.unlink(audio_path)

        delete_call_state(call_sid)
        print(f"[{call_sid}] Stream cleaned up")


# ════════════════════════════════════════════════════
# Twilio STT fallback
# ════════════════════════════════════════════════════

@app.post("/handle-speech")
async def handle_speech(
    request: Request,
    SpeechResult: str = Form(default=""),
    call_sid: str = ""
):
    """Fallback Twilio STT handler — only used if Deepgram fails"""
    host = request.headers.get("host")

    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")

    final_transcript = SpeechResult.strip()
    print(f"[{call_sid}] Twilio STT fallback: '{final_transcript}'")

    if not final_transcript:
        sorry_text = "Sorry, I didn't catch that. Please say your query again."
        play_block = get_play_block(sorry_text, host, call_sid)
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech"
            action="https://{host}/handle-speech?call_sid={call_sid}"
            method="POST"
            speechTimeout="auto"
            timeout="10"
            language="en-IN"
            speechModel="phone_call">
        {play_block}
    </Gather>
</Response>"""
        return Response(content=twiml, media_type="text/xml")

    goodbye_words = ["goodbye", "bye", "thank you", "thanks", "that's all"]
    if any(word in final_transcript.lower() for word in goodbye_words):
        end_call(call_sid)

    response_text = get_cached_response(call_sid, final_transcript)

    if not response_text:
        response_text = chat(final_transcript, call_sid=call_sid)

        # ── NEW: LLM requested transfer ──
        if response_text == "TRANSFER_TO_HUMAN":
            twiml = build_transfer_twiml(host, call_sid)
            return Response(content=twiml, media_type="text/xml")

        if response_text:
            store_llm_response(call_sid, final_transcript, response_text)

    if response_text is None:
        twiml = build_transfer_twiml(host, call_sid)
        return Response(content=twiml, media_type="text/xml")

    print(f"[{call_sid}] Agent: {response_text}")
    twiml = build_response_twiml(response_text, host, call_sid, verified=True)
    return Response(content=twiml, media_type="text/xml")


# ════════════════════════════════════════════════════
# Recording routes
# ════════════════════════════════════════════════════

@app.post("/handle-recording")
async def handle_recording(request: Request, call_sid: str = ""):
    if not call_sid:
        call_sid = request.query_params.get("call_sid", "unknown")

    form_data = await request.form()
    recording_url = form_data.get("RecordingUrl", "")
    recording_sid = form_data.get("RecordingSid", "")

    if recording_url:
        save_recording(call_sid, recording_url, recording_sid)

    return Response(content="OK", media_type="text/plain")


@app.post("/recording-status")
async def recording_status(request: Request):
    form_data = await request.form()
    status = form_data.get("RecordingStatus", "")
    recording_sid = form_data.get("RecordingSid", "")
    recording_url = form_data.get("RecordingUrl", "")
    call_sid = form_data.get("CallSid", "")

    print(f"Recording {recording_sid} status: {status}")

    if status == "completed" and recording_url and call_sid:
        save_recording(call_sid, recording_url, recording_sid)

    return Response(content="OK", media_type="text/plain")


# ════════════════════════════════════════════════════
# Admin routes
# ════════════════════════════════════════════════════

@app.get("/admin/calls")
async def view_calls():
    calls = get_all_calls()
    html = """
    <html>
    <head>
        <title>Call Centre Admin</title>
        <style>
            body { font-family: Arial; padding: 20px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
            th { background: #4CAF50; color: white; }
            tr:nth-child(even) { background: #f2f2f2; }
            a { color: #4CAF50; }
        </style>
    </head>
    <body>
        <h1>Call Centre — All Calls</h1>
        <table>
            <tr>
                <th>Call SID (click for transcript)</th>
                <th>From</th>
                <th>Started</th>
                <th>Ended</th>
                <th>Status</th>
                <th>Recording</th>
                <th>Messages</th>
            </tr>
    """

    for call in calls:
        recording_link = f"<a href='{call[5]}' target='_blank'>▶️ Play</a>" if call[5] else "No recording"
        html += f"""
        <tr>
            <td><a href='/admin/transcript/{call[0]}'>{call[0]}</a></td>
            <td>{call[1]}</td>
            <td>{call[2]}</td>
            <td>{call[3] or 'Active'}</td>
            <td>{call[4]}</td>
            <td>{recording_link}</td>
            <td>{call[6]}</td>
        </tr>"""

    html += "</table></body></html>"
    return Response(content=html, media_type="text/html")


@app.get("/admin/transcript/{call_sid}")
async def view_transcript(call_sid: str):
    transcript = get_call_transcript(call_sid)
    html = f"""
    <html>
    <head>
        <title>Transcript</title>
        <style>
            body {{ font-family: Arial; padding: 20px; }}
            pre {{ background: #f5f5f5; padding: 20px; border-radius: 8px;
                   white-space: pre-wrap; word-wrap: break-word; }}
        </style>
    </head>
    <body>
        <h1>Transcript: {call_sid}</h1>
        <a href='/admin/calls'>← Back to all calls</a><br><br>
        <pre>{transcript}</pre>
    </body>
    </html>"""
    return Response(content=html, media_type="text/html")


# ════════════════════════════════════════════════════
# Startup
# ════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        redis_client.ping()
        print("Redis connected successfully")
    except Exception:
        print("ERROR: Redis is not running. Start Redis before starting the server.")
        exit(1)

    print("Starting AI Call Centre Server...")
    print("Server running on http://localhost:5000")
    print("Admin panel: http://localhost:5000/admin/calls")
    uvicorn.run(app, host="0.0.0.0", port=5000)
