"""
Keeping calendar entries (models.CalendarEntry) in their Google calendar.

Nothing here raises: a failed call is logged, stored on the entry
(sync_error) and reported by the return value, so the entry itself is
always saved and staff can retry. The calendar check finds anything that
still drifted.
"""
import datetime
import logging

from django.utils import timezone

from .google_calendar import client_for

logger = logging.getLogger(__name__)

# Google Calendar's own event colours (colorId), closest to ours per kind.
KIND_COLOR_IDS = {
    'activity': '6',   # Tangerine
    'course': '3',     # Grape
    'meeting': '7',    # Peacock
    'closed': '8',     # Graphite
    'other': '4',      # Flamingo
}
# Marks the Google event as ours and names the entry; the calendar check reads it.
PRIVATE_KEY = 'makerslinkEntry'


def google_body(entry):
    """The Google Calendar event body for an entry."""
    tz = entry.calendar.timezone
    if entry.all_day:
        days = entry.local_days()
        start = {'date': days[0].isoformat()}
        end = {'date': (days[-1] + datetime.timedelta(days=1)).isoformat()}
    else:
        def moment(value):
            return {'dateTime': timezone.localtime(value, timezone=_zone(tz)).strftime('%Y-%m-%dT%H:%M:%S'),
                    'timeZone': tz}
        start, end = moment(entry.start), moment(entry.end)
    return {
        'summary': entry.title,
        'description': entry.description,
        'location': 'Makerspace Linköping',
        'start': start,
        'end': end,
        'colorId': KIND_COLOR_IDS.get(entry.kind, '4'),
        'extendedProperties': {'private': {PRIVATE_KEY: str(entry.pk)}},
        'reminders': {'useDefault': False, 'overrides': []},
    }


def _zone(name):
    import zoneinfo
    return zoneinfo.ZoneInfo(name)


def _not_found(error):
    status = getattr(getattr(error, 'resp', None), 'status', None)
    return status in (404, 410) or isinstance(error, LookupError)


def _record(entry, error=None):
    if error is None:
        entry.synced_at, entry.sync_error = timezone.now(), ''
    else:
        entry.sync_error = '{}: {}'.format(type(error).__name__, error)[:2000]
        logger.warning("Could not sync calendar entry %s: %s", entry.pk, entry.sync_error)
    entry.save(update_fields=['google_event_id', 'synced_at', 'sync_error'])
    return error is None


def push(entry, previous_calendar=None):
    """
    Make Google match the entry: create, update, or (with no calendar) remove
    its event. `previous_calendar` is where the event lived before an edit
    that moved it. Returns True when Google is up to date.
    """
    try:
        moved = previous_calendar is not None and previous_calendar != entry.calendar
        if entry.google_event_id and (moved or entry.calendar is None):
            client_for(previous_calendar or entry.calendar).delete(entry.google_event_id)
            entry.google_event_id = None
        if entry.calendar is None:
            # Only shown here: nothing in Google, nothing to report.
            entry.synced_at, entry.sync_error = None, ''
            entry.save(update_fields=['google_event_id', 'synced_at', 'sync_error'])
            return True
        client = client_for(entry.calendar)
        body = google_body(entry)
        if entry.google_event_id:
            try:
                client.update(entry.google_event_id, body)
            except Exception as error:
                if not _not_found(error):
                    raise
                # Someone deleted it in Google: put it back.
                entry.google_event_id = client.insert(body)
        else:
            entry.google_event_id = client.insert(body)
        return _record(entry)
    except Exception as error:
        return _record(entry, error)


def remove(entry):
    """Delete an entry's Google event before the entry is deleted. Returns an error text or ''."""
    if not (entry.calendar and entry.google_event_id):
        return ''
    try:
        client_for(entry.calendar).delete(entry.google_event_id)
        return ''
    except Exception as error:
        logger.warning("Could not delete Google event for entry %s: %s", entry.pk, error)
        return '{}: {}'.format(type(error).__name__, error)
