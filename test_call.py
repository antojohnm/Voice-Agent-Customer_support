from twilio.rest import Client
from dotenv import load_dotenv
import os

load_dotenv()

client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

call = client.calls.create(
    to="+916380904132",      
    from_=os.getenv("TWILIO_PHONE_NUMBER"),
    url="https://unsubtly-tutu-tile.ngrok-free.dev/incoming-call"
)

print(f"Call triggered!")
print(f"Call SID: {call.sid}")
print("Pick up your phone when it rings!")