# 🤖 AI Call Centre

An intelligent, voice-based customer support system that handles real phone calls using AI. Customers can call a real phone number, speak naturally, and get accurate responses about their orders, promotions, return policies, warranties, and store information — all powered by a live database.

---

## 📋 Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Database Schema](#database-schema)
- [Setup and Installation](#setup-and-installation)
- [Environment Variables](#environment-variables)
- [Running the Project](#running-the-project)
- [How It Works](#how-it-works)
- [API Routes](#api-routes)
- [Admin Panel](#admin-panel)

---

## Overview

This project is a fully functional AI-powered call centre built for a retail electronics company. When a customer calls the Twilio phone number, they are connected to **Maya** — an AI voice agent that can:

- Answer questions about promotions, offers, and discounts
- Explain return and refund policies
- Provide warranty information
- Share store locations and timings
- Look up live order status, delivery updates, and payment details using the customer's Order ID

The system uses **Deepgram** for real-time speech-to-text transcription, **Groq LLaMA** as the primary LLM with **OpenAI GPT** as a fallback, and **ElevenLabs** for natural text-to-speech with **Twilio Polly** as a fallback. All conversations are stored permanently in **PostgreSQL**, with **Redis** used for fast in-call caching.

---

## Architecture

```
Customer's Phone
      ↕ (real phone call)
   Twilio
      ↕ (WebSocket — real-time audio stream)
Your Python Server (FastAPI)
      ↕                    ↕                      ↕
Deepgram STT          Groq LLaMA             ElevenLabs TTS
(speech → text)    (text → response)       (response → audio)
      ↕                    ↕
   Redis               PostgreSQL
(fast cache)        (permanent storage)
```

### Call Flow

```
1. Customer calls Twilio number
2. Server plays ElevenLabs greeting (Maya introduces herself)
3. Deepgram opens WebSocket — listens to caller in real time
4. Customer speaks → Deepgram transcribes
5. llm.py determines intent:
   ├── General query (offers, policies, stores) → answers from DB
   ├── Order-specific query → asks for Order ID
   └── Order ID provided → verifies, loads full order context
6. LLM generates response using injected DB data
7. ElevenLabs converts response to audio → Twilio plays to caller
8. Loop continues until call ends
9. Redis cleared → PostgreSQL keeps permanent transcript + recording URL
```

---

## Features

### Voice Agent — Maya
- Natural, warm, empathetic personality
- Introduces herself on first turn — does not demand Order ID upfront
- Detects customer sentiment (ANGRY, FRUSTRATED, WORRIED, THREATENING_TO_CANCEL)
- Acknowledges emotions before answering
- Handles difficult situations gracefully

### Intelligent Query Routing
- General queries (offers, policies, stores) answered without Order ID
- Order-specific queries trigger Order ID collection
- Spoken order IDs parsed from both digits ("1001") and words ("one zero zero one")

### Anti-Hallucination System
- LLM only uses data explicitly injected into the system prompt
- Product-specific context fetched from database before each response
- Hard rules in prompt prevent inventing offers, policies, or order details
- If data unavailable: agent says so honestly rather than making things up

### Product Query Handling
- Customer must specify product or category before receiving offer/policy/warranty info
- Agent asks clarifying question if no product mentioned
- Category aliases handle natural speech ("smart ones" → Smartphones)
- Last discussed category stored in Redis for conversation continuity

### Dual Failover System
- **LLM:** Groq LLaMA → OpenAI GPT fallback
- **TTS:** ElevenLabs → Twilio Polly fallback
- **STT:** Deepgram → Twilio built-in STT fallback
- If both LLMs fail → call transferred to human agent

### Caching (Redis)
- Per-call order context cached for duration of call
- Intent-based response caching (status, delivery, payment, items, date)
- Global product categories cached for 5 minutes — shared across all callers
- All per-call data auto-expires (15 min TTL)
- Cache immediately invalidated when products are added/updated

### Call Recording and Transcripts
- Every call recorded via Twilio — recording URL stored in PostgreSQL
- Full conversation transcript stored per call
- Admin panel to view all calls and read transcripts

---

## Tech Stack

| Component | Technology |
|---|---|
| **Phone Infrastructure** | Twilio (Voice, Media Streams) |
| **Real-time STT** | Deepgram (Whisper fallback) |
| **LLM (Primary)** | Groq — LLaMA 3.1 8B Instant |
| **LLM (Fallback)** | OpenAI — GPT-3.5 Turbo |
| **TTS (Primary)** | ElevenLabs — Sarah voice |
| **TTS (Fallback)** | Twilio Polly — Joanna |
| **Web Framework** | FastAPI + Uvicorn |
| **Primary Database** | PostgreSQL |
| **Cache / Session Store** | Redis |
| **Tunnelling (dev)** | ngrok |
| **Language** | Python 3.10+ |

---

## Project Structure

```
ai-call-centre/
│
├── server.py          # FastAPI server — handles Twilio webhooks,
│                      # Deepgram WebSocket, TTS generation, routing
│
├── llm.py             # AI brain — system prompt management,
│                      # sentiment detection, intent routing,
│                      # LLM calls with Groq/OpenAI fallback
│
├── database.py        # All PostgreSQL + Redis operations —
│                      # call state, conversation history,
│                      # order context, product data, caching
│
├── stt.py             # Local STT using Groq Whisper
│                      # (used by main.py for desktop testing)
│
├── tts.py             # Local TTS using ElevenLabs + mpv
│                      # (used by main.py for desktop testing)
│
├── main.py            # Local desktop voice agent for testing
│                      # (no phone needed — uses mic + speakers)
│
├── test_call.py       # Script to trigger a test call via Twilio API
│
├── .env               # API keys and config (never commit this)
├── .gitignore         # Excludes .env and temp audio files
└── requirements.txt   # All Python dependencies
```

---

## Database Schema

### Call Centre Tables

| Table | Description |
|---|---|
| `calls` | One row per call — SID, caller number, timestamps, recording URL |
| `messages` | Full conversation transcript per call |
| `call_verifications` | Stores verified Order ID per call session |

### Business Data Tables

| Table | Description |
|---|---|
| `customers` | Customer name, phone, email, address |
| `orders` | Order ID, status, total amount, date |
| `order_items` | Line items per order — links to inventory |
| `inventory` | Stock items — links to product catalog |
| `product_catalog` | Product names, categories, prices, availability |
| `payments` | Payment method, status, amount per order |
| `deliveries` | Delivery status, address, expected and actual dates |
| `promotions_offers` | Active offers per product with discount and expiry |
| `return_refund_policies` | Return window and exchange rules per product |
| `warranty_information` | Warranty period and coverage per product |
| `store_locations` | Store name, city, opening/closing times |

### Key Relationships

```
customers → orders → order_items → inventory → product_catalog
                  → payments
                  → deliveries

product_catalog → promotions_offers
               → return_refund_policies
               → warranty_information

calls → messages
     → call_verifications
```

---

## Setup and Installation

### Prerequisites

- Python 3.10 or higher
- PostgreSQL 14+
- Redis 6+
- ngrok (for local development)
- ffmpeg (for audio conversion)

### Step 1 — Clone the Repository

```bash
git clone https://github.com/yourusername/ai-call-centre.git
cd ai-call-centre
```

### Step 2 — Install Python Dependencies

```bash
pip install -r requirements.txt
```

### Step 3 — Set Up PostgreSQL

1. Create a database called `call_centre`
2. The tables are created automatically when the server starts via `init_db()`

### Step 4 — Set Up Redis

Start Redis on the default port (6379):
```bash
redis-server
```

### Step 5 — Configure Environment Variables

Create a `.env` file in the project root (see [Environment Variables](#environment-variables) below).

### Step 6 — Set Up ngrok (Development)

```bash
ngrok http 5000
```

Copy the `https://` forwarding URL — you'll need it for Twilio configuration.

### Step 7 — Configure Twilio

1. Buy a Twilio phone number
2. In your Twilio console, set the webhook URL for incoming calls to:
   ```
   https://your-ngrok-url.ngrok-free.app/incoming-call
   ```
3. Verify your mobile number in Twilio's verified caller IDs (trial accounts only)

---

## Environment Variables

Create a `.env` file with the following:

```env
# Groq (Primary LLM + Whisper STT)
GROQ_API_KEY=gsk_your_groq_key

# OpenAI (Fallback LLM)
OPENAI_API_KEY=sk_your_openai_key

# ElevenLabs (Primary TTS)
ELEVENLABS_API_KEY=your_elevenlabs_key

# Twilio (Phone infrastructure)
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_PHONE_NUMBER=+1xxxxxxxxxx

# Human agent number (fallback when both LLMs fail)
HUMAN_AGENT_NUMBER=+91xxxxxxxxxx

# PostgreSQL
DB_HOST=localhost
DB_PORT=5433
DB_NAME=call_centre
DB_USER=postgres
DB_PASSWORD=your_password

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
```

> **Never commit your `.env` file to GitHub.**

---

## Running the Project

### Start the Server

```bash
python server.py
```

Server starts at `http://localhost:5000`

### Start ngrok (separate terminal)

```bash
ngrok http 5000
```

### Trigger a Test Call

```bash
python test_call.py
```

This calls your verified phone number from your Twilio number. Pick up and speak to Maya.

### Run Local Desktop Agent (no phone needed)

```bash
python main.py
```

Uses your microphone and speakers directly for testing without Twilio.

---

## How It Works

### Speech Processing Pipeline

```
Customer speaks
      ↓
Deepgram transcribes in real time
      ↓
llm.py chat() is called with transcript
      ↓
┌─────────────────────────────────────────────┐
│ Intent Detection                            │
│                                             │
│  General query?  → fetch DB context         │
│  Needs order ID? → ask customer for it      │
│  Order ID given? → verify + load order      │
└─────────────────────────────────────────────┘
      ↓
System prompt built with real DB data injected
      ↓
Groq LLaMA generates response
(OpenAI GPT if Groq fails)
      ↓
Response saved to PostgreSQL
      ↓
ElevenLabs generates audio
(Twilio Polly if ElevenLabs fails)
      ↓
Audio played to customer via Twilio
```

### Sentiment Detection

Every customer message is classified as one of:
- `NEUTRAL` — normal conversation
- `FRUSTRATED` — waiting, delays, repeated issues
- `ANGRY` — harsh language, strong dissatisfaction
- `WORRIED` — concern about missing/lost items
- `THREATENING_TO_CANCEL` — mentions cancel, refund, leave

When non-neutral sentiment is detected, the system prompt instructs Maya to acknowledge the emotion first before answering.

### Caching Strategy

```
Redis Key                    TTL        Scope
─────────────────────────────────────────────────────
call:{call_sid}              15 min     Per call — state flags
order_context:{call_sid}     15 min     Per call — order data
responses:{call_sid}         15 min     Per call — intent responses
global:product_categories    5 min      Global — shared across calls
```

---

## API Routes

| Method | Route | Description |
|---|---|---|
| `POST` | `/incoming-call` | Twilio webhook — handles new calls |
| `WS` | `/media-stream` | Deepgram WebSocket — real-time audio |
| `POST` | `/handle-order-id` | DTMF fallback for order ID entry |
| `POST` | `/confirm-order-id` | Confirms order ID before verification |
| `POST` | `/recording-status` | Twilio recording status callback |
| `POST` | `/transfer-status` | Tracks human agent transfer status |
| `GET` | `/audio/{filename}` | Serves ElevenLabs audio files to Twilio |
| `GET` | `/admin/calls` | Admin panel — all calls list |
| `GET` | `/admin/transcript/{call_sid}` | Full transcript of a specific call |

---

## Admin Panel

While the server is running, open in your browser:

**All calls:**
```
http://localhost:5000/admin/calls
```

Shows every call with caller number, timestamps, status, recording link, and message count.

**Individual transcript:**
```
http://localhost:5000/admin/transcript/{call_sid}
```

Shows the full colour-coded conversation transcript with timestamps.

---

## Development Notes

### Adding New Products

Use the `add_product()` function in `database.py` — it automatically invalidates the Redis product categories cache so the next caller gets fresh data:

```python
from database import add_product
add_product("iPhone 16", "Smartphones", 89999.00, stock_available=True)
```

### Updating Stock Availability

```python
from database import update_product_availability
update_product_availability(product_id=1, is_available=False)
```

### Testing Without a Phone

Run `main.py` for a desktop voice conversation that uses your microphone directly — no Twilio or ngrok needed.

---

## Security Notes

- API keys are loaded from `.env` — never hardcoded
- `.env` is excluded from version control via `.gitignore`
- Customer phone numbers stored in PostgreSQL but never read aloud unless explicitly requested
- Per-call Redis data is isolated by `call_sid` — no cross-caller data leakage
- Global Redis cache only contains non-personal product data
