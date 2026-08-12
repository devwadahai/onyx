from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from onyx.configs.app_configs import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_CONFIGURED,
    TWILIO_FROM_NUMBER,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()


def send_sms(phone_number: str, body: str) -> None:
    if not TWILIO_CONFIGURED:
        raise ValueError("SMS (Twilio) is not configured.")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=phone_number)
    except TwilioRestException:
        # Let callers decide how to handle a failed send (e.g. block login
        # vs. log-and-continue for a best-effort notification) rather than
        # swallowing it here.
        logger.exception("Failed to send SMS via Twilio to %s", phone_number)
        raise
