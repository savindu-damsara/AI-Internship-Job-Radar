
import os
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

sender = os.getenv("GMAIL_ADDRESS")
password = os.getenv("GMAIL_APP_PASSWORD")
recipient = os.getenv("ALERT_EMAIL")

if not all([sender, password, recipient]):
    raise ValueError("Check the three Gmail values in .env")

message = EmailMessage()
message["From"] = sender
message["To"] = recipient
message["Subject"] = "AI Job Radar - Test Alert"
message.set_content(
    "Congratulations! Your Gmail alert system works."
)

with smtplib.SMTP_SSL(
    "smtp.gmail.com", 465, timeout=30
) as smtp:
    smtp.login(sender, password.replace(" ", ""))
    smtp.send_message(message)

print("Test email sent successfully!")