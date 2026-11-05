"""
Prototype: the staff page for the Google Calendar check (calendar_check.py).
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from .models import CalendarCheck

from . import calendar_check
from .views_followup import staff_required


@staff_required
def calendar_check_page(request):
    if request.method == 'POST':
        checks = [calendar_check.run(calendar, ran_by=request.user)
                  for calendar in calendar_check.calendars_to_check()]
        found = sum(len(c.differences) for c in checks)
        failed = [c for c in checks if c.error]
        if failed:
            messages.error(request, 'Kontrollen kunde inte köras mot {}.'.format(
                ', '.join(str(c.calendar) for c in failed)))
        elif found:
            messages.warning(request, 'Kontrollen hittade {} skillnad{}.'.format(found, 'er' if found != 1 else ''))
        else:
            messages.success(request, 'Kontrollen är klar: Google-kalendern stämmer med bokningssystemet.')
        return redirect('calendar-check')

    checked = {check.calendar_id: check for check in calendar_check.latest_checks()}
    calendars = []
    for calendar in calendar_check.calendars_to_check():
        check = checked.get(calendar.pk)
        calendars.append({
            'calendar': calendar,
            'check': check,
            'groups': calendar_check.grouped(check) if check else [],
            'fix_labels': calendar_check.FIX_LABELS,
            'history': list(calendar.checks.all()[:6]) if check else [],
        })
    return render(request, 'calendar_check.html', {'calendars': calendars})


@staff_required
def calendar_check_fix(request, pk):
    """Fix one difference (key) or every difference of one kind (kind) in a stored check."""
    check = get_object_or_404(CalendarCheck, pk=pk)
    if request.method == 'POST':
        if request.POST.get('kind'):
            keys = {d['key'] for d in check.differences if d['kind'] == request.POST['kind']}
        else:
            keys = {request.POST.get('key', '')}
        fixed, failed = calendar_check.fix_in_check(check, keys)
        if fixed:
            messages.success(request, 'Rättade {} i Google-kalendern.'.format(
                ', '.join('{} ({})'.format(d['title'], d['when']) for d, _ in fixed)))
        for difference, error in failed:
            messages.error(request, 'Kunde inte rätta {} ({}): {}'.format(
                difference['title'], difference['when'], error))
    return redirect('calendar-check')
