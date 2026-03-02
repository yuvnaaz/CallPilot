# CallPilot MVP

Demo flow: parse request → simulate calls → rank providers → confirm best slot.

## Real providers (Google Maps)

To show **real dentist names, addresses, ratings, and distances** for a city:

1. **Create a Google Cloud project** and enable:
   - [Geocoding API](https://console.cloud.google.com/apis/library/geocoding-backend.googleapis.com)
   - [Places API](https://console.cloud.google.com/apis/library/places-backend.googleapis.com) (or “Places API (Legacy)” if you use the legacy endpoint)

2. **Create an API key** (APIs & Services → Credentials → Create credentials → API key) and restrict it to the APIs above.

3. **Set in `.env`**:
   ```bash
   GOOGLE_MAPS_API_KEY=your_api_key_here
   ```
   Optional: `DEFAULT_SEARCH_LOCATION=San Francisco, CA` (used when the user doesn’t say a city).

With the key set, booking requests use live Google Places results for the requested city. Without it, the app uses mock data.

## Real ElevenLabs elements in this build

- `GET /api/voice/voices` loads your real ElevenLabs voice list.
- `POST /api/voice/tts` generates spoken confirmation with your selected voice.
- `POST /api/voice/stt` transcribes recorded mic audio with ElevenLabs STT.

Set in `.env`:
```bash
ELEVENLABS_API_KEY=your_key_here
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
```
