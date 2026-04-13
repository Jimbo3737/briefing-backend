"""
Delivery Service
Sends briefings via email (SendGrid) and WhatsApp (Twilio).
"""

import sendgrid
from sendgrid.helpers.mail import Mail, Content, To
from twilio.rest import Client as TwilioClient


class DeliveryService:
    def __init__(
        self,
        sendgrid_key: str,
        twilio_sid: str,
        twilio_token: str,
        twilio_whatsapp_from: str,
        from_email: str,
    ):
        self.sg = sendgrid.SendGridAPIClient(api_key=sendgrid_key) if sendgrid_key else None
        self.twilio = TwilioClient(twilio_sid, twilio_token) if twilio_sid and twilio_token else None
        self.twilio_from = twilio_whatsapp_from
        self.from_email = from_email

    def send_email(
        self,
        to_email: str,
        subject: str,
        body_text: str,
        body_html: str = None,
    ) -> dict:
        if not self.sg:
            raise RuntimeError("SendGrid not configured. Set SENDGRID_API_KEY in .env")

        message = Mail(
            from_email=self.from_email,
            to_emails=to_email,
            subject=subject,
        )
        message.add_content(Content("text/plain", body_text))
        if body_html:
            message.add_content(Content("text/html", body_html))

        response = self.sg.send(message)

        if response.status_code not in (200, 202):
            raise RuntimeError(f"SendGrid error: {response.status_code} — {response.body}")

        return {"status": "sent", "to": to_email, "status_code": response.status_code}

    def send_whatsapp(self, to_number: str, message: str) -> dict:
        if not self.twilio:
            raise RuntimeError("Twilio not configured. Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env")

        # Ensure number has whatsapp: prefix
        to = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"

        msg = self.twilio.messages.create(
            body=message,
            from_=self.twilio_from,
            to=to,
        )

        return {"status": "sent", "to": to_number, "sid": msg.sid}
