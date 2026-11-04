"""
Every call to Google Calendar goes through a client, one per
SchedulingCalendar, chosen by the SCHEDULER_CALENDAR_CLIENT setting (a
dotted path). Production uses GoogleCalendarClient. Development, demos and
tests can use LocalFileCalendarClient, which keeps events in a JSON file and
never touches the network.

A client has four methods, all taking and returning Google Calendar API v3
event bodies (https://developers.google.com/calendar/api/v3/reference/events):

    insert(body) -> event id
    update(event_id, body)
    delete(event_id)          # an event that is already gone is not an error
    list(time_min, time_max)  # events overlapping the window, recurring ones expanded
"""
import datetime
import json
import os
import uuid
import zoneinfo

from django.conf import settings
from django.utils.module_loading import import_string

DEFAULT_CLIENT = 'scheduler.google_calendar.GoogleCalendarClient'


def client_for(calendar):
    """The configured client for one SchedulingCalendar."""
    return import_string(getattr(settings, 'SCHEDULER_CALENDAR_CLIENT', DEFAULT_CLIENT))(calendar)


class GoogleCalendarClient:
    """Google Calendar API v3, authenticated with the calendar's service account file."""

    def __init__(self, calendar):
        self.calendar = calendar
        self._service = None

    def _events(self):
        if self._service is None:
            import googleapiclient.discovery
            self._service = googleapiclient.discovery.build(
                'calendar', 'v3', credentials=self.calendar.get_credentials(),
                cache_discovery=False)
        return self._service.events()

    def insert(self, body):
        return self._events().insert(
            calendarId=self.calendar.google_calendar_id, body=body).execute()['id']

    def update(self, event_id, body):
        self._events().update(calendarId=self.calendar.google_calendar_id,
                              eventId=event_id, body=body).execute()

    def delete(self, event_id):
        from googleapiclient.errors import HttpError
        try:
            self._events().delete(calendarId=self.calendar.google_calendar_id,
                                  eventId=event_id).execute()
        except HttpError as error:
            if error.resp.status not in (404, 410):  # 410: deleted earlier
                raise

    def list(self, time_min, time_max):
        events, page_token = [], None
        while True:
            page = self._events().list(
                calendarId=self.calendar.google_calendar_id,
                timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
                singleEvents=True, showDeleted=False, maxResults=2500,
                pageToken=page_token).execute()
            events.extend(page.get('items', []))
            page_token = page.get('nextPageToken')
            if not page_token:
                return events


def event_bounds(event):
    """(start, end, all_day) of a Google event body; datetimes are aware, dates are dates."""
    start, end = event['start'], event['end']
    if 'date' in start:
        return (datetime.date.fromisoformat(start['date']),
                datetime.date.fromisoformat(end['date']), True)

    def parse(part):
        moment = datetime.datetime.fromisoformat(part['dateTime'])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zoneinfo.ZoneInfo(part.get('timeZone') or settings.TIME_ZONE))
        return moment
    return parse(start), parse(end), False


class LocalFileCalendarClient:
    """
    For development, demos and tests only. Stores each calendar's events in
    the JSON file named by the SCHEDULER_LOCAL_CALENDAR_FILE setting, and
    answers like Google does (times come back with a UTC offset).
    """

    def __init__(self, calendar):
        self.key = calendar.google_calendar_id
        self.path = settings.SCHEDULER_LOCAL_CALENDAR_FILE

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as handle:
            return json.load(handle)

    def _save(self, data):
        with open(self.path, 'w') as handle:
            json.dump(data, handle, indent=1, ensure_ascii=False)

    @staticmethod
    def _as_google_returns_it(body):
        body = json.loads(json.dumps(body))
        for part in (body['start'], body['end']):
            if 'dateTime' in part:
                moment = datetime.datetime.fromisoformat(part['dateTime'])
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=zoneinfo.ZoneInfo(part.get('timeZone') or settings.TIME_ZONE))
                part['dateTime'] = moment.isoformat()
        body.setdefault('status', 'confirmed')
        return body

    def insert(self, body):
        data = self._load()
        event_id = uuid.uuid4().hex
        data.setdefault(self.key, {})[event_id] = dict(self._as_google_returns_it(body), id=event_id)
        self._save(data)
        return event_id

    def update(self, event_id, body):
        data = self._load()
        events = data.setdefault(self.key, {})
        if event_id not in events:
            raise LookupError('No event {} in calendar {}'.format(event_id, self.key))
        events[event_id] = dict(self._as_google_returns_it(body), id=event_id)
        self._save(data)

    def delete(self, event_id):
        data = self._load()
        data.setdefault(self.key, {}).pop(event_id, None)
        self._save(data)

    def list(self, time_min, time_max):
        found = []
        for event in self._load().get(self.key, {}).values():
            start, end, all_day = event_bounds(event)
            if all_day:
                local = zoneinfo.ZoneInfo(settings.TIME_ZONE)
                start = datetime.datetime.combine(start, datetime.time(), local)
                end = datetime.datetime.combine(end, datetime.time(), local)
            if start < time_max and end > time_min:
                found.append(event)
        return sorted(found, key=lambda e: e['start'].get('dateTime') or e['start'].get('date'))
