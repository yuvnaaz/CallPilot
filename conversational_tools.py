from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

try:
    from .mock_data import PROVIDERS, USER_CALENDAR
    from .tools import rank_providers
except ImportError:
    from mock_data import PROVIDERS, USER_CALENDAR
    from tools import rank_providers

logger = logging.getLogger(__name__)


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


@dataclass
class BookingRecord:
    booking_id: str
    provider_id: str
    provider_name: str
    slot: str
    service_type: str
    created_at: str


@dataclass
class SessionContext:
    session_id: str
    latest_candidates: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    bookings: List[BookingRecord] = field(default_factory=list)


class ConversationalToolError(Exception):
    """Raised when a tool call is invalid or cannot be completed safely."""


class ConversationalToolsService:
    """Tool-call service used by the ElevenLabs conversational agent.

    The service exposes deterministic, validated methods so the voice agent can
    call tools safely during a live conversation.

    Environment requirements:
    - GOOGLE_MAPS_API_KEY (optional; improves provider discovery)
    - DEFAULT_SEARCH_LOCATION (optional)
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: Dict[str, SessionContext] = {}

    async def get_or_create_session(self, session_id: str) -> SessionContext:
        """Return session context, creating it if missing."""
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionContext(session_id=session_id)
            return self._sessions[session_id]

    async def clear_session(self, session_id: str) -> None:
        """Remove session data when call ends."""
        async with self._lock:
            self._sessions.pop(session_id, None)

    @staticmethod
    def _validate_date(date_text: str) -> None:
        if not DATE_RE.match(date_text):
            raise ConversationalToolError("date must use YYYY-MM-DD format")
        try:
            datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError as exc:
            raise ConversationalToolError("invalid calendar date") from exc

    @staticmethod
    def _validate_time(time_text: str) -> None:
        if not TIME_RE.match(time_text):
            raise ConversationalToolError("time must use HH:MM format")
        try:
            datetime.strptime(time_text, "%H:%M")
        except ValueError as exc:
            raise ConversationalToolError("invalid time value") from exc

    async def check_calendar(self, session_id: str, date: str, time: str) -> Dict[str, Any]:
        """Check calendar conflict status for a proposed date/time."""
        self._validate_date(date)
        self._validate_time(time)

        conflict_event: Optional[str] = None
        for event in USER_CALENDAR:
            if event.get("date") != date:
                continue
            event_range = event.get("time", "")
            if "-" not in event_range:
                continue
            start, end = [x.strip() for x in event_range.split("-", 1)]
            if start <= time < end:
                conflict_event = event.get("event", "Busy")
                break

        result = {
            "session_id": session_id,
            "date": date,
            "time": time,
            "has_conflict": bool(conflict_event),
            "conflict_event": conflict_event,
        }
        logger.info("tool.check_calendar result=%s", result)
        return result

    async def find_providers(
        self,
        session_id: str,
        service_type: str,
        location: str = "San Francisco, CA",
        preferred_day: str = "this week",
        preferred_time: str = "any",
        max_results: int = 3,
        weights: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Find and rank providers for the requested service.

        Uses rank_providers() from tools.py and stores candidates in session context
        so a follow-up booking tool call can validate provider/slot choices.
        """
        service_type = (service_type or "").strip().lower()
        if not service_type:
            raise ConversationalToolError("service_type is required")

        max_results = max(1, min(int(max_results), 15))

        ranked = await asyncio.to_thread(
            rank_providers,
            service=service_type,
            preferred_day=preferred_day,
            preferred_time=preferred_time,
            max_results=max_results,
            location=location,
            weights=weights,
        )

        session = await self.get_or_create_session(session_id)
        session.latest_candidates = {}
        for provider in ranked:
            provider_id = str(provider.get("id"))
            if provider_id:
                session.latest_candidates[provider_id] = provider

        response = {
            "session_id": session_id,
            "service_type": service_type,
            "location": location,
            "max_results": max_results,
            "providers": ranked,
            "count": len(ranked),
        }
        logger.info("tool.find_providers count=%s service=%s", len(ranked), service_type)
        return response

    async def book_appointment(
        self,
        session_id: str,
        provider_id: str,
        slot: str,
        service_type: str = "dentist",
    ) -> Dict[str, Any]:
        """Confirm a booking against validated candidates for this session."""
        provider_id = str(provider_id).strip()
        slot = (slot or "").strip()
        if not provider_id:
            raise ConversationalToolError("provider_id is required")
        if not slot:
            raise ConversationalToolError("slot is required")

        session = await self.get_or_create_session(session_id)
        provider = session.latest_candidates.get(provider_id)

        if provider is None:
            # Controlled fallback: resolve from static mock providers by id/name only.
            provider = next((p for p in PROVIDERS if str(p.get("id")) == provider_id), None)

        if provider is None:
            raise ConversationalToolError("provider_id was not found in current candidate set")

        has_conflict = False
        conflict_name = None
        try:
            date_part, time_part = slot.split(" ", 1)
            calendar_status = await self.check_calendar(session_id, date_part, time_part)
            has_conflict = bool(calendar_status.get("has_conflict"))
            conflict_name = calendar_status.get("conflict_event")
        except Exception:
            # If slot is malformed, reject instead of hallucinating success.
            raise ConversationalToolError("slot must use YYYY-MM-DD HH:MM format")

        if has_conflict:
            raise ConversationalToolError(f"slot conflicts with calendar event: {conflict_name}")

        booking = BookingRecord(
            booking_id=f"bk_{uuid4().hex[:10]}",
            provider_id=provider_id,
            provider_name=str(provider.get("name", "Unknown Provider")),
            slot=slot,
            service_type=service_type,
            created_at=datetime.utcnow().isoformat() + "Z",
        )
        session.bookings.append(booking)

        result = {
            "status": "confirmed",
            "booking_id": booking.booking_id,
            "provider_id": booking.provider_id,
            "provider_name": booking.provider_name,
            "slot": booking.slot,
            "service_type": booking.service_type,
            "created_at": booking.created_at,
        }
        logger.info("tool.book_appointment confirmed booking_id=%s", booking.booking_id)
        return result

    async def list_session_bookings(self, session_id: str) -> Dict[str, Any]:
        """Return all bookings created in a live session."""
        session = await self.get_or_create_session(session_id)
        return {
            "session_id": session_id,
            "bookings": [
                {
                    "booking_id": b.booking_id,
                    "provider_id": b.provider_id,
                    "provider_name": b.provider_name,
                    "slot": b.slot,
                    "service_type": b.service_type,
                    "created_at": b.created_at,
                }
                for b in session.bookings
            ],
        }


def build_tool_schemas() -> List[Dict[str, Any]]:
    """Return JSON-schema-like metadata for agent configuration/docs.

    This is useful when configuring the agent prompt or documenting expected
    client-side tools available during conversation.
    """
    return [
        {
            "name": "check_calendar",
            "description": "Check if a date/time conflicts with user calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM"},
                },
                "required": ["date", "time"],
            },
        },
        {
            "name": "find_providers",
            "description": "Find and rank providers by service, availability, rating, and distance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_type": {"type": "string"},
                    "location": {"type": "string"},
                    "preferred_day": {"type": "string"},
                    "preferred_time": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 15},
                },
                "required": ["service_type"],
            },
        },
        {
            "name": "book_appointment",
            "description": "Book a selected provider slot after user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider_id": {"type": "string"},
                    "slot": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                    "service_type": {"type": "string"},
                },
                "required": ["provider_id", "slot"],
            },
        },
    ]


if __name__ == "__main__":
    import asyncio
    import json

    logging.basicConfig(level=logging.INFO)

    async def _demo() -> None:
        svc = ConversationalToolsService()
        session_id = "demo-session"
        print(json.dumps(await svc.check_calendar(session_id, "2026-02-08", "14:00"), indent=2))
        providers = await svc.find_providers(
            session_id=session_id,
            service_type="dentist",
            location="Austin, TX",
            max_results=3,
        )
        print(json.dumps(providers, indent=2)[:1200])

    asyncio.run(_demo())
