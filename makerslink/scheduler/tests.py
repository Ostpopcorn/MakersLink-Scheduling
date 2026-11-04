import datetime
from unittest import mock

from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone

from accounts.models import User

from .models import (Event, EventInstance, EventTemplate, MISSING_RELATION_TITLE,
                     SchedulingCalendar, SchedulingPeriod)


def make_staff(email="staff@example.com", slack="staff"):
    user = User.objects.create(email=email, slackId=slack, is_active=True,
                               is_staff=True, is_registration_complete=True)
    user.set_password("password")
    user.save()
    return user


class ProtectedRelationTests(TestCase):
    """
    Deleting a calendar or a template used to SET_NULL the rows that depend on
    it, which left EventInstance unable to render its own title and took out
    every page that listed instances. These relations are PROTECT now.
    """

    def setUp(self):
        self.calendar = SchedulingCalendar.objects.create(
            name="cal", google_calendar_id="g", service_account_username="s",
            timezone="Europe/Stockholm")
        self.template = EventTemplate.objects.create(
            name="t", title="Open", num_participants=2,
            calendar=self.calendar, synchronize=False)
        today = timezone.now().date()
        self.period = SchedulingPeriod.objects.create(
            start=today - datetime.timedelta(days=30),
            end=today + datetime.timedelta(days=120))
        self.event = Event.objects.create(
            name="e", template=self.template,
            start=timezone.now() + datetime.timedelta(days=1),
            end=timezone.now() + datetime.timedelta(days=1, hours=3))
        self.instance = EventInstance.objects.create(
            event=self.event, start=self.event.start, end=self.event.end,
            status=0, period=self.period)

    def test_deleting_a_used_template_is_refused(self):
        with self.assertRaises(ProtectedError):
            self.template.delete()
        self.event.refresh_from_db()
        self.assertEqual(self.event.template, self.template)

    def test_deleting_a_used_calendar_is_refused(self):
        with self.assertRaises(ProtectedError):
            self.calendar.delete()
        self.template.refresh_from_db()
        self.assertEqual(self.template.calendar, self.calendar)

    def test_deleting_a_used_event_is_refused(self):
        with self.assertRaises(ProtectedError):
            self.event.delete()
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.event, self.event)

    def test_unused_template_can_still_be_deleted(self):
        spare = EventTemplate.objects.create(
            name="spare", title="Spare", calendar=self.calendar, synchronize=False)
        spare.delete()
        self.assertFalse(EventTemplate.objects.filter(name="spare").exists())


class ProtectedDeleteViewTests(TestCase):
    """
    The database refusal must reach the user as an explanation, not a 500.
    """

    def setUp(self):
        make_staff()
        self.client.login(email="staff@example.com", password="password")
        self.calendar = SchedulingCalendar.objects.create(
            name="Huvudkalender", google_calendar_id="g",
            service_account_username="s", timezone="Europe/Stockholm")
        self.template = EventTemplate.objects.create(
            name="Kvallsoppet", title="Open", calendar=self.calendar,
            synchronize=False)
        self.event = Event.objects.create(
            name="Tisdagskvall", template=self.template,
            start=timezone.now() + datetime.timedelta(days=1),
            end=timezone.now() + datetime.timedelta(days=1, hours=3))

    def test_deleting_used_template_renders_the_blockers(self):
        response = self.client.post(
            '/scheduler/template/{}/delete'.format(self.template.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cannot delete this template")
        self.assertContains(response, "Tisdagskvall")
        self.assertEqual(response.context['protected_count'], 1)
        self.assertTrue(EventTemplate.objects.filter(pk=self.template.pk).exists())

    def test_deleting_used_calendar_renders_the_blockers(self):
        response = self.client.post(
            '/scheduler/calendar/{}/delete'.format(self.calendar.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cannot delete this calendar")
        self.assertContains(response, "Kvallsoppet")
        self.assertTrue(SchedulingCalendar.objects.filter(pk=self.calendar.pk).exists())

    def test_deleting_an_unused_template_still_works(self):
        spare = EventTemplate.objects.create(
            name="Oanvand", title="Spare", calendar=self.calendar, synchronize=False)
        response = self.client.post('/scheduler/template/{}/delete'.format(spare.pk))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(EventTemplate.objects.filter(pk=spare.pk).exists())


class SynchronizeWithoutCalendarTests(TestCase):
    """
    synchronize=True with no calendar used to fail as an AttributeError inside
    EventInstance.save(). It is now rejected at validation time, and the
    remaining runtime path names the template instead of the missing attribute.
    """

    def test_clean_rejects_synchronize_without_calendar(self):
        template = EventTemplate(name="t", title="Open", synchronize=True, calendar=None)
        with self.assertRaises(ValidationError) as caught:
            template.clean()
        self.assertIn('calendar', caught.exception.message_dict)

    def test_clean_allows_no_calendar_when_not_synchronizing(self):
        EventTemplate(name="t", title="Open", synchronize=False, calendar=None).clean()

    def test_runtime_error_names_the_template(self):
        template = EventTemplate.objects.create(
            name="Trasig", title="Open", synchronize=True, calendar=None)
        with self.assertRaises(ValueError) as caught:
            template.createEventEntry(None, timezone.now(), timezone.now(), 0)
        self.assertIn("Trasig", str(caught.exception))


class OrphanedRowTests(TestCase):
    """
    Databases written before PROTECT still hold rows whose template or event was
    nulled. Those rows must stay loadable so they can be repaired or deleted.
    """

    def setUp(self):
        make_staff()
        self.client.login(email="staff@example.com", password="password")
        calendar = SchedulingCalendar.objects.create(
            name="cal", google_calendar_id="g", service_account_username="s",
            timezone="Europe/Stockholm")
        template = EventTemplate.objects.create(
            name="t", title="Open", calendar=calendar, synchronize=False)
        today = timezone.now().date()
        period = SchedulingPeriod.objects.create(
            start=today - datetime.timedelta(days=30),
            end=today + datetime.timedelta(days=120))
        event = Event.objects.create(
            name="e", template=template,
            start=timezone.now() + datetime.timedelta(days=1),
            end=timezone.now() + datetime.timedelta(days=1, hours=3))
        self.instance = EventInstance.objects.create(
            event=event, start=event.start, end=event.end, status=1,
            host=make_staff("host@example.com", "host"), period=period)
        # Reproduce the legacy state that SET_NULL used to produce.
        Event.objects.filter(pk=event.pk).update(template=None)
        self.orphan_event = Event.objects.get(pk=event.pk)
        self.instance.refresh_from_db()

    def test_title_falls_back_instead_of_raising(self):
        self.assertEqual(self.instance.title, MISSING_RELATION_TITLE)
        self.assertIsNone(self.instance.template)
        self.assertIsNone(self.instance.header)
        self.assertIsNone(self.instance.body)
        self.assertEqual(self.instance.max_num_participants, 0)
        self.assertEqual(self.orphan_event.max_num_participants, 0)

    def test_str_and_as_dict_do_not_raise(self):
        self.assertIn(MISSING_RELATION_TITLE, str(self.instance))
        self.assertEqual(self.instance.as_dict()['event'], self.orphan_event.pk)

    def test_orphaned_instance_can_still_be_saved_and_deleted(self):
        self.instance.status = 2
        self.instance.save()
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, 2)
        self.instance.delete()
        self.assertFalse(EventInstance.objects.filter(pk=self.instance.pk).exists())

    def test_instance_listing_pages_still_load(self):
        for url in ['/scheduler/eventinstances-admin', '/scheduler/joinEvent']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_admin_changelist_still_loads(self):
        admin = make_staff("su@example.com", "su")
        admin.is_superuser = True
        admin.save()
        self.client.login(email="su@example.com", password="password")
        response = self.client.get('/admin/scheduler/eventinstance/')
        self.assertEqual(response.status_code, 200)


class HostlessInstanceTests(TestCase):
    """
    host is nullable and is None for every unbooked instance, so display_host()
    took out the whole admin changelist as soon as one free slot existed.
    """

    def setUp(self):
        calendar = SchedulingCalendar.objects.create(
            name="cal", google_calendar_id="g", service_account_username="s",
            timezone="Europe/Stockholm")
        template = EventTemplate.objects.create(
            name="t", title="Open", calendar=calendar, synchronize=False)
        today = timezone.now().date()
        self.period = SchedulingPeriod.objects.create(
            start=today - datetime.timedelta(days=30),
            end=today + datetime.timedelta(days=120))
        self.event = Event.objects.create(
            name="e", template=template,
            start=timezone.now() + datetime.timedelta(days=1),
            end=timezone.now() + datetime.timedelta(days=1, hours=3))
        self.free = EventInstance.objects.create(
            event=self.event, start=self.event.start, end=self.event.end,
            status=0, host=None, period=self.period)

    def test_display_host_is_blank_without_a_host(self):
        self.assertIsNone(self.free.display_host())

    def test_display_host_returns_the_email_when_hosted(self):
        self.free.host = make_staff("host@example.com", "host")
        self.assertEqual(self.free.display_host(), "host@example.com")

    def test_admin_changelist_loads_with_a_free_instance(self):
        admin = make_staff("su@example.com", "su")
        admin.is_superuser = True
        admin.save()
        self.client.login(email="su@example.com", password="password")
        response = self.client.get('/admin/scheduler/eventinstance/')
        self.assertEqual(response.status_code, 200)


class HostlessCalendarEntryTests(TestCase):
    """
    An instance with no host still reaches the calendar sync path whenever it is
    saved against a synchronizing template, where the description was built from
    host.slackId without checking that there was a host.
    """

    def setUp(self):
        self.calendar = SchedulingCalendar.objects.create(
            name="cal", google_calendar_id="g", service_account_username="s",
            timezone="Europe/Stockholm")
        self.template = EventTemplate.objects.create(
            name="t", title="Open", calendar=self.calendar, synchronize=True,
            header="Rubrik", body="Brodtext")
        self.start = timezone.now() + datetime.timedelta(days=1)
        self.end = self.start + datetime.timedelta(hours=3)

    def data(self, host, status=0):
        return self.template._createUpdatedEventData(host, self.start, self.end, status)

    def test_description_omits_the_host_line_when_there_is_none(self):
        description = self.data(None)['description']
        self.assertNotIn("Värd", description)
        self.assertNotIn("None", description)
        self.assertEqual(description, "Rubrik\nBrodtext")

    def test_description_is_unchanged_when_there_is_a_host(self):
        host = make_staff("host@example.com", "host")
        self.assertEqual(self.data(host)['description'],
                         "Värd: host\nRubrik\nBrodtext")

    def test_description_is_empty_when_nothing_to_say(self):
        bare = EventTemplate.objects.create(
            name="bare", title="Open", calendar=self.calendar, synchronize=True)
        self.assertEqual(
            bare._createUpdatedEventData(None, self.start, self.end, 0)['description'], "")

    def test_cancelling_an_unhosted_instance_uses_the_cancelled_text(self):
        from django.conf import settings as django_settings
        data = self.data(None, status=2)
        self.assertEqual(data['description'], django_settings.CANCELLED_DESCRIPTION)
        self.assertEqual(data['summary'], django_settings.CANCELLED_TITLE + "Open")

    def test_saving_an_unhosted_instance_syncs_instead_of_crashing(self):
        today = timezone.now().date()
        period = SchedulingPeriod.objects.create(
            start=today - datetime.timedelta(days=30),
            end=today + datetime.timedelta(days=120))
        event = Event.objects.create(name="e", template=self.template,
                                     start=self.start, end=self.end)
        with mock.patch.object(SchedulingCalendar, 'createEvent',
                               return_value="cal-id-1") as created:
            instance = EventInstance.objects.create(
                event=event, start=self.start, end=self.end, status=0,
                host=None, period=period)
        self.assertEqual(instance.google_calendar_booking_id, "cal-id-1")
        self.assertNotIn("Värd", created.call_args.args[0]['description'])



class SlotsFixture:
    """
    Daily "Vardag 15-18" and "Vardag 18-21" slots in a period, two hosts,
    and the rule that the afternoon needs the evening on weekdays.
    """

    def setUp(self):
        from .models import BookingDependency, SchedulingRule
        self.tz = timezone.get_current_timezone()
        today = timezone.localdate()
        # A Monday at least two weeks out, so nothing below is in the past.
        self.monday = today + datetime.timedelta(days=14 - today.weekday())
        self.tuesday = self.monday + datetime.timedelta(days=1)
        self.saturday = self.monday + datetime.timedelta(days=5)

        calendar = SchedulingCalendar.objects.create(
            name="cal", google_calendar_id="g", service_account_username="s",
            timezone="Europe/Stockholm")
        template = EventTemplate.objects.create(
            name="Öppet", title="Öppet hus", calendar=calendar, synchronize=False)
        daily = SchedulingRule.objects.create(
            name="Dagligen", description="", frequency="DAILY", params="")
        first = self.monday - datetime.timedelta(days=7)
        self.afternoon = Event.objects.create(
            name="Vardag 15-18", template=template, rule=daily,
            start=self._at(first, 15), end=self._at(first, 18))
        self.evening = Event.objects.create(
            name="Vardag 18-21", template=template, rule=daily,
            start=self._at(first, 18), end=self._at(first, 21))
        SchedulingPeriod.objects.create(
            start=first, end=self.monday + datetime.timedelta(days=60))
        self.rule = BookingDependency.objects.create(name="Kvällen först", weekdays="0,1,2,3,4")
        self.rule.dependent_events.set([self.afternoon])
        self.rule.required_events.set([self.evening])

        self.alice = User.objects.create(email="alice@example.com", slackId="alice", is_active=True)
        self.bob = User.objects.create(email="bob@example.com", slackId="bob", is_active=True)

    def _at(self, day, hour):
        return datetime.datetime.combine(day, datetime.time(hour), self.tz)

    def _slot(self, event, day):
        start = self._at(day, 0)
        return next(i for i in event.get_events(start, start + datetime.timedelta(days=1))
                    if timezone.localtime(i.start).date() == day)

    def _book(self, event, day, host, status=1):
        instance = self._slot(event, day)
        instance.host, instance.status = host, status
        instance.save()
        return instance

    def _unmet(self, event, day):
        from .booking_rules import DependencyChecker
        return DependencyChecker(day, day).unmet(self._slot(event, day))

    def _toggle(self, user, event, day):
        self.client.force_login(user)
        return self.client.post('/scheduler/week/toggle', {
            'event': event.pk, 'start': self._slot(event, day).start.isoformat()}, follow=True)



class BookingDependencyTests(SlotsFixture, TestCase):
    """
    "Vardag 15-18 can only be booked when Vardag 18-21 has a host the same
    day, Monday to Friday", checked by the week view and its booking endpoint.
    """

    # The rule itself
    def test_afternoon_is_locked_without_an_evening_host(self):
        from .booking_rules import DependencyChecker
        unmet = self._unmet(self.afternoon, self.tuesday)
        self.assertEqual([r['rule'] for r in unmet], [self.rule])
        self.assertEqual(DependencyChecker.describe(unmet), "Vardag 18-21")

    def test_afternoon_opens_once_the_evening_has_a_host(self):
        self._book(self.evening, self.tuesday, self.alice)
        self.assertEqual(self._unmet(self.afternoon, self.tuesday), [])

    def test_a_cancelled_evening_does_not_open_the_afternoon(self):
        self._book(self.evening, self.tuesday, self.alice, status=-1)
        self.assertNotEqual(self._unmet(self.afternoon, self.tuesday), [])

    def test_the_rule_only_applies_on_its_weekdays(self):
        self.assertEqual(self._unmet(self.afternoon, self.saturday), [])

    def test_an_inactive_rule_is_ignored(self):
        self.rule.is_active = False
        self.rule.save()
        self.assertEqual(self._unmet(self.afternoon, self.tuesday), [])

    def test_the_required_slot_itself_is_never_locked(self):
        self.assertEqual(self._unmet(self.evening, self.tuesday), [])

    # The booking endpoint
    def test_booking_a_locked_afternoon_is_refused(self):
        response = self._toggle(self.alice, self.afternoon, self.tuesday)
        self.assertFalse(EventInstance.objects.filter(event=self.afternoon, status=1).exists())
        self.assertIn("kan bokas först när Vardag 18-21 har en värd",
                      " ".join(str(m) for m in response.context['messages']))

    def test_booking_the_evening_first_opens_the_afternoon(self):
        self._toggle(self.alice, self.evening, self.tuesday)
        self._toggle(self.bob, self.afternoon, self.tuesday)
        self.assertEqual(self._slot(self.afternoon, self.tuesday).host, self.bob)
        self.assertEqual(self._slot(self.afternoon, self.tuesday).status, 1)

    def test_cancelling_the_evening_is_allowed_and_warns_about_the_afternoon(self):
        self._book(self.evening, self.tuesday, self.alice)
        self._book(self.afternoon, self.tuesday, self.bob)
        response = self._toggle(self.alice, self.evening, self.tuesday)
        self.assertEqual(self._slot(self.evening, self.tuesday).status, -1)
        self.assertEqual(self._slot(self.afternoon, self.tuesday).status, 1)
        text = " ".join(str(m) for m in response.context['messages'])
        self.assertIn("Vardag 15-18", text)
        self.assertIn("värd: bob", text)
        # ...and the afternoon card now carries the warning in the week view.
        self.assertContains(response, "Obs: Vardag 18-21 saknar värd")

    def test_week_view_shows_the_lock_and_what_opens_it(self):
        self.client.force_login(self.alice)
        iso = self.tuesday.isocalendar()
        response = self.client.get('/scheduler/week/{}/{}/'.format(iso.year, iso.week))
        self.assertContains(response, "Öppnas när Vardag 18-21 har en värd")


class BookingDependencyFormTests(TestCase):

    def setUp(self):
        calendar = SchedulingCalendar.objects.create(
            name="cal", google_calendar_id="g", service_account_username="s",
            timezone="Europe/Stockholm")
        template = EventTemplate.objects.create(
            name="t", title="Open", calendar=calendar, synchronize=False)
        start = timezone.now() + datetime.timedelta(days=1)
        self.a = Event.objects.create(name="A", template=template, start=start,
                                      end=start + datetime.timedelta(hours=3))
        self.b = Event.objects.create(name="B", template=template, start=start,
                                      end=start + datetime.timedelta(hours=3))

    def _form(self, dependent, required, weekdays):
        from .forms import BookingDependencyForm
        return BookingDependencyForm(data={
            'name': 'r', 'is_active': 'on', 'weekdays': weekdays,
            'dependent_events': [e.pk for e in dependent],
            'required_events': [e.pk for e in required]})

    def test_weekdays_are_stored_as_a_sorted_list(self):
        form = self._form([self.a], [self.b], ['4', '0', '2'])
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().weekdays, "0,2,4")

    def test_a_slot_cannot_require_itself(self):
        form = self._form([self.a], [self.a, self.b], ['0'])
        self.assertFalse(form.is_valid())
        self.assertIn("cannot require itself", str(form.errors))


class HostProgressTests(TestCase):
    """Status per host in a period, as shown on the follow-up page."""

    def setUp(self):
        calendar = SchedulingCalendar.objects.create(
            name="cal", google_calendar_id="g", service_account_username="s",
            timezone="Europe/Stockholm")
        self.template = EventTemplate.objects.create(
            name="t", title="Open", count_key="D", calendar=calendar, synchronize=False)
        today = timezone.localdate()
        # Halfway through: an even pace means 4 of the 8 required (6 + "DF").
        self.period = SchedulingPeriod.objects.create(
            start=today - datetime.timedelta(days=50), end=today + datetime.timedelta(days=50),
            num_required_events=6, participant_key_string="DF")
        self.event = Event.objects.create(
            name="e", template=self.template, start=timezone.now(),
            end=timezone.now() + datetime.timedelta(hours=3))
        self.host = User.objects.create(email="h@example.com", slackId="h", is_active=True)
        self.period.hosts.add(self.host)

    def _hosted(self, count, status=1, days_from_now=1):
        for n in range(count):
            start = timezone.now() + datetime.timedelta(days=days_from_now, hours=n * 4)
            EventInstance.objects.create(event=self.event, start=start,
                                         end=start + datetime.timedelta(hours=3),
                                         status=status, host=self.host, period=self.period)

    def _progress(self):
        from .host_progress import progress_for
        return progress_for(self.period, [self.host])[0]

    def test_nothing_booked(self):
        self.assertEqual(self._progress().status, 'none')

    def test_cancelled_slots_do_not_count(self):
        self._hosted(3, status=-1)
        progress = self._progress()
        self.assertEqual(progress.host_committed, 0)
        self.assertEqual(progress.status, 'none')

    def test_behind_an_even_pace(self):
        self._hosted(2)
        self.assertEqual(self._progress().expected_by_now, 4)
        self.assertEqual(self._progress().status, 'behind')

    def test_on_track_at_an_even_pace(self):
        self._hosted(4)
        self.assertEqual(self._progress().status, 'on_track')

    def test_done_needs_the_participant_keys_too(self):
        self._hosted(6)
        self.assertEqual(self._progress().status, 'on_track')
        self.assertEqual(self._progress().missing_keys, "DF")
        start = timezone.now() + datetime.timedelta(days=2)
        other = User.objects.create(email="o@example.com", slackId="o", is_active=True)
        for key in "DF":
            template = EventTemplate.objects.create(
                name=key, title=key, count_key=key, calendar=self.template.calendar, synchronize=False)
            event = Event.objects.create(name=key, template=template, start=start,
                                         end=start + datetime.timedelta(hours=1))
            instance = EventInstance.objects.create(
                event=event, start=start, end=start + datetime.timedelta(hours=1),
                status=1, host=other, period=self.period)
            instance.participants.add(self.host)
        self.assertEqual(self._progress().missing_keys, "")
        self.assertEqual(self._progress().status, 'done')

    def test_an_ended_period_is_done_or_missed(self):
        self.period.start = timezone.localdate() - datetime.timedelta(days=100)
        self.period.end = timezone.localdate() - datetime.timedelta(days=1)
        self.period.save()
        self.assertEqual(self._progress().status, 'missed')

    def test_followup_page_is_for_staff_only(self):
        # Not self.host: logging in sets last_login, and "aldrig" is checked below.
        self.client.force_login(User.objects.create(
            email="n@example.com", slackId="n", is_active=True))
        self.assertEqual(self.client.get('/scheduler/followup/').status_code, 302)
        self.client.force_login(make_staff())
        response = self.client.get('/scheduler/followup/{}/'.format(self.period.pk))
        self.assertContains(response, "Inget bokat")
        self.assertContains(response, "aldrig")  # the host has never logged in


class ReminderTests(TestCase):

    def setUp(self):
        calendar = SchedulingCalendar.objects.create(
            name="cal", google_calendar_id="g", service_account_username="s",
            timezone="Europe/Stockholm")
        self.template = EventTemplate.objects.create(
            name="t", title="Open", count_key="D", calendar=calendar, synchronize=False)
        today = timezone.localdate()
        self.period = SchedulingPeriod.objects.create(
            start=today - datetime.timedelta(days=50), end=today + datetime.timedelta(days=50),
            num_required_events=6, participant_key_string="DF")
        start = timezone.now() + datetime.timedelta(days=2)
        self.event = Event.objects.create(name="Kvällsöppet", template=self.template,
                                          start=start, end=start + datetime.timedelta(hours=3))
        self.lazy = User.objects.create(email="lazy@example.com", slackId="lazy", is_active=True)
        self.keen = User.objects.create(email="keen@example.com", slackId="keen", is_active=True)
        self.period.hosts.set([self.lazy, self.keen])
        for n in range(6):
            slot_start = start + datetime.timedelta(days=n + 1)
            EventInstance.objects.create(event=self.event, start=slot_start,
                                         end=slot_start + datetime.timedelta(hours=3),
                                         status=1, host=self.keen, period=self.period)
        self.staff = make_staff()

    def test_the_email_says_what_is_missing_and_lists_bookable_slots(self):
        from .host_progress import progress_for
        from .reminders import bookable_soon, compose
        progress = progress_for(self.period, [self.lazy])[0]
        subject, body = compose(progress, "https://example.org", bookable_soon())
        self.assertIn(self.period.name, subject)
        self.assertIn("Du har inte bokat några pass än.", body)
        self.assertIn("Kvar att boka: 6 värdpass och deltagarpass D, F.", body)
        self.assertIn("Kvällsöppet", body)  # the free one-off slot
        self.assertIn("https://example.org/scheduler/week/", body)

    def test_staff_preview_then_send_logs_the_reminder(self):
        from django.core import mail
        from .models import HostReminder
        self.client.force_login(self.staff)
        url = '/scheduler/followup/{}/remind/{}/'.format(self.period.pk, self.lazy.pk)
        preview = self.client.get(url)
        self.assertContains(preview, "lazy@example.com")
        self.assertEqual(len(mail.outbox), 0)
        self.client.post(url)
        self.assertEqual([m.to for m in mail.outbox], [["lazy@example.com"]])
        reminder = HostReminder.objects.get()
        self.assertEqual((reminder.user, reminder.sent_by), (self.lazy, self.staff))
        page = self.client.get('/scheduler/followup/{}/'.format(self.period.pk))
        self.assertContains(page, "1 gång, senast")

    def test_the_scheduled_job_only_emails_hosts_who_are_behind(self):
        from io import StringIO
        from django.core import mail
        from django.core.management import call_command
        call_command('send_host_reminders', stdout=StringIO())
        self.assertEqual([m.to for m in mail.outbox], [["lazy@example.com"]])

    def test_the_scheduled_job_skips_recently_reminded_hosts(self):
        from io import StringIO
        from django.core import mail
        from django.core.management import call_command
        call_command('send_host_reminders', stdout=StringIO())
        out = StringIO()
        call_command('send_host_reminders', stdout=out)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("skip   lazy", out.getvalue())

    def test_a_dry_run_sends_nothing(self):
        from io import StringIO
        from django.core import mail
        from django.core.management import call_command
        from .models import HostReminder
        out = StringIO()
        call_command('send_host_reminders', '--dry-run', stdout=out)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(HostReminder.objects.exists())
        self.assertIn("would  lazy", out.getvalue())


class CalendarEntryTests(TestCase):
    """Free-form calendar entries: the form, and that only staff may change them."""

    def setUp(self):
        self.tz = timezone.get_current_timezone()
        self.day = timezone.localdate() + datetime.timedelta(days=10)
        self.alice = make_staff("alice@example.com", "alice")
        self.bob = User.objects.create(email="bob@example.com", slackId="bob", is_active=True)

    def _post_new(self, user, **fields):
        data = {'title': 'Kurs i lödning', 'kind': 'course', 'description': '',
                'date': self.day.isoformat(), 'start_time': '18:30', 'end_time': '20:30',
                'end_date': ''}
        data.update(fields)
        self.client.force_login(user)
        return self.client.post('/scheduler/entries/new/', data)

    def _entry(self, user, **fields):
        from .models import CalendarEntry
        start = datetime.datetime.combine(self.day, datetime.time(19), self.tz)
        values = dict(title="Styrelsemöte", kind="meeting", start=start,
                      end=start + datetime.timedelta(hours=2), created_by=user)
        values.update(fields)
        return CalendarEntry.objects.create(**values)

    def test_staff_can_add_a_timed_entry(self):
        from .models import CalendarEntry
        response = self._post_new(self.alice)
        entry = CalendarEntry.objects.get()
        self.assertRedirects(response, '/scheduler/week/{}/{}/'.format(*self.day.isocalendar()[:2]),
                             fetch_redirect_response=False)
        self.assertEqual(entry.created_by, self.alice)
        self.assertFalse(entry.all_day)
        self.assertEqual(timezone.localtime(entry.start).time(), datetime.time(18, 30))
        self.assertEqual(timezone.localtime(entry.end).time(), datetime.time(20, 30))

    def test_no_times_means_all_day_and_an_end_date_spans_days(self):
        from .models import CalendarEntry
        self._post_new(self.alice, kind='closed', start_time='', end_time='',
                       end_date=(self.day + datetime.timedelta(days=1)).isoformat())
        entry = CalendarEntry.objects.get()
        self.assertTrue(entry.all_day)
        self.assertEqual(entry.local_days(), [self.day, self.day + datetime.timedelta(days=1)])
        self.assertEqual(timezone.localtime(entry.end),
                         datetime.datetime.combine(self.day + datetime.timedelta(days=2), datetime.time(), self.tz))

    def test_only_one_time_is_rejected(self):
        response = self._post_new(self.alice, end_time='')
        self.assertContains(response, "Fyll i både från och till")

    def test_end_before_start_is_rejected(self):
        response = self._post_new(self.alice, start_time='20:00', end_time='19:00')
        self.assertContains(response, "Slutet måste vara efter starten")

    def test_the_edit_form_shows_the_saved_values(self):
        from .forms import CalendarEntryForm
        form = CalendarEntryForm(instance=self._entry(self.alice))
        self.assertEqual(form.initial['date'], self.day)
        self.assertEqual(form.initial['start_time'], datetime.time(19))
        self.assertNotIn('end_date', form.initial)

    def test_members_cannot_add_entries(self):
        from .models import CalendarEntry
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get('/scheduler/entries/new/').status_code, 403)
        self.assertEqual(self._post_new(self.bob).status_code, 403)
        self.assertFalse(CalendarEntry.objects.exists())

    def test_only_staff_may_change_even_an_entry_a_member_created(self):
        entry = self._entry(self.bob)  # e.g. created before entries became staff-only
        edit = '/scheduler/entries/{}/edit/'.format(entry.pk)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(edit).status_code, 403)
        self.assertEqual(self.client.post('/scheduler/entries/{}/delete/'.format(entry.pk)).status_code, 403)
        page = self.client.get(entry.get_absolute_url())
        self.assertContains(page, "Styrelsemöte")
        self.assertNotContains(page, edit)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(edit).status_code, 200)

    def test_staff_can_change_and_delete(self):
        from .models import CalendarEntry
        entry = self._entry(self.alice)
        self.client.force_login(self.alice)
        self.client.post('/scheduler/entries/{}/edit/'.format(entry.pk), {
            'title': 'Styrelsemöte (flyttat)', 'kind': 'meeting', 'description': '',
            'date': self.day.isoformat(), 'start_time': '20:00', 'end_time': '21:00', 'end_date': ''})
        entry.refresh_from_db()
        self.assertEqual(entry.title, 'Styrelsemöte (flyttat)')
        self.assertEqual(timezone.localtime(entry.start).time(), datetime.time(20))
        self.client.post('/scheduler/entries/{}/delete/'.format(entry.pk))
        self.assertFalse(CalendarEntry.objects.exists())


class CalendarEntryWeekViewTests(SlotsFixture, TestCase):
    """Entries in the week view, beside the daily 15-18 and 18-21 slots."""

    def _entry(self, day, start=None, end=None, all_day=False, last_day=None, **fields):
        from .models import CalendarEntry
        if all_day:
            begin = self._at(day, 0)
            finish = self._at((last_day or day) + datetime.timedelta(days=1), 0)
        else:
            begin, finish = self._at(day, start), self._at(day, end)
        values = dict(title="Styrelsemöte", kind="meeting", start=begin, end=finish,
                      all_day=all_day, created_by=self.alice)
        values.update(fields)
        return CalendarEntry.objects.create(**values)

    def _week(self, user, day):
        self.client.force_login(user)
        iso = day.isocalendar()
        return self.client.get('/scheduler/week/{}/{}/'.format(iso.year, iso.week))

    def _day_items(self, response, day):
        found = next(d for d in response.context['days'] if d['date'] == day)
        return [item['entry'].title if 'entry' in item else item['slot'].event.name
                for item in found['items']]

    def test_a_timed_entry_sits_between_the_slots_by_time(self):
        self._entry(self.tuesday, 16, 17, title="Kurs")
        response = self._week(self.alice, self.tuesday)
        self.assertEqual(self._day_items(response, self.tuesday),
                         ["Vardag 15-18", "Kurs", "Vardag 18-21"])
        self.assertContains(response, 'class="entry kind-meeting"')

    def test_all_day_entries_are_banners_on_every_day_they_cover(self):
        self._entry(self.monday, all_day=True, last_day=self.tuesday,
                    title="Golvet lackas", kind="closed")
        response = self._week(self.bob, self.monday)
        days = {d['date']: d for d in response.context['days']}
        self.assertEqual([e.title for e in days[self.monday]['banners']], ["Golvet lackas"])
        self.assertEqual([e.title for e in days[self.tuesday]['banners']], ["Golvet lackas"])
        self.assertEqual(days[self.monday + datetime.timedelta(days=2)]['banners'], [])
        self.assertNotIn("Golvet lackas", self._day_items(response, self.monday))

    def test_only_staff_see_the_edit_and_add_links(self):
        entry = self._entry(self.tuesday, 16, 17)
        edit = '/scheduler/entries/{}/edit/'.format(entry.pk)
        member = self._week(self.alice, self.tuesday)
        self.assertNotContains(member, edit)
        self.assertNotContains(member, "+ Händelse")
        self.assertNotContains(member, "+ Ny händelse")
        staff = self._week(make_staff(), self.tuesday)
        self.assertContains(staff, edit)
        self.assertContains(staff, "+ Händelse")

    def test_the_picker_marks_days_with_entries(self):
        self._entry(self.tuesday, 16, 17, kind="course")
        self.assertContains(self._week(self.alice, self.tuesday), 'class="pp-dot kind-course"')


class ClosureTests(SlotsFixture, TestCase):
    """A "Stängt" calendar entry blocks every slot it overlaps."""

    def _closure(self, day, start=None, end=None, kind="closed", title="Golvet lackas"):
        from .models import CalendarEntry
        if start is None:
            begin, finish, all_day = self._at(day, 0), self._at(day + datetime.timedelta(days=1), 0), True
        else:
            begin, finish, all_day = self._at(day, start), self._at(day, end), False
        return CalendarEntry.objects.create(title=title, kind=kind, start=begin, end=finish,
                                            all_day=all_day, created_by=self.alice)

    def _states(self, user, day):
        self.client.force_login(user)
        iso = day.isocalendar()
        response = self.client.get('/scheduler/week/{}/{}/'.format(iso.year, iso.week))
        found = next(d for d in response.context['days'] if d['date'] == day)
        return {s.event.name: s for s in found['slots']}, response

    def test_a_partial_closure_blocks_only_the_slot_it_overlaps(self):
        self._closure(self.tuesday, 16, 17)
        slots, response = self._states(self.alice, self.tuesday)
        self.assertEqual(slots["Vardag 15-18"].state, 'closed')
        self.assertEqual(slots["Vardag 15-18"].hint, "Golvet lackas")
        self.assertIsNone(slots["Vardag 15-18"].action)
        self.assertEqual(slots["Vardag 18-21"].state, 'free')

    def test_an_all_day_closure_blocks_every_slot_that_day(self):
        self._closure(self.tuesday)
        slots, _ = self._states(self.alice, self.tuesday)
        self.assertEqual({name: s.state for name, s in slots.items()},
                         {"Vardag 15-18": 'closed', "Vardag 18-21": 'closed'})

    def test_other_kinds_of_entry_do_not_block(self):
        self._closure(self.tuesday, kind="meeting")
        slots, _ = self._states(self.alice, self.tuesday)
        self.assertEqual(slots["Vardag 18-21"].state, 'free')

    def test_booking_a_closed_slot_is_refused(self):
        self._closure(self.tuesday)
        response = self._toggle(self.alice, self.evening, self.tuesday)
        self.assertFalse(EventInstance.objects.filter(status=1).exists())
        self.assertIn("lokalen är stängd: Golvet lackas",
                      " ".join(str(m) for m in response.context['messages']))

    def test_a_slot_booked_before_the_closure_keeps_its_host_and_is_flagged(self):
        self._book(self.evening, self.tuesday, self.alice)
        self._closure(self.tuesday)
        slots, response = self._states(self.alice, self.tuesday)
        self.assertEqual(slots["Vardag 18-21"].state, 'mine')
        self.assertContains(response, "Obs: Lokalen är stängd: Golvet lackas")

    def test_the_old_list_page_refuses_a_closed_slot_too(self):
        self._closure(self.tuesday)
        self.client.force_login(self.alice)
        formset = self.client.get('/scheduler/').context['formset']
        data = {'{}-{}'.format(formset.prefix, key): value
                for key, value in formset.management_form.initial.items()}
        target = self._slot(self.evening, self.tuesday).start
        for form in formset.forms:
            for name in form.fields:
                value = form[name].value()
                if name == 'perform_action':
                    if form.initial.get('start') == target and form.initial.get('event') == self.evening.pk:
                        data[form.add_prefix(name)] = 'on'
                    elif value:
                        data[form.add_prefix(name)] = 'on'
                elif value is not None:
                    data[form.add_prefix(name)] = value
        response = self.client.post('/scheduler/', data, follow=True)
        self.assertFalse(EventInstance.objects.filter(status=1).exists())
        self.assertContains(response, "lokalen är stängd: Golvet lackas")
