"""
How far each host has come towards a period's requirements.

Only bookings with status 1 (Bokat) count, plus seats on special passes
whose type counts as a session. A host who cancels keeps their
name on the instance with status -1 (Ombokning krävs); the profile page's
SchedulingPeriod.get_host_stats() still counts those as booked, this does not.
"""
import math
from collections import Counter

from django.utils import timezone

from .models import EntryHost, EventInstance, EventTemplate

STATUS_LABELS = {
    'done': 'Klar',
    'on_track': 'På väg',
    'behind': 'Efter',
    'none': 'Inget bokat',
    'missed': 'Nådde inte kravet',
}


class HostProgress:
    def __init__(self, user, period, hosted, participating, now, key_names=None, other_seats=()):
        self.user = user
        self.period = period
        self.now = now
        self.key_names = key_names or {}
        # Participating in sessions the period does not ask for (going
        # alongside a host on "Öppet hus"), and seats on special passes that
        # do not count as a session: shown as a note, not as progress.
        required = set(period.participant_key_string)
        self.other_participating = [i for i in participating
                                    if i.template and i.template.count_key not in required]
        self.other_seats = list(other_seats)
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

    @property
    def participant_bars(self):
        """
        One bar per participant key the period asks for (usually just "F",
        Fixarpass), in the order the keys first appear: how many are
        required, done and booked.
        """
        required = Counter(self.period.participant_key_string)
        done = Counter(self.participant_done_keys)
        booked = Counter(self.participant_booked_keys)
        bars = []
        for key in dict.fromkeys(self.period.participant_key_string):
            committed = done[key] + booked[key]
            scale = max(1, required[key], committed)
            bars.append({
                'key': key,
                'name': self.key_names.get(key, ''),
                'required': required[key],
                'done': done[key],
                'booked': booked[key],
                'committed': committed,
                'missing': max(0, required[key] - committed),
                'done_pct': 100 * done[key] / scale,
                'booked_pct': 100 * booked[key] / scale,
                'required_pct': 100 * required[key] / scale,
            })
        return bars

    @property
    def todo(self):
        """What is left to book, as (count, what): [(2, 'värdpass'), (1, 'Fixarpass')]."""
        parts = [(self.host_missing, 'värdpass')] if self.host_missing else []
        parts += [(bar['missing'], bar['name'] or 'deltagarpass ' + bar['key'])
                  for bar in self.participant_bars if bar['missing']]
        return parts

    @property
    def other_signups(self):
        """
        Everything else the host is signed up for in the period, grouped by
        what it is: [{'label': 'Öppet hus', 'as': 'deltagare', 'count': 2}, ...].
        """
        groups = {}
        for instance in self.other_participating:
            groups.setdefault((instance.template.title, 'deltagare'), []).append(instance.start)
        for entry in self.other_seats:
            groups.setdefault((entry.title, 'värd'), []).append(entry.start)
        return [{'label': label, 'as': role, 'count': len(starts), 'first': min(starts)}
                for (label, role), starts in sorted(groups.items(), key=lambda item: min(item[1]))]

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
    # A seat on a special pass whose type counts as a session is a hosted slot too.
    for seat in period.special_seats().filter(user__in=users).select_related('entry'):
        hosted.setdefault(seat.user_id, []).append(seat.entry)
    participating = {}
    for instance in EventInstance.objects.filter(
            period=period, participants__in=users).exclude(status=2).distinct().select_related(
            'event__template').prefetch_related('participants'):
        for participant in instance.participants.all():
            participating.setdefault(participant.id, []).append(instance)
    # Seats on special passes that do not count as a session.
    other_seats = {}
    for seat in EntryHost.objects.filter(
            user__in=users, entry__kind='special', entry__start__date__gte=period.start,
            entry__start__date__lte=period.end, handed_over_at__isnull=True).exclude(
            entry__special_type__counts_as_session=True).select_related('entry'):
        other_seats.setdefault(seat.user_id, []).append(seat.entry)
    names = key_names(period)
    return [HostProgress(user, period, hosted.get(user.id, []), participating.get(user.id, []),
                         now, names, other_seats.get(user.id, [])) for user in users]


def key_names(period):
    """
    What each of the period's participant keys stands for ("F": "Fixarpass"),
    from the templates that take participants. A key shared by several such
    templates gets no name, rather than a long list.
    """
    names = {}
    for key, name in (EventTemplate.objects.filter(count_key__in=set(period.participant_key_string))
                      .exclude(num_participants=0).values_list('count_key', 'name').distinct()):
        names.setdefault(key, set()).add(name)
    return {key: found.pop() for key, found in names.items() if len(found) == 1}
