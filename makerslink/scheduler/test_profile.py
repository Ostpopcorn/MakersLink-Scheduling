"""
Pages that depend on the current period.

The profile page (and the staff host page, which shares its view) used to
500 when no period covered today, and with ZeroDivisionError for a host with
nothing done in a period that requires nothing.
"""
import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from .models import SchedulingPeriod, percentage


class ProfilePageTests(TestCase):

    def setUp(self):
        self.user = User.objects.create(email="new@example.com", slackId="nykomling", is_active=True,
                                        is_staff=True, is_registration_complete=True)
        self.client.force_login(self.user)
        self.today = timezone.localdate()

    def current_period(self, **fields):
        return SchedulingPeriod.objects.create(start=self.today - datetime.timedelta(days=30),
                                               end=self.today + datetime.timedelta(days=120), **fields)

    def pages(self):
        return [reverse("profile"), reverse("host-detail", args=[self.user.slackId])]

    def assert_pages_render(self):
        for page in self.pages():
            with self.subTest(page=page):
                self.assertEqual(self.client.get(page).status_code, 200)

    def test_no_period_covers_today(self):
        SchedulingPeriod.objects.create(start=datetime.date(2022, 2, 1), end=datetime.date(2022, 3, 31))
        self.assert_pages_render()
        self.assertIn("Ingen period pågår just nu", self.client.get(reverse("profile")).content.decode())

    def test_no_periods_at_all(self):
        self.assert_pages_render()

    def test_current_period_that_requires_nothing(self):
        self.current_period(num_required_events=0, participant_key_string="")
        self.assert_pages_render()

    def test_old_period_that_requires_nothing(self):
        self.current_period()
        SchedulingPeriod.objects.create(start=datetime.date(2022, 2, 1), end=datetime.date(2022, 3, 31),
                                        num_required_events=0, participant_key_string="")
        self.assert_pages_render()

    def test_current_period_still_shows_its_stats(self):
        self.current_period(num_required_events=6)
        page = self.client.get(reverse("profile")).content.decode()
        self.assertIn("Aktuell period", page)
        self.assertIn("Boka ett pass", page)
        self.assertNotIn("Ingen period pågår just nu", page)


class PercentageTests(TestCase):
    def test_percentage(self):
        self.assertEqual(percentage(3, 6), 50)
        self.assertEqual(percentage(0, 0), 0)


class EventListWithoutCurrentPeriodTests(TestCase):
    """The staff event list's default "this period" filter crashed with no current period."""

    def setUp(self):
        staff = User.objects.create(email="staff@example.com", slackId="staff", is_active=True,
                                    is_staff=True, is_registration_complete=True)
        self.client.force_login(staff)

    def test_event_list_filters_render_without_a_current_period(self):
        SchedulingPeriod.objects.create(start=datetime.date(2022, 2, 1), end=datetime.date(2022, 3, 31))
        for page in [reverse("events"), reverse("events_filter", args=["last"]),
                     reverse("events_filter", args=["previous"]), reverse("events_filter", args=["none"])]:
            with self.subTest(page=page):
                self.assertEqual(self.client.get(page).status_code, 200)

    def test_previous_filter_in_the_first_period(self):
        today = timezone.localdate()
        SchedulingPeriod.objects.create(start=today - datetime.timedelta(days=30),
                                        end=today + datetime.timedelta(days=120))
        self.assertEqual(self.client.get(reverse("events_filter", args=["previous"])).status_code, 200)
