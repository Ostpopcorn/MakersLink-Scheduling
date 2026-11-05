import operator
import accounts.models
from django import utils
from accounts.models import User
from django.db.models.expressions import F
from django.db.models import Q, Count, Max, ProtectedError
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic.edit import CreateView, UpdateView, DeleteView
from django.views import generic
from dateutil.relativedelta import *
from datetime import datetime, timezone, time
from .forms import PeriodForm, RuleExclusionForm, EventInstanceAdminForm
from .models import EventTemplate, SchedulingCalendar, Event, EventInstance, SchedulingRule, SchedulingPeriod, SchedulingRuleExclusion
from .models import BookingDependency, EntryHost
from django.shortcuts import render
import logging
logger = logging.getLogger(__name__)


# Create your views here.

@login_required
def index(request):
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end = start + relativedelta(months=+1)

    event_objects = Event.objects.all()
    event_list = []
    for event in event_objects:
        event_list += event.get_events(start, end)

    event_list = sorted(
        event_list, key=lambda eventinstance: eventinstance.start)

    return render(
        request,
        'index.html',
        context={
            'event_list': event_list
        },
    )


class UserIsStaffMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_staff


class ProtectedDeleteMixin:
    """
    Templates, calendars and events are referenced by rows that cannot be
    rendered without them, so those relations are PROTECT and the database
    refuses the delete. Re-render the confirmation page naming what is still
    using the object instead of letting ProtectedError become a 500.
    """
    # Enough to identify the problem without rendering thousands of rows.
    protected_display_limit = 20

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ProtectedError as error:
            # Only render a bounded sample: an Event can be protected by every
            # instance ever scheduled from it, and each str() is a query.
            protected = list(error.protected_objects)
            shown = protected[:self.protected_display_limit]
            return self.render_to_response(self.get_context_data(
                form=form,
                protected_objects=sorted(str(obj) for obj in shown),
                protected_count=len(protected),
                protected_overflow=len(protected) - len(shown),
            ))


class SchedulingCalendarListView(UserIsStaffMixin, generic.ListView):
    model = SchedulingCalendar


class SchedulingCalendarDetailView(UserIsStaffMixin, generic.DetailView):
    model = SchedulingCalendar


class SchedulingCalendarCreateView(UserIsStaffMixin, CreateView):
    model = SchedulingCalendar
    fields = '__all__'


class SchedulingCalendarUpdateView(UserIsStaffMixin, UpdateView):
    model = SchedulingCalendar
    fields = '__all__'


class SchedulingCalendarDeleteView(ProtectedDeleteMixin, UserIsStaffMixin, DeleteView):
    model = SchedulingCalendar
    success_url = reverse_lazy('calendars')


class EventTemplateListView(UserIsStaffMixin, generic.ListView):
    model = EventTemplate


class EventTemplateDetailView(UserIsStaffMixin, generic.DetailView):
    model = EventTemplate


class EventTemplateCreateView(UserIsStaffMixin, CreateView):
    model = EventTemplate
    fields = '__all__'


class EventTemplateUpdateView(UserIsStaffMixin, UpdateView):
    model = EventTemplate
    fields = '__all__'


class EventTemplateDeleteView(ProtectedDeleteMixin, UserIsStaffMixin, DeleteView):
    model = EventTemplate
    success_url = reverse_lazy('templates')


class SchedulingRuleListView(UserIsStaffMixin, generic.ListView):
    model = SchedulingRule


class SchedulingRuleDetailView(UserIsStaffMixin, generic.DetailView):
    model = SchedulingRule

    def get_context_data(self, **kwargs):
        context = super(SchedulingRuleDetailView,
                        self).get_context_data(**kwargs)
        events = self.object.get_events(extra_params="count:5;")
        context['eventlist'] = events
        return context


class SchedulingRuleCreateView(UserIsStaffMixin, CreateView):
    model = SchedulingRule
    fields = '__all__'


class SchedulingRuleUpdateView(UserIsStaffMixin, UpdateView):
    model = SchedulingRule
    fields = '__all__'


class SchedulingRuleDeleteView(UserIsStaffMixin, DeleteView):
    model = SchedulingRule
    success_url = reverse_lazy('rules')


class EventListView(UserIsStaffMixin, generic.ListView):
    model = Event

    def get_context_data(self, **kwargs):
        # Call the base implementation first to get a context
        context = super(EventListView, self).get_context_data(**kwargs)
        context['filter_options'] = ["none", "previous", "last", "future"]

        if 'filter' in self.kwargs:
            context['filter'] = self.kwargs['filter']
        else:
            context['filter'] = "last"
            self.kwargs['filter'] = "last"

        return context

    def get_queryset(self):
        period = SchedulingPeriod.get_current_period()
        if not 'filter' in self.kwargs:
            self.kwargs['filter'] = "last"

        querySet = super(EventListView, self).get_queryset()

        if self.kwargs['filter'] == "last" or self.kwargs['filter'] == "previous":
            if self.kwargs['filter'] == "previous":
                period = period.get_previous_period()

            return querySet.filter(
                start__lte=datetime.combine(
                    period.end, time(0, 0), timezone.utc)
            ).filter(
                Q(end__gte=datetime.combine(period.start, time(0, 0), timezone.utc)) |
                Q(repeat_end__gte=datetime.combine(
                    period.start, time(0, 0), timezone.utc))
            )
        elif self.kwargs['filter'] == "future":
            return querySet.filter(start__gte=utils.timezone.now())

        return querySet


class EventDetailView(UserIsStaffMixin, generic.DetailView):
    model = Event

    def get_context_data(self, **kwargs):
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        end = start+relativedelta(months=+1)
        context = super(EventDetailView, self).get_context_data(**kwargs)
        events = self.object.get_events(start, end)
        context['eventlist'] = events
        return context


class EventCreateView(UserIsStaffMixin, CreateView):
    model = Event
    fields = '__all__'
    #form_class = EventForm


class EventUpdateView(UserIsStaffMixin, UpdateView):
    model = Event
    fields = '__all__'
    #form_class = EventForm


class EventDeleteView(ProtectedDeleteMixin, UserIsStaffMixin, DeleteView):
    model = Event
    success_url = reverse_lazy('events')


class PeriodListView(UserIsStaffMixin, generic.ListView):
    model = SchedulingPeriod


class PeriodDetailView(UserIsStaffMixin, generic.DetailView):
    model = SchedulingPeriod


class PeriodCreateView(UserIsStaffMixin, CreateView):
    model = SchedulingPeriod
    form_class = PeriodForm

    def form_valid(self, form):
        response = super().form_valid(form)
        # Booking rules belong to a period: start the new one with copies of
        # the previous period's, which staff can then change for this one.
        source = SchedulingPeriod.objects.filter(start__lt=self.object.start).exclude(
            pk=self.object.pk).order_by('-start').first()
        if source is not None:
            copied = BookingDependency.copy_period(source, self.object)
            if copied:
                messages.success(self.request, '{} {} kopierades från {}. Ändra dem under Bokningsregler.'.format(
                    copied, 'bokningsregel' if copied == 1 else 'bokningsregler', source.name))
        return response

    def get_context_data(self, **kwargs):
        context = super(PeriodCreateView, self).get_context_data(**kwargs)
        # None when this is the first period.
        context['hosts_last_period'] = SchedulingPeriod.objects.order_by('-end').first()
        return context


class PeriodUpdateView(UserIsStaffMixin, UpdateView):
    model = SchedulingPeriod
    form_class = PeriodForm


class PeriodDeleteView(UserIsStaffMixin, DeleteView):
    model = SchedulingPeriod
    success_url = reverse_lazy('periods')


class EventInstanceListView(LoginRequiredMixin, generic.ListView):
    model = EventInstance

    def get_queryset(self):
        today = datetime.now().date()
        queryset = EventInstance.objects.all().filter(Q(status__in=EventInstance.HOSTED_STATUSES) & ~Q(event__template__num_participants=0)
                                                      & Q(start__gte=today)).order_by("event__template", "start")
        # Each row shows its template and, for staff, its participants.
        queryset = queryset.select_related('event__template').prefetch_related('participants')

        return queryset


class EventInstanceUpdateView(LoginRequiredMixin, UpdateView):
    model = EventInstance
    # Empty field list as we save to the current user when submitted.
    fields = ()

    def form_valid(self, form):
        event = form.save(commit=False)
        if event.participants.filter(id=self.request.user.id).exists():
            # User was participant remove them.
            event.participants.remove(self.request.user)
        else:
            # User was not participant add them.
            event.participants.add(self.request.user)
        event.save()
        return HttpResponseRedirect(self.get_success_url())


class UnsecuredHostDetailView(generic.DetailView):
    model = User
    template_name = 'scheduler/host_detail.html'
    context_object_name = 'view_user'

    def get_context_data(self, **kwargs):
        context = super(UnsecuredHostDetailView,
                        self).get_context_data(**kwargs)
        context['current_period'] = SchedulingPeriod.get_current_period()
        context['host_stats'] = context['current_period'].get_host_stats(
            self.get_object())
        context['period_list'] = SchedulingPeriod.objects.all().order_by("-start").annotate(event_count=Count(
            'eventinstance', filter=Q(eventinstance__status__in=EventInstance.HOSTED_STATUSES, eventinstance__host=self.get_object())),
            participant_count=Count('eventinstance', filter=Q(eventinstance__status__in=EventInstance.HOSTED_STATUSES, eventinstance__participants=self.get_object())))
        for period in context['period_list']:
            period.extra_host = max(
                0, period.event_count - period.num_required_events)
            period.extra_participant = max(
                0, period.participant_count - period.num_required_participant)
            period.total_done = period.event_count + period.participant_count
            period.total_extra = period.extra_host + period.extra_participant
            period.max_done_required = max(
                period.total_done, period.get_required_total_number_of_required_events())
            period.total_done_percentage = (
                period.total_done / period.max_done_required) * 100
            period.total_extra_percentage = (
                period.total_extra / period.max_done_required) * 100
        context['period_host_list'] = SchedulingPeriod.get_all_host_count_key_lists()
        context['special_seats'] = EntryHost.objects.filter(
            user=self.get_object(), entry__kind='special').select_related(
            'entry__special_type').order_by('entry__start')
        context['hosted_events'] = self.get_object().eventinstance_set.filter(
            status__in=EventInstance.HOSTED_STATUSES).select_related('event__template', 'period').order_by('period', 'start')
        context['participant_events'] = self.get_object(
        ).participants.all().select_related('event__template', 'period').order_by('period', 'start')
        return context


class HostDetailView(UserIsStaffMixin, UnsecuredHostDetailView):
    slug_field = "slackId"


class ProfileView(LoginRequiredMixin, UnsecuredHostDetailView):

    def get_object(self):
        return self.request.user


class HostListView(UserIsStaffMixin, generic.ListView):
    model = SchedulingPeriod

    def get_context_data(self, **kwargs):
        # Call the base implementation first to get a context
        context = super(HostListView, self).get_context_data(**kwargs)
        if not 'filter' in self.kwargs:
            self.kwargs['filter'] = "none"
        only_hosts_in_last_period = self.kwargs['filter'] == "last"
        #context['booking_count'] = EventInstance.objects.filter(status=1).count
        context['filter_options'] = ["none", "last"]
        context['filter'] = self.kwargs['filter']
        context['period_host_list'] = SchedulingPeriod.get_all_host_count_key_lists(
            only_hosts_in_last_period=only_hosts_in_last_period)
        return context

    def get_queryset(self):
        return SchedulingPeriod.objects.all().order_by('-start').annotate(event_count=Count('eventinstance', filter=Q(eventinstance__status__in=EventInstance.HOSTED_STATUSES)))
        # queryset = accounts.models.User.objects.all().order_by('slackId', 'eventinstance__period').annotate(
        #	host_count=Count('slackId', filter=Q(eventinstance__status=1)),
        #	period=Max('eventinstance__period'))

        # return queryset


class SchedulingRuleExclusionListView(UserIsStaffMixin, generic.ListView):
    model = SchedulingRuleExclusion


class SchedulingRuleExclusionDetailView(UserIsStaffMixin, generic.DetailView):
    model = SchedulingRuleExclusion

    '''
    
    def get_context_data(self, **kwargs):
        context = super(SchedulingRuleExclusionDetailView, self).get_context_data(**kwargs)
        events = self.object.get_events(extra_params="count:5;")
        context['eventlist'] = events
        return context
        
    '''


class SchedulingRuleExclusionCreateView(UserIsStaffMixin, CreateView):
    model = SchedulingRuleExclusion
    form_class = RuleExclusionForm


class SchedulingRuleExclusionUpdateView(UserIsStaffMixin, UpdateView):
    model = SchedulingRuleExclusion
    form_class = RuleExclusionForm


class SchedulingRuleExclusionDeleteView(UserIsStaffMixin, DeleteView):
    model = SchedulingRuleExclusion
    success_url = reverse_lazy('ruleexclusions')

# EventInstance admin


class EventInstanceAdminListView(UserIsStaffMixin, generic.ListView):
    model = EventInstance
    template_name = "scheduler/eventinstanceadmin_list.html"

    def get_queryset(self):
        today = datetime.now().date()
        queryset = EventInstance.objects.all().filter(
            start__gte=today).order_by("start")
        return queryset


class EventInstanceAdminDetailView(UserIsStaffMixin, generic.DetailView):
    model = EventInstance
    template_name = "scheduler/eventinstanceadmin_detail.html"


class EventInstanceAdminUpdateView(UserIsStaffMixin, UpdateView):
    model = EventInstance
    form_class = EventInstanceAdminForm
    template_name = "scheduler/eventinstanceadmin_form.html"

    def get_success_url(self):
        return reverse_lazy('eventinstance-admin-detail', kwargs={'pk': self.object.id})


class EventInstanceAdminDeleteView(UserIsStaffMixin, DeleteView):
    model = EventInstance
    template_name = "scheduler/eventinstanceadmin_confirm_delete.html"
    success_url = reverse_lazy('eventinstances-admin')
