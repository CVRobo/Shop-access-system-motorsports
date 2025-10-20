import os
import time
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.errors import SlackApiError

# -------------------------
# Load environment variables
# -------------------------
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")

# -------------------------
# Initialize Slack clients
# -------------------------
web_client = WebClient(token=SLACK_BOT_TOKEN)
socket_client = SocketModeClient(app_token=SLACK_APP_TOKEN, web_client=web_client)

# -------------------------
# Mock data
# -------------------------
logged_in = ["Kushagra", "Julianna"]

# -------------------------
# Message handler
# -------------------------
def process_message(client: SocketModeClient, req: SocketModeRequest):
    if req.type == "events_api":
        event = req.payload.get("event", {})
        # Acknowledge event so Slack knows we received it
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        # If it's a message (not from a bot)
        if event.get("type") == "message" and "bot_id" not in event:
            text = event.get("text", "").lower()
            channel = event.get("channel")

            # Respond if user asked who's in the shop
            if "who is in the shop" in text:
                response = f"Current members in the shop:{', '.join(logged_in)}"
                try:
                    web_client.chat_postMessage(channel=channel, text=response)
                    print(f"[BOT] Sent message: {response}")
                except SlackApiError as e:
                    print(f"Error posting message: {e.response['error']}")

# -------------------------
# Attach listener and start
# -------------------------
socket_client.socket_mode_request_listeners.append(process_message)

print("Starting Slack bot… waiting for messages.")
socket_client.connect()

# Keep alive
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Exiting Slack bot.")
