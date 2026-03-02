from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

try:
    from .mock_data import PROVIDERS, USER_CALENDAR
except ImportError:
    from mock_data import PROVIDERS, USER_CALENDAR

load_dotenv()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
DEFAULT_SEARCH_LOCATION = os.getenv("DEFAULT_SEARCH_LOCATION", "San Francisco, CA").strip()
GOOGLE_TIMEOUT_SECONDS = 5

SERVICE_ALIASES = {
    "dentist": "dentist",
    "dental": "dentist",
    "hair": "hair_salon",
    "salon": "hair_salon",
    "barber": "hair_salon",
    "auto": "auto_repair",
    "car": "auto_repair",
    "repair": "auto_repair",
}


def _parse_slot(slot: str) -> datetime:
    return datetime.strptime(slot, "%Y-%m-%d %H:%M")


def _parse_event_time(time_range: str) -> tuple[str, str]:
    start, end = time_range.split("-")
    return start.strip(), end.strip()


def _slot_conflicts_with_calendar(slot: str) -> tuple[bool, Optional[str]]:
    slot_dt = _parse_slot(slot)
    slot_date = slot_dt.strftime("%Y-%m-%d")
    slot_time = slot_dt.strftime("%H:%M")

    for event in USER_CALENDAR:
        if event["date"] != slot_date:
            continue
        start, end = _parse_event_time(event["time"])
        if start <= slot_time < end:
            return True, event["event"]
    return False, None


def _extract_location_from_text(text: str) -> str:
    lowered = text.lower()
    for marker in (" near ", " in "):
        if marker in lowered:
            idx = lowered.rfind(marker)
            location = text[idx + len(marker):].strip(" .,!?:;")
            if len(location) >= 2:
                return location
    return DEFAULT_SEARCH_LOCATION


def normalize_service(user_text: str) -> str:
    text = user_text.lower()
    for keyword, normalized in SERVICE_ALIASES.items():
        if keyword in text:
            return normalized
    return "dentist"


def parse_user_request(text: str) -> Dict[str, Any]:
    service = normalize_service(text)
    lowered = text.lower()

    preferred_day = "this week"
    if "tomorrow" in lowered:
        preferred_day = "tomorrow"
    elif "friday" in lowered:
        preferred_day = "friday"

    preferred_time = "any"
    if "morning" in lowered:
        preferred_time = "morning"
    elif "afternoon" in lowered:
        preferred_time = "afternoon"
    elif "evening" in lowered:
        preferred_time = "evening"

    return {
        "raw": text,
        "service": service,
        "preferred_day": preferred_day,
        "preferred_time": preferred_time,
        "location": _extract_location_from_text(text),
    }


def _time_bucket(hour: int) -> str:
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    return "evening"


def _matches_preferences(slot: str, preferred_day: str, preferred_time: str) -> bool:
    slot_dt = _parse_slot(slot)

    if preferred_day == "tomorrow":
        tomorrow = datetime.now() + timedelta(days=1)
        if slot_dt.date() != tomorrow.date():
            return False
    elif preferred_day == "friday" and slot_dt.strftime("%A").lower() != "friday":
        return False

    if preferred_time != "any" and _time_bucket(slot_dt.hour) != preferred_time:
        return False

    return True


def _slot_score(slot: str, preferred_day: str, preferred_time: str) -> float:
    score = 0.5
    slot_dt = _parse_slot(slot)

    if preferred_day == "tomorrow":
        tomorrow = datetime.now() + timedelta(days=1)
        if slot_dt.date() == tomorrow.date():
            score += 0.3
    elif preferred_day == "friday" and slot_dt.strftime("%A").lower() == "friday":
        score += 0.3
    elif preferred_day == "this week":
        score += 0.2

    if preferred_time == "any":
        score += 0.2
    elif _time_bucket(slot_dt.hour) == preferred_time:
        score += 0.3

    return min(score, 1.0)


def _best_available_slot(provider: Dict[str, Any], preferred_day: str, preferred_time: str) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for slot in provider["available_slots"]:
        conflict, conflict_event = _slot_conflicts_with_calendar(slot)
        if conflict:
            continue

        if not _matches_preferences(slot, preferred_day, preferred_time):
            continue

        candidates.append({
            "slot": slot,
            "fit_score": _slot_score(slot, preferred_day, preferred_time),
            "calendar_conflict": False,
            "conflict_event": conflict_event,
        })

    if not candidates:
        for slot in provider["available_slots"]:
            conflict, conflict_event = _slot_conflicts_with_calendar(slot)
            if conflict:
                continue
            candidates.append({
                "slot": slot,
                "fit_score": _slot_score(slot, preferred_day, preferred_time) * 0.6,
                "calendar_conflict": False,
                "conflict_event": conflict_event,
            })

    if not candidates:
        return {
            "slot": None,
            "fit_score": 0.0,
            "calendar_conflict": True,
            "conflict_event": "No free slot",
        }

    best = sorted(candidates, key=lambda x: x["fit_score"], reverse=True)[0]
    return best


def _normalize_distance(distance_km: float) -> float:
    max_distance = 10.0
    clamped = min(distance_km, max_distance)
    return 1.0 - (clamped / max_distance)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * c


def _generate_mock_slots() -> List[str]:
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    base = now + timedelta(days=1)
    return [
        (base.replace(hour=10)).strftime("%Y-%m-%d %H:%M"),
        (base.replace(hour=14)).strftime("%Y-%m-%d %H:%M"),
        (base.replace(hour=16)).strftime("%Y-%m-%d %H:%M"),
        (base + timedelta(days=1)).replace(hour=9).strftime("%Y-%m-%d %H:%M"),
    ]


def _google_geocode(location_text: str) -> Optional[tuple[float, float]]:
    """Resolve city/address to lat,lng. Requires Geocoding API enabled and GOOGLE_MAPS_API_KEY."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": location_text, "key": GOOGLE_MAPS_API_KEY}
    try:
        response = requests.get(url, params=params, timeout=GOOGLE_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "OK" or not payload.get("results"):
            return None
        loc = payload["results"][0]["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])
    except Exception:
        return None


def _fetch_place_phone(place_id: str) -> Optional[str]:
    """Get formatted phone number for a place. Requires Places API enabled."""
    if not GOOGLE_MAPS_API_KEY or not place_id:
        return None
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": "formatted_phone_number",
        "key": GOOGLE_MAPS_API_KEY,
    }
    try:
        response = requests.get(url, params=params, timeout=2)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "OK":
            return None
        return (payload.get("result") or {}).get("formatted_phone_number")
    except Exception:
        return None


def _service_query_label(service: str) -> str:
    return {
        "dentist": "dentist",
        "hair_salon": "hair salon",
        "auto_repair": "auto repair",
    }.get(service, service.replace("_", " "))


def _fetch_live_providers_from_google(service: str, location: str, max_results: int) -> List[Dict[str, Any]]:
    """
    Fetch real businesses from Google Places (Text Search) with real names, addresses,
    ratings, and distances from the given location. Requires:
    - GOOGLE_MAPS_API_KEY in env
    - Geocoding API and Places API (or Places API (Legacy)) enabled in Google Cloud.
    """
    if not GOOGLE_MAPS_API_KEY:
        return []

    origin = _google_geocode(location)
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    query = f"{_service_query_label(service)} in {location}"
    params = {"query": query, "key": GOOGLE_MAPS_API_KEY}

    try:
        response = requests.get(url, params=params, timeout=GOOGLE_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            return []
    except Exception:
        return []

    providers: List[Dict[str, Any]] = []
    results = payload.get("results", [])[:max_results]
    for place in results:
        place_loc = place.get("geometry", {}).get("location", {})
        lat = float(place_loc.get("lat", 0.0))
        lng = float(place_loc.get("lng", 0.0))

        if origin and lat and lng:
            distance_km = round(_haversine_km(origin[0], origin[1], lat, lng), 1)
        else:
            distance_km = 7.5

        place_id = place.get("place_id") or place.get("name") or "unknown"
        providers.append(
            {
                "id": place_id,
                "name": place.get("name", "Unknown Provider"),
                "type": service,
                "phone": "N/A",
                "address": place.get("formatted_address") or location,
                "rating": float(place.get("rating", 4.0)),
                "distance_km": distance_km,
                "available_slots": _generate_mock_slots(),
                "source": "google_places",
            }
        )

    return providers


def _normalize_weights(weights: Optional[Dict[str, float]]) -> Tuple[float, float, float]:
    defaults = {
        "availability": 0.4,
        "rating": 0.3,
        "distance": 0.3,
    }
    if not weights:
        return defaults["availability"], defaults["rating"], defaults["distance"]

    availability = float(weights.get("availability", defaults["availability"]))
    rating = float(weights.get("rating", defaults["rating"]))
    distance = float(weights.get("distance", defaults["distance"]))
    total = availability + rating + distance
    if total <= 0:
        return defaults["availability"], defaults["rating"], defaults["distance"]
    return availability / total, rating / total, distance / total


def rank_providers(
    service: str,
    preferred_day: str,
    preferred_time: str,
    max_results: int = 3,
    location: str = DEFAULT_SEARCH_LOCATION,
    weights: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    availability_w, rating_w, distance_w = _normalize_weights(weights)
    live = _fetch_live_providers_from_google(
        service=service,
        location=location,
        max_results=max_results,
    )
    filtered = live if live else [p for p in PROVIDERS if p["type"] == service]
    results = []

    for provider in filtered:
        slot_info = _best_available_slot(provider, preferred_day, preferred_time)
        availability_score = slot_info["fit_score"]
        rating_score = provider["rating"] / 5.0
        distance_score = _normalize_distance(provider["distance_km"])

        final_score = (
            availability_score * availability_w
            + rating_score * rating_w
            + distance_score * distance_w
        )

        results.append({
            "id": provider["id"],
            "name": provider["name"],
            "type": provider["type"],
            "rating": provider["rating"],
            "distance_km": provider["distance_km"],
            "phone": provider["phone"],
            "address": provider.get("address", ""),
            "slot": slot_info["slot"],
            "score": round(final_score * 10, 2),
            "availability_score": round(availability_score, 3),
            "rating_score": round(rating_score, 3),
            "distance_score": round(distance_score, 3),
            "data_source": provider.get("source", "mock_data"),
        })

    ranked = sorted(results, key=lambda x: x["score"], reverse=True)
    return ranked[:max_results]


def simulate_call_log(provider_name: str, slot: Optional[str], service: str) -> List[str]:
    if not slot:
        return [
            f"Calling {provider_name}...",
            f"{provider_name}: Sorry, no available {service} slot right now.",
            "CallPilot: Thanks, ending call.",
        ]

    return [
        f"Calling {provider_name}...",
        f"CallPilot: Hi, I need a {service.replace('_', ' ')} appointment.",
        f"{provider_name}: We can do {slot}.",
        "CallPilot: Great, please hold that slot.",
        "CallPilot: Confirming with user now.",
    ]


def simulate_tool_logs(parsed: Dict[str, Any], providers: List[Dict[str, Any]]) -> List[str]:
    logs = [
        "tool.parse_request -> extracted service/day/time/location",
        f"tool.lookup_providers -> service={parsed['service']} location={parsed['location']}",
        f"tool.get_candidates -> count={len(providers)}",
        "tool.check_calendar -> conflicts filtered",
        "tool.rank_providers -> weighted scoring",
    ]
    if providers:
        logs.append(f"tool.select_best_match -> {providers[0]['name']}")
    return logs
