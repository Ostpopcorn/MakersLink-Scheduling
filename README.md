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
      "scope": ["openid", "profile", "email"]
    }

`server_url` is the only required key: every endpoint is discovered from
`.well-known/openid-configuration` underneath it. Only identity scopes are
requested: the provider establishes who someone is, and nothing more.

The **Provider ID must stay stable** once people have linked their accounts.
It is what `SocialAccount.provider` stores, so changing it orphans every
existing link.

#### Configuring without the admin

A provider can also be supplied through environment variables, which is
convenient for bringing up a fresh deployment with no manual steps. The names
are not tied to any particular provider -- any OpenID Connect server works.
Set all three or none:

    -e "OIDC_SERVER_URL=https://members.example.org/api/openid/" \
    -e "OIDC_CLIENT_ID=$CLIENT_ID" \
    -e "OIDC_CLIENT_SECRET=$CLIENT_SECRET"

Two more are optional, and default to the MakersLink provider:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OIDC_PROVIDER_ID` | `membermatters` | URL slug and `SocialAccount.provider` value |
| `OIDC_PROVIDER_NAME` | `MemberMatters` | Label on the login button |

`OIDC_PROVIDER_ID` appears in the callback URL, so changing it means
registering a different redirect URI with the provider. The same stability
warning as above applies: changing it once people have linked orphans every
existing link.

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

### New members

A member without a local account who logs in with MemberMatters is stopped at
a short form before anything is created. Their e-post comes from MemberMatters
and cannot be changed there; their **Slacknamn** is prefilled with a guess from
their MemberMatters screen name (or the e-post local part, if they have none)
for them to confirm or correct. A name that is already taken is rejected on the
form. Submitting creates the account, which then waits for approval in the
admin like any other new account.

### Adding another identity provider later

Nothing here is MemberMatters-specific beyond the configuration. A second
OpenID Connect provider is another **Social application** row in the admin,
with its own Provider ID and `server_url` -- no code change and no redeploy.
A provider that is not OpenID Connect (Slack, Google, GitHub) additionally
needs its `allauth.socialaccount.providers.*` app listed in `INSTALLED_APPS`.

Each provider is its own identity namespace, and a user can hold one linked
identity per provider.

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
