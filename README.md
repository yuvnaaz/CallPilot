# CallPilot MVP

CallPilot is a FastAPI + vanilla JS demo that takes a natural-language booking request, finds/ranks providers, simulates or runs call flows, and confirms the best appointment slot.

## Features

- Parse user requests like: “Book me a dentist appointment tomorrow afternoon”
- Rank providers by availability, rating, and distance
- Parallel and sequential (streamed) call simulation modes
- Booking confirmation + in-memory booking list
- Optional Google Places lookup for real providers
- Optional ElevenLabs integrations:
  - Text-to-speech (`/api/voice/tts`)
  - Speech-to-text (`/api/voice/stt`)
  - Live conversational voice websocket (`/api/voice/live/ws`)
- Optional Twilio webhook + outbound call bridge

## Tech Stack

- Backend: FastAPI, Uvicorn, HTTPX, python-dotenv
- Voice/Telephony: ElevenLabs SDK, Twilio SDK
- Frontend: HTML/CSS/JS (no framework)

## Project Structure

```text
callpilot/
├── backend/
│   ├── main.py
│   ├── tools.py
│   ├── conversational_tools.py
│   ├── voice_agent.py
│   ├── twilio_handler.py
│   └── mock_data.py
├── frontend/
│   ├── index.html
│   └── voice_interface.html
├── requirements.txt
├── .env.example
└── README.md
