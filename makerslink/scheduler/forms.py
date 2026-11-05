import logging
logger = logging.getLogger(__name__)

from django import forms
from django.db.models import Q
from .widgets import DateMonthPicker, DatePicker
from django.forms import BaseFormSet, Textarea
from .models import EventTemplate, SchedulingCalendar, EventInstance, SchedulingPeriod, SchedulingRuleExclusion, BookingDependency, CalendarEntry, SpecialPassType
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
import datetime #for checking renewal date range.
from django.utils import timezone

"""
1. Calendar - koppling mot google calendar - crud
2. Template - hur skall saker formateras - crud
3. Event - Koppla ihop calendars och templates - crud
4. Instance - Skapa en instance i kalendern
5. Visa instances
6. Boka instance genom att klicka
7. Reschedule/Cancel instance - Detta måste kopplas så att den automatiskt blir inställd efter en viss tid eller dylikt
8. Användare överallt istället för string-generic
9. Skapa ett schema som skapar instancer x-dagar framåt
10. Rapporter för vad som är vad
11. Fixarpass 
12. Bekräfta e-post för registrering
13. Slack-id och tagg-id på användare
14. Skapa rättigheter för de olika funktionerna
15. Skapa en vy där vi kan lägga till icke-medlemmar i grupper (välj grupp, visa lista på användare som inte är med, bocka i och spara)


"""


class PeriodForm(forms.ModelForm):
    add_from_last = forms.BooleanField(help_text="Add hosts from last period.", required=False)
    class Meta:
        fields = '__all__'
        model = SchedulingPeriod
        widgets = {
            'start': DateMonthPicker,
            'end': DateMonthPicker,
            'hosts': forms.CheckboxSelectMultiple,
        }
    def save(self, commit=True):
        if self.cleaned_data['add_from_last']:
            self.cleaned_data['hosts'] = SchedulingPeriod.objects.latest('end').hosts.all() | self.cleaned_data['hosts']
        
        return super(PeriodForm, self).save(commit=commit)

class RuleExclusionForm(forms.ModelForm):
    class Meta:
        fields = '__all__'
        model = SchedulingRuleExclusion
        widgets = {
            'excluded_date': DatePicker,
        }

class EventInstanceAdminForm(forms.ModelForm):
    class Meta:
        #fields = '__all__'
        fields = ('status', 'unique_title', 'unique_description', 'participants')
        model = EventInstance

class BookingDependencyForm(forms.ModelForm):
    """Weekdays as checkboxes instead of the stored "0,1,2,3,4" string."""
    weekdays = forms.TypedMultipleChoiceField(
        choices=BookingDependency.WEEKDAY_CHOICES, coerce=int,
        widget=forms.CheckboxSelectMultiple,
        help_text="The rule only applies on these days")

    class Meta:
        model = BookingDependency
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial['weekdays'] = sorted(
            self.instance.weekday_set() if self.instance.pk else {0, 1, 2, 3, 4})

    def clean_weekdays(self):
        return ",".join(str(day) for day in sorted(self.cleaned_data['weekdays']))

    def clean(self):
        cleaned = super().clean()
        both = set(cleaned.get('dependent_events') or []) & set(cleaned.get('required_events') or [])
        if both:
            raise ValidationError("A slot cannot require itself: {}".format(
                ", ".join(str(event) for event in both)))
        return cleaned


class CalendarEntryForm(forms.ModelForm):
    """
    A date and an optional time span instead of two datetimes: leave the
    times empty for an all-day entry, add an end date for several days.

    Uses the browser's own date and time inputs. The project-wide date
    picker widget (scheduler/__init__.py) does not render on Django 4+,
    so every widget here is set explicitly.
    """
    date = forms.DateField(label="Datum", widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    start_time = forms.TimeField(label="Från", required=False,
                                 widget=forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'))
    end_time = forms.TimeField(label="Till", required=False,
                               widget=forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'))
    end_date = forms.DateField(label="Slutdatum", required=False,
                               help_text="Bara om händelsen pågår flera dagar",
                               widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))

    class Meta:
        model = CalendarEntry
        fields = ('title', 'kind', 'description', 'calendar', 'special_type', 'hosts_needed')
        labels = {'title': 'Rubrik', 'kind': 'Typ', 'description': 'Beskrivning',
                  'calendar': 'Google-kalender', 'special_type': 'Sorts specialpass',
                  'hosts_needed': 'Antal värdar'}
        help_texts = {'calendar': 'Händelsen läggs också in i den här kalendern.',
                      'hosts_needed': 'Så många kan boka sig som värd på passet.'}
        widgets = {
            'kind': forms.RadioSelect,
            'description': forms.Textarea(attrs={'rows': 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['calendar'].empty_label = 'Ingen (visas bara här)'
        self.fields['special_type'].empty_label = '–'
        self.fields['hosts_needed'].required = False  # only a special pass needs it
        self.fields['special_type'].queryset = SpecialPassType.objects.filter(
            Q(is_active=True) | Q(pk=self.instance.special_type_id))
        entry = self.instance
        if not entry.pk:
            self.initial.setdefault('calendar', SchedulingCalendar.objects.order_by('name').first())
        if entry.pk:
            start = timezone.localtime(entry.start)
            days = entry.local_days()
            self.initial.setdefault('date', days[0])
            if len(days) > 1:
                self.initial.setdefault('end_date', days[-1])
            if not entry.all_day:
                self.initial.setdefault('start_time', start.time())
                self.initial.setdefault('end_time', timezone.localtime(entry.end).time())

    @staticmethod
    def initial_from_type(special_type, date=None):
        """Form values for a new special pass of `special_type`."""
        initial = {'kind': 'special', 'special_type': special_type, 'title': special_type.name,
                   'description': special_type.description, 'hosts_needed': special_type.hosts_needed,
                   'start_time': special_type.start_time, 'end_time': special_type.end_time}
        if date:
            initial['date'] = date
        return initial

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('kind') == 'special':
            if not cleaned.get('special_type'):
                self.add_error('special_type', 'Välj vilken sorts specialpass det är')
            if not cleaned.get('hosts_needed'):
                self.add_error('hosts_needed', 'Ett specialpass behöver minst en värd')
            elif self.instance.pk and cleaned['hosts_needed'] < self.instance.seats.count():
                self.add_error('hosts_needed', '{} värdar har redan bokat passet'.format(
                    self.instance.seats.count()))
            if cleaned.get('start_time') is None and cleaned.get('end_time') is None:
                self.add_error('start_time', 'Ett specialpass behöver en tid')
        else:
            cleaned['special_type'], cleaned['hosts_needed'] = None, 0
        date, end_date = cleaned.get('date'), cleaned.get('end_date')
        start_time, end_time = cleaned.get('start_time'), cleaned.get('end_time')
        if date is None:
            return cleaned
        end_date = end_date or date
        if end_date < date:
            self.add_error('end_date', 'Slutdatum kan inte vara före datum')
            return cleaned
        if (start_time is None) != (end_time is None):
            self.add_error('end_time' if end_time is None else 'start_time',
                           'Fyll i både från och till, eller ingen tid alls för en heldag')
            return cleaned
        tz = timezone.get_current_timezone()
        if start_time is None:
            self.instance.all_day = True
            self.instance.start = datetime.datetime.combine(date, datetime.time(), tz)
            self.instance.end = datetime.datetime.combine(
                end_date + datetime.timedelta(days=1), datetime.time(), tz)
        else:
            self.instance.all_day = False
            self.instance.start = datetime.datetime.combine(date, start_time, tz)
            self.instance.end = datetime.datetime.combine(end_date, end_time, tz)
            if self.instance.end <= self.instance.start:
                self.add_error('end_time', 'Slutet måste vara efter starten')
                # Already reported on "Till"; CalendarEntry.clean() would
                # report it again on `end`, which this form does not have.
                self.instance.end = None
        return cleaned


class SpecialPassTypeForm(forms.ModelForm):
    class Meta:
        model = SpecialPassType
        fields = ('name', 'description', 'start_time', 'end_time', 'hosts_needed',
                  'counts_as_session', 'blocks_slots', 'is_active')
        labels = {'name': 'Namn', 'description': 'Beskrivning', 'start_time': 'Börjar',
                  'end_time': 'Slutar', 'hosts_needed': 'Antal värdar',
                  'counts_as_session': 'Räknas som värdpass',
                  'blocks_slots': 'Vanliga pass samtidigt kan inte bokas',
                  'is_active': 'Aktiv'}
        help_texts = {'name': 'Blir också rubriken på varje pass, t.ex. "Introkväll".',
                      'description': 'Förifylld beskrivning på varje pass.',
                      'counts_as_session': 'En plats som värd räknas mot periodens krav, som ett vanligt pass.',
                      'blocks_slots': 'Ordinarie pass som krockar med specialpasset går inte att boka.',
                      'is_active': 'Inaktiva typer erbjuds inte för nya pass.'}
        widgets = {
            'start_time': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
            'end_time': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('start_time') and cleaned.get('end_time') and cleaned['end_time'] <= cleaned['start_time']:
            self.add_error('end_time', 'Slutet måste vara efter starten')
        if cleaned.get('hosts_needed') == 0:
            self.add_error('hosts_needed', 'Minst en värd')
        return cleaned
