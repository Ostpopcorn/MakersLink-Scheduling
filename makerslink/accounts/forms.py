import logging
logger = logging.getLogger(__name__)

from allauth.socialaccount.forms import SignupForm
from django import forms
from .models import User
from django.contrib.auth.forms import UserCreationForm, UserChangeForm

class RegistrationForm(forms.ModelForm):

    class Meta:
        model = User
        fields = ['email', 'slackId']

class CustomUserCreationForm(UserCreationForm):

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('email', 'slackId')

class CustomUserChangeForm(UserChangeForm):

    class Meta:
        model = User
        fields = ('email', 'slackId', 'is_active', 'is_staff', 'is_registration_complete',  'is_superuser')


class SocialSignupForm(SignupForm):
    """Completes an account the first time someone logs in with a provider.

    Shown once, before the account exists. E-post comes from the provider and
    is locked; Slacknamn is prefilled with a guess the member confirms or
    corrects.
    """

    slackId = forms.CharField(
        label='Slacknamn', max_length=100,
        validators=[User.slackIdValid],
        help_text='Som i Slack, utan @.')

    field_order = ['email', 'slackId']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].label = 'E-post'
        self.fields['email'].widget.attrs['placeholder'] = 'E-postadress'
        # A disabled field ignores whatever is posted and keeps its initial
        # value, so editing the form in the browser cannot change the address.
        # Left editable only if the provider sent none, since the member could
        # otherwise never submit the form.
        if self.initial.get('email'):
            self.fields['email'].disabled = True

    def clean_slackId(self):
        value = self.cleaned_data['slackId']
        if User.objects.filter(slackId=value).exists():
            raise forms.ValidationError(
                'Slacknamnet används redan av ett annat konto.')
        return value
