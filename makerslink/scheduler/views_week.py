"""
Prototype: book host slots one week at a time.

Weeks are ISO 8601 weeks, so they always start on Monday and the week
numbers match the ones printed in Swedish calendars.
"""
import calendar
import datetime

import pytz
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from .models import Event, SchedulingRuleExclusion

DAY_NAMES = ["måndag", "tisdag", "onsdag", "torsdag",
             "fredag", "lördag", "söndag"]
DAY_SHORT = ["må", "ti", "on", "to", "fr", "lö", "sö"]
MONTH_NAMES = ["januari", "februari", "mars", "april", "maj", "juni", "juli",
               "augusti", "september", "oktober", "november", "december"]

ONE_DAY = datetime.timedelta(days=1)


def _local_midnight(day):
    tz = pytz.timezone(settings.TIME_ZONE)
    return tz.localize(datetime.datetime.combine(day, datetime.time()))


def _week_url(day):
    iso = day.isocalendar()
    return reverse('week-view', args=[iso.year, iso.week])


def _slots_by_day(first_day, last_day):
    """
    Every occurrence, generated or already in the database, whose local start
    date is within [first_day, last_day], grouped by that date.
    """
    # One extra day on each side: generation works on UTC dates, which differ
    # from local dates around midnight. The filter below trims it back.
    start = _local_midnight(first_day - ONE_DAY).astimezone(pytz.utc)
    end = _local_midnight(last_day + 2 * ONE_DAY).astimezone(pytz.utc)
    by_day = {}
    for event in Event.objects.select_related('template', 'rule'):
        for instance in event.get_events(start, end):
            day = timezone.localtime(instance.start).date()
            if first_day <= day <= last_day:
                by_day.setdefault(day, []).append(instance)
    for slots in by_day.values():
        slots.sort(key=lambda instance: instance.start)
    return by_day


def _describe(instance, user, now):
    """Decorate an instance with what the week view shows."""
    instance.is_past = instance.start <= now
    if instance.status == 2:
        instance.state, instance.label = 'cancelled', 'Inställt'
    elif instance.status == 1 and instance.host_id == user.id:
        instance.state, instance.label = 'mine', 'Du är värd'
    elif instance.status == 1:
        instance.state = 'taken'
        instance.label = 'Värd: ' + instance.host.slackId if instance.host else 'Bokat'
    elif instance.status == -1:
        instance.state, instance.label = 'rebook', 'Ombokning krävs'
    else:
        instance.state, instance.label = 'free', 'Ledigt'
    return instance


def _month_picker(year, month, selected_monday, by_day, excluded, user, today):
    """A month grid of whole weeks, Monday first, each row one clickable week."""
    rows = []
    for week in calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(year, month):
        days = []
        for day in week:
            slots = [s for s in by_day.get(day, []) if not s.is_past]
            days.append({
                'date': day,
                'other_month': day.month != month,
                'today': day == today,
                'excluded': excluded.get(day),
                'mine': any(s.state == 'mine' for s in slots),
                'free': sum(1 for s in slots if s.state in ('free', 'rebook')),
            })
        rows.append({
            'number': week[0].isocalendar().week,
            'url': _week_url(week[0]),
            'selected': week[0] == selected_monday,
            'current': week[0] <= today <= week[6],
            'days': days,
        })
    first = datetime.date(year, month, 1)
    prev_month = (first - ONE_DAY).replace(day=1)
    next_month = (first + 31 * ONE_DAY).replace(day=1)
    here = _week_url(selected_monday)
    return {
        'title': '{} {}'.format(MONTH_NAMES[month - 1], year),
        'prev_url': '{}?month={:%Y-%m}'.format(here, prev_month),
        'prev_name': MONTH_NAMES[prev_month.month - 1],
        'next_url': '{}?month={:%Y-%m}'.format(here, next_month),
        'next_name': MONTH_NAMES[next_month.month - 1],
        'day_names': DAY_SHORT,
        'weeks': rows,
    }


@login_required
def week_view(request, year=None, week=None):
    today = timezone.localdate()
    if year is None:
        monday = today - today.weekday() * ONE_DAY
    else:
        try:
            monday = datetime.date.fromisocalendar(year, week, 1)
        except ValueError:
            raise Http404("No such week")
    sunday = monday + 6 * ONE_DAY

    # The picker shows the month the week mostly falls in (the ISO rule:
    # the month holding its Thursday) unless the user browsed to another one.
    shown = monday + 3 * ONE_DAY
    if 'month' in request.GET:
        try:
            shown = datetime.datetime.strptime(request.GET['month'], '%Y-%m').date()
        except ValueError:
            pass
    grid = calendar.Calendar(firstweekday=calendar.MONDAY).monthdatescalendar(
        shown.year, shown.month)

    first_day = min(monday, grid[0][0])
    last_day = max(sunday, grid[-1][-1])
    now = timezone.now()
    by_day = _slots_by_day(first_day, last_day)
    for slots in by_day.values():
        for instance in slots:
            _describe(instance, request.user, now)
    excluded = dict(SchedulingRuleExclusion.objects.filter(
        excluded_date__range=(first_day, last_day)).values_list('excluded_date', 'description'))

    days = []
    for offset in range(7):
        day = monday + offset * ONE_DAY
        days.append({
            'date': day,
            'name': DAY_NAMES[offset],
            'today': day == today,
            'past': day < today,
            'excluded': excluded.get(day),
            'slots': by_day.get(day, []),
        })

    if monday.month == sunday.month:
        span = '{}–{} {} {}'.format(monday.day, sunday.day, MONTH_NAMES[monday.month - 1], monday.year)
    else:
        span = '{} {} – {} {} {}'.format(monday.day, MONTH_NAMES[monday.month - 1][:3],
                                        sunday.day, MONTH_NAMES[sunday.month - 1][:3], sunday.year)

    return render(request, 'week_view.html', {
        'week': {
            'number': monday.isocalendar().week,
            'span': span,
            'is_current': monday <= today <= sunday,
            'prev_url': _week_url(monday - 7 * ONE_DAY),
            'prev_number': (monday - 7 * ONE_DAY).isocalendar().week,
            'next_url': _week_url(monday + 7 * ONE_DAY),
            'next_number': (monday + 7 * ONE_DAY).isocalendar().week,
            'free_count': sum(1 for d in days for s in d['slots'] if s.state in ('free', 'rebook') and not s.is_past),
            'mine_count': sum(1 for d in days for s in d['slots'] if s.state == 'mine'),
        },
        'days': days,
        'picker': _month_picker(shown.year, shown.month, monday, by_day, excluded,
                                request.user, today),
    })

