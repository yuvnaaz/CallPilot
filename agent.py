import os
import sys
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

# Reconfigure stdout to use UTF-8 to prevent UnicodeEncodeError when printing emojis on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Load environment variables
load_dotenv()

class CallPilotAgent:
    def __init__(self):
        self.api_key = os.getenv("ELEVENLABS_API_KEY")
        self.client = ElevenLabs(api_key=self.api_key)
        
    def test_connection(self):
        """Test if ElevenLabs API is working"""
        try:
            # Get list of available voices
            voices = self.client.voices.get_all()
            print("✅ ElevenLabs Connected Successfully!")
            print(f"✅ Found {len(voices.voices)} available voices")
            
            # Show first 3 voices
            print("\n📢 Sample Voices:")
            for voice in voices.voices[:3]:
                print(f"  - {voice.name} (ID: {voice.voice_id})")
            
            return True
            
        except Exception as e:
            print(f"❌ Error: {e}")
            return False
    
    def generate_test_speech(self, text="Hello, this is CallPilot testing voice generation."):
        """Generate a quick test audio"""
        try:
            print(f"\n🎙️ Generating speech: '{text}'")
            
            # Use ElevenLabs v2.x text_to_speech.convert method
            audio = self.client.text_to_speech.convert(
                voice_id="21m00Tcm4TlvDq8ikWAM",  # Rachel Voice ID
                text=text,
                model_id="eleven_monolingual_v1"
            )
            
            # Save audio file
            with open("test_audio.mp3", "wb") as f:
                for chunk in audio:
                    f.write(chunk)
            
            print("✅ Audio saved to test_audio.mp3")
            return True
            
        except Exception as e:
            print(f"❌ Error generating speech: {e}")
            return False

# Quick test
if __name__ == "__main__":
    print("🚀 CallPilot - Testing ElevenLabs Connection\n")
    
    agent = CallPilotAgent()
    
    # Test 1: Connection
    if agent.test_connection():
        print("\n" + "="*50)
        # Test 2: Generate speech
        if agent.generate_test_speech():
            print("\n✅ All tests passed! Ready to build CallPilot!")
        else:
            print("\n❌ Speech generation failed! Please check your voice settings.")
    else:
        print("\n❌ Fix the API key and try again")