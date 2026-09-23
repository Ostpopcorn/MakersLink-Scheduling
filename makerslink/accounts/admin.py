from django.contrib import admin
from .models import User
from .forms import CustomUserCreationForm, CustomUserChangeForm



@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = User
    list_display = ('email', 'slackId', 'is_active', 'is_registration_complete', 'is_profile_complete',
                    'has_password', 'social_logins')
    readonly_fields = ('has_password', 'social_logins')
    actions = ['make_approved']

    def get_queryset(self, request):
        # social_logins reads every listed user's linked accounts; fetch them
        # all in one query instead of one per row.
        return super().get_queryset(request).prefetch_related('socialaccount_set')

    @admin.display(boolean=True, description='Lösenord')
    def has_password(self, obj):
        # Django calls an empty password field "usable", but nobody can log
        # in with it.
        return bool(obj.password) and obj.has_usable_password()

    @admin.display(description='Social inloggning')
    def social_logins(self, obj):
        # The provider ids, e.g. "membermatters" -- the same value the Social
        # accounts admin shows and manual mapping uses.
        return ', '.join(sorted(
            account.provider for account in obj.socialaccount_set.all())) or None

    def make_approved(self, request, queryset):
        rows_updated = queryset.update(is_active=True, is_registration_complete=True)
        if rows_updated == 1:
            message_bit = "1 host was"
        else:
            message_bit = "%s hosts were" % rows_updated
        self.message_user(request, "%s successfully approved." % message_bit)
    make_approved.short_description = "Mark selected users as approved"
