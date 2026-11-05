"""
Prototype: the staff page for booking rules (models.BookingDependency),
instead of the Django admin. Rules belong to a hosting period, so the page
shows one period at a time; each rule is shown as a sentence, with what it
locks in the coming weeks so staff can see the effect of a change.
"""
import datetime

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .booking_rules import DependencyChecker
from .forms import BookingRuleForm
from .models import BookingDependency, Event, SchedulingPeriod
from .views_entries import staff_only
from .views_week import DAY_SHORT, _period_for, _slots_by_day, _week_url

LOOKAHEAD_DAYS = 28


def rules_url(period):
    return '{}?period={}'.format(reverse('booking-rules'), period.pk)


def _window(period, today):
    """The days the page shows the effect for: four weeks from today (or the period's start), within it."""
    first = max(today, period.start)
    last = min(period.end, first + datetime.timedelta(days=LOOKAHEAD_DAYS - 1))
    return (first, last) if first <= last else None


def _impact(rules, first, last, now):
    """
    For each rule id: how many slots nobody holds it locks between `first`
    and `last`, how many booked ones it warns about, and the first few.
    """
    checker = DependencyChecker(first, last)
    impact = {rule.pk: {'locked': 0, 'warned': 0, 'examples': []} for rule in rules}
    for day, slots in sorted(_slots_by_day(first, last).items()):
        for slot in slots:
            if slot.start <= now or slot.status == 2:
                continue
            held = slot.host_id is not None and slot.status != 0
            for unmet in checker.unmet(slot):
                found = impact.get(unmet['rule'].pk)
                if found is None:
                    continue
                found['warned' if held else 'locked'] += 1
                if len(found['examples']) < 3:
                    local = timezone.localtime(slot.start)
                    found['examples'].append({
                        'label': '{} {}/{} {:%H:%M}'.format(DAY_SHORT[day.weekday()], day.day, day.month, local),
                        'url': _week_url(day),
                        'held': held,
                    })
    return impact


def _previous(period):
    return SchedulingPeriod.objects.filter(start__lt=period.start).order_by('-start').first()


@staff_only
def booking_rules(request):
    today, now = timezone.localdate(), timezone.now()
    period = None
    if request.GET.get('period', '').isdigit():
        period = SchedulingPeriod.objects.filter(pk=request.GET['period']).first()
    period = period or _period_for(today)
    if period is None:
        return render(request, 'booking_rules.html', {'period': None})

    rules = list(period.booking_rules.prefetch_related('dependent_events', 'required_events')
                 .order_by('-is_active', 'name', 'pk'))
    window = _window(period, today)
    impact = _impact([rule for rule in rules if rule.is_active], *window, now) if window else {}
    for rule in rules:
        rule.text = rule.sentence(list(rule.dependent_events.all()), list(rule.required_events.all()))
        rule.impact = impact.get(rule.pk)

    previous = _previous(period)
    following = SchedulingPeriod.objects.filter(start__gt=period.start).order_by('start').first()
    return render(request, 'booking_rules.html', {
        'period': period,
        'rules': rules,
        'window': window,
        'previous': previous,
        'previous_rules': previous.booking_rules.count() if previous else 0,
        'prev_url': rules_url(previous) if previous else None,
        'next_url': rules_url(following) if following else None,
        'following': following,
    })


@staff_only
def booking_rule_edit(request, pk=None):
    rule = get_object_or_404(BookingDependency.objects.select_related('period'), pk=pk) if pk else None
    initial = {}
    if rule is None and request.GET.get('period', '').isdigit():
        initial['period'] = request.GET['period']
    elif rule is None:
        current = _period_for(timezone.localdate())
        initial['period'] = current.pk if current else None
    form = BookingRuleForm(request.POST or None, instance=rule, initial=initial)
    if request.method == 'POST' and form.is_valid():
        saved = form.save()
        messages.success(request, 'Regeln är sparad: {}'.format(saved.sentence(with_period=True)))
        return redirect(rules_url(saved.period))
    back = rules_url(rule.period) if rule else (
        '{}?period={}'.format(reverse('booking-rules'), initial['period']) if initial.get('period')
        else reverse('booking-rules'))
    return render(request, 'booking_rule_form.html', {
        'form': form, 'rule': rule, 'back': back,
        'event_names': {event.pk: event.name for event in Event.objects.all()},
        'weekday_names': [name.lower() for _, name in BookingDependency.WEEKDAY_CHOICES],
    })


@staff_only
@require_POST
def booking_rule_toggle(request, pk):
    rule = get_object_or_404(BookingDependency, pk=pk)
    if not rule.is_active:
        # Switching a rule back on must not make slots wait on each other:
        # run it through the form's checks as if it were saved active.
        data = {
            'period': rule.period_id,
            'dependent_events': [e.pk for e in rule.dependent_events.all()],
            'required_events': [e.pk for e in rule.required_events.all()],
            'require_all': str(rule.require_all), 'day_offset': str(rule.day_offset),
            'same_host': 'on' if rule.same_host else '',
            'weekdays': sorted(rule.weekday_set()),
            'name': rule.name, 'description': rule.description, 'is_active': 'on',
        }
        form = BookingRuleForm(data, instance=rule)
        if not form.is_valid():
            messages.error(request, 'Regeln kan inte slås på: {}'.format(
                ' '.join(error for errors in form.errors.values() for error in errors)))
            return redirect(rules_url(rule.period))
        form.save()
        messages.success(request, 'Regeln används igen.')
    else:
        rule.is_active = False
        rule.save(update_fields=['is_active'])
        messages.success(request, 'Regeln är pausad. Den sparas men låser inget.')
    return redirect(rules_url(rule.period))


@staff_only
@require_POST
def booking_rule_delete(request, pk):
    rule = get_object_or_404(BookingDependency, pk=pk)
    text, period = rule.sentence(), rule.period
    rule.delete()
    messages.success(request, 'Regeln är borttagen: {}'.format(text))
    return redirect(rules_url(period))


@staff_only
@require_POST
def booking_rules_copy(request, pk):
    """Copy the previous period's rules into period `pk` (skipping ones it already has)."""
    period = get_object_or_404(SchedulingPeriod, pk=pk)
    source = _previous(period)
    if source is None:
        messages.error(request, 'Det finns ingen tidigare period att kopiera från.')
    else:
        copied = BookingDependency.copy_period(source, period)
        if copied:
            messages.success(request, '{} {} kopierades från {}.'.format(
                copied, 'regel' if copied == 1 else 'regler', source.name))
        else:
            messages.info(request, 'Perioden har redan alla regler från {}.'.format(source.name))
    return redirect(rules_url(period))
