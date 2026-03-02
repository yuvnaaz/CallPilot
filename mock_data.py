# Mock provider database for demo

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
            "2024-02-09 10:00",
            "2024-02-09 14:00",
            "2024-02-09 16:30",
            "2024-02-10 09:00"
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
            "2024-02-09 11:00",
            "2024-02-09 15:00",
            "2024-02-10 10:00"
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
            "2024-02-10 14:00",
            "2024-02-11 09:00",
            "2024-02-11 11:00"
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
            "2024-02-09 12:00",
            "2024-02-09 14:30",
            "2024-02-09 17:00"
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
            "2024-02-09 08:00",
            "2024-02-10 08:00",
            "2024-02-10 13:00"
        ]
    }
]

# Mock user calendar (to check conflicts)
USER_CALENDAR = [
    {
        "date": "2024-02-09",
        "time": "09:00-11:00",
        "event": "Team Meeting"
    },
    {
        "date": "2024-02-09",
        "time": "14:00-15:00",
        "event": "Client Call"
    },
    {
        "date": "2024-02-10",
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
            # Simple conflict check (can be improved)
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