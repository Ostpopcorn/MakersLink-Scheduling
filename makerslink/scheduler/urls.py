from django.urls import path
import django.contrib.auth.views
from . import views
from . import views_calendar_check
from . import views_entries
from . import views_followup
from . import views_rules
from . import views_slots
from . import views_special
from . import views_week

# Booking: the list of a whole period is the start page
urlpatterns = [
    path('', views_week.list_view, name='index'),
]
# Prototype: week view for booking
urlpatterns += [
    path('week/', views_week.week_view, name='week-view-current'),
    path('week/<int:year>/<int:week>/', views_week.week_view, name='week-view'),
    path('week/toggle', views_week.week_toggle, name='week-toggle'),
    path('list/', views_week.list_view, name='booking-list'),
]
# Prototype: booking rules, for staff
urlpatterns += [
    path('booking-rules/', views_rules.booking_rules, name='booking-rules'),
    path('booking-rules/new/', views_rules.booking_rule_edit, name='booking-rule-create'),
    path('booking-rules/<int:pk>/', views_rules.booking_rule_edit, name='booking-rule-update'),
    path('booking-rules/<int:pk>/toggle/', views_rules.booking_rule_toggle, name='booking-rule-toggle'),
    path('booking-rules/<int:pk>/delete/', views_rules.booking_rule_delete, name='booking-rule-delete'),
    path('booking-rules/period/<int:pk>/copy/', views_rules.booking_rules_copy, name='booking-rule-copy'),
]
# Prototype: free-form calendar entries
urlpatterns += [
    path('entries/new/', views_entries.entry_create, name='entry-create'),
    path('entries/<int:pk>/', views_entries.entry_detail, name='entry-detail'),
    path('entries/<int:pk>/edit/', views_entries.entry_update, name='entry-update'),
    path('entries/<int:pk>/delete/', views_entries.entry_delete, name='entry-delete'),
    path('entries/<int:pk>/sync/', views_entries.entry_sync_now, name='entry-sync'),
]
# Prototype: staff booking, reassigning and removing bookings
urlpatterns += [
    path('slot/<int:event_pk>/<str:when>/', views_slots.slot_manage, name='slot-manage'),
]
# Prototype: special passes and their standard types
urlpatterns += [
    path('specialpass/', views_special.special_passes, name='special-passes'),
    path('specialpass/type/new/', views_special.special_type_edit, name='special-type-create'),
    path('specialpass/type/<int:pk>/', views_special.special_type_edit, name='special-type-update'),
    path('entries/<int:pk>/seat/', views_special.entry_seat, name='entry-seat'),
    path('entries/<int:pk>/seats/', views_special.entry_seats_manage, name='entry-seats-manage'),
]
# Prototype: compare Google Calendar with the schedule
urlpatterns += [
    path('calendar-check/', views_calendar_check.calendar_check_page, name='calendar-check'),
    path('calendar-check/<int:pk>/fix/', views_calendar_check.calendar_check_fix, name='calendar-check-fix'),
]
# Prototype: staff follow-up of hosts who are behind
urlpatterns += [
    path('followup/', views_followup.followup, name='followup'),
    path('followup/<int:pk>/', views_followup.followup, name='followup-period'),
    path('followup/<int:period_pk>/remind/<int:user_pk>/', views_followup.remind, name='followup-remind'),
]
# Calendars
urlpatterns += [
    path('calendars', views.SchedulingCalendarListView.as_view(extra_context={'commands': {
         'create': 'calendar-create'}}), name='calendars'),
    path('calendar/<uuid:pk>',
         views.SchedulingCalendarDetailView.as_view(extra_context={'commands': {
             'list': 'calendars', 'edit': 'calendar-update', 'delete': 'calendar-delete'}}), name='calendar-detail'),
    path('calendar/create/', views.SchedulingCalendarCreateView.as_view(),
         name='calendar-create'),
    path('calendar/<uuid:pk>/update',
         views.SchedulingCalendarUpdateView.as_view(), name='calendar-update'),
    path('calendar/<uuid:pk>/delete',
         views.SchedulingCalendarDeleteView.as_view(), name='calendar-delete'),
]
# Templates
urlpatterns += [
    path('templates', views.EventTemplateListView.as_view(extra_context={'commands': {
         'create': 'template-create'}}), name='templates'),
    path('template/<int:pk>', views.EventTemplateDetailView.as_view(extra_context={'commands': {
         'list': 'templates', 'edit': 'template-update', 'delete': 'template-delete'}}),
         name='template-detail'),
    path('template/create/', views.EventTemplateCreateView.as_view(),
         name='template-create'),
    path('template/<int:pk>/update',
         views.EventTemplateUpdateView.as_view(), name='template-update'),
    path('template/<int:pk>/delete',
         views.EventTemplateDeleteView.as_view(), name='template-delete'),
]
# Rules
urlpatterns += [
    path('rules', views.SchedulingRuleListView.as_view(extra_context={'commands': {
         'create': 'rule-create'}}), name='rules'),
    path('rule/<int:pk>', views.SchedulingRuleDetailView.as_view(extra_context={'commands': {
        'list': 'rules', 'edit': 'rule-update', 'delete': 'rule-delete'}}), name='rule-detail'),
    path('rule/create/', views.SchedulingRuleCreateView.as_view(), name='rule-create'),
    path('rule/<int:pk>/update',
         views.SchedulingRuleUpdateView.as_view(), name='rule-update'),
    path('rule/<int:pk>/delete',
         views.SchedulingRuleDeleteView.as_view(), name='rule-delete'),
]
# Events
urlpatterns += [
    path('events', views.EventListView.as_view(extra_context={'commands': {
         'create': 'event-create'}}), name='events'),
    path('events/<str:filter>', views.EventListView.as_view(extra_context={'commands': {
         'create': 'event-create'}}), name='events_filter'),
    path('event/<int:pk>', views.EventDetailView.as_view(extra_context={'commands': {
         'list': 'events', 'edit': 'event-update', 'delete': 'event-delete'}}), name='event-detail'),
    path('event/create/', views.EventCreateView.as_view(), name='event-create'),
    path('event/<int:pk>/update',
         views.EventUpdateView.as_view(), name='event-update'),
    path('event/<int:pk>/delete',
         views.EventDeleteView.as_view(), name='event-delete'),
]
# SchedulingPeriods
urlpatterns += [
    path('periods', views.PeriodListView.as_view(extra_context={'commands': {
         'create': 'period-create'}}), name='periods'),
    path('period/<int:pk>', views.PeriodDetailView.as_view(extra_context={'commands': {
        'list': 'periods', 'edit': 'period-update', 'delete': 'period-delete'}}), name='period-detail'),
    path('period/create/', views.PeriodCreateView.as_view(), name='period-create'),
    path('period/<int:pk>/update',
         views.PeriodUpdateView.as_view(), name='period-update'),
    path('period/<int:pk>/delete',
         views.PeriodDeleteView.as_view(), name='period-delete'),
]
# Join as participant
urlpatterns += [
    path('joinEvent', views.EventInstanceListView.as_view(), name='joinEvent'),
    path('joinEvent/<uuid:pk>', views.EventInstanceUpdateView.as_view(),
         name='eventinstance-join'),
    path('joinEvent/<uuid:pk>', views.EventDetailView.as_view(),
         name='eventinstance-detail'),
]
# Host details
urlpatterns += [
    path('host/<str:slug>', views.HostDetailView.as_view(), name='host-detail'),
    path('profile', views.ProfileView.as_view(), name='profile'),
    path('signup', views_week.list_view, name='host-signup'),
    path('hosts/<str:filter>',
         views.HostListView.as_view(template_name='host_list.html'), name='hosts_filter'),
    path('hosts', views.HostListView.as_view(
        template_name='host_list.html'), name='hosts'),
]
# SchedulingRuleExclusion
urlpatterns += [
    path('ruleexclusions', views.SchedulingRuleExclusionListView.as_view(extra_context={'commands': {
         'create': 'ruleexclusion-create'}}),
         name='ruleexclusions'),
    path('ruleexclusion/<int:pk>', views.SchedulingRuleExclusionDetailView.as_view(extra_context={'commands': {'list': 'ruleexclusions', 'edit': 'ruleexclusion-update', 'delete': 'ruleexclusion-delete'}}),
         name='ruleexclusion-detail'),
    path('ruleexclusion/create/', views.SchedulingRuleExclusionCreateView.as_view(),
         name='ruleexclusion-create'),
    path('ruleexclusion/<int:pk>/update',
         views.SchedulingRuleExclusionUpdateView.as_view(), name='ruleexclusion-update'),
    path('ruleexclusion/<int:pk>/delete',
         views.SchedulingRuleExclusionDeleteView.as_view(), name='ruleexclusion-delete'),
]

# EventInstance admin
urlpatterns += [
    path('eventinstances-admin', views.EventInstanceAdminListView.as_view(),
         name='eventinstances-admin'),
    path('eventinstance-admin/<uuid:pk>',
         views.EventInstanceAdminDetailView.as_view(extra_context={'commands': {
             'list': 'eventinstances-admin', 'edit': 'eventinstance-admin-update', 'delete': 'eventinstance-admin-delete'}}), name='eventinstance-admin-detail'),
    path('eventinstance-admin/<uuid:pk>/update',
         views.EventInstanceAdminUpdateView.as_view(), name='eventinstance-admin-update'),
    path('eventinstance-admin/<uuid:pk>/delete',
         views.EventInstanceAdminDeleteView.as_view(), name='eventinstance-admin-delete'),
]
