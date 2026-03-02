from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

try:
    from .conversational_tools import ConversationalToolsService
    from .mock_data import PROVIDERS
    from .tools import parse_user_request, rank_providers, simulate_call_log, simulate_tool_logs
    from .twilio_handler import TwilioIntegrationService
    from .voice_agent import VoiceAgentManager
except ImportError:
    from conversational_tools import ConversationalToolsService
    from mock_data import PROVIDERS
    from tools import parse_user_request, rank_providers, simulate_call_log, simulate_tool_logs
    from twilio_handler import TwilioIntegrationService
    from voice_agent import VoiceAgentManager


load_dotenv()

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
BOOKING_EXECUTOR = ThreadPoolExecutor(max_workers=6)

app = FastAPI(title="CallPilot Production Voice API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_http_client: Optional[httpx.AsyncClient] = None
conversational_tools = ConversationalToolsService()
voice_agent_manager = VoiceAgentManager(tools_service=conversational_tools)
twilio_service = TwilioIntegrationService()

# In-memory store for confirmed bookings from the form flow (not voice session)
_confirmed_bookings: List[Dict[str, Any]] = []


class BookingRequest(BaseModel):
    request_text: str
    max_calls: int = 3
    weights: Optional[Dict[str, float]] = None
    call_mode: str = "parallel"  # "parallel" | "sequential"


class BookConfirmRequest(BaseModel):
    winner: Dict[str, Any]
    parsed: Dict[str, Any]


class TTSRequest(BaseModel):
    text: str
    voice_id: Optional[str] = None


class OutboundCallRequest(BaseModel):
    to_number: str
    provider_id: Optional[str] = None
    provider_name: Optional[str] = None
    user_id: Optional[str] = None


@app.on_event("startup")
async def _startup() -> None:
    """Initialize shared async resources."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Shutdown async resources gracefully."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def _get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise HTTPException(status_code=500, detail="HTTP client not initialized")
    return _http_client


def _get_elevenlabs_api_key() -> str:
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="ELEVENLABS_API_KEY missing in .env")
    return api_key


def _default_voice_id() -> str:
    return os.getenv("ELEVENLABS_VOICE_ID", "").strip() or "21m00Tcm4TlvDq8ikWAM"


def _quick_fallback_rank(service: str, max_calls: int) -> List[Dict[str, Any]]:
    filtered = [p for p in PROVIDERS if p.get("type") == service][:max_calls]
    ranked: List[Dict[str, Any]] = []
    for provider in filtered:
        rating = float(provider.get("rating", 4.0))
        distance_km = float(provider.get("distance_km", 7.0))
        distance_score = max(0.0, 1.0 - min(distance_km, 10.0) / 10.0)
        final_score = (rating / 5.0) * 0.7 + distance_score * 0.3
        ranked.append(
            {
                "id": provider.get("id"),
                "name": provider.get("name"),
                "type": provider.get("type"),
                "rating": rating,
                "distance_km": distance_km,
                "phone": provider.get("phone", "N/A"),
                "address": provider.get("address", ""),
                "slot": (provider.get("available_slots") or [None])[0],
                "score": round(final_score * 10, 2),
                "availability_score": 0.5,
                "rating_score": round(rating / 5.0, 3),
                "distance_score": round(distance_score, 3),
                "data_source": "mock_data_fallback",
            }
        )
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    """Service health endpoint with integration readiness flags."""
    return {
        "status": "ok",
        "service": "callpilot-production-voice",
        "google_places_enabled": str(bool(os.getenv("GOOGLE_MAPS_API_KEY"))).lower(),
        "elevenlabs_enabled": str(bool(os.getenv("ELEVENLABS_API_KEY"))).lower(),
        "twilio_config": twilio_service.configured,
    }


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    """Return client config (e.g. agent_id for live voice Jump in)."""
    agent_id = os.getenv("ELEVENLABS_AGENT_ID", "").strip()
    return {"elevenlabs_agent_id": agent_id or None}


@app.post("/api/book")
async def book_appointment(payload: BookingRequest) -> Dict[str, Any]:
    """Core ranking flow used by web UI and agent tools."""
    max_calls = max(1, min(payload.max_calls, 15))
    parsed = parse_user_request(payload.request_text)

    ranking_source = "primary"
    try:
        ranked = await asyncio.wait_for(
            asyncio.to_thread(
                rank_providers,
                service=parsed["service"],
                preferred_day=parsed["preferred_day"],
                preferred_time=parsed["preferred_time"],
                max_results=max_calls,
                location=parsed["location"],
                weights=payload.weights,
            ),
            timeout=4.0,
        )
    except asyncio.TimeoutError:
        ranking_source = "timeout_fallback"
        ranked = _quick_fallback_rank(parsed["service"], max_calls)
    except Exception:
        ranking_source = "error_fallback"
        ranked = _quick_fallback_rank(parsed["service"], max_calls)

    if not ranked:
        return {
            "success": False,
            "message": "No providers found for that service.",
            "parsed": parsed,
            "options": [],
            "call_logs": [],
            "winner": None,
            "ranking_source": ranking_source,
        }

    winner = ranked[0]
    call_logs = [
        {
            "provider": option["name"],
            "log": simulate_call_log(option["name"], option.get("slot"), parsed["service"]),
        }
        for option in ranked
    ]
    tool_logs = simulate_tool_logs(parsed, ranked)

    confirmation = (
        f"Booked {parsed['service'].replace('_', ' ')} at {winner['name']} "
        f"for {winner['slot']}. Rating {winner['rating']} stars, {winner['distance_km']} km away."
        if winner.get("slot")
        else f"Best provider is {winner['name']}, but no free slot matched perfectly."
    )

    return {
        "success": True,
        "message": confirmation,
        "parsed": parsed,
        "location_used": parsed["location"],
        "max_calls": max_calls,
        "weights_used": payload.weights or {"availability": 0.4, "rating": 0.3, "distance": 0.3},
        "options": ranked,
        "call_logs": call_logs,
        "tool_logs": tool_logs,
        "swarm_mode": max_calls > 1,
        "swarm_parallel_calls_simulated": len(ranked),
        "ranking_source": ranking_source,
        "winner": winner,
        "call_mode": payload.call_mode,
    }


@app.post("/api/book/confirm")
async def confirm_booking(payload: BookConfirmRequest) -> Dict[str, Any]:
    """Confirm and store the selected appointment (from form flow)."""
    winner = payload.winner
    parsed = payload.parsed
    service = (parsed.get("service") or "dentist").replace("_", " ")
    slot = winner.get("slot")
    provider_name = winner.get("name", "Unknown Provider")
    provider_id = str(winner.get("id", ""))
    if not slot:
        raise HTTPException(status_code=400, detail="Winner has no slot; cannot book.")
    booking = {
        "booking_id": f"bk_{len(_confirmed_bookings) + 1:04d}",
        "provider_id": provider_id,
        "provider_name": provider_name,
        "slot": slot,
        "service_type": service,
        "address": winner.get("address", ""),
        "phone": winner.get("phone", ""),
        "rating": winner.get("rating"),
        "distance_km": winner.get("distance_km"),
    }
    _confirmed_bookings.append(booking)
    return {
        "success": True,
        "message": f"Booked {service} at {provider_name} for {slot}.",
        "booking": booking,
    }


@app.get("/api/bookings")
async def list_bookings() -> Dict[str, Any]:
    """List all confirmed bookings from the form flow."""
    return {"bookings": list(_confirmed_bookings), "count": len(_confirmed_bookings)}


async def _stream_sequential_booking(
    parsed: Dict[str, Any],
    ranked: List[Dict[str, Any]],
    call_logs: List[Dict[str, Any]],
    tool_logs: List[str],
    winner: Dict[str, Any],
    confirmation: str,
) -> Any:
    """Async generator that yields SSE events for sequential call simulation."""
    yield f"data: {json.dumps({'type': 'ranking_done', 'options': ranked, 'tool_logs': tool_logs})}\n\n"
    for i, entry in enumerate(call_logs):
        provider = entry.get("provider", "Provider")
        lines = entry.get("log", [])
        yield f"data: {json.dumps({'type': 'call_start', 'call_index': i, 'provider': provider})}\n\n"
        for line in lines:
            await asyncio.sleep(0.4)
            if line.startswith("CallPilot:"):
                speaker, text = "CallPilot", line.replace("CallPilot:", "").strip()
            elif ":" in line and not line.startswith("Calling"):
                speaker = line.split(":")[0].strip()
                text = line.split(":", 1)[1].strip() if ":" in line else line
            else:
                speaker = "system"
                text = line
            yield f"data: {json.dumps({'type': 'transcript', 'call_index': i, 'provider': provider, 'speaker': speaker, 'text': text})}\n\n"
        yield f"data: {json.dumps({'type': 'call_end', 'call_index': i, 'provider': provider})}\n\n"
    yield f"data: {json.dumps({'type': 'done', 'winner': winner, 'message': confirmation})}\n\n"


@app.post("/api/book/stream")
async def book_appointment_stream(payload: BookingRequest) -> StreamingResponse:
    """Stream booking flow with live transcript (sequential calls). Use when call_mode=sequential."""
    max_calls = max(1, min(payload.max_calls, 15))
    parsed = parse_user_request(payload.request_text)
    ranking_source = "primary"
    try:
        ranked = await asyncio.wait_for(
            asyncio.to_thread(
                rank_providers,
                service=parsed["service"],
                preferred_day=parsed["preferred_day"],
                preferred_time=parsed["preferred_time"],
                max_results=max_calls,
                location=parsed["location"],
                weights=payload.weights,
            ),
            timeout=4.0,
        )
    except (asyncio.TimeoutError, Exception):
        ranking_source = "error_fallback"
        ranked = _quick_fallback_rank(parsed["service"], max_calls)

    if not ranked:
        async def empty_stream() -> Any:
            yield f"data: {json.dumps({'type': 'done', 'success': False, 'message': 'No providers found.', 'options': [], 'winner': None})}\n\n"
        return StreamingResponse(
            empty_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    winner = ranked[0]
    call_logs = [
        {"provider": opt["name"], "log": simulate_call_log(opt["name"], opt.get("slot"), parsed["service"])}
        for opt in ranked
    ]
    tool_logs = simulate_tool_logs(parsed, ranked)
    confirmation = (
        f"Booked {parsed['service'].replace('_', ' ')} at {winner['name']} "
        f"for {winner['slot']}. Rating {winner['rating']} stars, {winner['distance_km']} km away."
        if winner.get("slot")
        else f"Best provider is {winner['name']}, but no free slot matched perfectly."
    )

    return StreamingResponse(
        _stream_sequential_booking(parsed, ranked, call_logs, tool_logs, winner, confirmation),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/voice/tts")
async def text_to_speech(payload: TTSRequest) -> Response:
    """ElevenLabs TTS endpoint used by both classic and live UIs."""
    api_key = _get_elevenlabs_api_key()
    voice_id = payload.voice_id or _default_voice_id()
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    body = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
    }

    client = _get_http_client()
    try:
        resp = await client.post(f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach ElevenLabs TTS: {exc}") from exc

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"ElevenLabs error {resp.status_code}: {resp.text[:200]}")

    return Response(content=resp.content, media_type="audio/mpeg")


@app.get("/api/voice/voices")
async def get_elevenlabs_voices() -> Dict[str, Any]:
    """Return available ElevenLabs voices for selection in UI."""
    api_key = _get_elevenlabs_api_key()
    client = _get_http_client()

    try:
        resp = await client.get(f"{ELEVENLABS_BASE_URL}/voices", headers={"xi-api-key": api_key})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach ElevenLabs voices API: {exc}") from exc

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"ElevenLabs voices error: {resp.status_code}")

    data = resp.json()
    voices = [
        {
            "voice_id": v.get("voice_id"),
            "name": v.get("name"),
            "category": v.get("category", "unknown"),
        }
        for v in data.get("voices", [])
    ]
    return {"voices": voices, "default_voice_id": _default_voice_id()}


@app.post("/api/voice/stt")
async def speech_to_text(file: UploadFile = File(...)) -> Dict[str, Any]:
    """ElevenLabs STT endpoint for recorded mic audio."""
    api_key = _get_elevenlabs_api_key()
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")

    client = _get_http_client()
    files = {
        "file": (file.filename or "audio.webm", audio_bytes, file.content_type or "audio/webm"),
    }
    data = {"model_id": "scribe_v1"}

    try:
        resp = await client.post(
            f"{ELEVENLABS_BASE_URL}/speech-to-text",
            headers={"xi-api-key": api_key},
            files=files,
            data=data,
            timeout=25.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach ElevenLabs STT API: {exc}") from exc

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"ElevenLabs STT error {resp.status_code}: {resp.text[:200]}")

    payload = resp.json()
    text = payload.get("text") or payload.get("transcript") or ""
    return {"text": text, "raw": payload}


@app.websocket("/api/voice/live/ws")
async def live_voice_websocket(websocket: WebSocket) -> None:
    """Live browser websocket endpoint for full ElevenLabs conversational session."""
    await voice_agent_manager.handle_socket(websocket)


@app.get("/api/voice/live/bookings/{session_id}")
async def list_live_session_bookings(session_id: str) -> Dict[str, Any]:
    """Inspect bookings created by agent tool calls in a session."""
    return await conversational_tools.list_session_bookings(session_id)


@app.post("/api/twilio/inbound")
async def twilio_inbound_call(
    request: Request,
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    To: str = Form(default=""),
    Direction: str = Form(default="inbound"),
) -> Response:
    """Inbound Twilio webhook: register call with ElevenLabs and return TwiML."""
    # request is injected for future signature validation; currently unused
    _ = request
    call_sid = CallSid or f"call_{os.urandom(4).hex()}"
    twiml = await twilio_service.register_inbound_call(
        call_sid=call_sid,
        from_number=From,
        to_number=To,
        user_id=None,
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/api/twilio/status")
async def twilio_status_callback(
    CallSid: str = Form(default=""),
    CallStatus: str = Form(default=""),
    RecordingUrl: str = Form(default=""),
) -> Dict[str, Any]:
    """Twilio status callback endpoint for call lifecycle tracking and recording pipeline."""
    if not CallSid:
        raise HTTPException(status_code=400, detail="CallSid is required")

    return await twilio_service.update_call_state(
        call_sid=CallSid,
        call_status=CallStatus,
        recording_url=RecordingUrl or None,
    )


@app.post("/api/twilio/outbound")
async def twilio_outbound_call(payload: OutboundCallRequest) -> Dict[str, Any]:
    """Launch outbound provider call (real if configured, simulated otherwise)."""
    to_number = payload.to_number.strip()
    if not to_number:
        raise HTTPException(status_code=400, detail="to_number is required")

    return await twilio_service.launch_outbound_call(
        to_number=to_number,
        provider_id=payload.provider_id,
        provider_name=payload.provider_name,
        user_id=payload.user_id,
    )


@app.get("/api/twilio/calls")
async def list_twilio_calls() -> Dict[str, Any]:
    """List tracked Twilio call records."""
    return await twilio_service.list_calls()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
