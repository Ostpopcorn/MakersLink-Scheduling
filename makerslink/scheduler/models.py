import pytz
from django.utils import timezone
import datetime
from django.db.models import Q, Count, Max
from .fields import ParticipantKeyField
import accounts.models
from dateutil.rrule import *
from google.oauth2 import service_account
from django.conf import settings
from .google_calendar import client_for
from django.core.files.storage import FileSystemStorage
import uuid
from django.urls import reverse
from django.db import models
from django.db.models import CheckConstraint, Q, F
from django.core.exceptions import ValidationError
import logging
logger = logging.getLogger(__name__)

# New Filesystem
client_secret_fs = FileSystemStorage(location=settings.CALENDAR_PK_DIR)

# Shown in place of a title for rows that lost their template or event
# before these relations were changed to PROTECT. New rows can no longer
# end up in that state, but old databases still hold some, and they must
# stay viewable so staff can repair or delete them.
MISSING_RELATION_TITLE = "(borttagen mall)"

# Create your models here.


class EventTemplate(models.Model):
    """
    Defines how something will look in the google calendar
    """

    # Fields
    name = models.CharField(
        max_length=50, help_text="Enter a human-friendly name for this template")
    count_key = models.CharField(
        max_length=1, help_text="Key displayed when counting participantcy", default='D')
    title = models.CharField(
        max_length=100, help_text="Enter title to be used for booking")
    header = models.CharField(
        max_length=200, help_text="Enter a, optional, header for the event to be inserted after the hosts name into the descriptionfield in the calendar event.", null=True, blank=True)
    body = models.TextField(
        max_length=1000, help_text="Enter a larger body of text to be inserted after the header in the description field in the calendar event", null=True, blank=True)
    num_participants = models.IntegerField(
        default=0, help_text="Number of participants, -1 for infinite")
    calendar = models.ForeignKey('SchedulingCalendar', on_delete=models.PROTECT,
                                 null=True, help_text="Select the calendar to sync events to.")
    synchronize = models.BooleanField(
        default=True, help_text="If active, scheduled events will be synced to Google calendar upon creation.")

    # Metadata
    class Meta:
        ordering = ["name"]

    # Methods
    def clean(self):
        # Synchronising with nowhere to synchronise to cannot work, and used
        # to surface as an AttributeError deep inside EventInstance.save().
        if self.synchronize and self.calendar_id is None:
            raise ValidationError(
                {'calendar': ('Select a calendar, or turn off synchronize')})

    def _requireCalendar(self):
        if self.calendar is None:
            raise ValueError(
                "EventTemplate '{}' is set to synchronize but has no calendar".format(self.name))
        return self.calendar

    def get_absolute_url(self):
        """
         Returns the url to access a particular instance of EventTemplate.
         """
        return reverse('template-detail', args=[str(self.id)])

    def createEventEntry(self, host, start, end, status):
        if self.synchronize:
            data = self._createUpdatedEventData(host, start, end, status)
            # logger.warning("Data:")
            # logger.warning(data)
            # return True
            return self._requireCalendar().createEvent(data)
        else:
            return True

    def updateEventEntry(self, booking_id, host, start, end, status):
        if self.synchronize:
            data = self._createUpdatedEventData(host, start, end, status)
            # logger.warning("Data:")
            # logger.warning(data)
            # return True
            return self._requireCalendar().updateEvent(booking_id, data)
        else:
            return True

    def _createUpdatedEventData(self, host, start, end, status, unique_title="", unique_description=""):
        #logger.warning("createEventData:start: %s", start)
        # Check the configuration before building anything: a template set to
        # synchronize with no calendar cannot produce an event either way, and
        # the misconfiguration is what the caller needs to be told about.
        calendar = self._requireCalendar()
        if status == 2:
            if unique_title == "":
                summary = settings.CANCELLED_TITLE + self.title
            else:
                summary = unique_title + self.title

            if unique_description == "":
                description = settings.CANCELLED_DESCRIPTION
            else:
                description = unique_description
        else:
            summary = self.title

            # This text goes on the public calendar, so an instance that has
            # no host yet says nothing about one rather than claiming "None".
            parts = []
            if host is not None:
                parts.append('Värd: ' + host.slackId)
            if self.header:
                parts.append(self.header)
            if self.body:
                parts.append(self.body)
            description = "\n".join(parts)

        calendarTZ = pytz.timezone(calendar.timezone)
        event_data = {
            'summary': summary,
            'location': 'Makerspace Linköping',
            'description': description,
            'start': {
                'dateTime': start.astimezone(calendarTZ).strftime('%Y-%m-%dT%H:%M:%S'),
                'timeZone': calendar.timezone,
            },
            'end': {
                'dateTime': end.astimezone(calendarTZ).strftime('%Y-%m-%dT%H:%M:%S'),
                'timeZone': calendar.timezone,
            },
            'reminders': {
                'useDefault': False,
                'overrides': [],
            },
        }

        return event_data

    def deleteEventEntry(self, booking_id):
        if self.synchronize:
            return self._requireCalendar().deleteEvent(booking_id)
        else:
            return True

    def __str__(self):
        """
        String for representing the EventTemplate object (in Admin site etc.)
        """
        return self.name


class SchedulingCalendar(models.Model):
    """
    Defines a Google calendar to insert bookings into
    https://stackoverflow.com/questions/37754999/google-calendar-integration-with-django
    """

    # Data
    TIMEZONES = (
        ("Europe/Stockholm", "Europe/Stockholm"),
    )

    # Fields
    id = models.UUIDField(primary_key=True, default=uuid.uuid4,
                          editable=False, help_text="Unique ID for this calendar")
    name = models.CharField(
        max_length=50, help_text="Enter a human-friendly name for this Google Calendar")
    google_calendar_id = models.CharField(
        max_length=250, help_text="Enter a calendar id")
    service_account_username = models.CharField(
        max_length=250, help_text="Enter the username for the service account used")
    timezone = models.CharField(
        max_length=50, choices=TIMEZONES, help_text='Calendar timezone')
    service_account = models.FileField(
        storage=client_secret_fs, help_text='Upload client_secret json-file', null=True, blank=True)
    scope = models.TextField(default="https://www.googleapis.com/auth/calendar",
                             help_text='Enter scope of api calls, change at your own risk')

    # Metadata
    class Meta:
        ordering = ["name"]

    # Methods
    def get_absolute_url(self):
        """
         Returns the url to access a particular instance of SchedulingCalendar.
         """
        return reverse('calendar-detail', args=[str(self.id)])

    # All Google traffic goes through scheduler/google_calendar.py. Failures
    # are re-raised as ValueError, as before, but keep the cause attached.
    def createEvent(self, data):
        logger.warning("SchedulingCalendar:createEvent called")
        try:
            return client_for(self).insert(data)
        except Exception as error:
            raise ValueError("Could not create event in calendar") from error

    def updateEvent(self, id, data):
        logger.warning("SchedulingCalendar:updateEvent called")
        try:
            client_for(self).update(id, data)
            return True
        except Exception as error:
            raise ValueError("Could not update event in calendar") from error

    def deleteEvent(self, id):
        logger.warning("SchedulingCalendar:deleteEvent called")
        try:
            client_for(self).delete(id)
            return True
        except Exception as error:
            raise ValueError("Could not delete event in calendar") from error

    def __str__(self):
        """
        String for representing the SchedulingCalendar object (in Admin site etc.)
        """
        return self.name

    def get_credentials(self):
        credentials = service_account.Credentials.from_service_account_file(
            self.service_account.path, scopes=[self.scope])
        return credentials


class Event(models.Model):
    """
    Defines a booking that users can create themselves.
    User only gets to chose day and time to create a EventInstance
    """
    # Data
    #objects = BookingManager()

    # Fields
    name = models.CharField(
        max_length=50, help_text="Enter a human-friendly name for this type of Event")
    description = models.CharField(
        max_length=300, help_text="Enter a description for this type of Event", null=True, blank=True)
    template = models.ForeignKey('EventTemplate', on_delete=models.PROTECT, null=True,
                                 help_text="Select a template for how scheduled Events will look in the calendar.")
    start = models.DateTimeField(
        help_text="Start of event repetition and start time of events", db_index=True)
    end = models.DateTimeField(
        help_text="End time of events, must be after start", db_index=True)
    repeat_end = models.DateTimeField(
        help_text="Date to end repetition, must be after start", null=True, blank=True)
    rule = models.ForeignKey('SchedulingRule', null=True, blank=True,
                             help_text="Select '----' for a one time only event.", on_delete=models.SET_NULL)

    # Metadata
    class Meta:
        ordering = ["-start"]
        constraints = [
            models.CheckConstraint(
                condition=Q(start__lt=F('end')),
                name='check_start_before_end'
            ),
            models.CheckConstraint(
                condition=Q(start__lt=F('repeat_end')),
                name='check_start_before_repeat_end'
            ),
        ]

    # Methods
    @property
    def max_num_participants(self):
        if self.template is None:
            return 0
        return self.template.num_participants

    def clean(self):
        # Only validate future requirement on create not on edit.
        if self.pk is None and self.start <= timezone.now():
            raise ValidationError(
                {'start': ('Start date is not in the future')})
        if self.start > self.end:
            raise ValidationError({'start': ('Start date is after end date')})
        if self.repeat_end and self.start > self.repeat_end:
            raise ValidationError(
                {'start': ('Start date is after repeat end date')})

    def get_absolute_url(self):
        """
         Returns the url to access a particular instance of EventTemplate.
         """
        return reverse('event-detail', args=[str(self.id)])

    def __str__(self):
        """
        String for representing the EventTemplate object (in Admin site etc.)
        """
        return self.name

    def create_eventinstance(self, start, end=None):
        if end is None:
            end = start + (self.end - self.start)
        #logger.warning("create_eventinstacreate_eventinstancence:start: %s", start)
        #logger.warning("create_eventinstance:end: %s", end)

        period = SchedulingPeriod.objects.filter(
            start__lte=start, end__gte=end)

        if period.count() == 1:
            period = period[0]
        else:
            period = None

        event = EventInstance(id=None, event=self, start=start,
                              end=end, status=0, period=period)

        return event

    def get_event_list(self, from_date, until_date):
        #logger.warning("Event:get_event_list called")
        #logger.warning("name: %s", self.name)
        # logger.warning("Event:get_event_list:from_date: %s", from_date)
        # logger.warning("Event:get_event_list:until_date: %s", until_date)
        #logger.warning("Event:get_event_list:self.start: %s", self.start.tzinfo)
        #logger.warning("Event:get_event_list:self.end: %s", self.end)

        # Create list of Events
        event_list = []
        # Check if this Event is meant to be started yet
        if self.start > until_date:
            # logger.warning("Event:get_event_list:Start parameter not within range")
            return event_list
        # If there is a rule for repetition
        if self.rule is not None:
            # logger.warning("Event:get_event_list:Rule for repetition found")
            # Calculate the duration of events
            event_duration = self.end - self.start

            # Check if Event starts later than given date
            # Set the start to the Events start if later
            if self.start > from_date:
                from_date = self.start
            else:
                # Set the date to the given startdate but keep the same time if earlier
                # As we are combining a current date with a time that might have
                # been set in with another DST set up, we have to convert the time
                # to local at the past timezone and convert it back to UTC with
                # timezone for current local.
                tz = pytz.timezone(settings.TIME_ZONE)
                from_date = datetime.datetime.combine(from_date.date(), self.start.astimezone(
                    tz).time()).astimezone(pytz.timezone("utc"))
                #from_date = datetime.datetime.combine(from_date.date(), self.start.time(), self.start.tzinfo)
            # Check if there is a date set to stop generating events, use that instead of given date if it is before given date
            if self.repeat_end and self.repeat_end < until_date:
                until_date = self.repeat_end
            #logger.warning("Event:get_event_list:from_date: %s", from_date)
            #logger.warning("Event:get_event_list:until_date: %s", until_date)
            start_list = self.rule.get_events(from_date, until_date)

            for startdate in start_list:
                event_instance = self.create_eventinstance(
                    startdate, startdate+event_duration)
                if event_instance.period:
                    event_list.append(event_instance)
        # If this is a single Event
        else:
            # logger.warning("Event:get_event_list:No rule for repetition found, adding single event")
            #logger.warning("get_event_list:single: %s", self.start)
            event_instance = self.create_eventinstance(self.start)
            if event_instance.period:
                event_list.append(event_instance)

        return event_list

    """
    Retrieves events between two dates
    If replace=True it replaces generated events with actual objects from DB
    If replace=False it removes generated events where there is an actual object in DB but does not replace it.
    """

    def get_events(self, from_date, until_date, replace=True):
        #logger.warning("Event:get_events called")
        #logger.warning("Event:get_events:from_date: %s", from_date)
        #logger.warning("Event:get_events:until_date: %s", until_date)

        # Get all actual EventInstances created from this Event
        event_instances = self.eventinstance_set.all()
        ## logger.warning("event_instances is: %s", event_instances)
        # Create an EventReplacer object containing these EventInstances
        event_replacer = EventReplacer(event_instances)
        # Generate EventInstances that this Event can create between dates
        generated_events = self.get_event_list(from_date, until_date)
        ## logger.warning("generated_events is: %s", generated_events)
        # Create list to hold a combination of generated and actual EventInstance
        final_eventlist = []
        # Go through all generated EventInstances
        for event_instance in generated_events:
            # Check if a generated EventInstance coincides with an actual EventInstance and add it
            if event_replacer.has_eventinstance(event_instance):
                if replace:
                    # logger.warning("Replacing with real event")
                    final_eventlist.append(
                        event_replacer.get_eventinstance(event_instance))
                # else:
                    # logger.warning("Skipping event since replace=False")
            # If it doesn't, add the generated EventInstance
            else:
                # logger.warning("Adding generated event with id:"+str(event_instance.id))
                final_eventlist.append(event_instance)

        # Finally we add the straggling EventInstances that coincides with these dates unless replace is false in which case we only want generated events
        if replace:
            final_eventlist += event_replacer.get_straggling_eventinstances(
                from_date, until_date)
        # Return list
        return final_eventlist

        """
        skapa funktion för att hämta events denna genererar
        skapa funktion för att skapa en "fejkad" EventInstance-objekt
        ta bort riktiga EventInstance från listan (jämför med länkade instances)
            Om instansen finns ska den tas bort
                om den riktiga dessutom är bokningsbar skall den in istället.
        returnera lista
        kanske något för att sortera?
        kanske ska det finnas någon funktion för att skapa en "ta denna" länk
        :return:
        """


class EventReplacer(object):
    """
    Used to replace generated Events with actual EventInstances
    """
    # Create a dict to keep track of actual EventInstances when creating an instance of this class

    def __init__(self, event_instances):
        # Instances orphaned before Event became PROTECT have no event to key
        # on, so they cannot match a generated occurrence. Skip them here
        # rather than raising on every page that builds an event list.
        lookup = [((event_instance.event_id, event_instance.start, event_instance.end),
                   event_instance) for event_instance in event_instances
                  if event_instance.event_id is not None]
        self.lookup = dict(lookup)

    # Return an actual EventInstance that matches "event_instance" and remove it since it has been matched
    def get_eventinstance(self, event_instance):
        return self.lookup.pop((event_instance.event.id, event_instance.start, event_instance.end), event_instance)

    # Check if an EventInstance exists in the list or raise an error if something goes wrong
    def has_eventinstance(self, event_instance):
        try:
            return (event_instance.event.id, event_instance.start, event_instance.end) in self.lookup
        except TypeError:
            if not self.lookup:
                return False
            else:
                raise TypeError(
                    'A problem with checking if an actual EventInstace exists occured!')

    # Return stragglers that start between the given dates. This can happen if you schedule events and then change the rule generating events
    def get_straggling_eventinstances(self, from_date, until_date):
        return [event_instance for _, event_instance in list(self.lookup.items()) if (event_instance.start < until_date and event_instance.start >= from_date and not event_instance.status == 2)]


class EventManager(models.Manager):
    def get_instances(self, fromTime, untilTime):
        return self.eventinstance_set.filter(start__gte=fromTime, start__lte=untilTime)


class EventInstance(models.Model):
    """
    Each actually scheduled time in the calendar corresponds with this.
    """

    # Data
    STATUS = (
        (-1, 'Ombokning krävs'),
        (0, 'Ledigt'),
        (1, 'Bokat'),
        (2, 'Inställt'),
    )

    # Fields
    id = models.UUIDField(primary_key=True, default=uuid.uuid4,
                          editable=False, help_text="Unique ID for this bookinginstance")
    google_calendar_booking_id = models.CharField(
        max_length=300, help_text="Unique ID from google after instance is created", null=True, blank=True)
    host = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True)
    participants = models.ManyToManyField(
        'accounts.User', related_name="participants", related_query_name="participant", blank=True)
    event = models.ForeignKey('Event', on_delete=models.PROTECT, null=True)
    start = models.DateTimeField(help_text="Start of event")
    end = models.DateTimeField(help_text="End of event")
    status = models.IntegerField(
        default=0, choices=STATUS, help_text="Instance status")
    period = models.ForeignKey(
        'SchedulingPeriod', on_delete=models.SET_NULL, null=True, blank=True)
    unique_title = models.CharField(max_length=100, default="", blank=True,
                                    help_text="Enter title to be inserted before the header when cancelling, leave blank to use default: {}".format(settings.CANCELLED_TITLE))
    unique_description = models.CharField(max_length=400, default="", blank=True,
                                          help_text="Enter a description for a cancelled event, leave blank to use default: {}".format(settings.CANCELLED_DESCRIPTION))

    @property
    def statusText(self):
        return EventInstance.STATUS[self.status + 1][1]

    @property
    def title(self):
        if self.template is None:
            return MISSING_RELATION_TITLE
        return self.template.title

    @property
    def header(self):
        if self.template is None:
            return None
        return self.template.header

    @property
    def body(self):
        if self.template is None:
            return None
        return self.template.body

    @property
    def max_num_participants(self):
        if self.template is None:
            return 0
        return self.template.num_participants

    @property
    def template(self):
        # Both links are PROTECT now, but rows orphaned by the old SET_NULL
        # behaviour still exist in databases created before that change.
        if self.event is None:
            return None
        return self.event.template

    # Metadata
    class Meta:
        ordering = ["start", "end"]
        unique_together = ('event', 'start', 'end')
        constraints = [
            models.CheckConstraint(
                condition=Q(start__lt=F('end')),
                name='check_instance_start_before_end'
            ),
        ]

    # Methods
    def clean(self):
        # Only validate future requirement on create not on edit.
        if self.pk is None and self.start <= timezone.now():
            raise ValidationError(
                {'start': ('Start date is not in the future')})
        if self.start > self.end:
            raise ValidationError({'start': ('Start date is after end date')})

    def get_absolute_url(self):
        """
         Returns the url to access a particular instance of EventInstance.
         """
        return reverse('eventinstance-detail', args=[str(self.id)])

    # Statuses that have a Google event. A slot with a host (1) is open; one
    # cancelled by staff (2) is shown as "Inställt: ..." so visitors know.
    # A slot without a host (-1 needs rebooking, 0 free) is not open, so it
    # must not be in the public calendar at all.
    PUBLISHED_STATUSES = (1, 2)

    def save(self, *args, **kwargs):
        logger.warning('EventInstance save called')
        template = self.template
        if template is None:
            # Orphaned row: there is no template to tell us how to render a
            # calendar entry. Let the save through so it can still be
            # repaired or deleted from the admin pages.
            logger.warning(
                "No template for this instance, skipping calendar synchronisation")
        elif not template.synchronize:
            logger.warning("Template does not synchronize, skipping calendar")
        elif self.status not in self.PUBLISHED_STATUSES:
            if self.google_calendar_booking_id is not None:
                logger.warning("Slot has no host, removing calendar entry with ID: " +
                               str(self.google_calendar_booking_id))
                if not template.deleteEventEntry(self.google_calendar_booking_id):
                    raise ValueError('Could not remove the calendar entry')
                self.google_calendar_booking_id = None
        elif self.google_calendar_booking_id is not None:
            logger.warning("Updating calendar entry with ID: " +
                           str(self.google_calendar_booking_id))

            if not template.updateEventEntry(self.google_calendar_booking_id, self.host, self.start, self.end, self.status):
                raise ValueError('Could not update calendar')
        else:
            logger.warning("Creating new calendar entry")
            calendar_id = template.createEventEntry(
                self.host, self.start, self.end, self.status)
            if isinstance(calendar_id, bool):
                logger.warning(
                    'No non-boolean ID was returned, not saving value as calendar id')
            else:
                logger.warning('Inserting new calendar ID into object')
                self.google_calendar_booking_id = calendar_id

        logger.warning("Calling super().save()")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Used to update the event instead, which left every deleted slot in
        # Google Calendar (bug audit #4).
        template = self.template
        if self.google_calendar_booking_id is not None and template is not None and template.synchronize:
            if not template.deleteEventEntry(self.google_calendar_booking_id):
                raise ValueError('Could not remove the calendar entry')
        super().delete(*args, **kwargs)

    def display_host(self):
        # host is null on every unbooked instance, and this feeds the admin
        # changelist, where one free slot used to break the whole page.
        # Returning None lets the admin render its own empty-value marker.
        if self.host is None:
            return None
        return self.host.email
    display_host.short_description = "Host"

    def as_dict(self):
        if self.id == None:
            return {
                'google_calendar_booking_id': self.google_calendar_booking_id,
                'host': str(self.host),
                'event': self.event_id,
                'start': self.start,
                'end': self.end,
                'status': self.status,
                'period': self.period_id
            }
        else:
            return {
                'id': str(self.id),
                'google_calendar_booking_id': self.google_calendar_booking_id,
                'host': str(self.host),
                'event': self.event_id,
                'start': self.start.strftime('%Y-%m-%d %H:%M:%S'),
                'end': self.end.strftime('%Y-%m-%d %H:%M:%S'),
                'status': self.status,
                'period': self.period_id
            }

    def can_take(self, user):
        if self.status > 0:
            return self.host == user
        else:
            return True

    def __str__(self):
        """
        String for representing the EventTemplate object (in Admin site etc.)
        """
        if self.host and self.period:
            return self.title+"("+self.host.slackId+", "+self.period.__str__()+")"
        else:
            return self.title


class SchedulingRule(models.Model):
    # Data
    freqs = (
        ("YEARLY", "Yearly"),
        ("MONTHLY", "Monthly"),
        ("WEEKLY", "Weekly"),
        ("DAILY", "Daily"),
    )

    _week_days = {
        'MO': MO,
        'TU': TU,
        'WE': WE,
        'TH': TH,
        'FR': FR,
        'SA': SA,
        'SU': SU
    }

    # Fields
    name = models.CharField(max_length=50)
    description = models.TextField()
    frequency = models.CharField("frequency", choices=freqs, max_length=10)
    params = models.TextField("params", blank=True)
    use_exclusions = models.BooleanField(default=True,
                                         help_text="If active, this rule for generating dates will ignore excluded dates generated dates falling on those dates will not be added.")

    # Functions
    def get_absolute_url(self):
        """
         Returns the url to access a particular instance of EventTemplate.
         """
        return reverse('rule-detail', args=[str(self.id)])

    def __str__(self):
        """
        String for representing the EventTemplate object (in Admin site etc.)
        """
        return self.name

    def rrule_frequency(self):
        compatibility_dict = {
            'DAILY': DAILY,
            'MONTHLY': MONTHLY,
            'WEEKLY': WEEKLY,
            'YEARLY': YEARLY
        }
        return compatibility_dict[self.frequency]

    def _weekday_or_number(self, param):
        '''
        Receives a rrule parameter value, returns a upper case version
        of the value if its a weekday or an integer if its a number
        '''
        try:
            return int(param)
        except (TypeError, ValueError):
            uparam = str(param).upper()
            if uparam in SchedulingRule._week_days:
                return SchedulingRule._week_days[uparam]

    def get_params(self, extra_params=""):
        """
        >>> rule = Rule(params = "count:1;bysecond:1;byminute:1,2,4,5")
        >>> rule.get_params()
        {'count': 1, 'byminute': [1, 2, 4, 5], 'bysecond': 1}
        """

        total_params = self.params + ";" + extra_params

        params = total_params.split(';')
        param_dict = []
        for param in params:
            param = param.split(':')
            if len(param) != 2:
                continue

            param = (
                str(param[0]).lower(),
                [x for x in
                 [self._weekday_or_number(v) for v in param[1].split(',')]
                 if x is not None],
            )

            if len(param[1]) == 1:
                param_value = self._weekday_or_number(param[1][0])
                param = (param[0], param_value)
            param_dict.append(param)
        return dict(param_dict)

    def get_events(self, dtstart=None, until=None, extra_params=""):
        #logger.warning("Rule:get_events called")
        #logger.warning("dtstart: %s", dtstart)
        #logger.warning("until: %s", until)
        #logger.warning("name: %s", self.name)
        params = self.get_params(extra_params=extra_params)
        if (('until' in params) and until):
            del params['until']
        if((until is None) and not('count' in params) and not('until' in params)):
            params['count'] = 10
        if(dtstart is None):
            dtstart = datetime.datetime.now()
        # if not 'tzid' in params:
        #    params['tzid'] = settings.TIME_ZONE
        frequency = self.rrule_frequency()
        # logger.warning("params: %s", params)
        # Move time to timezone currently used and make time objects unaware to avoid any problems with rrule
        if until:
            until = until.astimezone(pytz.timezone(settings.TIME_ZONE))
            until = until.replace(tzinfo=None)
        dtstart = dtstart.astimezone(pytz.timezone(settings.TIME_ZONE))
        dtstart = dtstart.replace(tzinfo=None)

        # Changing from rrule to rruleset
        #events = rrule(frequency, dtstart=dtstart, until=until, **params)
        rules = rruleset()
        rules.rrule(rrule(frequency, dtstart=dtstart, until=until, **params))

        if self.use_exclusions:
            # Here we should add all excluded dates
            if until:
                rule_exclusions = SchedulingRuleExclusion.objects.filter(
                    excluded_date__range=(dtstart.date(), until.date()))
            else:
                rule_exclusions = SchedulingRuleExclusion.objects.filter(
                    excluded_date__gte=dtstart.date())
            for rule_exclusion in rule_exclusions:
                excluded_date = rule_exclusion.excluded_date
                exclusion = dtstart.replace(
                    year=excluded_date.year, month=excluded_date.month, day=excluded_date.day)
                rules.exdate(exclusion)

        #events = rules.between(dtstart, until)
        events = rules

        tz = pytz.timezone(settings.TIME_ZONE)
        # logger.warning("get_events:tz"+tz)
        updatedEvents = []
        for event in events:
            updatedEvents.append(tz.normalize(
                tz.localize(event)).astimezone(pytz.utc))

        # for event in updatedEvents:
        #    logger.warning("get_events: %s", event)
        return updatedEvents


class SchedulingPeriod(models.Model):

    # Fields
    start = models.DateField(help_text="Start of period", db_index=True)
    end = models.DateField(help_text="End of period", db_index=True)
    num_required_events = models.IntegerField(
        default=6, help_text="Number of events a host should have in this period")
    participant_key_string = ParticipantKeyField(
        default="", help_text="A string representing the events a host is required to be a participant to in this period", max_length=10)
    hosts = models.ManyToManyField(
        accounts.models.User, related_name="periods", related_query_name="period", blank=True)

    class Meta:
        ordering = ('-start', '-end')
        constraints = [
            models.CheckConstraint(
                condition=Q(start__lt=F('end')),
                name='check_period_start_before_end'
            ),
        ]

    @property
    def name(self):
        return self.start.strftime('%Y %b') + "-" + self.end.strftime('%b')

    @property
    def num_required_participant(self):
        return len(self.participant_key_string)

    # Functions
    def clean(self):
        if self.start > self.end:
            raise ValidationError({'start': ('Start date is after end date')})

    def get_absolute_url(self):
        """
         Returns the url to access a particular instance of SchedulingPeriod.
         """
        return reverse('period-detail', args=[str(self.id)])

    def get_participant_key_list(self):
        userParticipantList = accounts.models.User.objects.all().order_by('eventinstance__participants__slackId', 'eventinstance__period', 'eventinstance__event__template__count_key').annotate(
            participantName=Max('eventinstance__participants__slackId'), keyChar=Max('eventinstance__event__template__count_key', filter=Q(eventinstance__period=self.id)))

        currentUser = None
        resultList = {}
        for userParticipant in userParticipantList:
            #logger.warning("get_participant_key_list: %s", userParticipant.participantName)
            if (currentUser == None or currentUser != userParticipant.participantName) and userParticipant.participantName != None:
                currentUser = userParticipant.participantName
                resultList[currentUser] = ""
            if currentUser != None and userParticipant.keyChar != None:
                resultList[currentUser] = resultList[currentUser] + \
                    userParticipant.keyChar

        return resultList

    def get_period_for_date(date):
        """
        Get the period that covers the provided date, will return the period that starts first and covers the date.
        """
        return SchedulingPeriod.objects.all().filter(start__lte=date, end__gte=date).order_by("start").first()

    def get_current_period():
        """
        Get the period that covers the current date, will return the period that starts first and covers the date.
        """
        return SchedulingPeriod.get_period_for_date(timezone.now())

    def get_previous_period(self):
        """
        Get the period the last period that starts before this period.
        """
        return SchedulingPeriod.objects.all().filter(start__lt=self.start).order_by("start").last()

    def get_length(self):
        return (self.end - self.start).days

    def get_elapsed(self):
        currentDate = timezone.now().date()
        if currentDate < self.start:
            # Period has not begun yet so zero days have elapsed.
            return 0
        elif currentDate > self.end:
            # Period has ended so all days have elepased.
            return self.get_length()
        else:
            return (currentDate - self.start).days

    def get_progress(self):
        return (self.get_elapsed() / self.get_length()) * 100

    def get_host_count_list(self):
        userHostList = accounts.models.User.objects.all().order_by('slackId', 'eventinstance__period').annotate(
            host_count=Count('slackId', filter=Q(eventinstance__status=1, eventinstance__period=self.id))).order_by('host_count')

        return userHostList

    def get_required_total_number_of_required_events(self):
        return self.num_required_events + len(self.participant_key_string)

    def special_seats(self):
        """Seats on special passes in this period that count as hosted slots."""
        return EntryHost.objects.filter(
            entry__kind='special', entry__special_type__counts_as_session=True,
            entry__start__date__gte=self.start, entry__start__date__lte=self.end)

    def get_host_stats(self, filterHost):
        currentDate = timezone.now().date()
        required_events = self.get_required_total_number_of_required_events()

        participant_events_booked = filterHost.participants.all().filter(
            period=self.id, start__gte=currentDate)
        participant_events_done = filterHost.participants.all().filter(
            period=self.id, start__lt=currentDate)

        values = {"host_booked": EventInstance.objects.all().filter(period=self.id, host=filterHost.id, start__gte=currentDate).count(
        ), "participant_booked": participant_events_booked.count(),
            "host_done": EventInstance.objects.all().filter(period=self.id, host=filterHost.id, start__lt=currentDate).count(
        ), "participant_done": participant_events_done.count()}
        # A seat on a special pass counts like a hosted slot.
        seats = self.special_seats().filter(user=filterHost)
        values["host_booked"] += seats.filter(entry__start__date__gte=currentDate).count()
        values["host_done"] += seats.filter(entry__start__date__lt=currentDate).count()

        values["participant_booked_key"] = ""
        for participant_event in participant_events_booked:
            values["participant_booked_key"] += participant_event.template.count_key

        values["participant_done_key"] = ""
        for participant_event in participant_events_done:
            values["participant_done_key"] += participant_event.template.count_key

        values["total_booked"] = values["host_booked"] + \
            values["participant_booked"]
        values["total_done"] = values["host_done"] + \
            values["participant_done"]

        values["max_commited_required"] = max(
            values["total_done"] + values["total_booked"], required_events)

        values["participant_commited"] = values["participant_done"] + \
            values["participant_booked"]
        values["host_commited"] = values["host_done"] + values["host_booked"]

        values["extra_participant"] = max(
            0, values["participant_commited"] - self.num_required_participant)
        values["extra_host"] = max(
            0, values["host_commited"] - self.num_required_events)

        values["total_extra"] = max(
            0, values["total_done"] + values["total_booked"] - required_events)
        values["total_extra_percentage"] = (
            values["total_extra"] / values["max_commited_required"]) * 100

        values["host_booked_percentage"] = (
            values["host_booked"] / values["max_commited_required"]) * 100
        values["participant_booked_percentage"] = (
            values["participant_booked"] / values["max_commited_required"]) * 100
        values["total_booked_percentage"] = (
            values["total_booked"] / values["max_commited_required"]) * 100

        values["host_done_percentage"] = (
            values["host_done"] / values["max_commited_required"]) * 100
        values["participant_done_percentage"] = (
            values["participant_done"] / values["max_commited_required"]) * 100
        values["total_done_percentage"] = (
            values["total_done"] / values["max_commited_required"]) * 100

        return values

    def get_host_count_key_list(self):
        count_list = self.get_host_count_list()
        key_list = self.get_participant_key_list()
        resultList = {}
        # Seats on special passes count like hosted slots.
        seats = dict(self.special_seats().values('user__slackId').annotate(n=Count('id'))
                     .values_list('user__slackId', 'n'))

        for host in count_list:
            resultList[host.slackId] = str(host.host_count + seats.get(host.slackId, 0))
            if host.slackId in key_list:
                resultList[host.slackId] = resultList[host.slackId] + \
                    " " + key_list[host.slackId]

        return resultList

    def get_all_host_count_key_lists(only_hosts_in_last_period=False):
        periodList = SchedulingPeriod.objects.all().order_by('-start')

        resultList = {}

        for period in periodList:
            for user, count_key in period.get_host_count_key_list().items():
                if not only_hosts_in_last_period or user in SchedulingPeriod.objects.latest('end').hosts.all().values_list('slackId', flat=True):
                    if user not in resultList:
                        resultList[user] = {}
                    resultList[user][period] = count_key

        return resultList

    @property
    def active_unassigned_hosts(self):
        """
        Returns a list of all hosts that have been active this period. Either as
        a host or as a participant.
        """

        hosts = accounts.models.User.objects.filter((Q(eventinstance__period=self.id) | Q(
            participant__period=self.id)) & ~Q(id__in=self.hosts.all())).order_by().distinct()

        return hosts

    def __str__(self):
        """
        String for representing the EventTemplate object (in Admin site etc.)
        """
        return self.name


class SchedulingRuleExclusion(models.Model):
    # Fields
    excluded_date = models.DateField(
        help_text="Date to exclude", db_index=True)
    description = models.TextField()

    @property
    def name(self):
        return '{} ({})'.format(self.excluded_date.strftime("%Y-%m-%d"), self.description)

    class Meta:
        ordering = ["excluded_date"]

    # Functions
    def get_absolute_url(self):
        """
         Returns the url to access a particular instance of SchedulingRuleExclusion.
         """
        return reverse('ruleexclusion-detail', args=[str(self.id)])

    def __str__(self):
        """
        String for representing the SchedulingRuleExclusion object (in Admin site etc.)
        """
        return self.name


class BookingDependency(models.Model):
    """
    A slot of one of `dependent_events` can only be booked when a slot of
    one of `required_events` already has a host on the same local date, on
    the weekdays listed. Several rules on the same slot must all be met.

    Example: on weekdays, "Vardag 15-18" requires "Vardag 18-21".
    """
    WEEKDAY_CHOICES = [(0, "Måndag"), (1, "Tisdag"), (2, "Onsdag"), (3, "Torsdag"),
                       (4, "Fredag"), (5, "Lördag"), (6, "Söndag")]

    name = models.CharField(
        max_length=100, help_text="A short name staff will recognise")
    description = models.TextField(
        blank=True, help_text="Why this rule exists (optional)")
    is_active = models.BooleanField(
        default=True, help_text="Inactive rules are kept but not enforced")
    dependent_events = models.ManyToManyField(
        'Event', related_name='booking_dependencies',
        help_text="These slots...")
    required_events = models.ManyToManyField(
        'Event', related_name='required_by_dependencies',
        help_text="...can only be booked when one of these has a host the same day")
    weekdays = models.CharField(
        max_length=13, default="0,1,2,3,4",
        help_text="Weekdays the rule applies on, 0 = Monday")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "booking dependencies"

    def weekday_set(self):
        return {int(day) for day in self.weekdays.split(',') if day.strip().isdigit()}

    def weekday_names(self):
        names = dict(self.WEEKDAY_CHOICES)
        return ", ".join(names[day] for day in sorted(self.weekday_set()))
    weekday_names.short_description = "Weekdays"

    def __str__(self):
        return self.name


class HostReminder(models.Model):
    """A booking reminder sent to a host, by staff or by the scheduled job."""
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='booking_reminders')
    period = models.ForeignKey('SchedulingPeriod', on_delete=models.CASCADE, related_name='reminders')
    sent_at = models.DateTimeField(auto_now_add=True)
    sent_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='sent_booking_reminders',
                                help_text="Empty when sent by the scheduled job")

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return '{} {} ({})'.format(self.user.slackId, self.period, self.sent_at.date())


class SpecialPassType(models.Model):
    """
    A standard kind of one-off session staff put in the calendar, such as
    "Introkväll": its default times and description, how many hosts it
    needs, and how it relates to the regular slots.
    """
    name = models.CharField(max_length=100, help_text="Also the default title of each pass")
    description = models.TextField(blank=True, help_text="Default description of each pass")
    start_time = models.TimeField(help_text="Default start")
    end_time = models.TimeField(help_text="Default end")
    hosts_needed = models.PositiveSmallIntegerField(default=2, help_text="Default number of hosts")
    counts_as_session = models.BooleanField(
        default=True, help_text="A seat as host counts as a hosted slot (värdpass) in the period")
    blocks_slots = models.BooleanField(
        default=True, help_text="Regular slots it overlaps cannot be booked")
    is_active = models.BooleanField(default=True, help_text="Inactive types are not offered for new passes")

    class Meta:
        ordering = ['name']

    def get_absolute_url(self):
        return reverse('special-passes')

    def __str__(self):
        return self.name


class EntryHost(models.Model):
    """One host's seat on a special pass."""
    entry = models.ForeignKey('CalendarEntry', on_delete=models.CASCADE, related_name='seats')
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='special_seats')
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['joined_at', 'pk']
        constraints = [models.UniqueConstraint(fields=['entry', 'user'], name='one_seat_per_host')]


class CalendarEntry(models.Model):
    """
    An item in the calendar that is not a regular slot: a course, a board
    meeting, the space being closed... Shown in the week view beside the
    slots, in a colour per kind. Informational, except "Stängt", which
    blocks the slots it overlaps, and "Specialpass", a one-off session of a
    SpecialPassType that hosts sign up for (hosts_needed seats).
    """
    KINDS = [
        ('activity', 'Aktivitet'),
        ('course', 'Kurs'),
        ('meeting', 'Möte'),
        ('closed', 'Stängt'),
        ('special', 'Specialpass'),
        ('other', 'Övrigt'),
    ]

    title = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=20, choices=KINDS, default='activity')
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(db_index=True)
    all_day = models.BooleanField(
        default=False, help_text="Shown as a banner on each day; start and end are then midnights")
    created_by = models.ForeignKey(
        'accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='calendar_entries')
    # Google Calendar sync (scheduler/entry_sync.py). No calendar: only shown here.
    calendar = models.ForeignKey(
        'SchedulingCalendar', on_delete=models.PROTECT, null=True, blank=True,
        related_name='entries', help_text="Google calendar to put this entry in")
    google_event_id = models.CharField(max_length=300, null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.TextField(blank=True)
    # Special passes only: what kind, how many hosts, who has a seat.
    special_type = models.ForeignKey(
        'SpecialPassType', on_delete=models.PROTECT, null=True, blank=True, related_name='passes')
    hosts_needed = models.PositiveSmallIntegerField(default=0)
    hosts = models.ManyToManyField('accounts.User', through='EntryHost', blank=True,
                                   related_name='special_passes')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start', 'end']
        verbose_name_plural = 'calendar entries'
        constraints = [
            models.CheckConstraint(condition=Q(start__lt=F('end')),
                                   name='check_entry_start_before_end'),
        ]

    def clean(self):
        if self.start and self.end and self.start >= self.end:
            raise ValidationError({'end': 'Slutet måste vara efter starten'})

    def can_edit(self, user):
        return user.is_authenticated and user.is_staff

    def local_days(self):
        """Every local date this entry touches (end is exclusive)."""
        first = timezone.localtime(self.start).date()
        last = timezone.localtime(self.end - datetime.timedelta(microseconds=1)).date()
        return [first + datetime.timedelta(days=n) for n in range((last - first).days + 1)]

    @property
    def is_special(self):
        return self.kind == 'special'

    @property
    def blocks_slots(self):
        """Whether regular slots overlapping it cannot be booked."""
        if self.kind == 'closed':
            return True
        return self.is_special and self.special_type is not None and self.special_type.blocks_slots

    @property
    def counts_as_session(self):
        return self.is_special and self.special_type is not None and self.special_type.counts_as_session

    def seat_hosts(self):
        """Hosts with a seat, in the order they joined."""
        return [seat.user for seat in self.seats.select_related('user')]

    @property
    def seats_left(self):
        return max(0, self.hosts_needed - self.seats.count())

    def get_absolute_url(self):
        return reverse('entry-detail', args=[self.pk])

    def __str__(self):
        return self.title


class CalendarCheck(models.Model):
    """
    One comparison of a Google calendar with the schedule
    (scheduler/calendar_check.py): what differed, or why it could not run.
    """
    calendar = models.ForeignKey('SchedulingCalendar', on_delete=models.CASCADE, related_name='checks')
    ran_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ran_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
                               help_text="Empty when run by the scheduled job")
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    differences = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-ran_at']

    @property
    def ok(self):
        return not self.error and not self.differences

    def signature(self):
        """What the differences are, ignoring when they were found; for "has anything changed?"."""
        return sorted((d['kind'], d['key']) for d in self.differences)

    def __str__(self):
        return '{} {:%Y-%m-%d %H:%M}'.format(self.calendar, self.ran_at)


class BookingChange(models.Model):
    """
    One change to who hosts a slot (instance) or holds a seat on a special
    pass (entry), by the host or by staff, kept for staff to look back on.
    """
    ACTIONS = [
        ('booked', 'Bokade'),
        ('cancelled', 'Bokade av'),
        ('staff_booked', 'Bokad av styrelsen'),
        ('staff_reassigned', 'Bytt värd av styrelsen'),
        ('staff_removed', 'Borttagen av styrelsen'),
    ]
    instance = models.ForeignKey('EventInstance', on_delete=models.CASCADE, related_name='changes',
                                 null=True, blank=True)
    entry = models.ForeignKey('CalendarEntry', on_delete=models.CASCADE, related_name='changes',
                              null=True, blank=True, help_text="For a seat on a special pass")
    action = models.CharField(max_length=20, choices=ACTIONS)
    host = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='+', help_text="The host the change is about")
    previous_host = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='+', help_text="For a change of host: who had it")
    by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True,
                           related_name='+', help_text="Who made the change")
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    note = models.TextField(blank=True, help_text="Reason given by staff")

    class Meta:
        ordering = ['-at']

    @classmethod
    def log(cls, instance, action, by, host=None, previous_host=None, note='', entry=None):
        if host is None and instance is not None:
            host = instance.host
        return cls.objects.create(instance=instance, entry=entry, action=action, by=by, host=host,
                                  previous_host=previous_host, note=note)

    def __str__(self):
        return '{} {} ({:%Y-%m-%d %H:%M})'.format(self.get_action_display(), self.instance or self.entry, self.at)
