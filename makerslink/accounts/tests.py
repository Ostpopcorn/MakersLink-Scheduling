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
from django.utils.http import urlencode

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
                sociallogin.response = complete_social_login(
                    request, sociallogin)
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

    def test_new_user_must_confirm_their_profile(self):
        """The Slacknamn is only a guess until the member has confirmed it."""
        self.social_login(
            self.make_request(),
            mm_claims('mm-2', 'new@example.org', 'newmember'))

        user = User.objects.get(email='new@example.org')
        self.assertFalse(user.is_profile_complete)
        self.assertTrue(user.needs_more_information())

    def test_new_user_sees_the_approval_page_first(self):
        """New accounts are inactive, so there is no session yet for the
        profile check to run in; that comes at the first login after
        approval."""
        sociallogin = self.social_login(
            self.make_request(),
            mm_claims('mm-2', 'new@example.org', 'newmember'))

        self.assertEqual(sociallogin.response['Location'],
                         reverse('account_inactive'))

    def test_slack_id_collision_gets_a_provisional_suffix(self):
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
            mm_claims('mm-4', 'bob@example.org', 'bob@slack.example.org'))

        user = User.objects.get(email='bob@example.org')
        self.assertEqual(user.slackId, 'bob')
        user.full_clean(exclude=['password'])

    def test_placeholder_screen_name_is_not_used_as_a_guess(self):
        """MemberMatters sends NO_SCREENNAME for a member without a screen
        name; the e-post local part is a better guess than that."""
        self.social_login(
            self.make_request(),
            mm_claims('mm-2', 'carl.s@example.org', 'NO_SCREENNAME'))

        self.assertEqual(
            User.objects.get(email='carl.s@example.org').slackId, 'carl.s')

    def test_inactive_membership_provisions_an_inactive_user(self):
        self.social_login(
            self.make_request(),
            mm_claims('mm-5', 'lapsed@example.org', 'lapsed', active=False))

        self.assertFalse(User.objects.get(email='lapsed@example.org').is_active)

    def test_connecting_leaves_an_existing_profile_complete(self):
        """Linking MemberMatters must not send an existing member to the
        profile form: they chose their Slacknamn when they registered."""
        user = self.make_password_user('legacy@example.org', 'legacyuser')

        self.social_login(
            self.make_request(user=user),
            mm_claims('mm-1', 'legacy@example.org', 'someone-else'),
            process=AuthProcess.CONNECT)

        user.refresh_from_db()
        self.assertTrue(user.is_profile_complete)
        self.assertEqual(user.slackId, 'legacyuser')

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

    def test_inactive_page_is_in_swedish(self):
        """allauth's own template is English and says only "This account is
        inactive."; the override explains the approval requirement."""
        response = self.client.get(reverse('account_inactive'))
        self.assertContains(response, 'Otillräcklig behörighet')
        self.assertContains(response, 'godkännas av en administratör')
        self.assertNotContains(response, 'This account is inactive')

    @override_settings(SUPPORT_EMAIL='styrelsen@example.org')
    def test_inactive_page_offers_a_contact_address(self):
        response = self.client.get(reverse('account_inactive'))
        self.assertContains(response, 'mailto:styrelsen@example.org')

    @override_settings(SUPPORT_EMAIL='')
    def test_inactive_page_reads_correctly_without_a_contact_address(self):
        """The sentence still has to end in a full stop, not a dangling
        preposition, when no address is configured."""
        response = self.client.get(reverse('account_inactive'))
        self.assertContains(response, 'Kontakta en administratör så')
        self.assertNotContains(response, 'mailto:')

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


@override_settings(SOCIALACCOUNT_PROVIDERS=MEMBERMATTERS_PROVIDER)
class ProfileCompletionTestCase(MemberMattersFlowMixin, TestCase):
    """The check after login that sends a member to the profile form."""

    def approved_member(self, claims):
        """A member created by a first MemberMatters login and then approved
        in the admin -- the first point at which they can log in at all."""
        self.social_login(self.make_request(), claims)
        user = User.objects.get(email=claims['email'])
        user.is_active = True
        user.save()
        self.client.force_login(user)
        return user

    def form(self, **query):
        url = reverse('complete-profile')
        return self.client.get(url, query).context['form']

    def submit(self, slack_id, next_url=None):
        url = reverse('complete-profile')
        if next_url is not None:
            url += '?' + urlencode({'next': next_url})
        return self.client.post(url, {'slackId': slack_id})

    # -- Who is sent to the form ----------------------------------------

    def test_incomplete_member_is_sent_to_the_form_from_any_page(self):
        self.approved_member(mm_claims('mm-1', 'new@example.org', 'newbie'))

        for page in (reverse('socialaccount_connections'),
                     reverse('host-signup'), '/admin/'):
            response = self.client.get(page)
            self.assertRedirects(
                response,
                reverse('complete-profile') + '?' + urlencode({'next': page}),
                fetch_redirect_response=False, msg_prefix=page)

    def test_complete_member_is_not_redirected(self):
        user = self.make_password_user('member@example.org', 'member')
        self.client.force_login(user)

        response = self.client.get(reverse('socialaccount_connections'))
        self.assertEqual(response.status_code, 200)

    def test_anonymous_visitors_are_not_affected(self):
        self.assertEqual(self.client.get(reverse('login')).status_code, 200)

    def test_logout_works_while_incomplete(self):
        self.approved_member(mm_claims('mm-1', 'new@example.org', 'newbie'))

        response = self.client.post(reverse('logout'))
        self.assertEqual(response.status_code, 200)

    def test_static_files_are_not_gated(self):
        self.approved_member(mm_claims('mm-1', 'new@example.org', 'newbie'))

        response = self.client.get('/static/css/anything.css')
        self.assertNotEqual(response.get('Location', ''),
                            reverse('complete-profile'))
        self.assertNotIn(reverse('complete-profile'),
                         response.get('Location', ''))

    # -- The form ----------------------------------------------------------

    def test_form_says_more_information_is_needed(self):
        self.approved_member(mm_claims('mm-1', 'new@example.org', 'newbie'))

        response = self.client.get(reverse('complete-profile'))
        self.assertContains(
            response,
            'Vi behöver mer information innan du kan använda systemet.')
        self.assertContains(response, 'Slacknamn')

    def test_form_prefills_the_guess_not_the_suffixed_name(self):
        """The stored "popular-2" only exists to keep the name unique; the
        form offers the member's actual screen name instead."""
        self.make_password_user('taken@example.org', 'popular')
        user = self.approved_member(
            mm_claims('mm-1', 'new@example.org', 'popular'))
        self.assertEqual(user.slackId, 'popular-2')

        self.assertEqual(self.form().initial['slackId'], 'popular')

    def test_form_falls_back_to_the_stored_name_without_a_provider(self):
        """If the provider has since been removed from the admin, there is
        nothing to ask, so the stored value is offered."""
        self.make_password_user('taken@example.org', 'popular')
        self.approved_member(mm_claims('mm-1', 'new@example.org', 'popular'))

        with override_settings(SOCIALACCOUNT_PROVIDERS={}):
            form = self.form()
        self.assertEqual(form.initial['slackId'], 'popular-2')

    def test_submitting_completes_the_profile_and_continues(self):
        user = self.approved_member(
            mm_claims('mm-1', 'new@example.org', 'newbie'))
        page = reverse('socialaccount_connections')

        response = self.submit('anna.s', next_url=page)

        self.assertRedirects(response, page, fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual(user.slackId, 'anna.s')
        self.assertTrue(user.is_profile_complete)
        self.assertEqual(self.client.get(page).status_code, 200)

    def test_member_may_keep_the_suggested_slack_id(self):
        user = self.approved_member(
            mm_claims('mm-1', 'new@example.org', 'newbie'))

        self.submit('newbie')

        user.refresh_from_db()
        self.assertEqual(user.slackId, 'newbie')
        self.assertTrue(user.is_profile_complete)

    def test_taken_slack_id_is_rejected(self):
        self.make_password_user('taken@example.org', 'popular')
        user = self.approved_member(
            mm_claims('mm-1', 'new@example.org', 'newbie'))

        response = self.submit('popular')

        self.assertContains(
            response, 'Slacknamnet används redan av ett annat konto.')
        user.refresh_from_db()
        self.assertFalse(user.is_profile_complete)

    def test_slack_id_with_an_at_sign_is_rejected(self):
        self.approved_member(mm_claims('mm-1', 'new@example.org', 'newbie'))

        self.assertContains(self.submit('@anna'), 'utan @-tecken')

    def test_empty_slack_id_is_rejected(self):
        self.approved_member(mm_claims('mm-1', 'new@example.org', 'newbie'))

        self.assertContains(self.submit(''), 'Ange ditt slacknamn.')

    def test_unsafe_next_is_ignored(self):
        self.approved_member(mm_claims('mm-1', 'new@example.org', 'newbie'))

        response = self.submit('anna.s', next_url='https://evil.example.org/')

        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_form_sends_a_complete_member_on(self):
        user = self.make_password_user('member@example.org', 'member')
        self.client.force_login(user)

        response = self.client.get(reverse('complete-profile'))
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_profile_flag_is_editable_in_admin(self):
        from .forms import CustomUserChangeForm
        self.assertIn('is_profile_complete', CustomUserChangeForm._meta.fields)


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
