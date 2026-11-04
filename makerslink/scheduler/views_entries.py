"""
Prototype: free-form calendar entries (courses, meetings, closures...).

Any logged-in member can add one. Only whoever created it, or staff, can
change or delete it.
"""
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import CalendarEntryForm
from .models import CalendarEntry
from .views_week import _week_url


def _back_to_week(entry):
    return _week_url(timezone.localtime(entry.start).date())


def _editable(request, pk):
    entry = get_object_or_404(CalendarEntry, pk=pk)
    if not entry.can_edit(request.user):
        raise PermissionDenied("Bara den som lade till händelsen, eller styrelsen, kan ändra den.")
    return entry


@login_required
def entry_create(request):
    initial = {}
    if request.GET.get('date'):
        try:
            initial['date'] = datetime.date.fromisoformat(request.GET['date'])
        except ValueError:
            pass
    form = CalendarEntryForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        entry = form.save(commit=False)
        entry.created_by = request.user
        entry.save()
        messages.success(request, 'Händelsen "{}" är tillagd.'.format(entry.title))
        return redirect(_back_to_week(entry))
    return render(request, 'entry_form.html', {'form': form, 'entry': None,
                                               'kinds': CalendarEntry.KINDS})


@login_required
def entry_detail(request, pk):
    entry = get_object_or_404(CalendarEntry.objects.select_related('created_by'), pk=pk)
    return render(request, 'entry_detail.html', {
        'entry': entry, 'can_edit': entry.can_edit(request.user),
        'week_url': _back_to_week(entry)})


@login_required
def entry_update(request, pk):
    entry = _editable(request, pk)
    form = CalendarEntryForm(request.POST or None, instance=entry)
    if request.method == 'POST' and form.is_valid():
        entry = form.save()
        messages.success(request, 'Händelsen "{}" är ändrad.'.format(entry.title))
        return redirect(_back_to_week(entry))
    return render(request, 'entry_form.html', {'form': form, 'entry': entry,
                                               'kinds': CalendarEntry.KINDS})


@login_required
def entry_delete(request, pk):
    entry = _editable(request, pk)
    if request.method == 'POST':
        week = _back_to_week(entry)
        title = entry.title
        entry.delete()
        messages.success(request, 'Händelsen "{}" är borttagen.'.format(title))
        return redirect(week)
    return render(request, 'entry_confirm_delete.html', {'entry': entry})
