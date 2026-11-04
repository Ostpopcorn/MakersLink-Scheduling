"""
Prototype: a staff page for following up hosts who are behind on booking.
"""
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Count, Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.models import User

from .booking_rules import DependencyChecker
from .host_progress import STATUS_LABELS, progress_for
from .models import HostReminder, SchedulingPeriod
from .reminders import NEEDS_REMINDER, bookable_soon, compose, send_reminder, site_url
from .views_week import DAY_SHORT, MONTH_NAMES, _period_for, _slots_by_day

# Who needs attention first.
STATUS_ORDER = ['none', 'missed', 'behind', 'on_track', 'done']
UPCOMING_DAYS = 14

staff_required = user_passes_test(lambda user: user.is_active and user.is_staff)


def _last_seen(user, now):
    if user.last_login is None:
        return 'aldrig', None
    days = (timezone.localdate(now) - timezone.localdate(user.last_login)).days
    text = 'idag' if days == 0 else 'igår' if days == 1 else 'för {} dagar sedan'.format(days)
    return text, days


def _unhosted_upcoming(today, now):
    """Slots in the coming days that nobody hosts, grouped by day."""
    last = today + datetime.timedelta(days=UPCOMING_DAYS - 1)
    checker = DependencyChecker(today, last)
    days = []
    total = 0
    for day, slots in sorted(_slots_by_day(today, last).items()):
        total += len(slots)
        open_slots = []
        for slot in slots:
            if slot.end <= now or slot.status in (1, 2):
                continue
            unmet = checker.unmet(slot)
            open_slots.append({
                'slot': slot,
                'locked': bool(unmet),
                'needs': checker.describe(unmet) if unmet else '',
                'rebook': slot.status == -1,
            })
        if open_slots:
            days.append({'date': day,
                         'label': '{} {} {}'.format(DAY_SHORT[day.weekday()], day.day,
                                                    MONTH_NAMES[day.month - 1][:3]).capitalize(),
                         'slots': open_slots})
    return days, total


@staff_required
def followup(request, pk=None):
    today = timezone.localdate()
    now = timezone.now()
    period = get_object_or_404(SchedulingPeriod, pk=pk) if pk else _period_for(today)
    if period is None:
        return render(request, 'followup.html', {'period': None})

    assigned = list(period.hosts.order_by('slackId'))
    # Active in the period (hosted or took part) without being one of its hosts.
    unassigned = list(period.active_unassigned_hosts.order_by('slackId'))
    rows = progress_for(period, assigned + unassigned, now)
    unassigned_ids = {user.id for user in unassigned}
    reminded = {r['user']: r for r in HostReminder.objects.filter(period=period)
                .values('user').annotate(count=Count('id'), last=Max('sent_at'))}
    is_current = period.start <= today <= period.end
    for row in rows:
        row.assigned = row.user.id not in unassigned_ids
        row.reminded = reminded.get(row.user.id)
        row.can_remind = row.assigned and is_current and row.status in NEEDS_REMINDER
        row.last_seen, row.last_seen_days = _last_seen(row.user, now)
        if row.next_slot:
            start = timezone.localtime(row.next_slot.start)
            row.next_label = '{} {} {} {:%H:%M}'.format(
                DAY_SHORT[start.weekday()], start.day, MONTH_NAMES[start.month - 1][:3], start)
    rows.sort(key=lambda r: (not r.assigned, STATUS_ORDER.index(r.status),
                             r.committed_total, r.user.slackId))

    counts = {status: sum(1 for r in rows if r.assigned and r.status == status)
              for status in STATUS_ORDER}
    upcoming, upcoming_total = (_unhosted_upcoming(today, now)
                                if period.start <= today <= period.end else ([], 0))

    previous = SchedulingPeriod.objects.filter(start__lt=period.start).order_by('-start').first()
    following = SchedulingPeriod.objects.filter(start__gt=period.start).order_by('start').first()
    return render(request, 'followup.html', {
        'period': period,
        'is_current': is_current,
        'is_over': today > period.end,
        'elapsed': period.get_elapsed(),
        'length': period.get_length(),
        'progress_pct': period.get_progress(),
        'rows': rows,
        'tiles': [{'status': s, 'label': STATUS_LABELS[s], 'count': counts[s]}
                  for s in (['missed', 'done'] if today > period.end
                            else ['none', 'behind', 'on_track', 'done'])],
        'upcoming': upcoming,
        'upcoming_open': sum(len(d['slots']) for d in upcoming),
        'upcoming_total': upcoming_total,
        'upcoming_days': UPCOMING_DAYS,
        'previous': previous,
        'following': following,
    })


@staff_required
def remind(request, period_pk, user_pk):
    """Preview a booking reminder to one host (GET) and send it (POST)."""
    period = get_object_or_404(SchedulingPeriod, pk=period_pk)
    host = get_object_or_404(User, pk=user_pk)
    progress = progress_for(period, [host])[0]
    slots = bookable_soon()
    base_url = site_url(request)
    if request.method == 'POST':
        send_reminder(progress, base_url, slots, sent_by=request.user)
        messages.success(request, 'Påminnelse skickad till {} ({}).'.format(host.slackId, host.email))
        return redirect('followup-period', pk=period.pk)
    subject, body = compose(progress, base_url, slots)
    return render(request, 'followup_remind.html', {
        'period': period,
        'host': host,
        'progress': progress,
        'subject': subject,
        'body': body,
        'earlier': HostReminder.objects.filter(period=period, user=host).select_related('sent_by'),
    })
