import logging
logger = logging.getLogger(__name__)

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.providers.base.constants import AuthProcess
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse


# Matches the provider_id configured in SOCIALACCOUNT_PROVIDERS. This is what
# SocialAccount.provider stores, not the base provider "openid_connect".
MEMBERMATTERS_PROVIDER_ID = 'membermatters'

# Claims that MemberMatters returns for the "membershipinfo" scope. See
# memberportal/membermatters/oidc_provider_settings.py in MemberMatters.
ACTIVE_CLAIM = 'active'
GROUPS_CLAIM = 'groups'
# Groups that should map to is_staff here, when staff syncing is enabled.
STAFF_GROUPS = ('staff', 'admin')


def sanitize_slack_id(value):
    """Turn a provider claim into something the slackId validator accepts.

    slackId rejects '@' entirely, so an e-post address falls back to its
    local part.
    """
    if not value:
        return ''
    return str(value).split('@')[0].strip()


class MemberMattersSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Maps MemberMatters OIDC claims onto accounts.User.

    Existing accounts keep working as they are: linking happens through
    allauth's connect flow (from the profile page), never by matching on
    e-post address, so a provider cannot claim a local account.
    """

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)

        # accounts.User has no username field, so allauth leaves slackId
        # alone. "username" here is the provider's preferred_username.
        if not user.slackId:
            user.slackId = self.generate_unique_slack_id(
                sanitize_slack_id(data.get('username'))
                or sanitize_slack_id(data.get('email'))
            )

        # Without this the pre_save signal in accounts/signals.py treats the
        # account as a half-finished e-post registration and deactivates it.
        user.is_registration_complete = True
        return user

    def generate_unique_slack_id(self, base):
        """slackId is unique and used as a URL slug, so collisions with
        accounts that already exist have to be resolved before saving."""
        from .models import User

        base = base or 'member'
        candidate = base[:100]
        suffix = 2
        while User.objects.filter(slackId=candidate).exists():
            tail = '-%d' % suffix
            candidate = base[:100 - len(tail)] + tail
            suffix += 1
        return candidate

    def save_user(self, request, sociallogin, form=None):
        user = sociallogin.user
        # No password is ever usable on a provider-provisioned account.
        # set_unusable_password() leaves _password as None, which keeps the
        # accounts/signals.py registration signal from deactivating the user.
        user.set_unusable_password()
        user = super().save_user(request, sociallogin, form=form)
        self.sync_membership(user, sociallogin)
        return user

    def pre_social_login(self, request, sociallogin):
        """Runs on every login and on connect, so membership changes made in
        MemberMatters take effect the next time someone signs in."""
        super().pre_social_login(request, sociallogin)

        if sociallogin.is_existing:
            self.sync_membership(sociallogin.user, sociallogin)
            return

        self.guide_existing_account_to_connect(request, sociallogin)

    def guide_existing_account_to_connect(self, request, sociallogin):
        """Send someone who already has a local account to the login page.

        Accounts are never matched on e-post address, so a member whose
        MemberMatters e-post matches an existing local account would
        otherwise land on a signup form they cannot complete. Point them at
        password login plus the connect flow instead.
        """
        from .models import User

        if sociallogin.state.get('process') == AuthProcess.CONNECT:
            return
        if request.user.is_authenticated:
            return

        email = (sociallogin.user.email or '').strip()
        if not email or not User.objects.filter(email__iexact=email).exists():
            return

        messages.info(
            request,
            'Det finns redan ett konto med e-postadressen %s. Logga in med '
            'ditt lösenord och välj sedan "Kopplade konton" för att koppla '
            'ihop det med MemberMatters. Nästa gång kan du logga in direkt '
            'med MemberMatters.' % email)
        raise ImmediateHttpResponse(redirect(reverse('login')))

    def sync_membership(self, user, sociallogin):
        """Mirror MemberMatters membership state onto the local account.

        Only claims that are actually present are applied, so losing the
        membershipinfo scope never silently locks anyone out. Superusers are
        never touched.
        """
        if sociallogin.account.provider != MEMBERMATTERS_PROVIDER_ID:
            return

        claims = sociallogin.account.extra_data or {}
        fields = []

        sync_active = getattr(settings, 'MEMBERMATTERS_SYNC_IS_ACTIVE', True)
        if sync_active and ACTIVE_CLAIM in claims:
            is_active = bool(claims.get(ACTIVE_CLAIM))
            if user.is_active != is_active:
                user.is_active = is_active
                fields.append('is_active')

        sync_staff = getattr(settings, 'MEMBERMATTERS_SYNC_IS_STAFF', False)
        if sync_staff and GROUPS_CLAIM in claims and not user.is_superuser:
            groups = claims.get(GROUPS_CLAIM) or []
            is_staff = any(group in groups for group in STAFF_GROUPS)
            if user.is_staff != is_staff:
                user.is_staff = is_staff
                fields.append('is_staff')

        if fields and user.pk:
            logger.info(
                "Synced %s from MemberMatters: %s", user.email, ', '.join(fields))
            user.save(update_fields=fields)
