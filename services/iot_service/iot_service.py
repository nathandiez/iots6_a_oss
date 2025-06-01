#!/usr/bin/env python3
# iot_service.py
import paho.mqtt.client as mqtt
import time
import os
import sys
import logging
import pytz
import json
import psycopg2  # For database functionality
import ssl
from datetime import datetime
import threading

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)


# Configure logging with timezone conversion
class TimezoneFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        # Convert UTC to Eastern time
        utc_dt = datetime.utcfromtimestamp(record.created)
        eastern_tz = pytz.timezone("America/New_York")
        eastern_dt = utc_dt.replace(tzinfo=pytz.UTC).astimezone(eastern_tz)

        if datefmt:
            return eastern_dt.strftime(datefmt)
        return eastern_dt.strftime("%Y-%m-%d %H:%M:%S")


# Set up logging with custom formatter
handler = logging.StreamHandler(sys.stdout)
formatter = TimezoneFormatter("%(asctime)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# Clear any existing handlers
logger.handlers = []
logger.addHandler(handler)


# Database connection
def get_db_connection():
    while True:
        try:
            conn = psycopg2.connect(
                dbname=os.getenv("POSTGRES_DB", "your_db_name"),
                user=os.getenv("POSTGRES_USER", "your_db_user"),
                password=os.getenv("POSTGRES_PASSWORD", "your_db_password"),
                host=os.getenv("POSTGRES_HOST", "timescaledb"),
                port=os.getenv("POSTGRES_PORT", "5432"),
            )
            logger.info("Successfully connected to database")
            return conn
        except psycopg2.OperationalError as e:
            logger.error(f"Could not connect to database: {e}")
            logger.info("Retrying in 5 seconds...")
            time.sleep(5)


def store_sensor_data(data):
    """Stores the complete sensor data dictionary into the database."""
    try:
        # Create a copy of the data to avoid modifying the original
        db_data = data.copy()
        
        # Ensure event_type exists, provide default if not (optional safety)
        db_data.setdefault("event_type", "unknown")

        # Convert numeric fields to appropriate types
        numeric_fields = ["temperature", "humidity", "pressure", "wifi_rssi", "uptime_seconds", "fan_pwm", "fans_active_level"]
        for key in numeric_fields:
            if db_data.get(key) is not None:
                try:
                    # Handle different numeric types
                    if key in ["uptime_seconds", "fan_pwm", "fans_active_level", "wifi_rssi"]:
                        db_data[key] = int(db_data[key])
                    else:
                        db_data[key] = float(db_data[key])
                except (ValueError, TypeError):
                    logger.warning(
                        f"Invalid numeric value for {key}: {db_data[key]}, setting to None"
                    )
                    db_data[key] = None

        # Ensure text fields are treated as strings
        text_fields = ["motion", "switch", "version", "uptime", "temp_sensor_type"]
        for key in text_fields:
            if key in db_data and db_data[key] is not None:
                db_data[key] = str(db_data[key])

        # Rename timestamp to time for database schema compatibility
        if "timestamp" in db_data:
            db_data["time"] = db_data.pop("timestamp")

        # Ensure all expected database fields exist with None as default
        expected_fields = [
            "time", "device_id", "event_type", "temperature", "humidity", 
            "pressure", "temp_sensor_type", "motion", "switch", "version", "uptime",
            "wifi_rssi", "uptime_seconds", "fan_pwm", "fans_active_level"
        ]
        
        for field in expected_fields:
            if field not in db_data:
                db_data[field] = None

        # Debug output to check field values
        logger.info(f"Storing {db_data['event_type']} data for device {db_data['device_id']} - "
                   f"temp={db_data.get('temperature')} motion={db_data.get('motion')} "
                   f"rssi={db_data.get('wifi_rssi')} uptime={db_data.get('uptime_seconds')}s")

        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sensor_data (
                    time, device_id, event_type, temperature, humidity, 
                    pressure, temp_sensor_type, motion, switch, version, uptime,
                    wifi_rssi, uptime_seconds, fan_pwm, fans_active_level
                )
                VALUES (
                    %(time)s, %(device_id)s, %(event_type)s, %(temperature)s, %(humidity)s,
                    %(pressure)s, %(temp_sensor_type)s, %(motion)s, %(switch)s, %(version)s, %(uptime)s,
                    %(wifi_rssi)s, %(uptime_seconds)s, %(fan_pwm)s, %(fans_active_level)s
                )
            """,
                db_data,
            )
            db_conn.commit()

        logger.info(f"Successfully stored {db_data['event_type']} data for device {db_data['device_id']}")

    except psycopg2.Error as e:
        logger.error(f"Database error storing sensor data: {e}")
        logger.error(f"Data that caused error: {db_data}")
        db_conn.rollback()

    except Exception as e:
        logger.error(f"Unexpected error storing sensor data: {e}")
        logger.error(f"Data that caused error: {db_data}")
        db_conn.rollback()


def on_connect(client, userdata, flags, rc):
    logger.info(f"Connected with result code {rc}")
    logger.info("Subscribing to your_topic_prefix/#")  # Updated to match your topic
    client.subscribe("your_topic_prefix/#")


def on_message(client, userdata, msg):
    try:
        start_time = time.time()
        message_text = msg.payload.decode()
        logger.info(f"Received message on {msg.topic}: {message_text}")

        # Check if the message has the prefix format
        if "MQTT event:" in message_text:
            # Extract just the JSON part
            json_start = message_text.find("{")
            if json_start != -1:
                json_text = message_text[json_start:]
                data = json.loads(json_text)
            else:
                raise ValueError("No JSON object found in message")
        else:
            # Regular JSON message
            data = json.loads(message_text)

        # Rest of function remains the same...
        device_id = data.get("device_id", "unknown")

        # Parse the ISO format timestamp (2025-04-21T17:21:47Z)
        if "timestamp" in data and data["timestamp"]:
            # Handle the timestamp format with 'Z' suffix for UTC
            if data["timestamp"].endswith("Z"):
                # Remove the Z and parse as UTC
                ts_str = data["timestamp"][:-1]
                dt = datetime.fromisoformat(ts_str)
                data["timestamp"] = dt.replace(tzinfo=pytz.UTC)
            else:
                # Try to parse in standard ISO format
                dt = datetime.fromisoformat(data["timestamp"])
                # If no timezone info, assume UTC
                if dt.tzinfo is None:
                    data["timestamp"] = dt.replace(tzinfo=pytz.UTC)
                else:
                    data["timestamp"] = dt

        # Store data in database
        store_sensor_data(data)

        # Log processing time
        processing_time = time.time() - start_time
        logger.debug(f"Message processing time: {processing_time:.3f} seconds")

    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON: {e}")
        logger.error(f"Raw message: {message_text}")
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        logger.error(f"Raw message: {message_text}")


def on_subscribe(client, userdata, mid, granted_qos):
    logger.info(f"Subscribed successfully! QoS: {granted_qos}")


def on_disconnect(client, userdata, rc):
    logger.info(f"Disconnected with result code: {rc}")


# Database connection initialization
db_conn = get_db_connection()

# Create client instance with explicit API version
client = mqtt.Client(
    client_id="",
    clean_session=True,
    userdata=None,
    protocol=mqtt.MQTTv311,
    transport="tcp",
    reconnect_on_failure=True,
)

# Assign callback functions
client.on_connect = on_connect
client.on_message = on_message
client.on_subscribe = on_subscribe
client.on_disconnect = on_disconnect

# Get broker address from environment variable, default to localhost
broker_address = os.getenv("MQTT_BROKER", "localhost")
broker_port = int(os.getenv("MQTT_PORT", "1883"))

logger.info(f"Connecting to broker at {broker_address}:{broker_port}...")

try:
    client.connect(broker_address, broker_port, 60)
    client.loop_forever()
except KeyboardInterrupt:
    logger.info("Shutting down...")
    client.disconnect()
    db_conn.close()
except Exception as e:
    logger.error(f"Error occurred: {e}")
    db_conn.close()