from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import uuid4

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

if TYPE_CHECKING:
    from elevenlabs.client import AsyncElevenLabs

try:
    from .conversational_tools import ConversationalToolError, ConversationalToolsService
except ImportError:
    from conversational_tools import ConversationalToolError, ConversationalToolsService

logger = logging.getLogger(__name__)


class ConversationState(str, Enum):
    """High-level call state for UI and debugging."""

    LISTENING = "listening"
    PROCESSING = "processing"
    CALLING_PROVIDERS = "calling_providers"
    CONFIRMING = "confirming"
    ENDED = "ended"
    ERROR = "error"


@dataclass
class VoiceSession:
    """Represents one live browser<->agent voice session."""

    session_id: str
    agent_id: str
    websocket: WebSocket
    tools: ConversationalToolsService
    requires_auth: bool = True
    user_id: Optional[str] = None
    state: ConversationState = ConversationState.LISTENING
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    conversation_id: Optional[str] = None

    _eleven_client: Optional[Any] = None
    _eleven_ws: Optional[websockets.WebSocketClientProtocol] = None
    _audio_input_queue: "asyncio.Queue[bytes]" = field(default_factory=asyncio.Queue)
    _audio_forward_task: Optional[asyncio.Task] = None
    _recv_task: Optional[asyncio.Task] = None
    _closed: bool = False

    async def start(self) -> None:
        """Start the ElevenLabs conversation websocket and background tasks."""
        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing")

        from elevenlabs.client import AsyncElevenLabs

        self._eleven_client = AsyncElevenLabs(api_key=api_key)
        ws_url = await self._resolve_ws_url()

        logger.info("session=%s connecting to ElevenLabs websocket", self.session_id)
        self._eleven_ws = await websockets.connect(ws_url, max_size=16 * 1024 * 1024)

        await self._emit(
            {
                "type": "session_started",
                "session_id": self.session_id,
                "agent_id": self.agent_id,
                "state": self.state,
            }
        )

        await self._send_initiation_message()

        self._audio_forward_task = asyncio.create_task(self._forward_input_audio_loop(), name=f"audio_forward_{self.session_id}")
        self._recv_task = asyncio.create_task(self._recv_loop(), name=f"recv_loop_{self.session_id}")

    async def stop(self) -> None:
        """Stop this session and release resources."""
        if self._closed:
            return
        self._closed = True
        self.state = ConversationState.ENDED

        for task in (self._audio_forward_task, self._recv_task):
            if task and not task.done():
                task.cancel()

        if self._eleven_ws is not None:
            try:
                await self._eleven_ws.close()
            except Exception:
                pass
            self._eleven_ws = None

        await self.tools.clear_session(self.session_id)
        await self._emit({"type": "state", "state": self.state})

    async def send_audio_chunk(self, audio_chunk: bytes) -> None:
        """Queue PCM16 audio chunk from frontend.

        Required format by ElevenLabs ConvAI websocket: 16-bit PCM mono, 16kHz.
        """
        if self._closed:
            return
        if not audio_chunk:
            return
        await self._audio_input_queue.put(audio_chunk)

    async def send_user_text(self, text: str) -> None:
        """Send text fallback message to the live agent session."""
        if not self._eleven_ws:
            raise RuntimeError("ElevenLabs websocket is not connected")
        payload = {"type": "user_message", "text": text}
        await self._eleven_ws.send(json.dumps(payload))

    async def register_user_activity(self) -> None:
        """Ping user activity to avoid idle timeout."""
        if not self._eleven_ws:
            return
        payload = {"type": "user_activity"}
        await self._eleven_ws.send(json.dumps(payload))

    async def send_contextual_update(self, text: str) -> None:
        """Send non-interrupting context update to the agent."""
        if not self._eleven_ws:
            return
        payload = {"type": "contextual_update", "text": text}
        await self._eleven_ws.send(json.dumps(payload))

    async def _resolve_ws_url(self) -> str:
        """Resolve authenticated websocket URL using ElevenLabs SDK."""
        if not self._eleven_client:
            raise RuntimeError("ElevenLabs client is not initialized")

        if self.requires_auth:
            signed = await self._eleven_client.conversational_ai.conversations.get_signed_url(
                agent_id=self.agent_id,
                include_conversation_id=True,
            )
            signed_url = signed.signed_url
            sep = "&" if "?" in signed_url else "?"
            return f"{signed_url}{sep}source=callpilot"

        return f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={self.agent_id}&source=callpilot"

    async def _send_initiation_message(self) -> None:
        """Send conversation initiation payload immediately after socket connect."""
        if not self._eleven_ws:
            return

        message = {
            "type": "conversation_initiation_client_data",
            "custom_llm_extra_body": {},
            "conversation_config_override": {},
            "dynamic_variables": {
                "session_id": self.session_id,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            },
        }
        if self.user_id:
            message["user_id"] = self.user_id

        await self._eleven_ws.send(json.dumps(message))

    async def _forward_input_audio_loop(self) -> None:
        """Read audio chunks from queue and stream to ElevenLabs."""
        assert self._eleven_ws is not None

        while not self._closed:
            chunk = await self._audio_input_queue.get()
            if self._closed:
                break
            payload = {
                "user_audio_chunk": base64.b64encode(chunk).decode("utf-8"),
            }
            try:
                await self._eleven_ws.send(json.dumps(payload))
            except Exception as exc:
                logger.exception("session=%s failed to forward audio: %s", self.session_id, exc)
                await self._emit_error("Failed to stream audio to ElevenLabs")
                await self.stop()
                break

    async def _recv_loop(self) -> None:
        """Receive events from ElevenLabs and relay to frontend + tools."""
        assert self._eleven_ws is not None

        while not self._closed:
            try:
                raw_msg = await self._eleven_ws.recv()
            except ConnectionClosed:
                logger.info("session=%s ElevenLabs websocket closed", self.session_id)
                await self.stop()
                break
            except Exception as exc:
                logger.exception("session=%s websocket receive error: %s", self.session_id, exc)
                await self._emit_error("ElevenLabs websocket receive failed")
                await self.stop()
                break

            try:
                message = json.loads(raw_msg)
            except json.JSONDecodeError:
                logger.warning("session=%s received non-json message", self.session_id)
                continue

            await self._handle_elevenlabs_message(message)

    async def _handle_elevenlabs_message(self, message: Dict[str, Any]) -> None:
        """Handle all ElevenLabs websocket event types."""
        msg_type = message.get("type")

        if msg_type == "conversation_initiation_metadata":
            meta = message.get("conversation_initiation_metadata_event", {})
            self.conversation_id = meta.get("conversation_id")
            await self._emit(
                {
                    "type": "conversation_ready",
                    "conversation_id": self.conversation_id,
                    "session_id": self.session_id,
                }
            )
            return

        if msg_type == "audio":
            event = message.get("audio_event", {})
            audio_b64 = event.get("audio_base_64")
            if audio_b64:
                await self._emit({"type": "audio", "audio_base64": audio_b64})
            return

        if msg_type == "agent_response":
            text = (message.get("agent_response_event", {}) or {}).get("agent_response", "").strip()
            await self._set_state(ConversationState.PROCESSING)
            await self._emit({"type": "agent_response", "text": text})
            return

        if msg_type == "agent_chat_response_part":
            event = message.get("text_response_part", {})
            await self._emit(
                {
                    "type": "agent_chat_response_part",
                    "text": event.get("text", ""),
                    "part": event.get("type", "delta"),
                }
            )
            return

        if msg_type == "user_transcript":
            transcript = (message.get("user_transcription_event", {}) or {}).get("user_transcript", "").strip()
            await self._set_state(ConversationState.PROCESSING)
            await self._emit({"type": "user_transcript", "text": transcript})
            return

        if msg_type == "interruption":
            await self._set_state(ConversationState.LISTENING)
            await self._emit({"type": "interruption"})
            return

        if msg_type == "ping":
            ping_event = message.get("ping_event", {})
            if self._eleven_ws:
                pong = {"type": "pong", "event_id": ping_event.get("event_id")}
                await self._eleven_ws.send(json.dumps(pong))
            await self._emit(
                {
                    "type": "latency",
                    "ping_ms": ping_event.get("ping_ms"),
                }
            )
            return

        if msg_type == "client_tool_call":
            await self._set_state(ConversationState.CALLING_PROVIDERS)
            await self._handle_tool_call(message.get("client_tool_call", {}))
            return

        if msg_type == "error":
            await self._emit_error(f"ElevenLabs error: {message}")
            await self._set_state(ConversationState.ERROR)
            return

        # Forward unknown events for debugging.
        await self._emit({"type": "event", "event": message})

    async def _handle_tool_call(self, tool_call: Dict[str, Any]) -> None:
        """Execute requested tool and send result back to ElevenLabs."""
        if not self._eleven_ws:
            return

        tool_name = tool_call.get("tool_name")
        tool_call_id = tool_call.get("tool_call_id")
        parameters = tool_call.get("parameters", {}) or {}

        await self._emit(
            {
                "type": "tool_call",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "parameters": parameters,
            }
        )

        try:
            result = await self._execute_tool(tool_name=tool_name, params=parameters)
            payload = {
                "type": "client_tool_result",
                "tool_call_id": tool_call_id,
                "result": result,
                "is_error": False,
            }
            await self._eleven_ws.send(json.dumps(payload))
            await self._emit(
                {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "result": result,
                }
            )
            await self._set_state(ConversationState.CONFIRMING)
        except ConversationalToolError as exc:
            payload = {
                "type": "client_tool_result",
                "tool_call_id": tool_call_id,
                "result": str(exc),
                "is_error": True,
            }
            await self._eleven_ws.send(json.dumps(payload))
            await self._emit_error(f"Tool validation failed: {exc}")
        except Exception as exc:
            logger.exception("session=%s tool execution failed: %s", self.session_id, exc)
            payload = {
                "type": "client_tool_result",
                "tool_call_id": tool_call_id,
                "result": "Internal tool execution error",
                "is_error": True,
            }
            await self._eleven_ws.send(json.dumps(payload))
            await self._emit_error("Tool execution failed")

    async def _execute_tool(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch supported agent tools with strict parameter validation."""
        if tool_name == "check_calendar":
            date = str(params.get("date", ""))
            time_text = str(params.get("time", ""))
            return await self.tools.check_calendar(self.session_id, date=date, time=time_text)

        if tool_name == "find_providers":
            return await self.tools.find_providers(
                session_id=self.session_id,
                service_type=str(params.get("service_type", "")).strip(),
                location=str(params.get("location", "San Francisco, CA")),
                preferred_day=str(params.get("preferred_day", "this week")),
                preferred_time=str(params.get("preferred_time", "any")),
                max_results=int(params.get("max_results", 3)),
                weights=params.get("weights") if isinstance(params.get("weights"), dict) else None,
            )

        if tool_name == "book_appointment":
            return await self.tools.book_appointment(
                session_id=self.session_id,
                provider_id=str(params.get("provider_id", "")),
                slot=str(params.get("slot", "")),
                service_type=str(params.get("service_type", "dentist")),
            )

        raise ConversationalToolError(f"Unsupported tool: {tool_name}")

    async def _set_state(self, new_state: ConversationState) -> None:
        if self.state == new_state:
            return
        self.state = new_state
        await self._emit({"type": "state", "state": self.state})

    async def _emit(self, payload: Dict[str, Any]) -> None:
        """Send event to browser websocket with failure protection."""
        if self._closed:
            return
        try:
            await self.websocket.send_json(payload)
        except Exception:
            logger.debug("session=%s websocket emit failed, closing", self.session_id)
            await self.stop()

    async def _emit_error(self, message: str) -> None:
        await self._emit({"type": "error", "message": message})


class VoiceAgentManager:
    """Manage browser websocket sessions for live ElevenLabs conversations."""

    def __init__(self, tools_service: ConversationalToolsService) -> None:
        self._tools = tools_service
        self._sessions: Dict[str, VoiceSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        websocket: WebSocket,
        agent_id: str,
        user_id: Optional[str] = None,
        requires_auth: bool = True,
    ) -> VoiceSession:
        """Create and start a new live session."""
        session_id = f"vs_{uuid4().hex[:12]}"
        session = VoiceSession(
            session_id=session_id,
            agent_id=agent_id,
            websocket=websocket,
            tools=self._tools,
            user_id=user_id,
            requires_auth=requires_auth,
        )
        async with self._lock:
            self._sessions[session_id] = session

        await session.start()
        return session

    async def close_session(self, session_id: str) -> None:
        """Stop and remove a session by ID."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            await session.stop()

    async def handle_socket(self, websocket: WebSocket) -> None:
        """Main socket loop for voice-first UI.

        Frontend message types:
        - start: {type, agent_id, user_id?, requires_auth?}
        - audio: {type, audio_base64}
        - user_message: {type, text}
        - user_activity: {type}
        - contextual_update: {type, text}
        - stop: {type}
        """
        await websocket.accept()
        active_session: Optional[VoiceSession] = None

        try:
            while True:
                message = await websocket.receive()

                if "bytes" in message and message["bytes"] is not None:
                    if active_session:
                        await active_session.send_audio_chunk(message["bytes"])
                    continue

                raw_text = message.get("text")
                if not raw_text:
                    continue

                try:
                    payload = json.loads(raw_text)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "message": "Invalid JSON payload"})
                    continue

                event_type = payload.get("type")

                if event_type == "start":
                    if active_session is not None:
                        await websocket.send_json({"type": "error", "message": "Session already started"})
                        continue
                    agent_id = str(payload.get("agent_id", "")).strip()
                    if not agent_id:
                        await websocket.send_json({"type": "error", "message": "agent_id is required"})
                        continue
                    requires_auth = bool(payload.get("requires_auth", True))
                    user_id = payload.get("user_id")
                    active_session = await self.create_session(
                        websocket=websocket,
                        agent_id=agent_id,
                        user_id=str(user_id) if user_id else None,
                        requires_auth=requires_auth,
                    )
                    continue

                if active_session is None:
                    await websocket.send_json({"type": "error", "message": "Send start event first"})
                    continue

                if event_type == "audio":
                    b64 = payload.get("audio_base64", "")
                    if b64:
                        try:
                            audio = base64.b64decode(b64)
                            await active_session.send_audio_chunk(audio)
                        except Exception:
                            await websocket.send_json({"type": "error", "message": "Invalid audio payload"})
                    continue

                if event_type == "user_message":
                    text = str(payload.get("text", "")).strip()
                    if text:
                        await active_session.send_user_text(text)
                    continue

                if event_type == "user_activity":
                    await active_session.register_user_activity()
                    continue

                if event_type == "contextual_update":
                    text = str(payload.get("text", "")).strip()
                    if text:
                        await active_session.send_contextual_update(text)
                    continue

                if event_type == "stop":
                    break

                await websocket.send_json({"type": "error", "message": f"Unsupported event type: {event_type}"})

        except WebSocketDisconnect:
            logger.info("Browser websocket disconnected")
        finally:
            if active_session:
                await self.close_session(active_session.session_id)


if __name__ == "__main__":
    """Basic import smoke-test.

    Run:
        python backend/voice_agent.py
    """
    logging.basicConfig(level=logging.INFO)
    print("voice_agent module loaded successfully")
