# MakersLink-Scheduling — Bug Audit

Audit date: 2026-09-21. Commit audited: `011a604`.

Every finding below was reproduced against the pinned stack
(Python 3.12 + `requirements.txt`, i.e. Django 6.0.2) using a throwaway test
database, unless explicitly marked *unverified*. Reproductions were run with
`DiscoverRunner`-provisioned databases and Django's test `Client`; no project
file was modified and the committed database was not touched.

Complexity estimates:

| Label | Meaning |
| --- | --- |
| **XS** | one line / one word, < 15 min |
| **S** | contained change in one function, < 1 h |
| **M** | touches several call sites or needs a migration, half a day |
| **L** | design change, multi-day |

---

## Ranked findings

| # | Severity | Finding | Complexity |
| --- | --- | --- | --- |
| 1 | Critical | Registration is dead: `make_random_password()` was removed in Django 5.1 | XS |
| 2 | Critical | Deleting a template/calendar `SET_NULL`s FKs and permanently 500s several pages | M |
| 3 | Critical | Any logged-in user can rewrite the date/time of any free event instance | M |
| 4 | Critical | Cancelled/deleted bookings are never removed from Google Calendar | XS |
| 5 | High | `check_unique_together` signal is Python-2 code — `NameError` on any duplicate | S |
| 6 | High | No current `SchedulingPeriod` ⇒ 500s; and the *first* period can never be created | S |
| 7 | High | Participant capacity (`num_participants`) is never enforced | S |
| 8 | High | Front page issues ~1900 queries / 1.9 s / 638 KB for a small dataset | M |
| 9 | High | `display_host()` crashes on host-less instances — Django admin changelist unusable | XS |
| 10 | High | Live SQLite DB with real accounts committed to git and baked into the image | S (+L to fully remediate) |
| 11 | High | `Event.clean()` uses `>` where the DB constraint uses `<` ⇒ `IntegrityError` 500 | XS |
| 12 | Medium | `unique_title` / `unique_description` never reach Google Calendar | S |
| 13 | Medium | Calendar API calls inside `save()`/`delete()`, outside any transaction | M |
| 14 | Medium | `ZeroDivisionError` in host statistics | XS |
| 15 | Medium | Bare `except:` swallows every Google API error | XS |
| 16 | Medium | `assign_unassigned` admin action raises `NameError` | XS |
| 17 | Medium | `finish_registration` re-enters `save()` and can deactivate existing users | S |
| 18 | Medium | Container startup runs `makemigrations` and `runserver --insecure` | S |
| 19 | Medium | Production security settings missing; dev `SECRET_KEY` committed | S |
| 20 | Medium | Booking race conditions — no locking, and the ownership check trusts POST data | M |
| 21 | Low | `{ message }}` typo — messages render as literal text | XS |
| 22 | Low | Duplicate URL pattern; `eventinstance-detail` resolves to the join view | XS |
| 23 | Low | Self-XSS: client-controlled `title` echoed through `{{ message|safe }}` | S |
| 24 | Low | Dead / broken-on-arrival code (`EventManager`, `views.index`, `caltest.py`, …) | XS |
| 25 | Low | `host_detail.html` regroups a variable the view never supplies, and shadows `period_list` | XS |
| 26 | Low | HTML `id`s built from free-text template names break the collapse widgets | S |
| 27 | Low | `HttpResponseRedirect('')` — empty `Location` header | XS |
| 28 | Low | Naive `datetime.now()` compared against `DateTimeField`s | S |
| 29 | Low | `get_participant_key_list()` likely undercounts repeated count keys (*unverified*) | M |
| 30 | Low | `requirements.txt` is a raw `pip freeze`; several pins are years stale | M |
| 31 | Low | No tests at all — both `tests.py` are empty stubs | L |

---

## Critical

### 1. Registration is dead — `make_random_password()` removed in Django 5.1
`makerslink/accounts/views.py:20`

```python
obj.set_password(User.objects.make_random_password())
```

`BaseUserManager.make_random_password()` was deprecated in Django 4.2 and
removed in 5.1. `requirements.txt` pins `Django==6.0.2`.

Reproduced — `POST /accounts/register/`:
`AttributeError: 'UserManager' object has no attribute 'make_random_password'`
(HTTP 500). **No one can create an account.**

Fix: `django.utils.crypto.get_random_string(32)`, or better
`obj.set_unusable_password()` — the account is activated through the
password-reset email anyway, so the random password is never used.

**Complexity: XS.**

### 2. Deleting a template or calendar permanently breaks pages
`makerslink/scheduler/models.py` — `Event.template`, `EventInstance.event`,
`EventInstance.host`, `EventInstance.period`, `EventTemplate.calendar` are all
`on_delete=models.SET_NULL`, but essentially no code path handles `None`.

`EventTemplateDeleteView` is a normal staff-facing button. After using it:

| URL | Before | After |
| --- | --- | --- |
| `/scheduler/eventinstances-admin` | 200 | `AttributeError: 'NoneType' object has no attribute 'title'` |
| `/scheduler/joinEvent` | 200 | same |
| `/admin/scheduler/eventinstance/` | 200 | same |
| `EventInstance.save()` | ok | `'NoneType' object has no attribute 'createEventEntry'` |

The damage is not recoverable through the UI — the affected pages are exactly
the ones you would use to repair the data.

Separately, a template with `synchronize=True` and `calendar=None` (the default
when a calendar is deleted, and also reachable straight from the create form)
fails at save time: `'NoneType' object has no attribute 'timezone'`.

Fix: switch the structural FKs (`Event.template`, `EventInstance.event`,
`EventTemplate.calendar`) to `PROTECT` with a migration, and null-guard the
genuinely optional ones (`host`, `period`). Add a `clean()` on `EventTemplate`
rejecting `synchronize=True` with no calendar.

**Complexity: M** — a migration plus ~15 call sites.

### 3. Any logged-in user can rewrite the schedule of any free event instance
`makerslink/scheduler/forms.py:70-76`, `makerslink/scheduler/views.py:369-383`

`EventInstanceForm` exposes `start`, `end`, `status`, `event` and `period` as
`HiddenInput`, and `EventSignupView` does `form.save(commit=False)` and trusts
the result. Hidden inputs are not read-only inputs.

Reproduced — user *bob* edits the hidden fields of a free instance and posts:

```
start was 2026-10-06 10:23:43+00:00  ->  now 2031-01-01 02:00:00+00:00
status=1, host=bob
```

The change is also pushed to the public Google Calendar by
`EventInstance.save()`. The same vector lets a user re-point an instance at a
different `event` or `period`.

The ownership check has the same shape of problem: `can_take()` is evaluated
against the **POSTed** `status`, not the value in the database.

Fix: the form should carry only `perform_action` plus the row identity; re-read
`status`/`start`/`end` from the database inside the POST handler and ignore
whatever the client sent.

**Complexity: M** — the formset drives the whole signup page, so the template
and the initial-data construction move together.

### 4. Cancelled/deleted bookings are never removed from Google Calendar
`makerslink/scheduler/models.py:562-566`

```python
def delete(self, *args, **kwargs):
    if self.google_calendar_booking_id is not None:
        if not self.event.template.updateEventEntry(...):   # <- should be deleteEventEntry
```

Reproduced by instrumenting both methods: deleting an `EventInstance` produces
`calendar calls on delete = ['update']`. `deleteEventEntry()` exists at
`models.py:122` and is never called from anywhere.

Every booking deleted since this code was written is still on the public
calendar, so the calendar shows opening hours that the space is not keeping.

Fix: call `deleteEventEntry(self.google_calendar_booking_id)`. Budget a one-off
reconciliation pass over the calendar for the already-orphaned entries.

**Complexity: XS** for the code; the cleanup is separate.

---

## High

### 5. `check_unique_together` is Python-2 code that never ran
`makerslink/scheduler/signals/handlers.py:45-46`

```python
'model_name': unicode(instance.__class__.__name__),
```

`unicode` does not exist in Python 3, and `FieldDoesNotExist` (line 30) is never
imported. Reproduced by saving a duplicate `EventInstance`:
`NameError: name 'unicode' is not defined`.

Because this is a `pre_save` receiver, the failure lands **after**
`EventInstance.save()` has already created the Google Calendar entry — so the
row is rolled back while the calendar entry survives, with its id lost.

The premise of the file is also obsolete: the docstring claims SQLite does not
enforce `unique_together`. It does, and Django emits the constraint. The
handler only adds a `COUNT(*)` query to every single save (visible in finding 8)
and a TOCTOU race.

Fix: delete the module and let the database constraint raise `IntegrityError`,
then handle that where bookings are taken.

**Complexity: S.**

### 6. No current period ⇒ 500s; the first period can never be created
`makerslink/scheduler/views.py:148,216,266`, `makerslink/scheduler/forms.py:45`

`SchedulingPeriod.get_current_period()` legitimately returns `None` when no
period covers today (verified), but callers dereference it directly. Reproduced
with periods that do not span today:

- `/scheduler/events` → `AttributeError: 'NoneType' object has no attribute 'end'`
- `/scheduler/profile` → `'NoneType' object has no attribute 'get_host_stats'`

This is not exotic — it happens on any gap between two periods, and it takes out
the page staff need to *fix* the gap.

Worse, `SchedulingPeriod.objects.latest('end')` in `PeriodCreateView` and
`PeriodForm.save()` has no empty-table guard. Reproduced on an empty table:
`/scheduler/period/create/` → `SchedulingPeriod.DoesNotExist`. **A fresh
install can never create its first period through the UI.**

Fix: `.first()` / `.exists()` guards plus an empty-state message in the
templates.

**Complexity: S.**

### 7. Participant capacity is never enforced
`makerslink/scheduler/views.py:246-254`

`EventInstanceUpdateView.form_valid()` toggles participation with no check
against `max_num_participants` (which is `EventTemplate.num_participants`, and
is otherwise carefully threaded through the models).

Reproduced — four users joined an instance whose template allows two:
`max_num_participants=2, actually joined=4, codes=[302, 302, 302, 302]`.

The `-1 for infinite` convention documented on `num_participants` is not
implemented anywhere either.

Fix: check capacity (honouring `-1`) before `participants.add()`, and report
refusal via `messages`.

**Complexity: S.**

### 8. The front page issues ~1900 queries per request
`makerslink/scheduler/models.py:289-301,364-372`, `makerslink/scheduler/forms.py:78-86`

`EventSignupView` is mounted at both `/scheduler/` and `/scheduler/signup`.
Measured with three daily-recurring events over a four-month window — a
deliberately modest dataset:

```
GET /scheduler/signup -> 200; queries=1922; 1.88s; html=638KB
    762x SELECT ... FROM scheduler_schedulingperiod ...
    382x SELECT ... FROM scheduler_event ...
    381x SELECT COUNT(*) FROM scheduler_schedulingperiod ...
    381x SELECT ... FROM scheduler_eventtemplate ...
```

Three separate N+1 loops:

- `Event.create_eventinstance()` runs a `SchedulingPeriod` filter **and** a
  `.count()` for every generated occurrence;
- `Event.get_events()` re-fetches `eventinstance_set` per `Event`;
- `EventInstanceForm.__init__()` runs `Event.objects.get()` and
  `SchedulingPeriod.objects.get()` for every one of the ~380 forms.

`DATA_UPLOAD_MAX_NUMBER_FIELDS = 10240` in `settings.py` is a symptom of the
same design: the page posts back every slot as hidden fields.

Fix: load periods once into an interval map, `select_related('template')`, and
pass the resolved objects into the form instead of re-querying by pk.

**Complexity: M.**

### 9. `display_host()` crashes on host-less instances
`makerslink/scheduler/models.py:568-570` — `''.join([self.host.email])`

`host` is nullable and is `None` for every unbooked instance, and
`display_host` is in `EventInstanceAdmin.list_display`. Reproduced:
`/admin/scheduler/eventinstance/` → `AttributeError: 'NoneType' object has no
attribute 'email'`. One free instance takes out the whole changelist.

`as_dict()` (`models.py:572`) has the same shape — `self.period.id` raises when
`period` is `None`, which is exactly the state the `assign_unassigned` admin
action exists to repair.

Fix: `return self.host.email if self.host else ""`, and the equivalent in
`as_dict()` / `__str__`.

**Complexity: XS.**

### 10. The live SQLite database is committed to git
`makerslink/makerslink/db/db.sqlite3` — 292 KB, tracked, and re-committed
across at least five commits (most recently `f72b3fa`).

It contains `accounts_user` (6 rows — e-mail addresses and password hashes),
`django_session`, `django_admin_log` and 31 `scheduler_eventinstance` rows.
`.gitignore` only excludes `/makerslink/makerslink/db/*.back`, and the
Dockerfile's `ADD . /scheduling` copies it into every image (the runtime
bind-mount shadows it, but the image still ships the data).

Fix: `git rm --cached` + `.gitignore`. Full remediation means rewriting history
and rotating every affected credential — treat the hashes as disclosed.

**Complexity: S** to stop the bleeding, **L** to fully remediate.

### 11. `Event.clean()` disagrees with the database constraint
`makerslink/scheduler/models.py:266-275` vs `models.py:250-259`

`clean()` rejects `start > end`; the `CheckConstraint` requires `start < end`.
`start == end` therefore passes form validation and then blows up at the
database. Reproduced: `IntegrityError: CHECK constraint failed:
check_start_before_end` — an unhandled 500 where the user should have seen a
field error. `repeat_end` has the same mismatch, as does
`EventInstance.clean()` (`models.py:526`).

Fix: use `>=` in all three `clean()` methods.

**Complexity: XS.**

---

## Medium

### 12. `unique_title` / `unique_description` never reach the calendar
`makerslink/scheduler/models.py:60-78`

`_createUpdatedEventData()` accepts `unique_title` and `unique_description`, but
neither `createEventEntry()` nor `updateEventEntry()` forwards them, and
`EventInstance.save()` does not pass them either. The two model fields are
editable in `EventInstanceAdminForm`, so staff can fill them in and believe they
took effect.

Reproduced — an instance cancelled with `unique_title="CUSTOM: "` and
`unique_description="custom desc"` produces
`summary='Inställt: Open'`, `description='Detta pass har blivit inställt…'` —
the global defaults.

**Complexity: S.**

### 13. Calendar API calls inside `save()` / `delete()`, outside any transaction
`makerslink/scheduler/models.py:540-566`

The Google call happens *before* `super().save()`. Any later failure —
validation, the unique constraint, finding 5's `NameError` — leaves a live
calendar entry whose id was never persisted. There is no `transaction.atomic`,
no retry, and no reconciliation.

It also means every participant toggle (finding 7) issues a calendar write.

Fix: move synchronisation out of `save()` into an explicit service function
called from the views, wrapped in `transaction.atomic()` with
`on_commit()` for the outbound call.

**Complexity: M.**

### 14. `ZeroDivisionError` in host statistics
`makerslink/scheduler/models.py:859-920`, `makerslink/scheduler/views.py:281-284`

`max_commited_required` is `max(total, num_required_events + len(key_string))`
and is used as a divisor six times. A period with `num_required_events=0`, an
empty key string and no bookings makes it `0`. Reproduced:
`ZeroDivisionError: division by zero`. `UnsecuredHostDetailView` recomputes the
same percentages in the view and divides by `max_done_required` with the same
flaw.

Fix: `or 1` on the divisor, or return `0` early.

**Complexity: XS.**

### 15. Bare `except:` swallows every Google API error
`makerslink/scheduler/models.py:183,196,208`

```python
except:
    raise ValueError("Could not create event in calendar")
```

The original exception — quota, auth, bad calendar id — is discarded, and a
bare `except` also catches `KeyboardInterrupt` and `SystemExit`. Debugging a
calendar problem in production is impossible.

Fix: `except Exception as exc: raise ValueError(...) from exc`, and log
`exc_info=True`.

**Complexity: XS.**

### 16. `assign_unassigned` admin action raises `NameError`
`makerslink/scheduler/admin.py:25` — `modeladmin.message_user(...)` where
`modeladmin` is never defined; it should be `self`. Reproduced by invoking the
action with two periods selected: `NameError: name 'modeladmin' is not defined`.
The guard path is exactly the one that fires when a user mis-selects.

**Complexity: XS.**

### 17. `finish_registration` re-enters `save()` and can deactivate users
`makerslink/accounts/signals.py:8-18`

A `pre_save` receiver that calls `instance.save()` — the model is written twice
per registration, and any `update_fields` passed by the caller is silently
bypassed on the inner write.

The condition is `not is_registration_complete and instance.id and
instance._password`, which fires for **any** password change on a user whose
registration was never marked complete — for example an admin-created user
going through password reset. They are silently deactivated and have to be
re-approved. (Encountered while building the fixtures for this audit: users
created with `is_active=True` came back inactive and could not log in.)

`instance._password` is also a private Django attribute with no stability
guarantee.

Fix: set the attributes on `instance` and let the in-flight save persist them —
no inner `save()` — and scope the condition to the registration flow.

**Complexity: S.**

### 18. Container startup runs `makemigrations` and `runserver --insecure`
`makerslink/start-server.sh`

```sh
python3 ./manage.py makemigrations
python3 ./manage.py migrate
python3 ./manage.py runserver --insecure 0.0.0.0:8000
```

`makemigrations` in an entrypoint writes migration files into the container's
ephemeral layer and applies them, so schema history diverges from the
repository. This is live right now: `manage.py makemigrations --check` reports a
pending `0035_alter_schedulingcalendar_service_account`, which every container
start will regenerate and re-apply.

`runserver` is a development server — single-threaded, no process supervision —
and `--insecure` serves static files through it in `DEBUG=False`.

Fix: drop `makemigrations` from the entrypoint, commit the pending migration,
and serve with gunicorn/uvicorn + WhiteNoise.

**Complexity: S.**

### 19. Production security settings missing
`makerslink/makerslink/settings.py:22-41`

`manage.py check --deploy` under `DJANGO_ENV=prod` reports five issues:
`SECURE_HSTS_SECONDS` unset (W004), `SECURE_SSL_REDIRECT` not True (W008),
weak `SECRET_KEY` (W009), `SESSION_COOKIE_SECURE` not True (W012),
`CSRF_COOKIE_SECURE` not True (W016). The site is served over HTTPS
(`SECURE_PROXY_SSL_HEADER` is configured), so session and CSRF cookies are
currently allowed onto plaintext connections.

Also: the `else` branch is the default for anything that is not exactly
`DJANGO_ENV=prod`, and it sets `DEBUG = True`, `ALLOWED_HOSTS = ['*']` and a
hard-coded `SECRET_KEY` that is committed to a public repository. A missing or
mistyped env var is the difference between production settings and a debug
server that will echo tracebacks and settings to the internet. In prod,
`SECRET_KEY = os.environ.get("DJANGO_SECRET")` silently becomes `None` if unset.

Fix: default to the *secure* branch and opt in to development explicitly; read
the secret with a hard failure if absent; add the four cookie/transport
settings.

**Complexity: S.**

### 20. Booking race conditions
`makerslink/scheduler/views.py:355-435`

The take/release path does read-modify-write with no `select_for_update()` and
no `transaction.atomic()`. Two hosts submitting the same slot are separated only
by the unique constraint — and the handler for that case is finding 5's broken
signal. The "already taken by someone else" message is produced from
`form.errors`, in a branch that reads `form.cleaned_data.get('title')` after
validation failed; if the failure removed `title` from `cleaned_data`, the
concatenation raises `TypeError` on `None`.

Fix: wrap each form's handling in `transaction.atomic()` with
`select_for_update()`, and re-read status from the locked row.

**Complexity: M.**

---

## Low

**21. `{ message }}` typo.** `eventinstance_host_form.html:40` and
`eventinstanceadmin_list.html:38` — a missing brace, so every message *without*
the `safe` tag renders as the literal string `{ message }}`. **XS.**

**22. Duplicate URL pattern.** `scheduler/urls.py:78-81` registers
`joinEvent/<uuid:pk>` twice, for `EventInstanceUpdateView` and `EventDetailView`.
The second is unreachable, and `EventInstance.get_absolute_url()` →
`reverse('eventinstance-detail')` produces a URL that resolves to the *join*
view. `EventDetailView` is also bound to the wrong model for a UUID pk. **XS.**

**23. Self-XSS through `{{ message|safe }}`.** `views.py:391,413` build HTML from
`form.cleaned_data.get('title')` — `title` is a non-model `CharField` whose value
comes straight from the POST — and the message is emitted with
`extra_tags='safe'` into `{{ message|safe }}`. Scope is limited to the poster's
own session, so it is self-XSS rather than stored XSS. Fix: build the list as
data and render it with normal escaping (the template already has a disabled
`taken_events` block that does exactly this). **S.**

**24. Dead and broken-on-arrival code.** `EventManager.get_instances`
(`models.py:451-453`) calls `self.eventinstance_set` on a *manager* — it cannot
work, and the manager is never attached (`models.py:216` is commented out).
`views.index` is unrouted. `auto_add_check_unique_together`
(`signals/handlers.py:55`) reads `settings.DATABASE_ENGINE`, removed in Django
1.2. `caltest.py` is a scratch script with a hard-coded `/home/bobo/…` path and
a stray Django template fragment appended after `main()`. **XS.**

**25. `host_detail.html` context mismatches.** Line 29 regroups
`participant_list`, which the view never supplies (it provides
`participant_events`) — a silent no-op. Lines 221 and 256 then regroup *into*
`period_list`, shadowing the `period_list` the view supplies. **XS.**

**26. HTML ids from free-text names.** `eventinstance_list.html:26-28` builds
`id="collapse{{ template.grouper }}"` from an `EventTemplate` name. Any name with
a space or non-ASCII character produces an id the `href="#collapse…"` selector
cannot match, so the collapse silently stops working. Use the template pk. **S.**

**27. `HttpResponseRedirect('')`.** `views.py:436` emits an empty `Location`
header. Browsers resolve it to the current URL, but it is not valid per RFC 7231
and proxies may mishandle it. Use `request.path`. **XS.**

**28. Naive datetimes against aware fields.** `EventInstanceListView`,
`EventInstanceAdminListView` (`datetime.now().date()`) and
`SchedulingPeriod.get_host_stats` compare `date`/naive values against
`DateTimeField`s. Django emits `RuntimeWarning: … received a naive datetime
while time zone support is active` and interprets them in the process timezone —
correct today only because Django sets `TZ` from `TIME_ZONE`. Use
`django.utils.timezone.localdate()`. Related: `Event.get_event_list`
(`models.py:334-337`) relies on the same accident when it calls `.astimezone()`
on the result of `datetime.combine()`; it should use `tz.localize()`. **S.**

**29. `get_participant_key_list()` grouping (*unverified*).**
`models.py:824-840` annotates after `order_by()` on multi-valued relations, so
the `GROUP BY` includes the ordering columns. Two events with the same
`count_key`, period and host should collapse into one row, undercounting
participation; and `participantName` is aggregated without the period filter
that `keyChar` has. The accumulator also resets `resultList[user]` whenever a
user is seen non-consecutively. I did not build a dataset to confirm the
miscount — flagging for review rather than asserting it. **M.**

**30. Dependency hygiene.** `requirements.txt` is a raw `pip freeze`: it ships
`autopep8`, `pycodestyle`, `toml`, `beautifulsoup4` and `soupsieve` into
production, and pins `pytz==2020.1`, `httplib2==0.19.0`, `urllib3==1.26.7`,
`rsa==4.7` and `google-auth-oauthlib==0.4.1`. `django-crispy-forms==1.13.0`
predates Django 4 entirely; it still imports and renders under Django 6.0.2, but
that combination is untested by its authors, and the 2.x line moved the
bootstrap4 pack into a separate `crispy-bootstrap4` package, so the upgrade is
not a version bump alone. `stop-env.sh` regenerating `requirements.txt` from `pip freeze` on every
`deactivate` is what produced this. Use `requirements.in` + `pip-compile`, which
is already half set up. **M.**

**31. No tests.** `scheduler/tests.py` and `accounts/tests.py` are both the
`startapp` stub. Every finding above would have been caught by a handful of
tests. The recurrence/DST logic in `SchedulingRule.get_events` and
`Event.get_event_list` in particular is intricate, timezone-sensitive, and
currently unverifiable. **L**, but incremental — start with the reproductions in
this document. **XS** each.

---

## Suggested order of work

1. **#1** (XS) — nobody can register; one line.
2. **#4, #9, #11, #14, #16, #21** (all XS) — an afternoon, removes six failure modes.
3. **#10** (S) — stop committing the database, then plan the history rewrite.
4. **#6, #5, #7, #19** (S) — the empty-state 500s, the Python-2 signal, capacity, cookie flags.
5. **#3, #20** (M) — the signup-form trust problem and its race; do them together.
6. **#2** (M) — `PROTECT` migration plus null guards.
7. **#8, #13** (M) — performance and calendar-sync correctness.
8. **#31** (L) — grow the suite as each fix lands.

Findings #1–#11 are the ones that make the system visibly not work today.
