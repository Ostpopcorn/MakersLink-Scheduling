import logging
logger = logging.getLogger(__name__)

from django.core.validators import RegexValidator
from django.db import models
from .managers import UserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

# Create your models here.

class User(AbstractBaseUser, PermissionsMixin):
    USERNAME_FIELD = 'email'
    # Field required to create user, used when creating users from teminal
    REQUIRED_FIELDS = ['slackId']
    
    class Meta:
        verbose_name = 'user'
        verbose_name_plural = 'users'
    
    objects = UserManager()
    
    slackIdValid = RegexValidator(r'^((?!@).)*$', 'Ange ditt slacknamn utan @-tecken.', 
            code='invalid_slackId')
    
    email = models.EmailField(unique=True, verbose_name="E-post")
    slackId = models.CharField(max_length=100, verbose_name="Slacknamn", unique=True, blank=False, null=False, validators=[slackIdValid])
    is_active = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    is_registration_complete = models.BooleanField(default=False)
    # False while the member still has to confirm or fill in PROFILE_FIELDS,
    # e.g. after an account was created from a provider login with a guessed
    # Slacknamn. Every other way of creating an account asks for them up
    # front, hence the default.
    is_profile_complete = models.BooleanField(
        default=True, verbose_name="Profil komplett")

    # What a member must have filled in before using the system. The
    # accounts.middleware gate and the form it sends people to both follow
    # this list, so adding a field here asks every member who lacks it.
    PROFILE_FIELDS = ['slackId']

    def needs_more_information(self):
        return not self.is_profile_complete or any(
            not getattr(self, name) for name in self.PROFILE_FIELDS)

    def get_full_name(self):
        return self.email
    def get_short_name(self):
        return self.email
