"""
How far each host has come towards a period's requirements.

Only bookings with status 1 (Bokat) count. A host who cancels keeps their
name on the instance with status -1 (Ombokning krävs); the profile page's
SchedulingPeriod.get_host_stats() still counts those as booked, this does not.
"""
import math

from django.utils import timezone

from .models import EventInstance

STATUS_LABELS = {
    'done': 'Klar',
    'on_track': 'På väg',
    'behind': 'Efter',
    'none': 'Inget bokat',
    'missed': 'Nådde inte kravet',
}


class HostProgress:
    def __init__(self, user, period, hosted, participating, now):
        self.user = user
        self.period = period
        self.now = now
        self.host_done = sum(1 for i in hosted if i.start < now)
        self.host_booked = len(hosted) - self.host_done
        self.participant_done_keys = ''.join(
            i.template.count_key for i in participating if i.template and i.start < now)
        self.participant_booked_keys = ''.join(
            i.template.count_key for i in participating if i.template and i.start >= now)
        upcoming = sorted((i for i in hosted if i.start >= now), key=lambda i: i.start)
        self.next_slot = upcoming[0] if upcoming else None

    # Hosting
    @property
    def host_committed(self):
        return self.host_done + self.host_booked

    @property
    def host_required(self):
        return self.period.num_required_events

    @property
    def host_missing(self):
        return max(0, self.host_required - self.host_committed)

    # Participating ("gå bredvid", fixarpass...), counted per template key
    @property
    def participant_keys(self):
        return self.participant_done_keys + self.participant_booked_keys

    @property
    def missing_keys(self):
        missing = list(self.period.participant_key_string)
        for key in self.participant_keys:
            if key in missing:
                missing.remove(key)
        return ''.join(missing)

    @property
    def key_chips(self):
        """(key, covered) for each required participant key, in order."""
        available = list(self.participant_keys)
        chips = []
        for key in self.period.participant_key_string:
            covered = key in available
            if covered:
                available.remove(key)
            chips.append((key, covered))
        return chips

    # Totals towards the requirement (extra slots do not make up for others)
    @property
    def required_total(self):
        return self.host_required + len(self.period.participant_key_string)

    @property
    def committed_total(self):
        return (min(self.host_committed, self.host_required)
                + len(self.period.participant_key_string) - len(self.missing_keys))

    @property
    def expected_by_now(self):
        """What an even pace through the period would have committed by today."""
        fraction = self.period.get_elapsed() / max(1, self.period.get_length())
        return math.ceil(self.required_total * fraction)

    @property
    def status(self):
        if self.host_missing == 0 and not self.missing_keys:
            return 'done'
        if timezone.localdate() > self.period.end:
            return 'missed'
        if self.host_committed == 0 and not self.participant_keys:
            return 'none'
        if self.committed_total < self.expected_by_now:
            return 'behind'
        return 'on_track'

    @property
    def status_label(self):
        return STATUS_LABELS[self.status]

    # Percentages for a stacked progress bar, scaled to the larger of
    # requirement and what was actually booked.
    def _pct(self, value):
        return 100 * value / max(1, self.host_required, self.host_committed)

    @property
    def host_done_pct(self):
        return self._pct(self.host_done)

    @property
    def host_booked_pct(self):
        return self._pct(self.host_booked)

    @property
    def host_required_pct(self):
        return self._pct(self.host_required)


def progress_for(period, users, now=None):
    """HostProgress for each user in `users`, in the order given."""
    now = now or timezone.now()
    users = list(users)
    hosted = {}
    for instance in EventInstance.objects.filter(
            period=period, status=1, host__in=users).select_related('event__template'):
        hosted.setdefault(instance.host_id, []).append(instance)
    participating = {}
    for instance in EventInstance.objects.filter(
            period=period, participants__in=users).exclude(status=2).distinct().select_related(
            'event__template').prefetch_related('participants'):
        for participant in instance.participants.all():
            participating.setdefault(participant.id, []).append(instance)
    return [HostProgress(user, period, hosted.get(user.id, []),
                         participating.get(user.id, []), now) for user in users]
