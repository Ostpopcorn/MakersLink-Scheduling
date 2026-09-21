# MakersLink-Scheduling
Project to develop a system to schecdule and book opening times at MakersLink

Rough roadmap:
1. Notification to email/slack
2. CSV-export?

## Login via MemberMatters (OpenID Connect)

Members can sign in with their MemberMatters account. MemberMatters ships an
OpenID Connect provider (django-oidc-provider, mounted at `/api/openid/`), and
this project acts as a relying party using django-allauth.

Password login keeps working alongside it, so existing accounts are not
disrupted.

### 1. Create the client in MemberMatters

In the MemberMatters admin, add an **OpenID Connect Provider Client**:

* Client type: `Confidential`
* Response type: `code (Authorization Code Flow)`
* Redirect URI: `https://<scheduling-host>/accounts/oidc/membermatters/login/callback/`

If MemberMatters has never signed a token before, generate its key first:

    python manage.py creatersakey

Note the generated Client ID and Client Secret.

### 2. Configure this application in Django admin

Apply the new tables first:

    python manage.py migrate

Then go to **Social applications** in Django admin (`/admin/socialaccount/socialapp/`)
and add one:

| Field | Value |
| --- | --- |
| Provider | `OpenID Connect` |
| Provider ID | `membermatters` |
| Name | `MemberMatters` (shown on the login button) |
| Client id | from step 1 |
| Secret key | from step 1 |
| Settings | see below |

The `Settings` field is JSON:

    {
      "server_url": "https://members.example.org/api/openid/",
      "scope": ["openid", "profile", "email", "membershipinfo"]
    }

`server_url` is the only required key: every endpoint is discovered from
`.well-known/openid-configuration` underneath it. Only identity scopes are
requested — see "Permissions and access" below.

The **Provider ID must stay stable** once people have linked their accounts.
It is what `SocialAccount.provider` stores, so changing it orphans every
existing link.

#### Configuring without the admin

A provider can also be supplied through environment variables, which is
convenient for bringing up a fresh deployment with no manual steps. Set all
three or none:

    -e "MEMBERMATTERS_SERVER_URL=https://members.example.org/api/openid/" \
    -e "MEMBERMATTERS_CLIENT_ID=$CLIENT_ID" \
    -e "MEMBERMATTERS_CLIENT_SECRET=$CLIENT_SECRET"

A provider configured in the admin always takes precedence over the environment
variables, so having both is safe.

If neither is configured, no provider is registered and the login page shows
only the password form.

### 3. Existing members link their account

Accounts are deliberately **not** matched on e-post address, because that would
let a provider take over any local account by asserting an address. Existing
members link their own account instead:

1. Log in with e-post and password as usual.
2. Open **Kopplade konton** in the menu.
3. Click **Koppla MemberMatters** and authenticate.

From then on either method signs them into the same account. Someone who tries
MemberMatters before linking is sent back to the login page with an explanation
rather than a signup form.

Staff can also map an account by hand in Django admin under
**Social accounts**: create a row with provider `membermatters` and the member's
MemberMatters `sub` as the UID. Note that the provider column holds
`membermatters` (the configured `provider_id`), not `openid_connect`.

### Adding another identity provider later

Nothing here is MemberMatters-specific beyond the configuration. A second
OpenID Connect provider is another **Social application** row in the admin,
with its own Provider ID and `server_url` -- no code change and no redeploy.
A provider that is not OpenID Connect (Slack, Google, GitHub) additionally
needs its `allauth.socialaccount.providers.*` app listed in `INSTALLED_APPS`.

Each provider is its own identity namespace, and a user can hold one linked
identity per provider.

## Permissions and access

**The provider establishes identity only.** No access flag (`is_active`,
`is_staff`, `is_superuser`) is ever set or cleared from provider claims. Who
may use the booking system, and who may administer it, stays a local decision
made in the Django admin.

In practice this means an account created by a first MemberMatters login is
inactive until someone approves it in the admin — exactly like an account
created through e-post registration. Linking an existing account never changes
what that account may already do.

MemberMatters does publish membership state today. Requesting its
`membershipinfo` scope returns `state`, `active`, `subscriptionState`,
`subscriptionActive`, `firstSubscribedDate` and `groups` (containing `staff`,
`admin`, `superuser` and `active`). That scope is deliberately **not**
requested, because the MemberMatters permission model is being reworked and
those claims are not yet a contract anything should depend on.

### If permissions should propagate later

The claim format belongs to **MemberMatters**, not to this client. It is the
single producer and there are several consumers (this project, Moodle,
Vikunja), so a format defined here would only ever be one client's private
guess at what the identity provider meant. Defining it once at the provider
keeps every consumer agreeing on the same vocabulary, and OpenID Connect
already has the mechanism for it: a named scope whose claims are documented
and versioned.

Worth fixing on the MemberMatters side when that work happens:

* **Namespace the claims.** `django-oidc-provider` flat-merges custom scope
  claims into the same object as the standard ones, so `active`, `state` and
  `groups` currently sit alongside registered claims like `email` and `sub`.
  A namespaced key (`https://membermatters.example/claims/membership`) cannot
  collide with a future standard claim or with another provider.
* **Publish stable machine identifiers**, not display names, for groups and
  roles, so renaming a role in the UI does not silently change access
  everywhere.
* **Version the scope**, so a breaking change is a new scope rather than a
  silent change of meaning in the existing one.

What stays here either way is the **policy**: the provider asserts facts
("this membership is active", "this person is in group X"); this project
decides what those facts permit. Roles do not mean the same thing across
applications — `staff` in MemberMatters is not `is_staff` here, which grants
edit access to calendars, templates and rules — so the mapping is local and
should fail closed, treating an unknown group as granting nothing and an
absent claim as changing nothing.

To deploy:
First on your local machine:
# Pull including current tags
git pull
# Make sure to get tags
git fetch --tags
# Look at old tags
git tag
# Invent a new version number
git tag $VERSION_NUM
# Push tag to github
git push origin $VERSION_NUM
# Build docker image locally
sudo docker build -t makerslink_scheduling .
# Save docker image
sudo docker save -o ./MakersLink-Scheduling-$VERSION_NUM.tar makerslink_scheduling:latest
# Change owner to your self
sudo chown me:me ./MakersLink-Scheduling-$VERSION_NUM.tar
# Push image to server
scp MakersLink-Scheduling-$VERSION_NUM.tar somebody@somewhere:/a/dir

Then on the server:
# Load docker image
sudo docker load -i MakersLink-Scheduling-$VERSION_NUM.tar
# Start with this command (on our server this is in the start_scheduling script).
sudo docker run -dt -e "DJANGO_SECRET=$SUPER_DUPER_SECRET" -e "SCHEDULING_PASS=$DIFFERENT_SECRET" --name scheduling --restart unless-stopped  -v /somewhere/db:/scheduling/makerslink/makerslink/db -v /somewhere/pks:/scheduling/pks -p 8000:8000 makerslink_scheduling
