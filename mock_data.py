from datetime import datetime, timedelta

# Dynamically generate dates relative to "today" to make the demo work correctly at any time
_today = datetime.now().date()
_tomorrow = _today + timedelta(days=1)
_day_after = _today + timedelta(days=2)

# Find Friday of this week
_today_weekday = _today.weekday()
_friday = _today + timedelta(days=(4 - _today_weekday) % 7)

# Format dates
_date_today_str = _today.strftime("%Y-%m-%d")
_date_tomorrow_str = _tomorrow.strftime("%Y-%m-%d")
_date_day_after_str = _day_after.strftime("%Y-%m-%d")
_date_friday_str = _friday.strftime("%Y-%m-%d")

PROVIDERS = [
    {
        "id": 1,
        "name": "Downtown Dental Care",
        "type": "dentist",
        "phone": "555-0101",
        "address": "123 Main St, Downtown",
        "rating": 4.7,
        "distance_km": 2.3,
        "available_slots": [
            f"{_date_tomorrow_str} 10:00",
            f"{_date_tomorrow_str} 14:00",
            f"{_date_tomorrow_str} 16:30",
            f"{_date_day_after_str} 09:00"
        ]
    },
    {
        "id": 2,
        "name": "City Dental Clinic",
        "type": "dentist",
        "phone": "555-0202",
        "address": "456 Oak Ave, Midtown",
        "rating": 4.3,
        "distance_km": 5.8,
        "available_slots": [
            f"{_date_tomorrow_str} 11:00",
            f"{_date_tomorrow_str} 15:00",
            f"{_date_day_after_str} 10:00"
        ]
    },
    {
        "id": 3,
        "name": "Uptown Dental Associates",
        "type": "dentist",
        "phone": "555-0303",
        "address": "789 Hill Rd, Uptown",
        "rating": 4.9,
        "distance_km": 8.2,
        "available_slots": [
            f"{_date_day_after_str} 14:00",
            f"{_date_friday_str} 09:00",
            f"{_date_friday_str} 11:00"
        ]
    },
    {
        "id": 4,
        "name": "QuickCuts Hair Salon",
        "type": "hair_salon",
        "phone": "555-0404",
        "address": "321 Style Blvd, Downtown",
        "rating": 4.5,
        "distance_km": 1.9,
        "available_slots": [
            f"{_date_tomorrow_str} 12:00",
            f"{_date_tomorrow_str} 14:30",
            f"{_date_tomorrow_str} 17:00"
        ]
    },
    {
        "id": 5,
        "name": "Elite Auto Repair",
        "type": "auto_repair",
        "phone": "555-0505",
        "address": "654 Garage Way, Industrial",
        "rating": 4.6,
        "distance_km": 6.5,
        "available_slots": [
            f"{_date_tomorrow_str} 08:00",
            f"{_date_day_after_str} 08:00",
            f"{_date_day_after_str} 13:00"
        ]
    }
]

# Mock user calendar (to check conflicts)
USER_CALENDAR = [
    {
        "date": _date_tomorrow_str,
        "time": "09:00-11:00",
        "event": "Team Meeting"
    },
    {
        "date": _date_tomorrow_str,
        "time": "14:00-15:00",
        "event": "Client Call"
    },
    {
        "date": _date_day_after_str,
        "time": "13:00-14:30",
        "event": "Lunch with Sarah"
    }
]

def get_providers_by_type(service_type):
    """Filter providers by service type"""
    return [p for p in PROVIDERS if p["type"] == service_type]

def check_calendar_conflict(date, time):
    """Check if proposed appointment conflicts with calendar"""
    for event in USER_CALENDAR:
        if event["date"] == date:
            if "-" in event["time"]:
                start, end = [x.strip() for x in event["time"].split("-", 1)]
                if start <= time < end:
                    return True, event["event"]
    return False, None

# Test the data
if __name__ == "__main__":
    print("📊 Mock Provider Database\n")
    print(f"Total providers: {len(PROVIDERS)}")
    print(f"Calendar events: {len(USER_CALENDAR)}")
    
    dentists = get_providers_by_type("dentist")
    print(f"\nDentists: {len(dentists)}")
    for d in dentists:
        print(f"  - {d['name']} ({d['rating']}⭐, {d['distance_km']}km)")