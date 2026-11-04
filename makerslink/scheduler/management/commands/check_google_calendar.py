"""
Compare every Google calendar the schedule writes to with the schedule, store
the result for the staff page (Kalenderkontroll), and email the staff when
there is something new to look at.

Meant to run from cron, for example every morning:

    docker exec scheduling python manage.py check_google_calendar

Staff are only emailed when the differences changed since the previous run,
so an unfixed difference does not arrive again every morning; --always-email
sends anyway, --no-email never sends.
"""
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from scheduler import calendar_check
from scheduler.reminders import site_url

LINES_PER_CALENDAR = 15


class Command(BaseCommand):
    help = "Compare Google Calendar with the schedule and email staff about differences."

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=calendar_check.DEFAULT_DAYS,
                            help="How far ahead to compare (default %(default)s days)")
        parser.add_argument('--no-email', action='store_true', help="Never email the staff")
        parser.add_argument('--always-email', action='store_true',
                            help="Email the staff even if nothing changed since the last run")

    def handle(self, *args, **options):
        to_report = []
        for calendar in calendar_check.calendars_to_check():
            previous = calendar.checks.first()
            check = calendar_check.run(calendar, days=options['days'])
            if check.error:
                self.stdout.write(self.style.ERROR('{}: could not check: {}'.format(calendar, check.error)))
            else:
                self.stdout.write('{}: {} difference(s)'.format(calendar, len(check.differences)))
                for difference in check.differences:
                    self.stdout.write('  {:<28} {:<26} {}'.format(
                        calendar_check.KINDS[difference['kind']][0], difference['when'], difference['title']))
            changed = (previous is None or previous.signature() != check.signature()
                       or bool(previous.error) != bool(check.error))
            if (check.differences or check.error) and (changed or options['always_email']):
                to_report.append(check)

        if not to_report or options['no_email']:
            self.stdout.write('No email sent.')
            return
        recipients = list(get_user_model().objects.filter(is_staff=True, is_active=True)
                          .exclude(email='').values_list('email', flat=True))
        if not recipients:
            self.stdout.write(self.style.WARNING('Nobody to email: no active staff with an address.'))
            return
        total = sum(len(c.differences) for c in to_report)
        subject = 'Kalenderkontroll: {} skillnad{} mot Google-kalendern'.format(total, 'er' if total != 1 else '')
        send_mail(subject, self._body(to_report), None, recipients)
        self.stdout.write(self.style.SUCCESS('Emailed {} staff member(s).'.format(len(recipients))))

    @staticmethod
    def _body(checks):
        lines = ['Hej!', '', 'Kalenderkontrollen hittade skillnader mellan Google-kalendern och bokningssystemet.', '']
        for check in checks:
            lines.append('{}:'.format(check.calendar))
            if check.error:
                lines.append('  Kunde inte kontrolleras: {}'.format(check.error))
            for difference in check.differences[:LINES_PER_CALENDAR]:
                lines.append('  - {}: {} {}'.format(calendar_check.KINDS[difference['kind']][0],
                                                    difference['when'], difference['title']))
            if len(check.differences) > LINES_PER_CALENDAR:
                lines.append('  ...och {} till.'.format(len(check.differences) - LINES_PER_CALENDAR))
            lines.append('')
        lines.append('Detaljer och vad du kan göra: {}/scheduler/calendar-check/'.format(site_url()))
        lines.append('')
        lines.append('MakersLink bokningssystem')
        return '\n'.join(lines)
