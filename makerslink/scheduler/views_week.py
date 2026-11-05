"""
Prototype: book host slots one week at a time.

Weeks are ISO 8601 weeks, so they always start on Monday and the week
numbers match the ones printed in Swedish calendars.
"""
import datetime

import pytz
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .booking_rules import Closures, DependencyChecker
from .host_progress import progress_for
from .models import BookingChange, CalendarEntry, Event, EventInstance, SchedulingPeriod, SchedulingRuleExclusion

DAY_NAMES = ["måndag", "tisdag", "onsdag", "torsdag",
             "fredag", "lördag", "söndag"]
DAY_SHORT = ["må", "ti", "on", "to", "fr", "lö", "sö"]
MONTH_NAMES = ["januari", "februari", "mars", "april", "maj", "juni", "juli",
               "augusti", "september", "oktober", "november", "december"]

ONE_DAY = datetime.timedelta(days=1)


def _local_midnight(day):
    tz = pytz.timezone(settings.TIME_ZONE)
    return tz.localize(datetime.datetime.combine(day, datetime.time()))


SLOT_URL_TIME = '%Y%m%dT%H%MZ'


def slot_url(instance):
    """The staff page of a slot, generated or saved, by its Event and UTC start."""
    return reverse('slot-manage', args=[
        instance.event_id, instance.start.astimezone(datetime.timezone.utc).strftime(SLOT_URL_TIME)])


def _week_url(day):
    iso = day.isocalendar()
    return reverse('week-view', args=[iso.year, iso.week])


JUST_CHANGED = 'booking_just_changed'


def slot_key(instance):
    return 'slot:{}:{}'.format(instance.event_id, int(instance.start.timestamp()))


def entry_key(entry):
    return 'entry:{}'.format(entry.pk)


def remember_change(request, key):
    """The next page shows (and highlights) this slot or pass, even if a filter would hide it."""
    request.session[JUST_CHANGED] = key


def _mark_just_changed(request, items):
    """Flag the slot or pass the member just booked or cancelled, and return its key."""
    key = request.session.pop(JUST_CHANGED, None)
    if key:
        for item in items:
            if 'slot' in item and slot_key(item['slot']) == key:
                item['slot'].just_changed = True
            elif 'entry' in item and entry_key(item['entry']) == key:
                item['entry'].just_changed = True
    return key


def back_url(request, fallback):
    """The posted `next` page if it is on this site, else `fallback`."""
    target = request.POST.get('next', '')
    if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return fallback


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


def _entries_by_day(first_day, last_day, user):
    """Calendar entries touching [first_day, last_day], listed on every day they touch."""
    start = _local_midnight(first_day)
    end = _local_midnight(last_day + ONE_DAY)
    by_day = {}
    now = timezone.now()
    entries = (CalendarEntry.objects.filter(start__lt=end, end__gt=start)
               .select_related('special_type').prefetch_related('seats__user'))
    for entry in entries:
        entry.editable = entry.can_edit(user)
        if entry.is_special:
            # Seats, and what this member can do about them.
            entry.seat = entry.seat_situation(user, now)
            entry.mine = entry.seat['mine'] is not None
            entry.is_past = entry.start <= now
            entry.can_join = entry.seat['action'] in ('join', 'take_over')
        for day in entry.local_days():
            if first_day <= day <= last_day:
                by_day.setdefault(day, []).append(entry)
    return by_day


def _day_items(day, slots, entries):
    """
    Timed entries and slots in one list by start time, so a 19:00 meeting
    sits between the 15-18 and 18-21 slots. Entries running past midnight
    sort from the start of the days they continue on.
    """
    midnight = _local_midnight(day)
    items = [{'slot': slot, 'at': slot.start} for slot in slots]
    items += [{'entry': entry, 'at': max(entry.start, midnight)}
              for entry in entries if not entry.all_day]
    return sorted(items, key=lambda item: item['at'])


def _describe(instance, user, now, checker):
    """Decorate an instance with what the week view shows and allows."""
    instance.is_past = instance.start <= now
    instance.start_iso = instance.start.isoformat()
    instance.manage_url = slot_url(instance)
    instance.action = None
    instance.hint = None
    instance.warning = None
    if instance.status == 2:
        instance.state, instance.label = 'cancelled', 'Inställt'
    elif instance.status == 1 and instance.host_id == user.id:
        # A host never unbooks: they ask for someone to take it over.
        instance.state, instance.label = 'mine', 'Du är värd'
        instance.action = 'Kan inte – lämna över'
    elif instance.status == 1:
        instance.state = 'taken'
        instance.label = 'Värd: ' + instance.host.slackId if instance.host else 'Bokat'
    elif instance.status == -1 and instance.host_id == user.id:
        instance.state, instance.label = 'handed', 'Ombokning önskas'
        instance.hint = 'Du är värd tills någon tar över'
        instance.action = 'Ta tillbaka'
    elif instance.status == -1:
        instance.state, instance.label = 'rebook', 'Ombokning önskas'
        if instance.host:
            instance.hint = '{} kan inte'.format(instance.host.slackId)
            instance.action = 'Ta över'
        else:
            instance.action = 'Boka'
    else:
        instance.state, instance.label = 'free', 'Ledigt'
        instance.action = 'Boka'
    if not instance.is_past and instance.state in ('free', 'rebook', 'mine', 'taken', 'handed'):
        closing = checker.closing(instance)
        replacing = checker.replacing(instance)
        unmet = checker.unmet(instance)
        # Free, or "Ombokning önskas" with nobody on it: nobody holds it, so a
        # closure, a special pass or a missing evening shows instead of it.
        # A slot someone holds (even one up for take-over) keeps its card and
        # gets a warning, and cannot be taken over while the warning stands.
        held = instance.host_id is not None and instance.status != 0
        if replacing and not closing and not held:
            # A special pass (e.g. an introductory evening) takes its place.
            instance.state, instance.label = 'replaced', 'Specialpass'
            instance.hint = '{} istället'.format(Closures.describe(replacing))
            instance.action = None
        elif replacing and not closing:
            instance.warning = 'Krockar med {}'.format(Closures.describe(replacing))
        elif closing and not held:
            # A "Stängt" entry overlaps the slot: nobody can book it.
            instance.state, instance.label = 'closed', 'Stängt'
            instance.hint = Closures.describe(closing)
            instance.action = None
        elif closing:
            # Booked before the closure was added: keep it, but say so.
            instance.warning = 'Lokalen är stängd: {}'.format(Closures.describe(closing))
        elif unmet and not held:
            # Not bookable yet: say what would open it.
            instance.state, instance.label = 'locked', 'Låst'
            instance.hint = 'Öppnas när {} har en värd'.format(checker.describe(unmet))
            instance.action = None
        elif unmet:
            # Booked, but what it depends on has no host (staff removed it).
            instance.warning = '{} saknar värd'.format(checker.describe(unmet))
        if instance.warning and instance.state == 'rebook':
            instance.action = None  # the booking endpoint would refuse the take-over
    if instance.is_past:
        instance.action = None
    return instance


def _period_for(day):
    """The period covering `day`, else the next one to start, else the latest."""
    return (SchedulingPeriod.get_period_for_date(day)
            or SchedulingPeriod.objects.filter(start__gt=day).order_by('start').first()
            or SchedulingPeriod.objects.order_by('-end').first())


def _segment(slot):
    """One small bar per slot in the picker: what state is that slot in?"""
    if slot.is_past:
        return 'done' if slot.status in EventInstance.HOSTED_STATUSES else 'past'
    if slot.state == 'handed':
        return 'rebook'
    return 'closed' if slot.state == 'replaced' else slot.state


def _period_picker(period, selected_monday, by_day, excluded, today, entries=None,
                   week_link=_week_url, period_link=None):
    """
    Every ISO week of `period`, Monday first, grouped under the month that
    holds the week's Thursday (so a week never appears twice). Each row is
    one clickable week; each day shows one bar per slot.

    Rows link to `week_link(monday)` and the arrows to `period_link(period)`,
    by default the week view.
    """
    if period_link is None:
        def period_link(other):
            return _week_url(other.start)
    months = []
    monday = period.start - period.start.weekday() * ONE_DAY
    while monday <= period.end:
        week = [monday + offset * ONE_DAY for offset in range(7)]
        thursday = week[3]
        if not months or months[-1]['month'] != (thursday.year, thursday.month):
            months.append({'month': (thursday.year, thursday.month),
                           'name': MONTH_NAMES[thursday.month - 1], 'weeks': []})
        days = []
        for day in week:
            in_period = period.start <= day <= period.end
            days.append({
                'date': day,
                'outside': not in_period,
                'first_of_month': day.day == 1,
                'today': day == today,
                'excluded': excluded.get(day),
                'segments': [_segment(s) for s in by_day.get(day, [])] if in_period else [],
                'free': sum(1 for s in by_day.get(day, [])
                            if in_period and not s.is_past and s.state in ('free', 'rebook')),
                'entries': (entries or {}).get(day, [])[:3] if in_period else [],
            })
        months[-1]['weeks'].append({
            'number': monday.isocalendar().week,
            'url': week_link(monday),
            'selected': monday == selected_monday,
            'days': days,
        })
        monday += 7 * ONE_DAY

    previous = SchedulingPeriod.objects.filter(start__lt=period.start).order_by('-start').first()
    following = SchedulingPeriod.objects.filter(start__gt=period.start).order_by('start').first()
    return {
        'period': period,
        'covers_week': period.start <= selected_monday + 6 * ONE_DAY and selected_monday <= period.end,
        'selected_number': selected_monday.isocalendar().week,
        'prev_url': period_link(previous) if previous else None,
        'prev_name': previous.name if previous else None,
        'next_url': period_link(following) if following else None,
        'next_name': following.name if following else None,
        'day_names': DAY_SHORT,
        'months': months,
    }


def _load_days(first_day, last_day, user, now):
    """Slots (described for `user`), closed dates and entries for [first_day, last_day]."""
    by_day = _slots_by_day(first_day, last_day)
    checker = DependencyChecker(first_day, last_day)
    for slots in by_day.values():
        for instance in slots:
            _describe(instance, user, now, checker)
    excluded = dict(SchedulingRuleExclusion.objects.filter(
        excluded_date__range=(first_day, last_day)).values_list('excluded_date', 'description'))
    entries = _entries_by_day(first_day, last_day, user)
    return by_day, excluded, entries


def _period_weeks(period):
    """Monday of the first week and Sunday of the last week that touch `period`."""
    return (period.start - period.start.weekday() * ONE_DAY,
            period.end + (6 - period.end.weekday()) * ONE_DAY)


def _span(monday):
    """'9–15 november 2026', or '30 nov – 6 dec 2026' across a month."""
    sunday = monday + 6 * ONE_DAY
    if monday.month == sunday.month:
        return '{}–{} {} {}'.format(monday.day, sunday.day, MONTH_NAMES[monday.month - 1], monday.year)
    return '{} {} – {} {} {}'.format(monday.day, MONTH_NAMES[monday.month - 1][:3],
                                    sunday.day, MONTH_NAMES[sunday.month - 1][:3], sunday.year)


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

    period = _period_for(monday)
    first_day, last_day = monday, sunday
    if period:
        period_first, period_last = _period_weeks(period)
        first_day, last_day = min(monday, period_first), max(sunday, period_last)
    now = timezone.now()
    by_day, excluded, entries = _load_days(first_day, last_day, request.user, now)

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
            'banners': [entry for entry in entries.get(day, []) if entry.all_day],
            'items': _day_items(day, by_day.get(day, []), entries.get(day, [])),
        })
    _mark_just_changed(request, [item for d in days for item in d['items']])

    return render(request, 'week_view.html', {
        'week': {
            'number': monday.isocalendar().week,
            'span': _span(monday),
            'is_current': monday <= today <= sunday,
            'prev_url': _week_url(monday - 7 * ONE_DAY),
            'prev_number': (monday - 7 * ONE_DAY).isocalendar().week,
            'next_url': _week_url(monday + 7 * ONE_DAY),
            'next_number': (monday + 7 * ONE_DAY).isocalendar().week,
            'free_count': sum(1 for d in days for s in d['slots'] if s.state in ('free', 'rebook') and not s.is_past),
            'mine_count': sum(1 for d in days for s in d['slots'] if s.state in ('mine', 'handed')),
        },
        'days': days,
        'picker': _period_picker(period, monday, by_day, excluded, today, entries) if period else None,
        'entry_kinds': CalendarEntry.KINDS,
        'progress': progress_for(period, [request.user], now)[0] if period else None,
        'list_url': _list_url(period, anchor=_week_anchor(monday)) if period else reverse('booking-list'),
    })


@login_required
@require_POST
def week_toggle(request):
    """Book or unbook one slot, identified by its Event and start time."""
    event = get_object_or_404(Event, pk=request.POST.get('event'))
    start = parse_datetime(request.POST.get('start', ''))
    if start is None:
        raise Http404("Bad start time")
    back = back_url(request, _week_url(timezone.localtime(start).date()))

    # Rebuild the occurrence on the server instead of trusting posted fields,
    # so a slot can only be booked at a time its Event actually generates.
    matches = [i for i in event.get_events(start - ONE_DAY, start + ONE_DAY)
               if i.start == start]
    if not matches:
        messages.error(request, 'Passet finns inte längre.')
        return redirect(back)
    instance = matches[0]
    remember_change(request, slot_key(instance))
    day = timezone.localtime(instance.start).date()
    when = timezone.localtime(instance.start).strftime('%Y-%m-%d %H:%M')
    checker = DependencyChecker(day, day)
    closing = checker.closing(instance)
    replacing = checker.replacing(instance)
    unmet = checker.unmet(instance)

    if instance.start <= timezone.now():
        messages.error(request, 'Passet {} har redan börjat.'.format(when))
    elif instance.status == 1 and instance.host_id == request.user.id:
        # Not a cancellation: the slot stays yours, in Google and in your
        # count, marked "Ombokning önskas" until another host takes it over.
        # Only staff remove bookings. Nothing changes in Google, so no save().
        EventInstance.objects.filter(pk=instance.pk).update(status=-1)
        instance.status = -1
        BookingChange.log(instance, 'cancelled', by=request.user)
        messages.success(request, 'Du har önskat ombokning av {}. Du är värd tills någon annan tar '
                                  'över.'.format(when))
        # Until someone takes it over it no longer opens slots that depend on
        # it: say which booked ones that leaves with a warning.
        checker = DependencyChecker(day, day)
        stranded = [i for i in EventInstance.objects.filter(
                        status=1, start__date=day).select_related('event', 'host')
                    if checker.unmet(i)]
        for other in stranded:
            messages.warning(request, '{} {} ({}) bygger på passet och är låst för nya bokningar tills '
                                      'någon tar över.'.format(
                                          other.event.name, timezone.localtime(other.start).strftime('%H:%M'),
                                          'ditt' if other.host_id == request.user.id
                                          else 'värd: ' + other.host.slackId))
    elif instance.status == -1 and instance.host_id == request.user.id:
        # Taking it back is just dropping the wish: still yours, Google untouched.
        EventInstance.objects.filter(pk=instance.pk).update(status=1)
        instance.status = 1
        BookingChange.log(instance, 'booked', by=request.user)
        messages.success(request, 'Du har tagit tillbaka {}.'.format(when))
    elif instance.status in (0, -1) and closing:
        messages.error(request, 'Passet {} kan inte bokas, lokalen är stängd: {}.'.format(
            when, Closures.describe(closing)))
    elif instance.status in (0, -1) and replacing:
        messages.error(request, 'Passet {} kan inte bokas, då är det {} istället. Boka en plats där.'.format(
            when, Closures.describe(replacing)))
    elif instance.status in (0, -1) and unmet:
        messages.error(request, 'Passet {} kan bokas först när {} har en värd samma dag.'.format(
            when, DependencyChecker.describe(unmet)))
    elif instance.status in (0, -1):
        previous = instance.host if instance.status == -1 else None
        instance.status = 1
        instance.host = request.user
        instance.save()  # a new host: Google shows them from now on
        BookingChange.log(instance, 'booked', by=request.user, previous_host=previous)
        if previous:
            messages.success(request, 'Du tog över {} från {}.'.format(when, previous.slackId))
        else:
            messages.success(request, 'Du är värd för {}.'.format(when))
    else:
        messages.error(request, 'Passet {} är redan bokat av någon annan.'.format(when))
    return redirect(back)


# The same slots as a list: a whole period, week by week, day by day.

LIST_FILTERS = [('alla', 'Alla pass'), ('lediga', 'Lediga'), ('mina', 'Mina')]


def _week_anchor(monday):
    iso = monday.isocalendar()
    return 'v{}-{}'.format(iso.year, iso.week)


def _list_url(period, show='alla', earlier=False, anchor=None):
    params = ['period={}'.format(period.pk)]
    if show != 'alla':
        params.append('visa=' + show)
    if earlier:
        params.append('tidigare=1')
    return '{}?{}{}'.format(reverse('booking-list'), '&'.join(params), '#' + anchor if anchor else '')


def _bookable(item):
    """A slot or special pass seat this member can take now (or, if locked, soon)."""
    if 'slot' in item:
        slot = item['slot']
        return not slot.is_past and slot.state in ('free', 'rebook', 'locked')
    return item['entry'].is_special and item['entry'].can_join


def _mine(item):
    if 'slot' in item:
        return item['slot'].state in ('mine', 'handed')
    return item['entry'].is_special and item['entry'].mine


def _just_changed(item):
    return getattr(item.get('slot') or item.get('entry'), 'just_changed', False)


def _counts(items):
    free = sum(1 for item in items if _bookable(item)
               and not ('slot' in item and item['slot'].state == 'locked'))
    return free, sum(1 for item in items if _mine(item))


@login_required
def list_view(request):
    today = timezone.localdate()
    now = timezone.now()
    show = request.GET.get('visa')
    if show not in dict(LIST_FILTERS):
        show = 'alla'
    earlier = request.GET.get('tidigare') == '1'
    period = None
    if request.GET.get('period', '').isdigit():
        period = SchedulingPeriod.objects.filter(pk=request.GET['period']).first()
    period = period or _period_for(today)
    if period is None:
        return render(request, 'booking_list.html', {'period': None, 'filters': []})

    first_day, last_day = _period_weeks(period)
    by_day, excluded, entries = _load_days(first_day, last_day, request.user, now)
    _mark_just_changed(request, [{'slot': slot} for slots in by_day.values() for slot in slots]
                       + [{'entry': entry} for day_entries in entries.values() for entry in day_entries])
    this_monday = today - today.weekday() * ONE_DAY
    # Days before today are hidden unless asked for: what's left to book comes first.
    shown_from = period.start if earlier else max(period.start, today)
    hidden_before = shown_from > period.start

    weeks = []
    totals = {'free': 0, 'mine': 0}
    monday = first_day
    while monday <= period.end:
        days = []
        week_items = []
        for offset in range(7):
            day = monday + offset * ONE_DAY
            if not shown_from <= day <= period.end:
                continue
            day_entries = entries.get(day, [])
            items = _day_items(day, by_day.get(day, []), day_entries)
            week_items += items
            banners = [entry for entry in day_entries if entry.all_day]
            if show == 'lediga':
                items = [item for item in items if _bookable(item) or _just_changed(item)]
            elif show == 'mina':
                items = [item for item in items if _mine(item) or _just_changed(item)]
            if show != 'alla' and not items:
                continue
            if show == 'alla' and not (items or banners or excluded.get(day)):
                continue
            days.append({
                'date': day,
                'name': DAY_NAMES[offset],
                'today': day == today,
                'past': day < today,
                'excluded': excluded.get(day),
                'banners': banners,
                'items': items,
            })
        free, mine = _counts(week_items)
        totals['free'] += free
        totals['mine'] += mine
        if days or monday + 6 * ONE_DAY >= shown_from:
            weeks.append({
                'number': monday.isocalendar().week,
                'anchor': _week_anchor(monday),
                'span': _span(monday),
                'url': _week_url(monday),
                'current': monday == this_monday,
                'free': free,
                'mine': mine,
                'days': days,
            })
        monday += 7 * ONE_DAY

    def week_link(row_monday):
        # Earlier weeks are only on the page when earlier days are shown.
        return _list_url(period, show, earlier or row_monday + 6 * ONE_DAY < shown_from,
                         anchor=_week_anchor(row_monday))

    selected = this_monday if first_day <= this_monday <= last_day else first_day
    picker = _period_picker(period, selected, by_day, excluded, today, entries,
                            week_link=week_link, period_link=lambda other: _list_url(other, show))
    picker['covers_week'] = True
    previous, following = picker['prev_url'], picker['next_url']

    return render(request, 'booking_list.html', {
        'period': period,
        'weeks': weeks,
        'show': show,
        'filters': [{'value': value, 'label': label, 'url': _list_url(period, value, earlier),
                     'count': totals['free'] if value == 'lediga' else totals['mine'] if value == 'mina' else None,
                     'active': value == show}
                    for value, label in LIST_FILTERS],
        'totals': totals,
        'earlier_url': _list_url(period, show, True) if hidden_before else None,
        'later_url': _list_url(period, show) if earlier and period.start < today else None,
        'prev_url': previous, 'prev_name': picker['prev_name'],
        'next_url': following, 'next_name': picker['next_name'],
        'week_url': _week_url(selected),
        'here': request.get_full_path(),
        'picker': picker,
        'entry_kinds': CalendarEntry.KINDS,
        'progress': progress_for(period, [request.user], now)[0],
    })
