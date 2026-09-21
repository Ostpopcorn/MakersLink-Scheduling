from allauth.core import context
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialApp
from allauth.socialaccount.providers.base.constants import AuthProcess
from django.contrib.auth import authenticate
from django.contrib.messages import get_messages
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from .models import User


MEMBERMATTERS_PROVIDER = {
    'openid_connect': {
        'APPS': [
            {
                'provider_id': 'membermatters',
                'name': 'MemberMatters',
                'client_id': 'test-client',
                'secret': 'test-secret',
                'settings': {
                    'server_url': 'https://mm.example.org/api/openid/',
                    'scope': ['openid', 'profile', 'email'],
                },
            },
        ],
    },
}


def mm_claims(sub, email, username, active=True, groups=None):
    """A userinfo response shaped like the one MemberMatters returns."""
    claims = {
        'sub': sub,
        'email': email,
        'email_verified': True,
        'preferred_username': username,
        'nickname': username,
        'name': 'Test Member',
        'given_name': 'Test',
        'family_name': 'Member',
    }
    if active is not None:
        claims['active'] = active
        claims['state'] = 'active' if active else 'inactive'
    if groups is not None:
        claims['groups'] = groups
    return claims


class MemberMattersFlowMixin:
    """Helpers for driving a social login without live HTTP."""

    def make_request(self, user=None):
        request = RequestFactory().get('/')
        SessionMiddleware(lambda r: None).process_request(request)
        MessageMiddleware(lambda r: None).process_request(request)
        request.session.save()
        if user is None:
            from django.contrib.auth.models import AnonymousUser
            user = AnonymousUser()
        request.user = user
        return request

    def provider(self, request):
        with context.request_context(request):
            return get_adapter().get_provider(
                request, 'openid_connect', client_id='test-client')

    def social_login(self, request, claims, process=AuthProcess.LOGIN):
        # AccountMiddleware normally establishes this context.
        with context.request_context(request):
            sociallogin = self.provider(request).sociallogin_from_response(
                request, claims)
            sociallogin.state['process'] = process
            try:
                complete_social_login(request, sociallogin)
            except ImmediateHttpResponse as exc:
                sociallogin.immediate_response = exc.response
        return sociallogin

    def make_password_user(self, email, slack_id, password='hunter2hunter2'):
        """A user as they exist today: e-post + lösenord, no social account."""
        user = User.objects.create(
            email=email, slackId=slack_id, is_active=True,
            # Set before the password so the registration signal in
            # accounts/signals.py does not deactivate the account.
            is_registration_complete=True)
        user.set_password(password)
        user.save()
        return user


@override_settings(SOCIALACCOUNT_PROVIDERS=MEMBERMATTERS_PROVIDER)
class MemberMattersLoginTestCase(MemberMattersFlowMixin, TestCase):

    # -- Linking an account that already exists -------------------------

    def test_existing_password_user_can_connect_membermatters(self):
        user = self.make_password_user('legacy@example.org', 'legacyuser')
        request = self.make_request(user=user)

        self.social_login(
            request,
            # Deliberately a different e-post than the local account.
            mm_claims('mm-1', 'other@mm.example.org', 'legacyuser'),
            process=AuthProcess.CONNECT)

        self.assertEqual(User.objects.count(), 1)
        account = SocialAccount.objects.get(user=user)
        self.assertEqual(account.provider, 'membermatters')
        self.assertEqual(account.uid, 'mm-1')
        user.refresh_from_db()
        self.assertEqual(user.email, 'legacy@example.org')

    def test_password_still_works_after_connecting(self):
        user = self.make_password_user('legacy@example.org', 'legacyuser')
        self.social_login(
            self.make_request(user=user),
            mm_claims('mm-1', 'legacy@example.org', 'legacyuser'),
            process=AuthProcess.CONNECT)

        self.assertIsNotNone(authenticate(
            self.make_request(), email='legacy@example.org',
            password='hunter2hunter2'))

    def test_connected_user_logs_into_the_same_account(self):
        user = self.make_password_user('legacy@example.org', 'legacyuser')
        self.social_login(
            self.make_request(user=user),
            mm_claims('mm-1', 'legacy@example.org', 'legacyuser'),
            process=AuthProcess.CONNECT)

        sociallogin = self.social_login(
            self.make_request(),
            mm_claims('mm-1', 'legacy@example.org', 'legacyuser'))

        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(sociallogin.user.pk, user.pk)

    def test_manual_admin_mapping_links_an_account(self):
        """Staff can map a MemberMatters sub onto a user by hand."""
        user = self.make_password_user('manual@example.org', 'manualuser')
        SocialAccount.objects.create(
            # Note: the provider_id, not "openid_connect".
            user=user, provider='membermatters', uid='mm-555', extra_data={})

        sociallogin = self.social_login(
            self.make_request(),
            mm_claims('mm-555', 'manual@mm.example.org', 'manualuser'))

        self.assertEqual(sociallogin.user.pk, user.pk)
        self.assertEqual(User.objects.count(), 1)

    def test_unmatched_email_does_not_hijack_a_local_account(self):
        """A provider must not take over an account by asserting its e-post."""
        existing = self.make_password_user('victim@example.org', 'victim')

        self.social_login(
            self.make_request(),
            mm_claims('mm-attacker', 'victim@example.org', 'attacker'))

        # No social account may be attached to the pre-existing user, and
        # their password login must be unaffected.
        self.assertFalse(SocialAccount.objects.filter(user=existing).exists())
        self.assertIsNotNone(authenticate(
            self.make_request(), email='victim@example.org',
            password='hunter2hunter2'))

    def test_unlinked_existing_account_is_sent_to_login_not_signup(self):
        """Without this the member lands on a signup form they cannot
        complete, because their e-post address is already taken."""
        self.make_password_user('dup@example.org', 'dup')
        request = self.make_request()

        self.social_login(
            request, mm_claims('mm-dup', 'dup@example.org', 'dup'))

        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(SocialAccount.objects.count(), 0)
        notices = [m.message for m in get_messages(request)]
        self.assertTrue(any('Kopplade konton' in m for m in notices), notices)

    # -- Provisioning new users -----------------------------------------

    def test_new_user_is_provisioned_from_claims(self):
        self.social_login(
            self.make_request(),
            mm_claims('mm-2', 'new@example.org', 'newmember'))

        user = User.objects.get(email='new@example.org')
        self.assertEqual(user.slackId, 'newmember')
        self.assertTrue(user.is_registration_complete)
        self.assertFalse(user.has_usable_password())

    def test_slack_id_collision_gets_a_suffix(self):
        self.make_password_user('taken@example.org', 'popular')

        self.social_login(
            self.make_request(),
            mm_claims('mm-3', 'new@example.org', 'popular'))

        user = User.objects.get(email='new@example.org')
        self.assertEqual(user.slackId, 'popular-2')

    def test_slack_id_never_contains_an_at_sign(self):
        """slackId has a validator that rejects '@'."""
        self.social_login(
            self.make_request(),
            mm_claims('mm-4', 'noname@example.org', None))

        user = User.objects.get(email='noname@example.org')
        self.assertNotIn('@', user.slackId)
        user.full_clean(exclude=['password'])

    def test_inactive_membership_provisions_an_inactive_user(self):
        self.social_login(
            self.make_request(),
            mm_claims('mm-5', 'lapsed@example.org', 'lapsed', active=False))

        self.assertFalse(User.objects.get(email='lapsed@example.org').is_active)

    # -- Access flags are never set from provider claims ----------------

    def test_new_user_waits_for_approval_even_when_claims_say_active(self):
        """Identity comes from the provider; access does not."""
        self.social_login(
            self.make_request(),
            mm_claims('mm-5', 'eager@example.org', 'eager', active=True,
                      groups=['active', 'staff']))

        user = User.objects.get(email='eager@example.org')
        self.assertFalse(user.is_active)
        self.assertFalse(user.is_staff)

    def test_active_user_is_not_deactivated_by_the_provider(self):
        user = self.make_password_user('member@example.org', 'member')
        self.social_login(
            self.make_request(user=user),
            mm_claims('mm-6', 'member@example.org', 'member'),
            process=AuthProcess.CONNECT)

        self.social_login(
            self.make_request(),
            mm_claims('mm-6', 'member@example.org', 'member', active=False,
                      groups=[]))

        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_staff_is_never_granted_by_the_provider(self):
        user = self.make_password_user('boss@example.org', 'boss')
        self.social_login(
            self.make_request(user=user),
            mm_claims('mm-8', 'boss@example.org', 'boss',
                      groups=['staff', 'admin', 'superuser']),
            process=AuthProcess.CONNECT)

        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_locally_granted_staff_survives_login(self):
        user = self.make_password_user('admin@example.org', 'admin')
        user.is_staff = True
        user.save()
        self.social_login(
            self.make_request(user=user),
            mm_claims('mm-9', 'admin@example.org', 'admin', groups=[]),
            process=AuthProcess.CONNECT)

        self.social_login(
            self.make_request(),
            mm_claims('mm-9', 'admin@example.org', 'admin', groups=[]))

        user.refresh_from_db()
        self.assertTrue(user.is_staff)


class AuthPagesTestCase(TestCase):

    def test_login_page_renders(self):
        self.assertEqual(self.client.get(reverse('login')).status_code, 200)

    @override_settings(SOCIALACCOUNT_PROVIDERS=MEMBERMATTERS_PROVIDER)
    def test_login_page_offers_the_provider(self):
        response = self.client.get(reverse('login'))
        self.assertContains(response, 'Logga in med MemberMatters')

    @override_settings(SOCIALACCOUNT_PROVIDERS={})
    def test_login_page_hides_button_when_unconfigured(self):
        response = self.client.get(reverse('login'))
        self.assertNotContains(response, 'Logga in med')

    def test_logout_accepts_post(self):
        """The nav posts to logout; LogoutView has been POST-only since
        Django 5 and the old <a href> returned 405."""
        user = User.objects.create(
            email='u@example.org', slackId='u', is_active=True,
            is_registration_complete=True)
        user.set_password('hunter2hunter2')
        user.save()
        self.client.force_login(user)
        self.assertEqual(self.client.post(reverse('logout')).status_code, 200)

    @override_settings(SOCIALACCOUNT_PROVIDERS=MEMBERMATTERS_PROVIDER)
    def test_connections_page_requires_login(self):
        response = self.client.get(reverse('socialaccount_connections'))
        self.assertEqual(response.status_code, 302)

    @override_settings(SOCIALACCOUNT_PROVIDERS=MEMBERMATTERS_PROVIDER)
    def test_connections_page_renders_for_a_logged_in_user(self):
        user = User.objects.create(
            email='u@example.org', slackId='u', is_active=True,
            is_registration_complete=True)
        user.set_password('hunter2hunter2')
        user.save()
        self.client.force_login(user)
        response = self.client.get(reverse('socialaccount_connections'))
        self.assertContains(response, 'Koppla MemberMatters')


def make_admin_configured_app(**settings_overrides):
    """A provider configured the way an admin would, in the database."""
    app_settings = {
        'server_url': 'https://mm.example.org/api/openid/',
        'scope': ['openid', 'profile', 'email'],
    }
    app_settings.update(settings_overrides)
    return SocialApp.objects.create(
        provider='openid_connect',
        provider_id='membermatters',
        name='MemberMatters',
        client_id='db-client',
        secret='db-secret',
        settings=app_settings,
    )


@override_settings(SOCIALACCOUNT_PROVIDERS={})
class AdminConfiguredProviderTestCase(MemberMattersLoginTestCase):
    """The same behaviour, with the provider configured in Django admin
    instead of through environment variables."""

    def setUp(self):
        super().setUp()
        self.app = make_admin_configured_app()

    def provider(self, request):
        with context.request_context(request):
            return get_adapter().get_provider(
                request, 'openid_connect', client_id='db-client')


class ProviderConfigurationTestCase(TestCase):

    def test_admin_configured_provider_is_offered_on_the_login_page(self):
        make_admin_configured_app()
        with override_settings(SOCIALACCOUNT_PROVIDERS={}):
            response = self.client.get(reverse('login'))
        self.assertContains(response, 'Logga in med MemberMatters')

    @override_settings(SOCIALACCOUNT_PROVIDERS=MEMBERMATTERS_PROVIDER)
    def test_admin_configuration_wins_over_the_environment_fallback(self):
        """allauth blends both sources and get_app() would otherwise raise
        MultipleObjectsReturned when a provider is configured twice."""
        make_admin_configured_app()
        request = RequestFactory().get('/')

        with context.request_context(request):
            app = get_adapter().get_app(request, 'membermatters')

        self.assertIsNotNone(app.pk)
        self.assertEqual(app.client_id, 'db-client')

    @override_settings(SOCIALACCOUNT_PROVIDERS=MEMBERMATTERS_PROVIDER)
    def test_environment_fallback_is_used_when_admin_has_no_app(self):
        request = RequestFactory().get('/')

        with context.request_context(request):
            app = get_adapter().get_app(request, 'membermatters')

        self.assertIsNone(app.pk)
        self.assertEqual(app.client_id, 'test-client')

    def test_social_application_is_editable_in_admin(self):
        from django.contrib import admin as django_admin
        self.assertIn(SocialApp, django_admin.site._registry)
