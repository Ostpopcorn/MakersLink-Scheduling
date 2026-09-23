import logging
logger = logging.getLogger(__name__)

from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.http import urlencode


class RequireCompleteProfileMiddleware:
    """Sends a logged-in member who still lacks required details to the form
    that asks for them, whatever page they were trying to open.

    Checked on every request rather than only right after login, so the form
    cannot be skipped by typing another address. The check reads a field on
    the user that the page loads anyway.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if (user.is_authenticated and user.needs_more_information()
                and not self.is_exempt(request.path)):
            return HttpResponseRedirect('%s?%s' % (
                reverse('complete-profile'),
                urlencode({'next': request.get_full_path()})))
        return self.get_response(request)

    def is_exempt(self, path):
        # The form itself, a way out, and the files the form page needs.
        return (path in (reverse('complete-profile'), reverse('logout'))
                or path.startswith(settings.STATIC_URL))
