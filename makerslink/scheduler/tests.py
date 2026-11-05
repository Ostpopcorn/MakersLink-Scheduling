import datetime
import os
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

    def _instance(self, status):
        today = timezone.now().date()
        period = SchedulingPeriod.objects.create(
            start=today - datetime.timedelta(days=30),
            end=today + datetime.timedelta(days=120))
        event = Event.objects.create(name="e", template=self.template,
                                     start=self.start, end=self.end)
        with mock.patch.object(SchedulingCalendar, 'createEvent',
                               return_value="cal-id-1") as created:
            instance = EventInstance.objects.create(
                event=event, start=self.start, end=self.end, status=status,
                host=None, period=period)
        return instance, created

    def test_saving_an_unhosted_cancelled_instance_syncs_instead_of_crashing(self):
        # Staff cancelling a slot nobody had booked: published as "Inställt".
        instance, created = self._instance(status=2)
        self.assertEqual(instance.google_calendar_booking_id, "cal-id-1")
        self.assertNotIn("Värd", created.call_args.args[0]['description'])

    def test_a_free_instance_is_not_published(self):
        instance, created = self._instance(status=0)
        created.assert_not_called()
        self.assertIsNone(instance.google_calendar_booking_id)



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
        self.period = SchedulingPeriod.objects.create(
            start=first, end=self.monday + datetime.timedelta(days=60))
        self.rule = BookingDependency.objects.create(name="Kvällen först", weekdays="0,1,2,3,4",
                                                     period=self.period)
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
        self.assertEqual(DependencyChecker.describe(unmet), "Vardag 18-21 är bokat")

    def test_afternoon_opens_once_the_evening_has_a_host(self):
        self._book(self.evening, self.tuesday, self.alice)
        self.assertEqual(self._unmet(self.afternoon, self.tuesday), [])

    def test_an_evening_up_for_rebooking_does_not_open_the_afternoon(self):
        # alice is still the host, but until someone takes it over the evening
        # is not booked, so the afternoon stays locked.
        self._book(self.evening, self.tuesday, self.alice, status=-1)
        self.assertNotEqual(self._unmet(self.afternoon, self.tuesday), [])

    def test_an_evening_without_a_host_does_not_open_the_afternoon(self):
        self._book(self.evening, self.tuesday, None, status=-1)
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
        self.assertIn("kan bokas först när Vardag 18-21 är bokat",
                      " ".join(str(m) for m in response.context['messages']))

    def test_booking_the_evening_first_opens_the_afternoon(self):
        self._toggle(self.alice, self.evening, self.tuesday)
        self._toggle(self.bob, self.afternoon, self.tuesday)
        self.assertEqual(self._slot(self.afternoon, self.tuesday).host, self.bob)
        self.assertEqual(self._slot(self.afternoon, self.tuesday).status, 1)

    def test_flagging_the_evening_warns_about_the_booked_afternoon(self):
        self._book(self.evening, self.tuesday, self.alice)
        self._book(self.afternoon, self.tuesday, self.bob)
        flagged = self._toggle(self.alice, self.evening, self.tuesday)  # "Ombokning önskas"
        text = " ".join(str(m) for m in flagged.context['messages'])
        self.assertIn("Vardag 15-18", text)
        self.assertIn("värd: bob", text)
        self.assertEqual(self._slot(self.afternoon, self.tuesday).status, 1)  # bob keeps it
        self.assertContains(flagged, "Obs: Kräver att Vardag 18-21 är bokat")

    def test_staff_removing_the_evening_warns_about_the_afternoon(self):
        self._book(self.evening, self.tuesday, self.alice)
        self._book(self.afternoon, self.tuesday, self.bob)
        from .views_week import slot_url
        self.client.force_login(make_staff())
        self.client.post(slot_url(self._slot(self.evening, self.tuesday)),
                         {'action': 'remove', 'after': '0', 'note': ''})
        self.assertEqual((self._slot(self.evening, self.tuesday).status, self._slot(self.afternoon, self.tuesday).status),
                         (0, 1))
        iso = self.tuesday.isocalendar()
        self.client.force_login(self.bob)
        week = self.client.get('/scheduler/week/{}/{}/'.format(iso.year, iso.week))
        self.assertContains(week, "Obs: Kräver att Vardag 18-21 är bokat")

    def test_week_view_shows_the_lock_and_what_opens_it(self):
        self.client.force_login(self.alice)
        iso = self.tuesday.isocalendar()
        response = self.client.get('/scheduler/week/{}/{}/'.format(iso.year, iso.week))
        self.assertContains(response, "Öppnas när Vardag 18-21 är bokat")


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
        self.period = SchedulingPeriod.objects.create(start=timezone.localdate(),
                                                      end=timezone.localdate() + datetime.timedelta(days=90))

    def _form(self, dependent, required, weekdays):
        from .forms import BookingDependencyForm
        return BookingDependencyForm(data={
            'name': 'r', 'is_active': 'on', 'weekdays': weekdays, 'day_offset': '0', 'period': self.period.pk,
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

    def test_slots_up_for_rebooking_count_until_taken_over_and_cancelled_ones_never(self):
        self._hosted(3, status=-1)
        self.assertEqual(self._progress().host_committed, 3)  # still this host's
        EventInstance.objects.filter(host=self.host).update(host=User.objects.create(
            email="t@example.com", slackId="t", is_active=True), status=1)  # taken over
        self.assertEqual(self._progress().host_committed, 0)
        self._hosted(2, status=2)  # cancelled by staff
        self.assertEqual(self._progress().host_committed, 0)
        self.assertEqual(self._progress().status, 'none')

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

    def test_one_bar_per_participant_key(self):
        self.period.participant_key_string = "DDF"
        self.period.save()
        other = User.objects.create(email="o@example.com", slackId="o", is_active=True)
        open_house = EventTemplate.objects.create(name="Öppet hus", title="Öppet hus", count_key="D",
                                                  num_participants=1, synchronize=False)
        EventTemplate.objects.create(name="Fixarpass", title="Fixarpass", count_key="F",
                                     num_participants=-1, synchronize=False)
        EventTemplate.objects.create(name="Fixarkväll", title="Fixarkväll", count_key="F",
                                     num_participants=-1, synchronize=False)  # F has two names
        for days in (-3, 4):  # one done, one booked
            start = timezone.now() + datetime.timedelta(days=days)
            event = Event.objects.create(name="D", template=open_house, start=start,
                                         end=start + datetime.timedelta(hours=1))
            instance = EventInstance.objects.create(
                event=event, start=start, end=start + datetime.timedelta(hours=1),
                status=1, host=other, period=self.period)
            instance.participants.add(self.host)
        bars = {bar['key']: bar for bar in self._progress().participant_bars}
        self.assertEqual(list(bars), ['D', 'F'])
        self.assertEqual({k: bars['D'][k] for k in ('name', 'required', 'done', 'booked', 'missing')},
                         {'name': 'Öppet hus', 'required': 2, 'done': 1, 'booked': 1, 'missing': 0})
        self.assertEqual((bars['D']['done_pct'], bars['D']['booked_pct']), (50, 50))
        self.assertEqual({k: bars['F'][k] for k in ('name', 'required', 'committed', 'missing')},
                         {'name': '', 'required': 1, 'committed': 0, 'missing': 1})

    def test_the_booking_page_shows_fixarpass_and_notes_the_rest(self):
        from .models import CalendarEntry, EntryHost, SpecialPassType
        # As in the real data: periods ask for Fixarpass (F); going alongside
        # a host on an open evening (D) is not required.
        self.period.participant_key_string = "F"
        self.period.save()
        EventTemplate.objects.create(name="Fixarpass", title="Fixarpass", count_key="F",
                                     num_participants=-1, synchronize=False)
        open_house = EventTemplate.objects.create(name="Makermåndag", title="Makermåndag", count_key="D",
                                                  num_participants=1, synchronize=False)
        other = User.objects.create(email="o@example.com", slackId="o", is_active=True)
        for days in (2, 9):
            start = timezone.now() + datetime.timedelta(days=days)
            event = Event.objects.create(name="M", template=open_house, start=start,
                                         end=start + datetime.timedelta(hours=3))
            instance = EventInstance.objects.create(
                event=event, start=start, end=start + datetime.timedelta(hours=3),
                status=1, host=other, period=self.period)
            instance.participants.add(self.host)
        tour = SpecialPassType.objects.create(name="Studiebesök", start_time=datetime.time(16),
                                              end_time=datetime.time(18), hosts_needed=2,
                                              counts_as_session=False)
        start = timezone.now() + datetime.timedelta(days=5)
        visit = CalendarEntry.objects.create(title="Studiebesök", kind="special", special_type=tour,
                                             hosts_needed=2, start=start, end=start + datetime.timedelta(hours=2))
        EntryHost.objects.create(entry=visit, user=self.host)

        self.client.force_login(self.host)
        page = self.client.get('/scheduler/list/', {'period': self.period.pk})
        self.assertContains(page, 'Fixarpass: <b>0</b> av 1')
        self.assertNotContains(page, 'Makermåndag: <b>')  # no bar for it
        self.assertContains(page, 'href="/scheduler/joinEvent" title="Anmäl dig till ett pass"', count=1)
        self.assertContains(page, 'Makermåndag som deltagare (2)</span>')
        self.assertContains(page, 'Studiebesök som värd</span>')
        self.assertContains(page, '<b>6</b> värdpass och <b>1</b> Fixarpass.')

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

    def test_the_list_shows_a_closed_slot_without_a_button(self):
        self._closure(self.tuesday)
        self.client.force_login(self.alice)
        response = self.client.get('/scheduler/list/')
        found = next(d for w in response.context['weeks'] for d in w['days'] if d['date'] == self.tuesday)
        self.assertEqual([item['slot'].state for item in found['items']], ['closed', 'closed'])
        self.assertTrue(all(item['slot'].action is None for item in found['items']))


class GoogleCalendarClientTests(TestCase):
    """The single place that talks to Google, and its local stand-in."""

    def setUp(self):
        self.calendar = SchedulingCalendar.objects.create(
            name="cal", google_calendar_id="cal-id@group", service_account_username="s",
            timezone="Europe/Stockholm")

    def _google(self):
        from .google_calendar import GoogleCalendarClient
        service = mock.MagicMock()
        patcher = mock.patch('googleapiclient.discovery.build', return_value=service)
        patcher.start()
        self.addCleanup(patcher.stop)
        creds = mock.patch.object(SchedulingCalendar, 'get_credentials', return_value=object())
        creds.start()
        self.addCleanup(creds.stop)
        return GoogleCalendarClient(self.calendar), service.events.return_value

    def test_list_follows_every_page(self):
        client, events = self._google()
        events.list.return_value.execute.side_effect = [
            {'items': [{'id': 'a'}], 'nextPageToken': 'p2'}, {'items': [{'id': 'b'}]}]
        now = timezone.now()
        self.assertEqual([e['id'] for e in client.list(now, now + datetime.timedelta(days=1))], ['a', 'b'])
        self.assertEqual(events.list.call_args_list[1].kwargs['pageToken'], 'p2')
        self.assertTrue(events.list.call_args_list[0].kwargs['singleEvents'])

    def test_deleting_an_event_that_is_already_gone_is_fine(self):
        import httplib2
        from googleapiclient.errors import HttpError
        client, events = self._google()
        events.delete.return_value.execute.side_effect = HttpError(httplib2.Response({'status': 410}), b'')
        client.delete('gone')  # no exception
        events.delete.return_value.execute.side_effect = HttpError(httplib2.Response({'status': 500}), b'')
        with self.assertRaises(HttpError):
            client.delete('broken')

    def test_slot_bookings_go_through_the_configured_client(self):
        import tempfile
        from django.test import override_settings
        from .google_calendar import client_for, event_bounds
        path = tempfile.mktemp(suffix='.json')
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        template = EventTemplate.objects.create(name="t", title="Öppet hus", calendar=self.calendar,
                                                synchronize=True)
        start = timezone.now() + datetime.timedelta(days=3)
        event = Event.objects.create(name="e", template=template, start=start,
                                     end=start + datetime.timedelta(hours=3))
        host = User.objects.create(email="h@example.com", slackId="hanna", is_active=True)
        with override_settings(SCHEDULER_CALENDAR_CLIENT='scheduler.google_calendar.LocalFileCalendarClient',
                               SCHEDULER_LOCAL_CALENDAR_FILE=path):
            instance = EventInstance.objects.create(event=event, start=start, end=event.end,
                                                    status=1, host=host)
            stored = client_for(self.calendar).list(start - datetime.timedelta(hours=1),
                                                    start + datetime.timedelta(hours=1))
        self.assertEqual([e['id'] for e in stored], [instance.google_calendar_booking_id])
        self.assertEqual(stored[0]['summary'], "Öppet hus")
        self.assertIn("Värd: hanna", stored[0]['description'])
        begin, end, all_day = event_bounds(stored[0])
        self.assertEqual((begin, end, all_day), (start.replace(microsecond=0), event.end.replace(microsecond=0), False))

    def test_failures_still_surface_as_valueerror_with_the_cause(self):
        from django.test import override_settings
        with override_settings(SCHEDULER_CALENDAR_CLIENT='scheduler.tests.BrokenClient'):
            with self.assertRaises(ValueError) as caught:
                self.calendar.createEvent({'summary': 'x'})
        self.assertIsInstance(caught.exception.__cause__, ConnectionError)


class BrokenClient:
    """A calendar client whose every call fails, for the test above."""

    def __init__(self, calendar):
        pass

    def insert(self, *args):
        raise ConnectionError("no network")

    update = delete = list = insert


class LocalCalendarMixin:
    """Points every calendar client at a throwaway LocalFileCalendarClient file."""

    def setUp(self):
        super().setUp()
        import tempfile
        from django.test import override_settings
        handle, self.calendar_file = tempfile.mkstemp(suffix='.json')
        os.close(handle)
        os.remove(self.calendar_file)
        self.addCleanup(lambda: os.path.exists(self.calendar_file) and os.remove(self.calendar_file))
        self.use_client('scheduler.google_calendar.LocalFileCalendarClient')

    def use_client(self, path):
        from django.test import override_settings
        overridden = override_settings(SCHEDULER_CALENDAR_CLIENT=path,
                                       SCHEDULER_LOCAL_CALENDAR_FILE=self.calendar_file)
        overridden.enable()
        self.addCleanup(overridden.disable)

    def google_events(self, calendar):
        import json
        if not os.path.exists(self.calendar_file):
            return {}
        with open(self.calendar_file) as handle:
            return json.load(handle).get(calendar.google_calendar_id, {})


class EntrySyncTests(LocalCalendarMixin, TestCase):
    """Calendar entries are put in, kept up to date in, and removed from Google."""

    def setUp(self):
        super().setUp()
        self.staff = make_staff()
        self.client.force_login(self.staff)
        self.main = SchedulingCalendar.objects.create(
            name="Öppettider", google_calendar_id="main@group", service_account_username="s",
            timezone="Europe/Stockholm")
        self.spare = SchedulingCalendar.objects.create(
            name="Reserv", google_calendar_id="spare@group", service_account_username="s",
            timezone="Europe/Stockholm")
        self.day = timezone.localdate() + datetime.timedelta(days=10)

    def _form(self, **fields):
        data = {'title': 'Kurs i lödning', 'kind': 'course', 'description': 'Ta med lödkolv',
                'date': self.day.isoformat(), 'start_time': '18:30', 'end_time': '20:30',
                'end_date': '', 'calendar': str(self.main.pk)}
        data.update(fields)
        return data

    def _new(self, **fields):
        from .models import CalendarEntry
        response = self.client.post('/scheduler/entries/new/', self._form(**fields), follow=True)
        return CalendarEntry.objects.latest('pk'), response

    def _edit(self, entry, **fields):
        self.client.post('/scheduler/entries/{}/edit/'.format(entry.pk), self._form(**fields))
        entry.refresh_from_db()
        return entry

    def test_a_new_entry_is_put_in_google_with_its_colour_and_marker(self):
        entry, response = self._new()
        event = self.google_events(self.main)[entry.google_event_id]
        self.assertEqual(event['summary'], 'Kurs i lödning')
        self.assertEqual(event['description'], 'Ta med lödkolv')
        self.assertEqual(event['colorId'], '3')
        self.assertEqual(event['extendedProperties']['private']['makerslinkEntry'], str(entry.pk))
        self.assertTrue(event['start']['dateTime'].startswith(self.day.isoformat() + 'T18:30:00'))
        self.assertIsNotNone(entry.synced_at)
        self.assertContains(response, 'Den finns nu också i Google-kalendern')

    def test_all_day_entries_are_sent_as_dates(self):
        entry, _ = self._new(kind='closed', start_time='', end_time='',
                             end_date=(self.day + datetime.timedelta(days=1)).isoformat())
        event = self.google_events(self.main)[entry.google_event_id]
        self.assertEqual(event['start'], {'date': self.day.isoformat()})
        self.assertEqual(event['end'], {'date': (self.day + datetime.timedelta(days=2)).isoformat()})
        self.assertEqual(event['colorId'], '8')

    def test_editing_updates_the_same_google_event(self):
        entry, _ = self._new()
        first_id = entry.google_event_id
        entry = self._edit(entry, start_time='19:00', end_time='21:00')
        self.assertEqual(entry.google_event_id, first_id)
        self.assertEqual(len(self.google_events(self.main)), 1)
        self.assertIn('T19:00:00', self.google_events(self.main)[first_id]['start']['dateTime'])

    def test_moving_it_to_another_calendar_or_none(self):
        entry, _ = self._new()
        entry = self._edit(entry, calendar=str(self.spare.pk))
        self.assertEqual(self.google_events(self.main), {})
        self.assertEqual(list(self.google_events(self.spare)), [entry.google_event_id])
        entry = self._edit(entry, calendar='')
        self.assertEqual(self.google_events(self.spare), {})
        self.assertIsNone(entry.google_event_id)

    def test_deleting_the_entry_deletes_the_google_event(self):
        entry, _ = self._new()
        self.client.post('/scheduler/entries/{}/delete/'.format(entry.pk))
        self.assertEqual(self.google_events(self.main), {})

    def test_a_google_failure_keeps_the_entry_and_can_be_retried(self):
        self.use_client('scheduler.tests.BrokenClient')
        entry, response = self._new()
        self.assertIsNone(entry.google_event_id)
        self.assertIn('ConnectionError', entry.sync_error)
        self.assertContains(response, 'kunde inte läggas i Google-kalendern')
        self.assertContains(self.client.get(entry.get_absolute_url()), 'Synka igen')
        self.use_client('scheduler.google_calendar.LocalFileCalendarClient')
        self.client.post('/scheduler/entries/{}/sync/'.format(entry.pk))
        entry.refresh_from_db()
        self.assertEqual(entry.sync_error, '')
        self.assertIn(entry.google_event_id, self.google_events(self.main))

    def test_an_event_deleted_in_google_is_put_back_on_the_next_save(self):
        from .google_calendar import client_for
        entry, _ = self._new()
        client_for(self.main).delete(entry.google_event_id)
        entry = self._edit(entry, title='Kurs i lödning (flyttad)')
        self.assertEqual(self.google_events(self.main)[entry.google_event_id]['summary'],
                         'Kurs i lödning (flyttad)')


class CalendarFixture(LocalCalendarMixin):
    """A synchronizing slot template on a local calendar, a host and a staff member."""

    def setUp(self):
        super().setUp()
        self.calendar = SchedulingCalendar.objects.create(
            name="Öppettider", google_calendar_id="main@group", service_account_username="s",
            timezone="Europe/Stockholm")
        self.template = EventTemplate.objects.create(
            name="Öppet", title="Öppet hus", calendar=self.calendar, synchronize=True)
        self.start = (timezone.now() + datetime.timedelta(days=5)).replace(second=0, microsecond=0)
        self.event = Event.objects.create(name="Kväll", template=self.template, start=self.start,
                                          end=self.start + datetime.timedelta(hours=3))
        self.leo = User.objects.create(email="leo@example.com", slackId="leo", is_active=True)
        self.staff = make_staff()

    def _book(self, status=1, host=None, hours_later=0):
        start = self.start + datetime.timedelta(hours=hours_later)
        return EventInstance.objects.create(event=self.event, start=start, end=start + datetime.timedelta(hours=3),
                                            status=status, host=host or self.leo)

    def _client(self):
        from .google_calendar import client_for
        return client_for(self.calendar)

    def _kinds(self):
        from . import calendar_check
        return [(d['kind'], d['title']) for d in calendar_check.run(self.calendar).differences]

    def _change_in_google(self, event_id, **changes):
        event = self.google_events(self.calendar)[event_id]
        event.update(changes)
        self._client().update(event_id, event)


    def _leftover(self):
        """A booking removed without its event: the event was left in Google."""
        instance = self._book()
        EventInstance.objects.filter(pk=instance.pk).update(status=0, host=None)
        return instance


class CalendarCheckTests(CalendarFixture, TestCase):
    """Google Calendar compared with the schedule."""

    def test_a_calendar_in_step_reports_nothing(self):
        from .models import CalendarEntry
        from . import entry_sync
        self._book()
        entry = CalendarEntry.objects.create(title="Kurs", kind="course", start=self.start,
                                             end=self.start + datetime.timedelta(hours=1), calendar=self.calendar)
        entry_sync.push(entry)
        self.assertEqual(self._kinds(), [])

    def test_an_event_deleted_in_google_is_missing(self):
        instance = self._book()
        self._client().delete(instance.google_calendar_booking_id)
        self.assertEqual(self._kinds(), [('missing', 'Öppet hus')])

    def test_a_moved_event_and_a_changed_host_or_title(self):
        instance = self._book()
        event = self.google_events(self.calendar)[instance.google_calendar_booking_id]
        moved = (datetime.datetime.fromisoformat(event['start']['dateTime']) + datetime.timedelta(hours=1)).isoformat()
        self._change_in_google(instance.google_calendar_booking_id, start={'dateTime': moved},
                               summary="Extra öppet", description="Värd: pelle")
        self.assertEqual(sorted(k for k, _ in self._kinds()), ['wrong_host', 'wrong_time', 'wrong_title'])

    def test_asking_for_rebooking_changes_nothing_in_google_and_a_take_over_does(self):
        instance = self._book()
        before = dict(self.google_events(self.calendar)[instance.google_calendar_booking_id])
        self.client.force_login(instance.host)
        self.client.post('/scheduler/week/toggle', {'event': instance.event_id, 'start': instance.start.isoformat()})
        instance.refresh_from_db()
        self.assertEqual(instance.status, -1)
        self.assertEqual(self.google_events(self.calendar)[instance.google_calendar_booking_id], before)
        self.assertEqual(self._kinds(), [])  # the check agrees: it is still booked
        self.client.force_login(self.staff)
        self.client.post('/scheduler/week/toggle', {'event': instance.event_id, 'start': instance.start.isoformat()})
        instance.refresh_from_db()
        self.assertEqual((instance.status, instance.host), (1, self.staff))
        self.assertIn("Värd: staff", self.google_events(self.calendar)[instance.google_calendar_booking_id]['description'])

    def test_deleting_a_slot_deletes_its_event(self):
        instance = self._book()
        instance.delete()
        self.assertEqual(self.google_events(self.calendar), {})

    def test_a_leftover_cancellation_still_looks_open_in_google(self):
        self._leftover()
        from . import calendar_check
        (difference,) = calendar_check.run(self.calendar).differences
        self.assertEqual(difference['kind'], 'cancelled_shown')
        self.assertEqual(difference['google'], 'Öppet hus, värd: leo')

    def test_a_cancelled_slot_titled_installt_is_fine(self):
        instance = self._book()
        instance.status = 2
        instance.save()
        self.assertEqual(self._kinds(), [])

    def test_events_only_in_google_are_reported_and_ours_are_recognised(self):
        from .models import CalendarEntry
        from . import entry_sync
        entry = CalendarEntry.objects.create(title="Borttagen kurs", kind="course", start=self.start,
                                             end=self.start + datetime.timedelta(hours=1), calendar=self.calendar)
        entry_sync.push(entry)
        CalendarEntry.objects.filter(pk=entry.pk).delete()  # gone here, left in Google
        self._client().insert({'summary': 'Extra öppet (Pelle)', 'start': {'dateTime': self.start.isoformat()},
                               'end': {'dateTime': (self.start + datetime.timedelta(hours=2)).isoformat()}})
        from . import calendar_check
        found = {d['title']: d['ours'] for d in calendar_check.run(self.calendar).differences
                 if d['kind'] == 'only_google'}
        self.assertEqual(found, {'Extra öppet (Pelle)': 'Finns inte här',
                                 'Borttagen kurs': 'Borttagen händelse #{}'.format(entry.pk)})

    def test_an_entry_that_never_reached_google(self):
        from .models import CalendarEntry
        CalendarEntry.objects.create(title="Kurs", kind="course", start=self.start, calendar=self.calendar,
                                     end=self.start + datetime.timedelta(hours=1), sync_error="ConnectionError: x")
        self.assertEqual(self._kinds(), [('not_synced', 'Kurs')])

    def test_when_google_cannot_be_reached_the_error_is_stored(self):
        from . import calendar_check
        self._book()
        self.use_client('scheduler.tests.BrokenClient')
        check = calendar_check.run(self.calendar)
        self.assertIn('ConnectionError', check.error)
        self.assertEqual(check.differences, [])

    def test_the_page_runs_the_check_and_the_menu_shows_the_count(self):
        instance = self._book()
        self._client().delete(instance.google_calendar_booking_id)
        self.client.force_login(self.leo)
        self.assertEqual(self.client.get('/scheduler/calendar-check/').status_code, 302)
        self.client.force_login(self.staff)
        response = self.client.post('/scheduler/calendar-check/', follow=True)
        self.assertContains(response, 'Saknas i Google')
        self.assertContains(response, 'Kontrollen hittade 1 skillnad.')
        self.assertContains(response, 'Kalenderkontroll <span class="badge badge-warning">1</span>')

    def test_the_job_emails_staff_only_when_something_changed(self):
        from io import StringIO
        from django.core import mail
        from django.core.management import call_command
        instance = self._book()
        self._client().delete(instance.google_calendar_booking_id)
        call_command('check_google_calendar', stdout=StringIO())
        call_command('check_google_calendar', stdout=StringIO())  # same difference: no new email
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.staff.email])
        self.assertIn('Saknas i Google', mail.outbox[0].body)
        self._book(hours_later=24)
        self._client().delete(EventInstance.objects.latest('start').google_calendar_booking_id)
        call_command('check_google_calendar', stdout=StringIO())
        self.assertEqual(len(mail.outbox), 2)
        call_command('check_google_calendar', '--no-email', '--always-email', stdout=StringIO())
        self.assertEqual(len(mail.outbox), 2)


class CalendarCheckFixTests(CalendarFixture, TestCase):
    """Fixing differences from the check page."""

    def _check_and_fix(self, **post):
        from . import calendar_check
        check = calendar_check.run(self.calendar)
        self.client.force_login(self.staff)
        response = self.client.post('/scheduler/calendar-check/{}/fix/'.format(check.pk), post, follow=True)
        check.refresh_from_db()
        return check, response

    def test_a_leftover_cancellation_can_be_removed_from_google(self):
        instance = self._leftover()
        leftover = instance.google_calendar_booking_id
        check, response = self._check_and_fix(key='slot:{}'.format(instance.pk))
        self.assertNotIn(leftover, self.google_events(self.calendar))
        instance.refresh_from_db()
        self.assertIsNone(instance.google_calendar_booking_id)
        self.assertEqual(check.differences, [])
        self.assertContains(response, 'Rättade Öppet hus')

    def test_missing_events_are_recreated_and_wrong_hosts_rewritten(self):
        gone = self._book()
        self._client().delete(gone.google_calendar_booking_id)
        edited = self._book(hours_later=24)
        self._change_in_google(edited.google_calendar_booking_id, description="Värd: pelle")
        self._check_and_fix(kind='missing')
        gone.refresh_from_db()
        self.assertIn(gone.google_calendar_booking_id, self.google_events(self.calendar))
        check, _ = self._check_and_fix(key='slot:{}'.format(edited.pk))
        self.assertIn('Värd: leo', self.google_events(self.calendar)[edited.google_calendar_booking_id]['description'])
        self.assertEqual(check.differences, [])

    def test_google_only_events_are_left_to_a_person(self):
        event_id = self._client().insert({'summary': 'Extra', 'start': {'dateTime': self.start.isoformat()},
                                          'end': {'dateTime': (self.start + datetime.timedelta(hours=1)).isoformat()}})
        check, response = self._check_and_fix(key='google:{}'.format(event_id))
        self.assertIn(event_id, self.google_events(self.calendar))
        self.assertEqual(len(check.differences), 1)
        self.assertContains(response, 'Kan inte rättas automatiskt')


class BookingLogTests(SlotsFixture, TestCase):
    """Every change of host is logged, whichever page it was made on."""

    def _log(self):
        from .models import BookingChange
        return [(c.action, c.host.slackId, c.by.slackId) for c in BookingChange.objects.order_by('at', 'pk')]

    def test_the_week_view_logs_booking_and_cancelling(self):
        self._toggle(self.alice, self.evening, self.tuesday)
        self._toggle(self.alice, self.evening, self.tuesday)
        self.assertEqual(self._log(), [('booked', 'alice', 'alice'), ('cancelled', 'alice', 'alice')])

    def test_staff_see_the_history_on_the_slot_page(self):
        self._toggle(self.alice, self.evening, self.tuesday)
        self.client.force_login(make_staff())
        page = self.client.get('/scheduler/eventinstance-admin/{}'.format(self._slot(self.evening, self.tuesday).pk))
        self.assertContains(page, 'Historik')
        self.assertContains(page, '<b>Bokade</b>:')


class StaffBookingTests(SlotsFixture, TestCase):
    """Staff booking a slot for a member, changing its host, and removing a booking."""

    def setUp(self):
        super().setUp()
        self.staff = make_staff()
        self.period = SchedulingPeriod.objects.get()
        self.period.hosts.set([self.alice, self.bob])

    def _url(self, event, day):
        from .views_week import slot_url
        return slot_url(self._slot(event, day))

    def _post(self, event, day, **data):
        self.client.force_login(self.staff)
        return self.client.post(self._url(event, day), data, follow=True)

    def _book_for(self, user, event=None, day=None, **extra):
        data = {'action': 'book', 'host': user.pk, 'notify': 'on', 'note': ''}
        data.update(extra)
        return self._post(event or self.evening, day or self.tuesday, **data)

    def _log(self):
        from .models import BookingChange
        return [(c.action, c.host.slackId if c.host else None,
                 c.previous_host.slackId if c.previous_host else None, c.by.slackId, c.note)
                for c in BookingChange.objects.order_by('at', 'pk')]

    def test_staff_can_book_a_free_slot_for_a_member(self):
        from django.core import mail
        response = self._book_for(self.bob, note='Tack för att du hoppar in!')
        slot = self._slot(self.evening, self.tuesday)
        self.assertEqual((slot.status, slot.host), (1, self.bob))
        self.assertEqual(self._log(), [('staff_booked', 'bob', None, 'staff', 'Tack för att du hoppar in!')])
        self.assertEqual([m.to for m in mail.outbox], [['bob@example.com']])
        self.assertIn('staff i styrelsen har bokat dig som värd', mail.outbox[0].body)
        self.assertIn('Tack för att du hoppar in!', mail.outbox[0].body)
        self.assertContains(response, 'bob är nu värd för')

    def test_unticking_mejla_sends_nothing(self):
        from django.core import mail
        self._book_for(self.bob, notify='')
        self.assertEqual(mail.outbox, [])

    def test_changing_host_tells_both(self):
        from django.core import mail
        self._book(self.evening, self.tuesday, self.alice)
        self._book_for(self.bob)
        self.assertEqual(self._slot(self.evening, self.tuesday).host, self.bob)
        self.assertEqual(self._log(), [('staff_reassigned', 'bob', 'alice', 'staff', '')])
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['alice@example.com', 'bob@example.com'])

    def test_removing_a_booking_with_a_reason(self):
        from django.core import mail
        self._book(self.evening, self.tuesday, self.alice)
        self._post(self.evening, self.tuesday, action='remove', after='-1', notify='on', note='Sjuk')
        slot = self._slot(self.evening, self.tuesday)
        self.assertEqual((slot.status, slot.host), (-1, None))
        self.assertEqual(self._log(), [('staff_removed', 'alice', None, 'staff', 'Sjuk')])
        self.assertIn('Anledning: Sjuk', mail.outbox[0].body)

    def test_removing_can_leave_the_slot_free(self):
        self._book(self.evening, self.tuesday, self.alice)
        self._post(self.evening, self.tuesday, action='remove', after='0', notify='')
        self.assertEqual(self._slot(self.evening, self.tuesday).status, 0)

    def test_a_locked_slot_needs_the_override(self):
        response = self._book_for(self.bob, event=self.afternoon)
        self.assertEqual(self._slot(self.afternoon, self.tuesday).status, 0)
        self.assertContains(response, 'Passet är låst')
        self._book_for(self.bob, event=self.afternoon, override='on')
        self.assertEqual(self._slot(self.afternoon, self.tuesday).host, self.bob)

    def test_a_closure_refuses_even_staff(self):
        from .models import CalendarEntry
        CalendarEntry.objects.create(title="Golvet lackas", kind="closed", all_day=True,
                                     start=self._at(self.tuesday, 0),
                                     end=self._at(self.tuesday + datetime.timedelta(days=1), 0))
        response = self._book_for(self.bob, override='on')
        self.assertEqual(self._slot(self.evening, self.tuesday).status, 0)
        self.assertContains(response, 'Lokalen är stängd då')

    def test_the_page_lists_period_hosts_with_their_progress(self):
        self.client.force_login(self.staff)
        page = self.client.get(self._url(self.evening, self.tuesday))
        self.assertContains(page, 'Ingen värd')
        self.assertContains(page, 'alice – 0 av 6')
        self.assertContains(page, 'Övriga medlemmar')

    def test_members_cannot_use_it_and_only_staff_see_the_link(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(self._url(self.evening, self.tuesday)).status_code, 403)
        refused = self.client.post(self._url(self.evening, self.tuesday),
                                   {'action': 'book', 'host': self.bob.pk})
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(self._slot(self.evening, self.tuesday).status, 0)
        iso = self.tuesday.isocalendar()
        week = '/scheduler/week/{}/{}/'.format(iso.year, iso.week)
        self.client.force_login(self.bob)
        self.assertNotContains(self.client.get(week), '>Hantera<')
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(week), '>Hantera<')


class SpecialPassFixture:
    """An "Introkväll" type (18-21, three hosts) and a staff member."""

    def setUp(self):
        super().setUp()
        from .models import SpecialPassType
        self.staff = make_staff()
        self.intro = SpecialPassType.objects.create(
            name="Introkväll", description="Vi visar nya medlemmar runt.",
            start_time=datetime.time(18), end_time=datetime.time(21), hosts_needed=3)
        self.pass_day = timezone.localdate() + datetime.timedelta(days=9)

    def _pass_form(self, **fields):
        data = {'title': 'Introkväll', 'kind': 'special', 'description': '',
                'date': self.pass_day.isoformat(), 'start_time': '18:00', 'end_time': '21:00',
                'end_date': '', 'calendar': '', 'special_type': str(self.intro.pk), 'hosts_needed': '3'}
        data.update(fields)
        return data


class SpecialPassTypeTests(SpecialPassFixture, TestCase):
    """Standard types of special pass, and creating passes from them."""

    def test_staff_can_create_a_type_and_members_cannot_see_the_page(self):
        from .models import SpecialPassType
        self.client.force_login(self.staff)
        self.client.post('/scheduler/specialpass/type/new/', {
            'name': 'Medlemskväll', 'description': '', 'start_time': '17:00', 'end_time': '20:00',
            'hosts_needed': '2', 'counts_as_session': 'on', 'blocks_slots': 'on', 'is_active': 'on'})
        self.assertTrue(SpecialPassType.objects.filter(name='Medlemskväll', hosts_needed=2).exists())
        self.assertContains(self.client.get('/scheduler/specialpass/'), 'Medlemskväll')
        member = User.objects.create(email="m@example.com", slackId="m", is_active=True)
        self.client.force_login(member)
        self.assertEqual(self.client.get('/scheduler/specialpass/').status_code, 403)

    def test_a_type_must_end_after_it_starts(self):
        self.client.force_login(self.staff)
        response = self.client.post('/scheduler/specialpass/type/new/', {
            'name': 'Fel', 'start_time': '20:00', 'end_time': '19:00', 'hosts_needed': '2'})
        self.assertContains(response, 'Slutet måste vara efter starten')

    def test_a_new_pass_is_prefilled_from_its_type(self):
        self.client.force_login(self.staff)
        form = self.client.get('/scheduler/entries/new/?type={}&date={}'.format(
            self.intro.pk, self.pass_day.isoformat())).context['form']
        self.assertEqual((form.initial['kind'], form.initial['title'], form.initial['hosts_needed']),
                         ('special', 'Introkväll', 3))
        self.assertEqual(form.initial['start_time'], datetime.time(18))
        self.assertEqual(form.initial['date'], self.pass_day)

    def test_saving_a_special_pass(self):
        from .models import CalendarEntry
        self.client.force_login(self.staff)
        self.client.post('/scheduler/entries/new/', self._pass_form(hosts_needed='4'))
        entry = CalendarEntry.objects.get()
        self.assertEqual((entry.kind, entry.special_type, entry.hosts_needed), ('special', self.intro, 4))
        self.assertTrue(entry.blocks_slots and entry.counts_as_session)

    def test_a_special_pass_needs_a_type_hosts_and_a_time(self):
        self.client.force_login(self.staff)
        response = self.client.post('/scheduler/entries/new/', self._pass_form(
            special_type='', hosts_needed='0', start_time='', end_time=''))
        for message in ('Välj vilken sorts specialpass', 'minst en värd', 'Ett specialpass behöver en tid'):
            self.assertContains(response, message)

    def test_other_kinds_drop_the_special_fields(self):
        from .models import CalendarEntry
        self.client.force_login(self.staff)
        self.client.post('/scheduler/entries/new/', self._pass_form(kind='course', title='Kurs'))
        entry = CalendarEntry.objects.get()
        self.assertEqual((entry.special_type, entry.hosts_needed), (None, 0))

    def test_hosts_needed_cannot_drop_below_the_seats_taken(self):
        from .models import CalendarEntry, EntryHost
        self.client.force_login(self.staff)
        self.client.post('/scheduler/entries/new/', self._pass_form())
        entry = CalendarEntry.objects.get()
        for slack in ('a', 'b'):
            EntryHost.objects.create(entry=entry, user=User.objects.create(
                email=slack + "@example.com", slackId=slack, is_active=True))
        response = self.client.post('/scheduler/entries/{}/edit/'.format(entry.pk), self._pass_form(hosts_needed='1'))
        self.assertContains(response, '2 värdar har redan bokat passet')


class SpecialPassSignupTests(SpecialPassFixture, SlotsFixture, TestCase):
    """Hosts taking seats on a special pass, and the pass replacing regular slots."""

    def setUp(self):
        super().setUp()
        from .models import CalendarEntry
        self.carl = User.objects.create(email="carl@example.com", slackId="carl", is_active=True)
        self.pass_ = CalendarEntry.objects.create(
            title="Introkväll", kind="special", special_type=self.intro, hosts_needed=2,
            start=self._at(self.tuesday, 18), end=self._at(self.tuesday, 21))

    def _seat(self, user, entry=None):
        self.client.force_login(user)
        return self.client.post('/scheduler/entries/{}/seat/'.format((entry or self.pass_).pk), follow=True)

    def _messages(self, response):
        return " ".join(str(m) for m in response.context['messages'])

    def test_hosts_take_seats_until_it_is_full_and_can_give_them_back(self):
        from .models import BookingChange
        self._seat(self.alice)
        self._seat(self.bob)
        full = self._seat(self.carl)
        self.assertIn('har redan alla 2 värdar', self._messages(full))
        self.assertEqual([u.slackId for u in self.pass_.seat_hosts()], ['alice', 'bob'])
        self._seat(self.alice)  # asks for rebooking: still hers until someone takes it over
        self.assertEqual([u.slackId for u in self.pass_.seat_hosts()], ['alice', 'bob'])
        self.assertEqual([(c.action, c.host.slackId) for c in BookingChange.objects.filter(entry=self.pass_).order_by('pk')],
                         [('booked', 'alice'), ('booked', 'bob'), ('cancelled', 'alice')])

    def test_a_handed_over_seat_is_taken_over_and_never_removed_by_the_host(self):
        from .models import BookingChange, EntryHost
        from .entry_sync import hosts_line
        self._seat(self.alice)
        self._seat(self.bob)
        handed = self._seat(self.alice)
        self.assertIn('Du har önskat ombokning av din plats', self._messages(handed))
        seat = EntryHost.objects.get(entry=self.pass_, user=self.alice)
        self.assertIsNotNone(seat.handed_over_at)  # still there, still hers
        self.assertEqual(hosts_line(self.pass_), 'Värdar: alice, bob')  # Google: nothing changes
        self.assertEqual(self.pass_.seat_situation(self.carl)['action'], 'take_over')
        taken = self._seat(self.carl)
        self.assertIn('Du tog över alices plats', self._messages(taken))
        self.assertEqual(sorted(s.user.slackId for s in self.pass_.seats.all()), ['bob', 'carl'])
        self.assertFalse(self.pass_.seats.filter(handed_over_at__isnull=False).exists())
        last = BookingChange.objects.filter(entry=self.pass_).order_by('-pk').first()
        self.assertEqual((last.host, last.previous_host), (self.carl, self.alice))

    def test_the_host_can_take_a_handed_over_seat_back(self):
        self._seat(self.alice)
        self._seat(self.alice)
        self.assertIn('Du har tagit tillbaka', self._messages(self._seat(self.alice)))
        self.assertEqual([u.slackId for u in self.pass_.seat_hosts()], ['alice'])

    def test_a_seat_counts_for_whoever_holds_it(self):
        from .host_progress import progress_for
        period = SchedulingPeriod.objects.get()
        self.pass_.hosts_needed = 1
        self.pass_.save()
        self._seat(self.alice)
        self._seat(self.alice)  # asks for rebooking: still counts for her
        self.assertEqual(progress_for(period, [self.alice])[0].host_committed, 1)
        self._seat(self.carl)  # takes it over
        self.assertEqual([p.host_committed for p in progress_for(period, [self.alice, self.carl])], [0, 1])

    def test_a_pass_that_has_started_cannot_be_changed(self):
        self.pass_.start = timezone.now() - datetime.timedelta(minutes=5)
        self.pass_.end = timezone.now() + datetime.timedelta(hours=2)
        self.pass_.save()
        self.assertIn('har redan börjat', self._messages(self._seat(self.alice)))
        self.assertEqual(self.pass_.seats.count(), 0)

    def test_a_closure_over_the_pass_stops_signups(self):
        from .models import CalendarEntry
        CalendarEntry.objects.create(title="Golvet lackas", kind="closed", all_day=True,
                                     start=self._at(self.tuesday, 0),
                                     end=self._at(self.tuesday + datetime.timedelta(days=1), 0))
        self.assertIn('lokalen är stängd: Golvet lackas', self._messages(self._seat(self.alice)))

    def test_the_week_view_shows_seats_and_the_right_button(self):
        self._seat(self.alice)
        iso = self.tuesday.isocalendar()
        week = '/scheduler/week/{}/{}/'.format(iso.year, iso.week)
        self.client.force_login(self.alice)
        mine = self.client.get(week)
        self.assertContains(mine, '<b>1 av 2</b> värdar: alice')
        self.assertContains(mine, 'Du är värd')
        self.assertContains(mine, '>Kan inte – lämna över<')
        self.client.force_login(self.bob)
        self.assertContains(self.client.get(week), '>Boka plats<')

    def test_it_replaces_the_regular_slot_it_overlaps(self):
        slots, response = self._states(self.alice, self.tuesday)
        self.assertEqual(slots["Vardag 18-21"].state, 'replaced')
        self.assertEqual(slots["Vardag 18-21"].hint, 'Introkväll istället')
        self.assertEqual(slots["Vardag 15-18"].state, 'locked')  # its evening has no host
        refused = self._toggle(self.bob, self.evening, self.tuesday)
        self.assertIn('då är det Introkväll istället', self._messages(refused))
        self.assertFalse(EventInstance.objects.filter(status=1).exists())

    def test_a_type_can_leave_regular_slots_alone(self):
        self.intro.blocks_slots = False
        self.intro.save()
        slots, _ = self._states(self.alice, self.tuesday)
        self.assertEqual(slots["Vardag 18-21"].state, 'free')

    def _states(self, user, day):
        self.client.force_login(user)
        iso = day.isocalendar()
        response = self.client.get('/scheduler/week/{}/{}/'.format(iso.year, iso.week))
        found = next(d for d in response.context['days'] if d['date'] == day)
        return {s.event.name: s for s in found['slots']}, response


class SpecialPassGoogleTests(SpecialPassFixture, LocalCalendarMixin, TestCase):
    """A special pass in Google lists its hosts, and follows every seat change."""

    def test_the_google_event_lists_hosts_and_free_seats(self):
        from .models import CalendarEntry
        from . import entry_sync
        calendar = SchedulingCalendar.objects.create(
            name="Öppettider", google_calendar_id="main@group", service_account_username="s",
            timezone="Europe/Stockholm")
        start = timezone.make_aware(datetime.datetime.combine(self.pass_day, datetime.time(18)))
        entry = CalendarEntry.objects.create(title="Introkväll", kind="special", special_type=self.intro,
                                             hosts_needed=3, start=start, end=start + datetime.timedelta(hours=3),
                                             calendar=calendar, description="Välkommen!")
        entry_sync.push(entry)
        event = self.google_events(calendar)[entry.google_event_id]
        self.assertEqual(event['colorId'], '5')
        self.assertEqual(event['description'], 'Välkommen!')  # no "hosts wanted" in public
        host = User.objects.create(email="h@example.com", slackId="hanna", is_active=True)
        self.client.force_login(host)
        self.client.post('/scheduler/entries/{}/seat/'.format(entry.pk))
        event = self.google_events(calendar)[entry.google_event_id]
        self.assertEqual(event['description'], 'Värdar: hanna\nVälkommen!')


class SpecialPassCountingTests(SpecialPassFixture, TestCase):
    """A seat on a special pass counts as a hosted slot, wherever slots are counted."""

    def setUp(self):
        super().setUp()
        from .models import CalendarEntry, EntryHost, SpecialPassType
        today = timezone.localdate()
        self.period = SchedulingPeriod.objects.create(
            start=today - datetime.timedelta(days=50), end=today + datetime.timedelta(days=50),
            num_required_events=6, participant_key_string="")
        self.host = User.objects.create(email="h@example.com", slackId="hanna", is_active=True)
        self.period.hosts.add(self.host)
        social = SpecialPassType.objects.create(name="Fika", start_time=datetime.time(15),
                                                end_time=datetime.time(16), hosts_needed=2,
                                                counts_as_session=False)

        def seat(days, special_type=None, hosts_needed=3):
            start = timezone.now().replace(microsecond=0) + datetime.timedelta(days=days)
            entry = CalendarEntry.objects.create(
                title=(special_type or self.intro).name, kind="special", special_type=special_type or self.intro,
                hosts_needed=hosts_needed, start=start, end=start + datetime.timedelta(hours=3))
            EntryHost.objects.create(entry=entry, user=self.host)
            return entry
        self.ahead = seat(3)              # counts, booked
        seat(-3)                          # counts, done
        seat(5, special_type=social)      # type does not count
        seat(80)                          # outside the period

    def test_progress_counts_seats_as_hosted_slots(self):
        from .host_progress import progress_for
        progress = progress_for(self.period, [self.host])[0]
        self.assertEqual((progress.host_done, progress.host_booked), (1, 1))
        self.assertEqual(progress.next_slot, self.ahead)

    def test_the_profile_page_and_the_hosts_list_count_them_too(self):
        self.client.force_login(self.host)
        stats = self.client.get('/scheduler/profile').context['host_stats']
        self.assertEqual((stats['host_done'], stats['host_booked']), (1, 1))
        self.assertEqual(self.period.get_host_count_key_list()['hanna'], '2')

    def test_follow_up_and_reminders_point_at_passes_that_need_hosts(self):
        from .host_progress import progress_for
        from .reminders import bookable_soon, compose, special_passes_soon
        self.client.force_login(self.staff)
        page = self.client.get('/scheduler/followup/{}/'.format(self.period.pk))
        self.assertContains(page, 'Specialpass som saknar värdar')
        self.assertContains(page, '2 kvar')
        _, body = compose(progress_for(self.period, [self.host])[0], 'https://example.org',
                          bookable_soon(), special_passes_soon())
        self.assertIn('Specialpass som behöver fler värdar:', body)
        self.assertIn('Introkväll (2 platser kvar, räknas som värdpass)', body)


class SpecialPassStaffTests(SpecialPassFixture, LocalCalendarMixin, TestCase):
    """Staff adding and removing hosts on a special pass, and the calendar check of its host list."""

    def setUp(self):
        super().setUp()
        from .models import CalendarEntry
        from . import entry_sync
        self.calendar = SchedulingCalendar.objects.create(
            name="Öppettider", google_calendar_id="main@group", service_account_username="s",
            timezone="Europe/Stockholm")
        start = timezone.make_aware(datetime.datetime.combine(self.pass_day, datetime.time(18)))
        self.entry = CalendarEntry.objects.create(
            title="Introkväll", kind="special", special_type=self.intro, hosts_needed=1,
            start=start, end=start + datetime.timedelta(hours=3), calendar=self.calendar)
        entry_sync.push(self.entry)
        self.hanna = User.objects.create(email="hanna@example.com", slackId="hanna", is_active=True)
        self.url = '/scheduler/entries/{}/seats/'.format(self.entry.pk)

    def _google_hosts(self):
        self.entry.refresh_from_db()
        return self.google_events(self.calendar)[self.entry.google_event_id]['description'].split('\n')[0]

    def test_staff_add_a_host_who_is_told_and_shown_in_google(self):
        from django.core import mail
        from .models import BookingChange
        self.client.force_login(self.staff)
        self.client.post(self.url, {'action': 'add', 'host': self.hanna.pk, 'note': 'Tack!', 'notify': 'on'})
        self.assertEqual(self.entry.seat_hosts(), [self.hanna])
        self.assertEqual(self._google_hosts(), 'Värdar: hanna')
        self.assertEqual(mail.outbox[0].to, ['hanna@example.com'])
        self.assertIn('har bokat dig som värd på Introkväll', mail.outbox[0].body)
        self.assertEqual(BookingChange.objects.get(entry=self.entry).action, 'staff_booked')

    def test_staff_remove_a_host_with_a_reason(self):
        from django.core import mail
        from .models import EntryHost
        EntryHost.objects.create(entry=self.entry, user=self.hanna)
        self.client.force_login(self.staff)
        self.client.post(self.url, {'action': 'remove', 'user': self.hanna.pk, 'note': 'Krock', 'notify': 'on'})
        self.assertEqual(self.entry.seats.count(), 0)
        self.assertEqual(self._google_hosts(), '')
        self.assertIn('Anledning: Krock', mail.outbox[0].body)

    def test_a_full_pass_takes_no_more_and_members_cannot_manage_seats(self):
        from .models import EntryHost
        EntryHost.objects.create(entry=self.entry, user=self.hanna)
        other = User.objects.create(email="o@example.com", slackId="olle", is_active=True)
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {'action': 'add', 'host': other.pk}, follow=True)
        self.assertContains(response, 'Passet har redan alla 1 värdar')
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url, {'action': 'remove', 'user': self.hanna.pk}).status_code, 403)
        self.assertEqual(self.entry.seats.count(), 1)

    def test_the_calendar_check_catches_an_edited_host_list_and_fixes_it(self):
        from .google_calendar import client_for
        from . import calendar_check
        event = self.google_events(self.calendar)[self.entry.google_event_id]
        event['description'] = 'Värdar: pelle'
        client_for(self.calendar).update(event['id'], event)
        check = calendar_check.run(self.calendar)
        (difference,) = check.differences
        self.assertEqual((difference['kind'], difference['google']), ('wrong_host', 'Värdar: pelle'))
        calendar_check.fix_in_check(check, {difference['key']})
        self.assertEqual(self._google_hosts(), '')


class LogoutTests(TestCase):
    def test_the_menu_logs_out_with_a_post(self):
        user = make_staff()
        self.client.force_login(user)
        page = self.client.get('/scheduler/joinEvent')  # any page with the menu
        self.assertContains(page, 'action="/accounts/logout/"')
        self.client.post('/accounts/logout/')
        self.assertNotIn('_auth_user_id', self.client.session)


class MenuTests(TestCase):
    """The grouped left-hand menu (scheduler/menu.py)."""

    def setUp(self):
        self.member = User.objects.create(email="m@example.com", slackId="m", is_active=True)
        self.staff = make_staff()

    def _menu(self, user, url):
        if user:
            self.client.force_login(user)
        return self.client.get(url).context['menu']

    @staticmethod
    def _labels(menu):
        return [(s.get('heading'), [i['label'] for i in s['items']]) for s in menu]

    @staticmethod
    def _active(menu):
        return [i['label'] for s in menu for i in s['items'] if i['active']]

    def test_members_see_three_items_and_the_links_out(self):
        menu = self._menu(self.member, '/scheduler/week/')
        self.assertEqual(self._labels(menu), [
            (None, ['Boka pass', 'Anmäl deltagande', 'Min profil']),
            (None, ['Schema', 'Hjälp', 'Logga ut'])])
        self.assertEqual(self._active(menu), ['Boka pass'])

    def test_both_booking_views_are_boka_pass(self):
        self.assertEqual(self._active(self._menu(self.member, '/scheduler/')), ['Boka pass'])

    def test_staff_get_styrelsen_and_a_collapsed_settings_group(self):
        menu = self._menu(self.staff, '/scheduler/followup/')
        self.assertEqual([heading for heading, _ in self._labels(menu)],
                         [None, 'Styrelsen', 'Inställningar', None])
        settings_group = menu[2]
        self.assertFalse(settings_group['open'])
        self.assertEqual(self._active(menu), ['Uppföljning'])

    def test_the_settings_group_opens_on_its_own_pages(self):
        menu = self._menu(self.staff, '/scheduler/periods')
        self.assertTrue(menu[2]['open'])
        self.assertEqual(self._active(menu), ['Perioder'])

    def test_kalenderkontroll_does_not_light_up_kalendrar(self):
        self.assertEqual(self._active(self._menu(self.staff, '/scheduler/calendar-check/')), ['Kalenderkontroll'])
        self.assertEqual(self._active(self._menu(self.staff, '/scheduler/calendars')), ['Kalendrar'])

    def test_visitors_only_get_the_links_out(self):
        menu = self._menu(None, '/accounts/login/')
        self.assertEqual(self._labels(menu), [(None, ['Schema', 'Hjälp'])])


class BookingListTests(SpecialPassFixture, SlotsFixture, TestCase):
    """The list of a whole period, built from the same cards as the week view."""

    def setUp(self):
        super().setUp()
        self.period = SchedulingPeriod.objects.get()
        # Not Monday: migration 0002 adds a "Måndagar" event.
        self.wednesday = self.monday + datetime.timedelta(days=2)
        self.thursday = self.monday + datetime.timedelta(days=3)

    def _list(self, user, **params):
        self.client.force_login(user)
        return self.client.get('/scheduler/list/', params)

    def _items(self, response, day):
        """What the list shows on `day`: slot names (with their state) and entry titles."""
        for week in response.context['weeks']:
            for found in week['days']:
                if found['date'] == day:
                    return [item['entry'].title if 'entry' in item
                            else '{} {}'.format(item['slot'].event.name, item['slot'].state)
                            for item in found['items']]
        return None

    def test_slots_have_the_same_states_and_hints_as_in_the_week_view(self):
        self._book(self.evening, self.wednesday, self.bob)
        response = self._list(self.alice)
        self.assertEqual(self._items(response, self.wednesday), ['Vardag 15-18 free', 'Vardag 18-21 taken'])
        self.assertEqual(self._items(response, self.tuesday), ['Vardag 15-18 locked', 'Vardag 18-21 free'])
        self.assertContains(response, 'Öppnas när Vardag 18-21 är bokat')
        self.assertContains(response, 'class="slot slot-locked"')

    def test_lediga_keeps_what_can_be_booked_and_mina_what_is_yours(self):
        self._book(self.evening, self.wednesday, self.bob)
        self._book(self.evening, self.tuesday, self.alice)
        free = self._list(self.alice, visa='lediga')
        self.assertEqual(self._items(free, self.wednesday), ['Vardag 15-18 free'])
        self.assertEqual(self._items(free, self.tuesday), ['Vardag 15-18 free'])
        # A locked slot stays in "Lediga": it opens once its evening has a host.
        self.assertEqual(self._items(free, self.thursday), ['Vardag 15-18 locked', 'Vardag 18-21 free'])
        mine = self._list(self.alice, visa='mina')
        self.assertEqual([d['date'] for w in mine.context['weeks'] for d in w['days']], [self.tuesday])
        self.assertEqual(self._items(mine, self.tuesday), ['Vardag 18-21 mine'])
        self.assertContains(mine, 'Mina (1)')

    def test_special_passes_are_in_the_list_and_count_as_free_seats(self):
        from .models import CalendarEntry
        CalendarEntry.objects.create(
            title="Introkväll", kind="special", special_type=self.intro, hosts_needed=2,
            start=self._at(self.tuesday, 18), end=self._at(self.tuesday, 21))
        response = self._list(self.alice, visa='lediga')
        self.assertEqual(self._items(response, self.tuesday), ['Vardag 15-18 locked', 'Introkväll'])
        self.assertContains(response, '>Boka plats<')

    def test_booking_from_the_list_comes_back_to_the_same_day(self):
        response = self._list(self.alice, period=self.period.pk, visa='lediga')
        back = '/scheduler/list/?period={}&visa=lediga'.format(self.period.pk)
        self.assertContains(response, 'name="next" value="{}#d-{}"'.format(
            back.replace('&', '&amp;'), self.tuesday.isoformat()))
        booked = self.client.post('/scheduler/week/toggle', {
            'event': self.evening.pk, 'start': self._slot(self.evening, self.tuesday).start.isoformat(),
            'next': back + '#d-' + self.tuesday.isoformat()})
        self.assertRedirects(booked, back + '#d-' + self.tuesday.isoformat(), fetch_redirect_response=False)
        self.assertEqual(self._slot(self.evening, self.tuesday).host, self.alice)

    def test_a_next_page_on_another_site_is_ignored(self):
        self.client.force_login(self.alice)
        booked = self.client.post('/scheduler/week/toggle', {
            'event': self.evening.pk, 'start': self._slot(self.evening, self.tuesday).start.isoformat(),
            'next': 'https://example.net/'})
        iso = self.tuesday.isocalendar()
        self.assertRedirects(booked, '/scheduler/week/{}/{}/'.format(iso.year, iso.week),
                             fetch_redirect_response=False)

    def test_days_already_gone_are_hidden_until_asked_for(self):
        today = timezone.localdate()
        self.period.start = today - datetime.timedelta(days=10)
        self.period.save()
        response = self._list(self.alice)
        shown = [d['date'] for w in response.context['weeks'] for d in w['days']]
        self.assertTrue(shown)
        self.assertTrue(all(day >= today for day in shown))
        self.assertContains(response, 'Visa tidigare dagar i perioden')
        earlier = self._list(self.alice, tidigare='1')
        self.assertIsNone(earlier.context['earlier_url'])
        self.assertContains(earlier, 'Dölj dagar som har passerat')

    def test_the_picker_rows_jump_to_the_weeks_in_the_list(self):
        response = self._list(self.alice)
        iso = self.tuesday.isocalendar()
        anchor = 'v{}-{}'.format(iso.year, iso.week)
        self.assertContains(response, 'id="{}"'.format(anchor))
        self.assertContains(response, '#{}"'.format(anchor))

    def test_the_start_page_and_the_old_signup_address_show_the_list(self):
        for url in ('/scheduler/', '/scheduler/signup'):
            self.client.force_login(self.alice)
            self.assertTemplateUsed(self.client.get(url), 'booking_list.html')

    def test_booked_slots_cost_no_extra_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def count():
            self._list(self.alice, tidigare='1')  # warm-up
            with CaptureQueriesContext(connection) as queries:
                self._list(self.alice, tidigare='1')
            return len(queries)
        before = count()
        hosts = [User.objects.create(email="h{}@example.com".format(n), slackId="h{}".format(n), is_active=True)
                 for n in range(10)]
        for n, host in enumerate(hosts):
            self._book(self.evening, self.monday + datetime.timedelta(days=n + 1), host)
        self.assertEqual(count(), before)

    def test_a_booked_slot_is_found_from_later_the_same_day(self):
        # get_event_list() starts the first day at the slot's own time, before `from_date`.
        self._book(self.afternoon, self.tuesday, self.bob)
        found = self.afternoon.get_events(self._at(self.tuesday, 20), self._at(self.tuesday, 23))
        self.assertEqual([(i.status, i.host) for i in found], [(1, self.bob)])

    def test_the_week_view_and_the_list_link_to_each_other(self):
        self.client.force_login(self.alice)
        iso = self.tuesday.isocalendar()
        week = self.client.get('/scheduler/week/{}/{}/'.format(iso.year, iso.week))
        self.assertContains(week, 'href="/scheduler/list/?period={}#v{}-{}">Lista<'.format(
            self.period.pk, iso.year, iso.week))
        self.assertContains(self._list(self.alice), '>Vecka<')


class JustChangedTests(SpecialPassFixture, SlotsFixture, TestCase):
    """The page you come back to keeps what you just booked or cancelled in view."""

    def _list(self, **params):
        response = self.client.get('/scheduler/list/', params)
        return {('{} {}'.format(item['slot'].event.name, item['slot'].state) if 'slot' in item
                 else item['entry'].title): getattr(item.get('slot') or item.get('entry'), 'just_changed', False)
                for week in response.context['weeks'] for day in week['days']
                if day['date'] == self.tuesday for item in day['items']}, response

    def _toggle_from_list(self, user, event, day):
        """Press the slot's button in the list, which comes back to the list."""
        self.client.force_login(user)
        self.client.post('/scheduler/week/toggle', {
            'event': event.pk, 'start': self._slot(event, day).start.isoformat(),
            'next': '/scheduler/list/'})

    def test_a_slot_booked_from_lediga_stays_there_highlighted_once(self):
        self._toggle_from_list(self.alice, self.evening, self.tuesday)
        items, response = self._list(visa='lediga')
        self.assertEqual(items, {'Vardag 15-18 free': False, 'Vardag 18-21 mine': True})
        self.assertContains(response, 'slot-mine just-changed')
        items, _ = self._list(visa='lediga')  # only on the page right after
        self.assertEqual(items, {'Vardag 15-18 free': False})

    def test_a_handed_over_slot_stays_in_mina(self):
        self._book(self.evening, self.tuesday, self.alice)
        self._toggle_from_list(self.alice, self.evening, self.tuesday)
        items, _ = self._list(visa='mina')
        self.assertEqual(items, {'Vardag 18-21 handed': True})

    def test_the_week_view_highlights_it_too(self):
        response = self._toggle(self.alice, self.evening, self.tuesday)
        self.assertContains(response, 'slot-mine just-changed')

    def test_a_special_pass_seat_taken_from_lediga_stays_there(self):
        from .models import CalendarEntry
        entry = CalendarEntry.objects.create(
            title="Introkväll", kind="special", special_type=self.intro, hosts_needed=1,
            start=self._at(self.tuesday, 18), end=self._at(self.tuesday, 21))
        self.client.force_login(self.alice)
        self.client.post('/scheduler/entries/{}/seat/'.format(entry.pk))
        items, _ = self._list(visa='lediga')
        self.assertEqual(items['Introkväll'], True)


class GeneratedPeriodTests(TestCase):
    """Generated occurrences get the same period from the in-memory lookup as from the database."""

    def setUp(self):
        from .models import SchedulingRule
        self.tz = timezone.get_current_timezone()
        template = EventTemplate.objects.create(name="Öppet", title="Öppet hus", synchronize=False)
        self.daily = SchedulingRule.objects.create(name="Dagligen", description="", frequency="DAILY", params="")
        year = timezone.localdate().year + 1
        day = lambda month, d: datetime.date(year, month, d)
        SchedulingPeriod.objects.create(start=day(1, 1), end=day(3, 31))
        SchedulingPeriod.objects.create(start=day(4, 1), end=day(6, 30))
        SchedulingPeriod.objects.create(start=day(5, 1), end=day(5, 31))  # overlaps: May has two periods
        first = datetime.datetime.combine(day(1, 1), datetime.time(22), self.tz)
        # 22:00-01:00 crosses midnight, so the last evening of a period belongs to none.
        self.late = Event.objects.create(name="Sent", template=template, rule=self.daily,
                                         start=first, end=first + datetime.timedelta(hours=3))
        self.early = Event.objects.create(name="Tidigt", template=template, rule=self.daily,
                                          start=first.replace(hour=0, minute=30),
                                          end=first.replace(hour=2))
        self.year = year

    def test_the_lookup_matches_the_query_for_every_occurrence(self):
        start = datetime.datetime(self.year - 1, 12, 20, tzinfo=self.tz)
        end = datetime.datetime(self.year, 7, 10, tzinfo=self.tz)
        periods = list(SchedulingPeriod.objects.all())
        checked = 0
        for event in (self.late, self.early):
            for when in self.daily.get_events(event.start, end):
                duration = event.end - event.start
                from_db = event.create_eventinstance(when, when + duration).period
                in_memory = event.create_eventinstance(when, when + duration, periods=periods).period
                self.assertEqual(in_memory, from_db, (event.name, when))
                checked += 1
        self.assertGreater(checked, 300)
        # And the list as a whole: an occurrence needs exactly one period covering all of it.
        names = {(i.event.name, timezone.localtime(i.start).date()): i.period.start.month
                 for e in (self.late, self.early) for i in e.get_event_list(start, end)}
        self.assertEqual(names[("Sent", datetime.date(self.year, 3, 30))], 1)
        self.assertNotIn(("Sent", datetime.date(self.year, 3, 31)), names)  # runs into April
        self.assertEqual(names[("Tidigt", datetime.date(self.year, 3, 31))], 1)
        self.assertNotIn(("Tidigt", datetime.date(self.year, 5, 15)), names)  # two periods
        self.assertEqual(names[("Sent", datetime.date(self.year, 5, 31))], 4)  # runs out of the May one

    def test_the_list_page_needs_a_fixed_number_of_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        user = User.objects.create(email="a@example.com", slackId="a", is_active=True)
        self.client.force_login(user)
        winter = SchedulingPeriod.objects.get(start=datetime.date(self.year, 1, 1))
        self.client.get('/scheduler/list/', {'period': winter.pk})  # warm-up
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/scheduler/list/', {'period': winter.pk, 'tidigare': '1'})
        slots = sum(1 for w in response.context['weeks'] for d in w['days'] for item in d['items'] if 'slot' in item)
        self.assertGreater(slots, 150)
        self.assertLess(len(queries), 40, "\n".join(q['sql'][:120] for q in queries.captured_queries))


class HandOverTests(SlotsFixture, TestCase):
    """A host never unbooks: they hand a slot over, and another host takes it over."""

    def _states(self, user, day):
        self.client.force_login(user)
        iso = day.isocalendar()
        response = self.client.get('/scheduler/week/{}/{}/'.format(iso.year, iso.week))
        found = next(d for d in response.context['days'] if d['date'] == day)
        return {s.event.name: s for s in found['slots']}, response

    def _messages(self, response):
        return " ".join(str(m) for m in response.context['messages'])

    def test_handing_over_keeps_the_host_and_asks_for_someone_else(self):
        self._book(self.evening, self.tuesday, self.alice)
        mine, page = self._states(self.alice, self.tuesday)
        self.assertEqual(mine["Vardag 18-21"].action, 'Kan inte – lämna över')
        self.assertContains(page, "onsubmit=\"return confirm(")
        response = self._toggle(self.alice, self.evening, self.tuesday)
        self.assertIn('Du har önskat ombokning', self._messages(response))
        slot = self._slot(self.evening, self.tuesday)
        self.assertEqual((slot.status, slot.host), (-1, self.alice))
        mine, _ = self._states(self.alice, self.tuesday)
        self.assertEqual((mine["Vardag 18-21"].state, mine["Vardag 18-21"].action), ('handed', 'Ta tillbaka'))
        others, _ = self._states(self.bob, self.tuesday)
        self.assertEqual((others["Vardag 18-21"].state, others["Vardag 18-21"].hint, others["Vardag 18-21"].action),
                         ('rebook', 'alice kan inte', 'Ta över'))

    def test_another_host_takes_it_over(self):
        from .models import BookingChange
        self._book(self.evening, self.tuesday, self.alice)
        self._toggle(self.alice, self.evening, self.tuesday)
        response = self._toggle(self.bob, self.evening, self.tuesday)
        self.assertIn('Du tog över', self._messages(response))
        self.assertIn('från alice', self._messages(response))
        slot = self._slot(self.evening, self.tuesday)
        self.assertEqual((slot.status, slot.host), (1, self.bob))
        last = BookingChange.objects.order_by('-at', '-pk').first()
        self.assertEqual((last.action, last.host, last.previous_host), ('booked', self.bob, self.alice))

    def test_the_host_can_take_it_back(self):
        self._book(self.evening, self.tuesday, self.alice)
        self._toggle(self.alice, self.evening, self.tuesday)
        response = self._toggle(self.alice, self.evening, self.tuesday)
        self.assertIn('Du har tagit tillbaka', self._messages(response))
        self.assertEqual(self._slot(self.evening, self.tuesday).status, 1)


class BookingRuleOptionTests(SlotsFixture, TestCase):
    """The options a booking rule can have, on the fixture's "afternoon needs the evening" rule."""

    def _rule(self, **fields):
        for name, value in fields.items():
            setattr(self.rule, name, value)
        self.rule.save()

    def test_the_rule_reads_as_a_sentence(self):
        self.assertEqual(self.rule.sentence(),
                         "Vardag 15-18 kan bara bokas när Vardag 18-21 är bokat samma dag. Gäller måndag–fredag.")
        self._rule(day_offset=-1, same_host=True, weekdays="5,6")
        self.assertEqual(self.rule.sentence(with_period=True),
                         "Vardag 15-18 kan bara bokas när Vardag 18-21 är bokat dagen före av samma värd. "
                         "Gäller lördag och söndag i {}.".format(self.period.name))

    def test_all_of_the_required_slots(self):
        noon = Event.objects.create(name="Vardag 12-15", template=self.evening.template, rule=self.evening.rule,
                                    start=self.evening.start.replace(hour=12), end=self.evening.start.replace(hour=15))
        self.rule.required_events.add(noon)
        self._book(self.evening, self.tuesday, self.alice)
        self.assertEqual(self._unmet(self.afternoon, self.tuesday), [])  # one of them is enough
        self._rule(require_all=True)
        from .booking_rules import DependencyChecker
        unmet = self._unmet(self.afternoon, self.tuesday)
        self.assertEqual(DependencyChecker.describe(unmet), "Vardag 18-21 och Vardag 12-15 är bokade")
        self._book(noon, self.tuesday, self.bob)
        self.assertEqual(self._unmet(self.afternoon, self.tuesday), [])

    def test_the_day_before(self):
        self._rule(day_offset=-1)
        self._book(self.evening, self.monday, self.alice)
        self.assertEqual(self._unmet(self.afternoon, self.tuesday), [])
        self.assertNotEqual(self._unmet(self.afternoon, self.monday), [])

    def test_the_same_host(self):
        from .booking_rules import DependencyChecker
        self._rule(same_host=True)
        self._book(self.evening, self.tuesday, self.alice)
        checker = DependencyChecker(self.tuesday, self.tuesday)
        afternoon = self._slot(self.afternoon, self.tuesday)
        self.assertEqual(checker.unmet(afternoon, user=self.alice), [])
        self.assertNotEqual(checker.unmet(afternoon, user=self.bob), [])
        refused = self._toggle(self.bob, self.afternoon, self.tuesday)
        self.assertIn('Vardag 18-21 är bokat av dig', " ".join(str(m) for m in refused.context['messages']))
        self._toggle(self.alice, self.afternoon, self.tuesday)
        self.assertEqual(self._slot(self.afternoon, self.tuesday).host, self.alice)

    def test_only_in_its_period(self):
        later = SchedulingPeriod.objects.create(start=self.period.end + datetime.timedelta(days=1),
                                                end=self.period.end + datetime.timedelta(days=90))
        self._rule(period=later)
        self.assertEqual(self._unmet(self.afternoon, self.tuesday), [])  # not this period's rule
        first_monday = later.start + datetime.timedelta(days=(7 - later.start.weekday()) % 7)
        self.assertNotEqual(self._unmet(self.afternoon, first_monday), [])


class BookingRulesPageTests(SlotsFixture, TestCase):
    """The staff page for booking rules."""

    def setUp(self):
        super().setUp()
        self.staff = make_staff()

    def _post(self, url, **data):
        self.client.force_login(self.staff)
        return self.client.post(url, data, follow=True)

    def _rule_data(self, dependent, required, weekdays=("0", "1", "2", "3", "4"), **extra):
        data = {'period': self.period.pk,
                'dependent_events': [e.pk for e in dependent], 'required_events': [e.pk for e in required],
                'require_all': 'False', 'day_offset': '0', 'weekdays': list(weekdays), 'is_active': 'on',
                'name': '', 'description': '', 'valid_from': '', 'valid_until': ''}
        data.update(extra)
        return data

    def test_only_staff_can_see_it(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get('/scheduler/booking-rules/').status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get('/scheduler/booking-rules/').status_code, 302)

    def test_rules_are_listed_as_sentences_with_what_they_lock(self):
        self.client.force_login(self.staff)
        page = self.client.get('/scheduler/booking-rules/')
        self.assertContains(page, "Vardag 15-18 kan bara bokas när Vardag 18-21 är bokat samma dag. "
                                  "Gäller måndag–fredag.")
        impact = page.context['rules'][0].impact
        self.assertGreater(impact['locked'], 5)  # weekday afternoons in the coming four weeks
        self.assertEqual(impact['warned'], 0)

    def test_staff_create_a_rule(self):
        from .models import BookingDependency
        response = self._post('/scheduler/booking-rules/new/', **self._rule_data(
            [self.evening], [self.afternoon], weekdays=("5", "6"), day_offset='-1', same_host='on',
            name='Helgkväll'))
        self.assertContains(response, 'Regeln är sparad')
        rule = BookingDependency.objects.get(name='Helgkväll')
        self.assertEqual((rule.weekdays, rule.day_offset, rule.same_host, rule.require_all), ("5,6", -1, True, False))
        self.assertEqual(list(rule.dependent_events.all()), [self.evening])

    def test_a_slot_cannot_require_itself_the_same_day(self):
        response = self._post('/scheduler/booking-rules/new/', **self._rule_data([self.evening], [self.evening]))
        self.assertContains(response, 'Ett pass kan inte kräva sig självt samma dag')
        ok = self._post('/scheduler/booking-rules/new/', **self._rule_data([self.evening], [self.evening],
                                                                           day_offset='-1'))
        self.assertContains(ok, 'Regeln är sparad')

    def test_two_rules_that_wait_on_each_other_are_refused(self):
        response = self._post('/scheduler/booking-rules/new/', **self._rule_data([self.evening], [self.afternoon]))
        self.assertContains(response, 'väntar på varandra')
        # On days the existing rule does not apply, there is no circle.
        ok = self._post('/scheduler/booking-rules/new/', **self._rule_data([self.evening], [self.afternoon],
                                                                           weekdays=("5", "6")))
        self.assertContains(ok, 'Regeln är sparad')

    def test_pause_resume_and_delete(self):
        from .models import BookingDependency
        url = '/scheduler/booking-rules/{}/'.format(self.rule.pk)
        self.assertContains(self._post(url + 'toggle/'), 'Regeln är pausad')
        self.assertEqual(self._unmet(self.afternoon, self.tuesday), [])  # nothing locked while paused
        # A rule the other way round now saves, so resuming the first one would make a circle.
        self._post('/scheduler/booking-rules/new/', **self._rule_data([self.evening], [self.afternoon]))
        self.assertContains(self._post(url + 'toggle/'), 'Regeln kan inte slås på')
        BookingDependency.objects.exclude(pk=self.rule.pk).delete()
        self.assertContains(self._post(url + 'toggle/'), 'Regeln används igen')
        self.assertNotEqual(self._unmet(self.afternoon, self.tuesday), [])
        self.assertContains(self._post(url + 'delete/'), 'Regeln är borttagen')
        self.assertFalse(BookingDependency.objects.exists())

    def test_the_page_shows_one_period_and_copies_from_the_previous_one(self):
        from .models import BookingDependency
        later = SchedulingPeriod.objects.create(start=self.period.end + datetime.timedelta(days=1),
                                                end=self.period.end + datetime.timedelta(days=90))
        self.client.force_login(self.staff)
        empty = self.client.get('/scheduler/booking-rules/', {'period': later.pk})
        self.assertEqual(list(empty.context['rules']), [])
        self.assertContains(empty, 'kopiera 1 regel från {}'.format(self.period.name))
        self.assertContains(empty, '?period={}">‹ {}'.format(self.period.pk, self.period.name))
        copied = self._post('/scheduler/booking-rules/period/{}/copy/'.format(later.pk))
        self.assertContains(copied, '1 regel kopierades')
        self.assertEqual(later.booking_rules.get().sentence(), self.rule.sentence())
        again = self._post('/scheduler/booking-rules/period/{}/copy/'.format(later.pk))
        self.assertContains(again, 'Perioden har redan alla regler')
        self.assertNotContains(again, 'Kopiera 1 regel')  # nothing left to copy, so no button
        self.assertEqual(BookingDependency.objects.count(), 2)
        # Changing the copy leaves the original alone.
        copy = later.booking_rules.get()
        copy.weekdays = "0"
        copy.save()
        self.rule.refresh_from_db()
        self.assertEqual(self.rule.weekdays, "0,1,2,3,4")

    def test_a_new_period_starts_with_the_previous_periods_rules(self):
        self.client.force_login(self.staff)
        start = self.period.end + datetime.timedelta(days=1)
        response = self.client.post('/scheduler/period/create/', {
            'start': start.isoformat(), 'end': (start + datetime.timedelta(days=89)).isoformat(),
            'num_required_events': '6', 'participant_key_string': 'F'}, follow=True)
        new = SchedulingPeriod.objects.get(start=start)
        self.assertEqual([r.sentence() for r in new.booking_rules.all()], [self.rule.sentence()])
        self.assertContains(response, '1 bokningsregel kopierades från {}'.format(self.period.name))
        self.assertContains(response, 'Bokningsregler:</strong> 1')

    def test_rules_in_another_period_do_not_make_a_circle(self):
        later = SchedulingPeriod.objects.create(start=self.period.end + datetime.timedelta(days=1),
                                                end=self.period.end + datetime.timedelta(days=90))
        ok = self._post('/scheduler/booking-rules/new/', **self._rule_data(
            [self.evening], [self.afternoon], period=later.pk))
        self.assertContains(ok, 'Regeln är sparad')

    def test_the_form_shows_a_live_preview_and_the_menu_links_here(self):
        self.client.force_login(self.staff)
        page = self.client.get('/scheduler/booking-rules/{}/'.format(self.rule.pk))
        self.assertContains(page, 'id="rule-preview"')
        self.assertContains(page, 'Vardag 15-18 (15:00–18:00)')
        settings_group = next(s for s in page.context['menu'] if s.get('heading') == 'Inställningar')
        item = next(i for i in settings_group['items'] if i['label'] == 'Bokningsregler')
        self.assertEqual((item['url'], item['active']), ('/scheduler/booking-rules/', True))


class DateFieldFormTests(TestCase):
    """Forms with date fields render (the old date picker crashed on Django 4+)."""

    def test_period_and_closed_date_forms_render_and_save(self):
        self.client.force_login(make_staff())
        for url in ('/scheduler/period/create/', '/scheduler/ruleexclusion/create/'):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, url)
            self.assertContains(page, 'type="date"')
        response = self.client.post('/scheduler/period/create/', {
            'start': '2031-01-01', 'end': '2031-03-31', 'num_required_events': '6',
            'participant_key_string': 'F'})
        self.assertEqual(response.status_code, 302)
        period = SchedulingPeriod.objects.get(start=datetime.date(2031, 1, 1))
        page = self.client.get('/scheduler/period/{}/update'.format(period.pk))
        self.assertContains(page, 'value="2031-03-31"')
