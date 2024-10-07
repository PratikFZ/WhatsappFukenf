from flask import Flask, request, abort
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from twilio.request_validator import RequestValidator
import pymongo
from datetime import datetime, timedelta
import logging
from logging.handlers import RotatingFileHandler
import os
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import json

load_dotenv()

app = Flask(__name__)

# Enhanced logging
handler = RotatingFileHandler('appointment_bot.log', maxBytes=10000, backupCount=3)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
app.logger.addHandler(handler)
app.logger.setLevel(logging.DEBUG)

# Twilio credentials
account_sid = 'ACf880dc484545a00019071a15b2fc1873'
auth_token = '97578bef63e187dac40529f0276e0f35'
twilio_client = Client(account_sid, auth_token)

# MongoDB connection
mongo_uri = 'mongodb://localhost:27017/'
client = pymongo.MongoClient(mongo_uri)
db = client['appointment_db']
appointments = db['appointments']

# Twilio request validation
validator = RequestValidator(auth_token)

def validate_twilio_request(request):
    # Get the full URL of the request
    url = request.url

    # Get the POST data
    post_data = request.form

    # Get the X-Twilio-Signature header
    signature = request.headers.get('X-Twilio-Signature', '')

    # Check if we're behind a proxy
    if request.headers.get('X-Forwarded-Proto'):
        url = request.headers.get('X-Forwarded-Proto') + '://' + request.host + request.path

    app.logger.debug(f"Validating request: URL={url}, Signature={signature}")

    # Validate the request
    if not validator.validate(url, post_data, signature):
        app.logger.warning('Invalid Twilio request')
        app.logger.debug(f"Request details: URL={url}, POST data={post_data}, Signature={signature}")
        return False
    else:
        app.logger.debug('Twilio request validated successfully')
        return True

def send_interactive_message(to, message, buttons):
    try:
        if len(buttons) > 3:
            app.logger.warning(f"Too many buttons provided ({len(buttons)}). Limiting to 3.")
            buttons = buttons[:3]  # WhatsApp allows only up to 3 quick reply buttons

        interactive_message = {
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": message
                },
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": button["reply"]["id"],
                                "title": button["reply"]["title"]
                            }
                        } for button in buttons
                    ]
                }
            }
        }

        # Send the interactive message using Twilio's API
        message = twilio_client.messages.create(
            from_='whatsapp:+14155238886',  # Your Twilio WhatsApp number
            to=to,  # Recipient WhatsApp number
            body=json.dumps(interactive_message)  # Send the interactive button structure
        )
        
        app.logger.info(f"Sent interactive message to {to} with {len(buttons)} buttons")
        return message.sid
    except Exception as e:
        app.logger.error(f"Failed to send interactive message: {str(e)}")
        return None

    
@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    if not validate_twilio_request(request):
        abort(403)

    incoming_msg = request.values.get('Body', '').lower()
    sender_number = request.values.get('From', '')
    service = ''

    app.logger.info(f"Received message from {sender_number}: {incoming_msg}")
    app.logger.debug(f"Full request details: {request.values}")

    response = MessagingResponse()
    msg = response.message()

    try:
        if 'hi' in incoming_msg or 'hello' in incoming_msg:
            greeting_msg = "Welcome to our appointment booking service! How can we help you today?"
            buttons = [
                {"reply": {"id": "book_now", "title": "Book Now"}},
                {"reply": {"id": "book_later", "title": "Book Later"}},
                # {"reply": {"id": "cancel_booking", "title": "Cancel Booking"}}
            ]
            send_interactive_message(sender_number, greeting_msg, buttons)
            return str(response)

        elif 'book_now' in incoming_msg or 'book' in incoming_msg:
            msg.body("What service would you like to book? (e.g., Haircut, Consultation, etc.)")
            app.logger.debug(f"Asked {sender_number} for service type.")
        elif 'book_later' in incoming_msg:
            msg.body("No problem! When you're ready to book, just type 'book' and we'll assist you.")
            app.logger.debug(f"{sender_number} chose to book later.")
        elif 'cancel_booking' in incoming_msg:
            existing_appointment = appointments.find_one({"phone_number": sender_number, "appointment_date": {"$gte": datetime.now()}})
            if existing_appointment:
                appointments.delete_one({"_id": existing_appointment["_id"]})
                msg.body(f"Your appointment for {existing_appointment['service']} on {existing_appointment['appointment_date']} has been cancelled.")
                app.logger.info(f"Cancelled appointment for {sender_number}")
            else:
                msg.body("You don't have any upcoming appointments to cancel.")
            app.logger.debug(f"{sender_number} attempted to cancel a booking.")
        elif 'haircut' in incoming_msg or 'consultation' in incoming_msg:
            service = incoming_msg.title()
            msg.body(f"When would you like to schedule your {service}? Please provide the date and time in this format: YYYY-MM-DD HH:MM.")
            app.logger.debug(f"{sender_number} is booking a {service}.")
        elif len(incoming_msg) == 16:  # Assuming date and time input
            try:
                appointment_date = datetime.strptime(incoming_msg, '%Y-%m-%d %H:%M')
                if appointment_date < datetime.now():
                    raise ValueError("Appointment date is in the past")
                
                app.logger.info(f"Scheduling {service} for {sender_number} on {appointment_date}.")
                
                appointments.insert_one({
                    "customer_name": "Customer",  # Replace with a way to get the customer's name
                    "phone_number": sender_number,
                    "service": service,
                    "appointment_date": appointment_date,
                    "reminder_sent": False,
                    "follow_up_sent": False
                })
                msg.body(f"Your {service} is scheduled for {appointment_date}. You will receive a reminder 24 hours before the appointment.")
            except ValueError as e:
                msg.body(f"Invalid date format or date is in the past. Please use YYYY-MM-DD HH:MM for a future date and time.")
                app.logger.error(f"Invalid date format provided by {sender_number}: {incoming_msg}. Error: {str(e)}")
        else:
            msg.body("I didn't understand that. Type 'hi' for options or 'book' to book an appointment.")
            app.logger.warning(f"Unrecognized message from {sender_number}: {incoming_msg}")
    
    except Exception as e:
        app.logger.error(f"Error processing message from {sender_number}: {str(e)}")
        msg.body("An error occurred while processing your request. Please try again later.")

    return str(response)

def send_reminder():
    app.logger.info("Running reminder job.")
    now = datetime.now()
    reminder_appointments = appointments.find({
        "appointment_date": {
            "$gte": now + timedelta(hours=23),
            "$lte": now + timedelta(hours=25)
        },
        "reminder_sent": False
    })
    
    for appointment in reminder_appointments:
        try:
            message = f"Reminder: Your {appointment['service']} appointment is scheduled for {appointment['appointment_date']}."
            twilio_client.messages.create(
                body=message,
                from_='whatsapp:+14155238886',
                to=appointment['phone_number']
            )
            app.logger.info(f"Sent reminder to {appointment['phone_number']} for {appointment['service']} on {appointment['appointment_date']}.")
            appointments.update_one({"_id": appointment["_id"]}, {"$set": {"reminder_sent": True}})
        except Exception as e:
            app.logger.error(f"Failed to send reminder to {appointment['phone_number']}: {str(e)}")

def send_follow_up():
    app.logger.info("Running follow-up job.")
    now = datetime.now()
    follow_up_appointments = appointments.find({
        "appointment_date": {
            "$gte": now - timedelta(hours=24),
            "$lte": now
        },
        "follow_up_sent": False
    })
    
    for appointment in follow_up_appointments:
        try:
            message = f"Hope you enjoyed your {appointment['service']}! Let us know if you need anything else."
            twilio_client.messages.create(
                body=message,
                from_='whatsapp:+14155238886',
                to=appointment['phone_number']
            )
            app.logger.info(f"Sent follow-up to {appointment['phone_number']} for {appointment['service']} on {appointment['appointment_date']}.")
            appointments.update_one({"_id": appointment["_id"]}, {"$set": {"follow_up_sent": True}})
        except Exception as e:
            app.logger.error(f"Failed to send follow-up to {appointment['phone_number']}: {str(e)}")

if __name__ == "__main__":
    # Set up scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(send_reminder, 'interval', minutes=1)
    scheduler.add_job(send_follow_up, 'interval', minutes=1)
    scheduler.start()

    # Run the Flask app
    app.run(host="0.0.0.0", port=int(os.getenv('PORT', 5000)), debug=True)