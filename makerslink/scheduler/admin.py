from django.contrib import admin
from .forms import BookingDependencyForm
from .models import EventTemplate, SchedulingCalendar, Event, EventInstance, SchedulingRule, SchedulingPeriod, SchedulingRuleExclusion, BookingDependency, CalendarEntry, BookingChange

# Register your models here.
admin.site.register(EventTemplate)
admin.site.register(SchedulingCalendar)
admin.site.register(SchedulingRule)
admin.site.register(SchedulingRuleExclusion)

@admin.register(BookingDependency)
class BookingDependencyAdmin(admin.ModelAdmin):
    form = BookingDependencyForm
    list_display = ('__str__', 'period', 'is_active', 'weekday_names')
    list_filter = ('period',)
    filter_horizontal = ('dependent_events', 'required_events')

@admin.register(CalendarEntry)
class CalendarEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'kind', 'start', 'end', 'all_day', 'created_by')
    list_filter = ('kind',)

@admin.register(BookingChange)
class BookingChangeAdmin(admin.ModelAdmin):
    list_display = ('at', 'action', 'instance', 'host', 'previous_host', 'by', 'note')
    list_filter = ('action',)
    readonly_fields = [f.name for f in BookingChange._meta.fields]

    def has_add_permission(self, request):
        return False

@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('name', 'start', 'end')

@admin.register(EventInstance)
class EventInstanceAdmin(admin.ModelAdmin):
    list_display = ('title', 'start', 'status', 'display_host', 'period')

@admin.register(SchedulingPeriod)
class UserAdmin(admin.ModelAdmin):
    actions = ['assign_unassigned']
    
    def assign_unassigned(self, request, queryset):
    
        if queryset.count() != 1:
            modeladmin.message_user(request, "Can not assign all unassigned to more then one period!")
            return
        rows_updated = EventInstance.objects.filter(period=None).update(period = queryset[0])
        
        if rows_updated == 1:
            message_bit = "1 eventInstance was"
        else:
            message_bit = "%s eventInstances were" % rows_updated
        
        self.message_user(request, "%s successfully assigned to %s." % (message_bit, queryset[0]))
    assign_unassigned.short_description = "Assign all unassigned eventInstances to selected period"

