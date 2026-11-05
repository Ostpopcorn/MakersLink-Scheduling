from django.conf import settings

def extra_menu_processor(request):
    extra = {}
    if hasattr(settings, "MENU_TITLE"):
    	extra['menu_title'] = settings.MENU_TITLE
    if hasattr(settings, "LOGO"):
        extra['logo'] = settings.LOGO
    if hasattr(settings, "MENU_EXTRA"):
        extra['menu_extra'] = settings.MENU_EXTRA
    
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and user.is_staff:
        # Shown as a badge on "Kalenderkontroll" in the staff menu.
        from .calendar_check import attention_count
        extra['calendar_check_count'] = attention_count()

    from .menu import build_menu
    extra['menu'] = build_menu(request, extra.get('calendar_check_count', 0))
    return extra
