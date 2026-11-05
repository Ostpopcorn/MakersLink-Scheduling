"""
The left-hand menu: what each user sees, grouped, and which item is the
current page. Rendered by base_generic.html from the `menu` context value
(scheduler.context_processors.extra_menu_processor).

An item is active when the current URL name is one of its `names`, or
starts with one of its `prefixes`.
"""
from django.conf import settings
from django.urls import reverse

# label, url name, tooltip, active url names, active url-name prefixes
MEMBER = [
    ('Boka pass', 'week-view-current', 'Boka värdpass vecka för vecka, eller som lista',
     {'week-view', 'week-view-current', 'week-toggle', 'index', 'host-signup', 'slot-manage'}, ('entry-',)),
    ('Anmäl deltagande', 'joinEvent', 'Gå bredvid och fixarpass', {'joinEvent', 'eventinstance-join'}, ()),
    ('Min profil', 'profile', 'Dina pass och hur långt du har kommit i perioden', {'profile'}, ()),
]
STAFF = [
    ('Uppföljning', 'followup', 'Vilka värdar ligger efter i perioden?', set(), ('followup',)),
    ('Specialpass', 'special-passes', 'Introkvällar och andra pass med flera värdar', set(), ('special-',)),
    ('Kalenderkontroll', 'calendar-check', 'Stämmer Google-kalendern med bokningssystemet?', set(), ('calendar-check',)),
    ('Värdar', 'hosts', 'Alla värdar och deras pass per period', {'hosts', 'hosts_filter', 'host-detail'}, ()),
]
SETTINGS = [
    ('Perioder', 'periods', None, {'periods'}, ('period-',)),
    ('Återkommande pass', 'events', None, {'events', 'events_filter'}, ('event-',)),
    ('Upprepningar', 'rules', None, {'rules'}, ('rule-',)),
    ('Mallar', 'templates', None, {'templates'}, ('template-',)),
    ('Kalendrar', 'calendars', None,
     {'calendars', 'calendar-detail', 'calendar-create', 'calendar-update', 'calendar-delete'}, ()),
    ('Stängda datum', 'ruleexclusions', 'Helgdagar och andra dagar utan pass', {'ruleexclusions'}, ('ruleexclusion-',)),
    ('Alla pass', 'eventinstances-admin', 'Alla pass som finns i databasen', {'eventinstances-admin'},
     ('eventinstance-admin-',)),
]


def _items(rows, current):
    items = []
    for label, url_name, tooltip, names, prefixes in rows:
        items.append({
            'label': label, 'url': reverse(url_name), 'tooltip': tooltip,
            'active': current in names or any(current.startswith(p) for p in prefixes),
        })
    return items


def build_menu(request, calendar_check_count=0):
    user = getattr(request, 'user', None)
    extras = [{'label': extra['name'], 'url': extra['link'], 'tooltip': extra.get('tooltip'),
               'external': True, 'active': False}
              for extra in getattr(settings, 'MENU_EXTRA', [])]
    if user is None or not user.is_authenticated:
        # Login and registration pages: only the links out (schedule, help).
        return [{'items': extras, 'footer': True}] if extras else None
    match = getattr(request, 'resolver_match', None)
    current = (match.url_name or '') if match else ''

    sections = [{'items': _items(MEMBER, current)}]
    if user.is_staff:
        staff = _items(STAFF, current)
        for item in staff:
            if item['label'] == 'Kalenderkontroll':
                item['badge'] = calendar_check_count
        sections.append({'heading': 'Styrelsen', 'items': staff})
        setup = _items(SETTINGS, current)
        setup.append({'label': 'Bokningsregler', 'url': '/admin/scheduler/bookingdependency/',
                      'tooltip': 'Pass som bara kan bokas när ett annat pass har en värd (Django-admin)',
                      'active': False})
        sections.append({'heading': 'Inställningar', 'items': setup, 'collapsible': True,
                         'open': any(item['active'] for item in setup)})

    footer = list(extras)
    footer.append({'label': 'Logga ut', 'url': reverse('logout'), 'tooltip': None, 'active': False,
                   'logout': True})
    sections.append({'items': footer, 'footer': True})
    return sections
