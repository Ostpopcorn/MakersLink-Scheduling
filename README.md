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
`.well-known/openid-configuration` underneath it. The `membershipinfo` scope is
what carries the membership state, so keep it unless you do not want the sync.

The **Provider ID must be `membermatters`** — it is what `SocialAccount.provider`
stores and what the membership sync keys off.

Two optional keys control how much MemberMatters governs the local account:

| Key | Default | Description |
| --- | --- | --- |
| `sync_is_active` | `true` | Mirror the MemberMatters membership state onto `is_active` at every login, so a lapsed membership loses access to the booking system. |
| `sync_is_staff` | `false` | Grant local staff rights to MemberMatters staff/admin. Off by default, because staff here also means edit access to calendars, templates and rules. Superusers are never demoted. |

Changing either takes effect on the next login, without a redeploy.

#### Configuring without the admin

A provider can also be supplied through environment variables, which is
convenient for bringing up a fresh deployment with no manual steps. Set all
three or none:

    -e "MEMBERMATTERS_SERVER_URL=https://members.example.org/api/openid/" \
    -e "MEMBERMATTERS_CLIENT_ID=$CLIENT_ID" \
    -e "MEMBERMATTERS_CLIENT_SECRET=$CLIENT_SECRET"

Their defaults for the sync toggles are `MEMBERMATTERS_SYNC_IS_ACTIVE` and
`MEMBERMATTERS_SYNC_IS_STAFF`.

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
identity per provider. The membership sync described above is specific to
MemberMatters and is keyed on the `membermatters` Provider ID, so other
providers only supply the login.

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
