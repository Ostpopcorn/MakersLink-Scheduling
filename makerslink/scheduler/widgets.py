"""
Date widgets: the browser's own date and time inputs.

These replaced django-bootstrap-datepicker-plus 3.0.5, whose template uses
{% ifnotequal %}, removed in Django 4, so every form with a date field
crashed. The class names are kept so forms and scheduler/__init__.py,
which makes them the default for date fields, need no change.
"""
from django import forms


class DateTimePicker(forms.DateTimeInput):
    input_type = 'datetime-local'

    def __init__(self, attrs=None, format='%Y-%m-%dT%H:%M'):
        super().__init__(attrs, format)


class DatePicker(forms.DateInput):
    input_type = 'date'

    def __init__(self, attrs=None, format='%Y-%m-%d'):
        super().__init__(attrs, format)


class DateMonthPicker(DatePicker):
    """Used for period start and end; a plain date input works for those too."""
