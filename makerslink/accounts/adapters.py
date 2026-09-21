import logging
logger = logging.getLogger(__name__)

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.providers.base.constants import AuthProcess
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse


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

    The provider establishes *identity* only. No access flag (is_active,
    is_staff, is_superuser) is ever set or cleared from provider claims:
    who may use the booking system, and who may administer it, stays a
    local decision made in the Django admin.

    MemberMatters does publish membership state today, under its
    "membershipinfo" scope, but its permission model is being reworked and
    those claims are not a stable contract yet. That scope is therefore not
    requested. See the README for what a propagation contract would need.

    Existing accounts keep working as they are: linking happens through
    allauth's connect flow (from the profile page), never by matching on
    e-post address, so a provider cannot claim a local account.
    """

    def list_apps(self, request, provider=None, client_id=None):
        """Let a provider configured in the admin override the one built from
        environment variables.

        allauth blends both sources, and get_app() raises
        MultipleObjectsReturned when two apps match. Configuring the same
        provider in both places is an easy mistake to make, so drop the
        settings-built duplicate instead of failing the login. Apps loaded
        from the database have a primary key; those built from settings do
        not.
        """
        apps = super().list_apps(request, provider=provider, client_id=client_id)
        from_db = {
            (app.provider, app.provider_id) for app in apps if app.pk is not None
        }
        return [
            app for app in apps
            if app.pk is not None
            or (app.provider, app.provider_id) not in from_db
        ]

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
        # is_active deliberately keeps the model default of False, so a new
        # account waits for approval in the admin exactly like one created
        # through e-post registration. Access is never granted by the
        # provider -- see the class docstring.
        return super().save_user(request, sociallogin, form=form)

    def pre_social_login(self, request, sociallogin):
        """Runs on every login and on connect."""
        super().pre_social_login(request, sociallogin)

        if sociallogin.is_existing:
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
