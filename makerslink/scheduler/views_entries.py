"""
Prototype: free-form calendar entries (courses, meetings, closures...).

Every logged-in member can see them; only staff can add, change or
delete them.
"""
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from functools import wraps
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import entry_sync
from .forms import CalendarEntryForm
from .models import CalendarEntry, SpecialPassType
from .views_week import _week_url


def staff_only(view):
    """Logged-in staff only; other members get a 403, visitors the login page."""
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied("Bara styrelsen kan lägga till och ändra händelser.")
        return view(request, *args, **kwargs)
    return wrapper


def _back_to_week(entry):
    return _week_url(timezone.localtime(entry.start).date())


def _sync(request, entry, previous_calendar=None):
    """Push the entry to Google and tell staff how it went."""
    if entry_sync.push(entry, previous_calendar):
        if entry.calendar:
            messages.info(request, 'Den finns nu också i Google-kalendern "{}".'.format(entry.calendar))
    else:
        messages.warning(request, 'Sparad här, men kunde inte läggas i Google-kalendern ({}). '
                                  'Försök igen från händelsens sida.'.format(entry.sync_error))


@staff_only
def entry_create(request):
    initial = {}
    if request.GET.get('date'):
        try:
            initial['date'] = datetime.date.fromisoformat(request.GET['date'])
        except ValueError:
            pass
    if request.GET.get('type', '').isdigit():
        special_type = SpecialPassType.objects.filter(pk=request.GET['type'], is_active=True).first()
        if special_type:
            initial = CalendarEntryForm.initial_from_type(special_type, initial.get('date'))
    form = CalendarEntryForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        entry = form.save(commit=False)
        entry.created_by = request.user
        entry.save()
        messages.success(request, 'Händelsen "{}" är tillagd.'.format(entry.title))
        _sync(request, entry)
        return redirect(_back_to_week(entry))
    return render(request, 'entry_form.html', {'form': form, 'entry': None,
                                               'kinds': CalendarEntry.KINDS,
                                               'special_types': SpecialPassType.objects.filter(is_active=True),
                                               'date': initial.get('date')})


@login_required
def entry_detail(request, pk):
    entry = get_object_or_404(CalendarEntry.objects.select_related('created_by', 'special_type'), pk=pk)
    context = {'entry': entry, 'can_edit': entry.can_edit(request.user), 'week_url': _back_to_week(entry)}
    if entry.is_special:
        hosts = entry.seat_hosts()
        mine = request.user in hosts
        upcoming = entry.start > timezone.now()
        context.update({
            'seat_hosts': hosts, 'mine': mine,
            'can_join': upcoming and not mine and len(hosts) < entry.hosts_needed,
            'can_leave': upcoming and mine,
            'changes': entry.changes.select_related('host', 'previous_host', 'by'),
        })
    return render(request, 'entry_detail.html', context)


@staff_only
def entry_update(request, pk):
    entry = get_object_or_404(CalendarEntry, pk=pk)
    previous_calendar = entry.calendar
    form = CalendarEntryForm(request.POST or None, instance=entry)
    if request.method == 'POST' and form.is_valid():
        entry = form.save()
        messages.success(request, 'Händelsen "{}" är ändrad.'.format(entry.title))
        _sync(request, entry, previous_calendar)
        return redirect(_back_to_week(entry))
    return render(request, 'entry_form.html', {'form': form, 'entry': entry,
                                               'kinds': CalendarEntry.KINDS})


@staff_only
def entry_delete(request, pk):
    entry = get_object_or_404(CalendarEntry, pk=pk)
    if request.method == 'POST':
        week = _back_to_week(entry)
        title = entry.title
        error = entry_sync.remove(entry)
        entry.delete()
        messages.success(request, 'Händelsen "{}" är borttagen.'.format(title))
        if error:
            messages.warning(request, 'Den kunde inte tas bort ur Google-kalendern ({}). '
                                      'Kalenderkontrollen kommer att visa den.'.format(error))
        return redirect(week)
    return render(request, 'entry_confirm_delete.html', {'entry': entry})


@staff_only
def entry_sync_now(request, pk):
    """Retry pushing one entry to Google."""
    entry = get_object_or_404(CalendarEntry, pk=pk)
    if request.method == 'POST':
        _sync(request, entry)
    return redirect(entry.get_absolute_url())
