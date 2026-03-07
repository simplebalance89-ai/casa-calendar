"""Casa Calendar — Family calendar with MCP + PWA.

No Google. No Microsoft. No Proton. Our data, our server.
"""

import json
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

from dateutil import parser as dtparser
from dateutil.rrule import DAILY, MONTHLY, WEEKLY, rrule
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --- Config ---
API_KEY = os.environ.get("API_KEY", "dev-key-change-me")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1024")
DB_PATH = os.environ.get("DB_PATH", "/tmp/casa_calendar.db")


# --- Database ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS calendars (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT DEFAULT '#D4A017',
            owner TEXT DEFAULT 'peter',
            shared_with TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            start TEXT NOT NULL,
            end TEXT NOT NULL,
            location TEXT DEFAULT '',
            description TEXT DEFAULT '',
            calendar TEXT DEFAULT 'family',
            recurring TEXT DEFAULT 'none',
            reminder_minutes INTEGER DEFAULT 15,
            created_by TEXT DEFAULT 'system',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
        CREATE INDEX IF NOT EXISTS idx_events_calendar ON events(calendar);
    """)
    # Seed default calendars
    for cal_id, name, color, owner in [
        ("family", "Family", "#D4A017", "peter"),
        ("peter_work", "Peter Work", "#4A90D9", "peter"),
        ("peter_personal", "Peter Personal", "#22c55e", "peter"),
        ("gladys", "Gladys", "#E91E90", "gladys"),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO calendars (id, name, color, owner) VALUES (?, ?, ?, ?)",
            (cal_id, name, color, owner),
        )
    conn.commit()
    conn.close()


# --- App ---
@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="Casa Calendar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# --- Auth ---
def verify_api_key(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    token = authorization.replace("Bearer ", "")
    if token != API_KEY:
        raise HTTPException(403, "Invalid API key")
    return token


def verify_pin(x_admin_pin: str = Header(None)):
    if x_admin_pin and x_admin_pin == ADMIN_PIN:
        return True
    return False


# --- Models ---
class EventCreate(BaseModel):
    title: str
    start: str
    end: str
    location: Optional[str] = ""
    description: Optional[str] = ""
    calendar: Optional[str] = "family"
    recurring: Optional[str] = "none"
    reminder_minutes: Optional[int] = 15
    created_by: Optional[str] = "system"


class EventUpdate(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    calendar: Optional[str] = None
    recurring: Optional[str] = None
    reminder_minutes: Optional[int] = None


# --- API Endpoints ---
@app.get("/health")
def health():
    return {"status": "ok", "service": "casa-calendar"}


@app.get("/api/calendars")
def list_calendars():
    conn = get_db()
    rows = conn.execute("SELECT * FROM calendars").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/events")
def create_event(ev: EventCreate):
    conn = get_db()
    now = datetime.utcnow().isoformat() + "Z"
    event_id = str(uuid.uuid4())[:8]
    conn.execute(
        """INSERT INTO events (id, title, start, end, location, description,
           calendar, recurring, reminder_minutes, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, ev.title, ev.start, ev.end, ev.location or "",
         ev.description or "", ev.calendar, ev.recurring, ev.reminder_minutes,
         ev.created_by or "system", now, now),
    )
    conn.commit()
    conn.close()
    return {"id": event_id, "title": ev.title, "start": ev.start, "end": ev.end}


@app.get("/api/events")
def list_events(
    calendar: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    conn = get_db()
    query = "SELECT * FROM events WHERE 1=1"
    params = []

    if calendar:
        query += " AND calendar = ?"
        params.append(calendar)
    if start_date:
        query += " AND end >= ?"
        params.append(start_date)
    if end_date:
        query += " AND start <= ?"
        params.append(end_date)

    query += " ORDER BY start ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    events = [dict(r) for r in rows]

    # Expand recurring events within the date range
    if start_date and end_date:
        expanded = []
        for ev in events:
            if ev["recurring"] == "none":
                expanded.append(ev)
            else:
                expanded.extend(_expand_recurring(ev, start_date, end_date))
        return expanded

    return events


@app.get("/api/events/{event_id}")
def get_event(event_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Event not found")
    return dict(row)


@app.put("/api/events/{event_id}")
def update_event(event_id: str, ev: EventUpdate):
    conn = get_db()
    existing = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "Event not found")

    updates = {}
    for field in ["title", "start", "end", "location", "description", "calendar", "recurring", "reminder_minutes"]:
        val = getattr(ev, field)
        if val is not None:
            updates[field] = val

    if updates:
        updates["updated_at"] = datetime.utcnow().isoformat() + "Z"
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE events SET {set_clause} WHERE id = ?",
            list(updates.values()) + [event_id],
        )
        conn.commit()

    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    return dict(row)


@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    conn = get_db()
    existing = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "Event not found")
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    return {"deleted": event_id}


@app.post("/api/import/ics")
def import_ics(request_body: dict):
    """Import events from an ICS URL."""
    import requests
    from icalendar import Calendar

    url = request_body.get("url")
    target_calendar = request_body.get("calendar", "peter_personal")
    if not url:
        raise HTTPException(400, "Missing 'url' field")

    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        raise HTTPException(502, f"Failed to fetch ICS: HTTP {resp.status_code}")

    cal = Calendar.from_ical(resp.text)
    conn = get_db()
    imported = 0
    now = datetime.utcnow().isoformat() + "Z"

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        title = str(component.get("SUMMARY", "Untitled"))
        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")
        location = str(component.get("LOCATION", ""))
        description = str(component.get("DESCRIPTION", ""))

        if not dtstart:
            continue

        start_dt = dtstart.dt
        end_dt = dtend.dt if dtend else start_dt

        # Convert to ISO string
        if hasattr(start_dt, "isoformat"):
            start_str = start_dt.isoformat()
            end_str = end_dt.isoformat()
        else:
            start_str = str(start_dt)
            end_str = str(end_dt)

        event_id = str(uuid.uuid4())[:8]
        conn.execute(
            """INSERT INTO events (id, title, start, end, location, description,
               calendar, recurring, reminder_minutes, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, title, start_str, end_str, location,
             description[:500] if description else "", target_calendar, "none", 15,
             "ics_import", now, now),
        )
        imported += 1

    conn.commit()
    conn.close()
    return {"imported": imported, "calendar": target_calendar}


# --- Recurring event expansion ---
def _expand_recurring(ev, start_date, end_date):
    freq_map = {"daily": DAILY, "weekly": WEEKLY, "monthly": MONTHLY}
    freq = freq_map.get(ev["recurring"])
    if not freq:
        return [ev]

    ev_start = dtparser.parse(ev["start"])
    ev_end = dtparser.parse(ev["end"])
    duration = ev_end - ev_start

    range_start = dtparser.parse(start_date)
    range_end = dtparser.parse(end_date)

    occurrences = list(rrule(freq, dtstart=ev_start, until=range_end))
    expanded = []
    for occ in occurrences:
        if occ + duration >= range_start:
            instance = dict(ev)
            instance["start"] = occ.isoformat()
            instance["end"] = (occ + duration).isoformat()
            instance["_recurring_instance"] = True
            expanded.append(instance)

    return expanded


# --- MCP SSE Endpoint ---
@app.get("/mcp")
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """MCP tool discovery and execution via HTTP.

    This implements a simple JSON-RPC style MCP over HTTP.
    Claude Code sends tool calls here.
    """
    if request.method == "GET":
        # Tool discovery — return available tools
        return {
            "tools": [
                {
                    "name": "casa_calendar__create_event",
                    "description": "Create a new calendar event.",
                    "parameters": {
                        "type": "object",
                        "required": ["title", "start", "end"],
                        "properties": {
                            "title": {"type": "string", "description": "Event title"},
                            "start": {"type": "string", "description": "Start datetime ISO format (YYYY-MM-DDTHH:MM:SS)"},
                            "end": {"type": "string", "description": "End datetime ISO format"},
                            "location": {"type": "string", "description": "Event location"},
                            "description": {"type": "string", "description": "Event description"},
                            "calendar": {"type": "string", "enum": ["family", "peter_work", "peter_personal", "gladys"], "default": "family"},
                            "recurring": {"type": "string", "enum": ["none", "daily", "weekly", "monthly"], "default": "none"},
                            "reminder_minutes": {"type": "integer", "default": 15},
                        },
                    },
                },
                {
                    "name": "casa_calendar__list_events",
                    "description": "List calendar events for a date range.",
                    "parameters": {
                        "type": "object",
                        "required": ["start_date", "end_date"],
                        "properties": {
                            "calendar": {"type": "string", "description": "Filter by calendar (optional)"},
                            "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                            "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
                        },
                    },
                },
                {
                    "name": "casa_calendar__update_event",
                    "description": "Update an existing event.",
                    "parameters": {
                        "type": "object",
                        "required": ["event_id"],
                        "properties": {
                            "event_id": {"type": "string"},
                            "title": {"type": "string"},
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "location": {"type": "string"},
                            "description": {"type": "string"},
                            "calendar": {"type": "string"},
                            "recurring": {"type": "string"},
                            "reminder_minutes": {"type": "integer"},
                        },
                    },
                },
                {
                    "name": "casa_calendar__delete_event",
                    "description": "Delete a calendar event.",
                    "parameters": {
                        "type": "object",
                        "required": ["event_id"],
                        "properties": {
                            "event_id": {"type": "string", "description": "Event ID to delete"},
                        },
                    },
                },
                {
                    "name": "casa_calendar__list_calendars",
                    "description": "List all calendars.",
                    "parameters": {"type": "object", "properties": {}},
                },
            ]
        }

    # POST — execute a tool call
    body = await request.json()
    auth = request.headers.get("authorization", "")
    token = auth.replace("Bearer ", "")
    if token != API_KEY:
        return JSONResponse({"error": "Unauthorized"}, status_code=403)

    tool_name = body.get("tool") or body.get("name")
    args = body.get("arguments") or body.get("params") or {}

    if tool_name == "casa_calendar__create_event":
        ev = EventCreate(**args)
        return create_event(ev)
    elif tool_name == "casa_calendar__list_events":
        return list_events(
            calendar=args.get("calendar"),
            start_date=args.get("start_date"),
            end_date=args.get("end_date"),
        )
    elif tool_name == "casa_calendar__update_event":
        eid = args.pop("event_id", None)
        if not eid:
            return JSONResponse({"error": "event_id required"}, status_code=400)
        return update_event(eid, EventUpdate(**args))
    elif tool_name == "casa_calendar__delete_event":
        return delete_event(args["event_id"])
    elif tool_name == "casa_calendar__list_calendars":
        return list_calendars()
    else:
        return JSONResponse({"error": f"Unknown tool: {tool_name}"}, status_code=400)


# --- PWA Frontend ---
@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html") as f:
        return f.read()


@app.get("/manifest.json")
def manifest():
    return {
        "name": "Casa Calendar",
        "short_name": "Calendar",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0D0D1A",
        "theme_color": "#D4A017",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
