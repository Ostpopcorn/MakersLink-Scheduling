"""
Email every host in the current period who has nothing booked or is behind.

Meant to run from cron, for example every Monday morning:

    docker exec scheduling python manage.py send_host_reminders

Hosts reminded within the last --min-days days (by staff or by this job)
are skipped, so running it more often does not spam anyone.
"""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from scheduler.host_progress import progress_for
from scheduler.models import HostReminder, SchedulingPeriod
from scheduler.reminders import NEEDS_REMINDER, bookable_soon, compose, send_reminder, site_url


class Command(BaseCommand):
    help = "Email booking reminders to hosts who are behind in the current period."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help="Print who would get what, send nothing")
        parser.add_argument('--min-days', type=int, default=7,
                            help="Skip hosts reminded within this many days (default 7)")

    def handle(self, *args, **options):
        period = SchedulingPeriod.get_current_period()
        if period is None:
            self.stdout.write("No current period, nothing to do.")
            return
        cutoff = timezone.now() - datetime.timedelta(days=options['min_days'])
        recent = set(HostReminder.objects.filter(period=period, sent_at__gte=cutoff)
                     .values_list('user_id', flat=True))
        slots = bookable_soon()
        base_url = site_url()
        sent = skipped = 0
        for progress in progress_for(period, period.hosts.filter(is_active=True).order_by('slackId')):
            if progress.status not in NEEDS_REMINDER:
                continue
            who = '{} <{}> ({})'.format(progress.user.slackId, progress.user.email, progress.status_label)
            if progress.user.id in recent:
                skipped += 1
                self.stdout.write("skip   {}: reminded within {} days".format(who, options['min_days']))
                continue
            if options['dry_run']:
                subject, body = compose(progress, base_url, slots)
                self.stdout.write("would  {}\n  Subject: {}\n{}\n".format(
                    who, subject, "\n".join("  | " + line for line in body.splitlines())))
            else:
                send_reminder(progress, base_url, slots)
                self.stdout.write("sent   {}".format(who))
            sent += 1
        self.stdout.write(self.style.SUCCESS("{} {}, {} skipped ({}).".format(
            sent, "would be sent" if options['dry_run'] else "sent", skipped, period.name)))
