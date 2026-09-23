import logging
logger = logging.getLogger(__name__)

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
        fields = ('email', 'slackId', 'is_active', 'is_staff', 'is_registration_complete', 'is_profile_complete', 'is_superuser')


class ProfileForm(forms.ModelForm):
    """The details a member must have before using the system.

    Its fields are User.PROFILE_FIELDS, so a field added there is asked for
    here as well.
    """

    class Meta:
        model = User
        fields = User.PROFILE_FIELDS
        help_texts = {'slackId': 'Som i Slack, utan @.'}
        # LANGUAGE_CODE is en-us, so Django's own messages would be English.
        error_messages = {
            'slackId': {
                'required': 'Ange ditt slacknamn.',
                'unique': 'Slacknamnet används redan av ett annat konto.',
                'max_length': 'Slacknamnet får vara högst 100 tecken.',
            },
        }
