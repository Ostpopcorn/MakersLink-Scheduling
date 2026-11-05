"""
Prototype: special passes, one-off sessions of a standard type (e.g.
"Introkväll") that several hosts sign up for. This module has the staff
pages for the types; the passes themselves are calendar entries.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import entry_sync
from .booking_rules import Closures
from .forms import SpecialPassTypeForm
from .models import BookingChange, CalendarEntry, EntryHost, SpecialPassType
from .views_entries import staff_only
from .views_week import _week_url


@staff_only
def special_passes(request):
    upcoming = (CalendarEntry.objects.filter(kind='special', end__gte=timezone.now())
                .select_related('special_type').prefetch_related('seats__user').order_by('start'))
    for entry in upcoming:
        entry.host_names = [seat.user.slackId for seat in entry.seats.all()]
    return render(request, 'special_passes.html', {
        'types': SpecialPassType.objects.all(),
        'upcoming': upcoming,
    })


@staff_only
def special_type_edit(request, pk=None):
    special_type = get_object_or_404(SpecialPassType, pk=pk) if pk else None
    form = SpecialPassTypeForm(request.POST or None, instance=special_type)
    if request.method == 'POST' and form.is_valid():
        saved = form.save()
        messages.success(request, 'Typen "{}" är sparad.'.format(saved))
        return redirect('special-passes')
    return render(request, 'special_type_form.html', {'form': form, 'special_type': special_type})


def seat_text(entry):
    start, end = timezone.localtime(entry.start), timezone.localtime(entry.end)
    return '{} {:%Y-%m-%d %H:%M}–{:%H:%M}'.format(entry.title, start, end)


@login_required
@require_POST
def entry_seat(request, pk):
    """A member taking or giving up a host seat on a special pass."""
    entry = get_object_or_404(CalendarEntry.objects.select_related('special_type'), pk=pk)
    if not entry.is_special:
        raise Http404("Not a special pass")
    back = request.POST.get('next') or _week_url(timezone.localtime(entry.start).date())
    what = seat_text(entry)
    if entry.start <= timezone.now():
        messages.error(request, '{} har redan börjat.'.format(what))
        return redirect(back)
    with transaction.atomic():
        seat = EntryHost.objects.filter(entry=entry, user=request.user).first()
        if seat:
            seat.delete()
            BookingChange.log(None, 'cancelled', by=request.user, host=request.user, entry=entry)
            messages.success(request, 'Du har bokat av din plats på {}.'.format(what))
        else:
            day = timezone.localtime(entry.start).date()
            closing = Closures(day, day).closing(entry)
            if closing:
                messages.error(request, '{} kan inte bokas, lokalen är stängd: {}.'.format(
                    what, Closures.describe(closing)))
                return redirect(back)
            if entry.seats.count() >= entry.hosts_needed:
                messages.error(request, '{} har redan alla {} värdar.'.format(what, entry.hosts_needed))
                return redirect(back)
            EntryHost.objects.create(entry=entry, user=request.user)
            BookingChange.log(None, 'booked', by=request.user, host=request.user, entry=entry)
            messages.success(request, 'Du är värd på {}.'.format(what))
    if not entry_sync.push(entry):
        messages.warning(request, 'Google-kalendern kunde inte uppdateras ({}); styrelsen ser det i '
                                  'kalenderkontrollen.'.format(entry.sync_error))
    return redirect(back)
