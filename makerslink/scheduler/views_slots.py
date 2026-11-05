"""
Prototype: staff booking a slot for a member, changing its host, or removing
a booking. Every change goes into the booking log (models.BookingChange),
and the hosts concerned are emailed unless staff untick that.
"""
import datetime

from django import forms
from django.contrib import messages
from django.core.mail import send_mail
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.models import User

from .booking_rules import Closures, DependencyChecker
from .host_progress import progress_for
from .models import BookingChange, Event, EventInstance
from .reminders import site_url
from .views_entries import staff_only
from .views_week import DAY_NAMES, MONTH_NAMES, SLOT_URL_TIME as URL_TIME, _week_url, slot_url


def _find_slot(event_pk, when):
    event = get_object_or_404(Event, pk=event_pk)
    try:
        start = datetime.datetime.strptime(when, URL_TIME).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        raise Http404("Bad slot time")
    for instance in event.get_events(start - datetime.timedelta(days=1), start + datetime.timedelta(days=1)):
        if instance.start == start:
            return instance
    raise Http404("No such slot")


def _describe_when(instance):
    start, end = timezone.localtime(instance.start), timezone.localtime(instance.end)
    return '{} {} {} {:%H:%M}–{:%H:%M}'.format(DAY_NAMES[start.weekday()], start.day,
                                               MONTH_NAMES[start.month - 1], start, end)


class StaffBookForm(forms.Form):
    host = forms.TypedChoiceField(label="Värd", coerce=int)
    override = forms.BooleanField(label="Boka trots att passet är låst", required=False)
    notify = forms.BooleanField(label="Mejla värden", required=False, initial=True)
    note = forms.CharField(label="Meddelande (valfritt)", required=False,
                           widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, period=None, exclude=None, **kwargs):
        super().__init__(*args, **kwargs)
        excluded = set(exclude) if isinstance(exclude, (set, list, tuple)) else {exclude}
        period_hosts = list(period.hosts.filter(is_active=True).order_by('slackId')) if period else []
        choices = []
        if period_hosts:
            progress = {p.user.id: p for p in progress_for(period, period_hosts)}
            choices.append(('Värdar i perioden', [
                (u.id, '{} – {} av {}'.format(u.slackId, progress[u.id].host_committed, progress[u.id].host_required))
                for u in period_hosts if u.id not in excluded]))
        others = (User.objects.filter(is_active=True).exclude(id__in=[u.id for u in period_hosts])
                  .exclude(id__in=[e for e in excluded if e is not None]).order_by('slackId'))
        if others:
            choices.append(('Övriga medlemmar', [(u.id, u.slackId) for u in others]))
        self.fields['host'].choices = [('', '– välj –')] + choices


class StaffRemoveForm(forms.Form):
    AFTER = [(-1, 'Ombokning önskas (visas i rött så att andra ser att passet behöver en värd)'), (0, 'Ledigt')]
    after = forms.TypedChoiceField(label="Sedan blir passet", choices=AFTER, coerce=int, initial=-1,
                                   widget=forms.RadioSelect)
    notify = forms.BooleanField(label="Mejla värden", required=False, initial=True)
    note = forms.CharField(label="Anledning (valfritt, står i mejlet och i historiken)", required=False,
                           widget=forms.Textarea(attrs={'rows': 2}))


def _mail(request, user, subject, lines):
    try:
        send_mail(subject, '\n'.join(lines + ['', 'Hälsningar,', 'MakersLink bokningssystem']),
                  None, [user.email])
        return True
    except Exception as error:
        messages.warning(request, 'Kunde inte mejla {}: {}'.format(user.slackId, error))
        return False


def _book(request, instance, form, checker):
    host = User.objects.get(pk=form.cleaned_data['host'])
    note = form.cleaned_data['note'].strip()
    when = _describe_when(instance)
    if checker.closing(instance):
        messages.error(request, 'Lokalen är stängd då ({}). Ändra stängningen först.'.format(
            Closures.describe(checker.closing(instance))))
        return
    if checker.replacing(instance):
        messages.error(request, 'Då är det {} istället. Lägg till värden på specialpasset i stället.'.format(
            Closures.describe(checker.replacing(instance))))
        return
    unmet = checker.unmet(instance)
    if unmet and not form.cleaned_data['override']:
        messages.error(request, 'Passet är låst: det kräver att {} har en värd. Kryssa i "Boka trots att '
                                'passet är låst" om det är meningen.'.format(DependencyChecker.describe(unmet)))
        return
    previous = instance.host if instance.status in EventInstance.HOSTED_STATUSES else None
    if previous == host:
        messages.info(request, '{} är redan värd för passet.'.format(host.slackId))
        return
    instance.status, instance.host = 1, host
    instance.save()
    BookingChange.log(instance, 'staff_reassigned' if previous else 'staff_booked', by=request.user,
                      host=host, previous_host=previous, note=note)
    messages.success(request, '{} är nu värd för {}.'.format(host.slackId, when))
    if form.cleaned_data['notify']:
        week = site_url(request) + _week_url(timezone.localtime(instance.start).date())
        _mail(request, host, 'Du är värd: {} {}'.format(instance.title, when), [
            'Hej {}!'.format(host.slackId), '',
            '{} i styrelsen har bokat dig som värd för {}, {}.'.format(request.user.slackId, instance.title, when)]
            + (['', note] if note else []) + ['', 'Kan du inte? Önska ombokning i veckovyn: {}'.format(week)])
        if previous:
            _mail(request, previous, 'Du är inte längre värd: {} {}'.format(instance.title, when), [
                'Hej {}!'.format(previous.slackId), '',
                '{} i styrelsen har flyttat passet {}, {}, till {}.'.format(
                    request.user.slackId, instance.title, when, host.slackId)] + (['', note] if note else []))


def _remove(request, instance, form):
    # Removing a booking is the real cancellation, also of one marked
    # "Ombokning önskas": only staff can do it.
    if instance.status not in EventInstance.HOSTED_STATUSES or instance.host is None:
        messages.error(request, 'Passet har ingen bokning att ta bort.')
        return
    previous = instance.host
    note = form.cleaned_data['note'].strip()
    when = _describe_when(instance)
    instance.status, instance.host = form.cleaned_data['after'], None
    instance.save()
    BookingChange.log(instance, 'staff_removed', by=request.user, host=previous, note=note)
    messages.success(request, 'Bokningen är borttagen: {} är inte längre värd för {}.'.format(previous.slackId, when))
    if form.cleaned_data['notify']:
        _mail(request, previous, 'Din bokning är borttagen: {} {}'.format(instance.title, when), [
            'Hej {}!'.format(previous.slackId), '',
            '{} i styrelsen har tagit bort din bokning av {}, {}.'.format(request.user.slackId, instance.title, when)]
            + (['', 'Anledning: {}'.format(note)] if note else []))


@staff_only
def slot_manage(request, event_pk, when):
    instance = _find_slot(event_pk, when)
    day = timezone.localtime(instance.start).date()
    checker = DependencyChecker(day, day)
    booked = instance.status in EventInstance.HOSTED_STATUSES and instance.host is not None
    exclude = instance.host_id if booked else None
    book_form = StaffBookForm(request.POST if request.POST.get('action') == 'book' else None,
                              period=instance.period, exclude=exclude)
    remove_form = StaffRemoveForm(request.POST if request.POST.get('action') == 'remove' else None)
    started = instance.start <= timezone.now()
    saved = instance.pk is not None and not instance._state.adding

    if request.method == 'POST' and not started:
        if request.POST.get('action') == 'book' and book_form.is_valid():
            _book(request, instance, book_form, checker)
            return redirect(slot_url(instance))
        if request.POST.get('action') == 'remove' and remove_form.is_valid():
            _remove(request, instance, remove_form)
            return redirect(slot_url(instance))

    return render(request, 'slot_manage.html', {
        'slot': instance,
        'when': _describe_when(instance),
        'booked': booked,
        'started': started,
        'closing': Closures.describe(checker.closing(instance)),
        'replacing': checker.replacing(instance),
        'unmet': DependencyChecker.describe(checker.unmet(instance)),
        'book_form': book_form,
        'remove_form': remove_form,
        'saved': saved,
        'changes': instance.changes.select_related('host', 'previous_host', 'by') if saved else [],
        'week_url': _week_url(day),
    })
