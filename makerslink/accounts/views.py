import logging
logger = logging.getLogger(__name__)

from django.shortcuts import render, resolve_url
from .adapters import slack_id_guess_for
from .models import User
from .forms import ProfileForm, RegistrationForm
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic.edit import CreateView, UpdateView
from django.urls import reverse_lazy
from django.contrib.auth.forms import PasswordResetForm

# Create your views here.

class RegistrationView(CreateView):
    form_class = RegistrationForm
    success_url = reverse_lazy('register-done')
    model = User

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.set_password(User.objects.make_random_password())
        obj.is_active = True  # PasswordResetForm won't send to inactive users.
        obj.save()

        # This form only requires the "email" field, so will validate.
        reset_form = PasswordResetForm(self.request.POST)
        reset_form.is_valid()  # Must trigger validation
        # Copied from django/contrib/auth/views.py : password_reset
        opts = {
            'use_https': self.request.is_secure(),
            'email_template_name': 'accounts/registration_password_email.html',
            'subject_template_name': 'accounts/verification_subject.txt',
            'request': self.request,
            # 'html_email_template_name': provide an HTML content template if you desire.
        }
        # This form sends the email on save()
        reset_form.save(**opts)
        #logger.warning("get sucess url:"+self.get_success_url())
        
        #return redirect(self.get_success_url())
        
        
        return CreateView.form_valid(self, form)


class CompleteProfileView(LoginRequiredMixin, UpdateView):
    """Asks a logged-in member for what User.PROFILE_FIELDS still lacks.

    accounts.middleware sends every request here until the profile is
    complete, with ?next= holding the page the member was going to.
    """
    form_class = ProfileForm
    template_name = 'accounts/complete_profile.html'

    def dispatch(self, request, *args, **kwargs):
        if (request.user.is_authenticated
                and not request.user.needs_more_information()):
            return HttpResponseRedirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        return self.request.user

    def get_initial(self):
        initial = super().get_initial()
        guess = slack_id_guess_for(self.request.user)
        if guess:
            initial['slackId'] = guess
        return initial

    def form_valid(self, form):
        form.instance.is_profile_complete = True
        return super().form_valid(form)

    def get_success_url(self):
        # The form posts back to its own URL, so ?next= is still there.
        next_url = self.request.GET.get('next')
        if next_url and url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={self.request.get_host()},
                require_https=self.request.is_secure()):
            return next_url
        return resolve_url(settings.LOGIN_REDIRECT_URL)
