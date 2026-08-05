import os
from fastapi import FastAPI, Request
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()


@app.post("/webhook")
async def vapi_webhook(request: Request):
    body = await request.json()
    message = body.get("message", {})

    if message.get("type") == "tool-calls":
        results = []
        for call in message.get("toolCalls", []):
            name = call["function"]["name"]
            args = call["function"].get("arguments", {})

            if name == "checkAvailability":
                result = check_availability(args)
            elif name == "bookAppointment":
                result = book_appointment(args)
            else:
                result = "Unknown tool."

            results.append({"toolCallId": call["id"], "result": result})

        return {"results": results}

    return {"received": True}


def check_availability(args: dict) -> str:
    return "We have openings tomorrow at 9am, 11am, and 2pm."


def book_appointment(args: dict) -> str:
    date = args.get("date", "the requested time")
    service = args.get("serviceType", "service")
    return f"Booked a {service} appointment for {date}. Confirmation sent."