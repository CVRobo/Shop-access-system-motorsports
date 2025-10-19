from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import os
from dotenv import load_dotenv

load_dotenv()  # loads .env
SLACK_TOKEN = os.getenv("SLACK_TOKEN")

# Replace with your Bot User OAuth Token
SLACK_TOKEN = ""  
CHANNEL = "#testch11"  # the channel you added your bot to

client = WebClient(token=SLACK_TOKEN)

try:
    response = client.chat_postMessage(
        channel=CHANNEL,
        text="Hello World"
    )
    print("Message sent! Timestamp:", response['ts'])
except SlackApiError as e:
    print("Error sending message:", e.response['error'])
