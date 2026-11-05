"""
Prototype: the staff page for booking rules (models.BookingDependency),
instead of the Django admin. Each rule is shown as a sentence, with what
it locks right now so staff can see the effect of a change.
"""
import datetime

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .booking_rules import DependencyChecker
from .forms import BookingRuleForm
from .models import BookingDependency, Event
from .views_entries import staff_only
from .views_week import DAY_SHORT, _slots_by_day, _week_url

LOOKAHEAD_DAYS = 28


def _impact(rules, today, now):
    """
    For each rule id: how many slots nobody holds it locks in the coming
    weeks, how many booked ones it warns about, and the first few of them.
    """
    last = today + datetime.timedelta(days=LOOKAHEAD_DAYS - 1)
    checker = DependencyChecker(today, last)
    impact = {rule.pk: {'locked': 0, 'warned': 0, 'examples': []} for rule in rules}
    for day, slots in sorted(_slots_by_day(today, last).items()):
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


@staff_only
def booking_rules(request):
    rules = list(BookingDependency.objects.prefetch_related('dependent_events', 'required_events')
                 .order_by('-is_active', 'name', 'pk'))
    today, now = timezone.localdate(), timezone.now()
    impact = _impact([rule for rule in rules if rule.is_active], today, now)
    for rule in rules:
        rule.text = rule.sentence(list(rule.dependent_events.all()), list(rule.required_events.all()))
        rule.impact = impact.get(rule.pk)
    return render(request, 'booking_rules.html', {'rules': rules, 'lookahead_weeks': LOOKAHEAD_DAYS // 7})


@staff_only
def booking_rule_edit(request, pk=None):
    rule = get_object_or_404(BookingDependency, pk=pk) if pk else None
    form = BookingRuleForm(request.POST or None, instance=rule)
    if request.method == 'POST' and form.is_valid():
        saved = form.save()
        messages.success(request, 'Regeln är sparad: {}'.format(saved.sentence()))
        return redirect('booking-rules')
    return render(request, 'booking_rule_form.html', {
        'form': form, 'rule': rule,
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
            'dependent_events': [e.pk for e in rule.dependent_events.all()],
            'required_events': [e.pk for e in rule.required_events.all()],
            'require_all': str(rule.require_all), 'day_offset': str(rule.day_offset),
            'same_host': 'on' if rule.same_host else '',
            'weekdays': sorted(rule.weekday_set()),
            'valid_from': rule.valid_from.isoformat() if rule.valid_from else '',
            'valid_until': rule.valid_until.isoformat() if rule.valid_until else '',
            'name': rule.name, 'description': rule.description, 'is_active': 'on',
        }
        form = BookingRuleForm(data, instance=rule)
        if not form.is_valid():
            messages.error(request, 'Regeln kan inte slås på: {}'.format(
                ' '.join(error for errors in form.errors.values() for error in errors)))
            return redirect('booking-rules')
        form.save()
        messages.success(request, 'Regeln används igen.')
    else:
        rule.is_active = False
        rule.save(update_fields=['is_active'])
        messages.success(request, 'Regeln är pausad. Den sparas men låser inget.')
    return redirect('booking-rules')


@staff_only
@require_POST
def booking_rule_delete(request, pk):
    rule = get_object_or_404(BookingDependency, pk=pk)
    text = rule.sentence()
    rule.delete()
    messages.success(request, 'Regeln är borttagen: {}'.format(text))
    return redirect('booking-rules')
