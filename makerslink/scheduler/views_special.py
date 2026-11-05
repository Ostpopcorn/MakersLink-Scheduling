"""
Prototype: special passes, one-off sessions of a standard type (e.g.
"Introkväll") that several hosts sign up for. This module has the staff
pages for the types; the passes themselves are calendar entries.
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import SpecialPassTypeForm
from .models import CalendarEntry, SpecialPassType
from .views_entries import staff_only


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
