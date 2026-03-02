from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import uuid4

if TYPE_CHECKING:
    from elevenlabs.client import AsyncElevenLabs

logger = logging.getLogger(__name__)


class CallState(str, Enum):
    """Tracked call lifecycle states."""

    RINGING = "ringing"
    ANSWERED = "answered"
    IN_PROGRESS = "in-progress"
    BUSY = "busy"
    NO_ANSWER = "no-answer"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class TrackedCall:
    """In-memory call record for monitoring and demo visibility."""

    call_sid: str
    direction: str
    from_number: str
    to_number: str
    state: CallState
    provider_id: Optional[str] = None
    provider_name: Optional[str] = None
    recording_url: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


class TwilioIntegrationService:
    """Async Twilio + ElevenLabs bridge used by backend endpoints.

    Environment variables:
    - ELEVENLABS_API_KEY (required)
    - ELEVENLABS_AGENT_ID (required for inbound register_call)
    - ELEVENLABS_AGENT_PHONE_NUMBER_ID (required for outbound_call)
    - TWILIO_PHONE_NUMBER (recommended)
    """

    def __init__(self) -> None:
        self._client: Optional[Any] = None
        self._agent_id = os.getenv("ELEVENLABS_AGENT_ID", "").strip()
        self._agent_phone_number_id = os.getenv("ELEVENLABS_AGENT_PHONE_NUMBER_ID", "").strip()
        self._twilio_phone_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
        self._calls: Dict[str, TrackedCall] = {}

    def _get_client(self) -> Any:
        """Lazily build ElevenLabs async client to avoid import-time latency."""
        if self._client is None:
            from elevenlabs.client import AsyncElevenLabs

            api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
            self._client = AsyncElevenLabs(api_key=api_key)
        return self._client

    @property
    def configured(self) -> Dict[str, bool]:
        """Return current telephony configuration status."""
        return {
            "elevenlabs_api_key": bool(os.getenv("ELEVENLABS_API_KEY", "").strip()),
            "agent_id": bool(self._agent_id),
            "agent_phone_number_id": bool(self._agent_phone_number_id),
            "twilio_phone_number": bool(self._twilio_phone_number),
        }

    async def register_inbound_call(
        self,
        call_sid: str,
        from_number: str,
        to_number: str,
        user_id: Optional[str] = None,
    ) -> str:
        """Register inbound Twilio call with ElevenLabs and return TwiML."""
        self._upsert_call(
            call_sid=call_sid,
            direction="inbound",
            from_number=from_number,
            to_number=to_number,
            state=CallState.RINGING,
        )

        if not self._agent_id:
            logger.warning("ELEVENLABS_AGENT_ID missing, returning fallback TwiML")
            return self._fallback_twiml("CallPilot is not configured for live calls yet.")

        try:
            extra_data: Dict[str, Any] = {}
            if user_id:
                extra_data["user_id"] = user_id

            client = self._get_client()
            raw_response = await client.conversational_ai.twilio.with_raw_response.register_call(
                agent_id=self._agent_id,
                from_number=from_number,
                to_number=to_number,
                # Keep direction as plain string to avoid importing heavy SDK enum modules at startup.
                direction="inbound",
                conversation_initiation_client_data=extra_data or None,
            )

            twiml = raw_response.response.text.strip() if raw_response and raw_response.response else ""
            if not twiml:
                logger.warning("register_call returned empty TwiML, using fallback")
                return self._fallback_twiml("Unable to start the AI agent right now.")

            return twiml
        except Exception as exc:
            logger.exception("register_inbound_call failed: %s", exc)
            return self._fallback_twiml("CallPilot hit a temporary error. Please try again.")

    async def launch_outbound_call(
        self,
        *,
        to_number: str,
        provider_id: Optional[str] = None,
        provider_name: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Trigger outbound provider call through ElevenLabs Twilio bridge.

        Falls back to simulation if outbound config is incomplete.
        """
        if not self._agent_id or not self._agent_phone_number_id:
            simulated_sid = f"sim_{uuid4().hex[:10]}"
            self._upsert_call(
                call_sid=simulated_sid,
                direction="outbound",
                from_number=self._twilio_phone_number or "N/A",
                to_number=to_number,
                state=CallState.IN_PROGRESS,
                provider_id=provider_id,
                provider_name=provider_name,
            )
            return {
                "success": True,
                "simulated": True,
                "call_sid": simulated_sid,
                "message": "Outbound telephony not fully configured; simulated outbound call created.",
            }

        try:
            extra_data: Dict[str, Any] = {}
            if user_id:
                extra_data["user_id"] = user_id

            client = self._get_client()
            response = await client.conversational_ai.twilio.outbound_call(
                agent_id=self._agent_id,
                agent_phone_number_id=self._agent_phone_number_id,
                to_number=to_number,
                conversation_initiation_client_data=extra_data or None,
            )

            # SDK response fields may vary over time, so we read defensively.
            call_sid = (
                getattr(response, "call_sid", None)
                or getattr(response, "sid", None)
                or getattr(response, "id", None)
                or f"out_{uuid4().hex[:10]}"
            )

            self._upsert_call(
                call_sid=str(call_sid),
                direction="outbound",
                from_number=self._twilio_phone_number or "N/A",
                to_number=to_number,
                state=CallState.RINGING,
                provider_id=provider_id,
                provider_name=provider_name,
            )

            return {
                "success": True,
                "simulated": False,
                "call_sid": str(call_sid),
                "response": self._safe_model_dump(response),
            }
        except Exception as exc:
            logger.exception("launch_outbound_call failed: %s", exc)
            return {
                "success": False,
                "simulated": False,
                "error": str(exc),
            }

    async def update_call_state(
        self,
        *,
        call_sid: str,
        call_status: str,
        recording_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update call state from Twilio status callbacks."""
        mapped = self._map_status(call_status)
        if call_sid in self._calls:
            call = self._calls[call_sid]
            call.state = mapped
            call.updated_at = datetime.utcnow().isoformat() + "Z"
            if recording_url:
                call.recording_url = recording_url
        else:
            self._upsert_call(
                call_sid=call_sid,
                direction="unknown",
                from_number="unknown",
                to_number="unknown",
                state=mapped,
                recording_url=recording_url,
            )

        return {
            "success": True,
            "call_sid": call_sid,
            "state": mapped,
            "recording_url": recording_url,
        }

    async def list_calls(self) -> Dict[str, Any]:
        """Return current tracked call records."""
        return {
            "count": len(self._calls),
            "calls": [asdict(call) for call in self._calls.values()],
        }

    def _upsert_call(
        self,
        *,
        call_sid: str,
        direction: str,
        from_number: str,
        to_number: str,
        state: CallState,
        provider_id: Optional[str] = None,
        provider_name: Optional[str] = None,
        recording_url: Optional[str] = None,
    ) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        if call_sid in self._calls:
            call = self._calls[call_sid]
            call.state = state
            call.updated_at = now
            if recording_url:
                call.recording_url = recording_url
            return

        self._calls[call_sid] = TrackedCall(
            call_sid=call_sid,
            direction=direction,
            from_number=from_number,
            to_number=to_number,
            state=state,
            provider_id=provider_id,
            provider_name=provider_name,
            recording_url=recording_url,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _map_status(raw_status: str) -> CallState:
        status = (raw_status or "").strip().lower()
        mapping = {
            "ringing": CallState.RINGING,
            "answered": CallState.ANSWERED,
            "in-progress": CallState.IN_PROGRESS,
            "in_progress": CallState.IN_PROGRESS,
            "busy": CallState.BUSY,
            "no-answer": CallState.NO_ANSWER,
            "no_answer": CallState.NO_ANSWER,
            "completed": CallState.COMPLETED,
            "failed": CallState.FAILED,
            "canceled": CallState.CANCELED,
            "cancelled": CallState.CANCELED,
        }
        return mapping.get(status, CallState.IN_PROGRESS)

    @staticmethod
    def _safe_model_dump(model: Any) -> Dict[str, Any]:
        """Serialize SDK response without assuming pydantic version internals."""
        if model is None:
            return {}
        if hasattr(model, "model_dump"):
            try:
                return model.model_dump()
            except Exception:
                pass
        if hasattr(model, "dict"):
            try:
                return model.dict()
            except Exception:
                pass
        return {"repr": repr(model)}

    @staticmethod
    def _fallback_twiml(message: str) -> str:
        """Return minimal valid TwiML fallback response."""
        safe_msg = (message or "CallPilot is currently unavailable.").replace("<", "").replace(">", "")
        return (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<Response>"
            f"<Say voice=\"Polly.Joanna\">{safe_msg}</Say>"
            "<Hangup/>"
            "</Response>"
        )


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)

    async def _demo() -> None:
        svc = TwilioIntegrationService()
        print("configured:", svc.configured)
        print(await svc.list_calls())

    asyncio.run(_demo())
