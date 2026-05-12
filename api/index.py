from flask import Flask, request, jsonify
from dotenv import load_dotenv
import os
import logging

# Load environment variables from .env file
load_dotenv()

# Configuration from environment
DEBUG = os.getenv("DEBUG", "False").lower() == "true"
WEBHOOK_ENDPOINT = os.getenv("WEBHOOK_ENDPOINT", "/webhook-endpoint")
APP_NAME = os.getenv("APP_NAME", "Confluence Webhook Listener")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Setup logging
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

app = Flask(APP_NAME)

@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route(WEBHOOK_ENDPOINT, methods=["GET"])
def webhook_info():
    return jsonify({
        "message": "This endpoint accepts POST requests from Confluence webhooks.",
        "endpoint": WEBHOOK_ENDPOINT,
        "method": "POST",
    })

@app.route(WEBHOOK_ENDPOINT, methods=["POST"])
def receive_webhook():
    try:
        data = request.get_json()
        logger.info("Webhook Event received: %s", data)
        print("Webhook Event received:", data)
        
        # Code to perform other functions.....
        
        return jsonify({"message": "Webhook received successfully"})
    except Exception as e:
        logger.error("Error processing webhook: %s", str(e))
        return jsonify({"message": "Error processing webhook", "error": str(e)}), 400

# Vercel expects the app to be named 'app'
# For serverless, we don't run app.run()